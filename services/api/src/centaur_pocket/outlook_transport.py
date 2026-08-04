from __future__ import annotations

import http.client
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

OUTLOOK_NETWORK_HOSTS = frozenset(
    {"login.microsoftonline.com", "graph.microsoft.com"}
)
MAX_RESPONSE_HEADERS = 64
MAX_HEADER_NAME_CHARS = 64
MAX_HEADER_VALUE_CHARS = 4_096
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 60.0
MAX_REQUEST_BODY_BYTES = 512 * 1024
MAX_RESPONSE_BODY_BYTES = 32 * 1024 * 1024
MAX_REQUEST_HEADERS = 16
ALLOWED_REQUEST_HEADERS = frozenset(
    {
        "accept",
        "authorization",
        "content-type",
        "prefer",
    }
)


class OutlookTransportError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OutlookHttpRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_bytes: int = 2 * 1024 * 1024


@dataclass(frozen=True)
class OutlookHttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class OutlookTransport(Protocol):
    def request(self, request: OutlookHttpRequest) -> OutlookHttpResponse: ...


class DirectHTTPSOutlookTransport:
    """Fixed-host HTTPS without environment proxies or redirect following."""

    def request(self, request: OutlookHttpRequest) -> OutlookHttpResponse:
        parsed = urlsplit(request.url)
        hostname = (parsed.hostname or "").lower()
        if (
            request.method not in {"GET", "POST", "PATCH", "DELETE"}
            or parsed.scheme != "https"
            or hostname not in OUTLOOK_NETWORK_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.fragment
            or not parsed.path.startswith("/")
            or len(request.url) > 32_768
            or request.timeout_seconds <= 0
            or request.timeout_seconds > MAX_TIMEOUT_SECONDS
            or request.max_bytes < 0
            or request.max_bytes > MAX_RESPONSE_BODY_BYTES
            or (request.body is not None and len(request.body) > MAX_REQUEST_BODY_BYTES)
        ):
            raise OutlookTransportError("request_not_allowed")

        request_headers = self._validated_request_headers(request.headers)

        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        deadline = time.monotonic() + request.timeout_seconds
        connection = http.client.HTTPSConnection(
            hostname,
            port=443,
            timeout=request.timeout_seconds,
            context=ssl.create_default_context(),
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
        headers = {
            **request_headers,
            "Accept-Encoding": "identity",
            "User-Agent": "CentaurAI-Pocket/0.3 OutlookConnector/1",
        }
        try:
            connection.request(
                request.method,
                target,
                body=request.body,
                headers=headers,
            )
            if deadline_reached.is_set() or time.monotonic() >= deadline:
                raise OutlookTransportError("deadline_exceeded")
            response = connection.getresponse()
            response_headers = self._validated_headers(response.getheaders())
            content_encoding = response_headers.get("content-encoding", "identity")
            if content_encoding.casefold() not in {"", "identity"}:
                raise OutlookTransportError("encoded_response_rejected")
            declared = response_headers.get("content-length")
            if declared is not None:
                try:
                    declared_length = int(declared)
                except ValueError as error:
                    raise OutlookTransportError("invalid_content_length") from error
                if declared_length < 0 or declared_length > request.max_bytes:
                    raise OutlookTransportError("response_too_large")

            chunks: list[bytes] = []
            received = 0
            while True:
                remaining = deadline - time.monotonic()
                if deadline_reached.is_set() or remaining <= 0:
                    raise OutlookTransportError("deadline_exceeded")
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read(min(65_536, request.max_bytes + 1 - received))
                if deadline_reached.is_set() or time.monotonic() >= deadline:
                    raise OutlookTransportError("deadline_exceeded")
                if not chunk:
                    break
                received += len(chunk)
                if received > request.max_bytes:
                    raise OutlookTransportError("response_too_large")
                chunks.append(chunk)
            return OutlookHttpResponse(response.status, response_headers, b"".join(chunks))
        except OutlookTransportError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as error:
            code = (
                "deadline_exceeded"
                if deadline_reached.is_set() or time.monotonic() >= deadline
                else "connection_failed"
            )
            raise OutlookTransportError(code) from error
        finally:
            timer.cancel()
            connection.close()

    @staticmethod
    def _validated_request_headers(values: dict[str, str]) -> dict[str, str]:
        if len(values) > MAX_REQUEST_HEADERS:
            raise OutlookTransportError("request_not_allowed")
        result: dict[str, str] = {}
        for name, value in values.items():
            normalized = name.casefold()
            if (
                normalized not in ALLOWED_REQUEST_HEADERS
                or not name
                or len(name) > MAX_HEADER_NAME_CHARS
                or len(value) > MAX_HEADER_VALUE_CHARS
                or any(ord(character) < 32 for character in name + value)
            ):
                raise OutlookTransportError("request_not_allowed")
            result[normalized] = value
        return result

    @staticmethod
    def _validated_headers(
        values: list[tuple[str, str]],
    ) -> dict[str, str]:
        if len(values) > MAX_RESPONSE_HEADERS:
            raise OutlookTransportError("too_many_headers")
        result: dict[str, str] = {}
        for name, value in values:
            if (
                not name
                or len(name) > MAX_HEADER_NAME_CHARS
                or len(value) > MAX_HEADER_VALUE_CHARS
                or any(ord(character) < 32 for character in name + value)
            ):
                raise OutlookTransportError("invalid_response_header")
            normalized = name.casefold()
            combined = (
                f"{result[normalized]}, {value}"
                if normalized in result
                else value
            )
            if len(combined) > MAX_HEADER_VALUE_CHARS:
                raise OutlookTransportError("invalid_response_header")
            result[normalized] = combined
        return result
