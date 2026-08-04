from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import re
import socket
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import SplitResult, urlsplit, urlunsplit
from xml.etree import ElementTree

from .database import Database
from .service import PocketError, json_loads, new_id, utc_now

MAX_FEED_BYTES = 1_048_576
MAX_XML_ELEMENTS = 3_000
MAX_XML_DEPTH = 32
MAX_FEED_ENTRIES = 100
MAX_FIELD_CHARS = 20_000
MAX_SUMMARY_CHARS = 4_000
FEED_BODY_TAGS = frozenset({"content", "description", "encoded", "summary"})
FETCH_TIMEOUT_SECONDS = 15.0
MAX_RESPONSE_HEADERS = 64
MAX_HEADER_NAME_CHARS = 64
MAX_HEADER_VALUE_CHARS = 4_096
MAX_ETAG_CHARS = 512
MAX_LAST_MODIFIED_CHARS = 200
MAX_DNS_JSON_BYTES = 64 * 1024
DNS_JSON_TIMEOUT_SECONDS = 8.0
DNS_JSON_ENDPOINT = "https://cloudflare-dns.com/dns-query"
BENCHMARK_FAKE_IP_NETWORK = ipaddress.IPv4Network("198.18.0.0/15")
ALLOWED_FEED_MIME_TYPES = frozenset(
    {
        "application/atom+xml",
        "application/rss+xml",
        "application/xml",
        "text/xml",
    }
)


@dataclass(frozen=True)
class FeedFetchRequest:
    url: str
    host: str
    port: int
    target: str
    resolved_ip: str
    headers: dict[str, str]
    timeout_seconds: float = FETCH_TIMEOUT_SECONDS
    max_bytes: int = MAX_FEED_BYTES


@dataclass(frozen=True)
class FeedFetchResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class FeedResolver(Protocol):
    def resolve(self, host: str, port: int) -> list[str]: ...


class FeedTransport(Protocol):
    def fetch(self, request: FeedFetchRequest) -> FeedFetchResponse: ...


