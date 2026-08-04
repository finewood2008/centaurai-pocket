from __future__ import annotations

import asyncio
import json
import sqlite3
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from centaur_pocket.main import (
    SensitiveJsonBodyLimitMiddleware,
    _derive_task_session_hmac_key,
)
from centaur_pocket.service import PocketError
from centaur_pocket.workspace.service import (
    TASK_AGREEMENT_MAX_REVISION_BYTES,
    TASK_AGREEMENT_MAX_REVISIONS,
    WorkspaceService,
    _canonical_task_agreement_json,
    _secret_hash,
    _task_agreement_digest,
)

WORKSPACE_ID = "ws_default"
OWNER_ID = "member_owner"
WORKSPACE_PATH = f"/api/v1/workspaces/{WORKSPACE_ID}"

AGREEMENT_KEYS = {
    "id",
    "workspace_id",
    "task_id",
    "issuer_member_id",
    "assignee_member_id",
    "status",
    "current_revision_no",
    "accepted_revision_no",
    "version",
    "created_at",
    "updated_at",
    "closed_at",
    "current_revision",
    "revisions",
    "decisions",
}
REVISION_KEYS = {
    "id",
    "case_id",
    "revision_no",
    "parent_revision_id",
    "base_task_version",
    "schema_version",
    "proposed_by_role",
    "proposed_by_member_id",
    "required_responder_role",
    "required_responder_member_id",
    "digest",
    "document",
    "reason",
    "created_at",
}
DOCUMENT_KEYS = {
    "schema",
    "workspace_id",
    "task_id",
    "agreement_id",
    "revision_no",
    "parent_digest",
    "proposer_role",
    "proposer_member_id",
    "responder_role",
    "responder_member_id",
    "issuer_member_id",
    "assignee_member_id",
    "acceptance_owner_id",
    "domain",
    "tier",
    "priority",
    "title",
    "purpose",
    "objective",
    "strategy",
    "key_points",
    "acceptance_criteria",
    "due_at",
}
DECISION_KEYS = {
    "id",
    "case_id",
    "revision_id",
    "revision_digest",
    "action",
    "actor_role",
    "actor_member_id",
    "actor_session_id",
    "assurance_method",
    "reason",
    "counter_revision_id",
    "version",
    "created_at",
}


def _headers(
    owner_headers: dict[str, str],
    key: str,
    *,
    device_id: str = "owner-test-device",
    version: int | None = None,
) -> dict[str, str]:
    result = {
        **owner_headers,
        "Idempotency-Key": key,
        "X-Device-ID": device_id,
    }
    if version is not None:
        result["If-Match"] = f'"{version}"'
    return result


