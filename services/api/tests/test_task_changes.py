from __future__ import annotations

import html
import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from centaur_pocket.database import Database
from centaur_pocket.service import PocketError
from centaur_pocket.workspace.service import (
    TASK_CHANGE_DOCUMENT_KEYS,
    TASK_CHANGE_V6_INDEXES,
    TASK_CHANGE_V6_TABLES,
    TASK_CHANGE_V6_TRIGGERS,
    WORKSPACE_SCHEMA,
    WorkspaceService,
    _canonical_task_change_json,
)

WORKSPACE_ID = "ws_default"
WORKSPACE_PATH = f"/api/v1/workspaces/{WORKSPACE_ID}"
OWNER_ID = "member_owner"


def _headers(
    owner_headers: dict[str, str],
    key: str,
    *,
    device_id: str = "task-change-owner-device",
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


def _task_payload(
    title: str,
    *,
    assignee_member_id: str = OWNER_ID,
) -> dict[str, Any]:
    return {
        "domain": "work",
        "title": title,
        "purpose": "形成可验证价值",
        "objective": "按期完成交付",
        "strategy": "先验证，再分批实施。",
        "key_points": ["资源", "回滚"],
        "acceptance_criteria": ["记录完整", "结果通过"],
        "issuer_member_id": OWNER_ID,
        "assignee_member_id": assignee_member_id,
        "acceptance_owner_id": OWNER_ID,
        "priority": "high",
        "tier": "strategic",
        "health": "on_track",
        "due_at": "2026-08-20T18:00:00+08:00",
    }


def _create_member(
    client: TestClient,
    owner_headers: dict[str, str],
    suffix: str,
) -> dict[str, Any]:
    response = client.post(
        f"{WORKSPACE_PATH}/members",
        headers=_headers(owner_headers, f"change-member-{suffix}"),
        json={
            "kind": "external",
            "role": "member",
            "display_name": f"承办人-{suffix}",
            "contact_ref": f"wecom://change/{suffix}",
            "client_mutation_id": f"change-member-local-{suffix}",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_task(
    client: TestClient,
    owner_headers: dict[str, str],
    suffix: str,
    *,
    assignee_member_id: str = OWNER_ID,
    issue: bool = False,
) -> dict[str, Any]:
    created = client.post(
        f"{WORKSPACE_PATH}/tasks",
        headers=_headers(owner_headers, f"change-task-{suffix}"),
        json=_task_payload(
            f"任务-{suffix}", assignee_member_id=assignee_member_id
        ),
    )
    assert created.status_code == 201, created.text
    task = created.json()
    if issue:
        issued = client.post(
            f"{WORKSPACE_PATH}/tasks/{task['id']}/transitions",
            headers=_headers(owner_headers, f"change-issue-{suffix}"),
            json={"target_stage": "issued", "expected_version": task["version"]},
        )
        assert issued.status_code == 200, issued.text
        task = issued.json()
    return task


def _propose(
    client: TestClient,
    owner_headers: dict[str, str],
    task: dict[str, Any],
    suffix: str,
    *,
    change_type: str = "due_at",
    patch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/changes",
        headers=_headers(owner_headers, f"change-propose-{suffix}"),
        json={
            "change_type": change_type,
            "base_version": task["version"],
            "reason": f"变更原因-{suffix}",
            "patch": patch or {"due_at": "2026-08-30T18:00:00+08:00"},
            "client_mutation_id": f"change-proposal-local-{suffix}",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _owner_protocol(
    client: TestClient,
    owner_headers: dict[str, str],
    change_id: str,
) -> dict[str, Any]:
    response = client.get(
        f"/api/v1/task-changes/{change_id}", headers=owner_headers
    )
    assert response.status_code == 200, response.text
    assert response.headers["etag"] == f'"{response.json()["version"]}"'
    return response.json()


def _exchange(
    client: TestClient,
    invitation: dict[str, Any],
    suffix: str,
    *,
    device_id: str | None = None,
) -> dict[str, Any]:
    device = device_id or f"change-assignee-device-{suffix}"
    response = client.post(
        "/api/v1/task-changes/exchange",
        headers={"Idempotency-Key": f"change-exchange-{suffix}"},
        json={
            "invitation_id": invitation["invitation_id"],
            "code": invitation["code"],
            "client_device_id": device,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _session_headers(
    exchange: dict[str, Any],
    key: str,
    version: int,
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {exchange['access_token']}",
        "X-Device-ID": exchange["session"]["client_device_id"],
        "Idempotency-Key": key,
        "If-Match": f'"{version}"',
    }


def _decision_body(
    protocol: dict[str, Any],
    decision: str,
    mutation_id: str,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "expected_change_version": protocol["version"],
        "expected_task_version": protocol["task"]["version"],
        "proposal_digest": protocol["proposal"]["digest"],
        "decision": decision,
        "reason": reason,
        "client_mutation_id": mutation_id,
    }


def test_self_managed_change_requires_explicit_protocol_decision(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    task = _create_task(client, owner_headers, "self")
    proposed = _propose(client, owner_headers, task, "self")
    assert proposed["patch"]["due_at"] == "2026-08-30T10:00:00Z"
    assert proposed["proposal_digest"].startswith("sha256:")

    protocol = _owner_protocol(client, owner_headers, proposed["id"])
    assert protocol["status"] == "proposed"
    assert protocol["actionable"] is True
    assert protocol["proposer_member_id"] == OWNER_ID
    assert protocol["responder_member_id"] == OWNER_ID
    assert set(protocol["proposal"]["document"]) == TASK_CHANGE_DOCUMENT_KEYS

    old_owner_accept = client.post(
        f"{WORKSPACE_PATH}/changes/{proposed['id']}/decision",
        headers=_headers(owner_headers, "old-owner-accept-self"),
        json={"decision": "accept", "expected_version": task["version"]},
    )
    assert old_owner_accept.status_code == 409
    assert "双方确认协议" in old_owner_accept.text

    accepted = client.post(
        f"/api/v1/task-changes/{proposed['id']}/decisions",
        headers=_headers(
            owner_headers,
            "protocol-owner-accept-self",
            version=protocol["version"],
        ),
        json=_decision_body(
            protocol, "accept", "protocol-owner-accept-self-mutation"
        ),
    )
    assert accepted.status_code == 200, accepted.text
    result = accepted.json()
    assert accepted.headers["etag"] == '"2"'
    assert result["change"]["status"] == "accepted"
    assert result["decision"]["actor_member_id"] == OWNER_ID
    assert result["decision"]["assurance_method"] == "owner_token"
    assert result["task"]["due_at"] == "2026-08-30T10:00:00Z"
    assert result["task"]["version"] == task["version"] + 1

    replay = client.post(
        f"/api/v1/task-changes/{proposed['id']}/decisions",
        headers=_headers(
            owner_headers,
            "protocol-owner-accept-self",
            version=protocol["version"],
        ),
        json=_decision_body(
            protocol, "accept", "protocol-owner-accept-self-mutation"
        ),
    )
    assert replay.status_code == 200
    assert replay.json() == result


def test_external_change_invitation_session_scope_and_accept(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    assignee = _create_member(client, owner_headers, "external")
    task = _create_task(
        client,
        owner_headers,
        "external",
        assignee_member_id=assignee["id"],
        issue=True,
    )
    proposed = _propose(client, owner_headers, task, "external")
    protocol = _owner_protocol(client, owner_headers, proposed["id"])

    owner_cannot_accept = client.post(
        f"/api/v1/task-changes/{proposed['id']}/decisions",
        headers=_headers(
            owner_headers,
            "external-owner-cannot-accept",
            version=protocol["version"],
        ),
        json=_decision_body(
            protocol, "accept", "external-owner-cannot-accept-mutation"
        ),
    )
    assert owner_cannot_accept.status_code == 403

    invitation = client.post(
        f"{WORKSPACE_PATH}/changes/{proposed['id']}/invitations",
        headers=_headers(
            owner_headers,
            "external-change-invitation",
            version=protocol["version"],
        ),
        json={
            "expected_change_version": protocol["version"],
            "expected_task_version": task["version"],
        },
    )
    assert invitation.status_code == 201, invitation.text
    invitation_body = invitation.json()
    assert invitation_body["confirmation_path"] == (
        f"/api/v1/task-change-invitations/{invitation_body['invitation_id']}"
    )
    assert invitation_body["code"] not in client.get(
        invitation_body["confirmation_path"]
    ).text
    shell = client.get(invitation_body["confirmation_path"])
    assert shell.status_code == 200
    assert shell.headers["cache-control"].startswith("no-store")
    assert task["title"] not in shell.text
    assert proposed["reason"] not in shell.text
    assert "<script" not in shell.text

    exchange = _exchange(client, invitation_body, "external")
    assert exchange["access_token"].startswith("cp_task_ch_")
    session_headers = _session_headers(
        exchange, "external-change-accept", protocol["version"]
    )
    scoped_get = client.get(
        f"/api/v1/task-changes/{proposed['id']}",
        headers={
            "Authorization": session_headers["Authorization"],
            "X-Device-ID": session_headers["X-Device-ID"],
        },
    )
    assert scoped_get.status_code == 200, scoped_get.text
    assert "origin_memo_id" not in scoped_get.text
    assert '"source"' not in scoped_get.text
    assert '"evidence"' not in scoped_get.text

    wrong_device = client.get(
        f"/api/v1/task-changes/{proposed['id']}",
        headers={
            "Authorization": session_headers["Authorization"],
            "X-Device-ID": "wrong-change-device",
        },
    )
    assert wrong_device.status_code == 403

    accepted = client.post(
        f"/api/v1/task-changes/{proposed['id']}/decisions",
        headers=session_headers,
        json=_decision_body(
            exchange["change"],
            "accept",
            "external-change-accept-mutation",
        ),
    )
    assert accepted.status_code == 200, accepted.text
    result = accepted.json()
    assert result["decision"]["actor_member_id"] == assignee["id"]
    assert result["decision"]["assurance_method"] == "task_change_session"
    assert result["task"]["due_at"] == "2026-08-30T10:00:00Z"

    exact_replay = client.post(
        f"/api/v1/task-changes/{proposed['id']}/decisions",
        headers=session_headers,
        json=_decision_body(
            exchange["change"],
            "accept",
            "external-change-accept-mutation",
        ),
    )
    assert exact_replay.status_code == 200, exact_replay.text
    assert exact_replay.json() == result

    with client.app.state.workspace_service.database.connect() as connection:
        dump = "\n".join(connection.iterdump())
    assert invitation_body["code"] not in dump
    assert exchange["access_token"] not in dump


def test_external_browser_flow_is_no_script_secret_scoped_and_replayable(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    assignee = _create_member(client, owner_headers, "browser")
    task = _create_task(
        client,
        owner_headers,
        "browser",
        assignee_member_id=assignee["id"],
        issue=True,
    )
    proposed = _propose(client, owner_headers, task, "browser")
    protocol = _owner_protocol(client, owner_headers, proposed["id"])
    invitation_response = client.post(
        f"{WORKSPACE_PATH}/changes/{proposed['id']}/invitations",
        headers=_headers(
            owner_headers,
            "browser-change-invitation",
            version=protocol["version"],
        ),
        json={
            "expected_change_version": protocol["version"],
            "expected_task_version": task["version"],
        },
    )
    assert invitation_response.status_code == 201, invitation_response.text
    invitation = invitation_response.json()
    shell = client.get(invitation["confirmation_path"])
    hidden = {
        name: html.unescape(value)
        for name, value in re.findall(
            r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"', shell.text
        )
    }
    preview_form = {
        "code": invitation["code"],
        "client_device_id": hidden["client_device_id"],
        "exchange_idempotency_key": hidden["exchange_idempotency_key"],
    }
    preview_path = f"{invitation['confirmation_path']}/preview"
    preview = client.post(preview_path, data=preview_form)
    assert preview.status_code == 200, preview.text
    assert task["title"] in preview.text
    assert proposed["proposal_digest"] in preview.text
    assert "localStorage" not in preview.text
    assert "sessionStorage" not in preview.text
    assert "<script" not in preview.text
    assert "cp_task_ch_" in preview.text
    assert "cp_task_ch_" not in str(preview.request.url)

    exact_preview_replay = client.post(preview_path, data=preview_form)
    assert exact_preview_replay.status_code == 200, exact_preview_replay.text

    accept_form_match = re.search(
        r'<form method="post" action="[^"]+/decide">(.*?)</form>',
        preview.text,
        re.DOTALL,
    )
    assert accept_form_match is not None
    accept_form = {
        name: html.unescape(value)
        for name, value in re.findall(
            r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"',
            accept_form_match.group(1),
        )
    }
    assert accept_form["decision"] == "accept"
    decided = client.post(
        f"{invitation['confirmation_path']}/decide", data=accept_form
    )
    assert decided.status_code == 200, decided.text
    assert "已接受" in decided.text
    assert accept_form["access_token"] not in decided.text

    exact_decision_replay = client.post(
        f"{invitation['confirmation_path']}/decide", data=accept_form
    )
    assert exact_decision_replay.status_code == 200, exact_decision_replay.text
    assert "已接受" in exact_decision_replay.text


def test_assignee_change_is_confirmed_by_current_assignee_then_realigns(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    current_assignee = _create_member(client, owner_headers, "current")
    next_assignee = _create_member(client, owner_headers, "next")
    task = _create_task(
        client,
        owner_headers,
        "assignee",
        assignee_member_id=current_assignee["id"],
        issue=True,
    )
    proposed = _propose(
        client,
        owner_headers,
        task,
        "assignee",
        change_type="assignee",
        patch={"assignee_member_id": next_assignee["id"]},
    )
    protocol = _owner_protocol(client, owner_headers, proposed["id"])
    assert protocol["responder_member_id"] == current_assignee["id"]
    invitation = client.post(
        f"{WORKSPACE_PATH}/changes/{proposed['id']}/invitations",
        headers=_headers(
            owner_headers,
            "assignee-change-invitation",
            version=protocol["version"],
        ),
        json={
            "expected_change_version": protocol["version"],
            "expected_task_version": task["version"],
        },
    )
    assert invitation.status_code == 201, invitation.text
    exchange = _exchange(client, invitation.json(), "assignee")
    accepted = client.post(
        f"/api/v1/task-changes/{proposed['id']}/decisions",
        headers=_session_headers(
            exchange, "assignee-change-accept", protocol["version"]
        ),
        json=_decision_body(
            exchange["change"],
            "accept",
            "assignee-change-accept-mutation",
        ),
    )
    assert accepted.status_code == 200, accepted.text
    changed_task = accepted.json()["task"]
    assert changed_task["assignee_member_id"] == next_assignee["id"]
    assert changed_task["stage"] == "issued"

    alignment_invitation = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/alignment-invitations",
        headers=_headers(owner_headers, "new-assignee-alignment-invitation"),
        json={"expected_version": changed_task["version"]},
    )
    assert alignment_invitation.status_code == 201, alignment_invitation.text
    assert (
        alignment_invitation.json()["assignee_member_id"] == next_assignee["id"]
    )


def test_external_exchange_and_decision_are_exactly_once_under_concurrency(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    assignee = _create_member(client, owner_headers, "concurrent")
    task = _create_task(
        client,
        owner_headers,
        "concurrent",
        assignee_member_id=assignee["id"],
        issue=True,
    )
    proposed = _propose(client, owner_headers, task, "concurrent")
    protocol = _owner_protocol(client, owner_headers, proposed["id"])
    invitation_response = client.post(
        f"{WORKSPACE_PATH}/changes/{proposed['id']}/invitations",
        headers=_headers(
            owner_headers,
            "concurrent-change-invitation",
            version=protocol["version"],
        ),
        json={
            "expected_change_version": protocol["version"],
            "expected_task_version": task["version"],
        },
    )
    assert invitation_response.status_code == 201, invitation_response.text
    invitation = invitation_response.json()
    exchange_payload = {
        "invitation_id": invitation["invitation_id"],
        "code": invitation["code"],
        "client_device_id": "change-concurrent-device",
    }

    def exchange_once() -> Any:
        return client.post(
            "/api/v1/task-changes/exchange",
            headers={"Idempotency-Key": "change-concurrent-exchange"},
            json=exchange_payload,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        exchanges = [
            future.result()
            for future in [
                executor.submit(exchange_once),
                executor.submit(exchange_once),
            ]
        ]
    assert [response.status_code for response in exchanges] == [200, 200]
    assert exchanges[0].json() == exchanges[1].json()
    exchange = exchanges[0].json()
    decision_headers = _session_headers(
        exchange, "change-concurrent-decision", protocol["version"]
    )
    decision_payload = _decision_body(
        exchange["change"],
        "accept",
        "change-concurrent-decision-mutation",
    )

    def decide_once() -> Any:
        return client.post(
            f"/api/v1/task-changes/{proposed['id']}/decisions",
            headers=decision_headers,
            json=decision_payload,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        decisions = [
            future.result()
            for future in [
                executor.submit(decide_once),
                executor.submit(decide_once),
            ]
        ]
    assert [response.status_code for response in decisions] == [200, 200]
    assert decisions[0].json() == decisions[1].json()
    with client.app.state.workspace_service.database.connect() as connection:
        decision_count = connection.execute(
            """
            SELECT COUNT(*) FROM secretary_task_change_decisions
            WHERE change_id = ?
            """,
            (proposed["id"],),
        ).fetchone()[0]
        persisted_task = connection.execute(
            "SELECT version, due_at FROM secretary_business_tasks WHERE id = ?",
            (task["id"],),
        ).fetchone()
    assert decision_count == 1
    assert persisted_task["version"] == task["version"] + 1
    assert persisted_task["due_at"] == "2026-08-30T10:00:00Z"


def test_v6_schema_and_bound_records_fail_closed(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    task = _create_task(client, owner_headers, "immutable")
    proposed = _propose(client, owner_headers, task, "immutable")
    service = client.app.state.workspace_service
    with service.database.connect() as connection:
        objects = {
            (row["type"], row["name"])
            for row in connection.execute(
                """
                SELECT type, name FROM sqlite_master
                WHERE name LIKE 'secretary_task_change_%'
                   OR name LIKE 'idx_secretary_%change%'
                   OR name LIKE 'trg_secretary_%change%'
                """
            ).fetchall()
        }
    for name in TASK_CHANGE_V6_TABLES:
        assert ("table", name) in objects
    for name in TASK_CHANGE_V6_INDEXES:
        assert ("index", name) in objects
    for name in TASK_CHANGE_V6_TRIGGERS:
        assert ("trigger", name) in objects

    with (
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
        service.database.transaction() as connection,
    ):
        connection.execute(
            """
            UPDATE secretary_task_changes SET base_version = base_version + 1
            WHERE id = ?
            """,
            (proposed["id"],),
        )
    with (
        pytest.raises(sqlite3.IntegrityError, match="state transition"),
        service.database.transaction() as connection,
    ):
        connection.execute(
            """
            UPDATE secretary_task_changes
            SET status = 'accepted', decided_by = ?, decided_at = ?,
                version = version + 1
            WHERE id = ?
            """,
            (OWNER_ID, "2026-08-02T00:00:00Z", proposed["id"]),
        )
    with (
        pytest.raises(sqlite3.IntegrityError, match="decision binding"),
        service.database.transaction() as connection,
    ):
        connection.execute(
            """
            INSERT INTO secretary_task_change_decisions(
                id, change_id, proposal_digest, action, actor_member_id,
                assurance_method, client_mutation_id, created_at
            ) VALUES (
                'forged-decision', ?, ?, 'accept', ?, 'owner_token',
                'forged-decision-mutation', '2026-08-02T00:00:00Z'
            )
            """,
            (proposed["id"], "sha256:" + "0" * 64, OWNER_ID),
        )


def test_task_change_decision_rolls_back_atomically_when_audit_append_fails(
    client: TestClient,
    owner_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _create_task(client, owner_headers, "rollback")
    proposed = _propose(client, owner_headers, task, "rollback")
    protocol = _owner_protocol(client, owner_headers, proposed["id"])
    service = client.app.state.workspace_service
    original_append = service._append_event

    def failing_append(*args: Any, **kwargs: Any) -> int:
        if kwargs.get("event_type") == "task.change_accepted":
            raise RuntimeError("forced task change audit failure")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(service, "_append_event", failing_append)
    with pytest.raises(RuntimeError, match="forced task change audit failure"):
        client.post(
            f"/api/v1/task-changes/{proposed['id']}/decisions",
            headers=_headers(
                owner_headers,
                "change-rollback-decision",
                version=protocol["version"],
            ),
            json=_decision_body(
                protocol,
                "accept",
                "change-rollback-decision-mutation",
            ),
        )
    with service.database.connect() as connection:
        persisted_change = connection.execute(
            "SELECT status, version FROM secretary_task_changes WHERE id = ?",
            (proposed["id"],),
        ).fetchone()
        persisted_task = connection.execute(
            "SELECT version, due_at FROM secretary_business_tasks WHERE id = ?",
            (task["id"],),
        ).fetchone()
        decisions = connection.execute(
            """
            SELECT COUNT(*) FROM secretary_task_change_decisions
            WHERE change_id = ?
            """,
            (proposed["id"],),
        ).fetchone()[0]
        cached = connection.execute(
            """
            SELECT COUNT(*) FROM secretary_workspace_idempotency
            WHERE operation = ? AND idempotency_key = ?
            """,
            (
                f"task_change.respond:{proposed['id']}",
                "change-rollback-decision",
            ),
        ).fetchone()[0]
    assert tuple(persisted_change) == ("proposed", 1)
    assert persisted_task["version"] == task["version"]
    assert persisted_task["due_at"] == task["due_at"]
    assert decisions == 0
    assert cached == 0


@pytest.mark.parametrize(
    ("change_type", "before", "patch"),
    [
        ("assignee", "member_owner", {"assignee_member_id": True}),
        (
            "acceptance_criteria",
            ["旧标准"],
            {"acceptance_criteria": "not-a-list"},
        ),
        ("abnormal_close", None, {"abnormal_close_reason": None}),
    ],
)
def test_task_change_canonicalizer_rejects_invalid_typed_values(
    change_type: str,
    before: Any,
    patch: dict[str, Any],
) -> None:
    document = {
        "schema": "centaur.task-change.v1",
        "workspace_id": WORKSPACE_ID,
        "task_id": "task-canonical",
        "change_id": "change-canonical",
        "change_type": change_type,
        "base_task_version": 1,
        "proposer_role": "issuer",
        "proposer_member_id": OWNER_ID,
        "responder_role": "issuer",
        "responder_member_id": OWNER_ID,
        "before": before,
        "patch": patch,
        "reason": "规范化测试",
    }
    with pytest.raises(PocketError):
        _canonical_task_change_json(document)


def test_task_change_routes_reject_query_and_wrong_scope(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    task = _create_task(client, owner_headers, "strict")
    proposed = _propose(client, owner_headers, task, "strict")
    query = client.get(
        f"/api/v1/task-changes/{proposed['id']}?leak=1",
        headers=owner_headers,
    )
    assert query.status_code == 400
    wrong_scope = client.get(
        f"/api/v1/task-changes/{proposed['id']}",
        headers={
            "Authorization": "Bearer cp_task_at_wrong-scope",
            "X-Device-ID": "wrong-scope-device",
        },
    )
    assert wrong_scope.status_code == 401
    assert re.fullmatch(r'\{"detail":"[^\"]+"\}', wrong_scope.text)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/task-changes/exchange",
        "/api/v1/task-changes/change-body-limit/decisions",
    ],
)
def test_task_change_secret_json_body_limit_is_applied_before_validation(
    client: TestClient,
    path: str,
) -> None:
    marker = "TASK_CHANGE_OVERSIZED_SECRET"
    body = json.dumps({"secret": marker * 400_000}).encode("utf-8")
    response = client.post(
        path,
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": "task-change-body-limit",
            "X-Device-ID": "task-change-body-limit-device",
        },
        content=body,
    )
    assert response.status_code == 413
    assert response.json() == {"detail": "请求体过大"}
    assert marker not in response.text
    assert response.headers["cache-control"].startswith("no-store")


def _marked_v5_service(path: Path) -> WorkspaceService:
    database = Database(path)
    with database.connect() as connection:
        connection.executescript(WORKSPACE_SCHEMA)
        connection.executemany(
            """
            INSERT INTO secretary_workspace_schema_migrations(version, applied_at)
            VALUES (?, '2026-08-01T00:00:00Z')
            """,
            [(version,) for version in range(1, 6)],
        )
    return WorkspaceService(database, task_session_hmac_key=b"v" * 32)


def test_clean_v5_to_v6_migration_is_structurally_verified_and_replayable(
    tmp_path: Path,
) -> None:
    service = _marked_v5_service(tmp_path / "clean-v5.db")
    service.initialize()
    with service.database.connect() as connection:
        marker_before = connection.execute(
            """
            SELECT applied_at FROM secretary_workspace_schema_migrations
            WHERE version = 6
            """
        ).fetchone()[0]
        objects_before = [
            tuple(row)
            for row in connection.execute(
                """
                SELECT type, name, sql FROM sqlite_master
                WHERE name LIKE 'secretary_task_change_%'
                   OR name LIKE 'idx_secretary_%change%'
                   OR name LIKE 'trg_secretary_%change%'
                ORDER BY type, name
                """
            ).fetchall()
        ]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    service.initialize()
    with service.database.connect() as connection:
        marker_after = connection.execute(
            """
            SELECT applied_at FROM secretary_workspace_schema_migrations
            WHERE version = 6
            """
        ).fetchone()[0]
        objects_after = [
            tuple(row)
            for row in connection.execute(
                """
                SELECT type, name, sql FROM sqlite_master
                WHERE name LIKE 'secretary_task_change_%'
                   OR name LIKE 'idx_secretary_%change%'
                   OR name LIKE 'trg_secretary_%change%'
                ORDER BY type, name
                """
            ).fetchall()
        ]
    assert marker_after == marker_before
    assert objects_after == objects_before


def test_v6_migration_rejects_same_name_weak_table_without_marker(
    tmp_path: Path,
) -> None:
    service = _marked_v5_service(tmp_path / "weak-collision-v5.db")
    with service.database.connect() as connection:
        connection.execute(
            """
            CREATE TABLE secretary_task_change_proposals(
                change_id TEXT, workspace_id TEXT, task_id TEXT,
                proposer_member_id TEXT, responder_member_id TEXT,
                base_task_version INTEGER, digest TEXT, canonical_json TEXT,
                created_at TEXT
            )
            """
        )
    with pytest.raises(RuntimeError, match="同名对象"):
        service.initialize()
    with service.database.connect() as connection:
        assert connection.execute(
            """
            SELECT 1 FROM secretary_workspace_schema_migrations WHERE version = 6
            """
        ).fetchone() is None
        assert connection.execute(
            "PRAGMA table_info(secretary_task_change_proposals)"
        ).fetchone()["pk"] == 0


def test_v6_marker_without_protocol_objects_fails_startup(
    tmp_path: Path,
) -> None:
    service = _marked_v5_service(tmp_path / "forged-marker-v6.db")
    with service.database.connect() as connection:
        connection.execute(
            """
            INSERT INTO secretary_workspace_schema_migrations(version, applied_at)
            VALUES (6, '2026-08-01T00:00:00Z')
            """
        )
    with pytest.raises(RuntimeError, match="schema 对象缺失"):
        service.initialize()
