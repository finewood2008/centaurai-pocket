from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from centaur_pocket.outlook_mail import SEND_INTENT_PROPERTY_ID
from centaur_pocket.outlook_transport import (
    OutlookHttpRequest,
    OutlookHttpResponse,
    OutlookTransportError,
)
from centaur_pocket.service import PocketError


def json_response(status: int, payload: object) -> OutlookHttpResponse:
    return OutlookHttpResponse(
        status=status,
        headers={"content-type": "application/json"},
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )


class QueuedTransport:
    def __init__(
        self, responses: list[OutlookHttpResponse | Exception]
    ) -> None:
        self.responses = responses
        self.requests: list[OutlookHttpRequest] = []

    def request(self, request: OutlookHttpRequest) -> OutlookHttpResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError(f"unexpected Outlook request: {request.url}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class AttachmentTransport:
    graph_attachment_id = "AQMk-secret-graph-attachment-id"
    content = b"safe attachment text\n"

    def __init__(self) -> None:
        self.requests: list[OutlookHttpRequest] = []

    def request(self, request: OutlookHttpRequest) -> OutlookHttpResponse:
        self.requests.append(request)
        path = urlsplit(request.url).path
        if request.method == "GET" and path.endswith(
            "/immutable-incoming-1/attachments"
        ):
            return json_response(
                200,
                {
                    "value": [
                        {
                            "@odata.type": "#microsoft.graph.fileAttachment",
                            "id": self.graph_attachment_id,
                            "name": "notes.txt",
                            "contentType": "text/plain",
                            "size": len(self.content),
                            "isInline": False,
                        }
                    ]
                },
            )
        if request.method == "GET" and path.endswith(
            f"/{self.graph_attachment_id}/$value"
        ):
            return OutlookHttpResponse(
                status=200,
                headers={"content-type": "application/octet-stream"},
                body=self.content,
            )
        raise AssertionError(f"unexpected attachment request: {request.method} {path}")


class FakeGraphMailbox:
    """Stateful, network-free Graph double for the draft/send state machine."""

    remote_id = "immutable-remote-draft-1"
    sent_folder_id = "immutable-sent-folder"

    def __init__(self) -> None:
        self.requests: list[OutlookHttpRequest] = []
        self.message: dict[str, Any] | None = None
        self.attachments: list[dict[str, Any]] = []
        self.lose_prepare_response = False
        self.lose_send_response = False
        self.attach_after_send = False
        self.attachment_lookup_failures = 0
        self.drafts_lookup_unavailable = False
        self.sent_lookup_unavailable = False

    @property
    def send_count(self) -> int:
        return sum(
            request.method == "POST"
            and urlsplit(request.url).path.endswith(f"/{self.remote_id}/send")
            for request in self.requests
        )

    @property
    def create_count(self) -> int:
        return sum(
            request.method == "POST"
            and urlsplit(request.url).path == "/v1.0/me/messages"
            for request in self.requests
        )

    def request(self, request: OutlookHttpRequest) -> OutlookHttpResponse:
        self.requests.append(request)
        parsed = urlsplit(request.url)
        path = parsed.path

        if request.method == "POST" and path == "/v1.0/me/messages":
            payload = json.loads((request.body or b"{}").decode("utf-8"))
            self.message = {
                "id": self.remote_id,
                "isDraft": True,
                "changeKey": "change-key-1",
                "subject": payload["subject"],
                "body": deepcopy(payload["body"]),
                "toRecipients": deepcopy(payload["toRecipients"]),
                "ccRecipients": deepcopy(payload["ccRecipients"]),
                "bccRecipients": deepcopy(payload["bccRecipients"]),
                "replyTo": deepcopy(payload["replyTo"]),
                "from": {
                    "emailAddress": {
                        "name": "Mailbox Owner",
                        "address": "owner@example.com",
                    }
                },
                "sender": {
                    "emailAddress": {
                        "name": "Mailbox Owner",
                        "address": "owner@example.com",
                    }
                },
                "parentFolderId": "immutable-drafts-folder",
                "sentDateTime": None,
                "hasAttachments": False,
                "isReadReceiptRequested": payload["isReadReceiptRequested"],
                "isDeliveryReceiptRequested": payload[
                    "isDeliveryReceiptRequested"
                ],
                "importance": payload["importance"],
                "singleValueExtendedProperties": deepcopy(
                    payload["singleValueExtendedProperties"]
                ),
            }
            if self.lose_prepare_response:
                raise OutlookTransportError("connection_failed")
            return json_response(201, {"id": self.remote_id, "isDraft": True})

        if request.method == "GET" and path.endswith("/attachments"):
            if self.attachment_lookup_failures > 0:
                self.attachment_lookup_failures -= 1
                raise OutlookTransportError("connection_failed")
            if self.message is None:
                return json_response(404, {"error": {"code": "notFound"}})
            return json_response(200, {"value": deepcopy(self.attachments)})

        if request.method == "GET" and path.endswith(f"/{self.remote_id}"):
            if self.message is None:
                return json_response(404, {"error": {"code": "notFound"}})
            return json_response(200, deepcopy(self.message))

        if request.method == "GET" and path.endswith(
            "/mailFolders/drafts/messages"
        ):
            if self.drafts_lookup_unavailable:
                raise OutlookTransportError("connection_failed")
            values: list[dict[str, Any]] = []
            if self.message is not None and self.message["isDraft"] is True:
                values.append({"id": self.remote_id, "isDraft": True})
            return json_response(200, {"value": values})

        if request.method == "GET" and path.endswith("/mailFolders/sentitems"):
            if self.sent_lookup_unavailable:
                raise OutlookTransportError("connection_failed")
            return json_response(200, {"id": self.sent_folder_id})

        if request.method == "GET" and path.endswith(
            "/mailFolders/sentitems/messages"
        ):
            if self.sent_lookup_unavailable:
                raise OutlookTransportError("connection_failed")
            values = []
            if self.message is not None and self.message["isDraft"] is False:
                values.append(
                    {
                        "id": self.remote_id,
                        "isDraft": False,
                        "parentFolderId": self.sent_folder_id,
                        "sentDateTime": self.message["sentDateTime"],
                    }
                )
            return json_response(200, {"value": values})

        if request.method == "POST" and path.endswith(f"/{self.remote_id}/send"):
            if self.message is None:
                return json_response(404, {"error": {"code": "notFound"}})
            self.message["isDraft"] = False
            self.message["changeKey"] = "change-key-sent"
            self.message["parentFolderId"] = self.sent_folder_id
            self.message["sentDateTime"] = "2026-08-02T01:02:03Z"
            if self.attach_after_send:
                # Inline attachments can coexist with hasAttachments=false.
                self.attachments = [{"id": "inline-attachment"}]
            if self.lose_send_response:
                raise OutlookTransportError("connection_failed")
            return json_response(202, {})

        raise AssertionError(f"unexpected fake Graph request: {request.method} {path}")


def _write_headers(
    owner_headers: dict[str, str], key: str, version: int
) -> dict[str, str]:
    return {
        **owner_headers,
        "Idempotency-Key": key,
        "X-Device-ID": "pytest-desktop",
        "If-Match": f'"{version}"',
    }


def _seed_connected_mailbox(client: TestClient) -> tuple[str, str]:
    service = client.app.state.mail_service
    now = "2026-08-02T00:00:00Z"
    account_id = "outlook-account-1"
    message_id = "outlook-message-1"
    token = {
        "access_token": "encrypted-at-rest-access-token",
        "refresh_token": "encrypted-at-rest-refresh-token",
        "expires_at": "2099-01-01T00:00:00Z",
    }
    ciphertext = service._encrypt_json("token_cache", account_id, token)
    with service.database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO sources(
                id, kind, provider, name, config_json, schedule, enabled,
                created_at, updated_at, last_sync_at
            ) VALUES (?, 'outlook_mail', 'microsoft_graph', ?, '{}', 'manual', 1,
                      ?, ?, NULL)
            """,
            ("outlook-source-1", "Work Outlook", now, now),
        )
        connection.execute(
            """
            INSERT INTO outlook_accounts(
                id, source_id, account_label, client_id, tenant, scopes_json,
                mailbox_fingerprint, status, sync_enabled,
                sync_interval_minutes, version, connected_at, last_sync_at,
                next_sync_at, last_error_code, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'common', ?, ?, 'connected', 1, 15, 1, ?,
                      NULL, NULL, NULL, ?, ?)
            """,
            (
                account_id,
                "outlook-source-1",
                "Work Outlook",
                "88f7ec22-5d55-45e8-9709-e4d7786f1a04",
                '["offline_access","Mail.ReadWrite","Mail.Send"]',
                "f" * 40,
                now,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO outlook_credentials(account_id, token_ciphertext, updated_at)
            VALUES (?, ?, ?)
            """,
            (account_id, ciphertext, now),
        )
        connection.execute(
            """
            INSERT INTO outlook_messages(
                id, account_id, graph_message_id, conversation_id,
                internet_message_id, subject, sender_json,
                to_recipients_json, cc_recipients_json, body_preview,
                importance, is_read, has_attachments, received_at, sent_at,
                change_key, status, version, created_at, updated_at, deleted_at
            ) VALUES (?, ?, ?, NULL, NULL, ?, ?, '[]', '[]', ?, 'normal', 0, 0,
                      ?, NULL, ?, 'active', 1, ?, ?, NULL)
            """,
            (
                message_id,
                account_id,
                "immutable-incoming-1",
                "Project update",
                json.dumps(
                    {"name": "Alice", "address": "alice@example.com"},
                    ensure_ascii=False,
                ),
                "Please reply",
                now,
                "incoming-change-key",
                now,
                now,
            ),
        )
    return account_id, message_id


