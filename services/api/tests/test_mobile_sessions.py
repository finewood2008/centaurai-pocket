from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from centaur_pocket.config import Settings
from centaur_pocket.database import (
    LATEST_SCHEMA_VERSION,
    MIGRATION_V3,
    SCHEMA_V1,
    Database,
)
from centaur_pocket.main import create_app

MOBILE_PAIRINGS_PATH = "/api/v1/mobile/pairings"
MOBILE_CROCKFORD_ALPHABET = frozenset("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
SENSITIVE_RESPONSE_HEADERS = {
    "cache-control": "no-store, max-age=0",
    "pragma": "no-cache",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
}


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _create_pairing(
    client: TestClient, owner_headers: dict[str, str]
) -> dict[str, str]:
    response = client.post(MOBILE_PAIRINGS_PATH, headers=owner_headers)
    assert response.status_code == 201, response.text
    return response.json()


def _claim_pairing(
    client: TestClient,
    code: str,
    *,
    device_id: str = "pytest-phone-001",
    display_name: str = "测试手机",
) -> dict:
    response = client.post(
        f"{MOBILE_PAIRINGS_PATH}/claim",
        json={
            "code": code,
            "device_id": device_id,
            "display_name": display_name,
            "platform": "android",
            "app_version": "1.2.3",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _device_headers(session: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['access_token']}"}


def _assert_exact_token_response(payload: dict) -> None:
    assert set(payload) == {
        "token_type",
        "access_token",
        "access_expires_at",
        "refresh_token",
        "refresh_expires_at",
        "device",
    }
    assert payload["token_type"] == "Bearer"
    assert payload["access_token"].startswith("cp_device_")
    assert payload["refresh_token"].startswith("cp_refresh_")
    assert set(payload["device"]) == {
        "id",
        "device_id",
        "display_name",
        "platform",
        "app_version",
        "status",
        "last_seen_at",
        "created_at",
    }


def _assert_sensitive_response_headers(response) -> None:
    assert {
        name: response.headers.get(name) for name in SENSITIVE_RESPONSE_HEADERS
    } == SENSITIVE_RESPONSE_HEADERS


def test_every_mobile_response_is_non_cacheable(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    created = client.post(MOBILE_PAIRINGS_PATH, headers=owner_headers)
    assert created.status_code == 201, created.text
    _assert_sensitive_response_headers(created)

    claimed = client.post(
        f"{MOBILE_PAIRINGS_PATH}/claim",
        json={
            "code": created.json()["code"],
            "device_id": "pytest-mobile-header-device",
            "display_name": "测试手机",
            "platform": "android",
            "app_version": "1.2.3",
        },
    )
    assert claimed.status_code == 200, claimed.text
    _assert_sensitive_response_headers(claimed)

    refreshed = client.post(
        "/api/v1/mobile/sessions/refresh",
        json={
            "refresh_token": claimed.json()["refresh_token"],
            "device_id": "pytest-mobile-header-device",
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    _assert_sensitive_response_headers(refreshed)

    missing_auth = client.post(MOBILE_PAIRINGS_PATH)
    assert missing_auth.status_code == 401
    _assert_sensitive_response_headers(missing_auth)

    malformed_claim = client.post(
        f"{MOBILE_PAIRINGS_PATH}/claim",
        json={"code": "secret-that-must-not-be-reflected"},
    )
    assert malformed_claim.status_code == 422
    assert "secret-that-must-not-be-reflected" not in malformed_claim.text
    _assert_sensitive_response_headers(malformed_claim)

    rejected_claim = client.post(
        f"{MOBILE_PAIRINGS_PATH}/claim",
        json={
            "code": "0000-0000-0000",
            "device_id": "pytest-mobile-header-device",
            "display_name": "测试手机",
            "platform": "android",
            "app_version": "1.2.3",
        },
    )
    assert rejected_claim.status_code == 401
    _assert_sensitive_response_headers(rejected_claim)

    missing_route = client.get("/api/v1/mobile/not-a-route")
    assert missing_route.status_code == 404
    _assert_sensitive_response_headers(missing_route)


def test_owner_creates_one_time_hashed_crockford_pairing(
    client: TestClient,
    owner_headers: dict[str, str],
    agent_headers: dict[str, str],
) -> None:
    assert client.post(MOBILE_PAIRINGS_PATH).status_code == 401
    assert client.post(MOBILE_PAIRINGS_PATH, headers=agent_headers).status_code == 401

    before = datetime.now(UTC)
    pairing = _create_pairing(client, owner_headers)
    assert set(pairing) == {"pairing_id", "code", "expires_at"}
    groups = pairing["code"].split("-")
    assert [len(group) for group in groups] == [4, 4, 4]
    assert set("".join(groups)) <= MOBILE_CROCKFORD_ALPHABET
    assert timedelta(minutes=9, seconds=55) <= _parse_utc(
        pairing["expires_at"]
    ) - before <= timedelta(minutes=10, seconds=5)

    service = client.app.state.service
    with service.database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM mobile_pairings WHERE id = ?",
            (pairing["pairing_id"],),
        ).fetchone()
    assert row is not None
    assert row["code_hash"] == _hash_secret(pairing["code"])
    assert pairing["code"] not in tuple(str(value) for value in row)

    rejected_extra = client.post(
        MOBILE_PAIRINGS_PATH,
        headers=owner_headers,
        json={"unexpected": True},
    )
    assert rejected_extra.status_code == 422


def test_desktop_session_may_create_pairing(tmp_path: Path) -> None:
    desktop_token = "cp_desktop_pytest-mobile-session"
    settings = Settings(
        data_root=tmp_path / "desktop-mobile-runtime",
        owner_token="cp_owner_pytest-mobile-owner",
        agent_token="cp_live_pytest-mobile-agent",
        desktop_session_token=desktop_token,
        scheduler_poll_seconds=0,
    )
    with TestClient(create_app(settings)) as desktop_client:
        response = desktop_client.post(
            MOBILE_PAIRINGS_PATH,
            headers={"Authorization": f"Bearer {desktop_token}"},
        )
    assert response.status_code == 201, response.text


def test_claim_is_case_insensitive_single_use_and_stores_only_hashes(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    pairing = _create_pairing(client, owner_headers)
    strict_rejection = client.post(
        f"{MOBILE_PAIRINGS_PATH}/claim",
        json={
            "code": pairing["code"],
            "device_id": "pytest-phone-001",
            "display_name": "测试手机",
            "platform": "android",
            "app_version": "1.2.3",
            "unexpected": "forbidden",
        },
    )
    assert strict_rejection.status_code == 422
    assert pairing["code"] not in strict_rejection.text

    before = datetime.now(UTC)
    session = _claim_pairing(
        client,
        pairing["code"].lower().replace("-", ""),
    )
    _assert_exact_token_response(session)
    assert session["device"]["device_id"] == "pytest-phone-001"
    assert session["device"]["display_name"] == "测试手机"
    assert session["device"]["platform"] == "android"
    assert session["device"]["app_version"] == "1.2.3"
    assert session["device"]["status"] == "active"
    assert timedelta(minutes=14, seconds=55) <= _parse_utc(
        session["access_expires_at"]
    ) - before <= timedelta(minutes=15, seconds=5)
    assert timedelta(days=29, hours=23, minutes=59) <= _parse_utc(
        session["refresh_expires_at"]
    ) - before <= timedelta(days=30, seconds=5)

    service = client.app.state.service
    with service.database.connect() as connection:
        stored_session = connection.execute(
            "SELECT * FROM mobile_sessions WHERE mobile_device_id = ?",
            (session["device"]["id"],),
        ).fetchone()
        stored_pairing = connection.execute(
            "SELECT * FROM mobile_pairings WHERE id = ?",
            (pairing["pairing_id"],),
        ).fetchone()
    assert stored_session is not None
    assert stored_pairing is not None
    assert stored_session["access_token_hash"] == _hash_secret(
        session["access_token"]
    )
    assert stored_session["refresh_token_hash"] == _hash_secret(
        session["refresh_token"]
    )
    assert session["access_token"] not in tuple(
        str(value) for value in stored_session
    )
    assert session["refresh_token"] not in tuple(
        str(value) for value in stored_session
    )
    assert stored_pairing["claimed_at"] is not None

    session_view = client.get(
        "/api/v1/mobile/session", headers=_device_headers(session)
    )
    assert session_view.status_code == 200, session_view.text
    assert set(session_view.json()) == {
        "token_type",
        "access_expires_at",
        "device",
    }
    assert session_view.json()["device"]["id"] == session["device"]["id"]

    reused = client.post(
        f"{MOBILE_PAIRINGS_PATH}/claim",
        json={
            "code": pairing["code"],
            "device_id": "another-phone",
            "display_name": "另一台手机",
            "platform": "ios",
            "app_version": "2.0",
        },
    )
    assert reused.status_code == 401
    assert pairing["code"] not in reused.text


def test_refresh_atomically_rotates_both_tokens_and_binds_device(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    pairing = _create_pairing(client, owner_headers)
    first = _claim_pairing(
        client,
        pairing["code"],
        device_id="测试手机-一号",
    )

    wrong_device = client.post(
        "/api/v1/mobile/sessions/refresh",
        json={
            "refresh_token": first["refresh_token"],
            "device_id": "not-this-phone",
        },
    )
    assert wrong_device.status_code == 401
    assert first["refresh_token"] not in wrong_device.text

    refreshed = client.post(
        "/api/v1/mobile/sessions/refresh",
        json={
            "refresh_token": first["refresh_token"],
            "device_id": first["device"]["device_id"],
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    second = refreshed.json()
    _assert_exact_token_response(second)
    assert second["access_token"] != first["access_token"]
    assert second["refresh_token"] != first["refresh_token"]
    assert second["device"]["id"] == first["device"]["id"]

    assert (
        client.get("/api/v1/mobile/session", headers=_device_headers(first)).status_code
        == 401
    )
    replayed = client.post(
        "/api/v1/mobile/sessions/refresh",
        json={
            "refresh_token": first["refresh_token"],
            "device_id": first["device"]["device_id"],
        },
    )
    assert replayed.status_code == 401
    assert first["refresh_token"] not in replayed.text
    assert (
        client.get("/api/v1/mobile/session", headers=_device_headers(second)).status_code
        == 200
    )

    strict_rejection = client.post(
        "/api/v1/mobile/sessions/refresh",
        json={
            "refresh_token": second["refresh_token"],
            "device_id": second["device"]["device_id"],
            "unexpected": True,
        },
    )
    assert strict_rejection.status_code == 422
    assert second["refresh_token"] not in strict_rejection.text

    malformed_secret = "cp_refresh_must-never-be-reflected"
    malformed = client.post(
        "/api/v1/mobile/sessions/refresh",
        json=malformed_secret,
    )
    assert malformed.status_code == 422
    assert malformed_secret not in malformed.text

    wrong_type_secret = "cp_refresh_nested-must-never-be-reflected"
    wrong_type = client.post(
        "/api/v1/mobile/sessions/refresh",
        json={
            "refresh_token": {"secret": wrong_type_secret},
            "device_id": second["device"]["device_id"],
        },
    )
    assert wrong_type.status_code == 422
    assert wrong_type_secret not in wrong_type.text

    service = client.app.state.service
    with service.database.connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM mobile_sessions
            WHERE mobile_device_id = ?
            ORDER BY created_at, id
            """,
            (first["device"]["id"],),
        ).fetchall()
    assert len(rows) == 2
    assert sum(row["revoked_at"] is None for row in rows) == 1
    assert {
        row["access_token_hash"] for row in rows
    } == {_hash_secret(first["access_token"]), _hash_secret(second["access_token"])}
    assert {
        row["refresh_token_hash"] for row in rows
    } == {_hash_secret(first["refresh_token"]), _hash_secret(second["refresh_token"])}


def test_device_access_is_limited_to_secretary_routes_and_default_workspace(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    session = _claim_pairing(
        client,
        _create_pairing(client, owner_headers)["code"],
        device_id="secretary-phone-01",
    )
    headers = _device_headers(session)

    assert client.get("/api/v1/dashboard", headers=headers).status_code == 200
    assert client.get("/api/v1/sources", headers=headers).status_code == 401
    assert client.get("/api/v1/items", headers=headers).status_code == 401
    assert client.get("/api/v1/mobile/devices", headers=headers).status_code == 401
    assert client.post(MOBILE_PAIRINGS_PATH, headers=headers).status_code == 401
    assert (
        client.post(
            "/api/v1/agent/search",
            headers=headers,
            json={"query": "private"},
        ).status_code
        == 401
    )

    default_workspace = client.get(
        "/api/v1/workspaces/ws_default/bootstrap",
        headers=headers,
    )
    assert default_workspace.status_code == 200, default_workspace.text

    memo_payload = {
        "domain": "work",
        "title": "已绑定设备写入的备忘",
        "content": "此记录用于验证服务端设备身份绑定。",
    }
    forged_device = client.post(
        "/api/v1/workspaces/ws_default/memos",
        headers={
            **headers,
            "Idempotency-Key": "mobile-forged-device-001",
            "X-Device-ID": "forged-device-id",
        },
        json=memo_payload,
    )
    assert forged_device.status_code == 403
    authentic_device = client.post(
        "/api/v1/workspaces/ws_default/memos",
        headers={
            **headers,
            "Idempotency-Key": "mobile-authentic-device-001",
            "X-Device-ID": session["device"]["device_id"],
        },
        json=memo_payload,
    )
    assert authentic_device.status_code == 201, authentic_device.text

    created_member = client.post(
        "/api/v1/workspaces/ws_default/members",
        headers={
            **headers,
            "Idempotency-Key": "mobile-authentic-member-001",
            "X-Device-ID": session["device"]["device_id"],
        },
        json={
            "kind": "person",
            "role": "viewer",
            "display_name": "手机端创建的观察者",
            "contact_ref": None,
        },
    )
    assert created_member.status_code == 201, created_member.text
    assert created_member.json()["workspace_id"] == "ws_default"
    assert created_member.json()["role"] == "viewer"
    assert created_member.json()["version"] == 1

    assert (
        client.get(
            "/api/v1/workspaces/ws_other/bootstrap",
            headers=headers,
        ).status_code
        == 403
    )

    assert (
        client.get(
            "/api/v1/governance/tasks?status=pending", headers=headers
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/v1/governance/tasks?status=applied", headers=headers
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/knowledge/candidates?status=provisional", headers=headers
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/v1/knowledge/candidates?status=confirmed", headers=headers
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/governance/tasks/nonexistent", headers=headers
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/knowledge/candidates/nonexistent/confirm", headers=headers
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/v1/knowledge/candidates/nonexistent/dismiss", headers=headers
        ).status_code
        == 404
    )

    first_capture = client.post(
        "/api/v1/captures",
        headers=headers,
        json={
            "title": "手机秘书捕获一",
            "text": "由已配对设备提交并等待治理。",
            "idempotency_key": "mobile-session-capture-1",
        },
    )
    assert first_capture.status_code == 201, first_capture.text
    applied = client.post(
        f"/api/v1/governance/tasks/{first_capture.json()['task_id']}/apply",
        headers=headers,
    )
    assert applied.status_code == 200, applied.text

    second_capture = client.post(
        "/api/v1/captures",
        headers=headers,
        json={
            "title": "手机秘书捕获二",
            "text": "第二条独立内容用于验证跳过流程。",
            "idempotency_key": "mobile-session-capture-2",
        },
    )
    assert second_capture.status_code == 201, second_capture.text
    skipped = client.post(
        f"/api/v1/governance/tasks/{second_capture.json()['task_id']}/skip",
        headers=headers,
    )
    assert skipped.status_code == 200, skipped.text


def test_owner_lists_and_revokes_device_and_all_sessions(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    session = _claim_pairing(
        client,
        _create_pairing(client, owner_headers)["code"],
    )
    device_id = session["device"]["id"]

    listed = client.get("/api/v1/mobile/devices", headers=owner_headers)
    assert listed.status_code == 200, listed.text
    assert set(listed.json()) == {"items", "total"}
    assert listed.json()["total"] == 1
    assert listed.json()["items"] == [session["device"]]
    assert session["access_token"] not in listed.text
    assert session["refresh_token"] not in listed.text

    assert (
        client.delete(
            f"/api/v1/mobile/devices/{device_id}",
            headers=_device_headers(session),
        ).status_code
        == 401
    )
    revoked = client.delete(
        f"/api/v1/mobile/devices/{device_id}",
        headers=owner_headers,
    )
    assert revoked.status_code == 204, revoked.text
    assert revoked.content == b""

    assert (
        client.get(
            "/api/v1/mobile/session", headers=_device_headers(session)
        ).status_code
        == 401
    )
    refresh = client.post(
        "/api/v1/mobile/sessions/refresh",
        json={
            "refresh_token": session["refresh_token"],
            "device_id": session["device"]["device_id"],
        },
    )
    assert refresh.status_code == 401
    assert session["refresh_token"] not in refresh.text

    listed_after = client.get("/api/v1/mobile/devices", headers=owner_headers).json()
    assert listed_after["items"][0]["status"] == "revoked"
    assert (
        client.delete(
            "/api/v1/mobile/devices/mdev_missing", headers=owner_headers
        ).status_code
        == 404
    )


def test_pairing_access_and_refresh_expiry_are_enforced(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    expired_pairing = _create_pairing(client, owner_headers)
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    service = client.app.state.service
    with service.database.transaction() as connection:
        connection.execute(
            "UPDATE mobile_pairings SET expires_at = ? WHERE id = ?",
            (past, expired_pairing["pairing_id"]),
        )
    rejected_claim = client.post(
        f"{MOBILE_PAIRINGS_PATH}/claim",
        json={
            "code": expired_pairing["code"],
            "device_id": "expired-phone",
            "display_name": "过期手机",
            "platform": "android",
            "app_version": "1.0",
        },
    )
    assert rejected_claim.status_code == 401
    assert expired_pairing["code"] not in rejected_claim.text

    session = _claim_pairing(
        client,
        _create_pairing(client, owner_headers)["code"],
        device_id="expiry-test-phone",
    )
    with service.database.transaction() as connection:
        connection.execute(
            """
            UPDATE mobile_sessions SET access_expires_at = ?
            WHERE access_token_hash = ?
            """,
            (past, _hash_secret(session["access_token"])),
        )
    access_rejected = client.get(
        "/api/v1/mobile/session", headers=_device_headers(session)
    )
    assert access_rejected.status_code == 401
    assert session["access_token"] not in access_rejected.text

    with service.database.transaction() as connection:
        connection.execute(
            """
            UPDATE mobile_sessions SET refresh_expires_at = ?
            WHERE refresh_token_hash = ?
            """,
            (past, _hash_secret(session["refresh_token"])),
        )
    refresh_rejected = client.post(
        "/api/v1/mobile/sessions/refresh",
        json={
            "refresh_token": session["refresh_token"],
            "device_id": session["device"]["device_id"],
        },
    )
    assert refresh_rejected.status_code == 401
    assert session["refresh_token"] not in refresh_rejected.text


def test_v3_database_migrates_mobile_schema_without_losing_data(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pocket-v3.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_V1)
    created_at = datetime.now(UTC).isoformat()
    connection.execute(
        """
        INSERT INTO sources(
            id, kind, name, config_json, schedule, enabled, created_at, updated_at
        ) VALUES ('src_before_mobile', 'folder', '迁移前目录', '{}', 'manual', 1, ?, ?)
        """,
        (created_at, created_at),
    )
    connection.commit()
    Database(path)._migrate_to_v2(connection)
    connection.executescript(MIGRATION_V3)
    connection.close()

    database = Database(path)
    database.initialize()
    with database.connect() as migrated:
        version = migrated.execute("SELECT version FROM schema_meta").fetchone()[0]
        source = migrated.execute(
            "SELECT name FROM sources WHERE id = 'src_before_mobile'"
        ).fetchone()
        tables = {
            row[0]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        mobile_session_fks = migrated.execute(
            "PRAGMA foreign_key_list(mobile_sessions)"
        ).fetchall()
        violations = migrated.execute("PRAGMA foreign_key_check").fetchall()

    assert version == LATEST_SCHEMA_VERSION
    assert source is not None and source["name"] == "迁移前目录"
    assert {"mobile_pairings", "mobile_devices", "mobile_sessions"} <= tables
    assert any(
        row["table"] == "mobile_devices" and row["on_delete"] == "CASCADE"
        for row in mobile_session_fks
    )
    assert violations == []