def _create_external_issued_task(
    client: TestClient,
    owner_headers: dict[str, str],
    suffix: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    member = client.post(
        f"{WORKSPACE_PATH}/members",
        headers=_headers(owner_headers, f"p1ba-member-{suffix}"),
        json={
            "kind": "external",
            "role": "member",
            "display_name": f"承办人-{suffix}",
            "contact_ref": f"wecom://p1ba/{suffix}",
            "client_mutation_id": f"member-local-{suffix}",
        },
    )
    assert member.status_code == 201, member.text
    assignee = member.json()
    task = client.post(
        f"{WORKSPACE_PATH}/tasks",
        headers=_headers(owner_headers, f"p1ba-task-{suffix}"),
        json={
            "domain": "work",
            "title": f"任务-{suffix}",
            "purpose": "形成可验证价值",
            "objective": "按期完成交付",
            "strategy": "先验证，再分批实施。",
            "key_points": ["资源", "回滚"],
            "acceptance_criteria": ["记录完整", "结果通过"],
            "issuer_member_id": OWNER_ID,
            "assignee_member_id": assignee["id"],
            "acceptance_owner_id": OWNER_ID,
            "priority": "high",
            "tier": "strategic",
            "health": "on_track",
            "due_at": "2026-08-20T18:00:00+08:00",
        },
    )
    assert task.status_code == 201, task.text
    issued = client.post(
        f"{WORKSPACE_PATH}/tasks/{task.json()['id']}/transitions",
        headers=_headers(owner_headers, f"p1ba-issue-{suffix}"),
        json={
            "target_stage": "issued",
            "expected_version": task.json()["version"],
        },
    )
    assert issued.status_code == 200, issued.text
    return issued.json(), assignee


def _create_pending_agreement(
    client: TestClient,
    owner_headers: dict[str, str],
    suffix: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    task, assignee = _create_external_issued_task(client, owner_headers, suffix)
    invitation = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/alignment-invitations",
        headers=_headers(owner_headers, f"p1ba-invite-{suffix}"),
        json={"expected_version": task["version"]},
    )
    assert invitation.status_code == 201, invitation.text
    agreement = client.get(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/agreement",
        headers=owner_headers,
    )
    assert agreement.status_code == 200, agreement.text
    return task, assignee, {**invitation.json(), "agreement": agreement.json()}


def _exchange(
    client: TestClient,
    invitation: dict[str, Any],
    suffix: str,
    *,
    device_id: str | None = None,
) -> dict[str, Any]:
    device = device_id or f"assignee-device-{suffix}"
    response = client.post(
        "/api/v1/task-alignments/exchange",
        headers={"Idempotency-Key": f"p1ba-exchange-{suffix}"},
        json={
            "invitation_id": invitation["invitation_id"],
            "code": invitation["code"],
            "client_device_id": device,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _task_headers(
    exchange: dict[str, Any],
    key: str,
    version: int,
    *,
    device_id: str | None = None,
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {exchange['access_token']}",
        "X-Device-ID": device_id or exchange["session"]["client_device_id"],
        "Idempotency-Key": key,
        "If-Match": f'"{version}"',
    }


def _response_body(
    agreement: dict[str, Any],
    action: str,
    mutation_id: str,
    *,
    reason: str | None = None,
    counter_document: dict[str, Any] | None = None,
) -> dict[str, Any]:
    revision = agreement["current_revision"]
    return {
        "expected_agreement_version": agreement["version"],
        "revision_id": revision["id"],
        "expected_digest": revision["digest"],
        "action": action,
        "reason": reason,
        "counter_document": counter_document,
        "client_mutation_id": mutation_id,
    }


def test_task_agreement_golden_vectors_use_backend_canonicalizer() -> None:
    fixture_path = (
        Path(__file__).parent / "fixtures" / "task-agreement-canonical-vectors.json"
    )
    vectors = json.loads(fixture_path.read_text(encoding="utf-8"))
    shared_path = Path(
        "/home/user/centaur-executive-os-prototype/testdata/"
        "task-agreement-canonical-vectors.json"
    )
    if shared_path.exists():
        assert vectors == json.loads(shared_path.read_text(encoding="utf-8"))

    for vector in vectors:
        assert _canonical_task_agreement_json(vector["document"]) == vector["canonical"]
        canonical, digest = _task_agreement_digest(vector["document"])
        assert canonical == vector["canonical"]
        assert digest == vector["digest"]

    normalized_variant = deepcopy(vectors[0]["document"])
    normalized_variant["strategy"] = "先演练\r\n再分批迁移"
    normalized_variant["due_at"] = "2026-08-20T18:00:00+08:00"
    normalized_variant["title"] = unicodedata.normalize(
        "NFD", normalized_variant["title"]
    )
    assert _task_agreement_digest(normalized_variant)[1] == vectors[0]["digest"]

    float_document = deepcopy(vectors[0]["document"])
    float_document["revision_no"] = 1.0
    with pytest.raises(PocketError, match="浮点数"):
        _canonical_task_agreement_json(float_document)
    surrogate_document = deepcopy(vectors[0]["document"])
    surrogate_document["title"] = "invalid-\ud800"
    with pytest.raises(PocketError, match="Unicode"):
        _canonical_task_agreement_json(surrogate_document)


def test_sensitive_json_body_limit_handles_declared_and_chunked_payloads(
    client: TestClient,
) -> None:
    marker = "OVERSIZED_SECRET_MARKER"
    oversized = json.dumps(
        {
            "invitation_id": "align_body_limit",
            "code": marker * 400_000,
            "client_device_id": "body-limit-device",
        }
    ).encode("utf-8")
    declared = client.post(
        "/api/v1/task-alignments/exchange",
        headers={
            "Idempotency-Key": "p1ba-body-limit",
            "Content-Type": "application/json",
        },
        content=oversized,
    )
    assert declared.status_code == 413
    assert declared.json() == {"detail": "请求体过大"}
    assert marker not in declared.text
    assert declared.headers["cache-control"].startswith("no-store")

    downstream_called = False

    async def downstream(_scope: Any, _receive: Any, _send: Any) -> None:
        nonlocal downstream_called
        downstream_called = True

    middleware = SensitiveJsonBodyLimitMiddleware(downstream, limit=16)
    chunks = iter(
        [
            {"type": "http.request", "body": b"1234567890", "more_body": True},
            {"type": "http.request", "body": b"abcdefghij", "more_body": False},
        ]
    )
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return next(chunks)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    asyncio.run(
        middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/task-agreements/agreement-x/responses",
                "headers": [(b"transfer-encoding", b"chunked")],
            },
            receive,
            send,
        )
    )
    assert downstream_called is False
    assert sent[0]["status"] == 413
    assert json.loads(sent[1]["body"]) == {"detail": "请求体过大"}


def test_v4_schema_double_init_and_immutable_records(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    task, _assignee, invitation = _create_pending_agreement(
        client, owner_headers, "immutable"
    )
    exchange = _exchange(client, invitation, "immutable")
    agreement = exchange["agreement"]
    rejected = client.post(
        f"/api/v1/task-agreements/{agreement['id']}/responses",
        headers=_task_headers(exchange, "p1ba-reject-immutable", agreement["version"]),
        json=_response_body(
            agreement,
            "reject",
            "reject-immutable",
            reason="当前资源不足",
        ),
    )
    assert rejected.status_code == 200, rejected.text
    decision_id = rejected.json()["decision"]["id"]
    revision_id = agreement["current_revision"]["id"]

    service = client.app.state.workspace_service
    service.initialize()
    service.initialize()
    with service.database.connect() as connection:
        assert [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM secretary_workspace_schema_migrations "
                "ORDER BY version"
            )
        ] == [1, 2, 3, 4, 5, 6, 7]
        trigger_names = {
            row["name"]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'trigger' AND name LIKE 'trg_secretary_alignment_%'
                """
            )
        }
        assert len(trigger_names) == 6

    with (
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
        service.database.transaction() as connection,
    ):
        connection.execute(
            "UPDATE secretary_task_alignment_revisions SET reason = ? WHERE id = ?",
            ("tamper", revision_id),
        )
    with (
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
        service.database.transaction() as connection,
    ):
        connection.execute(
            "DELETE FROM secretary_task_alignment_decisions WHERE id = ?",
            (decision_id,),
        )
    with (
        pytest.raises(sqlite3.IntegrityError, match="binding is immutable"),
        service.database.transaction() as connection,
    ):
        connection.execute(
            """
            UPDATE secretary_task_alignment_invitations
            SET alignment_case_id = NULL,
                alignment_revision_id = NULL,
                alignment_revision_digest = NULL
            WHERE task_id = ?
            """,
            (task["id"],),
        )


def test_exchange_and_accept_are_scoped_secret_safe_and_replayable(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    task, _assignee, invitation = _create_pending_agreement(
        client, owner_headers, "accept"
    )
    exchange = _exchange(client, invitation, "accept")
    agreement = exchange["agreement"]
    assert set(agreement) == AGREEMENT_KEYS
    assert set(agreement["current_revision"]) == REVISION_KEYS
    assert set(agreement["current_revision"]["document"]) == DOCUMENT_KEYS
    assert exchange["token_type"] == "Bearer"
    assert exchange["access_token"].startswith("cp_task_at_")
    original_expiry = exchange["expires_at"]

    token_confusion = client.get(
        f"{WORKSPACE_PATH}/tasks",
        headers={
            "Authorization": f"Bearer {exchange['access_token']}",
            "X-Device-ID": exchange["session"]["client_device_id"],
        },
    )
    assert token_confusion.status_code == 401
    scoped_headers = {
        "Authorization": f"Bearer {exchange['access_token']}",
        "X-Device-ID": exchange["session"]["client_device_id"],
    }
    documents_confusion = client.get(
        f"{WORKSPACE_PATH}/documents", headers=scoped_headers
    )
    assert documents_confusion.status_code == 401
    mail_confusion = client.get("/api/v1/mail/accounts", headers=scoped_headers)
    assert mail_confusion.status_code == 401
    mixed_owner_context = client.get(
        f"/api/v1/task-agreements/{agreement['id']}",
        headers={
            **scoped_headers,
            "X-Owner-Token": owner_headers["Authorization"].removeprefix("Bearer "),
        },
    )
    assert mixed_owner_context.status_code == 403

    missing_device = client.get(
        f"/api/v1/task-agreements/{agreement['id']}",
        headers={"Authorization": f"Bearer {exchange['access_token']}"},
    )
    assert missing_device.status_code == 428
    wrong_device = client.get(
        f"/api/v1/task-agreements/{agreement['id']}",
        headers={
            "Authorization": f"Bearer {exchange['access_token']}",
            "X-Device-ID": "wrong-device",
        },
    )
    assert wrong_device.status_code == 403

    body = _response_body(agreement, "accept", "accept-mutation")
    response_headers = _task_headers(
        exchange, "p1ba-accept-response", agreement["version"]
    )
    accepted = client.post(
        f"/api/v1/task-agreements/{agreement['id']}/responses",
        headers=response_headers,
        json=body,
    )
    assert accepted.status_code == 200, accepted.text
    result = accepted.json()
    assert set(result) == {"agreement", "decision", "task"}
    assert set(result["decision"]) == DECISION_KEYS
    assert result["agreement"]["status"] == "accepted"
    assert result["agreement"]["accepted_revision_no"] == 1
    assert result["decision"]["assurance_method"] == "task_session"
    assert result["decision"]["actor_session_id"] == exchange["session"]["id"]
    assert result["task"] == {
        "id": task["id"],
        "stage": "aligned",
        "version": task["version"] + 1,
        "updated_at": result["task"]["updated_at"],
    }

    service = client.app.state.workspace_service
    with service.database.connect() as connection:
        terminal_session_before = connection.execute(
            """
            SELECT revoked_at, revoke_reason
            FROM secretary_task_assignee_sessions WHERE id = ?
            """,
            (exchange["session"]["id"],),
        ).fetchone()
        terminal_invitation_before = connection.execute(
            """
            SELECT revoked_at FROM secretary_task_alignment_invitations
            WHERE id = ?
            """,
            (invitation["invitation_id"],),
        ).fetchone()
    assert terminal_session_before["revoke_reason"] == "agreement_accepted"
    closed_exchange = client.post(
        "/api/v1/task-alignments/exchange",
        headers={"Idempotency-Key": "p1ba-exchange-accept"},
        json={
            "invitation_id": invitation["invitation_id"],
            "code": invitation["code"],
            "client_device_id": "assignee-device-accept",
        },
    )
    assert closed_exchange.status_code == 409
    with service.database.connect() as connection:
        terminal_session_after = connection.execute(
            """
            SELECT revoked_at, revoke_reason
            FROM secretary_task_assignee_sessions WHERE id = ?
            """,
            (exchange["session"]["id"],),
        ).fetchone()
        terminal_invitation_after = connection.execute(
            """
            SELECT revoked_at FROM secretary_task_alignment_invitations
            WHERE id = ?
            """,
            (invitation["invitation_id"],),
        ).fetchone()
    assert dict(terminal_session_after) == dict(terminal_session_before)
    assert dict(terminal_invitation_after) == dict(terminal_invitation_before)

    exact_replay = client.post(
        f"/api/v1/task-agreements/{agreement['id']}/responses",
        headers=response_headers,
        json=body,
    )
    assert exact_replay.status_code == 200, exact_replay.text
    assert exact_replay.json() == result

    changed_body = {**body, "client_mutation_id": "changed-mutation"}
    changed_replay = client.post(
        f"/api/v1/task-agreements/{agreement['id']}/responses",
        headers=response_headers,
        json=changed_body,
    )
    assert changed_replay.status_code == 409
    new_key = client.post(
        f"/api/v1/task-agreements/{agreement['id']}/responses",
        headers=_task_headers(exchange, "p1ba-accept-new-key", agreement["version"]),
        json=body,
    )
    assert new_key.status_code == 401
    closed_get = client.get(
        f"/api/v1/task-agreements/{agreement['id']}",
        headers={
            "Authorization": f"Bearer {exchange['access_token']}",
            "X-Device-ID": exchange["session"]["client_device_id"],
        },
    )
    assert closed_get.status_code == 401
    closed_wrong_device = client.post(
        f"/api/v1/task-agreements/{agreement['id']}/responses",
        headers=_task_headers(
            exchange,
            "p1ba-accept-response",
            agreement["version"],
            device_id="other-device",
        ),
        json=body,
    )
    assert closed_wrong_device.status_code == 403

    _other_task, _other_assignee, other_invitation = _create_pending_agreement(
        client, owner_headers, "closed-cross-case"
    )
    other_agreement = other_invitation["agreement"]
    closed_cross_case = client.post(
        f"/api/v1/task-agreements/{other_agreement['id']}/responses",
        headers=_task_headers(
            exchange,
            "p1ba-closed-cross-case",
            other_agreement["version"],
        ),
        json=_response_body(other_agreement, "accept", "closed-cross-case-mutation"),
    )
    assert closed_cross_case.status_code == 404

    with service.database.connect() as connection:
        session = connection.execute(
            "SELECT * FROM secretary_task_assignee_sessions WHERE id = ?",
            (exchange["session"]["id"],),
        ).fetchone()
        assert session["expires_at"] == original_expiry
        assert session["revoked_at"] is not None
        assert session["token_hash"] == _secret_hash(exchange["access_token"])
        dump = "\n".join(connection.iterdump())
    assert exchange["access_token"] not in dump
    assert invitation["code"] not in dump
    audit = client.get(f"{WORKSPACE_PATH}/audit?after=0", headers=owner_headers)
    assert exchange["access_token"] not in audit.text
    assert invitation["code"] not in audit.text


def test_non_agreement_version_change_survives_but_content_drift_stales(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    task, _assignee, invitation = _create_pending_agreement(
        client, owner_headers, "health"
    )
    health_update = client.patch(
        f"{WORKSPACE_PATH}/tasks/{task['id']}",
        headers=_headers(
            owner_headers,
            "p1ba-health-update",
            version=task["version"],
        ),
        json={"health": "at_risk"},
    )
    assert health_update.status_code == 200, health_update.text
    assert health_update.json()["version"] == task["version"] + 1
    exchange = _exchange(client, invitation, "health")
    agreement = exchange["agreement"]
    accepted = client.post(
        f"/api/v1/task-agreements/{agreement['id']}/responses",
        headers=_task_headers(exchange, "p1ba-health-accept", agreement["version"]),
        json=_response_body(agreement, "accept", "health-accept-mutation"),
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["task"]["version"] == task["version"] + 2
    accepted_task = accepted.json()["task"]
    locked_patches = [
        {"strategy": "未签署的新策略"},
        {"key_points": ["未签署的新关键点"]},
        {"priority": "critical"},
        {"title": "未签署的新标题"},
    ]
    for index, patch in enumerate(locked_patches):
        locked = client.patch(
            f"{WORKSPACE_PATH}/tasks/{task['id']}",
            headers=_headers(
                owner_headers,
                f"p1ba-accepted-patch-{index}",
                version=accepted_task["version"],
            ),
            json=patch,
        )
        assert locked.status_code == 409
    accepted_due_change = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/changes",
        headers=_headers(owner_headers, "p1ba-accepted-due-change"),
        json={
            "change_type": "due_at",
            "base_version": accepted_task["version"],
            "reason": "尝试改变已接受期限",
            "patch": {"due_at": "2026-09-01T18:00:00+08:00"},
            "client_mutation_id": "accepted-due-change",
        },
    )
    assert accepted_due_change.status_code == 201, accepted_due_change.text
    assert accepted_due_change.json()["status"] == "proposed"
    assert accepted_due_change.json()["proposal_digest"].startswith("sha256:")
    accepted_health = client.patch(
        f"{WORKSPACE_PATH}/tasks/{task['id']}",
        headers=_headers(
            owner_headers,
            "p1ba-accepted-health",
            version=accepted_task["version"],
        ),
        json={"health": "blocked"},
    )
    assert accepted_health.status_code == 200, accepted_health.text
    assert accepted_health.json()["health"] == "blocked"

    drift_task, _drift_assignee, drift_invitation = _create_pending_agreement(
        client, owner_headers, "drift"
    )
    service = client.app.state.workspace_service
    with service.database.transaction() as connection:
        connection.execute(
            """
            UPDATE secretary_business_tasks
            SET objective = '越权改变后的目标', version = version + 1
            WHERE id = ?
            """,
            (drift_task["id"],),
        )
    drift_exchange = client.post(
        "/api/v1/task-alignments/exchange",
        headers={"Idempotency-Key": "p1ba-drift-exchange"},
        json={
            "invitation_id": drift_invitation["invitation_id"],
            "code": drift_invitation["code"],
            "client_device_id": "drift-device",
        },
    )
    assert drift_exchange.status_code == 409
    stale = client.get(
        f"{WORKSPACE_PATH}/tasks/{drift_task['id']}/agreement",
        headers=owner_headers,
    )
    assert stale.status_code == 200
    assert stale.json()["status"] == "stale"


def test_pending_agreement_blocks_changes_and_accepted_agreement_uses_p1bb(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    task, _assignee = _create_external_issued_task(
        client, owner_headers, "change-bypass"
    )
    proposed = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/changes",
        headers=_headers(owner_headers, "p1ba-change-before-agreement"),
        json={
            "change_type": "due_at",
            "base_version": task["version"],
            "reason": "先提出延期",
            "patch": {"due_at": "2026-08-30T18:00:00+08:00"},
            "client_mutation_id": "change-before-agreement",
        },
    )
    assert proposed.status_code == 201, proposed.text
    invitation = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/alignment-invitations",
        headers=_headers(owner_headers, "p1ba-change-bypass-invite"),
        json={"expected_version": task["version"]},
    )
    assert invitation.status_code == 201, invitation.text

    accepted_change = client.post(
        f"{WORKSPACE_PATH}/changes/{proposed.json()['id']}/decision",
        headers=_headers(owner_headers, "p1ba-accept-change-bypass"),
        json={"decision": "accept", "expected_version": task["version"]},
    )
    assert accepted_change.status_code == 409
    assert "双方确认协议" in accepted_change.text

    new_change = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/changes",
        headers=_headers(owner_headers, "p1ba-change-during-agreement"),
        json={
            "change_type": "acceptance_criteria",
            "base_version": task["version"],
            "reason": "尝试绕过协议",
            "patch": {"acceptance_criteria": ["新的验收条件"]},
            "client_mutation_id": "change-during-agreement",
        },
    )
    assert new_change.status_code == 409
    assert "协议待回应" in new_change.text

    agreement = client.get(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/agreement",
        headers=owner_headers,
    )
    assert agreement.status_code == 200
    assert agreement.json()["status"] == "pending"
    assert (
        agreement.json()["current_revision"]["document"]["due_at"] == (task["due_at"])
    )

    exchange = _exchange(client, invitation.json(), "change-bypass")
    accepted_agreement = client.post(
        f"/api/v1/task-agreements/{agreement.json()['id']}/responses",
        headers=_task_headers(
            exchange, "p1ba-change-bypass-align", agreement.json()["version"]
        ),
        json=_response_body(agreement.json(), "accept", "change-bypass-align-mutation"),
    )
    assert accepted_agreement.status_code == 200, accepted_agreement.text
    accepted_task = accepted_agreement.json()["task"]

    canceled_stale = client.post(
        f"{WORKSPACE_PATH}/changes/{proposed.json()['id']}/decision",
        headers=_headers(owner_headers, "p1ba-cancel-stale-change"),
        json={
            "decision": "cancel",
            "expected_version": accepted_task["version"],
            "reason": "任务已完成初始对齐，取消旧基线提案",
        },
    )
    assert canceled_stale.status_code == 200, canceled_stale.text
    assert canceled_stale.json()["change"]["status"] == "canceled"

    new_abnormal = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/changes",
        headers=_headers(owner_headers, "p1ba-accepted-abnormal-create"),
        json={
            "change_type": "abnormal_close",
            "base_version": accepted_task["version"],
            "reason": "外部条件发生变化，发起双方确认",
            "patch": {"abnormal_close_reason": "外部条件不具备"},
            "client_mutation_id": "accepted-abnormal-create",
        },
    )
    assert new_abnormal.status_code == 201, new_abnormal.text
    assert new_abnormal.json()["status"] == "proposed"
    assert new_abnormal.json()["proposal_digest"].startswith("sha256:")

    owner_still_cannot_accept = client.post(
        f"{WORKSPACE_PATH}/changes/{new_abnormal.json()['id']}/decision",
        headers=_headers(owner_headers, "p1bb-owner-cannot-accept-external"),
        json={"decision": "accept", "expected_version": accepted_task["version"]},
    )
    assert owner_still_cannot_accept.status_code == 409
    assert "双方确认协议" in owner_still_cannot_accept.text


def test_counter_owner_response_reject_and_cross_case_idor(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    task, assignee, invitation = _create_pending_agreement(
        client, owner_headers, "counter"
    )
    exchange = _exchange(client, invitation, "counter")
    agreement = exchange["agreement"]
    revision = agreement["current_revision"]

    owner_self_accept = client.post(
        f"/api/v1/task-agreements/{agreement['id']}/responses",
        headers=_headers(
            owner_headers,
            "p1ba-owner-self-accept",
            version=agreement["version"],
        ),
        json=_response_body(agreement, "accept", "owner-self-accept"),
    )
    assert owner_self_accept.status_code == 403

    sensitive_marker = "COUNTER_SECRET_MUST_NOT_REFLECT"
    invalid_counter_document = {
        **revision["document"],
        "revision_no": 2,
        "parent_digest": revision["digest"],
        "proposer_role": "assignee",
        "proposer_member_id": assignee["id"],
        "responder_role": "issuer",
        "responder_member_id": OWNER_ID,
        "unexpected_sensitive_field": sensitive_marker,
    }
    invalid_counter = client.post(
        f"/api/v1/task-agreements/{agreement['id']}/responses",
        headers=_task_headers(exchange, "p1ba-invalid-counter", agreement["version"]),
        json=_response_body(
            agreement,
            "counter",
            "invalid-counter-mutation",
            reason="测试严格字段",
            counter_document=invalid_counter_document,
        ),
    )
    assert invalid_counter.status_code == 422
    assert invalid_counter.json() == {"detail": "请求格式无效"}
    assert sensitive_marker not in invalid_counter.text

    counter_document = {
        **revision["document"],
        "revision_no": 2,
        "parent_digest": revision["digest"],
        "proposer_role": "assignee",
        "proposer_member_id": assignee["id"],
        "responder_role": "issuer",
        "responder_member_id": OWNER_ID,
        "title": "任务-counter（承办人反提案）",
        "purpose": "形成经过双方确认的可验证价值",
        "strategy": "策" * 200_000,
        "due_at": "2026-08-21T18:00:00+08:00",
    }
    valid_counter_body = _response_body(
        agreement,
        "counter",
        "counter-mutation",
        reason="需要增加一天并澄清价值",
        counter_document=counter_document,
    )
    coercion_cases = [
        {**valid_counter_body, "expected_agreement_version": True},
        {
            **valid_counter_body,
            "counter_document": {**counter_document, "revision_no": 2.0},
        },
        {
            **valid_counter_body,
            "counter_document": {**counter_document, "due_at": 1_786_752_000},
        },
    ]
    for index, coercion_body in enumerate(coercion_cases):
        coercion = client.post(
            f"/api/v1/task-agreements/{agreement['id']}/responses",
            headers=_task_headers(
                exchange, f"p1ba-no-coercion-{index}", agreement["version"]
            ),
            json=coercion_body,
        )
        assert coercion.status_code == 422
        assert coercion.json() == {"detail": "请求格式无效"}

    countered = client.post(
        f"/api/v1/task-agreements/{agreement['id']}/responses",
        headers=_task_headers(exchange, "p1ba-counter-response", agreement["version"]),
        json=valid_counter_body,
    )
    assert countered.status_code == 200, countered.text
    counter_result = countered.json()
    counter_agreement = counter_result["agreement"]
    assert counter_result["task"] is None
    assert counter_agreement["status"] == "pending"
    assert counter_agreement["current_revision_no"] == 2
    assert counter_agreement["version"] == agreement["version"] + 1
    assert counter_agreement["current_revision"]["base_task_version"] == task["version"]
    assert counter_agreement["current_revision"]["document"]["due_at"] == (
        "2026-08-21T10:00:00Z"
    )
    assert (
        counter_result["decision"]["counter_revision_id"]
        == (counter_agreement["current_revision"]["id"])
    )

    forbidden_invite = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/alignment-invitations",
        headers=_headers(owner_headers, "p1ba-counter-waits-owner"),
        json={"expected_version": task["version"]},
    )
    assert forbidden_invite.status_code == 409
    assert "下达人回应" in forbidden_invite.text

    owner_accept_body = _response_body(
        counter_agreement, "accept", "owner-accept-counter"
    )
    owner_accept = client.post(
        f"/api/v1/task-agreements/{agreement['id']}/responses",
        headers=_headers(
            owner_headers,
            "p1ba-owner-accept-counter",
            version=counter_agreement["version"],
        ),
        json=owner_accept_body,
    )
    assert owner_accept.status_code == 200, owner_accept.text
    assert owner_accept.json()["decision"]["assurance_method"] == "owner_token"
    assert owner_accept.json()["decision"]["actor_session_id"] is None
    assert owner_accept.json()["task"]["stage"] == "aligned"

    task_two, _assignee_two, invitation_two = _create_pending_agreement(
        client, owner_headers, "reject"
    )
    exchange_two = _exchange(client, invitation_two, "reject")
    agreement_two = exchange_two["agreement"]
    cross_case = client.get(
        f"/api/v1/task-agreements/{agreement_two['id']}",
        headers={
            "Authorization": f"Bearer {exchange['access_token']}",
            "X-Device-ID": exchange["session"]["client_device_id"],
        },
    )
    assert cross_case.status_code == 401  # first session was closed and revoked
    active_cross_case = client.get(
        f"/api/v1/task-agreements/{agreement['id']}",
        headers={
            "Authorization": f"Bearer {exchange_two['access_token']}",
            "X-Device-ID": exchange_two["session"]["client_device_id"],
        },
    )
    assert active_cross_case.status_code == 404

    reject_headers = _task_headers(
        exchange_two, "p1ba-reject-response", agreement_two["version"]
    )
    reject_body = _response_body(
        agreement_two,
        "reject",
        "reject-mutation",
        reason="验收路径不可执行",
    )
    rejected = client.post(
        f"/api/v1/task-agreements/{agreement_two['id']}/responses",
        headers=reject_headers,
        json=reject_body,
    )
    assert rejected.status_code == 200, rejected.text
    rejected_result = rejected.json()
    assert rejected_result["agreement"]["status"] == "rejected"
    assert rejected_result["task"] is None
    service = client.app.state.workspace_service
    closed_reject_exchange = client.post(
        "/api/v1/task-alignments/exchange",
        headers={"Idempotency-Key": "p1ba-exchange-reject"},
        json={
            "invitation_id": invitation_two["invitation_id"],
            "code": invitation_two["code"],
            "client_device_id": "assignee-device-reject",
        },
    )
    assert closed_reject_exchange.status_code == 409
    with service.database.connect() as connection:
        rejected_session = connection.execute(
            """
            SELECT revoke_reason FROM secretary_task_assignee_sessions
            WHERE id = ?
            """,
            (exchange_two["session"]["id"],),
        ).fetchone()
    assert rejected_session["revoke_reason"] == "agreement_rejected"
    reject_exact_replay = client.post(
        f"/api/v1/task-agreements/{agreement_two['id']}/responses",
        headers=reject_headers,
        json=reject_body,
    )
    assert reject_exact_replay.status_code == 200, reject_exact_replay.text
    assert reject_exact_replay.json() == rejected_result
    tasks = client.get(f"{WORKSPACE_PATH}/tasks", headers=owner_headers).json()["items"]
    stored_task_two = next(item for item in tasks if item["id"] == task_two["id"])
    assert stored_task_two["stage"] == "issued"
    assert stored_task_two["version"] == task_two["version"]


def test_exchange_replay_requires_valid_dual_channel_secret(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    task, _assignee, invitation = _create_pending_agreement(
        client, owner_headers, "exchange-replay"
    )
    owner_context = client.post(
        "/api/v1/task-alignments/exchange",
        headers={
            **owner_headers,
            "Idempotency-Key": "p1ba-owner-exchange",
        },
        json={
            "invitation_id": invitation["invitation_id"],
            "code": invitation["code"],
            "client_device_id": "assignee-replay-device",
        },
    )
    assert owner_context.status_code == 403

    sensitive_code = "CODE_SECRET_MUST_NOT_REFLECT" * 8
    invalid_exchange = client.post(
        "/api/v1/task-alignments/exchange",
        headers={"Idempotency-Key": "p1ba-invalid-exchange"},
        json={
            "invitation_id": invitation["invitation_id"],
            "code": sensitive_code,
            "client_device_id": "assignee-replay-device",
        },
    )
    assert invalid_exchange.status_code == 422
    assert invalid_exchange.json() == {"detail": "请求格式无效"}
    assert "CODE_SECRET_MUST_NOT_REFLECT" not in invalid_exchange.text

    exact_payload = {
        "invitation_id": invitation["invitation_id"],
        "code": invitation["code"],
        "client_device_id": "assignee-replay-device",
    }

    def exact_exchange() -> Any:
        return client.post(
            "/api/v1/task-alignments/exchange",
            headers={"Idempotency-Key": "p1ba-exchange-exchange-replay"},
            json=exact_payload,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        concurrent_initial = [
            future.result()
            for future in [
                executor.submit(exact_exchange),
                executor.submit(exact_exchange),
            ]
        ]
    assert [item.status_code for item in concurrent_initial] == [200, 200]
    assert concurrent_initial[0].json() == concurrent_initial[1].json()
    exchange = concurrent_initial[0].json()

    wrong_code = client.post(
        "/api/v1/task-alignments/exchange",
        headers={"Idempotency-Key": "p1ba-exchange-wrong-code"},
        json={
            "invitation_id": invitation["invitation_id"],
            "code": "AAAA-AAAA-AAAA",
            "client_device_id": "assignee-replay-device",
        },
    )
    assert wrong_code.status_code == 401
    still_live = client.get(
        f"/api/v1/task-agreements/{exchange['agreement']['id']}",
        headers={
            "Authorization": f"Bearer {exchange['access_token']}",
            "X-Device-ID": "assignee-replay-device",
        },
    )
    assert still_live.status_code == 200

    exact_retries = [exact_exchange() for _ in range(3)]
    assert all(item.status_code == 200 for item in exact_retries)
    assert all(item.json() == exchange for item in exact_retries)
    deterministic_live = client.get(
        f"/api/v1/task-agreements/{exchange['agreement']['id']}",
        headers={
            "Authorization": f"Bearer {exchange['access_token']}",
            "X-Device-ID": "assignee-replay-device",
        },
    )
    assert deterministic_live.status_code == 200

    service = client.app.state.workspace_service
    restarted_service = WorkspaceService(
        service.database,
        task_session_hmac_key=_derive_task_session_hmac_key(
            client.app.state.service.owner_token
        ),
    )
    restarted_exchange = restarted_service.exchange_task_alignment(
        exact_payload,
        idempotency_key="p1ba-exchange-exchange-replay",
    )
    assert restarted_exchange == exchange
    with service.database.connect() as connection:
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM secretary_task_assignee_sessions
                WHERE invitation_id = ?
                """,
                (invitation["invitation_id"],),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM secretary_workspace_events
                WHERE aggregate_id = ?
                  AND event_type = 'task.agreement_session_issued'
                """,
                (exchange["agreement"]["id"],),
            ).fetchone()[0]
            == 1
        )

    conflicting_replay = client.post(
        "/api/v1/task-alignments/exchange",
        headers={"Idempotency-Key": "p1ba-exchange-replay-key"},
        json=exact_payload,
    )
    assert conflicting_replay.status_code == 409
    assert exchange["access_token"] not in conflicting_replay.text
    revoked = client.get(
        f"/api/v1/task-agreements/{exchange['agreement']['id']}",
        headers={
            "Authorization": f"Bearer {exchange['access_token']}",
            "X-Device-ID": "assignee-replay-device",
        },
    )
    assert revoked.status_code == 401

    replacement = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/alignment-invitations",
        headers=_headers(owner_headers, "p1ba-replacement-invite"),
        json={"expected_version": task["version"]},
    )
    assert replacement.status_code == 201, replacement.text
    replacement_exchange = _exchange(client, replacement.json(), "expired-recovery")
    with client.app.state.workspace_service.database.transaction() as connection:
        connection.execute(
            """
            UPDATE secretary_task_assignee_sessions
            SET expires_at = '2026-01-01T00:00:00Z'
            WHERE id = ?
            """,
            (replacement_exchange["session"]["id"],),
        )
        invitation_revoked_before = connection.execute(
            """
            SELECT revoked_at FROM secretary_task_alignment_invitations
            WHERE id = ?
            """,
            (replacement.json()["invitation_id"],),
        ).fetchone()["revoked_at"]
    expired_recovery = client.post(
        "/api/v1/task-alignments/exchange",
        headers={"Idempotency-Key": "p1ba-exchange-expired-recovery"},
        json={
            "invitation_id": replacement.json()["invitation_id"],
            "code": replacement.json()["code"],
            "client_device_id": "assignee-device-expired-recovery",
        },
    )
    assert expired_recovery.status_code == 409
    with client.app.state.workspace_service.database.connect() as connection:
        expired_session = connection.execute(
            """
            SELECT revoked_at, revoke_reason
            FROM secretary_task_assignee_sessions WHERE id = ?
            """,
            (replacement_exchange["session"]["id"],),
        ).fetchone()
        invitation_revoked_after = connection.execute(
            """
            SELECT revoked_at FROM secretary_task_alignment_invitations
            WHERE id = ?
            """,
            (replacement.json()["invitation_id"],),
        ).fetchone()["revoked_at"]
    assert expired_session["revoked_at"] is not None
    assert expired_session["revoke_reason"] == "expired"
    assert invitation_revoked_after == invitation_revoked_before


def test_replacement_invitation_revokes_live_session_and_closes_auth_race(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    task, _assignee, invitation = _create_pending_agreement(
        client, owner_headers, "replacement-live"
    )
    exchange = _exchange(client, invitation, "replacement-live")
    agreement = exchange["agreement"]
    service = client.app.state.workspace_service
    old_principal = service.authenticate_task_session(
        exchange["access_token"],
        requested_device_id="assignee-device-replacement-live",
    )

    replacement = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/alignment-invitations",
        headers=_headers(owner_headers, "p1ba-replacement-live-new"),
        json={"expected_version": task["version"]},
    )
    assert replacement.status_code == 201, replacement.text
    with service.database.connect() as connection:
        old_session = connection.execute(
            """
            SELECT revoked_at, revoke_reason
            FROM secretary_task_assignee_sessions WHERE id = ?
            """,
            (exchange["session"]["id"],),
        ).fetchone()
        old_invitation = connection.execute(
            """
            SELECT revoked_at FROM secretary_task_alignment_invitations
            WHERE id = ?
            """,
            (invitation["invitation_id"],),
        ).fetchone()
    assert old_session["revoked_at"] is not None
    assert old_session["revoke_reason"] == "superseded_by_new_invitation"
    assert old_invitation["revoked_at"] is not None

    old_token = client.get(
        f"/api/v1/task-agreements/{agreement['id']}",
        headers={
            "Authorization": f"Bearer {exchange['access_token']}",
            "X-Device-ID": "assignee-device-replacement-live",
        },
    )
    assert old_token.status_code == 401

    # Simulate dependency authentication completing immediately before the
    # Owner supersedes the invitation. The write transaction must revalidate.
    with pytest.raises(PocketError) as raced_response:
        service.respond_task_agreement(
            agreement["id"],
            _response_body(agreement, "accept", "superseded-race"),
            old_principal,
            idempotency_key="p1ba-superseded-race",
            device_id="assignee-device-replacement-live",
        )
    assert raced_response.value.status_code == 401
    with service.database.connect() as connection:
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM secretary_task_alignment_decisions
                WHERE case_id = ?
                """,
                (agreement["id"],),
            ).fetchone()[0]
            == 0
        )

    replacement_exchange = _exchange(
        client,
        replacement.json(),
        "replacement-live-new",
        device_id="assignee-device-replacement-live-new",
    )
    assert replacement_exchange["agreement"]["id"] == agreement["id"]
    assert replacement_exchange["access_token"] != exchange["access_token"]


