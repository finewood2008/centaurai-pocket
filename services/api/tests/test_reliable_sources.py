from __future__ import annotations

import ipaddress
import json
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from centaur_pocket import reliable_sources as reliable_sources_module
from centaur_pocket.database import (
    LATEST_SCHEMA_VERSION,
    MIGRATION_V3,
    MIGRATION_V4,
    SCHEMA_V1,
    Database,
)
from centaur_pocket.main import _collect_due_reliable_source_safely
from centaur_pocket.reliable_sources import (
    DnsJsonFeedResolver,
    FakeIpFallbackFeedResolver,
    FeedFetchRequest,
    FeedFetchResponse,
    PinnedHTTPSFeedTransport,
)
from centaur_pocket.service import PocketError

PUBLIC_IP = "93.184.216.34"
OWNER_TOKEN = "cp_owner_test-token"


def local_nat64_address(ipv4: str) -> str:
    packed = bytearray(ipaddress.IPv6Network("64:ff9b:1::/48").network_address.packed)
    embedded = ipaddress.IPv4Address(ipv4).packed
    packed[6:8] = embedded[:2]
    packed[8] = 0
    packed[9:11] = embedded[2:]
    return str(ipaddress.IPv6Address(bytes(packed)))


class FakeResolver:
    def __init__(self, addresses: Iterable[str] = (PUBLIC_IP,)) -> None:
        self.addresses = list(addresses)
        self.calls: list[tuple[str, int]] = []

    def resolve(self, host: str, port: int) -> list[str]:
        self.calls.append((host, port))
        return list(self.addresses)


