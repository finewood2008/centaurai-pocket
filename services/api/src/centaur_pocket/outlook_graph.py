from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlencode

from .outlook_security import (
    OutlookSecurityError,
    normalize_outlook_client_id,
    normalize_outlook_tenant,
    validate_graph_delta_url,
    validate_microsoft_verification_uri,
)
from .outlook_transport import (
    DirectHTTPSOutlookTransport,
    OutlookHttpRequest,
    OutlookHttpResponse,
    OutlookTransport,
    OutlookTransportError,
)

OUTLOOK_SCOPES = ("offline_access", "Mail.ReadWrite", "Mail.Send")
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
MAX_OAUTH_RESPONSE_BYTES = 128 * 1024
MAX_GRAPH_JSON_BYTES = 2 * 1024 * 1024


class OutlookRemoteError(RuntimeError):
    """A bounded, sanitized error returned to the mail domain service."""

    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        retry_after_seconds: int | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class DeviceAuthorization:
    flow: dict[str, Any]
    user_code: str
    verification_uri: str
    interval_seconds: int
    expires_in_seconds: int


@dataclass(frozen=True, slots=True)
class DevicePollResult:
    status: Literal["pending", "denied", "expired", "failed", "authorized"]
    token: dict[str, Any] | None = None
    error_code: str | None = None
    interval_seconds: int | None = None