def _create_and_prepare(
    client: TestClient,
    owner_headers: dict[str, str],
    fake: FakeGraphMailbox,
    *,
    suffix: str,
    body_text: str = "Approved reply",
) -> tuple[dict[str, Any], dict[str, Any]]:
    client.app.state.mail_service.replace_transport_for_testing(fake)
    _, message_id = _seed_connected_mailbox(client)
    draft_response = client.post(
        f"/api/v1/mail/messages/{message_id}/reply-drafts",
        json={"expected_version": 1, "body_text": body_text},
        headers=_write_headers(owner_headers, f"draft-create-{suffix}", 1),
    )
    assert draft_response.status_code == 201, draft_response.text
    draft = draft_response.json()
    prepare_response = client.post(
        f"/api/v1/mail/drafts/{draft['id']}/prepare",
        json={"expected_version": draft["version"]},
        headers=_write_headers(
            owner_headers, f"draft-prepare-{suffix}", draft["version"]
        ),
    )
    assert prepare_response.status_code == 200, prepare_response.text
    return draft, prepare_response.json()


def _confirm(
    client: TestClient,
    owner_headers: dict[str, str],
    intent: dict[str, Any],
    *,
    suffix: str,
) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/mail/send-intents/{intent['id']}/confirm",
        json={
            "expected_version": intent["version"],
            "preview_hash": intent["preview_hash"],
            "confirmation": "send_exact_preview",
        },
        headers=_write_headers(
            owner_headers, f"send-confirm-{suffix}", intent["version"]
        ),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_mail_routes_allow_owner_and_bound_paired_device(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    owner_response = client.get("/api/v1/mail/accounts", headers=owner_headers)
    assert owner_response.status_code == 200
    assert owner_response.headers["cache-control"] == "no-store, max-age=0"
    assert client.get("/api/v1/mail/accounts").status_code == 401

    pairing = client.post("/api/v1/mobile/pairings", headers=owner_headers).json()
    claimed = client.post(
        "/api/v1/mobile/pairings/claim",
        json={
            "code": pairing["code"],
            "device_id": "paired-phone-1",
            "display_name": "测试手机",
            "platform": "android",
            "app_version": "1.0.0",
        },
    ).json()
    device_headers = {
        "Authorization": f"Bearer {claimed['access_token']}",
        "X-Device-ID": "paired-phone-1",
    }
    assert client.get("/api/v1/mail/accounts", headers=device_headers).status_code == 200
    assert (
        client.get(
            "/api/v1/mail/accounts",
            headers={**device_headers, "X-Device-ID": "another-phone"},
        ).status_code
        == 403
    )


def test_inbox_pagination_excludes_tombstones_server_side(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    account_id, _ = _seed_connected_mailbox(client)
    service = client.app.state.mail_service
    with service.database.transaction() as connection:
        connection.execute(
            "UPDATE outlook_messages SET status = 'deleted' WHERE id = ?",
            ("outlook-message-1",),
        )
        for index in range(51):
            state = "deleted" if index < 50 else "active"
            connection.execute(
                """
                INSERT INTO outlook_messages(
                    id, account_id, graph_message_id, subject, sender_json,
                    to_recipients_json, cc_recipients_json, body_preview,
                    importance, is_read, has_attachments, status, version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, '[]', '[]', '', 'normal', 0, 0, ?, 1, ?, ?)
                """,
                (
                    f"page-message-{index}",
                    account_id,
                    f"graph-page-{index}",
                    f"Message {index}",
                    '{"name":"Alice","address":"alice@example.com"}',
                    state,
                    f"2026-08-02T00:{index:02d}:00Z",
                    f"2026-08-02T00:{index:02d}:00Z",
                ),
            )

    response = client.get(
        f"/api/v1/mail/accounts/{account_id}/inbox?limit=50&offset=0",
        headers=owner_headers,
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert [item["id"] for item in response.json()["items"]] == ["page-message-50"]


def test_prepare_transport_loss_recovers_by_marker_without_second_create(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    fake = FakeGraphMailbox()
    fake.lose_prepare_response = True
    draft, intent = _create_and_prepare(
        client, owner_headers, fake, suffix="prepare-loss"
    )
    assert intent["status"] == "prepare_uncertain"
    assert intent["preview"]["from_address"] is None
    assert fake.create_count == 1

    fake.lose_prepare_response = False
    reconcile = client.post(
        f"/api/v1/mail/send-intents/{intent['id']}/reconcile",
        json={"expected_version": intent["version"]},
        headers=_write_headers(
            owner_headers, "prepare-loss-reconcile", intent["version"]
        ),
    )
    assert reconcile.status_code == 200, reconcile.text
    assert reconcile.json()["status"] == "ready"
    assert reconcile.json()["preview"]["from_address"] == "owner@example.com"
    assert fake.create_count == 1
    assert client.get(
        f"/api/v1/mail/drafts/{draft['id']}", headers=owner_headers
    ).json()["status"] == "prepared"


@pytest.mark.parametrize(
    "mutation",
    [
        "attachment",
        "body_suffix",
        "bidi_body",
        "reply_to",
        "read_receipt",
        "delivery_receipt",
        "importance",
        "sender",
        "recipient_name",
    ],
)
def test_remote_draft_mutation_fails_closed_before_send(
    client: TestClient,
    owner_headers: dict[str, str],
    mutation: str,
) -> None:
    fake = FakeGraphMailbox()
    draft, intent = _create_and_prepare(
        client, owner_headers, fake, suffix=f"tamper-{mutation}"
    )
    assert intent["status"] == "ready"
    assert fake.message is not None
    if mutation == "attachment":
        # Proves the attachment collection is authoritative even when this flag
        # misses an inline attachment.
        fake.message["hasAttachments"] = False
        fake.attachments = [{"id": "inline-attachment"}]
    elif mutation == "body_suffix":
        fake.message["body"]["content"] += " attacker suffix"
    elif mutation == "bidi_body":
        fake.message["body"]["content"] += "\u202e"
    elif mutation == "reply_to":
        fake.message["replyTo"] = [
            {"emailAddress": {"name": "Mallory", "address": "mallory@example.com"}}
        ]
    elif mutation == "read_receipt":
        fake.message["isReadReceiptRequested"] = True
    elif mutation == "delivery_receipt":
        fake.message["isDeliveryReceiptRequested"] = True
    elif mutation == "importance":
        fake.message["importance"] = "high"
    elif mutation == "sender":
        fake.message["sender"]["emailAddress"]["address"] = "other@example.com"
    elif mutation == "recipient_name":
        fake.message["toRecipients"][0]["emailAddress"]["name"] = "Mallory"

    failed = _confirm(
        client, owner_headers, intent, suffix=f"tamper-{mutation}"
    )
    assert failed["status"] == "failed"
    assert failed["last_error_code"] == "remote_draft_changed"
    assert fake.send_count == 0
    assert client.get(
        f"/api/v1/mail/drafts/{draft['id']}", headers=owner_headers
    ).json()["status"] == "canceled"

    replacement = client.post(
        "/api/v1/mail/messages/outlook-message-1/reply-drafts",
        json={"expected_version": 1, "body_text": "Replacement reply"},
        headers=_write_headers(
            owner_headers, f"replacement-{mutation}", 1
        ),
    )
    assert replacement.status_code == 201, replacement.text
    assert replacement.json()["id"] != draft["id"]


def test_exact_send_is_verified_once_and_never_exposes_remote_ids(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    fake = FakeGraphMailbox()
    _, intent = _create_and_prepare(client, owner_headers, fake, suffix="happy-send")
    assert intent["preview"]["from_address"] == "owner@example.com"

    sent = _confirm(client, owner_headers, intent, suffix="happy-send")

    assert sent["status"] == "sent_items_verified"
    assert sent["verified_at"] is not None
    assert fake.send_count == 1
    serialized = json.dumps(sent, ensure_ascii=False)
    assert fake.remote_id not in serialized
    assert SEND_INTENT_PROPERTY_ID not in serialized
    assert "encrypted-at-rest-access-token" not in serialized

    read_back = client.get(
        f"/api/v1/mail/send-intents/{intent['id']}", headers=owner_headers
    ).json()
    assert read_back["status"] == "sent_items_verified"
    assert fake.send_count == 1


def test_sent_attachment_prevents_exact_verification_after_single_send(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    fake = FakeGraphMailbox()
    fake.attach_after_send = True
    _, intent = _create_and_prepare(
        client, owner_headers, fake, suffix="sent-attachment"
    )

    result = _confirm(client, owner_headers, intent, suffix="sent-attachment")

    assert result["status"] == "verifying"
    assert result["last_error_code"] == "sent_item_content_mismatch"
    assert result["verified_at"] is None
    assert fake.send_count == 1

    reconcile = client.post(
        f"/api/v1/mail/send-intents/{intent['id']}/reconcile",
        json={"expected_version": result["version"]},
        headers=_write_headers(
            owner_headers, "sent-attachment-reconcile", result["version"]
        ),
    )
    assert reconcile.status_code == 200
    assert reconcile.json()["status"] == "verifying"
    assert reconcile.json()["last_error_code"] == "sent_item_content_mismatch"
    assert fake.send_count == 1


def test_send_response_loss_never_resends_and_reconcile_verifies(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    fake = FakeGraphMailbox()
    _, intent = _create_and_prepare(client, owner_headers, fake, suffix="send-loss")
    fake.lose_send_response = True
    fake.sent_lookup_unavailable = True

    ambiguous = _confirm(client, owner_headers, intent, suffix="send-loss")
    assert ambiguous["status"] == "send_uncertain"
    assert ambiguous["verified_at"] is None
    assert fake.send_count == 1

    fake.sent_lookup_unavailable = False
    reconciled = client.post(
        f"/api/v1/mail/send-intents/{intent['id']}/reconcile",
        json={"expected_version": ambiguous["version"]},
        headers=_write_headers(
            owner_headers, "send-loss-reconcile", ambiguous["version"]
        ),
    )
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["status"] == "sent_items_verified"
    assert fake.send_count == 1


def test_known_remote_prepare_uncertainty_detects_external_send(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    fake = FakeGraphMailbox()
    fake.attachment_lookup_failures = 1
    draft, intent = _create_and_prepare(
        client, owner_headers, fake, suffix="known-remote-external-send"
    )
    assert intent["status"] == "prepare_uncertain"
    assert fake.message is not None
    fake.message["isDraft"] = False
    fake.message["changeKey"] = "externally-sent-change-key"
    fake.message["parentFolderId"] = fake.sent_folder_id
    fake.message["sentDateTime"] = "2026-08-02T01:02:03Z"

    reconciled = client.post(
        f"/api/v1/mail/send-intents/{intent['id']}/reconcile",
        json={"expected_version": intent["version"]},
        headers=_write_headers(
            owner_headers,
            "known-remote-external-send-reconcile",
            intent["version"],
        ),
    )

    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["status"] == "sent_items_verified"
    assert fake.send_count == 0
    assert client.get(
        f"/api/v1/mail/drafts/{draft['id']}", headers=owner_headers
    ).json()["status"] == "sent"


def test_expired_prepare_uncertainty_checks_sent_items_before_unlocking(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    fake = FakeGraphMailbox()
    fake.lose_prepare_response = True
    draft, intent = _create_and_prepare(
        client, owner_headers, fake, suffix="expired-but-sent"
    )
    assert fake.message is not None
    fake.message["isDraft"] = False
    fake.message["parentFolderId"] = fake.sent_folder_id
    fake.message["sentDateTime"] = "2026-08-02T01:02:03Z"
    fake.message["changeKey"] = "change-key-sent-externally"
    fake.lose_prepare_response = False
    with client.app.state.service.database.transaction() as connection:
        connection.execute(
            "UPDATE outlook_send_intents SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00Z", intent["id"]),
        )

    response = client.post(
        f"/api/v1/mail/send-intents/{intent['id']}/reconcile",
        json={"expected_version": intent["version"]},
        headers=_write_headers(
            owner_headers, "expired-but-sent-reconcile", intent["version"]
        ),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "sent_items_verified"
    assert fake.send_count == 0
    assert client.get(
        f"/api/v1/mail/drafts/{draft['id']}", headers=owner_headers
    ).json()["status"] == "sent"


def test_second_get_401_requires_reauthorization_after_one_refresh(
    client: TestClient,
) -> None:
    account_id, _ = _seed_connected_mailbox(client)
    transport = QueuedTransport(
        [
            json_response(401, {"error": {"code": "InvalidAuthenticationToken"}}),
            json_response(
                200,
                {
                    "access_token": "refreshed-access-token",
                    "token_type": "Bearer",
                    "scope": "Mail.ReadWrite Mail.Send",
                    "expires_in": 3600,
                },
            ),
            json_response(401, {"error": {"code": "InvalidAuthenticationToken"}}),
        ]
    )
    service = client.app.state.mail_service
    service.graph.client_id = "88f7ec22-5d55-45e8-9709-e4d7786f1a04"
    service.replace_transport_for_testing(transport)

    with pytest.raises(PocketError) as raised:
        service._graph_request(account_id, "GET", "/me/messages/immutable-incoming-1")

    assert raised.value.status_code == 409
    assert [request.method for request in transport.requests] == ["GET", "POST", "GET"]
    assert sum(
        request.method == "GET"
        and urlsplit(request.url).hostname == "graph.microsoft.com"
        for request in transport.requests
    ) == 2
    with service.database.connect() as connection:
        account = connection.execute(
            "SELECT status, sync_enabled, next_sync_at FROM outlook_accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
    assert account["status"] == "action_required"
    assert account["sync_enabled"] == 0
    assert account["next_sync_at"] is None


def test_graph_post_401_refreshes_once_but_never_replays_post(
    client: TestClient,
) -> None:
    account_id, _ = _seed_connected_mailbox(client)
    transport = QueuedTransport(
        [
            json_response(401, {"error": {"code": "InvalidAuthenticationToken"}}),
            json_response(
                200,
                {
                    "access_token": "refreshed-access-token",
                    "token_type": "Bearer",
                    "scope": "Mail.ReadWrite Mail.Send",
                    "expires_in": 3600,
                },
            ),
        ]
    )
    service = client.app.state.mail_service
    service.graph.client_id = "88f7ec22-5d55-45e8-9709-e4d7786f1a04"
    service.replace_transport_for_testing(transport)

    response = service._graph_request(
        account_id,
        "POST",
        "/me/messages/immutable-remote-draft-1/send",
    )

    assert response.status == 401
    graph_posts = [
        request
        for request in transport.requests
        if request.method == "POST"
        and urlsplit(request.url).hostname == "graph.microsoft.com"
    ]
    assert len(graph_posts) == 1
    assert len(transport.requests) == 2
    with service.database.connect() as connection:
        account_status = connection.execute(
            "SELECT status FROM outlook_accounts WHERE id = ?", (account_id,)
        ).fetchone()["status"]
    assert account_status == "connected"


def test_reauthorization_mailbox_mismatch_preserves_old_account_and_token(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    account_id, _ = _seed_connected_mailbox(client)
    service = client.app.state.mail_service
    service.graph.client_id = "88f7ec22-5d55-45e8-9709-e4d7786f1a04"
    with service.database.transaction() as connection:
        connection.execute(
            """
            UPDATE outlook_accounts
            SET status = 'action_required', sync_enabled = 0,
                last_error_code = 'reauthorization_required'
            WHERE id = ?
            """,
            (account_id,),
        )
        old_ciphertext = connection.execute(
            "SELECT token_ciphertext FROM outlook_credentials WHERE account_id = ?",
            (account_id,),
        ).fetchone()["token_ciphertext"]
    transport = QueuedTransport(
        [
            json_response(
                200,
                {
                    "device_code": "new-device-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://microsoft.com/devicelogin",
                    "interval": 1,
                    "expires_in": 900,
                },
            ),
            json_response(
                200,
                {
                    "access_token": "different-mailbox-access-token",
                    "refresh_token": "different-mailbox-refresh-token",
                    "token_type": "Bearer",
                    "scope": "Mail.ReadWrite Mail.Send",
                    "expires_in": 3600,
                },
            ),
            json_response(200, {"id": "different-mailbox-inbox-id"}),
        ]
    )
    service.replace_transport_for_testing(transport)
    created = client.post(
        "/api/v1/mail/outlook/device-authorizations",
        json={"account_label": "Work Outlook"},
        headers={
            **owner_headers,
            "Idempotency-Key": "reauth-mismatch-create",
            "X-Device-ID": "pytest-desktop",
        },
    )
    assert created.status_code == 201, created.text
    authorization = created.json()
    with service.database.transaction() as connection:
        connection.execute(
            "UPDATE outlook_device_authorizations SET next_poll_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00Z", authorization["id"]),
        )

    polled = client.post(
        f"/api/v1/mail/outlook/device-authorizations/{authorization['id']}/poll",
        json={"expected_version": authorization["version"]},
        headers=_write_headers(
            owner_headers, "reauth-mismatch-poll", authorization["version"]
        ),
    )

    assert polled.status_code == 200, polled.text
    assert polled.json()["status"] == "failed"
    assert polled.json()["error_code"] == "mailbox_identity_mismatch"
    serialized = json.dumps(polled.json(), ensure_ascii=False)
    assert "different-mailbox" not in serialized
    with service.database.connect() as connection:
        account = connection.execute(
            "SELECT status, last_error_code FROM outlook_accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        credential = connection.execute(
            "SELECT token_ciphertext FROM outlook_credentials WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        flow = connection.execute(
            "SELECT device_flow_ciphertext FROM outlook_device_authorizations WHERE id = ?",
            (authorization["id"],),
        ).fetchone()
    assert account["status"] == "action_required"
    assert account["last_error_code"] == "reauthorization_required"
    assert credential["token_ciphertext"] == old_ciphertext
    assert flow["device_flow_ciphertext"] is None


def test_attachment_ids_are_opaque_and_archive_is_encrypted_owner_only(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    _, message_id = _seed_connected_mailbox(client)
    service = client.app.state.mail_service
    transport = AttachmentTransport()
    service.replace_transport_for_testing(transport)

    listed = client.get(
        f"/api/v1/mail/messages/{message_id}/attachments", headers=owner_headers
    )
    assert listed.status_code == 200, listed.text
    item = listed.json()["items"][0]
    assert item["id"] != transport.graph_attachment_id
    assert transport.graph_attachment_id not in listed.text

    archived = client.post(
        f"/api/v1/mail/messages/{message_id}/attachments/{item['id']}/archive",
        json={"expected_version": 1},
        headers=_write_headers(owner_headers, "archive-safe-text", 1),
    )
    assert archived.status_code == 201, archived.text
    assert transport.graph_attachment_id not in archived.text
    archive = archived.json()
    with service.database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM outlook_archived_attachments WHERE id = ?",
            (archive["id"],),
        ).fetchone()
    blob_path = service.data_root / row["archive_relpath"]
    encrypted = blob_path.read_bytes()
    assert encrypted != transport.content
    assert transport.content not in encrypted
    assert blob_path.stat().st_mode & 0o777 == 0o600
    assert service.secret_box.decrypt_bytes(
        "attachment_archive", archive["id"], encrypted
    ) == transport.content
    document = service.workspace_service.get_document(
        "ws_default", archive["document_id"]
    )
    assert document["access_scope"] == "owner_only"
    assert document["source_item_id"] is None
    assert document["storage_ref"].startswith("mailblob://")
    assert transport.graph_attachment_id not in json.dumps(
        document, ensure_ascii=False
    )


def test_attachment_archive_rejects_symlink_directory(
    client: TestClient,
    owner_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    _, message_id = _seed_connected_mailbox(client)
    service = client.app.state.mail_service
    transport = AttachmentTransport()
    service.replace_transport_for_testing(transport)
    item = client.get(
        f"/api/v1/mail/messages/{message_id}/attachments", headers=owner_headers
    ).json()["items"][0]
    outside = tmp_path / "outside-archive"
    outside.mkdir()
    (service.data_root / "outlook-attachments").symlink_to(
        outside, target_is_directory=True
    )

    response = client.post(
        f"/api/v1/mail/messages/{message_id}/attachments/{item['id']}/archive",
        json={"expected_version": 1},
        headers=_write_headers(owner_headers, "archive-symlink-reject", 1),
    )

    assert response.status_code == 503
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "label",
    [" Work Outlook", "Work Outlook\nFinance", "Work\tOutlook", "Work\u202eOutlook"],
)
def test_account_label_rejects_whitespace_controls_and_bidi(
    client: TestClient,
    owner_headers: dict[str, str],
    label: str,
) -> None:
    response = client.post(
        "/api/v1/mail/outlook/device-authorizations",
        json={"account_label": label},
        headers={
            **owner_headers,
            "Idempotency-Key": f"unsafe-label-{abs(hash(label))}",
            "X-Device-ID": "pytest-desktop",
        },
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "邮件请求格式无效"}
