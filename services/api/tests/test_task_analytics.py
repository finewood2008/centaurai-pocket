from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient

WORKSPACE_ID = "ws_default"
WORKSPACE_PATH = f"/api/v1/workspaces/{WORKSPACE_ID}"
OWNER_MEMBER_ID = "member_owner"
OWNER_DEVICE_ID = "task-analysis-owner-device"

SECURITY_EVENT_TYPES = {
    "task.alignment_invitation_created",
    "task.agreement_session_issued",
    "task.execution_invitation_created",
    "task.execution_session_issued",
}


def _write_headers(
    owner_headers: dict[str, str],
    key: str,
    *,
    version: int | None = None,
) -> dict[str, str]:
    headers = {
        **owner_headers,
        "X-Device-ID": OWNER_DEVICE_ID,
        "Idempotency-Key": key,
    }
    if version is not None:
        headers["If-Match"] = f'"{version}"'
    return headers


def _task_payload(
    *,
    title: str,
    assignee_member_id: str = OWNER_MEMBER_ID,
    due_at: str = "2026-08-10T10:00:00+08:00",
    steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "domain": "work",
        "title": title,
        "purpose": "验证任务分析",
        "objective": "形成可核对的期间统计",
        "strategy": "按任务事实和事件保证等级分别聚合。",
        "key_points": ["事实与归属分离"],
        "acceptance_criteria": ["统计口径可复核"],
        "issuer_member_id": OWNER_MEMBER_ID,
        "assignee_member_id": assignee_member_id,
        "acceptance_owner_id": OWNER_MEMBER_ID,
        "priority": "normal",
        "health": "on_track",
        "due_at": due_at,
        "steps": steps or [],
        "source": {"source_kind": "manual", "authority": "user_provided"},
    }


def _create_task(
    client: TestClient,
    owner_headers: dict[str, str],
    *,
    title: str,
    suffix: str,
    assignee_member_id: str = OWNER_MEMBER_ID,
    due_at: str = "2026-08-10T10:00:00+08:00",
    steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    response = client.post(
        f"{WORKSPACE_PATH}/tasks",
        headers=_write_headers(owner_headers, f"analysis-task-{suffix}"),
        json=_task_payload(
            title=title,
            assignee_member_id=assignee_member_id,
            due_at=due_at,
            steps=steps,
        ),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_external_member(
    client: TestClient,
    owner_headers: dict[str, str],
    suffix: str,
) -> dict[str, Any]:
    response = client.post(
        f"{WORKSPACE_PATH}/members",
        headers=_write_headers(owner_headers, f"analysis-member-{suffix}"),
        json={
            "kind": "external",
            "role": "member",
            "display_name": f"分析承办人-{suffix}",
            "contact_ref": f"wecom://analysis/{suffix}",
            "client_mutation_id": f"analysis-member-local-{suffix}",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _record_owner_change_decision(
    client: TestClient,
    owner_headers: dict[str, str],
    task: dict[str, Any],
) -> dict[str, Any]:
    proposed = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/changes",
        headers=_write_headers(owner_headers, "analysis-change-propose"),
        json={
            "change_type": "due_at",
            "base_version": task["version"],
            "reason": "用真实不可变决定验证 A2",
            "patch": {"due_at": "2026-08-30T18:00:00+08:00"},
            "client_mutation_id": "analysis-change-propose-local",
        },
    )
    assert proposed.status_code == 201, proposed.text
    change_id = proposed.json()["id"]

    protocol_response = client.get(
        f"/api/v1/task-changes/{change_id}",
        headers=owner_headers,
    )
    assert protocol_response.status_code == 200, protocol_response.text
    protocol = protocol_response.json()
    decided = client.post(
        f"/api/v1/task-changes/{change_id}/decisions",
        headers=_write_headers(
            owner_headers,
            "analysis-change-accept",
            version=protocol["version"],
        ),
        json={
            "expected_change_version": protocol["version"],
            "expected_task_version": protocol["task"]["version"],
            "proposal_digest": protocol["proposal"]["digest"],
            "decision": "accept",
            "reason": None,
            "client_mutation_id": "analysis-change-accept-local",
        },
    )
    assert decided.status_code == 200, decided.text
    result = decided.json()
    assert result["decision"]["assurance_method"] == "owner_token"
    return result


def _create_aligned_external_task(
    client: TestClient,
    owner_headers: dict[str, str],
    suffix: str,
    *,
    due_at: str = "2026-08-10T10:00:00+08:00",
) -> tuple[dict[str, Any], dict[str, Any]]:
    assignee = _create_external_member(client, owner_headers, suffix)
    task = _create_task(
        client,
        owner_headers,
        title="真实 v7 外部执行任务",
        suffix=suffix,
        assignee_member_id=assignee["id"],
        due_at=due_at,
    )
    issued = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/transitions",
        headers=_write_headers(owner_headers, f"analysis-issue-{suffix}"),
        json={"target_stage": "issued", "expected_version": task["version"]},
    )
    assert issued.status_code == 200, issued.text
    task = issued.json()

    invitation_response = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/alignment-invitations",
        headers=_write_headers(
            owner_headers,
            f"analysis-alignment-invite-{suffix}",
        ),
        json={"expected_version": task["version"]},
    )
    assert invitation_response.status_code == 201, invitation_response.text
    invitation = invitation_response.json()
    exchange_response = client.post(
        "/api/v1/task-alignments/exchange",
        headers={"Idempotency-Key": f"analysis-alignment-exchange-{suffix}"},
        json={
            "invitation_id": invitation["invitation_id"],
            "code": invitation["code"],
            "client_device_id": f"analysis-alignment-device-{suffix}",
        },
    )
    assert exchange_response.status_code == 200, exchange_response.text
    exchange = exchange_response.json()
    agreement = exchange["agreement"]
    revision = agreement["current_revision"]
    accepted = client.post(
        f"/api/v1/task-agreements/{agreement['id']}/responses",
        headers={
            "Authorization": f"Bearer {exchange['access_token']}",
            "X-Device-ID": exchange["session"]["client_device_id"],
            "Idempotency-Key": f"analysis-alignment-accept-{suffix}",
            "If-Match": f'"{agreement["version"]}"',
        },
        json={
            "expected_agreement_version": agreement["version"],
            "revision_id": revision["id"],
            "expected_digest": revision["digest"],
            "action": "accept",
            "reason": None,
            "counter_document": None,
            "client_mutation_id": f"analysis-alignment-accept-local-{suffix}",
        },
    )
    assert accepted.status_code == 200, accepted.text

    listed = client.get(f"{WORKSPACE_PATH}/tasks", headers=owner_headers)
    assert listed.status_code == 200, listed.text
    aligned = next(
        item for item in listed.json()["items"] if item["id"] == task["id"]
    )
    assert aligned["stage"] == "aligned"
    return aligned, assignee


def _create_pending_external_agreement(
    client: TestClient,
    owner_headers: dict[str, str],
    suffix: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    assignee = _create_external_member(client, owner_headers, suffix)
    task = _create_task(
        client,
        owner_headers,
        title="等待反提案的外部任务",
        suffix=suffix,
        assignee_member_id=assignee["id"],
    )
    issued = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/transitions",
        headers=_write_headers(owner_headers, f"analysis-counter-issue-{suffix}"),
        json={"target_stage": "issued", "expected_version": task["version"]},
    )
    assert issued.status_code == 200, issued.text
    task = issued.json()
    invitation_response = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/alignment-invitations",
        headers=_write_headers(
            owner_headers,
            f"analysis-counter-invite-{suffix}",
        ),
        json={"expected_version": task["version"]},
    )
    assert invitation_response.status_code == 201, invitation_response.text
    invitation = invitation_response.json()
    exchange_response = client.post(
        "/api/v1/task-alignments/exchange",
        headers={"Idempotency-Key": f"analysis-counter-exchange-{suffix}"},
        json={
            "invitation_id": invitation["invitation_id"],
            "code": invitation["code"],
            "client_device_id": f"analysis-counter-device-{suffix}",
        },
    )
    assert exchange_response.status_code == 200, exchange_response.text
    return task, assignee, exchange_response.json()