def test_exact_exchange_remains_stable_across_counter_roundtrip(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    _task, assignee, invitation = _create_pending_agreement(
        client, owner_headers, "counter-roundtrip"
    )
    exchange = _exchange(client, invitation, "counter-roundtrip")
    agreement = exchange["agreement"]
    revision = agreement["current_revision"]
    assignee_counter_document = {
        **revision["document"],
        "revision_no": 2,
        "parent_digest": revision["digest"],
        "proposer_role": "assignee",
        "proposer_member_id": assignee["id"],
        "responder_role": "issuer",
        "responder_member_id": OWNER_ID,
        "strategy": "承办人先提交可验证样例。",
    }
    assignee_counter = client.post(
        f"/api/v1/task-agreements/{agreement['id']}/responses",
        headers=_task_headers(
            exchange, "p1ba-counter-roundtrip-assignee", agreement["version"]
        ),
        json=_response_body(
            agreement,
            "counter",
            "counter-roundtrip-assignee",
            reason="补充样例步骤",
            counter_document=assignee_counter_document,
        ),
    )
    assert assignee_counter.status_code == 200, assignee_counter.text
    agreement = assignee_counter.json()["agreement"]

    exact_exchange_payload = {
        "invitation_id": invitation["invitation_id"],
        "code": invitation["code"],
        "client_device_id": "assignee-device-counter-roundtrip",
    }
    after_assignee_counter = client.post(
        "/api/v1/task-alignments/exchange",
        headers={"Idempotency-Key": "p1ba-exchange-counter-roundtrip"},
        json=exact_exchange_payload,
    )
    assert after_assignee_counter.status_code == 200
    assert after_assignee_counter.json()["access_token"] == exchange["access_token"]
    assert after_assignee_counter.json()["agreement"] == agreement

    revision = agreement["current_revision"]
    owner_counter_document = {
        **revision["document"],
        "revision_no": 3,
        "parent_digest": revision["digest"],
        "proposer_role": "issuer",
        "proposer_member_id": OWNER_ID,
        "responder_role": "assignee",
        "responder_member_id": assignee["id"],
        "strategy": "下达人确认样例后再分批执行。",
    }
    owner_counter = client.post(
        f"/api/v1/task-agreements/{agreement['id']}/responses",
        headers=_headers(
            owner_headers,
            "p1ba-counter-roundtrip-owner",
            version=agreement["version"],
        ),
        json=_response_body(
            agreement,
            "counter",
            "counter-roundtrip-owner",
            reason="增加下达人核验",
            counter_document=owner_counter_document,
        ),
    )
    assert owner_counter.status_code == 200, owner_counter.text
    agreement = owner_counter.json()["agreement"]

    after_owner_counter = client.post(
        "/api/v1/task-alignments/exchange",
        headers={"Idempotency-Key": "p1ba-exchange-counter-roundtrip"},
        json=exact_exchange_payload,
    )
    assert after_owner_counter.status_code == 200
    assert after_owner_counter.json()["access_token"] == exchange["access_token"]
    assert after_owner_counter.json()["agreement"] == agreement

    accepted = client.post(
        f"/api/v1/task-agreements/{agreement['id']}/responses",
        headers=_task_headers(
            exchange, "p1ba-counter-roundtrip-accept", agreement["version"]
        ),
        json=_response_body(agreement, "accept", "counter-roundtrip-final-accept"),
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["agreement"]["status"] == "accepted"


def test_agreement_revision_capacity_limits_are_atomic(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    _task, _assignee, invitation = _create_pending_agreement(
        client, owner_headers, "capacity"
    )
    service = client.app.state.workspace_service
    agreement = invitation["agreement"]
    revision = agreement["current_revision"]
    oversized_document = {
        **revision["document"],
        "revision_no": 2,
        "parent_digest": revision["digest"],
        "proposer_role": "assignee",
        "proposer_member_id": agreement["assignee_member_id"],
        "responder_role": "issuer",
        "responder_member_id": agreement["issuer_member_id"],
        "strategy": "x" * TASK_AGREEMENT_MAX_REVISION_BYTES,
    }
    with (
        pytest.raises(PocketError) as oversized_error,
        service.database.transaction() as connection,
    ):
        service._insert_alignment_revision(
            connection,
            case_id=agreement["id"],
            revision_id="agreement_revision_oversized",
            revision_no=2,
            parent_revision_id=revision["id"],
            base_task_version=revision["base_task_version"],
            proposer_role="assignee",
            proposer_member_id=agreement["assignee_member_id"],
            responder_role="issuer",
            responder_member_id=agreement["issuer_member_id"],
            document=oversized_document,
            reason="容量边界测试",
            now="2026-08-02T00:00:00Z",
        )
    assert oversized_error.value.status_code == 413
    with service.database.connect() as connection:
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM secretary_task_alignment_revisions
                WHERE case_id = ?
                """,
                (agreement["id"],),
            ).fetchone()[0]
            == 1
        )

    exchange = _exchange(client, invitation, "capacity")
    agreement = exchange["agreement"]
    capacity_failure = None
    for index in range(1, 9):
        revision = agreement["current_revision"]
        actor_role = revision["required_responder_role"]
        actor_member_id = revision["required_responder_member_id"]
        responder_role = "issuer" if actor_role == "assignee" else "assignee"
        responder_member_id = (
            agreement["issuer_member_id"]
            if responder_role == "issuer"
            else agreement["assignee_member_id"]
        )
        counter_document = {
            **revision["document"],
            "revision_no": revision["revision_no"] + 1,
            "parent_digest": revision["digest"],
            "proposer_role": actor_role,
            "proposer_member_id": actor_member_id,
            "responder_role": responder_role,
            "responder_member_id": responder_member_id,
            "strategy": "😀" * 200_000,
        }
        body = _response_body(
            agreement,
            "counter",
            f"capacity-counter-{index}",
            reason=f"容量累计测试 {index}",
            counter_document=counter_document,
        )
        headers = (
            _task_headers(
                exchange, f"p1ba-capacity-counter-{index}", agreement["version"]
            )
            if actor_role == "assignee"
            else _headers(
                owner_headers,
                f"p1ba-capacity-counter-{index}",
                version=agreement["version"],
            )
        )
        countered = client.post(
            f"/api/v1/task-agreements/{agreement['id']}/responses",
            headers=headers,
            json=body,
        )
        if countered.status_code == 413:
            capacity_failure = countered
            break
        assert countered.status_code == 200, countered.text
        agreement = countered.json()["agreement"]
    assert capacity_failure is not None
    persisted = client.get(
        f"{WORKSPACE_PATH}/tasks/{agreement['task_id']}/agreement",
        headers=owner_headers,
    )
    assert persisted.status_code == 200
    assert persisted.json() == agreement

    _count_task, _count_assignee, count_invitation = _create_pending_agreement(
        client, owner_headers, "revision-count"
    )
    count_agreement = count_invitation["agreement"]
    previous = count_agreement["current_revision"]
    with service.database.transaction() as connection:
        for revision_no in range(2, TASK_AGREEMENT_MAX_REVISIONS + 1):
            document = {
                **previous["document"],
                "revision_no": revision_no,
                "parent_digest": previous["digest"],
            }
            row = service._insert_alignment_revision(
                connection,
                case_id=count_agreement["id"],
                revision_id=f"agreement_revision_count_{revision_no}",
                revision_no=revision_no,
                parent_revision_id=previous["id"],
                base_task_version=previous["base_task_version"],
                proposer_role=previous["proposed_by_role"],
                proposer_member_id=previous["proposed_by_member_id"],
                responder_role=previous["required_responder_role"],
                responder_member_id=previous["required_responder_member_id"],
                document=document,
                reason="修订数边界",
                now="2026-08-02T00:00:00Z",
            )
            previous = service._agreement_revision_dict(row)
    overflow_document = {
        **previous["document"],
        "revision_no": TASK_AGREEMENT_MAX_REVISIONS + 1,
        "parent_digest": previous["digest"],
    }
    with (
        pytest.raises(PocketError) as count_error,
        service.database.transaction() as connection,
    ):
        service._insert_alignment_revision(
            connection,
            case_id=count_agreement["id"],
            revision_id="agreement_revision_count_overflow",
            revision_no=TASK_AGREEMENT_MAX_REVISIONS + 1,
            parent_revision_id=previous["id"],
            base_task_version=previous["base_task_version"],
            proposer_role=previous["proposed_by_role"],
            proposer_member_id=previous["proposed_by_member_id"],
            responder_role=previous["required_responder_role"],
            responder_member_id=previous["required_responder_member_id"],
            document=overflow_document,
            reason="修订数溢出",
            now="2026-08-02T00:00:00Z",
        )
    assert count_error.value.status_code == 413
    with service.database.connect() as connection:
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM secretary_task_alignment_revisions
                WHERE case_id = ?
                """,
                (count_agreement["id"],),
            ).fetchone()[0]
            == TASK_AGREEMENT_MAX_REVISIONS
        )


def test_legacy_unbound_invitation_keeps_exact_flow_without_retroactive_proof(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    task, assignee = _create_external_issued_task(client, owner_headers, "legacy")
    invitation_id = "align_legacy_v3_unbound"
    code = "ABCD-EFGH-JKMP"
    now = datetime.now(UTC)
    expires_at = (
        (now + timedelta(minutes=10))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    created_at = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    service = client.app.state.workspace_service
    with service.database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO secretary_task_alignment_invitations(
                id, workspace_id, task_id, task_version, assignee_member_id,
                code_hash, failed_attempts, max_attempts, created_by,
                created_device_id, creation_idempotency_key, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, 5, ?, ?, ?, ?, ?)
            """,
            (
                invitation_id,
                WORKSPACE_ID,
                task["id"],
                task["version"],
                assignee["id"],
                _secret_hash(code),
                OWNER_ID,
                "legacy-device",
                "legacy-v3-invite-key",
                created_at,
                expires_at,
            ),
        )

    preview = client.post(
        "/api/v1/task-alignments/preview",
        json={"invitation_id": invitation_id, "code": code},
    )
    assert preview.status_code == 200, preview.text
    assert set(preview.json()) == {
        "invitation_id",
        "confirmation_token",
        "confirmation_expires_at",
        "alignment",
    }
    confirmed = client.post(
        "/api/v1/task-alignments/confirm",
        json={
            "invitation_id": invitation_id,
            "confirmation_token": preview.json()["confirmation_token"],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert set(confirmed.json()) == {
        "invitation_id",
        "task_id",
        "stage",
        "version",
        "assignee_member_id",
        "assignee_label",
        "confirmed_at",
    }
    with service.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM secretary_task_alignment_cases WHERE task_id = ?",
                (task["id"],),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                """
                SELECT COUNT(*) FROM secretary_task_alignment_decisions decision
                JOIN secretary_task_alignment_revisions revision
                  ON revision.id = decision.revision_id
                JOIN secretary_task_alignment_cases agreement
                  ON agreement.id = revision.case_id
                WHERE agreement.task_id = ?
                """,
                (task["id"],),
            ).fetchone()[0]
            == 0
        )


def test_accept_rolls_back_decision_case_and_task_on_database_failure(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    task, _assignee, invitation = _create_pending_agreement(
        client, owner_headers, "atomic"
    )
    exchange = _exchange(client, invitation, "atomic")
    agreement = exchange["agreement"]
    service = client.app.state.workspace_service
    with service.database.transaction() as connection:
        connection.execute(
            f"""
            CREATE TRIGGER fail_p1ba_accept_for_test
            BEFORE UPDATE ON secretary_business_tasks
            WHEN NEW.id = '{task["id"]}' AND NEW.stage = 'aligned'
            BEGIN
                SELECT RAISE(ABORT, 'forced atomicity failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced atomicity failure"):
        client.post(
            f"/api/v1/task-agreements/{agreement['id']}/responses",
            headers=_task_headers(
                exchange, "p1ba-atomic-response", agreement["version"]
            ),
            json=_response_body(agreement, "accept", "atomic-mutation"),
        )

    with service.database.transaction() as connection:
        connection.execute("DROP TRIGGER fail_p1ba_accept_for_test")
        case = connection.execute(
            "SELECT * FROM secretary_task_alignment_cases WHERE id = ?",
            (agreement["id"],),
        ).fetchone()
        stored_task = connection.execute(
            "SELECT * FROM secretary_business_tasks WHERE id = ?", (task["id"],)
        ).fetchone()
        decision_count = connection.execute(
            "SELECT COUNT(*) FROM secretary_task_alignment_decisions WHERE case_id = ?",
            (agreement["id"],),
        ).fetchone()[0]
        idempotency_count = connection.execute(
            """
            SELECT COUNT(*) FROM secretary_workspace_idempotency
            WHERE operation = ? AND idempotency_key = ?
            """,
            (
                f"task_agreement.respond:{agreement['id']}",
                "p1ba-atomic-response",
            ),
        ).fetchone()[0]
    assert case["status"] == "pending"
    assert case["version"] == agreement["version"]
    assert stored_task["stage"] == "issued"
    assert stored_task["version"] == task["version"]
    assert decision_count == 0
    assert idempotency_count == 0
