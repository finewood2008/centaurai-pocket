from __future__ import annotations

import json
from collections.abc import Iterable
from urllib.parse import parse_qs

import pytest

from centaur_pocket.outlook_graph import (
    OUTLOOK_SCOPES,
    OutlookGraphClient,
    OutlookRemoteError,
)
from centaur_pocket.outlook_security import OutlookSecurityError
from centaur_pocket.outlook_transport import (
    OutlookHttpRequest,
    OutlookHttpResponse,
    OutlookTransportError,
)


class FakeOutlookTransport:
    def __init__(
        self,
        responses: Iterable[OutlookHttpResponse | Exception],
    ) -> None:
        self.responses = list(responses)
        self.requests: list[OutlookHttpRequest] = []

    def request(self, request: OutlookHttpRequest) -> OutlookHttpResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected Outlook request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def json_response(
    status: int,
    payload: object,
    *,
    headers: dict[str, str] | None = None,
) -> OutlookHttpResponse:
    return OutlookHttpResponse(
        status=status,
        headers={"content-type": "application/json", **(headers or {})},
        body=json.dumps(payload).encode(),
    )


def graph_client(
    transport: FakeOutlookTransport,
    *,
    tenant: str = "common",
) -> OutlookGraphClient:
    return OutlookGraphClient(
        client_id="88f7ec22-5d55-45e8-9709-e4d7786f1a04",
        tenant=tenant,
        transport=transport,
    )


def test_device_authorization_uses_only_fixed_public_client_parameters() -> None:
    transport = FakeOutlookTransport(
        [
            json_response(
                200,
                {
                    "device_code": "opaque-device-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://microsoft.com/devicelogin",
                    "interval": 5,
                    "expires_in": 900,
                    "verification_uri_complete": (
                        "https://microsoft.com/devicelogin?otc=must-not-be-used"
                    ),
                },
            )
        ]
    )

    authorization = graph_client(transport).start_device_authorization()

    request = transport.requests[0]
    form = parse_qs((request.body or b"").decode())
    assert request.url == (
        "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode"
    )
    assert form == {
        "client_id": ["88f7ec22-5d55-45e8-9709-e4d7786f1a04"],
        "scope": [" ".join(OUTLOOK_SCOPES)],
    }
    assert "client_secret" not in form
    assert authorization.user_code == "ABCD-EFGH"
    assert authorization.verification_uri == "https://microsoft.com/devicelogin"
    assert "verification_uri_complete" not in authorization.flow


def test_device_authorization_rejects_a_microsoft_lookalike_uri() -> None:
    transport = FakeOutlookTransport(
        [
            json_response(
                200,
                {
                    "device_code": "opaque-device-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": (
                        "https://microsoft.com.evil.example/devicelogin"
                    ),
                    "interval": 5,
                    "expires_in": 900,
                },
            )
        ]
    )

    with pytest.raises(
        OutlookSecurityError, match="Microsoft 登录地址不在允许范围内"
    ):
        graph_client(transport).start_device_authorization()


def test_device_poll_returns_sanitized_states_and_requires_fixed_scope() -> None:
    responses = [
        json_response(400, {"error": "authorization_pending", "trace_id": "drop"}),
        json_response(400, {"error": "slow_down", "error_description": "drop"}),
        json_response(
            200,
            {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "token_type": "Bearer",
                "scope": "Mail.ReadWrite Mail.Send",
                "expires_in": 3600,
            },
        ),
    ]
    transport = FakeOutlookTransport(responses)
    client = graph_client(transport)
    flow = {
        "device_code": "opaque-device-code",
        "interval": 5,
        "requested_scopes": list(OUTLOOK_SCOPES),
    }

    pending = client.poll_device_authorization(flow)
    slower = client.poll_device_authorization(flow)
    authorized = client.poll_device_authorization(flow)

    assert pending.status == "pending"
    assert pending.error_code == "authorization_pending"
    assert slower.status == "pending"
    assert slower.interval_seconds == 10
    assert authorized.status == "authorized"
    assert authorized.token is not None
    assert authorized.token["refresh_token"] == "refresh-token"
    assert not hasattr(pending, "trace_id")
    for request in transport.requests:
        assert b"client_secret" not in (request.body or b"")

    with pytest.raises(OutlookRemoteError, match="invalid_device_authorization"):
        client.poll_device_authorization(
            {**flow, "requested_scopes": ["Mail.Read"]}
        )


def test_refresh_keeps_rotating_public_token_when_response_omits_refresh_token() -> None:
    transport = FakeOutlookTransport(
        [
            json_response(
                200,
                {
                    "access_token": "new-access-token",
                    "token_type": "Bearer",
                    "scope": "Mail.ReadWrite Mail.Send",
                    "expires_in": 3600,
                },
            )
        ]
    )

    refreshed = graph_client(transport).refresh_token(
        {"refresh_token": "existing-refresh-token"}
    )

    assert refreshed["access_token"] == "new-access-token"
    assert refreshed["refresh_token"] == "existing-refresh-token"
    form = parse_qs((transport.requests[0].body or b"").decode())
    assert form["grant_type"] == ["refresh_token"]
    assert form["scope"] == [" ".join(OUTLOOK_SCOPES)]
    assert "client_secret" not in form


def test_oauth_throttling_and_transport_loss_are_never_retried_or_leaked() -> None:
    throttled = FakeOutlookTransport(
        [
            OutlookHttpResponse(
                status=429,
                headers={"retry-after": "17", "content-type": "text/plain"},
                body=b"raw upstream details must not be parsed",
            )
        ]
    )
    flow = {
        "device_code": "opaque-device-code",
        "interval": 5,
        "requested_scopes": list(OUTLOOK_SCOPES),
    }
    with pytest.raises(OutlookRemoteError) as throttled_error:
        graph_client(throttled).poll_device_authorization(flow)
    assert throttled_error.value.code == "throttled"
    assert throttled_error.value.retry_after_seconds == 17
    assert len(throttled.requests) == 1

    failed = FakeOutlookTransport([OutlookTransportError("connection_failed")])
    with pytest.raises(OutlookRemoteError) as transport_error:
        graph_client(failed).start_device_authorization()
    assert transport_error.value.code == "transport_connection_failed"
    assert len(failed.requests) == 1


def test_graph_continuation_is_inbox_delta_only_and_profile_scope_is_absent() -> None:
    delta_url = (
        "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta"
        "?$skiptoken=opaque"
    )
    transport = FakeOutlookTransport([json_response(200, {"value": []})])
    client = graph_client(transport)

    response = client.graph_request(
        "GET",
        delta_url,
        access_token="access-token",
    )

    assert response.status == 200
    assert transport.requests[0].url == delta_url
    assert transport.requests[0].headers["Authorization"] == "Bearer access-token"
    assert not hasattr(client, "get_profile")
    assert "User.Read" not in OUTLOOK_SCOPES

    with pytest.raises(OutlookRemoteError, match="graph_url_not_allowed"):
        client.graph_request(
            "GET",
            "https://graph.microsoft.com/v1.0/me/messages?$top=1",
            access_token="access-token",
        )
