from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from centaur_pocket.config import Settings
from centaur_pocket.database import (
    LATEST_SCHEMA_VERSION,
    MIGRATION_V2,
    MIGRATION_V3,
    MIGRATION_V4,
    MIGRATION_V5,
    SCHEMA_V1,
    Database,
)
from centaur_pocket.outlook_security import (
    OUTLOOK_KEY_FILE,
    OutlookSecretBox,
    OutlookSecurityError,
    normalize_outlook_client_id,
    normalize_outlook_tenant,
    sanitize_outlook_text,
    validate_graph_delta_url,
    validate_microsoft_verification_uri,
)
from centaur_pocket.outlook_transport import (
    DirectHTTPSOutlookTransport,
    OutlookHttpRequest,
    OutlookTransportError,
)


def test_outlook_secret_box_round_trip_and_aad_binding(tmp_path: Path) -> None:
    box = OutlookSecretBox(tmp_path / "private")

    encrypted_text = box.encrypt_text("oauth-token", "account-1", "secret-token")
    encrypted_bytes = box.encrypt_bytes("attachment", "archive-1", b"document")

    assert "secret-token" not in encrypted_text
    assert box.decrypt_text("oauth-token", "account-1", encrypted_text) == "secret-token"
    assert box.decrypt_bytes("attachment", "archive-1", encrypted_bytes) == b"document"
    assert (tmp_path / "private" / OUTLOOK_KEY_FILE).stat().st_mode & 0o777 == 0o600

    with pytest.raises(OutlookSecurityError, match="无法解密"):
        box.decrypt_text("oauth-token", "account-2", encrypted_text)
    with pytest.raises(OutlookSecurityError, match="无法解密"):
        box.decrypt_bytes("attachment", "archive-2", encrypted_bytes)