def _start_real_execution(
    client: TestClient,
    owner_headers: dict[str, str],
    task: dict[str, Any],
    suffix: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    invitation_response = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/execution-invitations",
        headers=_write_headers(
            owner_headers,
            f"analysis-execution-invite-{suffix}",
            version=task["version"],
        ),
        json={"expected_task_version": task["version"]},
    )
    assert invitation_response.status_code == 201, invitation_response.text
    invitation = invitation_response.json()
    exchange_response = client.post(
        "/api/v1/task-executions/exchange",
        headers={"Idempotency-Key": f"analysis-execution-exchange-{suffix}"},
        json={
            "invitation_id": invitation["invitation_id"],
            "code": invitation["code"],
            "client_device_id": f"analysis-execution-device-{suffix}",
        },
    )
    assert exchange_response.status_code == 200, exchange_response.text
    exchange = exchange_response.json()
    access_headers = {
        "Authorization": f"Bearer {exchange['access_token']}",
        "X-Device-ID": exchange["session"]["client_device_id"],
    }
    view_response = client.get(
        f"/api/v1/task-executions/{task['id']}",
        headers=access_headers,
    )
    assert view_response.status_code == 200, view_response.text
    view = view_response.json()
    started = client.post(
        f"/api/v1/task-executions/{task['id']}/start",
        headers={
            **access_headers,
            "Idempotency-Key": f"analysis-execution-start-{suffix}",
            "If-Match": view_response.headers["etag"],
        },
        json={
            "expected_task_version": view["version"],
            "client_mutation_id": f"analysis-execution-start-local-{suffix}",
            "note": "真实执行会话启动",
        },
    )
    assert started.status_code == 200, started.text
    assert started.json()["task"]["stage"] == "in_progress"
    return exchange, started.json()["task"]