class FakeTransport:
    def __init__(self, responses: Iterable[FeedFetchResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[FeedFetchRequest] = []

    def fetch(self, request: FeedFetchRequest) -> FeedFetchResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected feed fetch")
        return self.responses.pop(0)


def test_fake_ip_dns_uses_safe_fallback_only_for_benchmark_range() -> None:
    primary = FakeResolver(["198.18.63.199", "198.19.10.4"])
    fallback = FakeResolver([PUBLIC_IP])
    resolver = FakeIpFallbackFeedResolver(primary, fallback)

    assert resolver.resolve("github.blog", 443) == [PUBLIC_IP]
    assert primary.calls == [("github.blog", 443)]
    assert fallback.calls == [("github.blog", 443)]

    private_primary = FakeResolver(["10.0.0.8"])
    unused_fallback = FakeResolver([PUBLIC_IP])
    private_resolver = FakeIpFallbackFeedResolver(
        private_primary,
        unused_fallback,
    )
    assert private_resolver.resolve("internal.example", 443) == ["10.0.0.8"]
    assert unused_fallback.calls == []


def test_dns_json_resolver_binds_question_and_collects_address_types() -> None:
    calls: list[tuple[str, str]] = []

    def query_json(host: str, record_type: str) -> dict[str, Any]:
        calls.append((host, record_type))
        answer = (
            [{"name": f"{host}.", "type": 1, "data": PUBLIC_IP}]
            if record_type == "A"
            else [{"name": f"{host}.", "type": 28, "data": "2606:2800:220:1:248:1893:25c8:1946"}]
        )
        return {
            "Status": 0,
            "Question": [
                {"name": f"{host}.", "type": 1 if record_type == "A" else 28}
            ],
            "Answer": answer,
        }

    resolver = DnsJsonFeedResolver(query_json=query_json)
    assert resolver.resolve("news.example.com", 443) == [
        PUBLIC_IP,
    ]
    assert calls == [("news.example.com", "A")]

    ipv6_resolver = DnsJsonFeedResolver(
        query_json=lambda host, record_type: {
            "Status": 0,
            "Question": [
                {"name": f"{host}.", "type": 1 if record_type == "A" else 28}
            ],
            "Answer": []
            if record_type == "A"
            else [
                {
                    "name": f"{host}.",
                    "type": 28,
                    "data": "2606:2800:220:1:248:1893:25c8:1946",
                }
            ],
        }
    )
    assert ipv6_resolver.resolve("ipv6.example.com", 443) == [
        "2606:2800:220:1:248:1893:25c8:1946"
    ]

    with pytest.raises(PocketError, match="查询绑定无效"):
        DnsJsonFeedResolver(
            query_json=lambda _host, _record_type: {
                "Status": 0,
                "Question": [{"name": "other.example.com.", "type": 1}],
            }
        ).resolve("news.example.com", 443)


def rss_response(body: bytes, *, etag: str = '"feed-v1"') -> FeedFetchResponse:
    return FeedFetchResponse(
        status=200,
        headers={
            "content-type": "application/rss+xml; charset=utf-8",
            "content-encoding": "identity",
            "etag": etag,
            "last-modified": "Wed, 01 Jul 2026 09:00:00 GMT",
        },
        body=body,
    )


def rss_body(summary: str = "第一条可靠事实。第二条可靠事实。") -> bytes:
    escaped_summary = (
        summary.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>示例官方发布</title>
  <link>https://news.example.com/feed.xml</link>
  <item>
    <guid>official-article-1</guid>
    <title>季度政策更新</title>
    <link>https://news.example.com/releases/q3</link>
    <pubDate>Wed, 01 Jul 2026 08:30:00 GMT</pubDate>
    <author>示例机构</author>
    <description>{escaped_summary}</description>
  </item>
</channel></rss>""".encode()


def test_feed_parser_allows_html_doctype_inside_cdata() -> None:
    body = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><item>
  <guid>cdata-doctype-1</guid>
  <title>CDATA HTML article</title>
  <description><![CDATA[<!DOCTYPE html><html><body>safe article</body></html>]]></description>
</item></channel></rss>"""

    entries = reliable_sources_module.parse_feed(
        body,
        feed_url="https://news.example.com/feed.xml",
        display_name="示例官方发布",
    )

    assert len(entries) == 1
    assert entries[0].summary == "safe article"


def test_feed_parser_bounds_large_body_fields_separately_from_titles() -> None:
    large_body = "正文" * 12_000
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><item>
  <guid>large-body-1</guid>
  <title>Large body article</title>
  <description><![CDATA[{large_body}]]></description>
</item></channel></rss>""".encode()

    entries = reliable_sources_module.parse_feed(
        body,
        feed_url="https://news.example.com/feed.xml",
        display_name="示例官方发布",
    )

    assert len(entries) == 1
    assert len(entries[0].summary) == reliable_sources_module.MAX_SUMMARY_CHARS

    oversized_title = f"""<rss><channel><item>
<title>{"x" * (reliable_sources_module.MAX_FIELD_CHARS + 1)}</title>
</item></channel></rss>""".encode()
    with pytest.raises(PocketError, match="字段过长"):
        reliable_sources_module.parse_feed(
            oversized_title,
            feed_url="https://news.example.com/feed.xml",
            display_name="示例官方发布",
        )


def write_headers(key: str, *, device_id: str = "desktop-owner") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {OWNER_TOKEN}",
        "X-Device-ID": device_id,
        "Idempotency-Key": key,
    }


def propose(
    client: TestClient,
    *,
    key: str = "candidate-key-0001",
    feed_url: str = "https://news.example.com/feed.xml",
) -> Any:
    return client.post(
        "/api/v1/reliable-source-candidates",
        headers=write_headers(key),
        json={
            "display_name": "示例官方发布",
            "organization_origin": "示例机构官方网站",
            "feed_url": feed_url,
            "trust_reason": "由机构官网明确公布的官方 RSS",
            "scope": "机构政策与公告",
            "review_due_at": "2026-12-31T12:00:00+08:00",
        },
    )


def confirm(
    client: TestClient,
    candidate: dict[str, Any],
    *,
    key: str = "confirm-key-0001",
    schedule: str = "manual",
) -> Any:
    return client.post(
        f"/api/v1/reliable-source-candidates/{candidate['id']}/confirm",
        headers={**write_headers(key), "If-Match": f'"{candidate["version"]}"'},
        json={"expected_version": candidate["version"], "schedule": schedule},
    )


def configure_network(
    client: TestClient,
    responses: Iterable[FeedFetchResponse],
    *,
    addresses: Iterable[str] = (PUBLIC_IP,),
) -> tuple[FakeResolver, FakeTransport]:
    resolver = FakeResolver(addresses)
    transport = FakeTransport(responses)
    client.app.state.service.reliable_sources.set_network(
        resolver=resolver,
        transport=transport,
    )
    return resolver, transport


def collect(
    client: TestClient,
    source: dict[str, Any],
    *,
    key: str,
) -> Any:
    return client.post(
        f"/api/v1/reliable-sources/{source['id']}/collect",
        headers={**write_headers(key), "If-Match": f'"{source["version"]}"'},
        json={"expected_version": source["version"]},
    )


def test_candidate_is_offline_until_confirmed_and_writes_are_payload_bound(
    client: TestClient,
) -> None:
    resolver, transport = configure_network(client, [rss_response(rss_body())])

    created = propose(client)
    assert created.status_code == 201
    candidate = created.json()
    assert created.headers["etag"] == '"1"'
    assert candidate["feed_url"] == "https://news.example.com/feed.xml"
    assert candidate["status"] == "pending"
    assert resolver.calls == []
    assert transport.requests == []

    replay = propose(client)
    assert replay.status_code == 201
    assert replay.json() == candidate
    conflict = propose(
        client,
        key="candidate-key-0001",
        feed_url="https://news.example.com/other.xml",
    )
    assert conflict.status_code == 409
    assert resolver.calls == []
    assert transport.requests == []

    confirmed = confirm(client, candidate)
    assert confirmed.status_code == 200
    result = confirmed.json()
    assert confirmed.headers["etag"] == '"2"'
    assert result["candidate"]["status"] == "confirmed"
    assert result["reliable_source"]["version"] == 1
    assert result["collection_plan"]["schedule"] == "manual"
    assert resolver.calls == []
    assert transport.requests == []


def test_collection_governance_agent_citation_dedup_and_changed_version(
    client: TestClient,
    agent_headers: dict[str, str],
) -> None:
    injected = (
        "第一条可靠事实。"
        '<img src=x onerror="steal()">第二条可靠事实。'
        "<script>execute_me()</script>"
    )
    _resolver, transport = configure_network(
        client,
        [
            rss_response(rss_body(injected), etag='"v1"'),
            rss_response(rss_body(injected), etag='"v1"'),
            rss_response(rss_body("替换后的新事实。"), etag='"v2"'),
        ],
    )
    candidate = propose(client).json()
    source = confirm(client, candidate).json()["reliable_source"]

    before = client.post(
        "/api/v1/agent/search",
        headers=agent_headers,
        json={"query": "季度政策更新"},
    )
    assert before.status_code == 200
    assert before.json()["results"] == []

    first = collect(client, source, key="collect-key-0001")
    assert first.status_code == 200
    first_result = first.json()
    source = first_result["source"]
    snapshot = first_result["snapshot"]
    assert first.headers["etag"] == '"2"'
    assert snapshot["status"] == "completed"
    assert snapshot["entry_count"] == 1
    assert snapshot["new_entry_count"] == 1
    assert "body" not in snapshot
    assert transport.requests[0].resolved_ip == PUBLIC_IP
    assert transport.requests[0].host == "news.example.com"
    assert transport.requests[0].headers["Host"] == "news.example.com"
    assert transport.requests[0].headers["Accept-Encoding"] == "identity"

    entries_response = client.get(
        f"/api/v1/reliable-sources/{source['id']}/entries?limit=10",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
    )
    assert entries_response.status_code == 200
    entry = entries_response.json()["items"][0]
    assert entry["state"] == "needs_review"
    assert entry["url"] == "https://news.example.com/releases/q3"
    assert entry["url_trust"] == "feed_claimed_unverified"
    assert "<" not in entry["summary"]
    assert "onerror" not in entry["summary"]
    assert "execute_me" not in entry["summary"]
    assert entry["evidence"]
    assert set(entry["evidence"][0]) == {
        "snapshot_id",
        "snapshot_hash",
        "field",
        "start_offset",
        "end_offset",
        "offset_unit",
        "excerpt",
    }
    assert entry["evidence"][0]["offset_unit"] == "unicode_code_points"

    hidden = client.post(
        "/api/v1/agent/search",
        headers=agent_headers,
        json={"query": "季度政策更新"},
    ).json()
    assert hidden["results"] == []

    applied = client.post(
        f"/api/v1/governance/tasks/{entry['governance_task_id']}/apply",
        headers={**write_headers("govern-news-0001")},
        json={"patch": {"state": "ready"}},
    )
    assert applied.status_code == 200
    visible = client.post(
        "/api/v1/agent/search",
        headers=agent_headers,
        json={"query": "季度政策更新"},
    ).json()["results"]
    assert len(visible) == 1
    citation = visible[0]["citations"][0]
    assert visible[0]["content_type"] == "news"
    assert citation["type"] == "web_snapshot"
    assert citation["publisher"] == "示例官方发布"
    assert citation["url"] == "https://news.example.com/releases/q3"
    assert citation["url_trust"] == "feed_claimed_unverified"
    assert citation["published_at"] == "2026-07-01T08:30:00Z"
    assert citation["collected_at"]
    assert citation["snapshot_hash"] == snapshot["content_hash"]
    assert citation["evidence"][0]["field"] == "title"
    assert citation["evidence"][0]["excerpt"] == "季度政策更新"
    serialized_visible = json.dumps(visible, ensure_ascii=False)
    assert "<rss" not in serialized_visible
    assert "resolved_ip" not in serialized_visible

    duplicate = collect(client, source, key="collect-key-0002")
    assert duplicate.status_code == 200
    assert duplicate.json()["snapshot"]["duplicate_entry_count"] == 1
    assert client.get(
        f"/api/v1/reliable-sources/{source['id']}/entries",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
    ).json()["total"] == 1
    source = duplicate.json()["source"]

    changed = collect(client, source, key="collect-key-0003")
    assert changed.status_code == 200
    assert changed.json()["snapshot"]["changed_entry_count"] == 1
    latest = client.get(
        f"/api/v1/reliable-sources/{source['id']}/entries",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
    ).json()["items"][0]
    assert latest["current_version"] == 2
    assert latest["state"] == "needs_review"

    # The old governed generation remains searchable until the replacement is
    # explicitly accepted.
    still_old = client.post(
        "/api/v1/agent/search",
        headers=agent_headers,
        json={"query": "第一条可靠事实"},
    ).json()["results"]
    assert len(still_old) == 1
    not_new = client.post(
        "/api/v1/agent/search",
        headers=agent_headers,
        json={"query": "替换后的新事实"},
    ).json()["results"]
    assert not_new == []


def test_304_conditional_request_and_collect_idempotency(client: TestClient) -> None:
    _resolver, transport = configure_network(
        client,
        [
            rss_response(rss_body(), etag='"stable"'),
            FeedFetchResponse(
                status=304,
                headers={"etag": '"stable"', "content-encoding": "identity"},
                body=b"",
            ),
        ],
    )
    candidate = propose(client).json()
    source = confirm(client, candidate).json()["reliable_source"]
    first = collect(client, source, key="collect-304-first").json()
    source = first["source"]
    second = collect(client, source, key="collect-304-second")
    assert second.status_code == 200
    assert second.json()["snapshot"]["status"] == "not_modified"
    assert second.json()["snapshot"]["content_hash"] == first["snapshot"]["content_hash"]
    assert transport.requests[1].headers["If-None-Match"] == '"stable"'
    assert "If-Modified-Since" in transport.requests[1].headers

    replay = collect(client, source, key="collect-304-second")
    assert replay.status_code == 200
    assert replay.json() == second.json()
    assert len(transport.requests) == 2

    mismatched = client.post(
        f"/api/v1/reliable-sources/{source['id']}/collect",
        headers={**write_headers("collect-304-second"), "If-Match": '"999"'},
        json={"expected_version": 999},
    )
    assert mismatched.status_code == 409


@pytest.mark.parametrize(
    ("response", "detail_fragment"),
    [
        (
            FeedFetchResponse(
                302,
                {"location": "https://elsewhere.example/feed"},
                b"",
            ),
            "重定向",
        ),
        (
            FeedFetchResponse(200, {"content-type": "text/html"}, b"<html></html>"),
            "MIME",
        ),
        (
            FeedFetchResponse(
                200,
                {"content-type": "application/rss+xml; charset=utf-16"},
                rss_body(),
            ),
            "UTF-8",
        ),
        (
            FeedFetchResponse(
                200,
                {"content-type": "application/rss+xml", "content-encoding": "gzip"},
                b"compressed",
            ),
            "压缩",
        ),
        (
            rss_response(
                b'<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
                b'<rss><channel><item><title>&xxe;</title></item></channel></rss>'
            ),
            "DOCTYPE",
        ),
        (
            rss_response(rss_body().decode().encode("utf-16")),
            "UTF-8",
        ),
        (
            rss_response(
                rss_body().replace(
                    b'encoding="UTF-8"',
                    b'encoding="ISO-8859-1"',
                )
            ),
            "UTF-8",
        ),
        (
            rss_response(
                b"<!--" + b"x" * 5000 + b"-->"
                b"<!DOCTYPE rss><rss><channel></channel></rss>"
            ),
            "DOCTYPE",
        ),
        (
            FeedFetchResponse(
                200,
                {
                    "content-type": "application/rss+xml",
                    "etag": "ok\r\nInjected: yes",
                },
                rss_body(),
            ),
            "header",
        ),
        (
            FeedFetchResponse(
                200,
                {
                    "content-type": "application/rss+xml",
                    "etag": '"' + "x" * 513 + '"',
                },
                rss_body(),
            ),
            "ETag",
        ),
        (
            rss_response(
                ("<rss><channel>" + "<x>" * 40 + "boom" + "</x>" * 40 + "</channel></rss>").encode()
            ),
            "嵌套",
        ),
        (
            rss_response(b"<html><body>not a feed</body></html>"),
            "RSS",
        ),
    ],
)
def test_malicious_or_unsupported_feed_is_rejected_without_raw_body_persistence(
    client: TestClient,
    response: FeedFetchResponse,
    detail_fragment: str,
) -> None:
    _resolver, transport = configure_network(client, [response])
    candidate = propose(client).json()
    source = confirm(client, candidate).json()["reliable_source"]
    reject_key = f"reject-{abs(hash(detail_fragment))}-0001"
    rejected = collect(
        client,
        source,
        key=reject_key,
    )
    assert rejected.status_code == 502
    assert detail_fragment in rejected.json()["detail"]
    assert len(transport.requests) == 1
    replay = collect(client, source, key=reject_key)
    assert replay.status_code == rejected.status_code
    assert replay.json() == rejected.json()
    assert len(transport.requests) == 1
    with client.app.state.service.database.connect() as connection:
        snapshot = connection.execute(
            """
            SELECT status, error_code FROM reliable_feed_snapshots
            WHERE reliable_source_id = ? ORDER BY rowid DESC LIMIT 1
            """,
            (source["id"],),
        ).fetchone()
        assert dict(snapshot)["status"] == "failed"
        assert connection.execute(
            """
            SELECT COUNT(*) FROM reliable_feed_snapshots
            WHERE reliable_source_id = ? AND status = 'failed'
            """,
            (source["id"],),
        ).fetchone()[0] == 1
        table_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(reliable_feed_snapshots)"
            ).fetchall()
        }
        assert "body" not in table_columns
        assert "raw_body" not in table_columns


@pytest.mark.parametrize(
    "feed_url",
    [
        "http://news.example.com/feed.xml",
        "https://user:secret@news.example.com/feed.xml",
        "https://127.0.0.1/feed.xml",
        "https://[::1]/feed.xml",
        "https://news.example.com:8443/feed.xml",
        "https://news.example.com/feed.xml#section",
        "https://localhost/feed.xml",
        "https://news.example.com/feed.xml?topic=has space",
        "https://news.example.com/feed.xml?topic=bad\nvalue",
    ],
)
def test_candidate_rejects_non_public_https_url_shapes_without_fetch(
    client: TestClient,
    feed_url: str,
) -> None:
    resolver, transport = configure_network(client, [])
    response = propose(client, key=f"bad-url-{abs(hash(feed_url))}", feed_url=feed_url)
    assert response.status_code == 422
    assert resolver.calls == []
    assert transport.requests == []


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["10.0.0.8"],
        ["169.254.169.254"],
        ["224.0.0.1"],
        ["::1"],
        ["fe80::1"],
        ["::ffff:127.0.0.1"],
        ["64:ff9b::a9fe:a9fe"],
        [local_nat64_address("169.254.169.254")],
        ["2002:7f00:1::"],
        [PUBLIC_IP, "192.168.1.5"],
    ],
)
def test_dns_ssrf_and_rebinding_addresses_are_rejected_before_transport(
    client: TestClient,
    addresses: list[str],
) -> None:
    resolver, transport = configure_network(client, [], addresses=addresses)
    candidate = propose(client).json()
    source = confirm(client, candidate).json()["reliable_source"]
    response = collect(client, source, key=f"ssrf-{len(addresses)}-{addresses[-1]}")
    assert response.status_code == 403
    assert resolver.calls == [("news.example.com", 443)]
    assert transport.requests == []


def test_default_transport_enforces_absolute_deadline_with_slow_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown = threading.Event()

    class SlowSocket:
        def shutdown(self, _how: int) -> None:
            shutdown.set()

        def settimeout(self, _timeout: float) -> None:
            pass

    class SlowConnection:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.sock: SlowSocket | None = SlowSocket()

        def request(self, *_args: Any, **_kwargs: Any) -> None:
            shutdown.wait(1)

        def getresponse(self) -> Any:
            raise AssertionError("deadline must stop before response parsing")

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        reliable_sources_module,
        "_PinnedHTTPSConnection",
        SlowConnection,
    )
    request = FeedFetchRequest(
        url="https://news.example.com/feed.xml",
        host="news.example.com",
        port=443,
        target="/feed.xml",
        resolved_ip=PUBLIC_IP,
        headers={"Host": "news.example.com", "Accept-Encoding": "identity"},
        timeout_seconds=0.02,
    )
    started = time.monotonic()
    with pytest.raises(PocketError, match="绝对时间上限"):
        PinnedHTTPSFeedTransport().fetch(request)
    assert time.monotonic() - started < 0.2
    assert shutdown.is_set()


def test_permissions_device_binding_version_and_plan_endpoint_immutability(
    client: TestClient,
    agent_headers: dict[str, str],
) -> None:
    assert client.get("/api/v1/reliable-sources", headers=agent_headers).status_code == 401
    assert propose(client).status_code == 201
    candidate = client.get(
        "/api/v1/reliable-source-candidates?status=pending",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
    ).json()["items"][0]

    missing_match = client.post(
        f"/api/v1/reliable-source-candidates/{candidate['id']}/confirm",
        headers=write_headers("missing-match-0001"),
        json={"expected_version": 1, "schedule": "manual"},
    )
    assert missing_match.status_code == 428
    mismatch = client.post(
        f"/api/v1/reliable-source-candidates/{candidate['id']}/confirm",
        headers={**write_headers("version-mismatch-01"), "If-Match": '"1"'},
        json={"expected_version": 2, "schedule": "manual"},
    )
    assert mismatch.status_code == 412
    source = confirm(client, candidate).json()["reliable_source"]

    plan_response = client.get(
        f"/api/v1/reliable-sources/{source['id']}/collection-plan",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
    )
    assert plan_response.headers["etag"] == '"1"'
    forbidden_endpoint_change = client.patch(
        f"/api/v1/reliable-sources/{source['id']}/collection-plan",
        headers={**write_headers("plan-endpoint-0001"), "If-Match": '"1"'},
        json={"feed_url": "https://attacker.example/feed"},
    )
    assert forbidden_endpoint_change.status_code == 422
    updated = client.patch(
        f"/api/v1/reliable-sources/{source['id']}/collection-plan",
        headers={**write_headers("plan-update-0001"), "If-Match": '"1"'},
        json={"schedule": "daily", "enabled": False},
    )
    assert updated.status_code == 200
    assert updated.headers["etag"] == '"2"'
    assert updated.json()["schedule"] == "daily"
    assert updated.json()["enabled"] is False
    internal_source = client.get(
        f"/api/v1/sources/{source['source_id']}",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
    ).json()
    assert internal_source["schedule"] == "daily"
    assert internal_source["enabled"] is False
    stale = client.patch(
        f"/api/v1/reliable-sources/{source['id']}/collection-plan",
        headers={**write_headers("plan-stale-0001"), "If-Match": '"1"'},
        json={"enabled": True},
    )
    assert stale.status_code == 412

    pairing = client.post(
        "/api/v1/mobile/pairings",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
        json={},
    ).json()
    claimed = client.post(
        "/api/v1/mobile/pairings/claim",
        json={
            "code": pairing["code"],
            "device_id": "phone-reliable-1",
            "display_name": "测试手机",
            "platform": "android",
            "app_version": "1.0.0",
        },
    ).json()
    mobile_headers = {
        "Authorization": f"Bearer {claimed['access_token']}",
        "X-Device-ID": "phone-reliable-1",
    }
    assert client.get("/api/v1/reliable-sources", headers=mobile_headers).status_code == 200
    assert client.get(
        "/api/v1/reliable-sources",
        headers={**mobile_headers, "X-Device-ID": "other-phone"},
    ).status_code == 403


def test_cross_host_entry_url_is_retained_but_explicitly_unverified(
    client: TestClient,
) -> None:
    body = rss_body().replace(
        b"https://news.example.com/releases/q3",
        b"https://official.example.org/releases/q3",
    )
    _resolver, _transport = configure_network(client, [rss_response(body)])
    candidate = propose(client).json()
    source = confirm(client, candidate).json()["reliable_source"]
    collected = collect(client, source, key="cross-host-collect-01")
    assert collected.status_code == 200
    entry = client.get(
        f"/api/v1/reliable-sources/{source['id']}/entries",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
    ).json()["items"][0]
    assert entry["url"] == "https://official.example.org/releases/q3"
    assert entry["url_trust"] == "feed_claimed_unverified"


def test_stable_guid_with_changed_url_is_one_versioned_entry(
    client: TestClient,
) -> None:
    first_body = rss_body("旧版本正文。")
    second_body = rss_body("新版本正文。").replace(
        b"https://news.example.com/releases/q3",
        b"https://news.example.com/releases/q3-revised",
    )
    _resolver, _transport = configure_network(
        client,
        [rss_response(first_body, etag='"old"'), rss_response(second_body, etag='"new"')],
    )
    candidate = propose(client).json()
    source = confirm(client, candidate).json()["reliable_source"]
    first = collect(client, source, key="guid-version-first").json()
    first_entry = client.get(
        f"/api/v1/reliable-sources/{source['id']}/entries",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
    ).json()["items"][0]
    assert client.post(
        f"/api/v1/governance/tasks/{first_entry['governance_task_id']}/apply",
        headers=write_headers("guid-version-apply-first"),
        json={"patch": {"state": "ready"}},
    ).status_code == 200

    second = collect(
        client,
        first["source"],
        key="guid-version-second",
    )
    assert second.status_code == 200
    entries = client.get(
        f"/api/v1/reliable-sources/{source['id']}/entries",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
    ).json()
    assert entries["total"] == 1
    current = entries["items"][0]
    assert current["id"] == first_entry["id"]
    assert current["current_version"] == 2
    assert current["url"] == "https://news.example.com/releases/q3-revised"
    assert current["state"] == "needs_review"
    assert client.post(
        f"/api/v1/governance/tasks/{current['governance_task_id']}/apply",
        headers=write_headers("guid-version-apply-second"),
        json={"patch": {"state": "ready"}},
    ).status_code == 200
    with client.app.state.service.database.connect() as connection:
        old_item = connection.execute(
            "SELECT state FROM items WHERE id = ?",
            (first_entry["item_id"],),
        ).fetchone()
        assert old_item["state"] == "archived"


def test_daily_failure_backs_off_and_does_not_starve_next_source(
    client: TestClient,
) -> None:
    _resolver, transport = configure_network(
        client,
        [
            FeedFetchResponse(500, {"content-type": "text/plain"}, b"failed"),
            rss_response(rss_body()),
        ],
    )
    first_candidate = propose(
        client,
        key="daily-candidate-first",
        feed_url="https://news.example.com/feed.xml",
    ).json()
    second_candidate = propose(
        client,
        key="daily-candidate-second",
        feed_url="https://alerts.example.org/feed.xml",
    ).json()
    first = confirm(
        client,
        first_candidate,
        key="daily-confirm-first",
        schedule="daily",
    ).json()["reliable_source"]
    second = confirm(
        client,
        second_candidate,
        key="daily-confirm-second",
        schedule="daily",
    ).json()["reliable_source"]

    _collect_due_reliable_source_safely(client.app.state.service, first["id"])
    _collect_due_reliable_source_safely(client.app.state.service, second["id"])
    assert len(transport.requests) == 2
    assert client.app.state.service.reliable_sources.due_source_ids() == []
    assert client.get(
        f"/api/v1/reliable-sources/{second['id']}/entries",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
    ).json()["total"] == 1
    with client.app.state.service.database.connect() as connection:
        failed_plan = connection.execute(
            """
            SELECT failure_count, last_failure_at, next_run_at
            FROM reliable_collection_plans WHERE reliable_source_id = ?
            """,
            (first["id"],),
        ).fetchone()
        assert failed_plan["failure_count"] == 1
        assert failed_plan["last_failure_at"]
        assert failed_plan["next_run_at"] > failed_plan["last_failure_at"]
        successful_plan = connection.execute(
            """
            SELECT failure_count, last_failure_at
            FROM reliable_collection_plans WHERE reliable_source_id = ?
            """,
            (second["id"],),
        ).fetchone()
        assert successful_plan["failure_count"] == 0
        assert successful_plan["last_failure_at"] is None


def test_v4_to_v5_migration_preserves_existing_source_item_foreign_keys(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v4.sqlite3"
    database = Database(path)
    with database.connect() as connection:
        connection.executescript(SCHEMA_V1)
        Database._migrate_to_v2(connection)
        connection.executescript(MIGRATION_V3)
        connection.executescript(MIGRATION_V4)
        now = "2026-07-01T00:00:00Z"
        connection.execute(
            """
            INSERT INTO sources(
                id, kind, provider, name, config_json, schedule,
                enabled, created_at, updated_at
            ) VALUES ('src_existing', 'folder', NULL, '原有目录', ?, 'manual', 1, ?, ?)
            """,
            (json.dumps({"path": "/srv/existing"}), now, now),
        )
        connection.execute(
            """
            INSERT INTO items(
                id, content_hash, first_source_id, origin_uri, file_name,
                mime_type, title, text_content, size_bytes, state,
                tags_json, metadata_json, created_at, updated_at
            ) VALUES ('item_existing', ?, 'src_existing', 'file:///srv/existing/a.txt',
                      'a.txt', 'text/plain', '原有内容', '保留我', 9, 'ready',
                      '[]', '{}', ?, ?)
            """,
            ("a" * 64, now, now),
        )
        connection.execute(
            """
            INSERT INTO item_sources(
                source_id, origin_uri, item_id, first_seen_at, last_seen_at
            ) VALUES ('src_existing', 'file:///srv/existing/a.txt',
                      'item_existing', ?, ?)
            """,
            (now, now),
        )

    Database(path).initialize()
    with Database(path).connect() as connection:
        assert (
            connection.execute("SELECT version FROM schema_meta").fetchone()[0]
            == LATEST_SCHEMA_VERSION
        )
        source = connection.execute(
            "SELECT kind, name FROM sources WHERE id = 'src_existing'"
        ).fetchone()
        assert tuple(source) == ("folder", "原有目录")
        linked = connection.execute(
            "SELECT item_id FROM item_sources WHERE source_id = 'src_existing'"
        ).fetchone()
        assert linked["item_id"] == "item_existing"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        source_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sources'"
        ).fetchone()["sql"]
        assert "'rss'" in source_sql


def test_confirmed_candidate_has_enforced_cyclic_registry_integrity(
    client: TestClient,
) -> None:
    candidate = propose(client).json()
    confirmed = confirm(client, candidate)
    assert confirmed.status_code == 200
    with client.app.state.service.database.connect() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(reliable_source_candidates)"
        ).fetchall()
        assert any(row["table"] == "reliable_sources" for row in foreign_keys)
