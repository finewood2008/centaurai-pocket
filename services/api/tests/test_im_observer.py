from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from centaur_pocket.database import LATEST_SCHEMA_VERSION, SCHEMA_V1, Database


def _create_observer(
    client: TestClient, owner_headers: dict[str, str], *, name: str = "微信网页"
) -> dict:
    response = client.post(
        "/api/v1/sources",
        headers=owner_headers,
        json={
            "kind": "wechat_visible_web",
            "display_name": name,
            "config": {"capture_mode": "visible_dom"},
            "schedule": "continuous",
            "enabled": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _pair_observer(
    client: TestClient, owner_headers: dict[str, str], source_id: str
) -> tuple[dict, dict[str, str]]:
    pairing_response = client.post(
        f"/api/v1/sources/{source_id}/pairings",
        headers=owner_headers,
    )
    assert pairing_response.status_code == 201, pairing_response.text
    pairing = pairing_response.json()
    assert pairing["pairing_code"].startswith("cp_pair_")
    handshake_response = client.post(
        f"/api/v1/collectors/v1/sources/{source_id}/handshake",
        headers={"Authorization": f"Bearer {pairing['pairing_code']}"},
        json={
            "extension_id": "centaurai-pocket-observer@example.com",
            "extension_version": "0.1.0",
            "browser_name": "firefox",
            "browser_version": "141.0",
            "parser_version": "wechat-web-v1",
        },
    )
    assert handshake_response.status_code == 201, handshake_response.text
    handshake = handshake_response.json()
    assert handshake["collector_token"].startswith("cp_collector_")
    return pairing, {
        "Authorization": f"Bearer {handshake['collector_token']}"
    }


def _heartbeat_payload(
    *, state: str = "active", unread_conversation_count: int = 0
) -> dict:
    return {
        "browser_session_id": "browser-session-1",
        "state": state,
        "observed_at": datetime.now(UTC).isoformat(),
        "browser_version": "141.0",
        "extension_version": "0.1.0",
        "parser_version": "wechat-web-v1",
        "current_conversation_id": "conversation-alice",
        "current_conversation_name": "Alice",
        "unread_conversation_count": unread_conversation_count,
    }


def _message(
    provider_msgid: str,
    *,
    text: str = "收到",
    conversation_id: str = "conversation-alice",
) -> dict:
    return {
        "provider_msgid": provider_msgid,
        "provider_conversation_id": conversation_id,
        "conversation_name": "Alice",
        "conversation_type": "direct",
        "direction": "incoming",
        "message_type": "text",
        "sender_provider_id": "alice",
        "sender_display_name": "Alice",
        "text": text,
        "displayed_time_text": "10:30",
        "observed_at": datetime.now(UTC).isoformat(),
    }


def test_observer_pairing_heartbeat_ingest_and_owner_reads(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    source = _create_observer(client, owner_headers)
    assert source["kind"] == "wechat_visible_web"
    assert source["provider"] == "wechat_visible_web"
    assert source["schedule"] == "continuous"
    assert client.post(
        f"/api/v1/sources/{source['id']}/sync", headers=owner_headers
    ).status_code == 409

    pairing, collector_headers = _pair_observer(
        client, owner_headers, source["id"]
    )
    reused = client.post(
        f"/api/v1/collectors/v1/sources/{source['id']}/handshake",
        headers={"Authorization": f"Bearer {pairing['pairing_code']}"},
        json={
            "extension_id": "centaurai-pocket-observer@example.com",
            "extension_version": "0.1.0",
            "browser_name": "firefox",
            "parser_version": "wechat-web-v1",
        },
    )
    assert reused.status_code == 401

    heartbeat = client.post(
        f"/api/v1/collectors/v1/sources/{source['id']}/heartbeat",
        headers=collector_headers,
        json=_heartbeat_payload(unread_conversation_count=2),
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["state"] == "active"

    events = [_message("msg-1"), _message("msg-2")]
    batch = {
        "batch_id": "batch-1",
        "browser_session_id": "browser-session-1",
        "events": events,
    }
    ingested = client.post(
        f"/api/v1/collectors/v1/sources/{source['id']}/events",
        headers=collector_headers,
        json=batch,
    )
    assert ingested.status_code == 202, ingested.text
    assert ingested.json()["accepted_count"] == 2
    assert ingested.json()["duplicate_count"] == 0

    # Exact batch replay is idempotent. A new batch containing the same
    # provider IDs is accepted as a request but records both as duplicates.
    replay = client.post(
        f"/api/v1/collectors/v1/sources/{source['id']}/events",
        headers=collector_headers,
        json=batch,
    )
    assert replay.json() == ingested.json()
    duplicate_batch = client.post(
        f"/api/v1/collectors/v1/sources/{source['id']}/events",
        headers=collector_headers,
        json={**batch, "batch_id": "batch-2"},
    )
    assert duplicate_batch.status_code == 202
    assert duplicate_batch.json()["accepted_count"] == 0
    assert duplicate_batch.json()["duplicate_count"] == 2

    changed_replay = client.post(
        f"/api/v1/collectors/v1/sources/{source['id']}/events",
        headers=collector_headers,
        json={**batch, "events": [_message("msg-3")]},
    )
    assert changed_replay.status_code == 409

    conversations = client.get(
        f"/api/v1/im/conversations?source_id={source['id']}",
        headers=owner_headers,
    )
    assert conversations.status_code == 200
    conversation = conversations.json()["items"][0]
    assert conversation["message_count"] == 2
    assert conversation["policy"] == {
        "agent_enabled": False,
        "retention_days": 365,
    }

    messages = client.get(
        f"/api/v1/im/conversations/{conversation['id']}/messages",
        headers=owner_headers,
    ).json()
    assert messages["total"] == 2
    assert {item["provider_msgid"] for item in messages["items"]} == {
        "msg-1",
        "msg-2",
    }
    assert {item["text"] for item in messages["items"]} == {"收到"}

    policy = client.patch(
        f"/api/v1/im/conversations/{conversation['id']}/policy",
        headers=owner_headers,
        json={"agent_enabled": True, "retention_days": 90},
    )
    assert policy.status_code == 200
    assert policy.json()["policy"] == {
        "agent_enabled": True,
        "retention_days": 90,
    }

    client.post(
        f"/api/v1/collectors/v1/sources/{source['id']}/heartbeat",
        headers=collector_headers,
        json=_heartbeat_payload(unread_conversation_count=0),
    )
    gaps = client.get(
        f"/api/v1/sources/{source['id']}/coverage-gaps",
        headers=owner_headers,
    ).json()
    unread_gap = next(
        item for item in gaps["items"] if item["kind"] == "unopened_conversations"
    )
    assert unread_gap["ended_at"] is not None

    status = client.get(
        f"/api/v1/sources/{source['id']}/observer-status",
        headers=owner_headers,
    )
    assert status.status_code == 200
    status_json = json.dumps(status.json())
    assert "cp_pair_" not in status_json
    assert "cp_collector_" not in status_json
    assert "token_hash" not in status_json
    assert status.json()["message_count"] == 2

    paused = client.post(
        f"/api/v1/sources/{source['id']}/pause", headers=owner_headers
    )
    assert paused.status_code == 200
    assert paused.json()["enabled"] is False
    assert client.post(
        f"/api/v1/collectors/v1/sources/{source['id']}/heartbeat",
        headers=collector_headers,
        json=_heartbeat_payload(),
    ).status_code == 409
    resumed = client.post(
        f"/api/v1/sources/{source['id']}/resume", headers=owner_headers
    )
    assert resumed.status_code == 200
    assert resumed.json()["enabled"] is True


def test_observer_credentials_are_scoped_hashed_and_payload_is_strict(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    first = _create_observer(client, owner_headers, name="微信一")
    second = _create_observer(client, owner_headers, name="微信二")
    pairing, collector_headers = _pair_observer(client, owner_headers, first["id"])
    raw_token = collector_headers["Authorization"].split(" ", 1)[1]

    service = client.app.state.service
    with service.database.connect() as connection:
        stored_pairing = connection.execute(
            "SELECT code_hash FROM collector_pairings WHERE source_id = ?",
            (first["id"],),
        ).fetchone()[0]
        stored_token = connection.execute(
            "SELECT token_hash FROM collector_tokens WHERE source_id = ?",
            (first["id"],),
        ).fetchone()[0]
    assert stored_pairing != pairing["pairing_code"]
    assert stored_token != raw_token
    assert len(stored_pairing) == len(stored_token) == 64

    wrong_source = client.post(
        f"/api/v1/collectors/v1/sources/{second['id']}/heartbeat",
        headers=collector_headers,
        json=_heartbeat_payload(),
    )
    assert wrong_source.status_code == 401
    owner_is_not_collector = client.post(
        f"/api/v1/collectors/v1/sources/{first['id']}/heartbeat",
        headers=owner_headers,
        json=_heartbeat_payload(),
    )
    assert owner_is_not_collector.status_code == 401
    extra_field = client.post(
        f"/api/v1/collectors/v1/sources/{first['id']}/heartbeat",
        headers=collector_headers,
        json={**_heartbeat_payload(), "html": "<main>must not be accepted</main>"},
    )
    assert extra_field.status_code == 422


def test_collector_has_a_persistent_per_token_rate_limit(
    client: TestClient,
    owner_headers: dict[str, str],
    monkeypatch,
) -> None:
    monkeypatch.setattr("centaur_pocket.service.COLLECTOR_REQUESTS_PER_MINUTE", 1)
    source = _create_observer(client, owner_headers)
    _, collector_headers = _pair_observer(client, owner_headers, source["id"])
    first = client.post(
        f"/api/v1/collectors/v1/sources/{source['id']}/heartbeat",
        headers=collector_headers,
        json=_heartbeat_payload(),
    )
    assert first.status_code == 200
    limited = client.post(
        f"/api/v1/collectors/v1/sources/{source['id']}/heartbeat",
        headers=collector_headers,
        json=_heartbeat_payload(),
    )
    assert limited.status_code == 429


def test_v1_database_migrates_without_losing_folder_relations(tmp_path: Path) -> None:
    path = tmp_path / "pocket.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_V1)
    connection.execute(
        """
        INSERT INTO sources(
            id, kind, name, config_json, schedule, enabled, created_at, updated_at
        ) VALUES ('src_old', 'folder', '旧目录', '{}', 'manual', 1, ?, ?)
        """,
        (datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
    )
    connection.commit()
    connection.close()

    database = Database(path)
    database.initialize()
    with database.connect() as migrated:
        assert (
            migrated.execute("SELECT version FROM schema_meta").fetchone()[0]
            == LATEST_SCHEMA_VERSION
        )
        source = migrated.execute(
            "SELECT kind, provider, name FROM sources WHERE id = 'src_old'"
        ).fetchone()
        assert tuple(source) == ("folder", None, "旧目录")
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = {
            row[0]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"ingest_events", "im_conversations", "im_messages"} <= tables


def test_agent_im_search_requires_opt_in_and_confirmed_claims_have_citations(
    client: TestClient,
    owner_headers: dict[str, str],
    agent_headers: dict[str, str],
) -> None:
    source = _create_observer(client, owner_headers)
    _, collector_headers = _pair_observer(client, owner_headers, source["id"])
    client.post(
        f"/api/v1/collectors/v1/sources/{source['id']}/heartbeat",
        headers=collector_headers,
        json=_heartbeat_payload(),
    )
    ingested = client.post(
        f"/api/v1/collectors/v1/sources/{source['id']}/events",
        headers=collector_headers,
        json={
            "batch_id": "knowledge-batch-1",
            "browser_session_id": "browser-session-1",
            "events": [
                _message("decision-1", text="决定：下周一上线预算系统")
            ],
        },
    )
    assert ingested.status_code == 202, ingested.text

    conversation = client.get(
        f"/api/v1/im/conversations?source_id={source['id']}",
        headers=owner_headers,
    ).json()["items"][0]
    candidates = client.get(
        "/api/v1/knowledge/candidates?status=provisional",
        headers=owner_headers,
    )
    assert candidates.status_code == 200, candidates.text
    candidate = candidates.json()["items"][0]
    assert candidate["claim_type"] == "decision"
    assert candidate["status"] == "provisional"
    assert candidate["evidence"][0]["provider_msgid"] == "decision-1"

    hidden = client.post(
        "/api/v1/agent/search",
        headers=agent_headers,
        json={"query": "预算系统"},
    )
    assert hidden.status_code == 200
    assert hidden.json()["results"] == []

    confirmed = client.post(
        f"/api/v1/knowledge/candidates/{candidate['id']}/confirm",
        headers=owner_headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"
    assert client.post(
        "/api/v1/agent/search",
        headers=agent_headers,
        json={"query": "预算系统", "filters": {"item_kinds": ["knowledge"]}},
    ).json()["results"] == []

    enabled = client.patch(
        f"/api/v1/im/conversations/{conversation['id']}/policy",
        headers=owner_headers,
        json={"agent_enabled": True},
    )
    assert enabled.status_code == 200

    knowledge = client.post(
        "/api/v1/agent/search",
        headers=agent_headers,
        json={
            "query": "预算系统",
            "filters": {
                "item_kinds": ["knowledge"],
                "source_ids": [source["id"]],
                "conversation_ids": [conversation["id"]],
                "participant_ids": ["alice"],
            },
        },
    )
    assert knowledge.status_code == 200, knowledge.text
    result = knowledge.json()["results"][0]
    assert result["kind"] == "knowledge"
    assert result["status"] == "confirmed"
    assert result["authority"] == "observed"
    assert result["citations"][0]["provider_msgid"] == "decision-1"
    assert knowledge.json()["visibility"] == "ready_and_opted_in_im"

    message = client.post(
        "/api/v1/agent/search",
        headers=agent_headers,
        json={
            "query": "预算系统",
            "filters": {"item_kinds": ["im_message"]},
        },
    ).json()["results"][0]
    assert message["kind"] == "im_message"
    assert message["acquisition"] == "rendered_dom"
    assert message["citations"][0]["message_id"] == message["message_id"]

    excluded_participant = client.post(
        "/api/v1/agent/search",
        headers=agent_headers,
        json={
            "query": "预算系统",
            "filters": {
                "item_kinds": ["im_message"],
                "participant_ids": ["not-alice"],
            },
        },
    )
    assert excluded_participant.json()["results"] == []

    naive_time = client.post(
        "/api/v1/agent/search",
        headers=agent_headers,
        json={
            "query": "预算系统",
            "filters": {"sent_from": "2026-07-01T00:00:00"},
        },
    )
    assert naive_time.status_code == 422


def test_retention_deletes_expired_raw_messages_but_keeps_confirmed_evidence(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    source = _create_observer(client, owner_headers)
    _, collector_headers = _pair_observer(client, owner_headers, source["id"])
    client.post(
        f"/api/v1/collectors/v1/sources/{source['id']}/heartbeat",
        headers=collector_headers,
        json=_heartbeat_payload(),
    )
    confirmed_evidence = _message("old-confirmed", text="决定：保留证据原文")
    expired_raw = _message("old-raw", text="普通的过期聊天正文")
    for event in (confirmed_evidence, expired_raw):
        event["observed_at"] = "2020-01-01T00:00:00Z"
        event["sent_at"] = "2020-01-01T00:00:00Z"
    response = client.post(
        f"/api/v1/collectors/v1/sources/{source['id']}/events",
        headers=collector_headers,
        json={
            "batch_id": "retention-batch-1",
            "browser_session_id": "browser-session-1",
            "events": [confirmed_evidence, expired_raw],
        },
    )
    assert response.status_code == 202, response.text

    conversation = client.get(
        f"/api/v1/im/conversations?source_id={source['id']}",
        headers=owner_headers,
    ).json()["items"][0]
    client.patch(
        f"/api/v1/im/conversations/{conversation['id']}/policy",
        headers=owner_headers,
        json={"retention_days": 1},
    )
    candidate = client.get(
        "/api/v1/knowledge/candidates?status=provisional",
        headers=owner_headers,
    ).json()["items"][0]
    client.post(
        f"/api/v1/knowledge/candidates/{candidate['id']}/confirm",
        headers=owner_headers,
    )

    preview = client.get(
        "/api/v1/maintenance/retention-preview",
        headers=owner_headers,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["eligible_message_count"] == 1
    assert preview.json()["protected_evidence_count"] == 1
    assert client.post(
        "/api/v1/maintenance/retention-apply",
        headers=owner_headers,
        json={"confirm": "wrong"},
    ).status_code == 422

    applied = client.post(
        "/api/v1/maintenance/retention-apply",
        headers=owner_headers,
        json={"confirm": "delete_expired_messages"},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["deleted_message_count"] == 1
    remaining = client.get(
        f"/api/v1/im/conversations/{conversation['id']}/messages",
        headers=owner_headers,
    ).json()
    assert remaining["total"] == 1
    assert remaining["items"][0]["provider_msgid"] == "old-confirmed"