class SystemFeedResolver:
    def resolve(self, host: str, port: int) -> list[str]:
        try:
            records = socket.getaddrinfo(
                host,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except OSError as error:
            raise PocketError(502, "可靠信源域名解析失败") from error
        result: list[str] = []
        for record in records:
            address = record[4][0]
            if address not in result:
                result.append(address)
        if not result:
            raise PocketError(502, "可靠信源域名没有可用地址")
        return result


class DnsJsonFeedResolver:
    def __init__(
        self,
        *,
        query_json: Callable[[str, str], Any] | None = None,
    ) -> None:
        self._query_json = query_json or self._query_cloudflare

    @staticmethod
    def _query_cloudflare(host: str, record_type: str) -> Any:
        query = urllib.parse.urlencode({"name": host, "type": record_type})
        request = urllib.request.Request(
            f"{DNS_JSON_ENDPOINT}?{query}",
            headers={
                "Accept": "application/dns-json",
                "User-Agent": "CentaurAI-Pocket/0.3",
            },
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=DNS_JSON_TIMEOUT_SECONDS,
            ) as response:
                if response.status != 200 or response.geturl() != request.full_url:
                    raise PocketError(502, "可靠信源安全 DNS 查询失败")
                content_type = response.headers.get_content_type().casefold()
                if content_type != "application/dns-json":
                    raise PocketError(502, "可靠信源安全 DNS 响应类型无效")
                body = response.read(MAX_DNS_JSON_BYTES + 1)
        except PocketError:
            raise
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            raise PocketError(502, "可靠信源安全 DNS 查询失败") from error
        if len(body) > MAX_DNS_JSON_BYTES:
            raise PocketError(502, "可靠信源安全 DNS 响应过大")
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PocketError(502, "可靠信源安全 DNS 响应格式无效") from error

    def resolve(self, host: str, port: int) -> list[str]:
        if port != 443:
            raise PocketError(502, "可靠信源安全 DNS 仅支持 HTTPS 默认端口")
        result: list[str] = []
        for record_type, expected_type in (("A", 1), ("AAAA", 28)):
            payload = self._query_json(host, record_type)
            if not isinstance(payload, dict) or payload.get("Status") != 0:
                raise PocketError(502, "可靠信源安全 DNS 响应状态无效")
            question = payload.get("Question")
            if not isinstance(question, list) or not question:
                raise PocketError(502, "可靠信源安全 DNS 响应缺少查询绑定")
            first_question = question[0]
            if (
                not isinstance(first_question, dict)
                or not isinstance(first_question.get("name"), str)
                or first_question["name"].rstrip(".").casefold() != host.casefold()
                or first_question.get("type") != expected_type
            ):
                raise PocketError(502, "可靠信源安全 DNS 响应查询绑定无效")
            answers = payload.get("Answer", [])
            if not isinstance(answers, list):
                raise PocketError(502, "可靠信源安全 DNS 响应记录无效")
            for answer in answers:
                if not isinstance(answer, dict) or answer.get("type") != expected_type:
                    continue
                value = answer.get("data")
                if not isinstance(value, str):
                    raise PocketError(502, "可靠信源安全 DNS 返回了无效地址")
                try:
                    address = ipaddress.ip_address(value)
                except ValueError as error:
                    raise PocketError(502, "可靠信源安全 DNS 返回了无效地址") from error
                normalized = address.compressed
                if normalized not in result:
                    result.append(normalized)
            if result:
                break
        if not result:
            raise PocketError(502, "可靠信源安全 DNS 没有可用地址")
        return result


class FakeIpFallbackFeedResolver:
    def __init__(self, primary: FeedResolver, fallback: FeedResolver) -> None:
        self.primary = primary
        self.fallback = fallback

    def resolve(self, host: str, port: int) -> list[str]:
        values = self.primary.resolve(host, port)
        addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        for value in values:
            try:
                addresses.append(ipaddress.ip_address(value))
            except ValueError:
                return values
        if addresses and all(
            isinstance(address, ipaddress.IPv4Address)
            and address in BENCHMARK_FAKE_IP_NETWORK
            for address in addresses
        ):
            return self.fallback.resolve(host, port)
        return values


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        *,
        port: int,
        resolved_ip: str,
        timeout: float,
        deadline: float,
    ) -> None:
        super().__init__(
            host,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._resolved_ip = resolved_ip
        self._deadline = deadline

    def connect(self) -> None:
        # Connect to the already validated address, while TLS SNI and
        # certificate verification continue to use the original hostname.
        remaining_seconds = self._deadline - time.monotonic()
        if remaining_seconds <= 0:
            raise TimeoutError("reliable feed absolute deadline reached")
        raw_socket = socket.create_connection(
            (self._resolved_ip, self.port),
            timeout=remaining_seconds,
        )
        try:
            remaining_seconds = self._deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise TimeoutError("reliable feed absolute deadline reached")
            raw_socket.settimeout(remaining_seconds)
            self.sock = raw_socket
            self.sock = self._context.wrap_socket(
                raw_socket,
                server_hostname=self.host,
            )
        except Exception:
            raw_socket.close()
            self.sock = None
            raise


class PinnedHTTPSFeedTransport:
    def fetch(self, request: FeedFetchRequest) -> FeedFetchResponse:
        deadline = time.monotonic() + request.timeout_seconds
        connection = _PinnedHTTPSConnection(
            request.host,
            port=request.port,
            resolved_ip=request.resolved_ip,
            timeout=request.timeout_seconds,
            deadline=deadline,
        )
        deadline_reached = threading.Event()

        def abort_at_deadline() -> None:
            deadline_reached.set()
            connected_socket = connection.sock
            if connected_socket is not None:
                try:
                    connected_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()

        timer = threading.Timer(request.timeout_seconds, abort_at_deadline)
        timer.daemon = True
        timer.start()
        try:
            connection.request(
                "GET",
                request.target,
                headers=request.headers,
            )
            if deadline_reached.is_set() or time.monotonic() >= deadline:
                raise PocketError(502, "可靠信源响应超过绝对时间上限")
            response = connection.getresponse()
            headers: dict[str, str] = {}
            for name, value in response.getheaders():
                normalized = name.casefold()
                headers[normalized] = (
                    f"{headers[normalized]}, {value}"
                    if normalized in headers
                    else value
                )
            headers = _validated_response_headers(headers)
            content_length = headers.get("content-length")
            if content_length:
                try:
                    declared_length = int(content_length)
                except ValueError as error:
                    raise PocketError(502, "可靠信源响应长度无效") from error
                if declared_length < 0 or declared_length > request.max_bytes:
                    raise PocketError(502, "可靠信源响应超过大小上限")
            chunks: list[bytes] = []
            received = 0
            while True:
                remaining_seconds = deadline - time.monotonic()
                if deadline_reached.is_set() or remaining_seconds <= 0:
                    raise PocketError(502, "可靠信源响应超过绝对时间上限")
                if connection.sock is not None:
                    connection.sock.settimeout(remaining_seconds)
                chunk = response.read(min(65_536, request.max_bytes + 1 - received))
                if deadline_reached.is_set() or time.monotonic() >= deadline:
                    raise PocketError(502, "可靠信源响应超过绝对时间上限")
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
                if received > request.max_bytes:
                    raise PocketError(502, "可靠信源响应超过大小上限")
            return FeedFetchResponse(response.status, headers, b"".join(chunks))
        except PocketError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as error:
            if deadline_reached.is_set() or time.monotonic() >= deadline:
                raise PocketError(
                    502,
                    "可靠信源响应超过绝对时间上限",
                ) from error
            raise PocketError(502, "可靠信源连接失败") from error
        finally:
            timer.cancel()
            connection.close()


@dataclass(frozen=True)
class ParsedFeedEntry:
    identity_hint: str
    title: str
    summary: str
    url: str
    url_trust: str
    publisher: str
    published_at: str | None
    evidence: list[dict[str, Any]]


class _PlainTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(
        self,
        tag: str,
        _attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() in {"script", "style", "template"}:
            self.ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "template"} and self.ignored_depth:
            self.ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def _plain_text(value: str, *, limit: int) -> str:
    parser = _PlainTextExtractor()
    parser.feed(value)
    parser.close()
    result = " ".join(parser.parts)
    result = re.sub(r"\s+", " ", result).strip()
    return result[:limit]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _element_text(element: ElementTree.Element | None, *, limit: int) -> str:
    if element is None:
        return ""
    raw = "".join(element.itertext())
    return _plain_text(raw, limit=limit)


def _first_child(
    element: ElementTree.Element,
    *names: str,
) -> ElementTree.Element | None:
    accepted = {name.casefold() for name in names}
    return next(
        (child for child in element if _local_name(child.tag) in accepted),
        None,
    )


def _children(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    target = name.casefold()
    return [child for child in element if _local_name(child.tag) == target]


def _normalize_date(value: str) -> str | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    parsed: datetime
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(cleaned)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _evidence_points(title: str, summary: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if title:
        result.append(
            {
                "field": "title",
                "start_offset": 0,
                "end_offset": len(title),
                "offset_unit": "unicode_code_points",
                "excerpt": title[:240],
            }
        )
    if summary:
        spans = [
            match
            for match in re.finditer(r"[^。！？.!?]+[。！？.!?]?", summary)
            if match.group(0).strip()
        ]
        for match in spans[:3]:
            excerpt = match.group(0).strip()
            leading = len(match.group(0)) - len(match.group(0).lstrip())
            start = match.start() + leading
            result.append(
                {
                    "field": "summary",
                    "start_offset": start,
                    "end_offset": start + len(excerpt),
                    "offset_unit": "unicode_code_points",
                    "excerpt": excerpt[:240],
                }
            )
    return result


def _validate_xml_shape(root: ElementTree.Element) -> None:
    count = 0
    stack: list[tuple[ElementTree.Element, int]] = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        count += 1
        if count > MAX_XML_ELEMENTS:
            raise PocketError(502, "可靠信源 XML 元素过多")
        if depth > MAX_XML_DEPTH:
            raise PocketError(502, "可靠信源 XML 嵌套过深")
        text_limit = (
            MAX_FEED_BYTES
            if _local_name(element.tag) in FEED_BODY_TAGS
            else MAX_FIELD_CHARS
        )
        if element.text and len(element.text) > text_limit:
            raise PocketError(502, "可靠信源 XML 字段过长")
        if element.tail and len(element.tail) > MAX_FIELD_CHARS:
            raise PocketError(502, "可靠信源 XML 字段过长")
        stack.extend((child, depth + 1) for child in element)


def parse_feed(
    raw: bytes,
    *,
    feed_url: str,
    display_name: str,
) -> list[ParsedFeedEntry]:
    if b"\x00" in raw:
        raise PocketError(502, "首版可靠信源只接受 UTF-8 XML")
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise PocketError(502, "首版可靠信源只接受 UTF-8 XML") from error
    declaration = re.match(r"\s*<\?xml\s+([^?]+)\?>", decoded, flags=re.IGNORECASE)
    if declaration:
        encoding = re.search(
            r"\bencoding\s*=\s*(['\"])([^'\"]+)\1",
            declaration.group(1),
            flags=re.IGNORECASE,
        )
        if encoding and encoding.group(2).replace("_", "-").casefold() not in {
            "utf-8",
            "utf8",
        }:
            raise PocketError(502, "首版可靠信源只接受 UTF-8 XML declaration")
    if _contains_forbidden_xml_declaration(decoded):
        raise PocketError(502, "可靠信源 XML 禁止 DOCTYPE 或 ENTITY")
    try:
        root = ElementTree.fromstring(decoded)
    except ElementTree.ParseError as error:
        raise PocketError(502, "可靠信源不是有效的 RSS/Atom XML") from error
    _validate_xml_shape(root)
    root_name = _local_name(root.tag)
    if root_name == "rss":
        channel = _first_child(root, "channel")
        if channel is None:
            raise PocketError(502, "RSS 缺少 channel")
        raw_entries = _children(channel, "item")
        flavor = "rss"
    elif root_name == "feed":
        raw_entries = _children(root, "entry")
        flavor = "atom"
    elif root_name == "rdf":
        raw_entries = _children(root, "item")
        flavor = "rss"
    else:
        raise PocketError(502, "响应不是 RSS 或 Atom feed")
    if len(raw_entries) > MAX_FEED_ENTRIES:
        raise PocketError(502, "可靠信源条目数超过上限")

    result: list[ParsedFeedEntry] = []
    for element in raw_entries:
        title = _element_text(_first_child(element, "title"), limit=500)
        if flavor == "atom":
            summary = _element_text(
                _first_child(element, "summary", "content"),
                limit=MAX_SUMMARY_CHARS,
            )
            identity_hint = _element_text(_first_child(element, "id"), limit=2048)
            link = ""
            for link_element in _children(element, "link"):
                relation = link_element.attrib.get("rel", "alternate").casefold()
                href = link_element.attrib.get("href", "").strip()
                if href and relation in {"", "alternate"}:
                    link = href
                    break
            published = _element_text(
                _first_child(element, "published", "updated"),
                limit=200,
            )
        else:
            summary = _element_text(
                _first_child(element, "description", "encoded", "content"),
                limit=MAX_SUMMARY_CHARS,
            )
            identity_hint = _element_text(_first_child(element, "guid"), limit=2048)
            link = _element_text(_first_child(element, "link"), limit=2048)
            published = _element_text(
                _first_child(element, "pubdate", "date"),
                limit=200,
            )
        title = title or summary[:120] or "未命名资讯"
        safe_url, url_trust = safe_entry_url(link, feed_url=feed_url)
        identity = identity_hint
        if not identity and url_trust == "feed_claimed_unverified":
            identity = safe_url
        if not identity:
            identity = hashlib.sha256(
                f"{title}\0{published}".encode()
            ).hexdigest()
        result.append(
            ParsedFeedEntry(
                identity_hint=identity,
                title=title,
                summary=summary,
                url=safe_url,
                url_trust=url_trust,
                # Remote author/channel labels are untrusted feed claims and
                # must not be allowed to impersonate the owner-approved source.
                publisher=display_name,
                published_at=_normalize_date(published),
                evidence=_evidence_points(title, summary),
            )
        )
    return result


def _contains_forbidden_xml_declaration(value: str) -> bool:
    position = 0
    while position < len(value):
        opening = value.find("<!", position)
        if opening < 0:
            return False
        if value.startswith("<![CDATA[", opening):
            closing = value.find("]]>", opening + 9)
            if closing < 0:
                return False
            position = closing + 3
            continue
        if value.startswith("<!--", opening):
            closing = value.find("-->", opening + 4)
            if closing < 0:
                return False
            position = closing + 3
            continue
        marker = value[opening : opening + 10].casefold()
        if marker.startswith(("<!doctype", "<!entity")):
            return True
        position = opening + 2
    return False


def _canonical_https_url(value: str) -> tuple[str, SplitResult]:
    raw_value = value.strip()
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in raw_value
    ):
        raise PocketError(422, "可靠信源 URL 包含非法空白或控制字符")
    try:
        parsed = urlsplit(raw_value)
        port = parsed.port
    except ValueError as error:
        raise PocketError(422, "feed_url 格式无效") from error
    if parsed.scheme.casefold() != "https":
        raise PocketError(422, "可靠信源只允许 HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise PocketError(422, "可靠信源 URL 不能包含 userinfo")
    if port not in {None, 443}:
        raise PocketError(422, "可靠信源只允许 HTTPS 默认端口 443")
    if parsed.fragment:
        raise PocketError(422, "可靠信源 URL 不能包含 fragment")
    raw_host = parsed.hostname
    if not raw_host:
        raise PocketError(422, "可靠信源 URL 缺少域名")
    try:
        ipaddress.ip_address(raw_host)
    except ValueError:
        pass
    else:
        raise PocketError(422, "可靠信源 URL 不能使用 IP literal")
    try:
        host = raw_host.rstrip(".").encode("idna").decode("ascii").casefold()
    except UnicodeError as error:
        raise PocketError(422, "可靠信源域名无效") from error
    if (
        not host
        or len(host) > 253
        or "." not in host
        or host == "localhost"
        or host.endswith((".localhost", ".local", ".internal"))
        or not re.fullmatch(r"[a-z0-9.-]+", host)
    ):
        raise PocketError(422, "可靠信源必须使用公开域名")
    path = parsed.path or "/"
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in path
    ):
        raise PocketError(422, "可靠信源 URL 包含非法字符")
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in parsed.query
    ):
        raise PocketError(422, "可靠信源 URL query 包含非法字符")
    canonical = urlunsplit(("https", host, path, parsed.query, ""))
    return canonical, urlsplit(canonical)


def canonical_feed_url(value: str) -> str:
    return _canonical_https_url(value)[0]


def safe_entry_url(value: str, *, feed_url: str) -> tuple[str, str]:
    if not value.strip():
        return feed_url, "feed_url_fallback_missing"
    try:
        canonical, _parsed = _canonical_https_url(value)
    except PocketError:
        return feed_url, "feed_url_fallback_invalid"
    # This remains an unverified claim from the feed. It is retained for
    # provenance but is never fetched, redirected to, or executed by Pocket.
    return canonical, "feed_claimed_unverified"


def _validated_response_headers(values: dict[str, str]) -> dict[str, str]:
    if len(values) > MAX_RESPONSE_HEADERS:
        raise PocketError(502, "可靠信源响应 header 数量超过上限")
    result: dict[str, str] = {}
    for raw_name, raw_value in values.items():
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            raise PocketError(502, "可靠信源响应 header 格式无效")
        name = raw_name.casefold()
        if (
            not name
            or len(name) > MAX_HEADER_NAME_CHARS
            or not re.fullmatch(r"[a-z0-9!#$%&'*+.^_`|~-]+", name)
        ):
            raise PocketError(502, "可靠信源响应 header 名称无效")
        if len(raw_value) > MAX_HEADER_VALUE_CHARS or any(
            ord(character) < 32 or ord(character) == 127
            for character in raw_value
        ):
            raise PocketError(502, "可靠信源响应 header 值无效或过长")
        result[name] = raw_value
    return result


def _bounded_metadata_header(
    value: str | None,
    *,
    field: str,
    max_chars: int,
) -> str | None:
    if value is None:
        return None
    if len(value) > max_chars:
        raise PocketError(502, f"可靠信源 {field} 超过长度上限")
    return value


def _public_resolved_ips(resolver: FeedResolver, host: str, port: int) -> list[str]:
    values = resolver.resolve(host, port)
    result: list[str] = []
    for value in values:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as error:
            raise PocketError(502, "可靠信源 DNS 返回了无效地址") from error
        embedded_ipv4: list[ipaddress.IPv4Address] = []
        has_scope_id = False
        if isinstance(address, ipaddress.IPv6Address):
            has_scope_id = address.scope_id is not None
            if address.ipv4_mapped is not None:
                embedded_ipv4.append(address.ipv4_mapped)
            if address.sixtofour is not None:
                embedded_ipv4.append(address.sixtofour)
            if address.teredo is not None:
                embedded_ipv4.extend(address.teredo)
            well_known_nat64 = ipaddress.IPv6Network("64:ff9b::/96")
            local_nat64 = ipaddress.IPv6Network("64:ff9b:1::/48")
            if address in well_known_nat64:
                embedded_ipv4.append(ipaddress.IPv4Address(address.packed[-4:]))
            elif address in local_nat64:
                embedded_ipv4.append(
                    ipaddress.IPv4Address(address.packed[6:8] + address.packed[9:11])
                )
        embedded_is_unsafe = any(
            not embedded.is_global
            or embedded.is_private
            or embedded.is_loopback
            or embedded.is_link_local
            or embedded.is_reserved
            or embedded.is_multicast
            or embedded.is_unspecified
            for embedded in embedded_ipv4
        )
        if (
            not address.is_global
            or address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
            or embedded_is_unsafe
            or has_scope_id
        ):
            raise PocketError(403, "可靠信源 DNS 指向非公网地址")
        normalized = address.compressed
        if normalized not in result:
            result.append(normalized)
    if not result:
        raise PocketError(502, "可靠信源域名没有可用公网地址")
    return result


def _payload_hash(payload: dict[str, Any]) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class ReliableSourceService:
    def __init__(
        self,
        database: Database,
        *,
        resolver: FeedResolver | None = None,
        transport: FeedTransport | None = None,
    ) -> None:
        self.database = database
        self.resolver = resolver or FakeIpFallbackFeedResolver(
            SystemFeedResolver(),
            DnsJsonFeedResolver(),
        )
        self.transport = transport or PinnedHTTPSFeedTransport()

    def set_network(
        self,
        *,
        resolver: FeedResolver,
        transport: FeedTransport,
    ) -> None:
        self.resolver = resolver
        self.transport = transport

    @staticmethod
    def _candidate(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "display_name": row["display_name"],
            "organization_origin": row["organization_origin"],
            "feed_url": row["feed_url"],
            "trust_reason": row["trust_reason"],
            "scope": row["scope"],
            "review_due_at": row["review_due_at"],
            "status": row["status"],
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "confirmed_at": row["confirmed_at"],
            "dismissed_at": row["dismissed_at"],
            "dismiss_reason": row["dismiss_reason"],
            "reliable_source_id": row["reliable_source_id"],
        }

    @staticmethod
    def _source(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "source_id": row["source_id"],
            "display_name": row["display_name"],
            "organization_origin": row["organization_origin"],
            "feed_url": row["feed_url"],
            "trust_reason": row["trust_reason"],
            "scope": row["scope"],
            "status": row["status"],
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_collected_at": row["last_collected_at"],
        }

    @staticmethod
    def _plan(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "reliable_source_id": row["reliable_source_id"],
            "schedule": row["schedule"],
            "enabled": bool(row["enabled"]),
            "review_due_at": row["review_due_at"],
            "version": row["version"],
            "last_collected_at": row["last_collected_at"],
            "next_run_at": row["next_run_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "reliable_source_id": row["reliable_source_id"],
            "status": row["status"],
            "request_url": row["request_url"],
            "http_status": row["http_status"],
            "content_hash": row["content_hash"],
            "etag": row["etag"],
            "last_modified": row["last_modified"],
            "byte_count": row["byte_count"],
            "entry_count": row["entry_count"],
            "new_entry_count": row["new_entry_count"],
            "changed_entry_count": row["changed_entry_count"],
            "duplicate_entry_count": row["duplicate_entry_count"],
            "collected_at": row["collected_at"],
        }

    @staticmethod
    def _get_idempotent(
        connection: sqlite3.Connection,
        *,
        actor_id: str,
        operation: str,
        idempotency_key: str,
        payload_hash: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT payload_hash, response_json
            FROM reliable_domain_idempotency
            WHERE actor_id = ? AND operation = ? AND idempotency_key = ?
            """,
            (actor_id, operation, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["payload_hash"] != payload_hash:
            raise PocketError(409, "同一 Idempotency-Key 不能绑定不同请求")
        response = json_loads(row["response_json"], {})
        cached_error = response.get("__pocket_error__")
        if isinstance(cached_error, dict):
            status_code = cached_error.get("status_code")
            detail = cached_error.get("detail")
            if isinstance(status_code, int) and isinstance(detail, str):
                raise PocketError(status_code, detail)
            raise PocketError(500, "可靠信源幂等错误记录无效")
        return response

    @staticmethod
    def _put_idempotent(
        connection: sqlite3.Connection,
        *,
        actor_id: str,
        operation: str,
        idempotency_key: str,
        payload_hash: str,
        response: dict[str, Any],
    ) -> None:
        try:
            connection.execute(
                """
                INSERT INTO reliable_domain_idempotency(
                    actor_id, operation, idempotency_key, payload_hash,
                    response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    actor_id,
                    operation,
                    idempotency_key,
                    payload_hash,
                    json.dumps(response, ensure_ascii=False),
                    utc_now(),
                ),
            )
        except sqlite3.IntegrityError:
            cached = ReliableSourceService._get_idempotent(
                connection,
                actor_id=actor_id,
                operation=operation,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
            )
            if cached != response:
                raise PocketError(409, "幂等请求发生并发冲突") from None

    @staticmethod
    def _assert_version(current: int, expected: int) -> None:
        if current != expected:
            raise PocketError(412, "资源版本已变化，请刷新后重试")

    def list_candidates(self, status: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM reliable_source_candidates
                WHERE status = ? ORDER BY created_at DESC, id DESC
                """,
                (status,),
            ).fetchall()
        return {"items": [self._candidate(row) for row in rows], "total": len(rows)}

    def create_candidate(
        self,
        payload: dict[str, Any],
        *,
        actor_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        values = dict(payload)
        values["feed_url"] = canonical_feed_url(values["feed_url"])
        request_hash = _payload_hash(values)
        operation = "reliable-candidate:create"
        with self.database.transaction() as connection:
            cached = self._get_idempotent(
                connection,
                actor_id=actor_id,
                operation=operation,
                idempotency_key=idempotency_key,
                payload_hash=request_hash,
            )
            if cached is not None:
                return cached
            candidate_id = new_id("rscand")
            now = utc_now()
            connection.execute(
                """
                INSERT INTO reliable_source_candidates(
                    id, display_name, organization_origin, feed_url,
                    trust_reason, scope, review_due_at,
                    created_by_device_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    values["display_name"],
                    values["organization_origin"],
                    values["feed_url"],
                    values["trust_reason"],
                    values["scope"],
                    values.get("review_due_at"),
                    actor_id,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM reliable_source_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            assert row is not None
            response = self._candidate(row)
            self._put_idempotent(
                connection,
                actor_id=actor_id,
                operation=operation,
                idempotency_key=idempotency_key,
                payload_hash=request_hash,
                response=response,
            )
            return response

    def confirm_candidate(
        self,
        candidate_id: str,
        payload: dict[str, Any],
        *,
        expected_version: int,
        actor_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        request_values = {**payload, "if_match": expected_version}
        request_hash = _payload_hash(request_values)
        operation = f"reliable-candidate:{candidate_id}:confirm"
        with self.database.transaction() as connection:
            cached = self._get_idempotent(
                connection,
                actor_id=actor_id,
                operation=operation,
                idempotency_key=idempotency_key,
                payload_hash=request_hash,
            )
            if cached is not None:
                return cached
            row = connection.execute(
                "SELECT * FROM reliable_source_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise PocketError(404, "可靠信源候选不存在")
            self._assert_version(row["version"], expected_version)
            self._assert_version(row["version"], payload["expected_version"])
            if row["status"] != "pending":
                raise PocketError(409, "只有待确认候选可以确认")
            existing = connection.execute(
                "SELECT id FROM reliable_sources WHERE feed_url = ?",
                (row["feed_url"],),
            ).fetchone()
            if existing is not None:
                raise PocketError(409, "该 feed_url 已在可靠信源清单中")
            now = utc_now()
            source_id = new_id("src")
            reliable_source_id = new_id("rsrc")
            plan_id = new_id("rplan")
            source_config = {
                "feed_url": row["feed_url"],
                "reliable_source_id": reliable_source_id,
            }
            connection.execute(
                """
                INSERT INTO sources(
                    id, kind, provider, name, config_json, schedule, enabled,
                    created_at, updated_at
                ) VALUES (?, 'rss', 'rss', ?, ?, ?, 1, ?, ?)
                """,
                (
                    source_id,
                    row["display_name"],
                    json.dumps(source_config, ensure_ascii=False),
                    payload["schedule"],
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO reliable_sources(
                    id, source_id, candidate_id, display_name,
                    organization_origin, feed_url, trust_reason, scope,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reliable_source_id,
                    source_id,
                    candidate_id,
                    row["display_name"],
                    row["organization_origin"],
                    row["feed_url"],
                    row["trust_reason"],
                    row["scope"],
                    now,
                    now,
                ),
            )
            next_run = now if payload["schedule"] == "daily" else None
            connection.execute(
                """
                INSERT INTO reliable_collection_plans(
                    id, reliable_source_id, schedule, enabled,
                    review_due_at, next_run_at, created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    reliable_source_id,
                    payload["schedule"],
                    row["review_due_at"],
                    next_run,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE reliable_source_candidates
                SET status = 'confirmed', version = version + 1,
                    confirmed_at = ?, reliable_source_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, reliable_source_id, now, candidate_id),
            )
            candidate_row = connection.execute(
                "SELECT * FROM reliable_source_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            source_row = connection.execute(
                "SELECT * FROM reliable_sources WHERE id = ?",
                (reliable_source_id,),
            ).fetchone()
            plan_row = connection.execute(
                "SELECT * FROM reliable_collection_plans WHERE id = ?",
                (plan_id,),
            ).fetchone()
            assert candidate_row is not None and source_row is not None and plan_row is not None
            response = {
                "candidate": self._candidate(candidate_row),
                "reliable_source": self._source(source_row),
                "collection_plan": self._plan(plan_row),
            }
            self._put_idempotent(
                connection,
                actor_id=actor_id,
                operation=operation,
                idempotency_key=idempotency_key,
                payload_hash=request_hash,
                response=response,
            )
            return response

    def dismiss_candidate(
        self,
        candidate_id: str,
        payload: dict[str, Any],
        *,
        expected_version: int,
        actor_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        request_hash = _payload_hash({**payload, "if_match": expected_version})
        operation = f"reliable-candidate:{candidate_id}:dismiss"
        with self.database.transaction() as connection:
            cached = self._get_idempotent(
                connection,
                actor_id=actor_id,
                operation=operation,
                idempotency_key=idempotency_key,
                payload_hash=request_hash,
            )
            if cached is not None:
                return cached
            row = connection.execute(
                "SELECT * FROM reliable_source_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise PocketError(404, "可靠信源候选不存在")
            self._assert_version(row["version"], expected_version)
            self._assert_version(row["version"], payload["expected_version"])
            if row["status"] != "pending":
                raise PocketError(409, "只有待确认候选可以驳回")
            now = utc_now()
            connection.execute(
                """
                UPDATE reliable_source_candidates
                SET status = 'dismissed', version = version + 1,
                    dismissed_at = ?, dismiss_reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, payload["reason"], now, candidate_id),
            )
            updated = connection.execute(
                "SELECT * FROM reliable_source_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            assert updated is not None
            response = self._candidate(updated)
            self._put_idempotent(
                connection,
                actor_id=actor_id,
                operation=operation,
                idempotency_key=idempotency_key,
                payload_hash=request_hash,
                response=response,
            )
            return response

    def list_sources(self) -> dict[str, Any]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reliable_sources ORDER BY created_at DESC, id DESC"
            ).fetchall()
        return {"items": [self._source(row) for row in rows], "total": len(rows)}

    def get_plan(self, reliable_source_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM reliable_collection_plans
                WHERE reliable_source_id = ?
                """,
                (reliable_source_id,),
            ).fetchone()
        if row is None:
            raise PocketError(404, "可靠信源采集计划不存在")
        return self._plan(row)

    def update_plan(
        self,
        reliable_source_id: str,
        payload: dict[str, Any],
        *,
        expected_version: int,
        actor_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        request_hash = _payload_hash({**payload, "if_match": expected_version})
        operation = f"reliable-source:{reliable_source_id}:plan:update"
        with self.database.transaction() as connection:
            cached = self._get_idempotent(
                connection,
                actor_id=actor_id,
                operation=operation,
                idempotency_key=idempotency_key,
                payload_hash=request_hash,
            )
            if cached is not None:
                return cached
            row = connection.execute(
                """
                SELECT * FROM reliable_collection_plans
                WHERE reliable_source_id = ?
                """,
                (reliable_source_id,),
            ).fetchone()
            if row is None:
                raise PocketError(404, "可靠信源采集计划不存在")
            self._assert_version(row["version"], expected_version)
            schedule = payload.get("schedule", row["schedule"])
            enabled = payload.get("enabled", bool(row["enabled"]))
            review_due_at = (
                payload["review_due_at"]
                if "review_due_at" in payload
                else row["review_due_at"]
            )
            now = utc_now()
            next_run_at = row["next_run_at"]
            if not enabled or schedule == "manual":
                next_run_at = None
            elif row["schedule"] != "daily" or not row["enabled"]:
                next_run_at = now
            connection.execute(
                """
                UPDATE reliable_collection_plans
                SET schedule = ?, enabled = ?, review_due_at = ?,
                    next_run_at = ?, version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (
                    schedule,
                    int(enabled),
                    review_due_at,
                    next_run_at,
                    now,
                    row["id"],
                ),
            )
            connection.execute(
                """
                UPDATE sources
                SET schedule = ?, enabled = ?, updated_at = ?
                WHERE id = (
                    SELECT source_id FROM reliable_sources WHERE id = ?
                )
                """,
                (schedule, int(enabled), now, reliable_source_id),
            )
            updated = connection.execute(
                "SELECT * FROM reliable_collection_plans WHERE id = ?",
                (row["id"],),
            ).fetchone()
            assert updated is not None
            response = self._plan(updated)
            self._put_idempotent(
                connection,
                actor_id=actor_id,
                operation=operation,
                idempotency_key=idempotency_key,
                payload_hash=request_hash,
                response=response,
            )
            return response

    def _source_row(self, reliable_source_id: str) -> sqlite3.Row:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM reliable_sources WHERE id = ?",
                (reliable_source_id,),
            ).fetchone()
        if row is None:
            raise PocketError(404, "可靠信源不存在")
        return row

    def collect(
        self,
        reliable_source_id: str,
        *,
        expected_version: int,
        actor_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        request_values = {
            "expected_version": expected_version,
            "reliable_source_id": reliable_source_id,
        }
        request_hash = _payload_hash(request_values)
        operation = f"reliable-source:{reliable_source_id}:collect"
        with self.database.connect() as connection:
            cached = self._get_idempotent(
                connection,
                actor_id=actor_id,
                operation=operation,
                idempotency_key=idempotency_key,
                payload_hash=request_hash,
            )
            if cached is not None:
                return cached
        source = self._source_row(reliable_source_id)
        self._assert_version(source["version"], expected_version)
        canonical_url, parsed = _canonical_https_url(source["feed_url"])
        resolved_ip = "unresolved"
        try:
            resolved_ips = _public_resolved_ips(
                self.resolver,
                parsed.hostname or "",
                443,
            )
            resolved_ip = resolved_ips[0]
            conditional_headers: dict[str, str] = {}
            with self.database.connect() as connection:
                previous = connection.execute(
                    """
                    SELECT * FROM reliable_feed_snapshots
                    WHERE reliable_source_id = ?
                      AND status IN ('completed', 'not_modified')
                    ORDER BY collected_at DESC, rowid DESC LIMIT 1
                    """,
                    (reliable_source_id,),
                ).fetchone()
            if previous is not None:
                previous_etag = _bounded_metadata_header(
                    previous["etag"],
                    field="ETag",
                    max_chars=MAX_ETAG_CHARS,
                )
                previous_modified = _bounded_metadata_header(
                    previous["last_modified"],
                    field="Last-Modified",
                    max_chars=MAX_LAST_MODIFIED_CHARS,
                )
                if previous_etag:
                    conditional_headers["If-None-Match"] = previous_etag
                if previous_modified:
                    conditional_headers["If-Modified-Since"] = previous_modified
            target = parsed.path or "/"
            if parsed.query:
                target = f"{target}?{parsed.query}"
            headers = {
                "Host": parsed.hostname or "",
                "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8",
                "Accept-Encoding": "identity",
                "User-Agent": "CentaurAI-Pocket-ReliableFeed/1",
                "Connection": "close",
                **conditional_headers,
            }
            fetch_request = FeedFetchRequest(
                url=canonical_url,
                host=parsed.hostname or "",
                port=443,
                target=target,
                resolved_ip=resolved_ip,
                headers=headers,
            )
            fetched = self.transport.fetch(fetch_request)
            return self._persist_collection(
                source,
                expected_version=expected_version,
                fetched=fetched,
                resolved_ip=resolved_ip,
                actor_id=actor_id,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
        except PocketError as error:
            if error.status_code in {403, 502}:
                self._record_failed_snapshot(
                    reliable_source_id,
                    canonical_url,
                    resolved_ip,
                    error_code=f"collection_{error.status_code}",
                )
                with self.database.transaction() as connection:
                    self._put_idempotent(
                        connection,
                        actor_id=actor_id,
                        operation=operation,
                        idempotency_key=idempotency_key,
                        payload_hash=request_hash,
                        response={
                            "__pocket_error__": {
                                "status_code": error.status_code,
                                "detail": error.detail[:500],
                            }
                        },
                    )
            raise

    def _persist_collection(
        self,
        source: sqlite3.Row,
        *,
        expected_version: int,
        fetched: FeedFetchResponse,
        resolved_ip: str,
        actor_id: str,
        operation: str,
        idempotency_key: str,
        request_hash: str,
    ) -> dict[str, Any]:
        headers = _validated_response_headers(fetched.headers)
        if 300 <= fetched.status < 400 and fetched.status != 304:
            raise PocketError(502, "可靠信源禁止 HTTP 重定向")
        if fetched.status not in {200, 304}:
            raise PocketError(502, "可靠信源返回了不支持的 HTTP 状态")
        encoding = headers.get("content-encoding", "identity").strip().casefold()
        if encoding not in {"", "identity"}:
            raise PocketError(502, "可靠信源响应不能使用压缩编码")
        transfer_encoding = headers.get("transfer-encoding", "").strip().casefold()
        if transfer_encoding not in {"", "identity", "chunked"}:
            raise PocketError(502, "可靠信源响应不能使用压缩传输编码")
        if len(fetched.body) > MAX_FEED_BYTES:
            raise PocketError(502, "可靠信源响应超过大小上限")
        if fetched.status == 304:
            if fetched.body:
                raise PocketError(502, "304 响应不能携带 feed body")
            entries: list[ParsedFeedEntry] = []
            feed_hash = None
        else:
            raw_content_type = headers.get("content-type", "")
            content_type = raw_content_type.split(";", 1)[0].strip().casefold()
            if content_type not in ALLOWED_FEED_MIME_TYPES:
                raise PocketError(502, "可靠信源响应 MIME 不是 RSS/Atom XML")
            charset = re.search(
                r"(?:^|;)\s*charset\s*=\s*['\"]?([^;'\"\s]+)",
                raw_content_type,
                flags=re.IGNORECASE,
            )
            if charset and charset.group(1).replace("_", "-").casefold() not in {
                "utf-8",
                "utf8",
            }:
                raise PocketError(502, "首版可靠信源只接受 UTF-8 MIME charset")
            feed_hash = hashlib.sha256(fetched.body).hexdigest()
            entries = parse_feed(
                fetched.body,
                feed_url=source["feed_url"],
                display_name=source["display_name"],
            )

        with self.database.transaction() as connection:
            cached = self._get_idempotent(
                connection,
                actor_id=actor_id,
                operation=operation,
                idempotency_key=idempotency_key,
                payload_hash=request_hash,
            )
            if cached is not None:
                return cached
            current = connection.execute(
                "SELECT * FROM reliable_sources WHERE id = ?",
                (source["id"],),
            ).fetchone()
            if current is None:
                raise PocketError(404, "可靠信源不存在")
            self._assert_version(current["version"], expected_version)
            plan = connection.execute(
                """
                SELECT * FROM reliable_collection_plans
                WHERE reliable_source_id = ?
                """,
                (source["id"],),
            ).fetchone()
            if plan is None or not plan["enabled"]:
                raise PocketError(409, "可靠信源采集计划已停用")
            now = utc_now()
            previous = connection.execute(
                """
                SELECT * FROM reliable_feed_snapshots
                WHERE reliable_source_id = ? AND status IN ('completed', 'not_modified')
                ORDER BY collected_at DESC, rowid DESC LIMIT 1
                """,
                (source["id"],),
            ).fetchone()
            if fetched.status == 304:
                if previous is None:
                    raise PocketError(502, "可靠信源首次采集不能返回 304")
                feed_hash = previous["content_hash"]
            etag = _bounded_metadata_header(
                headers.get("etag") or (previous["etag"] if previous else None),
                field="ETag",
                max_chars=MAX_ETAG_CHARS,
            )
            last_modified = _bounded_metadata_header(
                headers.get("last-modified")
                or (previous["last_modified"] if previous else None),
                field="Last-Modified",
                max_chars=MAX_LAST_MODIFIED_CHARS,
            )
            snapshot_id = new_id("rsnap")
            connection.execute(
                """
                INSERT INTO reliable_feed_snapshots(
                    id, reliable_source_id, request_url, resolved_ip,
                    http_status, status, content_hash, etag, last_modified,
                    byte_count, collected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    source["id"],
                    source["feed_url"],
                    resolved_ip,
                    fetched.status,
                    "not_modified" if fetched.status == 304 else "completed",
                    feed_hash,
                    etag,
                    last_modified,
                    len(fetched.body),
                    now,
                ),
            )
            counts = {"new": 0, "changed": 0, "duplicate": 0}
            if fetched.status == 200:
                for entry in entries:
                    outcome = self._ingest_entry(
                        connection,
                        source=current,
                        snapshot_id=snapshot_id,
                        snapshot_hash=feed_hash or "",
                        entry=entry,
                        collected_at=now,
                    )
                    counts[outcome] += 1
            connection.execute(
                """
                UPDATE reliable_feed_snapshots
                SET entry_count = ?, new_entry_count = ?,
                    changed_entry_count = ?, duplicate_entry_count = ?
                WHERE id = ?
                """,
                (
                    len(entries),
                    counts["new"],
                    counts["changed"],
                    counts["duplicate"],
                    snapshot_id,
                ),
            )
            next_run = (
                (datetime.now(UTC) + timedelta(days=1))
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
                if plan["schedule"] == "daily" and plan["enabled"]
                else None
            )
            connection.execute(
                """
                UPDATE reliable_collection_plans
                SET last_collected_at = ?, next_run_at = ?,
                    failure_count = 0, last_failure_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, next_run, now, plan["id"]),
            )
            connection.execute(
                """
                UPDATE reliable_sources
                SET version = version + 1, last_collected_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, source["id"]),
            )
            connection.execute(
                """
                UPDATE sources
                SET last_sync_at = ?, updated_at = ? WHERE id = ?
                """,
                (now, now, source["source_id"]),
            )
            source_row = connection.execute(
                "SELECT * FROM reliable_sources WHERE id = ?",
                (source["id"],),
            ).fetchone()
            plan_row = connection.execute(
                "SELECT * FROM reliable_collection_plans WHERE id = ?",
                (plan["id"],),
            ).fetchone()
            snapshot_row = connection.execute(
                "SELECT * FROM reliable_feed_snapshots WHERE id = ?",
                (snapshot_id,),
            ).fetchone()
            assert source_row is not None and plan_row is not None and snapshot_row is not None
            response = {
                "source": self._source(source_row),
                "collection_plan": self._plan(plan_row),
                "snapshot": self._snapshot(snapshot_row),
            }
            self._put_idempotent(
                connection,
                actor_id=actor_id,
                operation=operation,
                idempotency_key=idempotency_key,
                payload_hash=request_hash,
                response=response,
            )
            return response

    def _record_failed_snapshot(
        self,
        reliable_source_id: str,
        request_url: str,
        resolved_ip: str,
        *,
        error_code: str,
    ) -> None:
        try:
            with self.database.transaction() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM reliable_sources WHERE id = ?",
                    (reliable_source_id,),
                ).fetchone()
                if exists is None:
                    return
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO reliable_feed_snapshots(
                        id, reliable_source_id, request_url, resolved_ip,
                        http_status, status, error_code, collected_at
                    ) VALUES (?, ?, ?, ?, 0, 'failed', ?, ?)
                    """,
                    (
                        new_id("rsnap"),
                        reliable_source_id,
                        request_url,
                        resolved_ip,
                        error_code,
                        now,
                    ),
                )
                plan = connection.execute(
                    """
                    SELECT * FROM reliable_collection_plans
                    WHERE reliable_source_id = ?
                    """,
                    (reliable_source_id,),
                ).fetchone()
                if plan is not None:
                    failure_count = plan["failure_count"] + 1
                    next_run_at = plan["next_run_at"]
                    if plan["enabled"] and plan["schedule"] == "daily":
                        delay_hours = min(2 ** min(failure_count - 1, 5), 24)
                        next_run_at = (
                            datetime.now(UTC) + timedelta(hours=delay_hours)
                        ).isoformat(timespec="seconds").replace("+00:00", "Z")
                    connection.execute(
                        """
                        UPDATE reliable_collection_plans
                        SET failure_count = ?, last_failure_at = ?,
                            next_run_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            failure_count,
                            now,
                            next_run_at,
                            now,
                            plan["id"],
                        ),
                    )
        except sqlite3.Error:
            # The original bounded collection error remains the API result.
            return

    @staticmethod
    def _ingest_entry(
        connection: sqlite3.Connection,
        *,
        source: sqlite3.Row,
        snapshot_id: str,
        snapshot_hash: str,
        entry: ParsedFeedEntry,
        collected_at: str,
    ) -> str:
        identity_key = hashlib.sha256(entry.identity_hint.encode("utf-8")).hexdigest()
        content_payload = {
            "title": entry.title,
            "summary": entry.summary,
            "url": entry.url,
            "url_trust": entry.url_trust,
            "publisher": entry.publisher,
            "published_at": entry.published_at,
        }
        content_hash = _payload_hash(content_payload)
        current_entry = connection.execute(
            """
            SELECT * FROM reliable_entries
            WHERE reliable_source_id = ? AND identity_key = ?
            """,
            (source["id"], identity_key),
        ).fetchone()
        if current_entry is None and entry.url_trust == "feed_claimed_unverified":
            current_entry = connection.execute(
                """
                SELECT entry.*
                FROM reliable_entries entry
                WHERE entry.reliable_source_id = ?
                  AND entry.canonical_url = ?
                ORDER BY entry.created_at, entry.id
                LIMIT 1
                """,
                (source["id"], entry.url),
            ).fetchone()
        if current_entry is not None:
            current_version = connection.execute(
                """
                SELECT * FROM reliable_entry_versions
                WHERE entry_id = ? AND version = ?
                """,
                (current_entry["id"], current_entry["current_version"]),
            ).fetchone()
            if current_version is not None and current_version["content_hash"] == content_hash:
                connection.execute(
                    "UPDATE reliable_entries SET updated_at = ? WHERE id = ?",
                    (collected_at, current_entry["id"]),
                )
                return "duplicate"
            entry_id = current_entry["id"]
            version = current_entry["current_version"] + 1
            supersedes_item_id = current_version["item_id"] if current_version else None
            outcome = "changed"
            if supersedes_item_id:
                connection.execute(
                    """
                    DELETE FROM item_sources
                    WHERE source_id = ? AND item_id = ?
                    """,
                    (source["source_id"], supersedes_item_id),
                )
        else:
            entry_id = new_id("rentry")
            version = 1
            supersedes_item_id = None
            outcome = "new"
            connection.execute(
                """
                INSERT INTO reliable_entries(
                    id, reliable_source_id, identity_key, canonical_url,
                    url_trust, publisher, published_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    source["id"],
                    identity_key,
                    entry.url,
                    entry.url_trust,
                    entry.publisher,
                    entry.published_at,
                    collected_at,
                    collected_at,
                ),
            )
        item_id = new_id("item")
        task_id = new_id("task")
        text_content = entry.title
        if entry.summary:
            text_content = f"{entry.title}\n\n{entry.summary}"
        evidence = [
            {
                "snapshot_id": snapshot_id,
                "snapshot_hash": snapshot_hash,
                **point,
            }
            for point in entry.evidence
        ]
        citation = {
            "type": "web_snapshot",
            "entry_id": entry_id,
            "reliable_source_id": source["id"],
            "publisher": entry.publisher,
            "url": entry.url,
            "url_trust": entry.url_trust,
            "published_at": entry.published_at,
            "collected_at": collected_at,
            "snapshot_id": snapshot_id,
            "snapshot_hash": snapshot_hash,
            "evidence": evidence,
        }
        metadata: dict[str, Any] = {
            "source_name": source["display_name"],
            "news_citation": citation,
            "reliable_entry_id": entry_id,
            "reliable_entry_version": version,
        }
        if supersedes_item_id:
            metadata["supersedes_item_id"] = supersedes_item_id
            metadata["supersedes_item_ids"] = [supersedes_item_id]
        item_content_hash = hashlib.sha256(
            f"news\0{source['id']}\0{entry_id}\0{content_hash}".encode()
        ).hexdigest()
        tags = ["可靠信源", entry.publisher[:64]]
        connection.execute(
            """
            INSERT INTO items(
                id, content_hash, first_source_id, origin_uri, file_name,
                mime_type, title, text_content, size_bytes,
                source_modified_at, state, category, tags_json,
                metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'text/plain', ?, ?, ?, ?,
                      'needs_review', 'news', ?, ?, ?, ?)
            """,
            (
                item_id,
                item_content_hash,
                source["source_id"],
                entry.url,
                f"{entry_id}-v{version}.txt",
                entry.title,
                text_content,
                len(text_content.encode("utf-8")),
                entry.published_at,
                json.dumps(tags, ensure_ascii=False),
                json.dumps(metadata, ensure_ascii=False),
                collected_at,
                collected_at,
            ),
        )
        link_origin = f"{entry.url}#centaur-entry={entry_id}"
        connection.execute(
            """
            INSERT INTO item_sources(
                source_id, origin_uri, item_id, source_modified_at,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, origin_uri) DO UPDATE SET
                item_id = excluded.item_id,
                source_modified_at = excluded.source_modified_at,
                last_seen_at = excluded.last_seen_at
            """,
            (
                source["source_id"],
                link_origin,
                item_id,
                entry.published_at,
                collected_at,
                collected_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO item_fts(item_id, title, body, tags, category)
            VALUES (?, ?, ?, ?, 'news')
            """,
            (item_id, entry.title, text_content, " ".join(tags)),
        )
        proposal = {
            "patch": {
                "title": entry.title,
                "category": "news",
                "tags": tags,
                "state": "ready",
            },
            "suggestion": "确认确定性摘要与逐条 feed 字段证据后向 Agent 开放",
            "reason": "可靠信源新资讯必须经本人确认",
            "confidence": 1.0,
            "citation": citation,
        }
        if supersedes_item_id:
            proposal["supersedes_item_id"] = supersedes_item_id
            proposal["supersedes_item_ids"] = [supersedes_item_id]
        connection.execute(
            """
            INSERT INTO governance_tasks(
                id, item_id, kind, status, proposal_json,
                created_at, updated_at
            ) VALUES (?, ?, 'news_summary', 'pending', ?, ?, ?)
            """,
            (
                task_id,
                item_id,
                json.dumps(proposal, ensure_ascii=False),
                collected_at,
                collected_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO reliable_entry_versions(
                id, entry_id, version, content_hash, snapshot_id,
                item_id, governance_task_id, title, summary,
                evidence_json, collected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("rever"),
                entry_id,
                version,
                content_hash,
                snapshot_id,
                item_id,
                task_id,
                entry.title,
                entry.summary,
                json.dumps(evidence, ensure_ascii=False),
                collected_at,
            ),
        )
        connection.execute(
            """
            UPDATE reliable_entries
            SET canonical_url = ?, url_trust = ?, publisher = ?, published_at = ?,
                current_version = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                entry.url,
                entry.url_trust,
                entry.publisher,
                entry.published_at,
                version,
                collected_at,
                entry_id,
            ),
        )
        return outcome

    def list_entries(self, reliable_source_id: str, *, limit: int) -> dict[str, Any]:
        with self.database.connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM reliable_sources WHERE id = ?",
                (reliable_source_id,),
            ).fetchone()
            if exists is None:
                raise PocketError(404, "可靠信源不存在")
            total = connection.execute(
                "SELECT COUNT(*) FROM reliable_entries WHERE reliable_source_id = ?",
                (reliable_source_id,),
            ).fetchone()[0]
            rows = connection.execute(
                """
                SELECT entry.*, version.title, version.summary,
                       version.item_id, version.governance_task_id,
                       version.evidence_json, version.collected_at,
                       item.state, snapshot.content_hash AS snapshot_hash
                FROM reliable_entries entry
                JOIN reliable_entry_versions version
                  ON version.entry_id = entry.id
                 AND version.version = entry.current_version
                JOIN items item ON item.id = version.item_id
                JOIN reliable_feed_snapshots snapshot
                  ON snapshot.id = version.snapshot_id
                WHERE entry.reliable_source_id = ?
                ORDER BY COALESCE(entry.published_at, version.collected_at) DESC,
                         entry.id DESC
                LIMIT ?
                """,
                (reliable_source_id, limit),
            ).fetchall()
        items = [
            {
                "id": row["id"],
                "identity_key": row["identity_key"],
                "title": row["title"],
                "summary": row["summary"],
                "url": row["canonical_url"],
                "url_trust": row["url_trust"],
                "publisher": row["publisher"],
                "published_at": row["published_at"],
                "collected_at": row["collected_at"],
                "current_version": row["current_version"],
                "snapshot_hash": row["snapshot_hash"],
                "state": row["state"],
                "item_id": row["item_id"],
                "governance_task_id": row["governance_task_id"],
                "evidence": json_loads(row["evidence_json"], []),
            }
            for row in rows
        ]
        return {"items": items, "total": total, "limit": limit}

    def due_source_ids(self) -> list[str]:
        now = utc_now()
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT source.id
                FROM reliable_sources source
                JOIN reliable_collection_plans plan
                  ON plan.reliable_source_id = source.id
                WHERE source.status = 'active'
                  AND plan.enabled = 1
                  AND plan.schedule = 'daily'
                  AND (plan.next_run_at IS NULL OR plan.next_run_at <= ?)
                ORDER BY COALESCE(plan.next_run_at, plan.created_at), source.id
                """,
                (now,),
            ).fetchall()
        return [row["id"] for row in rows]

    def collect_due(self, reliable_source_id: str) -> dict[str, Any]:
        source = self._source_row(reliable_source_id)
        with self.database.connect() as connection:
            plan = connection.execute(
                """
                SELECT failure_count, next_run_at
                FROM reliable_collection_plans
                WHERE reliable_source_id = ?
                """,
                (reliable_source_id,),
            ).fetchone()
        if plan is None:
            raise PocketError(404, "可靠信源采集计划不存在")
        attempt_marker = hashlib.sha256(
            f"{plan['failure_count']}\0{plan['next_run_at']}".encode()
        ).hexdigest()[:16]
        return self.collect(
            reliable_source_id,
            expected_version=source["version"],
            actor_id="scheduler",
            idempotency_key=(
                f"daily:{source['version']}:{attempt_marker}"
            ),
        )
