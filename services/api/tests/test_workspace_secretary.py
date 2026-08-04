from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from centaur_pocket.database import Database
from centaur_pocket.workspace.service import WorkspaceService

WORKSPACE_PATH = "/api/v1/workspaces/ws_default"
DEVICE_ID = "pytest-secretary-device"
OWNER_MEMBER_ID = "member_owner"
ALIGNMENT_PATH = "/api/v1/task-alignments"


def write_headers(
    auth_headers: dict[str, str],
    idempotency_key: str,
    *,
    if_match: int | None = None,
) -> dict[str, str]:
    headers = {
        **auth_headers,
        "Idempotency-Key": idempotency_key,
        "X-Device-ID": DEVICE_ID,
    }
    if if_match is not None:
        headers["If-Match"] = f'"{if_match}"'
    return headers


def memo_payload(*, title: str = "记录合同复核事项") -> dict[str, Any]:
    return {
        "record_type": "note",
        "domain": "work",
        "horizon": "short_term",
        "urgency": "high",
        "title": title,
        "content": "复核退出条款，并保留给主人确认的结论。",
        "due_at": "2026-08-03T10:00:00+08:00",
        "source": {
            "source_kind": "manual",
            "authority": "user_provided",
        },
    }


def member_payload(*, display_name: str = "交付协作组") -> dict[str, Any]:
    return {
        "kind": "team",
        "role": "member",
        "display_name": display_name,
        "contact_ref": "wecom://delivery-team",
        "client_mutation_id": "member-client-mutation-001",
    }