def _record_real_execution_checkin(
    client: TestClient,
    task_id: str,
    exchange: dict[str, Any],
    suffix: str,
) -> dict[str, Any]:
    access_headers = {
        "Authorization": f"Bearer {exchange['access_token']}",
        "X-Device-ID": exchange["session"]["client_device_id"],
    }
    view_response = client.get(
        f"/api/v1/task-executions/{task_id}",
        headers=access_headers,
    )
    assert view_response.status_code == 200, view_response.text
    view = view_response.json()
    response = client.post(
        f"/api/v1/task-executions/{task_id}/check-ins",
        headers={
            **access_headers,
            "Idempotency-Key": f"analysis-execution-checkin-{suffix}",
            "If-Match": view_response.headers["etag"],
        },
        json={
            "expected_task_version": view["version"],
            "summary": "真实执行会话复盘",
            "reported_progress": 25,
            "risks": [],
            "blockers": [],
            "next_actions": ["继续验证"],
            "forecast_at": None,
            "client_mutation_id": f"analysis-execution-checkin-local-{suffix}",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _insert_execution_claim_event(
    connection: Any,
    *,
    event_id: str,
    task_id: str,
    event_type: str,
    payload: dict[str, Any],
    occurred_at: str,
    device_id: str,
    aggregate_type: str = "task",
    aggregate_id: str | None = None,
    aggregate_version: int = 2,
    actor_type: str = "system",
    actor_member_id: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO secretary_workspace_events(
            event_id, workspace_id, aggregate_type, aggregate_id,
            aggregate_version, event_type, operation, actor_type,
            actor_member_id, device_id, payload_json, occurred_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'upsert', ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            WORKSPACE_ID,
            aggregate_type,
            aggregate_id or task_id,
            aggregate_version,
            event_type,
            actor_type,
            actor_member_id,
            device_id,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            occurred_at,
        ),
    )


def _clone_event(
    connection: Any,
    source: Any,
    *,
    event_id: str,
    payload: dict[str, Any] | None = None,
    event_type: str | None = None,
    aggregate_type: str | None = None,
    aggregate_id: str | None = None,
    occurred_at: str | None = None,
) -> None:
    source_payload = json.loads(source["payload_json"])
    _insert_execution_claim_event(
        connection,
        event_id=event_id,
        task_id=str(source["aggregate_id"]),
        event_type=event_type or str(source["event_type"]),
        payload=payload or source_payload,
        occurred_at=occurred_at or str(source["occurred_at"]),
        device_id=str(source["device_id"]),
        aggregate_type=aggregate_type or str(source["aggregate_type"]),
        aggregate_id=aggregate_id or str(source["aggregate_id"]),
        aggregate_version=int(source["aggregate_version"]),
        actor_type=str(source["actor_type"]),
        actor_member_id=source["actor_member_id"],
    )


def _action(item: dict[str, Any], action: str) -> dict[str, Any]:
    return next(
        bucket
        for bucket in item["attribution_evidence"]["actions"]
        if bucket["action"] == action
    )


def _database_dump(client: TestClient) -> str:
    service = client.app.state.workspace_service
    with service.database.connect() as connection:
        return "\n".join(connection.iterdump())


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def test_task_analysis_separates_facts_and_verified_attribution(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    service = client.app.state.workspace_service
    accepted_task = _create_task(
        client,
        owner_headers,
        title="按期验收任务",
        suffix="accepted",
    )
    owner_decision = _record_owner_change_decision(
        client,
        owner_headers,
        accepted_task,
    )
    assert owner_decision["decision"]["id"]

    external_task, external_member = _create_aligned_external_task(
        client,
        owner_headers,
        "external",
    )
    execution, _started_projection = _start_real_execution(
        client,
        owner_headers,
        external_task,
        "external",
    )
    before_period_task = _create_task(
        client,
        owner_headers,
        title="期间前已终结任务",
        suffix="before-period",
    )

    with service.database.transaction() as connection:
        connection.execute(
            """
            UPDATE secretary_business_tasks
            SET stage = 'accepted', progress = 100,
                started_at = '2026-08-01T00:00:00Z',
                accepted_at = '2026-08-02T02:00:00Z',
                created_at = '2026-07-31T17:00:00Z',
                updated_at = '2026-08-02T02:00:00Z'
            WHERE id = ?
            """,
            (accepted_task["id"],),
        )
        connection.execute(
            """
            UPDATE secretary_business_tasks
            SET health = 'at_risk'
            WHERE id = ?
            """,
            (external_task["id"],),
        )
        connection.execute(
            """
            UPDATE secretary_business_tasks
            SET stage = 'accepted', progress = 100,
                due_at = '2026-07-09T02:00:00Z',
                started_at = '2026-07-05T02:00:00Z',
                accepted_at = '2026-07-10T02:00:00Z',
                created_at = '2026-07-01T00:00:00Z',
                updated_at = '2026-07-10T02:00:00Z'
            WHERE id = ?
            """,
            (before_period_task["id"],),
        )

        real_start = connection.execute(
            """
            SELECT * FROM secretary_workspace_events
            WHERE workspace_id = ? AND aggregate_id = ?
              AND event_type = 'task.execution_started'
            """,
            (WORKSPACE_ID, external_task["id"]),
        ).fetchone()
        assert real_start is not None
        valid_payload = json.loads(real_start["payload_json"])

        orphan_payload = dict(valid_payload)
        orphan_payload.update(
            {
                "actor_session_id": "execution_session_orphan",
                "actor_subject_id": "execution_session_orphan",
                "refresh_family_id": "execution_family_orphan",
                "on_behalf_of_member_id": "member_payload_injected",
            }
        )
        _insert_execution_claim_event(
            connection,
            event_id="evt_analysis_orphan_execution",
            task_id=external_task["id"],
            event_type="task.execution_checkin_recorded",
            payload=orphan_payload,
            occurred_at=real_start["occurred_at"],
            device_id=real_start["device_id"],
        )

        misbound_payload = dict(valid_payload)
        misbound_payload["assignment_epoch"] += 1
        _insert_execution_claim_event(
            connection,
            event_id="evt_analysis_misbound_execution",
            task_id=external_task["id"],
            event_type="task.execution_step_updated",
            payload=misbound_payload,
            occurred_at=real_start["occurred_at"],
            device_id=real_start["device_id"],
        )

        security_rows = connection.execute(
            """
            SELECT event_type FROM secretary_workspace_events
            WHERE workspace_id = ? AND event_type IN (?, ?, ?, ?)
            """,
            (WORKSPACE_ID, *sorted(SECURITY_EVENT_TYPES)),
        ).fetchall()
    assert {row["event_type"] for row in security_rows} == SECURITY_EVENT_TYPES

    dump_before = _database_dump(client)
    response = client.get(
        f"{WORKSPACE_PATH}/task-analysis?from=2026-08-01&to=2026-08-31",
        headers=owner_headers,
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert _database_dump(client) == dump_before

    result = response.json()
    assert result["schema"] == "centaur.task-analysis.v1"
    assert result["period"] == {
        "from": "2026-08-01",
        "to": "2026-08-31",
        "start_at": "2026-07-31T16:00:00Z",
        "end_exclusive_at": "2026-08-31T16:00:00Z",
        "workspace_timezone": "Asia/Shanghai",
        "task_scope": "current_assignment_overlapping_period",
    }
    assert result["task_facts"] == {
        "total_tasks": 2,
        "open_tasks": 1,
        "accepted_tasks": 1,
        "accepted_on_time_tasks": 1,
        "accepted_late_tasks": 0,
        "accepted_without_due_tasks": 0,
        "abnormal_closed_tasks": 0,
        "overdue_open_tasks": 1,
        "current_risk_tasks": 1,
        "rework_event_count": 0,
        "median_start_to_accept_seconds": 93_600,
        "start_to_accept_sample_count": 1,
    }

    assert result["coverage"] == {
        "events_considered": 5,
        "resolved_typed_events": 3,
        "a0_unknown_events": 2,
        "excluded_security_events": 4,
        "integrity_mismatch_events": 2,
        "domain_rows_without_event": 0,
        "strong_member_identity_supported": False,
        "limitation": result["coverage"]["limitation"],
    }
    assert "不证明自然人本人操作" in result["coverage"]["limitation"]

    policy = result["assurance_policy"]
    assert policy["id"] == "centaur.task-attribution-assurance"
    assert policy["version"] == 1
    assert [
        (level["level"], level["tier"], level["weight_basis_points"])
        for level in policy["levels"]
    ] == [
        ("A2", "a2_owner_control", 10_000),
        ("A1", "a1_capability", 5_000),
        ("A0", "a0_unknown", 0),
    ]
    assert "不是身份概率或绩效分" in policy["warning"]
    assert all(level["person_identity_verified"] is False for level in policy["levels"])

    tasks = {item["task_id"]: item for item in result["tasks"]}
    assert set(tasks) == {accepted_task["id"], external_task["id"]}
    accepted = tasks[accepted_task["id"]]
    assert accepted["period_outcome"] == "accepted_on_time"
    owner_change = _action(accepted, "change_response")
    assert owner_change == {
        "action": "change_response",
        "raw_event_count": 1,
        "classified_event_count": 1,
        "weighted_event_basis_points": 10_000,
        "by_tier": {
            "a2_owner_control": 1,
            "a1_capability": 0,
            "a0_unknown": 0,
        },
        "assignment_epoch_count": 0,
    }

    external = tasks[external_task["id"]]
    assert external["period_outcome"] == "open_overdue"
    external_start = _action(external, "start")
    assert external_start["raw_event_count"] == 1
    assert external_start["classified_event_count"] == 1
    assert external_start["weighted_event_basis_points"] == 5_000
    assert external_start["by_tier"] == {
        "a2_owner_control": 0,
        "a1_capability": 1,
        "a0_unknown": 0,
    }
    assert external_start["assignment_epoch_count"] == 1

    agreement = _action(external, "agreement_response")
    assert agreement["by_tier"]["a1_capability"] == 1
    assert agreement["weighted_event_basis_points"] == 5_000
    orphan = _action(external, "checkin")
    assert orphan["raw_event_count"] == 1
    assert orphan["classified_event_count"] == 0
    assert orphan["by_tier"]["a0_unknown"] == 1
    assert orphan["weighted_event_basis_points"] == 0
    assert orphan["assignment_epoch_count"] == 0
    misbound = _action(external, "step_status")
    assert misbound["raw_event_count"] == 1
    assert misbound["classified_event_count"] == 0
    assert misbound["by_tier"]["a0_unknown"] == 1
    assert misbound["weighted_event_basis_points"] == 0
    assert misbound["assignment_epoch_count"] == 0

    assignees = {item["member_id"]: item for item in result["assignees"]}
    assert set(assignees) == {OWNER_MEMBER_ID, external_member["id"]}
    assert assignees[OWNER_MEMBER_ID]["current_assignment_snapshot"][
        "current_stage_counts"
    ]["accepted"] == 1
    external_assignee = assignees[external_member["id"]]
    assert external_assignee["current_assignment_snapshot"]["task_ids"] == [
        external_task["id"]
    ]
    assert external_assignee["current_assignment_snapshot"][
        "current_stage_counts"
    ]["in_progress"] == 1
    assert _action(external_assignee, "start") == external_start
    assert _action(external_assignee, "checkin")["raw_event_count"] == 0
    assert _action(external_assignee, "step_status")["raw_event_count"] == 0
    assert "member_payload_injected" not in assignees

    serialized = json.dumps(result, ensure_ascii=False)
    assert execution["access_token"] not in serialized
    assert execution["session"]["id"] not in serialized
    assert execution["session"]["client_device_id"] not in serialized
    assert "wecom://analysis/external" not in serialized
    assert "summary" not in result
    assert "score" not in serialized.lower()
    assert "rank" not in serialized.lower()

    scoped_denied = client.get(
        f"{WORKSPACE_PATH}/task-analysis?from=2026-08-01&to=2026-08-31",
        headers={
            "Authorization": f"Bearer {execution['access_token']}",
            "X-Device-ID": execution["session"]["client_device_id"],
        },
    )
    assert scoped_denied.status_code == 401
    assert scoped_denied.headers["cache-control"] == "no-store, max-age=0"


def test_task_analysis_requires_typed_aggregate_payload_and_domain_bindings(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    service = client.app.state.workspace_service
    owner_task = _create_task(
        client,
        owner_headers,
        title="聚合绑定 Owner 任务",
        suffix="binding-owner",
    )
    _record_owner_change_decision(client, owner_headers, owner_task)
    external_task, external_member = _create_aligned_external_task(
        client,
        owner_headers,
        "binding-external",
    )
    execution, _projection = _start_real_execution(
        client,
        owner_headers,
        external_task,
        "binding-external",
    )
    _record_real_execution_checkin(
        client,
        external_task["id"],
        execution,
        "binding-external",
    )

    with service.database.transaction() as connection:
        owner_change = connection.execute(
            """
            SELECT * FROM secretary_workspace_events
            WHERE event_type = 'task.change_accepted'
              AND aggregate_id IN (
                SELECT id FROM secretary_task_changes WHERE task_id = ?
              )
            """,
            (owner_task["id"],),
        ).fetchone()
        agreement = connection.execute(
            """
            SELECT * FROM secretary_workspace_events
            WHERE event_type = 'task.agreement_accept'
              AND payload_json LIKE ?
            """,
            (f'%"task_id":"{external_task["id"]}"%',),
        ).fetchone()
        execution_start = connection.execute(
            """
            SELECT * FROM secretary_workspace_events
            WHERE event_type = 'task.execution_started' AND aggregate_id = ?
            """,
            (external_task["id"],),
        ).fetchone()
        execution_checkin = connection.execute(
            """
            SELECT * FROM secretary_workspace_events
            WHERE event_type = 'task.execution_checkin_recorded'
              AND json_extract(payload_json, '$.check_in.task_id') = ?
            """,
            (external_task["id"],),
        ).fetchone()
        assert all(
            row is not None
            for row in (
                owner_change,
                agreement,
                execution_start,
                execution_checkin,
            )
        )

        _clone_event(
            connection,
            owner_change,
            event_id="evt_analysis_wrong_change_aggregate",
            aggregate_id="change_wrong_aggregate",
        )
        _clone_event(
            connection,
            agreement,
            event_id="evt_analysis_wrong_agreement_aggregate",
            aggregate_id="agreement_wrong_aggregate",
        )

        conflicting_task_payload = json.loads(execution_start["payload_json"])
        assert conflicting_task_payload["task"]["id"] == external_task["id"]
        conflicting_task_payload["task"]["id"] = owner_task["id"]
        _clone_event(
            connection,
            execution_start,
            event_id="evt_analysis_execution_payload_task_conflict",
            payload=conflicting_task_payload,
        )
        _clone_event(
            connection,
            execution_checkin,
            event_id="evt_analysis_checkin_domain_orphan",
            aggregate_id="checkin_missing_domain_row",
        )

    response = client.get(
        f"{WORKSPACE_PATH}/task-analysis?from=2026-08-01&to=2026-08-31",
        headers=owner_headers,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["coverage"]["events_considered"] == 8
    # A duplicated immutable decision reference is itself an integrity loss:
    # both the genuine event and its wrong-aggregate duplicate fail closed.
    assert result["coverage"]["resolved_typed_events"] == 2
    assert result["coverage"]["a0_unknown_events"] == 6
    assert result["coverage"]["integrity_mismatch_events"] == 6
    assert result["coverage"]["excluded_security_events"] == 4
    assert result["coverage"]["domain_rows_without_event"] == 0

    tasks = {item["task_id"]: item for item in result["tasks"]}
    owner_change_action = _action(tasks[owner_task["id"]], "change_response")
    assert owner_change_action["raw_event_count"] == 2
    assert owner_change_action["classified_event_count"] == 0
    assert owner_change_action["by_tier"] == {
        "a2_owner_control": 0,
        "a1_capability": 0,
        "a0_unknown": 2,
    }
    assert owner_change_action["weighted_event_basis_points"] == 0

    external = tasks[external_task["id"]]
    agreement_action = _action(external, "agreement_response")
    assert agreement_action["raw_event_count"] == 2
    assert agreement_action["classified_event_count"] == 0
    assert agreement_action["by_tier"] == {
        "a2_owner_control": 0,
        "a1_capability": 0,
        "a0_unknown": 2,
    }
    assert agreement_action["weighted_event_basis_points"] == 0
    for action in ("start", "checkin"):
        bucket = _action(external, action)
        assert bucket["raw_event_count"] == 2
        assert bucket["classified_event_count"] == 1
        assert bucket["by_tier"] == {
            "a2_owner_control": 0,
            "a1_capability": 1,
            "a0_unknown": 1,
        }
        assert bucket["weighted_event_basis_points"] == 5_000

    assignees = {item["member_id"]: item for item in result["assignees"]}
    assert _action(assignees[OWNER_MEMBER_ID], "change_response")[
        "raw_event_count"
    ] == 0
    external_assignee = assignees[external_member["id"]]
    assert _action(external_assignee, "agreement_response")[
        "raw_event_count"
    ] == 0
    assert _action(external_assignee, "start")["raw_event_count"] == 1
    assert _action(external_assignee, "checkin")["raw_event_count"] == 1


def test_task_analysis_execution_revocation_is_temporal_and_expiry_is_strict(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    service = client.app.state.workspace_service
    revoked_task, revoked_member = _create_aligned_external_task(
        client,
        owner_headers,
        "revoked",
    )
    _revoked_execution, _projection = _start_real_execution(
        client,
        owner_headers,
        revoked_task,
        "revoked",
    )

    due_at = datetime.now(UTC) - timedelta(days=7) + timedelta(minutes=5)
    expiry_task, expiry_member = _create_aligned_external_task(
        client,
        owner_headers,
        "expiry",
        due_at=_iso(due_at),
    )
    _expiry_execution, _projection = _start_real_execution(
        client,
        owner_headers,
        expiry_task,
        "expiry",
    )

    with service.database.transaction() as connection:
        revoked_start = connection.execute(
            """
            SELECT * FROM secretary_workspace_events
            WHERE event_type = 'task.execution_started' AND aggregate_id = ?
            """,
            (revoked_task["id"],),
        ).fetchone()
        expiry_start = connection.execute(
            """
            SELECT * FROM secretary_workspace_events
            WHERE event_type = 'task.execution_started' AND aggregate_id = ?
            """,
            (expiry_task["id"],),
        ).fetchone()
        assert revoked_start is not None and expiry_start is not None

        revoked_payload = json.loads(revoked_start["payload_json"])
        revoked_at = datetime.fromisoformat(
            str(revoked_start["occurred_at"])
        ) + timedelta(seconds=1)
        after_revoke = revoked_at + timedelta(seconds=1)
        connection.execute(
            """
            UPDATE secretary_task_execution_sessions
            SET revoked_at = ?, revoke_reason = 'analysis_temporal_fixture'
            WHERE id = ?
            """,
            (_iso(revoked_at), revoked_payload["actor_session_id"]),
        )
        connection.execute(
            """
            UPDATE secretary_task_execution_refresh_families
            SET revoked_at = ?, revoke_reason = 'analysis_temporal_fixture'
            WHERE id = ?
            """,
            (_iso(revoked_at), revoked_payload["refresh_family_id"]),
        )
        _clone_event(
            connection,
            revoked_start,
            event_id="evt_analysis_after_execution_revocation",
            event_type="task.execution_checkin_recorded",
            occurred_at=_iso(after_revoke),
        )

        expiry_payload = json.loads(expiry_start["payload_json"])
        expiry_session = connection.execute(
            """
            SELECT * FROM secretary_task_execution_sessions WHERE id = ?
            """,
            (expiry_payload["actor_session_id"],),
        ).fetchone()
        expiry_family = connection.execute(
            """
            SELECT * FROM secretary_task_execution_refresh_families WHERE id = ?
            """,
            (expiry_payload["refresh_family_id"],),
        ).fetchone()
        expiry_task_row = connection.execute(
            "SELECT due_at FROM secretary_business_tasks WHERE id = ?",
            (expiry_task["id"],),
        ).fetchone()
        assert expiry_session is not None and expiry_family is not None
        assert expiry_task_row is not None
        effective_due_expiry = datetime.fromisoformat(
            str(expiry_task_row["due_at"])
        ) + timedelta(days=7)
        assert expiry_family["absolute_expires_at"] == _iso(effective_due_expiry)
        assert expiry_session["expires_at"] == expiry_family["absolute_expires_at"]
        _clone_event(
            connection,
            expiry_start,
            event_id="evt_analysis_at_execution_expiry",
            event_type="task.execution_step_updated",
            occurred_at=str(expiry_session["expires_at"]),
        )

    response = client.get(
        f"{WORKSPACE_PATH}/task-analysis?from=2026-08-01&to=2026-08-31",
        headers=owner_headers,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["coverage"]["events_considered"] == 6
    assert result["coverage"]["resolved_typed_events"] == 4
    assert result["coverage"]["a0_unknown_events"] == 2
    assert result["coverage"]["integrity_mismatch_events"] == 2
    assert result["coverage"]["excluded_security_events"] == 8

    tasks = {item["task_id"]: item for item in result["tasks"]}
    assert _action(tasks[revoked_task["id"]], "start")["by_tier"][
        "a1_capability"
    ] == 1
    revoked_claim = _action(tasks[revoked_task["id"]], "checkin")
    assert revoked_claim["by_tier"]["a0_unknown"] == 1
    assert revoked_claim["assignment_epoch_count"] == 0
    expiry_claim = _action(tasks[expiry_task["id"]], "step_status")
    assert expiry_claim["by_tier"]["a0_unknown"] == 1
    assert expiry_claim["assignment_epoch_count"] == 0

    assignees = {item["member_id"]: item for item in result["assignees"]}
    assert _action(assignees[revoked_member["id"]], "checkin")[
        "raw_event_count"
    ] == 0
    assert _action(assignees[expiry_member["id"]], "step_status")[
        "raw_event_count"
    ] == 0


def test_task_analysis_uses_explicit_step_status_events_not_legacy_metadata(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    service = client.app.state.workspace_service
    task = _create_task(
        client,
        owner_headers,
        title="明确步骤状态事件",
        suffix="step-status",
        steps=[
            {
                "step_type": "action",
                "title": "可执行步骤",
                "assignee_member_id": OWNER_MEMBER_ID,
                "position": 0,
            }
        ],
    )
    step = task["steps"][0]
    updated = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/steps/{step['id']}",
        headers=_write_headers(
            owner_headers,
            "analysis-step-status-set",
            version=task["version"],
        ),
        json={
            "expected_version": task["version"],
            "status": "in_progress",
            "note": "开始执行明确步骤",
        },
    )
    assert updated.status_code == 200, updated.text

    with service.database.transaction() as connection:
        status_event = connection.execute(
            """
            SELECT * FROM secretary_workspace_events
            WHERE aggregate_id = ? AND event_type = 'task.step_status_updated'
            """,
            (task["id"],),
        ).fetchone()
        assert status_event is not None
        _clone_event(
            connection,
            status_event,
            event_id="evt_analysis_legacy_step_updated",
            event_type="task.step_updated",
        )
        _clone_event(
            connection,
            status_event,
            event_id="evt_analysis_legacy_step_schedule_status_changed",
            event_type="task.step_schedule_status_changed",
        )

    response = client.get(
        f"{WORKSPACE_PATH}/task-analysis?from=2026-08-01&to=2026-08-31",
        headers=owner_headers,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["coverage"]["events_considered"] == 1
    assert result["coverage"]["a0_unknown_events"] == 1
    assert result["coverage"]["integrity_mismatch_events"] == 0
    analyzed_task = next(
        item for item in result["tasks"] if item["task_id"] == task["id"]
    )
    step_status = _action(analyzed_task, "step_status")
    assert step_status["raw_event_count"] == 1
    assert step_status["classified_event_count"] == 0
    assert step_status["by_tier"]["a0_unknown"] == 1


def test_task_analysis_counts_checkin_domain_rows_without_events(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    service = client.app.state.workspace_service
    task = _create_task(
        client,
        owner_headers,
        title="缺事件业务行覆盖",
        suffix="domain-gap",
        steps=[
            {
                "step_type": "action",
                "title": "没有状态事件的步骤",
                "assignee_member_id": OWNER_MEMBER_ID,
                "position": 0,
            }
        ],
    )
    with service.database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO secretary_task_checkins(
                id, workspace_id, task_id, task_version, report_date,
                summary, reported_progress, risks_json, blockers_json,
                next_actions_json, forecast_at, created_by, device_id,
                client_mutation_id, version, created_at
            ) VALUES (
                'checkin_analysis_without_event', ?, ?, ?, '2026-08-02',
                '该业务行没有对应审计事件', 10, '[]', '[]', '[]', NULL,
                ?, ?, 'analysis-domain-gap-local', 1, '2026-08-02T03:00:00Z'
            )
            """,
            (
                WORKSPACE_ID,
                task["id"],
                task["version"],
                OWNER_MEMBER_ID,
                OWNER_DEVICE_ID,
            ),
        )

    response = client.get(
        f"{WORKSPACE_PATH}/task-analysis?from=2026-08-01&to=2026-08-31",
        headers=owner_headers,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    # v1 coverage defines this field as check-in domain rows missing their
    # event. A step row alone must not silently widen that denominator.
    assert result["coverage"]["domain_rows_without_event"] == 1
    assert result["coverage"]["events_considered"] == 0
    analyzed_task = next(
        item for item in result["tasks"] if item["task_id"] == task["id"]
    )
    assert all(
        bucket["raw_event_count"] == 0
        for bucket in analyzed_task["attribution_evidence"]["actions"]
    )


def test_task_analysis_classifies_real_owner_cancel_change_decision_as_a2(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    service = client.app.state.workspace_service
    task = _create_task(
        client,
        owner_headers,
        title="Owner 取消变更决定",
        suffix="cancel-change",
    )
    proposed_response = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/changes",
        headers=_write_headers(owner_headers, "analysis-cancel-change-propose"),
        json={
            "change_type": "due_at",
            "base_version": task["version"],
            "reason": "验证取消决定也有不可变证据",
            "patch": {"due_at": "2026-08-30T18:00:00+08:00"},
            "client_mutation_id": "analysis-cancel-change-propose-local",
        },
    )
    assert proposed_response.status_code == 201, proposed_response.text
    change = proposed_response.json()
    canceled = client.post(
        f"{WORKSPACE_PATH}/changes/{change['id']}/decision",
        headers=_write_headers(owner_headers, "analysis-cancel-change"),
        json={
            "decision": "cancel",
            "reason": "Owner 明确撤销本次提案",
            "expected_version": task["version"],
        },
    )
    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["change"]["status"] == "canceled"

    with service.database.connect() as connection:
        decision = connection.execute(
            """
            SELECT * FROM secretary_task_change_decisions WHERE change_id = ?
            """,
            (change["id"],),
        ).fetchone()
        event = connection.execute(
            """
            SELECT * FROM secretary_workspace_events
            WHERE aggregate_type = 'task_change' AND aggregate_id = ?
              AND event_type = 'task.change_canceled'
            """,
            (change["id"],),
        ).fetchone()
    assert decision is not None and event is not None
    event_payload = json.loads(event["payload_json"])
    assert event_payload["decision"]["id"] == decision["id"]
    assert event_payload["decision"]["action"] == "cancel"
    assert event_payload["decision"]["assurance_method"] == "owner_token"

    response = client.get(
        f"{WORKSPACE_PATH}/task-analysis?from=2026-08-01&to=2026-08-31",
        headers=owner_headers,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    analyzed_task = next(
        item for item in result["tasks"] if item["task_id"] == task["id"]
    )
    change_response = _action(analyzed_task, "change_response")
    assert change_response["raw_event_count"] == 1
    assert change_response["classified_event_count"] == 1
    assert change_response["by_tier"]["a2_owner_control"] == 1
    assert change_response["weighted_event_basis_points"] == 10_000


def test_task_analysis_classifies_real_owner_counter_response_as_a2(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    task, assignee, exchange = _create_pending_external_agreement(
        client,
        owner_headers,
        "owner-counter",
    )
    agreement = exchange["agreement"]
    revision = agreement["current_revision"]
    counter_document = {
        **revision["document"],
        "revision_no": 2,
        "parent_digest": revision["digest"],
        "proposer_role": "assignee",
        "proposer_member_id": assignee["id"],
        "responder_role": "issuer",
        "responder_member_id": OWNER_MEMBER_ID,
        "due_at": "2026-08-11T10:00:00Z",
    }
    countered = client.post(
        f"/api/v1/task-agreements/{agreement['id']}/responses",
        headers={
            "Authorization": f"Bearer {exchange['access_token']}",
            "X-Device-ID": exchange["session"]["client_device_id"],
            "Idempotency-Key": "analysis-owner-counter-external",
            "If-Match": f'"{agreement["version"]}"',
        },
        json={
            "expected_agreement_version": agreement["version"],
            "revision_id": revision["id"],
            "expected_digest": revision["digest"],
            "action": "counter",
            "reason": "承办人提出新的期限",
            "counter_document": counter_document,
            "client_mutation_id": "analysis-owner-counter-external-local",
        },
    )
    assert countered.status_code == 200, countered.text
    counter_agreement = countered.json()["agreement"]
    counter_revision = counter_agreement["current_revision"]

    owner_accepted = client.post(
        f"/api/v1/task-agreements/{agreement['id']}/responses",
        headers=_write_headers(
            owner_headers,
            "analysis-owner-counter-accept",
            version=counter_agreement["version"],
        ),
        json={
            "expected_agreement_version": counter_agreement["version"],
            "revision_id": counter_revision["id"],
            "expected_digest": counter_revision["digest"],
            "action": "accept",
            "reason": None,
            "counter_document": None,
            "client_mutation_id": "analysis-owner-counter-accept-local",
        },
    )
    assert owner_accepted.status_code == 200, owner_accepted.text
    owner_decision = owner_accepted.json()["decision"]
    assert owner_decision["actor_role"] == "issuer"
    assert owner_decision["actor_member_id"] == OWNER_MEMBER_ID
    assert owner_decision["assurance_method"] == "owner_token"

    service = client.app.state.workspace_service
    with service.database.connect() as connection:
        owner_event = connection.execute(
            """
            SELECT * FROM secretary_workspace_events
            WHERE event_type = 'task.agreement_accept'
              AND json_extract(payload_json, '$.decision.id') = ?
            """,
            (owner_decision["id"],),
        ).fetchone()
    assert owner_event is not None
    assert owner_event["actor_type"] == "owner"

    response = client.get(
        f"{WORKSPACE_PATH}/task-analysis?from=2026-08-01&to=2026-08-31",
        headers=owner_headers,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    analyzed_task = next(
        item for item in result["tasks"] if item["task_id"] == task["id"]
    )
    responses = _action(analyzed_task, "agreement_response")
    assert responses["raw_event_count"] == 2
    assert responses["classified_event_count"] == 2
    assert responses["by_tier"] == {
        "a2_owner_control": 1,
        "a1_capability": 1,
        "a0_unknown": 0,
    }
    assert responses["weighted_event_basis_points"] == 15_000
    assert result["coverage"]["resolved_typed_events"] == 2
    assert result["coverage"]["excluded_security_events"] == 2


def test_task_analysis_period_query_and_authorization_are_strict(
    client: TestClient,
    owner_headers: dict[str, str],
    agent_headers: dict[str, str],
) -> None:
    path = f"{WORKSPACE_PATH}/task-analysis"
    valid_query = "from=2026-08-01&to=2026-08-31"

    missing_auth = client.get(f"{path}?{valid_query}")
    assert missing_auth.status_code == 401
    assert missing_auth.headers["cache-control"] == "no-store, max-age=0"
    assert client.get(
        f"{path}?{valid_query}", headers=agent_headers
    ).status_code == 401

    assert client.get(
        f"{path}?from=2026-08-02&to=2026-08-01",
        headers=owner_headers,
    ).status_code == 422
    assert client.get(
        f"{path}?from=2025-01-01&to=2026-08-01",
        headers=owner_headers,
    ).status_code == 422
    assert client.get(
        f"{path}?from=bad&to=2026-08-01",
        headers=owner_headers,
    ).status_code == 422
    assert client.get(
        f"{path}?from=2026-08-01",
        headers=owner_headers,
    ).status_code == 422
    assert client.get(
        f"{path}?from=2026-08-01&to=2026-08-02&extra=1",
        headers=owner_headers,
    ).status_code == 400
    assert client.get(
        f"{path}?from=2026-08-01&from=2026-08-01&to=2026-08-02",
        headers=owner_headers,
    ).status_code == 400
    assert client.get(
        f"{path}?from=2026-08-01&to=2026-08-02&to=2026-08-02",
        headers=owner_headers,
    ).status_code == 400

    unknown_workspace = client.get(
        f"/api/v1/workspaces/ws_missing/task-analysis?{valid_query}",
        headers=owner_headers,
    )
    assert unknown_workspace.status_code == 404
    assert unknown_workspace.headers["cache-control"] == "no-store, max-age=0"