def test_outlook_secret_box_rejects_tampering_and_unsafe_key_permissions(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "private"
    box = OutlookSecretBox(data_root)
    ciphertext = box.encrypt_text("oauth-token", "account-1", "secret-token")
    tampered = ciphertext[:-2] + ("AA" if ciphertext[-2:] != "AA" else "BB")

    with pytest.raises(OutlookSecurityError, match="无法解密"):
        OutlookSecretBox(data_root).decrypt_text(
            "oauth-token", "account-1", tampered
        )

    key_path = data_root / OUTLOOK_KEY_FILE
    key_path.chmod(0o644)
    with pytest.raises(OutlookSecurityError, match="权限或格式无效"):
        OutlookSecretBox(data_root).decrypt_text(
            "oauth-token", "account-1", ciphertext
        )


def test_outlook_secret_box_rejects_symlink_key(tmp_path: Path) -> None:
    data_root = tmp_path / "private"
    data_root.mkdir()
    target = tmp_path / "elsewhere"
    target.write_bytes(os.urandom(32))
    (data_root / OUTLOOK_KEY_FILE).symlink_to(target)

    with pytest.raises(OutlookSecurityError, match="权限或格式无效"):
        OutlookSecretBox(data_root).encrypt_text("oauth-token", "account-1", "x")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("88F7EC22-5D55-45E8-9709-E4D7786F1A04", "88f7ec22-5d55-45e8-9709-e4d7786f1a04"),
        ("common", "common"),
        ("ORGANIZATIONS", "organizations"),
        ("consumers", "consumers"),
    ],
)
def test_outlook_identity_values_are_normalized(value: str, expected: str) -> None:
    normalizer = normalize_outlook_tenant if expected in {
        "common",
        "organizations",
        "consumers",
    } else normalize_outlook_client_id
    assert normalizer(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "https://login.microsoftonline.com/common",
        "00000000-0000-0000-0000-000000000000",
        "../../tenant",
    ],
)
def test_outlook_identity_values_reject_urls_and_empty_uuid(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_outlook_tenant(value)
    with pytest.raises(ValueError):
        normalize_outlook_client_id(value)


def test_outlook_urls_are_fixed_to_microsoft_graph_boundaries() -> None:
    assert (
        validate_microsoft_verification_uri("https://microsoft.com/devicelogin")
        == "https://microsoft.com/devicelogin"
    )
    delta = (
        "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta"
        "?$skiptoken=opaque"
    )
    assert validate_graph_delta_url(delta) == delta

    for value in (
        "http://microsoft.com/devicelogin",
        "https://microsoft.com/other",
        "https://microsoft.com/devicelogin?code=attacker-controlled",
        "https://www.microsoft.com/devicelogin",
        "https://login.microsoftonline.com/common/oauth2/deviceauth",
        "https://microsoft.com.evil.example/devicelogin",
        "https://microsoft.com@evil.example/devicelogin",
        "https://graph.microsoft.com/v1.0/me/messages/delta?$skiptoken=x",
        "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta",
        "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta#$skiptoken=x",
    ):
        validator = (
            validate_microsoft_verification_uri
            if "microsoft.com" in value and "graph.microsoft.com" not in value
            else validate_graph_delta_url
        )
        with pytest.raises(OutlookSecurityError):
            validator(value)


def test_outlook_text_is_plain_normalized_and_bounded() -> None:
    assert sanitize_outlook_text("  A\r\nB\x00\u202eC  ", max_chars=10) == "A\nBC"
    assert sanitize_outlook_text({"html": "<b>x</b>"}, max_chars=10) == ""
    assert sanitize_outlook_text("123456", max_chars=4) == "1234"


def test_outlook_transport_rejects_unapproved_requests_before_network() -> None:
    transport = DirectHTTPSOutlookTransport()
    for request in (
        OutlookHttpRequest("GET", "http://graph.microsoft.com/v1.0/me", {}),
        OutlookHttpRequest("GET", "https://graph.microsoft.com.evil.example/v1.0/me", {}),
        OutlookHttpRequest("GET", "https://login.microsoftonline.com@evil.example/x", {}),
        OutlookHttpRequest("PUT", "https://graph.microsoft.com/v1.0/me", {}),
        OutlookHttpRequest("GET", "https://graph.microsoft.com/v1.0/me#secret", {}),
        OutlookHttpRequest("GET", "https://graph.microsoft.com/v1.0/me", {}, max_bytes=-1),
        OutlookHttpRequest(
            "GET",
            "https://graph.microsoft.com/v1.0/me",
            {"Accept-Encoding": "gzip"},
        ),
        OutlookHttpRequest(
            "POST",
            "https://graph.microsoft.com/v1.0/me/messages",
            {},
            body=b"x" * (512 * 1024 + 1),
        ),
    ):
        with pytest.raises(OutlookTransportError, match="request_not_allowed"):
            transport.request(request)


def test_outlook_transport_rejects_unsafe_response_headers() -> None:
    validate = DirectHTTPSOutlookTransport._validated_headers
    assert validate([("Content-Type", "application/json")]) == {
        "content-type": "application/json"
    }
    with pytest.raises(OutlookTransportError, match="invalid_response_header"):
        validate([("X-Test", "safe\nInjected: yes")])
    with pytest.raises(OutlookTransportError, match="too_many_headers"):
        validate([(f"X-{index}", "value") for index in range(65)])


def test_outlook_transport_allows_only_fixed_safe_request_headers() -> None:
    validate = DirectHTTPSOutlookTransport._validated_request_headers
    assert validate(
        {
            "Authorization": "Bearer opaque-token",
            "Content-Type": "application/json",
            "Prefer": 'IdType="ImmutableId"',
        }
    ) == {
        "authorization": "Bearer opaque-token",
        "content-type": "application/json",
        "prefer": 'IdType="ImmutableId"',
    }
    for headers in (
        {"Host": "evil.example"},
        {"Connection": "keep-alive"},
        {"Authorization": "Bearer safe\r\nX-Evil: yes"},
    ):
        with pytest.raises(OutlookTransportError, match="request_not_allowed"):
            validate(headers)


def test_outlook_v6_migration_preserves_existing_sources_and_items(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "pocket.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(SCHEMA_V1)
        connection.executescript(MIGRATION_V2)
        connection.executescript(MIGRATION_V3)
        connection.executescript(MIGRATION_V4)
        connection.executescript(MIGRATION_V5)
        connection.execute(
            """
            INSERT INTO sources(
                id, kind, provider, name, config_json, schedule, enabled,
                created_at, updated_at, last_sync_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "source-existing",
                "rss",
                "rss",
                "Existing source",
                "{}",
                "manual",
                1,
                "2026-08-01T00:00:00Z",
                "2026-08-01T00:00:00Z",
                None,
            ),
        )
        connection.execute(
            """
            INSERT INTO items(
                id, content_hash, first_source_id, origin_uri, file_name,
                mime_type, title, text_content, size_bytes, state,
                tags_json, metadata_json, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "item-existing",
                "a" * 64,
                "source-existing",
                "https://example.com/item",
                "item.txt",
                "text/plain",
                "Existing item",
                "Existing content",
                16,
                "ready",
                "[]",
                "{}",
                1,
                "2026-08-01T00:00:00Z",
                "2026-08-01T00:00:00Z",
            ),
        )

    Database(database_path).initialize()

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        version = connection.execute(
            "SELECT MAX(version) AS version FROM schema_meta"
        ).fetchone()["version"]
        source = connection.execute(
            "SELECT kind, provider, name FROM sources WHERE id = ?",
            ("source-existing",),
        ).fetchone()
        item = connection.execute(
            "SELECT first_source_id, title FROM items WHERE id = ?",
            ("item-existing",),
        ).fetchone()
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        outlook_tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'outlook_%'"
            )
        }
        outlook_indexes = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_outlook_%'"
            )
        }

    assert version == LATEST_SCHEMA_VERSION == 6
    assert dict(source) == {
        "kind": "rss",
        "provider": "rss",
        "name": "Existing source",
    }
    assert dict(item) == {
        "first_source_id": "source-existing",
        "title": "Existing item",
    }
    assert not violations
    assert {
        "outlook_accounts",
        "outlook_credentials",
        "outlook_messages",
        "outlook_local_drafts",
        "outlook_send_intents",
        "outlook_domain_idempotency",
    }.issubset(outlook_tables)
    assert "idx_outlook_one_active_reply_draft" in outlook_indexes
    assert "idx_outlook_one_running_sync" in outlook_indexes


def test_outlook_configuration_is_explicit_and_has_no_client_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CENTAURAI_POCKET_OUTLOOK_CLIENT_ID",
        " 88f7ec22-5d55-45e8-9709-e4d7786f1a04 ",
    )
    monkeypatch.setenv("CENTAURAI_POCKET_OUTLOOK_TENANT", " organizations ")
    monkeypatch.setenv("CENTAURAI_POCKET_OUTLOOK_CLIENT_SECRET", "must-be-ignored")

    configured = Settings.from_env()

    assert configured.outlook_client_id == "88f7ec22-5d55-45e8-9709-e4d7786f1a04"
    assert configured.outlook_tenant == "organizations"
    assert not hasattr(configured, "outlook_client_secret")