def task_payload(
    *,
    title: str = "完成合同复核",
    due_at: str = "2026-08-05T10:00:00+08:00",
    with_steps: bool = False,
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    if with_steps:
        steps = [
            {
                "step_type": "action",
                "title": "核对退出条款",
                "assignee_member_id": OWNER_MEMBER_ID,
                "position": 0,
            },
            {
                "step_type": "milestone",
                "title": "提交复核结论",
                "assignee_member_id": OWNER_MEMBER_ID,
                "position": 1,
            },
        ]
    return {
        "domain": "work",
        "title": title,
        "purpose": "降低合同执行风险",
        "objective": "形成可验收的合同复核结论",
        "strategy": "逐条核对关键条款并提交带引用的结论。",
        "key_points": ["退出条款", "违约责任"],
        "acceptance_criteria": ["关键条款均有明确结论", "结论包含原文引用"],
        "issuer_member_id": OWNER_MEMBER_ID,
        "assignee_member_id": OWNER_MEMBER_ID,
        "acceptance_owner_id": OWNER_MEMBER_ID,
        "priority": "high",
        "health": "on_track",
        "due_at": due_at,
        "steps": steps,
        "source": {
            "source_kind": "manual",
            "authority": "user_provided",
        },
    }


def create_task(
    client: TestClient,
    owner_headers: dict[str, str],
    *,
    idempotency_key: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    response = client.post(
        f"{WORKSPACE_PATH}/tasks",
        headers=write_headers(owner_headers, idempotency_key),
        json=payload,
    )
    assert response.status_code == 201, response.text
    return response.json()


def transition_task(
    client: TestClient,
    owner_headers: dict[str, str],
    task: dict[str, Any],
    target_stage: str,
    idempotency_key: str,
) -> dict[str, Any]:
    response = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/transitions",
        headers=write_headers(owner_headers, idempotency_key),
        json={
            "target_stage": target_stage,
            "expected_version": task["version"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def create_external_issued_task(
    client: TestClient,
    owner_headers: dict[str, str],
    *,
    suffix: str,
) -> tuple[dict[str, Any], str]:
    assignee_id = f"member-alignment-{suffix}"
    with client.app.state.workspace_service.database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO secretary_workspace_members(
                id, workspace_id, kind, role, display_name, active,
                created_at, updated_at
            ) VALUES (?, 'ws_default', 'person', 'member', ?, 1, ?, ?)
            """,
            (
                assignee_id,
                f"执行负责人-{suffix}",
                "2026-08-02T00:00:00Z",
                "2026-08-02T00:00:00Z",
            ),
        )
    payload = task_payload(title=f"跨团队交付任务-{suffix}")
    payload["assignee_member_id"] = assignee_id
    task = create_task(
        client,
        owner_headers,
        idempotency_key=f"align-task-create-{suffix}",
        payload=payload,
    )
    task = transition_task(
        client,
        owner_headers,
        task,
        "issued",
        f"align-task-issue-{suffix}",
    )
    assert task["stage"] == "issued"
    return task, assignee_id


def create_alignment_invitation(
    client: TestClient,
    owner_headers: dict[str, str],
    task: dict[str, Any],
    *,
    suffix: str,
) -> dict[str, Any]:
    response = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/alignment-invitations",
        headers=write_headers(owner_headers, f"alignment-invite-{suffix}"),
        json={"expected_version": task["version"]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_owner_agent_boundary_and_empty_bootstrap(
    client: TestClient,
    owner_headers: dict[str, str],
    agent_headers: dict[str, str],
) -> None:
    assert client.get(f"{WORKSPACE_PATH}/bootstrap").status_code == 401
    assert (
        client.get(f"{WORKSPACE_PATH}/bootstrap", headers=agent_headers).status_code
        == 401
    )

    workspace = client.get(WORKSPACE_PATH, headers=owner_headers)
    assert workspace.status_code == 200, workspace.text
    assert workspace.json()["current_member_id"] == OWNER_MEMBER_ID
    assert workspace.json()["role"] == "owner"
    assert workspace.json()["members"] == [
        {
            "id": OWNER_MEMBER_ID,
            "workspace_id": "ws_default",
            "kind": "person",
            "role": "owner",
            "display_name": "主人",
            "contact_ref": None,
            "active": True,
            "version": 1,
            "created_at": workspace.json()["members"][0]["created_at"],
            "updated_at": workspace.json()["members"][0]["updated_at"],
        }
    ]

    bootstrap = client.get(f"{WORKSPACE_PATH}/bootstrap", headers=owner_headers)
    assert bootstrap.status_code == 200, bootstrap.text
    assert bootstrap.json()["workspace"]["id"] == "ws_default"
    assert bootstrap.json()["current_member_id"] == OWNER_MEMBER_ID
    assert bootstrap.json()["cursor"] == 0
    assert bootstrap.json()["memos"] == []
    assert bootstrap.json()["tasks"] == []
    assert bootstrap.json()["calendar"] == []
    assert bootstrap.json()["meetings"] == []

    rejected_write = client.post(
        f"{WORKSPACE_PATH}/memos",
        headers=write_headers(agent_headers, "agent-memo-create-001"),
        json=memo_payload(),
    )
    assert rejected_write.status_code == 401
    assert (
        client.get(f"{WORKSPACE_PATH}/bootstrap", headers=owner_headers).json()[
            "cursor"
        ]
        == 0
    )


def test_workspace_member_create_contract_idempotency_and_audit(
    client: TestClient,
    owner_headers: dict[str, str],
    agent_headers: dict[str, str],
) -> None:
    path = f"{WORKSPACE_PATH}/members"
    payload = member_payload()
    valid_headers = write_headers(owner_headers, "workspace-member-create-001")

    assert (
        client.post(
            path,
            headers={
                "Idempotency-Key": "workspace-member-no-auth-001",
                "X-Device-ID": DEVICE_ID,
            },
            json=payload,
        ).status_code
        == 401
    )
    assert (
        client.post(
            path,
            headers=write_headers(agent_headers, "workspace-member-agent-001"),
            json=payload,
        ).status_code
        == 401
    )
    assert (
        client.post(
            path,
            headers={**owner_headers, "Idempotency-Key": "workspace-member-no-device"},
            json=payload,
        ).status_code
        == 422
    )
    assert (
        client.post(
            path,
            headers={**owner_headers, "X-Device-ID": DEVICE_ID},
            json=payload,
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"{path}?unexpected=1", headers=valid_headers, json=payload
        ).status_code
        == 400
    )

    invalid_payloads = [
        {**payload, "kind": "robot"},
        {**payload, "role": "owner"},
        {**payload, "display_name": "   "},
        {**payload, "display_name": "名" * 501},
        {**payload, "contact_ref": "   "},
        {**payload, "contact_ref": "x" * 2_001},
        {**payload, "unexpected": True},
    ]
    for index, invalid_payload in enumerate(invalid_payloads):
        invalid = client.post(
            path,
            headers=write_headers(
                owner_headers, f"workspace-member-invalid-{index:03d}"
            ),
            json=invalid_payload,
        )
        assert invalid.status_code == 422, invalid.text

    created = client.post(path, headers=valid_headers, json=payload)
    assert created.status_code == 201, created.text
    member = created.json()
    assert created.headers["etag"] == '"1"'
    assert set(member) == {
        "id",
        "workspace_id",
        "kind",
        "role",
        "display_name",
        "contact_ref",
        "active",
        "version",
        "created_at",
        "updated_at",
    }
    assert member == {
        "id": member["id"],
        "workspace_id": "ws_default",
        "kind": "team",
        "role": "member",
        "display_name": "交付协作组",
        "contact_ref": "wecom://delivery-team",
        "active": True,
        "version": 1,
        "created_at": member["created_at"],
        "updated_at": member["updated_at"],
    }

    replay = client.post(path, headers=valid_headers, json=payload)
    assert replay.status_code == 201, replay.text
    assert replay.json() == member
    conflict = client.post(
        path,
        headers=valid_headers,
        json={**payload, "display_name": "另一个成员"},
    )
    assert conflict.status_code == 409

    workspace = client.get(WORKSPACE_PATH, headers=owner_headers)
    assert workspace.status_code == 200, workspace.text
    listed = next(
        item for item in workspace.json()["members"] if item["id"] == member["id"]
    )
    assert listed == member

    with client.app.state.workspace_service.database.connect() as connection:
        stored_members = connection.execute(
            """
            SELECT COUNT(*) FROM secretary_workspace_members
            WHERE workspace_id = ? AND id != ?
            """,
            ("ws_default", OWNER_MEMBER_ID),
        ).fetchone()[0]
        idempotency_rows = connection.execute(
            """
            SELECT COUNT(*) FROM secretary_workspace_idempotency
            WHERE workspace_id = ? AND actor_id = ?
              AND operation = 'workspace_member.create'
              AND idempotency_key = ? AND status_code = 201
            """,
            ("ws_default", OWNER_MEMBER_ID, "workspace-member-create-001"),
        ).fetchone()[0]
        events = connection.execute(
            """
            SELECT aggregate_type, aggregate_id, aggregate_version,
                   event_type, operation, actor_member_id, device_id, payload_json
            FROM secretary_workspace_events
            WHERE aggregate_type = 'workspace_member' AND aggregate_id = ?
            """,
            (member["id"],),
        ).fetchall()
    assert stored_members == 1
    assert idempotency_rows == 1
    assert len(events) == 1
    event = events[0]
    assert event["aggregate_type"] == "workspace_member"
    assert event["aggregate_id"] == member["id"]
    assert event["aggregate_version"] == 1
    assert event["event_type"] == "workspace.member_created"
    assert event["operation"] == "upsert"
    assert event["actor_member_id"] == OWNER_MEMBER_ID
    assert event["device_id"] == DEVICE_ID
    assert json.loads(event["payload_json"]) == member


def test_workspace_member_v3_migration_is_legacy_safe_and_replayable(
    client: TestClient,
) -> None:
    service = client.app.state.workspace_service
    with service.database.transaction() as connection:
        owner_before = dict(
            connection.execute(
                """
                SELECT id, workspace_id, kind, role, display_name, contact_ref,
                       active, created_at, updated_at
                FROM secretary_workspace_members WHERE id = ?
                """,
                (OWNER_MEMBER_ID,),
            ).fetchone()
        )
        connection.execute(
            "ALTER TABLE secretary_workspace_members DROP COLUMN version"
        )
        connection.execute(
            "DELETE FROM secretary_workspace_schema_migrations WHERE version = 3"
        )

    service.initialize()
    with service.database.connect() as connection:
        columns = {
            row["name"]: row
            for row in connection.execute(
                "PRAGMA table_info(secretary_workspace_members)"
            ).fetchall()
        }
        owner_after = connection.execute(
            "SELECT * FROM secretary_workspace_members WHERE id = ?",
            (OWNER_MEMBER_ID,),
        ).fetchone()
        markers_before_replay = [
            dict(row)
            for row in connection.execute(
                """
                SELECT version, applied_at
                FROM secretary_workspace_schema_migrations ORDER BY version
                """
            ).fetchall()
        ]
    assert columns["version"]["notnull"] == 1
    assert str(columns["version"]["dflt_value"]).strip("'") == "1"
    assert owner_after["version"] == 1
    assert {key: owner_after[key] for key in owner_before} == owner_before
    assert [marker["version"] for marker in markers_before_replay] == [
        1,
        2,
        3,
        4,
        5,
        6,
        7,
    ]

    service.initialize()
    with service.database.transaction() as connection:
        markers_after_replay = [
            dict(row)
            for row in connection.execute(
                """
                SELECT version, applied_at
                FROM secretary_workspace_schema_migrations ORDER BY version
                """
            ).fetchall()
        ]
        owner_after_replay = dict(
            connection.execute(
                "SELECT * FROM secretary_workspace_members WHERE id = ?",
                (OWNER_MEMBER_ID,),
            ).fetchone()
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE secretary_workspace_members SET version = 0 WHERE id = ?",
                (OWNER_MEMBER_ID,),
            )
    assert markers_after_replay == markers_before_replay
    assert owner_after_replay["version"] == 1


def test_memo_idempotency_version_conflict_delete_and_sync_tombstone(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    create_headers = write_headers(owner_headers, "memo-create-001")
    payload = memo_payload()
    created = client.post(
        f"{WORKSPACE_PATH}/memos",
        headers=create_headers,
        json=payload,
    )
    assert created.status_code == 201, created.text
    assert created.headers["etag"] == '"1"'
    memo = created.json()
    assert memo["version"] == 1
    assert memo["domain"] == "work"
    assert memo["due_at"] == "2026-08-03T02:00:00Z"

    replayed = client.post(
        f"{WORKSPACE_PATH}/memos",
        headers=create_headers,
        json=payload,
    )
    assert replayed.status_code == 201, replayed.text
    assert replayed.json() == memo

    changed_replay = client.post(
        f"{WORKSPACE_PATH}/memos",
        headers=create_headers,
        json=memo_payload(title="同一幂等键下的另一项备忘"),
    )
    assert changed_replay.status_code == 409

    updated = client.patch(
        f"{WORKSPACE_PATH}/memos/{memo['id']}",
        headers=write_headers(
            owner_headers,
            "memo-update-001",
            if_match=memo["version"],
        ),
        json={"title": "已复核合同关键条款"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.headers["etag"] == '"2"'
    updated_memo = updated.json()
    assert updated_memo["version"] == 2

    stale = client.patch(
        f"{WORKSPACE_PATH}/memos/{memo['id']}",
        headers=write_headers(owner_headers, "memo-stale-001", if_match=1),
        json={"content": "这个过期客户端写入不能覆盖新版本。"},
    )
    assert stale.status_code == 412
    listed = client.get(f"{WORKSPACE_PATH}/memos", headers=owner_headers).json()
    assert listed["total"] == 1
    assert listed["items"][0]["title"] == "已复核合同关键条款"
    assert listed["items"][0]["content"] == payload["content"]

    deleted = client.post(
        f"{WORKSPACE_PATH}/memos/{memo['id']}/delete",
        headers=write_headers(owner_headers, "memo-delete-001"),
        json={"expected_version": updated_memo["version"]},
    )
    assert deleted.status_code == 200, deleted.text
    tombstone = deleted.json()
    assert tombstone["id"] == memo["id"]
    assert tombstone["workspace_id"] == "ws_default"
    assert tombstone["version"] == 3
    assert tombstone["deleted_at"]
    assert client.get(f"{WORKSPACE_PATH}/memos", headers=owner_headers).json() == {
        "items": [],
        "total": 0,
    }

    sync = client.get(f"{WORKSPACE_PATH}/sync?after=0", headers=owner_headers)
    assert sync.status_code == 200, sync.text
    changes = sync.json()["changes"]
    assert [change["event_type"] for change in changes] == [
        "memo.created",
        "memo.updated",
        "memo.deleted",
    ]
    assert changes[-1]["operation"] == "delete"
    assert changes[-1]["aggregate_id"] == memo["id"]
    assert changes[-1]["payload"] == tombstone
    assert sync.json()["next_cursor"] == changes[-1]["cursor"]


def test_owner_task_steps_lifecycle_and_illegal_transition(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    task = create_task(
        client,
        owner_headers,
        idempotency_key="task-create-001",
        payload=task_payload(with_steps=True),
    )
    assert task["stage"] == "draft"
    assert task["requires_alignment"] is False
    assert task["progress"] == 0
    assert len(task["steps"]) == 2

    illegal = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/transitions",
        headers=write_headers(owner_headers, "task-illegal-001"),
        json={"target_stage": "submitted", "expected_version": task["version"]},
    )
    assert illegal.status_code == 409
    unchanged = client.get(f"{WORKSPACE_PATH}/tasks", headers=owner_headers).json()[
        "items"
    ][0]
    assert unchanged["stage"] == "draft"
    assert unchanged["version"] == 1

    first_step, second_step = task["steps"]
    first_done = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/steps/{first_step['id']}",
        headers=write_headers(
            owner_headers, "task-step-done-001", if_match=task["version"]
        ),
        json={"status": "done", "expected_version": task["version"]},
    )
    assert first_done.status_code == 200, first_done.text
    task = first_done.json()
    assert task["progress"] == 50
    assert task["version"] == 2
    assert task["steps"][0]["status"] == "done"

    second_done = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/steps/{second_step['id']}",
        headers=write_headers(
            owner_headers, "task-step-done-002", if_match=task["version"]
        ),
        json={"status": "done", "expected_version": task["version"]},
    )
    assert second_done.status_code == 200, second_done.text
    task = second_done.json()
    assert task["progress"] == 100
    assert task["version"] == 3

    task = transition_task(
        client, owner_headers, task, "issued", "task-transition-issued-001"
    )
    assert task["stage"] == "aligned"
    assert task["version"] == 4

    task = transition_task(
        client,
        owner_headers,
        task,
        "in_progress",
        "task-transition-progress-001",
    )
    assert task["stage"] == "in_progress"
    assert task["started_at"]

    task = transition_task(
        client,
        owner_headers,
        task,
        "submitted",
        "task-transition-submit-001",
    )
    assert task["stage"] == "submitted"
    assert task["submitted_at"]

    task = transition_task(
        client,
        owner_headers,
        task,
        "accepted",
        "task-transition-accept-001",
    )
    assert task["stage"] == "accepted"
    assert task["accepted_at"]
    assert task["progress"] == 100

    closed_transition = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/transitions",
        headers=write_headers(owner_headers, "task-illegal-closed-001"),
        json={
            "target_stage": "in_progress",
            "expected_version": task["version"],
        },
    )
    assert closed_transition.status_code == 409
    persisted = client.get(f"{WORKSPACE_PATH}/tasks", headers=owner_headers).json()[
        "items"
    ][0]
    assert persisted["stage"] == "accepted"
    assert persisted["version"] == task["version"]

    events = client.get(f"{WORKSPACE_PATH}/sync?after=0", headers=owner_headers).json()[
        "changes"
    ]
    assert "task.aligned_automatically" in {event["event_type"] for event in events}


def test_issued_task_can_refine_alignment_before_assignee_confirms(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    assignee_id = "member-alignment-assignee"
    with client.app.state.workspace_service.database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO secretary_workspace_members(
                id, workspace_id, kind, role, display_name, active, created_at, updated_at
            ) VALUES (?, 'ws_default', 'person', 'member', '执行负责人', 1, ?, ?)
            """,
            (assignee_id, "2026-08-02T00:00:00Z", "2026-08-02T00:00:00Z"),
        )

    payload = task_payload(title="对齐新产品发布方案")
    payload["assignee_member_id"] = assignee_id
    task = create_task(
        client,
        owner_headers,
        idempotency_key="task-alignment-create-001",
        payload=payload,
    )
    assert task["requires_alignment"] is True

    task = transition_task(
        client,
        owner_headers,
        task,
        "issued",
        "task-alignment-issue-001",
    )
    assert task["stage"] == "issued"

    refined = client.patch(
        f"{WORKSPACE_PATH}/tasks/{task['id']}",
        headers=write_headers(
            owner_headers,
            "task-alignment-refine-001",
            if_match=task["version"],
        ),
        json={
            "purpose": "降低跨团队发布返工",
            "objective": "所有发布门槛在启动前获得负责人确认",
            "strategy": "先对齐门槛，再分解里程碑和每日行动。",
            "key_points": ["发布门槛", "资源锁定", "回滚预案"],
        },
    )
    assert refined.status_code == 200, refined.text
    task = refined.json()
    assert task["stage"] == "issued"
    assert task["purpose"] == "降低跨团队发布返工"
    assert task["objective"] == "所有发布门槛在启动前获得负责人确认"

    owner_cannot_impersonate_assignee = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/transitions",
        headers=write_headers(owner_headers, "task-alignment-owner-confirm-001"),
        json={"target_stage": "aligned", "expected_version": task["version"]},
    )
    assert owner_cannot_impersonate_assignee.status_code == 409
    assert "独立确认凭据" in owner_cannot_impersonate_assignee.json()["detail"]

    locked_task = create_task(
        client,
        owner_headers,
        idempotency_key="task-alignment-locked-create-001",
        payload=task_payload(title="已完成对齐的内部任务"),
    )
    locked_task = transition_task(
        client,
        owner_headers,
        locked_task,
        "issued",
        "task-alignment-locked-issue-001",
    )
    assert locked_task["stage"] == "aligned"

    locked = client.patch(
        f"{WORKSPACE_PATH}/tasks/{locked_task['id']}",
        headers=write_headers(
            owner_headers,
            "task-alignment-locked-001",
            if_match=locked_task["version"],
        ),
        json={"objective": "对齐后不能静默覆盖的另一目标"},
    )
    assert locked.status_code == 409
    items = client.get(f"{WORKSPACE_PATH}/tasks", headers=owner_headers).json()["items"]
    persisted = next(item for item in items if item["id"] == locked_task["id"])
    assert persisted["objective"] == locked_task["objective"]
    assert persisted["version"] == locked_task["version"]


def test_task_due_at_change_is_applied_only_after_explicit_protocol_accept(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    task = create_task(
        client,
        owner_headers,
        idempotency_key="task-change-create-001",
        payload=task_payload(
            title="准备董事会材料",
            due_at="2026-08-05T18:00:00+08:00",
        ),
    )
    assert task["due_at"] == "2026-08-05T10:00:00Z"

    proposed = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/changes",
        headers=write_headers(owner_headers, "task-due-propose-001"),
        json={
            "change_type": "due_at",
            "base_version": task["version"],
            "reason": "等待财务提供最终数字",
            "patch": {"due_at": "2026-08-08T18:00:00+08:00"},
        },
    )
    assert proposed.status_code == 201, proposed.text
    change = proposed.json()
    assert change["status"] == "proposed"
    assert change["before"] == task["due_at"]
    assert change["patch"]["due_at"] == "2026-08-08T10:00:00Z"

    before_decision = client.get(
        f"{WORKSPACE_PATH}/tasks", headers=owner_headers
    ).json()["items"][0]
    assert before_decision["due_at"] == task["due_at"]
    assert before_decision["version"] == task["version"]

    protocol = client.get(
        f"/api/v1/task-changes/{change['id']}", headers=owner_headers
    )
    assert protocol.status_code == 200, protocol.text
    accepted = client.post(
        f"/api/v1/task-changes/{change['id']}/decisions",
        headers=write_headers(
            owner_headers,
            "task-due-accept-001",
            if_match=protocol.json()["version"],
        ),
        json={
            "expected_change_version": protocol.json()["version"],
            "expected_task_version": task["version"],
            "proposal_digest": protocol.json()["proposal"]["digest"],
            "decision": "accept",
            "reason": None,
            "client_mutation_id": "task-due-accept-mutation-001",
        },
    )
    assert accepted.status_code == 200, accepted.text
    result = accepted.json()
    assert result["change"]["status"] == "accepted"
    assert result["task"]["version"] == task["version"] + 1
    assert result["task"]["due_at"] == "2026-08-08T10:00:00Z"


def test_calendar_meeting_minutes_require_owner_confirmation(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    calendar = client.post(
        f"{WORKSPACE_PATH}/calendar",
        headers=write_headers(owner_headers, "calendar-create-001"),
        json={
            "domain": "work",
            "title": "董事会准备会",
            "description": "对齐会议材料与结论。",
            "start_at": "2026-08-06T09:00:00+08:00",
            "end_at": "2026-08-06T10:00:00+08:00",
            "timezone": "Asia/Shanghai",
        },
    )
    assert calendar.status_code == 201, calendar.text
    calendar_entry = calendar.json()
    assert calendar_entry["start_at"] == "2026-08-06T01:00:00Z"
    assert calendar_entry["status"] == "scheduled"

    meeting = client.post(
        f"{WORKSPACE_PATH}/meetings",
        headers=write_headers(owner_headers, "meeting-create-001"),
        json={
            "domain": "work",
            "calendar_entry_id": calendar_entry["id"],
            "title": "董事会准备会",
            "purpose": "确认材料口径和后续行动",
            "agenda": ["材料检查", "行动确认"],
            "organizer_member_id": OWNER_MEMBER_ID,
            "start_at": "2026-08-06T09:00:00+08:00",
            "end_at": "2026-08-06T10:00:00+08:00",
            "timezone": "Asia/Shanghai",
        },
    )
    assert meeting.status_code == 201, meeting.text
    meeting_record = meeting.json()
    assert meeting_record["status"] == "planned"
    assert meeting_record["participants"][0]["member_id"] == OWNER_MEMBER_ID
    assert meeting_record["participants"][0]["role"] == "organizer"

    minutes = client.post(
        f"{WORKSPACE_PATH}/meetings/{meeting_record['id']}/minutes",
        headers=write_headers(
            owner_headers,
            "meeting-minutes-create-001",
            if_match=meeting_record["version"],
        ),
        json={
            "content": "确认采用统一财务口径，并由主人验收最终材料。",
            "required_confirmer_member_ids": [OWNER_MEMBER_ID],
        },
    )
    assert minutes.status_code == 201, minutes.text
    minutes_record = minutes.json()
    assert minutes_record.get("version") == 1, minutes_record
    assert minutes.headers["etag"] == '"1"'
    assert minutes_record["status"] == "confirming"
    assert minutes_record["confirmations"] == [
        {
            "member_id": OWNER_MEMBER_ID,
            "display_name": "主人",
            "status": "pending",
            "comment": None,
            "decided_at": None,
        }
    ]

    confirmed = client.post(
        f"{WORKSPACE_PATH}/minutes/{minutes_record['id']}/decision",
        headers=write_headers(owner_headers, "meeting-minutes-confirm-001"),
        json={
            "decision": "confirm",
            "expected_version": minutes_record["version"],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"
    assert confirmed.json()["confirmations"][0]["status"] == "confirmed"

    meetings = client.get(f"{WORKSPACE_PATH}/meetings", headers=owner_headers).json()
    assert meetings["total"] == 1
    assert meetings["items"][0]["status"] == "minutes_confirmed"
    assert meetings["items"][0]["minutes"][0]["status"] == "confirmed"

    bootstrap = client.get(f"{WORKSPACE_PATH}/bootstrap", headers=owner_headers).json()
    assert [item["id"] for item in bootstrap["calendar"]] == [calendar_entry["id"]]
    assert [item["id"] for item in bootstrap["meetings"]] == [meeting_record["id"]]


def test_sync_cursor_acknowledgement_is_monotonic_per_device(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    for index in (1, 2):
        created = client.post(
            f"{WORKSPACE_PATH}/memos",
            headers=write_headers(owner_headers, f"cursor-memo-create-{index:03d}"),
            json=memo_payload(title=f"游标测试备忘 {index}"),
        )
        assert created.status_code == 201, created.text

        sync = client.get(f"{WORKSPACE_PATH}/sync?after=0", headers=owner_headers)
        assert sync.status_code == 200, sync.text
        cursor = sync.json()["next_cursor"]
        acknowledged = client.put(
            f"{WORKSPACE_PATH}/sync/cursor",
            headers=write_headers(owner_headers, f"cursor-ack-{index:03d}"),
            json={"last_sequence": cursor},
        )
        assert acknowledged.status_code == 200, acknowledged.text
        assert acknowledged.json()["device_id"] == DEVICE_ID
        assert acknowledged.json()["cursor"] == cursor

    backwards = client.put(
        f"{WORKSPACE_PATH}/sync/cursor",
        headers=write_headers(owner_headers, "cursor-ack-backwards-001"),
        json={"last_sequence": cursor - 1},
    )
    assert backwards.status_code == 409

    beyond_server = client.put(
        f"{WORKSPACE_PATH}/sync/cursor",
        headers=write_headers(owner_headers, "cursor-ack-future-001"),
        json={"last_sequence": cursor + 1},
    )
    assert beyond_server.status_code == 409


def test_public_member_to_alignment_invitation_contract_is_bound_and_idempotent(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    member_url = f"{WORKSPACE_PATH}/members"
    member_headers = write_headers(owner_headers, "public-alignment-member-create-001")
    external_member_payload = {
        "kind": "external",
        "role": "member",
        "display_name": "外部交付负责人",
        "contact_ref": "wecom://external-delivery-owner",
        "client_mutation_id": "public-alignment-member-local-001",
    }
    created_member = client.post(
        member_url,
        headers=member_headers,
        json=external_member_payload,
    )
    assert created_member.status_code == 201, created_member.text
    member = created_member.json()
    assert member["kind"] == "external"
    assert member["role"] == "member"
    assert member["workspace_id"] == "ws_default"

    replayed_member = client.post(
        member_url,
        headers=member_headers,
        json=external_member_payload,
    )
    assert replayed_member.status_code == 201, replayed_member.text
    assert replayed_member.json() == member

    business_task_payload = task_payload(
        title="完成外部系统联合交付",
        due_at="2026-08-20T18:00:00+08:00",
    )
    business_task_payload["assignee_member_id"] = member["id"]
    task_headers = write_headers(owner_headers, "public-alignment-task-create-001")
    created_task = client.post(
        f"{WORKSPACE_PATH}/tasks",
        headers=task_headers,
        json=business_task_payload,
    )
    assert created_task.status_code == 201, created_task.text
    task = created_task.json()
    assert task["stage"] == "draft"
    assert task["requires_alignment"] is True
    assert task["issuer_member_id"] == OWNER_MEMBER_ID
    assert task["assignee_member_id"] == member["id"]
    assert task["assignee_label"] == member["display_name"]
    assert task["acceptance_owner_id"] == OWNER_MEMBER_ID
    assert task["purpose"] == business_task_payload["purpose"]
    assert task["objective"] == business_task_payload["objective"]
    assert task["strategy"] == business_task_payload["strategy"]
    assert task["key_points"] == business_task_payload["key_points"]
    assert task["acceptance_criteria"] == business_task_payload["acceptance_criteria"]
    assert task["due_at"] == "2026-08-20T10:00:00Z"

    replayed_task = client.post(
        f"{WORKSPACE_PATH}/tasks",
        headers=task_headers,
        json=business_task_payload,
    )
    assert replayed_task.status_code == 201, replayed_task.text
    assert replayed_task.json() == task

    transition_headers = write_headers(
        owner_headers, "public-alignment-task-issued-001"
    )
    transition_body = {
        "target_stage": "issued",
        "expected_version": task["version"],
    }
    issued_response = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/transitions",
        headers=transition_headers,
        json=transition_body,
    )
    assert issued_response.status_code == 200, issued_response.text
    issued_task = issued_response.json()
    assert issued_task["stage"] == "issued"
    assert issued_task["requires_alignment"] is True
    assert issued_task["assignee_member_id"] == member["id"]

    replayed_transition = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/transitions",
        headers=transition_headers,
        json=transition_body,
    )
    assert replayed_transition.status_code == 200, replayed_transition.text
    assert replayed_transition.json() == issued_task

    invitation_url = f"{WORKSPACE_PATH}/tasks/{task['id']}/alignment-invitations"
    invitation_headers = write_headers(
        owner_headers, "public-alignment-invitation-create-001"
    )
    invitation_body = {"expected_version": issued_task["version"]}
    created_invitation = client.post(
        invitation_url,
        headers=invitation_headers,
        json=invitation_body,
    )
    assert created_invitation.status_code == 201, created_invitation.text
    invitation = created_invitation.json()
    assert invitation["task_id"] == task["id"]
    assert invitation["task_version"] == issued_task["version"]
    assert invitation["assignee_member_id"] == member["id"]
    assert invitation["assignee_label"] == member["display_name"]
    assert invitation["confirmation_path"] == (
        f"{ALIGNMENT_PATH}/{invitation['invitation_id']}"
    )
    assert invitation["code"] not in invitation["confirmation_path"]

    replayed_invitation = client.post(
        invitation_url,
        headers=invitation_headers,
        json=invitation_body,
    )
    assert replayed_invitation.status_code == 409
    assert invitation["code"] not in replayed_invitation.text

    audit = client.get(
        f"{WORKSPACE_PATH}/audit?after=0",
        headers=owner_headers,
    )
    assert audit.status_code == 200, audit.text
    assert invitation["code"] not in audit.text
    events = audit.json()["changes"]
    assert [event["event_type"] for event in events] == [
        "workspace.member_created",
        "task.created",
        "task.issued",
        "task.agreement_created",
        "task.alignment_invitation_created",
    ]
    assert events[0]["aggregate_type"] == "workspace_member"
    assert events[0]["aggregate_id"] == member["id"]
    assert [event["aggregate_id"] for event in events[1:3]] == [
        task["id"],
        task["id"],
    ]
    assert events[3]["aggregate_type"] == "task_agreement"
    assert events[4]["aggregate_id"] == task["id"]
    assert events[-1]["payload"]["assignee_member_id"] == member["id"]

    with client.app.state.workspace_service.database.connect() as connection:
        stored_task = connection.execute(
            "SELECT * FROM secretary_business_tasks WHERE id = ?",
            (task["id"],),
        ).fetchone()
        invitations = connection.execute(
            """
            SELECT * FROM secretary_task_alignment_invitations
            WHERE task_id = ? AND creation_idempotency_key = ?
            """,
            (task["id"], "public-alignment-invitation-create-001"),
        ).fetchall()
        idempotency_rows = connection.execute(
            """
            SELECT operation, COUNT(*) AS row_count
            FROM secretary_workspace_idempotency
            WHERE workspace_id = ? AND actor_id = ?
            GROUP BY operation ORDER BY operation
            """,
            ("ws_default", OWNER_MEMBER_ID),
        ).fetchall()
    assert stored_task is not None
    assert stored_task["assignee_member_id"] == member["id"]
    assert stored_task["requires_alignment"] == 1
    assert stored_task["stage"] == "issued"
    assert len(invitations) == 1
    assert invitations[0]["assignee_member_id"] == member["id"]
    assert invitations[0]["task_version"] == issued_task["version"]
    assert {row["operation"]: row["row_count"] for row in idempotency_rows} == {
        "task.create": 1,
        f"task.transition:{task['id']}": 1,
        "workspace_member.create": 1,
    }


def test_external_assignee_alignment_requires_two_explicit_steps_and_audits_actor(
    client: TestClient,
    owner_headers: dict[str, str],
    agent_headers: dict[str, str],
) -> None:
    task, assignee_id = create_external_issued_task(
        client, owner_headers, suffix="happy"
    )
    invitation_url = f"{WORKSPACE_PATH}/tasks/{task['id']}/alignment-invitations"
    rejected_agent = client.post(
        invitation_url,
        headers=write_headers(agent_headers, "alignment-invite-agent"),
        json={"expected_version": task["version"]},
    )
    assert rejected_agent.status_code == 401

    invitation_headers = write_headers(owner_headers, "alignment-invite-happy")
    created_after = datetime.now(UTC)
    created = client.post(
        invitation_url,
        headers=invitation_headers,
        json={"expected_version": task["version"]},
    )
    assert created.status_code == 201, created.text
    assert created.headers["cache-control"].startswith("no-store")
    assert created.headers["referrer-policy"] == "no-referrer"
    invitation = created.json()
    assert re.fullmatch(r"align_[0-9a-f]{32}", invitation["invitation_id"])
    assert invitation["task_id"] == task["id"]
    assert invitation["task_version"] == task["version"]
    assert invitation["assignee_member_id"] == assignee_id
    assert invitation["confirmation_path"] == (
        f"{ALIGNMENT_PATH}/{invitation['invitation_id']}"
    )
    invitation_expiry = datetime.fromisoformat(invitation["expires_at"])
    assert (
        timedelta(minutes=9, seconds=55)
        <= (invitation_expiry - created_after)
        <= timedelta(minutes=10, seconds=5)
    )
    compact_code = invitation["code"].replace("-", "")
    assert len(compact_code) == 12
    assert set(compact_code) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")

    replayed_creation = client.post(
        invitation_url,
        headers=invitation_headers,
        json={"expected_version": task["version"]},
    )
    assert replayed_creation.status_code == 409
    assert invitation["code"] not in replayed_creation.text

    with client.app.state.workspace_service.database.connect() as connection:
        stored = connection.execute(
            """
            SELECT * FROM secretary_task_alignment_invitations WHERE id = ?
            """,
            (invitation["invitation_id"],),
        ).fetchone()
    assert stored is not None
    assert stored["task_version"] == task["version"]
    assert stored["assignee_member_id"] == assignee_id
    assert (
        stored["code_hash"]
        == hashlib.sha256(invitation["code"].encode("utf-8")).hexdigest()
    )
    assert invitation["code"] not in {
        str(value) for value in tuple(stored) if value is not None
    }

    page = client.get(invitation["confirmation_path"])
    assert page.status_code == 200, page.text
    assert page.headers["cache-control"].startswith("no-store")
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert task["title"] not in page.text
    assert task["purpose"] not in page.text
    assert invitation["assignee_label"] not in page.text
    assert invitation["code"] not in page.text
    assert "localStorage" not in page.text
    assert "<script" not in page.text

    owner_cannot_preview = client.post(
        f"{ALIGNMENT_PATH}/preview",
        headers=owner_headers,
        json={
            "invitation_id": invitation["invitation_id"],
            "code": invitation["code"],
        },
    )
    assert owner_cannot_preview.status_code == 403
    assert invitation["code"] not in owner_cannot_preview.text

    previewed = client.post(
        f"{ALIGNMENT_PATH}/preview",
        json={
            "invitation_id": invitation["invitation_id"],
            "code": invitation["code"].lower().replace("-", ""),
        },
    )
    assert previewed.status_code == 200, previewed.text
    assert previewed.headers["cache-control"].startswith("no-store")
    preview = previewed.json()
    assert preview["confirmation_token"].startswith("cp_align_confirm_")
    confirmation_expiry = datetime.fromisoformat(preview["confirmation_expires_at"])
    assert confirmation_expiry <= invitation_expiry
    assert (
        timedelta(minutes=4, seconds=50)
        <= (confirmation_expiry - datetime.now(UTC))
        <= timedelta(minutes=5)
    )
    assert preview["alignment"] == {
        "task_id": task["id"],
        "task_version": task["version"],
        "assignee_member_id": assignee_id,
        "assignee_label": invitation["assignee_label"],
        "title": task["title"],
        "purpose": task["purpose"],
        "objective": task["objective"],
        "strategy": task["strategy"],
        "key_points": task["key_points"],
        "acceptance_criteria": task["acceptance_criteria"],
        "due_at": task["due_at"],
    }
    assert invitation["code"] not in previewed.text

    with client.app.state.workspace_service.database.connect() as connection:
        unlocked = connection.execute(
            """
            SELECT * FROM secretary_task_alignment_invitations WHERE id = ?
            """,
            (invitation["invitation_id"],),
        ).fetchone()
    assert unlocked["code_used_at"] is not None
    assert (
        unlocked["confirmation_token_hash"]
        == hashlib.sha256(preview["confirmation_token"].encode("utf-8")).hexdigest()
    )
    assert preview["confirmation_token"] not in {
        str(value) for value in tuple(unlocked) if value is not None
    }

    code_replay = client.post(
        f"{ALIGNMENT_PATH}/preview",
        json={
            "invitation_id": invitation["invitation_id"],
            "code": invitation["code"],
        },
    )
    assert code_replay.status_code == 409

    owner_transition = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/transitions",
        headers=write_headers(owner_headers, "alignment-owner-still-blocked"),
        json={"target_stage": "aligned", "expected_version": task["version"]},
    )
    assert owner_transition.status_code == 409

    owner_cannot_confirm = client.post(
        f"{ALIGNMENT_PATH}/confirm",
        headers=owner_headers,
        json={
            "invitation_id": invitation["invitation_id"],
            "confirmation_token": preview["confirmation_token"],
        },
    )
    assert owner_cannot_confirm.status_code == 403
    assert preview["confirmation_token"] not in owner_cannot_confirm.text

    confirmed = client.post(
        f"{ALIGNMENT_PATH}/confirm",
        json={
            "invitation_id": invitation["invitation_id"],
            "confirmation_token": preview["confirmation_token"],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    result = confirmed.json()
    assert result["task_id"] == task["id"]
    assert result["stage"] == "aligned"
    assert result["version"] == task["version"] + 1
    assert result["assignee_member_id"] == assignee_id

    replayed_confirmation = client.post(
        f"{ALIGNMENT_PATH}/confirm",
        json={
            "invitation_id": invitation["invitation_id"],
            "confirmation_token": preview["confirmation_token"],
        },
    )
    assert replayed_confirmation.status_code == 409
    assert preview["confirmation_token"] not in replayed_confirmation.text

    tasks = client.get(f"{WORKSPACE_PATH}/tasks", headers=owner_headers).json()["items"]
    persisted_task = next(item for item in tasks if item["id"] == task["id"])
    assert persisted_task["stage"] == "aligned"
    assert persisted_task["updated_by"] == assignee_id

    agreement = client.get(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/agreement",
        headers=owner_headers,
    )
    assert agreement.status_code == 200, agreement.text
    assert agreement.json()["status"] == "accepted"
    assert len(agreement.json()["decisions"]) == 1
    legacy_decision = agreement.json()["decisions"][0]
    assert legacy_decision["action"] == "accept"
    assert legacy_decision["actor_session_id"] is None
    assert legacy_decision["assurance_method"] == "dual_channel_capability"

    audit = client.get(f"{WORKSPACE_PATH}/audit?after=0", headers=owner_headers)
    assert audit.status_code == 200, audit.text
    assert invitation["code"] not in audit.text
    assert preview["confirmation_token"] not in audit.text
    alignment_events = [
        event
        for event in audit.json()["changes"]
        if event["aggregate_id"] == task["id"]
        and event["event_type"] == "task.aligned_by_assignee"
    ]
    assert len(alignment_events) == 1
    assert alignment_events[0]["actor_type"] == "member"
    assert alignment_events[0]["actor_member_id"] == assignee_id
    assert alignment_events[0]["payload"]["stage"] == "aligned"


def test_alignment_code_attempt_limit_and_secret_validation_are_fail_closed(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    task, _assignee_id = create_external_issued_task(
        client, owner_headers, suffix="attempts"
    )
    invitation = create_alignment_invitation(
        client, owner_headers, task, suffix="attempts"
    )
    wrong_codes = [
        "bad",
        "0000-0000-0000",
        "1111-1111-1111",
        "2222-2222-2222",
        "3333-3333-3333",
    ]
    assert invitation["code"] not in wrong_codes
    for wrong_code in wrong_codes:
        rejected = client.post(
            f"{ALIGNMENT_PATH}/preview",
            json={
                "invitation_id": invitation["invitation_id"],
                "code": wrong_code,
            },
        )
        assert rejected.status_code == 401
        assert wrong_code not in rejected.text

    correct_after_lock = client.post(
        f"{ALIGNMENT_PATH}/preview",
        json={
            "invitation_id": invitation["invitation_id"],
            "code": invitation["code"],
        },
    )
    assert correct_after_lock.status_code == 401
    assert invitation["code"] not in correct_after_lock.text
    with client.app.state.workspace_service.database.connect() as connection:
        locked = connection.execute(
            """
            SELECT * FROM secretary_task_alignment_invitations WHERE id = ?
            """,
            (invitation["invitation_id"],),
        ).fetchone()
    assert locked["failed_attempts"] == 5
    assert locked["revoked_at"] is not None

    nested_secret = "must-never-be-reflected-alignment-code"
    malformed = client.post(
        f"{ALIGNMENT_PATH}/preview",
        json={
            "invitation_id": invitation["invitation_id"],
            "code": {"secret": nested_secret},
        },
    )
    assert malformed.status_code == 422
    assert malformed.json() == {"detail": "请求格式无效"}
    assert nested_secret not in malformed.text
    assert malformed.headers["cache-control"].startswith("no-store")


def test_alignment_expiry_and_task_version_change_invalidate_credentials(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    expired_task, _ = create_external_issued_task(
        client, owner_headers, suffix="expired"
    )
    expired_invitation = create_alignment_invitation(
        client, owner_headers, expired_task, suffix="expired"
    )
    with client.app.state.workspace_service.database.transaction() as connection:
        connection.execute(
            """
            UPDATE secretary_task_alignment_invitations
            SET expires_at = '2026-08-01T00:00:00Z'
            WHERE id = ?
            """,
            (expired_invitation["invitation_id"],),
        )
    expired = client.post(
        f"{ALIGNMENT_PATH}/preview",
        json={
            "invitation_id": expired_invitation["invitation_id"],
            "code": expired_invitation["code"],
        },
    )
    assert expired.status_code == 401
    assert expired_invitation["code"] not in expired.text

    replacement_task, _ = create_external_issued_task(
        client, owner_headers, suffix="replacement"
    )
    first_invitation = create_alignment_invitation(
        client, owner_headers, replacement_task, suffix="replacement-first"
    )
    second_invitation = create_alignment_invitation(
        client, owner_headers, replacement_task, suffix="replacement-second"
    )
    replaced = client.post(
        f"{ALIGNMENT_PATH}/preview",
        json={
            "invitation_id": first_invitation["invitation_id"],
            "code": first_invitation["code"],
        },
    )
    assert replaced.status_code == 401
    replacement_preview = client.post(
        f"{ALIGNMENT_PATH}/preview",
        json={
            "invitation_id": second_invitation["invitation_id"],
            "code": second_invitation["code"],
        },
    )
    assert replacement_preview.status_code == 200, replacement_preview.text

    changed_before_task, _ = create_external_issued_task(
        client, owner_headers, suffix="changed-before"
    )
    changed_before_invitation = create_alignment_invitation(
        client, owner_headers, changed_before_task, suffix="changed-before"
    )
    changed_before = client.patch(
        f"{WORKSPACE_PATH}/tasks/{changed_before_task['id']}",
        headers=write_headers(
            owner_headers,
            "alignment-change-before-preview",
            if_match=changed_before_task["version"],
        ),
        json={"strategy": "改为先锁定资源，再启动跨团队交付。"},
    )
    assert changed_before.status_code == 409, changed_before.text
    stale_code = client.post(
        f"{ALIGNMENT_PATH}/preview",
        json={
            "invitation_id": changed_before_invitation["invitation_id"],
            "code": changed_before_invitation["code"],
        },
    )
    assert stale_code.status_code == 200
    assert changed_before_invitation["code"] not in stale_code.text

    changed_task, _ = create_external_issued_task(
        client, owner_headers, suffix="changed"
    )
    changed_invitation = create_alignment_invitation(
        client, owner_headers, changed_task, suffix="changed"
    )
    preview = client.post(
        f"{ALIGNMENT_PATH}/preview",
        json={
            "invitation_id": changed_invitation["invitation_id"],
            "code": changed_invitation["code"],
        },
    )
    assert preview.status_code == 200, preview.text
    confirmation_token = preview.json()["confirmation_token"]

    refined = client.patch(
        f"{WORKSPACE_PATH}/tasks/{changed_task['id']}",
        headers=write_headers(
            owner_headers,
            "alignment-change-after-preview",
            if_match=changed_task["version"],
        ),
        json={"objective": "已变更、必须重新取得承办人确认的新目标"},
    )
    assert refined.status_code == 409, refined.text
    with client.app.state.workspace_service.database.transaction() as connection:
        connection.execute(
            """
            UPDATE secretary_business_tasks
            SET objective = ?, version = version + 1
            WHERE id = ?
            """,
            ("模拟外部旧版本写入造成的漂移", changed_task["id"]),
        )

    stale_confirmation = client.post(
        f"{ALIGNMENT_PATH}/confirm",
        json={
            "invitation_id": changed_invitation["invitation_id"],
            "confirmation_token": confirmation_token,
        },
    )
    assert stale_confirmation.status_code == 409
    assert confirmation_token not in stale_confirmation.text
    persisted = next(
        item
        for item in client.get(f"{WORKSPACE_PATH}/tasks", headers=owner_headers).json()[
            "items"
        ]
        if item["id"] == changed_task["id"]
    )
    assert persisted["stage"] == "issued"
    assert persisted["objective"] == "模拟外部旧版本写入造成的漂移"

    token_task, _ = create_external_issued_task(
        client, owner_headers, suffix="token-expired"
    )
    token_invitation = create_alignment_invitation(
        client, owner_headers, token_task, suffix="token-expired"
    )
    token_preview = client.post(
        f"{ALIGNMENT_PATH}/preview",
        json={
            "invitation_id": token_invitation["invitation_id"],
            "code": token_invitation["code"],
        },
    ).json()
    with client.app.state.workspace_service.database.transaction() as connection:
        connection.execute(
            """
            UPDATE secretary_task_alignment_invitations
            SET confirmation_expires_at = '2026-08-01T00:00:00Z'
            WHERE id = ?
            """,
            (token_invitation["invitation_id"],),
        )
    expired_token = client.post(
        f"{ALIGNMENT_PATH}/confirm",
        json={
            "invitation_id": token_invitation["invitation_id"],
            "confirmation_token": token_preview["confirmation_token"],
        },
    )
    assert expired_token.status_code == 401
    assert token_preview["confirmation_token"] not in expired_token.text


def test_alignment_mobile_html_flow_keeps_secrets_out_of_urls_and_storage(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    task, _ = create_external_issued_task(client, owner_headers, suffix="html")
    invitation = create_alignment_invitation(client, owner_headers, task, suffix="html")
    assert invitation["code"] not in invitation["confirmation_path"]

    invitation_page = client.get(invitation["confirmation_path"])
    assert invitation_page.status_code == 200
    assert "下达人生成邀请时会同时看到邀请链接和确认码" in invitation_page.text
    assert "相互独立的渠道" in invitation_page.text

    preview_page = client.post(
        f"{ALIGNMENT_PATH}/{invitation['invitation_id']}/preview",
        data={"code": invitation["code"]},
    )
    assert preview_page.status_code == 200, preview_page.text
    assert task["title"] in preview_page.text
    assert task["purpose"] in preview_page.text
    assert task["objective"] in preview_page.text
    assert task["strategy"] in preview_page.text
    for criterion in task["acceptance_criteria"]:
        assert criterion in preview_page.text
    assert invitation["code"] not in preview_page.text
    assert invitation["code"] not in str(preview_page.url)
    assert "localStorage" not in preview_page.text
    assert "<script" not in preview_page.text
    assert "提交仅证明本次两段凭据的持有能力" in preview_page.text
    assert "映射到邀请中指定的承办人记录" in preview_page.text
    assert "不证明自然人或企业身份" in preview_page.text
    assert "不构成电子签名" in preview_page.text
    assert "以本次凭据持有者身份确认" in preview_page.text
    assert "我已核对，并确认任务对齐" not in preview_page.text
    assert preview_page.headers["cache-control"].startswith("no-store")

    token_match = re.search(
        r'name="confirmation_token" value="([^"]+)"', preview_page.text
    )
    assert token_match is not None
    confirmation_token = token_match.group(1)
    assert confirmation_token not in str(preview_page.url)

    confirmation_page = client.post(
        f"{ALIGNMENT_PATH}/{invitation['invitation_id']}/confirm",
        data={"confirmation_token": confirmation_token},
    )
    assert confirmation_page.status_code == 200, confirmation_page.text
    assert "任务已完成对齐" in confirmation_page.text
    assert "双渠道邀请凭据" in confirmation_page.text
    assert "不构成实名身份、电子签名或授权代理证明" in confirmation_page.text
    assert "已明确确认" not in confirmation_page.text
    assert confirmation_token not in confirmation_page.text
    assert confirmation_token not in str(confirmation_page.url)
    assert confirmation_page.headers["referrer-policy"] == "no-referrer"


def test_workspace_schema_upgrade_preserves_existing_database(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "existing-pocket.db")
    database.initialize()
    workspace_service = WorkspaceService(
        database, task_session_hmac_key=b"test-task-session-hmac-key-32b!!"
    )
    workspace_service.initialize()
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO secretary_workspace_members(
                id, workspace_id, kind, role, display_name, active,
                created_at, updated_at
            ) VALUES (
                'member-existing', 'ws_default', 'person', 'member',
                '既有成员', 1, '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z'
            )
            """
        )
        connection.execute("DROP TABLE secretary_task_alignment_invitations")
        connection.execute("DROP TABLE secretary_task_checkins")

    workspace_service.initialize()
    workspace_service.initialize()
    with database.connect() as connection:
        member = connection.execute(
            """
            SELECT display_name FROM secretary_workspace_members
            WHERE id = 'member-existing'
            """
        ).fetchone()
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(secretary_task_alignment_invitations)"
            ).fetchall()
        }
        checkin_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(secretary_task_checkins)"
            ).fetchall()
        }
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert member["display_name"] == "既有成员"
    assert {
        "code_hash",
        "task_version",
        "assignee_member_id",
        "confirmation_token_hash",
        "failed_attempts",
        "consumed_at",
    } <= columns
    assert {
        "id",
        "workspace_id",
        "task_id",
        "task_version",
        "report_date",
        "summary",
        "reported_progress",
        "risks_json",
        "blockers_json",
        "next_actions_json",
        "forecast_at",
        "created_by",
        "device_id",
        "client_mutation_id",
        "version",
        "created_at",
    } == checkin_columns
    assert violations == []


def test_task_checkin_is_append_only_idempotent_private_and_strict(
    client: TestClient,
    owner_headers: dict[str, str],
    agent_headers: dict[str, str],
) -> None:
    task = create_task(
        client,
        owner_headers,
        idempotency_key="checkin-task-create",
        payload=task_payload(title="跟进关键客户交付"),
    )
    task = transition_task(
        client,
        owner_headers,
        task,
        "issued",
        "checkin-task-align",
    )
    task_url = f"{WORKSPACE_PATH}/tasks/{task['id']}"
    checkin_url = f"{task_url}/check-ins"
    payload = {
        "expected_version": task["version"],
        "summary": "核心方案已经评审，等待客户确认上线窗口。",
        "reported_progress": 90,
        "risks": ["客户窗口仍可能调整"],
        "blockers": ["等待客户最终确认"],
        "next_actions": ["今天发送上线清单"],
        "forecast_at": "2026-08-06T18:00:00+08:00",
        "client_mutation_id": "mobile-checkin-001",
    }
    headers = write_headers(
        owner_headers,
        "task-checkin-create-001",
        if_match=task["version"],
    )
    headers["Origin"] = "http://127.0.0.1:17818"

    created = client.post(checkin_url, headers=headers, json=payload)
    assert created.status_code == 201, created.text
    assert created.headers["etag"] == '"1"'
    assert created.headers["access-control-expose-headers"] == "ETag"
    assert created.headers["cache-control"] == "no-store, max-age=0"
    assert created.headers["pragma"] == "no-cache"
    assert created.headers["referrer-policy"] == "no-referrer"
    assert created.headers["x-content-type-options"] == "nosniff"
    checkin = created.json()
    assert set(checkin) == {
        "id",
        "workspace_id",
        "task_id",
        "task_version",
        "report_date",
        "summary",
        "reported_progress",
        "risks",
        "blockers",
        "next_actions",
        "forecast_at",
        "created_by",
        "version",
        "client_mutation_id",
        "created_at",
    }
    assert "device_id" not in checkin
    assert checkin["task_id"] == task["id"]
    assert checkin["task_version"] == task["version"]
    assert checkin["version"] == 1
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", checkin["report_date"])
    assert checkin["forecast_at"] == "2026-08-06T10:00:00Z"

    after_checkin = next(
        item
        for item in client.get(f"{WORKSPACE_PATH}/tasks", headers=owner_headers).json()[
            "items"
        ]
        if item["id"] == task["id"]
    )
    assert {
        key: after_checkin[key] for key in ("version", "stage", "health", "progress")
    } == {key: task[key] for key in ("version", "stage", "health", "progress")}

    replayed = client.post(checkin_url, headers=headers, json=payload)
    assert replayed.status_code == 201, replayed.text
    assert replayed.json() == checkin
    assert replayed.headers["etag"] == '"1"'

    changed_payload = {**payload, "summary": "同一请求键下的不同内容"}
    changed_replay = client.post(
        checkin_url,
        headers=headers,
        json=changed_payload,
    )
    assert changed_replay.status_code == 409
    assert changed_replay.headers["cache-control"] == "no-store, max-age=0"

    missing_if_match = client.post(
        checkin_url,
        headers=write_headers(owner_headers, "task-checkin-no-etag"),
        json=payload,
    )
    assert missing_if_match.status_code == 428
    assert missing_if_match.headers["referrer-policy"] == "no-referrer"

    header_body_mismatch = client.post(
        checkin_url,
        headers=write_headers(
            owner_headers,
            "task-checkin-version-mismatch",
            if_match=task["version"] + 1,
        ),
        json=payload,
    )
    assert header_body_mismatch.status_code == 412

    stale_payload = {**payload, "expected_version": task["version"] - 1}
    stale = client.post(
        checkin_url,
        headers=write_headers(
            owner_headers,
            "task-checkin-stale",
            if_match=task["version"] - 1,
        ),
        json=stale_payload,
    )
    assert stale.status_code == 412

    rejected_agent = client.post(
        checkin_url,
        headers=write_headers(
            agent_headers,
            "task-checkin-agent",
            if_match=task["version"],
        ),
        json=payload,
    )
    assert rejected_agent.status_code == 401
    assert rejected_agent.headers["x-content-type-options"] == "nosniff"

    rejected_query = client.post(
        f"{checkin_url}?source=mobile",
        headers=write_headers(
            owner_headers,
            "task-checkin-query",
            if_match=task["version"],
        ),
        json=payload,
    )
    assert rejected_query.status_code == 400
    listed_with_query = client.get(f"{checkin_url}?limit=1", headers=owner_headers)
    assert listed_with_query.status_code == 400

    malformed = client.post(
        checkin_url,
        headers=write_headers(
            owner_headers,
            "task-checkin-malformed",
            if_match=task["version"],
        ),
        json={**payload, "summary": "x" * 4001, "unexpected": True},
    )
    assert malformed.status_code == 422
    assert malformed.headers["cache-control"] == "no-store, max-age=0"

    listed = client.get(checkin_url, headers=owner_headers)
    assert listed.status_code == 200, listed.text
    assert listed.headers["cache-control"] == "no-store, max-age=0"
    assert listed.json() == {"items": [checkin], "total": 1}
    assert client.get(checkin_url, headers=agent_headers).status_code == 401

    events = client.get(f"{WORKSPACE_PATH}/sync?after=0", headers=owner_headers).json()[
        "changes"
    ]
    checkin_events = [
        event for event in events if event["event_type"] == "task.checkin_recorded"
    ]
    assert len(checkin_events) == 1
    assert checkin_events[0]["aggregate_type"] == "task_checkin"
    assert checkin_events[0]["aggregate_id"] == checkin["id"]
    assert checkin_events[0]["payload"] == checkin
    assert "device_id" not in checkin_events[0]["payload"]

    with client.app.state.workspace_service.database.transaction() as connection:
        connection.execute(
            """
            UPDATE secretary_business_tasks SET stage = 'accepted'
            WHERE id = ?
            """,
            (task["id"],),
        )
    terminal_fresh_request = client.post(
        checkin_url,
        headers=write_headers(
            owner_headers,
            "task-checkin-terminal",
            if_match=task["version"],
        ),
        json=payload,
    )
    assert terminal_fresh_request.status_code == 409
    terminal_replay = client.post(checkin_url, headers=headers, json=payload)
    assert terminal_replay.status_code == 201
    assert terminal_replay.json() == checkin


def test_task_checkin_report_date_uses_workspace_timezone(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    task = create_task(
        client,
        owner_headers,
        idempotency_key="timezone-task-create",
        payload=task_payload(title="跨午夜交付复盘"),
    )
    task = transition_task(
        client,
        owner_headers,
        task,
        "issued",
        "timezone-task-align",
    )
    service = client.app.state.workspace_service
    base_payload = {
        "expected_version": task["version"],
        "summary": "记录工作区本地日期边界。",
        "reported_progress": 10,
        "risks": [],
        "blockers": [],
        "next_actions": [],
        "forecast_at": None,
        "client_mutation_id": None,
    }
    before_midnight = service.create_task_checkin(
        "ws_default",
        task["id"],
        base_payload,
        idempotency_key="timezone-checkin-before",
        device_id=DEVICE_ID,
        as_of=datetime(2026, 8, 2, 15, 59, tzinfo=UTC),
    )
    after_midnight = service.create_task_checkin(
        "ws_default",
        task["id"],
        {**base_payload, "summary": "跨过工作区午夜后的复盘。"},
        idempotency_key="timezone-checkin-after",
        device_id=DEVICE_ID,
        as_of=datetime(2026, 8, 2, 16, 1, tzinfo=UTC),
    )
    assert before_midnight["report_date"] == "2026-08-02"
    assert after_midnight["report_date"] == "2026-08-03"
    assert before_midnight["task_version"] == after_midnight["task_version"]
    assert before_midnight["task_version"] == task["version"]


def test_task_attention_derives_all_reasons_without_mutating_tasks(
    client: TestClient,
    owner_headers: dict[str, str],
    agent_headers: dict[str, str],
) -> None:
    as_of = datetime(2026, 8, 2, 4, 0, tzinfo=UTC)
    service = client.app.state.workspace_service

    review_task = create_task(
        client,
        owner_headers,
        idempotency_key="attention-review-create",
        payload=task_payload(
            title="准备客户发布",
            due_at="2026-08-03T12:00:00+08:00",
        ),
    )
    review_task = transition_task(
        client,
        owner_headers,
        review_task,
        "issued",
        "attention-review-align",
    )
    review_task = transition_task(
        client,
        owner_headers,
        review_task,
        "in_progress",
        "attention-review-start",
    )
    with service.database.transaction() as connection:
        connection.execute(
            """
            UPDATE secretary_business_tasks
            SET started_at = '2026-08-01T03:00:00Z'
            WHERE id = ?
            """,
            (review_task["id"],),
        )

    before = service.task_attention("ws_default", as_of=as_of)
    review_before = next(
        item for item in before["items"] if item["task_id"] == review_task["id"]
    )
    before_codes = {reason["code"] for reason in review_before["reasons"]}
    assert {"plan_missing", "review_due", "due_soon"} <= before_codes
    before_snapshot = (
        review_before["task_version"],
        review_before["stage"],
        review_before["progress"],
    )

    first_checkin = service.create_task_checkin(
        "ws_default",
        review_task["id"],
        {
            "expected_version": review_task["version"],
            "summary": "今天已经复盘，但仍在等待外部依赖。",
            "reported_progress": 100,
            "risks": [],
            "blockers": ["依赖一", "依赖二", "依赖三", "依赖四"],
            "next_actions": ["继续催办"],
            "forecast_at": "2026-08-04T04:00:00Z",
            "client_mutation_id": None,
        },
        idempotency_key="attention-review-checkin",
        device_id=DEVICE_ID,
        as_of=as_of,
    )
    after = service.task_attention("ws_default", as_of=as_of)
    review_after = next(
        item for item in after["items"] if item["task_id"] == review_task["id"]
    )
    after_codes = {reason["code"] for reason in review_after["reasons"]}
    assert "review_due" not in after_codes
    assert {"plan_missing", "blocked", "forecast_slip", "due_soon"} <= after_codes
    assert review_after["latest_check_in_at"] == first_checkin["created_at"]
    assert review_after["latest_reported_progress"] == 100
    assert review_after["progress"] == 0
    blocked_reason = next(
        reason for reason in review_after["reasons"] if reason["code"] == "blocked"
    )
    assert blocked_reason["evidence"]["blockers"] == [
        "依赖一",
        "依赖二",
        "依赖三",
    ]
    assert (
        review_after["task_version"],
        review_after["stage"],
        review_after["progress"],
    ) == before_snapshot

    service.create_task_checkin(
        "ws_default",
        review_task["id"],
        {
            "expected_version": review_task["version"],
            "summary": "外部依赖已经解除。",
            "reported_progress": 100,
            "risks": [],
            "blockers": [],
            "next_actions": ["按原计划继续"],
            "forecast_at": None,
            "client_mutation_id": None,
        },
        idempotency_key="attention-review-cleared",
        device_id=DEVICE_ID,
        as_of=as_of,
    )
    cleared = service.task_attention("ws_default", as_of=as_of)
    cleared_item = next(
        item for item in cleared["items"] if item["task_id"] == review_task["id"]
    )
    cleared_codes = {reason["code"] for reason in cleared_item["reasons"]}
    assert "blocked" not in cleared_codes
    assert "forecast_slip" not in cleared_codes
    assert "review_due" not in cleared_codes
    assert "due_soon" in cleared_codes

    critical_task = create_task(
        client,
        owner_headers,
        idempotency_key="attention-critical-create",
        payload=task_payload(
            title="处理逾期且阻塞的交付",
            due_at="2026-08-02T11:00:00+08:00",
            with_steps=True,
        ),
    )
    critical_task = transition_task(
        client,
        owner_headers,
        critical_task,
        "issued",
        "attention-critical-align",
    )
    critical_task = transition_task(
        client,
        owner_headers,
        critical_task,
        "in_progress",
        "attention-critical-start",
    )
    blocked_step = critical_task["steps"][0]
    with service.database.transaction() as connection:
        connection.execute(
            """
            UPDATE secretary_business_tasks
            SET health = 'blocked', started_at = '2026-08-01T03:00:00Z'
            WHERE id = ?
            """,
            (critical_task["id"],),
        )
        connection.execute(
            """
            UPDATE secretary_task_steps
            SET status = 'blocked', due_at = '2026-08-02T02:00:00Z'
            WHERE id = ?
            """,
            (blocked_step["id"],),
        )
    calendar = client.post(
        f"{WORKSPACE_PATH}/calendar",
        headers=write_headers(owner_headers, "attention-calendar-create"),
        json={
            "domain": "work",
            "title": "已错过的交付日程",
            "description": "验证派生预警。",
            "start_at": "2026-08-02T08:00:00+08:00",
            "end_at": "2026-08-02T09:00:00+08:00",
            "timezone": "Asia/Shanghai",
            "task_id": critical_task["id"],
        },
    )
    assert calendar.status_code == 201, calendar.text

    draft_task = create_task(
        client,
        owner_headers,
        idempotency_key="attention-draft-create",
        payload=task_payload(title="尚未下达的草稿任务"),
    )
    issued_task, _ = create_external_issued_task(
        client, owner_headers, suffix="attention-issued"
    )
    accepted_task = create_task(
        client,
        owner_headers,
        idempotency_key="attention-accepted-create",
        payload=task_payload(title="已经验收的任务"),
    )
    accepted_task = transition_task(
        client,
        owner_headers,
        accepted_task,
        "issued",
        "attention-accepted-align",
    )
    abnormal_task = create_task(
        client,
        owner_headers,
        idempotency_key="attention-abnormal-create",
        payload=task_payload(title="非正常关闭的任务"),
    )
    abnormal_task = transition_task(
        client,
        owner_headers,
        abnormal_task,
        "issued",
        "attention-abnormal-align",
    )
    with service.database.transaction() as connection:
        connection.execute(
            "UPDATE secretary_business_tasks SET stage = 'accepted' WHERE id = ?",
            (accepted_task["id"],),
        )
        connection.execute(
            """
            UPDATE secretary_business_tasks SET stage = 'abnormal_closed'
            WHERE id = ?
            """,
            (abnormal_task["id"],),
        )

    attention = service.task_attention("ws_default", as_of=as_of)
    assert attention["generated_at"] == "2026-08-02T04:00:00Z"
    assert attention["workspace_timezone"] == "Asia/Shanghai"
    assert attention["total"] == len(attention["items"])
    returned_ids = {item["task_id"] for item in attention["items"]}
    assert {
        draft_task["id"],
        issued_task["id"],
        accepted_task["id"],
        abnormal_task["id"],
    }.isdisjoint(returned_ids)

    critical_item = next(
        item for item in attention["items"] if item["task_id"] == critical_task["id"]
    )
    critical_codes = {reason["code"] for reason in critical_item["reasons"]}
    assert {"step_overdue", "schedule_missed", "task_overdue", "blocked"} <= (
        critical_codes
    )
    final_codes = {
        reason["code"] for item in attention["items"] for reason in item["reasons"]
    }
    assert final_codes | before_codes | after_codes == {
        "plan_missing",
        "review_due",
        "step_overdue",
        "schedule_missed",
        "task_overdue",
        "blocked",
        "forecast_slip",
        "due_soon",
    }
    assert attention["items"][0]["severity"] == "critical"
    allowed_evidence_keys = {
        "task_due_at",
        "step_id",
        "step_title",
        "step_due_at",
        "schedule_id",
        "schedule_title",
        "schedule_end_at",
        "latest_check_in_at",
        "forecast_at",
        "blockers",
        "canonical_progress",
        "threshold_progress",
        "hours_remaining",
        "local_date",
    }
    for item in attention["items"]:
        assert set(item) == {
            "task_id",
            "task_version",
            "title",
            "stage",
            "progress",
            "due_at",
            "latest_check_in_at",
            "latest_reported_progress",
            "severity",
            "reasons",
        }
        assert item["severity"] in {"warning", "critical"}
        for reason in item["reasons"]:
            assert set(reason) == {"code", "severity", "message", "evidence"}
            assert reason["severity"] in {"warning", "critical"}
            assert set(reason["evidence"]) <= allowed_evidence_keys

    injected_clock = client.get(
        f"{WORKSPACE_PATH}/task-attention?as_of=2026-08-02T04:00:00Z",
        headers=owner_headers,
    )
    assert injected_clock.status_code == 400
    assert injected_clock.headers["cache-control"] == "no-store, max-age=0"
    http_attention = client.get(
        f"{WORKSPACE_PATH}/task-attention", headers=owner_headers
    )
    assert http_attention.status_code == 200, http_attention.text
    assert http_attention.headers["cache-control"] == "no-store, max-age=0"
    assert http_attention.headers["pragma"] == "no-cache"
    assert http_attention.headers["referrer-policy"] == "no-referrer"
    assert http_attention.headers["x-content-type-options"] == "nosniff"
    assert set(http_attention.json()) == {
        "generated_at",
        "workspace_timezone",
        "items",
        "total",
    }
    assert (
        client.get(
            f"{WORKSPACE_PATH}/task-attention", headers=agent_headers
        ).status_code
        == 401
    )


def test_p1_step_create_patch_reorder_graph_and_idempotency(
    client: TestClient, owner_headers: dict[str, str]
) -> None:
    task = create_task(
        client,
        owner_headers,
        idempotency_key="p1-step-task",
        payload=task_payload(title="主人自办步骤编排"),
    )
    path = f"{WORKSPACE_PATH}/tasks/{task['id']}/steps"
    payload = {
        "expected_version": task["version"],
        "step_type": "action",
        "title": "整理材料",
        "depends_on_step_ids": [],
        "client_mutation_id": "p1-client-step-first",
    }
    assert (
        client.post(
            path,
            headers=write_headers(owner_headers, "p1-no-if-match"),
            json=payload,
        ).status_code
        == 428
    )
    assert (
        client.post(
            path,
            headers=write_headers(owner_headers, "p1-wrong-if-match", if_match=2),
            json=payload,
        ).status_code
        == 412
    )
    assert (
        client.post(
            f"{path}?unexpected=1",
            headers=write_headers(owner_headers, "p1-query", if_match=1),
            json=payload,
        ).status_code
        == 400
    )
    assert (
        client.post(
            path,
            headers=write_headers(owner_headers, "p1-empty-kr", if_match=1),
            json={
                "expected_version": 1,
                "step_type": "key_result",
                "title": "无度量关键结果",
                "success_metric": {},
            },
        ).status_code
        == 422
    )
    headers = write_headers(owner_headers, "p1-step-first", if_match=1)
    first_response = client.post(path, headers=headers, json=payload)
    assert first_response.status_code == 201, first_response.text
    task = first_response.json()
    first = task["steps"][0]
    assert first_response.headers["etag"] == '"2"'
    assert first["assignee_member_id"] == OWNER_MEMBER_ID
    assert first["depends_on_step_ids"] == []
    assert first["schedule_id"] is None
    with client.app.state.workspace_service.database.connect() as connection:
        stored_mutation = connection.execute(
            "SELECT client_mutation_id FROM secretary_task_steps WHERE id = ?",
            (first["id"],),
        ).fetchone()
        assert stored_mutation["client_mutation_id"] == "p1-client-step-first"
    assert client.post(path, headers=headers, json=payload).json() == task
    assert (
        client.post(
            path,
            headers=headers,
            json={**payload, "client_mutation_id": "p1-client-step-different"},
        ).status_code
        == 409
    )
    second_response = client.post(
        path,
        headers=write_headers(owner_headers, "p1-step-second", if_match=2),
        json={
            "expected_version": 2,
            "step_type": "key_result",
            "title": "形成输出",
            "success_metric": {"target": 1},
            "depends_on_step_ids": [first["id"]],
        },
    )
    assert second_response.status_code == 201, second_response.text
    task = second_response.json()
    second = task["steps"][1]
    assert second["depends_on_step_ids"] == [first["id"]]
    patched = client.patch(
        f"{path}/{second['id']}",
        headers=write_headers(owner_headers, "p1-step-patch", if_match=3),
        json={"title": "形成一份输出"},
    )
    assert patched.status_code == 200, patched.text
    task = patched.json()
    second = next(item for item in task["steps"] if item["id"] == second["id"])
    assert second["version"] == 2
    assert (
        client.patch(
            f"{path}/{first['id']}",
            headers=write_headers(owner_headers, "p1-step-cycle", if_match=4),
            json={"depends_on_step_ids": [second["id"]]},
        ).status_code
        == 422
    )
    reordered = client.post(
        f"{path}/reorder",
        headers=write_headers(owner_headers, "p1-step-reorder", if_match=4),
        json={
            "expected_version": 4,
            "step_ids": [second["id"], first["id"]],
        },
    )
    assert reordered.status_code == 200, reordered.text
    task = reordered.json()
    assert [item["id"] for item in task["steps"]] == [second["id"], first["id"]]
    assert [item["position"] for item in task["steps"]] == [0, 1]
    assert [item["version"] for item in task["steps"]] == [3, 2]
    event_count_before_stale = len(
        client.get(f"{WORKSPACE_PATH}/sync?after=0", headers=owner_headers).json()[
            "changes"
        ]
    )
    stale_version = task["version"] - 1
    stale = client.post(
        path,
        headers=write_headers(owner_headers, "p1-step-stale", if_match=stale_version),
        json={"expected_version": stale_version, "title": "陈旧写入不能落库"},
    )
    assert stale.status_code == 412
    persisted = client.get(f"{WORKSPACE_PATH}/tasks", headers=owner_headers).json()[
        "items"
    ][0]
    assert persisted["version"] == task["version"]
    assert len(persisted["steps"]) == 2
    assert (
        len(
            client.get(f"{WORKSPACE_PATH}/sync?after=0", headers=owner_headers).json()[
                "changes"
            ]
        )
        == event_count_before_stale
    )
    with client.app.state.workspace_service.database.connect() as connection:
        assert (
            connection.execute(
                """
                SELECT 1 FROM secretary_workspace_idempotency
                WHERE operation = ? AND idempotency_key = ?
                """,
                (f"task.step.create:{task['id']}", "p1-step-stale"),
            ).fetchone()
            is None
        )

    other_task = create_task(
        client,
        owner_headers,
        idempotency_key="p1-step-other-task",
        payload=task_payload(title="其他任务", with_steps=True),
    )
    foreign_step_id = other_task["steps"][0]["id"]
    for key, relation in (
        ("parent_step_id", foreign_step_id),
        ("depends_on_step_ids", [foreign_step_id]),
    ):
        rejected = client.post(
            path,
            headers=write_headers(
                owner_headers,
                f"p1-step-cross-{key}",
                if_match=task["version"],
            ),
            json={
                "expected_version": task["version"],
                "title": f"跨任务 {key}",
                key: relation,
            },
        )
        assert rejected.status_code == 422
    event_types = [
        item["event_type"]
        for item in client.get(
            f"{WORKSPACE_PATH}/sync?after=0", headers=owner_headers
        ).json()["changes"]
    ]
    assert event_types.count("task.step_created") == 2
    assert event_types.count("task.step_metadata_updated") == 1
    assert event_types.count("task.steps_reordered") == 1


def test_p1_step_schedule_upsert_status_attention_and_public_bypass(
    client: TestClient, owner_headers: dict[str, str]
) -> None:
    task = create_task(
        client,
        owner_headers,
        idempotency_key="p1-schedule-task",
        payload=task_payload(title="主人自办排期", with_steps=True),
    )
    task = transition_task(client, owner_headers, task, "issued", "p1-schedule-issue")
    task = transition_task(
        client, owner_headers, task, "in_progress", "p1-schedule-start"
    )
    step = task["steps"][0]
    path = f"{WORKSPACE_PATH}/tasks/{task['id']}/steps/{step['id']}/schedule"
    payload = {
        "expected_version": task["version"],
        "title": "专注处理主人任务",
        "start_at": "2026-08-02T08:00:00+08:00",
        "end_at": "2026-08-02T09:00:00+08:00",
        "timezone": "Asia/Shanghai",
        "kind": "focus",
    }
    headers = write_headers(
        owner_headers, "p1-schedule-create", if_match=task["version"]
    )
    scheduled = client.put(path, headers=headers, json=payload)
    assert scheduled.status_code == 200, scheduled.text
    result = scheduled.json()
    task = result["task"]
    calendar = result["calendar_entry"]
    current_step = next(item for item in task["steps"] if item["id"] == step["id"])
    assert calendar["step_id"] == step["id"]
    assert current_step["schedule_id"] == calendar["id"]
    assert current_step["status"] == "pending"
    assert current_step["version"] == step["version"] + 1
    assert client.put(path, headers=headers, json=payload).json() == result
    assert (
        client.put(
            path,
            headers=headers,
            json={**payload, "title": "同键不同排期"},
        ).status_code
        == 409
    )

    attention = client.app.state.workspace_service.task_attention(
        "ws_default", as_of=datetime(2026, 8, 3, tzinfo=UTC)
    )
    item = next(value for value in attention["items"] if value["task_id"] == task["id"])
    assert "schedule_missed" in {reason["code"] for reason in item["reasons"]}
    assert (
        client.patch(
            f"{WORKSPACE_PATH}/calendar/{calendar['id']}",
            headers=write_headers(
                owner_headers, "p1-calendar-bypass-patch", if_match=calendar["version"]
            ),
            json={"title": "禁止绕过"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"{WORKSPACE_PATH}/calendar/{calendar['id']}/cancel",
            headers=write_headers(owner_headers, "p1-calendar-bypass-cancel"),
            json={"expected_version": calendar["version"]},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"{WORKSPACE_PATH}/calendar",
            headers=write_headers(owner_headers, "p1-calendar-bypass-create"),
            json={
                "domain": "work",
                "title": "禁止直接关联步骤",
                "start_at": "2026-08-04T08:00:00+08:00",
                "end_at": "2026-08-04T09:00:00+08:00",
                "timezone": "Asia/Shanghai",
                "step_id": step["id"],
            },
        ).status_code
        == 422
    )
    moved = client.put(
        path,
        headers=write_headers(
            owner_headers, "p1-schedule-move", if_match=task["version"]
        ),
        json={
            **payload,
            "expected_version": task["version"],
            "start_at": "2026-08-04T08:00:00+08:00",
            "end_at": "2026-08-04T09:00:00+08:00",
        },
    )
    assert moved.status_code == 200, moved.text
    moved_result = moved.json()
    assert moved_result["calendar_entry"]["id"] == calendar["id"]
    assert moved_result["calendar_entry"]["version"] == calendar["version"] + 1
    task = moved_result["task"]
    completed_headers = write_headers(
        owner_headers, "p1-schedule-complete", if_match=task["version"]
    )
    completed_payload = {
        "expected_version": task["version"],
        "target_status": "completed",
    }
    completed = client.post(
        f"{path}/status",
        headers=completed_headers,
        json=completed_payload,
    )
    assert completed.status_code == 200, completed.text
    completed_result = completed.json()
    completed_step = next(
        item for item in completed_result["task"]["steps"] if item["id"] == step["id"]
    )
    assert completed_result["calendar_entry"]["status"] == "completed"
    assert completed_step["schedule_id"] is None
    assert completed_step["status"] == "pending"
    assert (
        client.post(
            f"{path}/status",
            headers=completed_headers,
            json=completed_payload,
        ).json()
        == completed_result
    )
    assert (
        client.post(
            f"{path}/status",
            headers=completed_headers,
            json={**completed_payload, "target_status": "canceled"},
        ).status_code
        == 409
    )
    attention = client.app.state.workspace_service.task_attention(
        "ws_default", as_of=datetime(2026, 8, 3, tzinfo=UTC)
    )
    assert not any(
        value["task_id"] == task["id"]
        and any(reason["code"] == "schedule_missed" for reason in value["reasons"])
        for value in attention["items"]
    )

    relevant = [
        event["event_type"]
        for event in client.get(
            f"{WORKSPACE_PATH}/sync?after=0", headers=owner_headers
        ).json()["changes"]
        if event["event_type"].startswith("calendar.")
        or event["event_type"].startswith("task.step_schedule")
        or event["event_type"] == "task.step_scheduled"
    ]
    assert relevant == [
        "calendar.created",
        "task.step_scheduled",
        "calendar.updated",
        "task.step_scheduled",
        "calendar.completed",
        "task.step_schedule_status_changed",
    ]


def test_p1_step_commands_reject_external_plans_terminal_tasks_and_non_leaf_schedule(
    client: TestClient, owner_headers: dict[str, str]
) -> None:
    external_task, _ = create_external_issued_task(
        client, owner_headers, suffix="p1-step-external-task"
    )
    assert (
        client.post(
            f"{WORKSPACE_PATH}/tasks/{external_task['id']}/steps",
            headers=write_headers(
                owner_headers,
                "p1-external-task-step",
                if_match=external_task["version"],
            ),
            json={
                "expected_version": external_task["version"],
                "title": "不能伪装外部承办",
            },
        ).status_code
        == 409
    )


def test_workspace_step_migration_replays_with_existing_position_index_and_syncs_repairs(
    client: TestClient, owner_headers: dict[str, str]
) -> None:
    task = create_task(
        client,
        owner_headers,
        idempotency_key="p1-migration-task",
        payload=task_payload(title="步骤迁移回放", with_steps=True),
    )
    step = task["steps"][0]
    schedule = client.put(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/steps/{step['id']}/schedule",
        headers=write_headers(owner_headers, "p1-migration-schedule", if_match=1),
        json={
            "expected_version": 1,
            "title": "原活动排期",
            "start_at": "2026-08-04T08:00:00+08:00",
            "end_at": "2026-08-04T09:00:00+08:00",
            "timezone": "Asia/Shanghai",
        },
    )
    assert schedule.status_code == 200, schedule.text
    task = schedule.json()["task"]
    original_calendar = schedule.json()["calendar_entry"]
    service = client.app.state.workspace_service
    replacement_id = "calendar_p1_migration_newest"
    with service.database.transaction() as connection:
        connection.execute("DROP INDEX idx_secretary_calendar_active_step_unique")
        connection.execute(
            """
            UPDATE secretary_task_steps
            SET position = CASE id WHEN ? THEN 2000000000 ELSE 2000000001 END
            WHERE task_id = ?
            """,
            (task["steps"][0]["id"], task["id"]),
        )
        connection.execute(
            """
            INSERT INTO secretary_calendar_entries(
                id, workspace_id, memo_id, task_id, step_id, title, description,
                start_at_utc, end_at_utc, timezone, all_day, kind, domain, status,
                attendees_json, external_provider, external_id, version,
                created_by, updated_by, client_mutation_id, created_at, updated_at,
                deleted_at
            )
            SELECT ?, workspace_id, memo_id, task_id, step_id, '最新活动排期',
                   description, start_at_utc, end_at_utc, timezone, all_day, kind,
                   domain, status, attendees_json, external_provider, external_id,
                   version, created_by, updated_by, client_mutation_id, created_at,
                   '2999-01-01T00:00:00Z', deleted_at
            FROM secretary_calendar_entries WHERE id = ?
            """,
            (replacement_id, original_calendar["id"]),
        )
        connection.execute(
            "ALTER TABLE secretary_task_steps DROP COLUMN client_mutation_id"
        )
        # Rehearse the v1/v2 repair only. Newer protocol migrations are
        # intentionally fail-closed when their objects exist without markers.
        connection.execute(
            "DELETE FROM secretary_workspace_schema_migrations "
            "WHERE version IN (1, 2)"
        )
        cursor_before = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM secretary_workspace_events"
        ).fetchone()[0]

    service.initialize()
    with service.database.connect() as connection:
        marker = connection.execute(
            "SELECT version FROM secretary_workspace_schema_migrations ORDER BY version"
        ).fetchall()
        assert [row["version"] for row in marker] == [1, 2, 3, 4, 5, 6, 7]
        step_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(secretary_task_steps)"
            ).fetchall()
        }
        assert "client_mutation_id" in step_columns
        positions = connection.execute(
            """
            SELECT position FROM secretary_task_steps
            WHERE task_id = ? ORDER BY position
            """,
            (task["id"],),
        ).fetchall()
        assert [row["position"] for row in positions] == [0, 1]
        schedules = connection.execute(
            """
            SELECT id, status FROM secretary_calendar_entries
            WHERE step_id = ? ORDER BY id
            """,
            (step["id"],),
        ).fetchall()
        assert {row["id"]: row["status"] for row in schedules} == {
            original_calendar["id"]: "canceled",
            replacement_id: "scheduled",
        }
        migrated_task = connection.execute(
            "SELECT version FROM secretary_business_tasks WHERE id = ?",
            (task["id"],),
        ).fetchone()
        assert migrated_task["version"] == task["version"] + 1
        migration_events = connection.execute(
            """
            SELECT event_type, actor_type, payload_json
            FROM secretary_workspace_events
            WHERE sequence > ? ORDER BY sequence
            """,
            (cursor_before,),
        ).fetchall()
        assert [row["event_type"] for row in migration_events] == [
            "calendar.canceled",
            "task.step_schedule_status_changed",
        ]
        assert all(row["actor_type"] == "system" for row in migration_events)
        assert json.loads(migration_events[0]["payload_json"])["status"] == "canceled"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        event_count = connection.execute(
            "SELECT COUNT(*) FROM secretary_workspace_events"
        ).fetchone()[0]

    service.initialize()
    with service.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM secretary_workspace_events"
            ).fetchone()[0]
            == event_count
        )


def test_p1_step_commands_reject_mixed_plans_terminal_tasks_and_non_leaf_schedule(
    client: TestClient, owner_headers: dict[str, str]
) -> None:
    task = create_task(
        client,
        owner_headers,
        idempotency_key="p1-mixed-plan-task",
        payload=task_payload(title="混合承办计划", with_steps=True),
    )
    first, second = task["steps"]
    service = client.app.state.workspace_service
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    with service.database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO secretary_workspace_members(
                id, workspace_id, kind, role, display_name, active,
                created_at, updated_at
            ) VALUES ('member-p1-external-step', 'ws_default', 'person', 'member',
                      '外部步骤承办人', 1, ?, ?)
            """,
            (now, now),
        )
        connection.execute(
            """
            UPDATE secretary_task_steps
            SET assignee_member_id = 'member-p1-external-step',
                assignee_label = '外部步骤承办人'
            WHERE id = ?
            """,
            (second["id"],),
        )
    assert (
        client.patch(
            f"{WORKSPACE_PATH}/tasks/{task['id']}/steps/{first['id']}",
            headers=write_headers(owner_headers, "p1-mixed-patch", if_match=1),
            json={"title": "不能编辑混合计划"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"{WORKSPACE_PATH}/tasks/{task['id']}/steps/reorder",
            headers=write_headers(owner_headers, "p1-mixed-reorder", if_match=1),
            json={
                "expected_version": 1,
                "step_ids": [second["id"], first["id"]],
            },
        ).status_code
        == 409
    )

    leaf_task = create_task(
        client,
        owner_headers,
        idempotency_key="p1-non-leaf-task",
        payload=task_payload(title="非叶子排期", with_steps=True),
    )
    parent, child = leaf_task["steps"]
    with service.database.transaction() as connection:
        connection.execute(
            "UPDATE secretary_task_steps SET parent_step_id = ? WHERE id = ?",
            (parent["id"], child["id"]),
        )
    schedule = client.put(
        f"{WORKSPACE_PATH}/tasks/{leaf_task['id']}/steps/{parent['id']}/schedule",
        headers=write_headers(owner_headers, "p1-non-leaf-schedule", if_match=1),
        json={
            "expected_version": 1,
            "title": "不能安排非叶子",
            "start_at": "2026-08-04T08:00:00+08:00",
            "end_at": "2026-08-04T09:00:00+08:00",
            "timezone": "Asia/Shanghai",
        },
    )
    assert schedule.status_code == 409
    with service.database.transaction() as connection:
        connection.execute(
            "UPDATE secretary_business_tasks SET stage = 'accepted' WHERE id = ?",
            (leaf_task["id"],),
        )
    assert (
        client.post(
            f"{WORKSPACE_PATH}/tasks/{leaf_task['id']}/steps",
            headers=write_headers(owner_headers, "p1-terminal-step", if_match=1),
            json={"expected_version": 1, "title": "终态不能新增"},
        ).status_code
        == 409
    )