def _utc_after(seconds: int) -> str:
    return (
        (datetime.now(UTC) + timedelta(seconds=seconds))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _parse_json_object(response: OutlookHttpResponse) -> dict[str, Any]:
    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
    if media_type.casefold() != "application/json" and not media_type.casefold().endswith(
        "+json"
    ):
        raise OutlookRemoteError("invalid_json_response", status=response.status)
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OutlookRemoteError(
            "invalid_json_response", status=response.status
        ) from error
    if not isinstance(payload, dict):
        raise OutlookRemoteError("invalid_json_response", status=response.status)
    return payload


def _bounded_text(value: object, *, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or len(stripped) > maximum:
        return None
    return stripped


def _retry_after(response: OutlookHttpResponse) -> int | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        parsed = int(raw)
    except ValueError:
        return None
    return min(max(parsed, 1), 3_600)


class OutlookGraphClient:
    """Strict OAuth/Graph client with fixed scopes, hosts and response bounds."""

    def __init__(
        self,
        *,
        client_id: str | None,
        tenant: str,
        transport: OutlookTransport | None = None,
    ):
        self.client_id = (
            normalize_outlook_client_id(client_id) if client_id else None
        )
        self.tenant = normalize_outlook_tenant(tenant)
        self.transport: OutlookTransport = transport or DirectHTTPSOutlookTransport()

    @property
    def configured(self) -> bool:
        return self.client_id is not None

    @property
    def authority_root(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0"

    def start_device_authorization(self) -> DeviceAuthorization:
        client_id = self._required_client_id()
        response = self._form_post(
            f"{self.authority_root}/devicecode",
            {"client_id": client_id, "scope": " ".join(OUTLOOK_SCOPES)},
        )
        if response.status != 200:
            raise self._http_error(response, "device_authorization_failed")
        payload = _parse_json_object(response)
        device_code = _bounded_text(payload.get("device_code"), maximum=16_384)
        user_code = _bounded_text(payload.get("user_code"), maximum=64)
        verification_uri = _bounded_text(
            payload.get("verification_uri") or payload.get("verification_url"),
            maximum=2_048,
        )
        try:
            interval = int(payload.get("interval", 5))
            expires_in = int(payload.get("expires_in"))
        except (TypeError, ValueError) as error:
            raise OutlookRemoteError("invalid_device_authorization") from error
        if (
            device_code is None
            or user_code is None
            or verification_uri is None
            or not 1 <= interval <= 60
            or not 60 <= expires_in <= 3_600
        ):
            raise OutlookRemoteError("invalid_device_authorization")
        verification_uri = validate_microsoft_verification_uri(verification_uri)
        flow = {
            "device_code": device_code,
            "user_code": user_code,
            "verification_uri": verification_uri,
            "interval": interval,
            "expires_in": expires_in,
            "requested_scopes": list(OUTLOOK_SCOPES),
        }
        return DeviceAuthorization(
            flow=flow,
            user_code=user_code,
            verification_uri=verification_uri,
            interval_seconds=interval,
            expires_in_seconds=expires_in,
        )

    def poll_device_authorization(
        self, flow: dict[str, Any]
    ) -> DevicePollResult:
        client_id = self._required_client_id()
        device_code = _bounded_text(flow.get("device_code"), maximum=16_384)
        if device_code is None or flow.get("requested_scopes") != list(OUTLOOK_SCOPES):
            raise OutlookRemoteError("invalid_device_authorization")
        response = self._form_post(
            f"{self.authority_root}/token",
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id,
                "device_code": device_code,
            },
        )
        if response.status == 200:
            payload = _parse_json_object(response)
            return DevicePollResult(status="authorized", token=self._token(payload))
        if response.status in {429, 500, 502, 503, 504}:
            raise self._http_error(response, "authorization_temporarily_unavailable")
        payload = _parse_json_object(response)
        oauth_error = payload.get("error")
        if oauth_error == "authorization_pending":
            return DevicePollResult(status="pending", error_code="authorization_pending")
        if oauth_error == "slow_down":
            try:
                current = int(flow.get("interval", 5))
            except (TypeError, ValueError):
                current = 5
            interval = min(max(current + 5, 5), 60)
            return DevicePollResult(
                status="pending",
                error_code="slow_down",
                interval_seconds=interval,
            )
        if oauth_error in {"authorization_declined", "access_denied"}:
            return DevicePollResult(status="denied", error_code="authorization_denied")
        if oauth_error in {"expired_token", "invalid_grant"}:
            return DevicePollResult(status="expired", error_code="authorization_expired")
        return DevicePollResult(status="failed", error_code="authorization_failed")

    def refresh_token(self, token: dict[str, Any]) -> dict[str, Any]:
        client_id = self._required_client_id()
        refresh_token = _bounded_text(token.get("refresh_token"), maximum=65_536)
        if refresh_token is None:
            raise OutlookRemoteError("reauthorization_required", status=401)
        response = self._form_post(
            f"{self.authority_root}/token",
            {
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_token,
                "scope": " ".join(OUTLOOK_SCOPES),
            },
        )
        if response.status in {429, 500, 502, 503, 504}:
            raise self._http_error(response, "token_refresh_failed")
        if response.status != 200:
            payload = _parse_json_object(response)
            if payload.get("error") == "invalid_client":
                raise OutlookRemoteError("connector_misconfigured", status=401)
            if payload.get("error") in {"invalid_grant", "interaction_required"}:
                raise OutlookRemoteError("reauthorization_required", status=401)
            raise self._http_error(response, "token_refresh_failed")
        refreshed = self._token(
            _parse_json_object(response),
            require_refresh_token=False,
        )
        refreshed["refresh_token"] = refreshed.get("refresh_token") or refresh_token
        return refreshed

    def graph_request(
        self,
        method: str,
        path_or_url: str,
        *,
        access_token: str,
        body: dict[str, Any] | None = None,
        max_bytes: int = MAX_GRAPH_JSON_BYTES,
        prefer: str | None = None,
        accept: str = "application/json",
    ) -> OutlookHttpResponse:
        if path_or_url.startswith("/"):
            url = f"{GRAPH_ROOT}{path_or_url}"
        elif path_or_url.startswith(f"{GRAPH_ROOT}/"):
            try:
                url = validate_graph_delta_url(path_or_url)
            except OutlookSecurityError as error:
                raise OutlookRemoteError("graph_url_not_allowed") from error
        else:
            raise OutlookRemoteError("graph_url_not_allowed")
        token = _bounded_text(access_token, maximum=65_536)
        if token is None:
            raise OutlookRemoteError("reauthorization_required", status=401)
        headers = {"Accept": accept, "Authorization": f"Bearer {token}"}
        encoded: bytes | None = None
        if body is not None:
            encoded = json.dumps(
                body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if prefer is not None:
            headers["Prefer"] = prefer
        try:
            return self.transport.request(
                OutlookHttpRequest(
                    method=method,
                    url=url,
                    headers=headers,
                    body=encoded,
                    max_bytes=max_bytes,
                )
            )
        except OutlookTransportError as error:
            raise OutlookRemoteError(f"transport_{error.code}") from error

    @staticmethod
    def json_object(response: OutlookHttpResponse) -> dict[str, Any]:
        return _parse_json_object(response)

    @staticmethod
    def http_error(response: OutlookHttpResponse, fallback: str) -> OutlookRemoteError:
        return OutlookGraphClient._http_error(response, fallback)

    def _form_post(self, url: str, values: dict[str, str]) -> OutlookHttpResponse:
        try:
            return self.transport.request(
                OutlookHttpRequest(
                    method="POST",
                    url=url,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    body=urlencode(values).encode("ascii"),
                    max_bytes=MAX_OAUTH_RESPONSE_BYTES,
                )
            )
        except OutlookTransportError as error:
            raise OutlookRemoteError(f"transport_{error.code}") from error

    def _required_client_id(self) -> str:
        if self.client_id is None:
            raise OutlookRemoteError("connector_not_configured")
        return self.client_id

    @staticmethod
    def _token(
        payload: dict[str, Any], *, require_refresh_token: bool = True
    ) -> dict[str, Any]:
        access_token = _bounded_text(payload.get("access_token"), maximum=65_536)
        refresh_token = _bounded_text(payload.get("refresh_token"), maximum=65_536)
        token_type = _bounded_text(payload.get("token_type"), maximum=32)
        scope_value = _bounded_text(payload.get("scope"), maximum=4_096)
        try:
            expires_in = int(payload.get("expires_in"))
        except (TypeError, ValueError) as error:
            raise OutlookRemoteError("invalid_token_response") from error
        granted = {value.casefold() for value in (scope_value or "").split()}
        if (
            access_token is None
            or (require_refresh_token and refresh_token is None)
            or token_type is None
            or token_type.casefold() != "bearer"
            or not 60 <= expires_in <= 86_400
            or not {"mail.readwrite", "mail.send"}.issubset(granted)
        ):
            raise OutlookRemoteError("invalid_token_response")
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "scope": list(OUTLOOK_SCOPES),
            "expires_at": _utc_after(expires_in),
        }

    @staticmethod
    def _http_error(
        response: OutlookHttpResponse, fallback: str
    ) -> OutlookRemoteError:
        code = fallback
        if response.status == 401:
            code = "reauthorization_required"
        elif response.status == 403:
            code = "permission_denied"
        elif response.status == 404:
            code = "remote_not_found"
        elif response.status == 409:
            code = "remote_conflict"
        elif response.status == 410:
            code = "sync_state_expired"
        elif response.status == 429:
            code = "throttled"
        elif response.status >= 500:
            code = "remote_unavailable"
        return OutlookRemoteError(
            code,
            status=response.status,
            retry_after_seconds=_retry_after(response),
        )
