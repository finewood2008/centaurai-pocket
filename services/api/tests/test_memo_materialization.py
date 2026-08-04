from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from fastapi.testclient import TestClient

from centaur_pocket.workspace.service import (
    TASK_CHANGE_V6_INDEXES,
    TASK_CHANGE_V6_TRIGGERS,
)

WORKSPACE_ID = "ws_default"
WORKSPACE_PATH = f"/api/v1/workspaces/{WORKSPACE_ID}"
OWNER_ID = "member_owner"
DEVICE_ID = "pytest-memo-materialization"
RAW_CONTENT = "PRIVATE-MEMO-CONTENT-9f2a"
RAW_EXCERPT = "PRIVATE-SOURCE-EXCERPT-4c71"
RAW_SOURCE_REF = "message://private-source/7b31"


def _headers(
    owner_headers: dict[str, str],
    key: str,
    *,
    if_match: int | str | None = None,
) -> dict[str, str]:
    result = {
        **owner_headers,
        "Idempotency-Key": key,
        "X-Device-ID": DEVICE_ID,
    }
    if if_match is not None:
        result["If-Match"] = (
            f'"{if_match}"' if isinstance(if_match, int) else if_match
        )
    return result


def _memo_payload(
    *,
    domain: str = "work",
    title: str = "核对合同退出条款",
    content: str = RAW_CONTENT,
) -> dict[str, Any]:
    return {
        "record_type": "task_candidate",
        "domain": domain,
        "horizon": "short_term",
        "urgency": "high",
        "title": title,
        "content": content,
        "due_at": "2026-08-03T10:00:00+08:00",
        "source": {
            "source_kind": "im",
            "source_ref": RAW_SOURCE_REF,
            "excerpt": RAW_EXCERPT,
            "authority": "observed",
            "observed_at": "2026-08-02T08:00:00+08:00",
        },
        "tags": ["合同", "待办"],
        "pinned": True,
        "client_mutation_id": "memo-local-001",
    }


def _create_memo(
    client: TestClient,
    owner_headers: dict[str, str],
    *,
    key: str,
    domain: str = "work",
    title: str = "核对合同退出条款",
    content: str = RAW_CONTENT,
) -> dict[str, Any]:
    response = client.post(
        f"{WORKSPACE_PATH}/memos",
        headers=_headers(owner_headers, key),
        json=_memo_payload(domain=domain, title=title, content=content),
    )
    assert response.status_code == 201, response.text
    memo = response.json()
    assert response.headers["etag"] == '"1"'
    assert memo["authority"] == "observed"
    assert memo["source"]["authority"] == "observed"
    return memo


def _task_payload(
    memo_version: Any,
    *,
    assignee_id: str = OWNER_ID,
    confirm_personal_disclosure: Any = False,
    title: str = "完成合同复核",
) -> dict[str, Any]:
    return {
        "expected_memo_version": memo_version,
        "title": title,
        "purpose": "降低合同执行风险",
        "objective": "形成可验收的合同复核结论",
        "strategy": "逐条核对关键条款并记录依据。",
        "key_points": ["退出条款", "违约责任"],
        "acceptance_criteria": ["关键条款均有结论", "结论包含依据"],
        "assignee_member_id": assignee_id,
        "due_at": "2026-08-05T18:00:00+08:00",
        "priority": "high",
        "tier": "standard",
        "confirm_personal_disclosure": confirm_personal_disclosure,
        "client_mutation_id": "task-from-memo-local-001",
    }


def _calendar_payload(
    memo_version: Any,
    *,
    description: Any = "集中核对合同条款并记录结论。",
    all_day: Any = False,
    title: str = "合同复核专注时间",
) -> dict[str, Any]:
    return {
        "expected_memo_version": memo_version,
        "title": title,
        "description": description,
        "start_at": "2026-08-04T09:00:00+08:00",
        "end_at": "2026-08-04T10:30:00+08:00",
        "timezone": "Asia/Shanghai",
        "all_day": all_day,
        "kind": "focus",
        "client_mutation_id": "calendar-from-memo-local-001",
    }


def _materialize_task(
    client: TestClient,
    owner_headers: dict[str, str],
    memo: dict[str, Any],
    *,
    key: str,
    payload: dict[str, Any] | None = None,
):
    return client.post(
        f"{WORKSPACE_PATH}/memos/{memo['id']}/task",
        headers=_headers(owner_headers, key, if_match=memo["version"]),
        json=payload or _task_payload(memo["version"]),
    )


def _materialize_calendar(
    client: TestClient,
    owner_headers: dict[str, str],
    memo: dict[str, Any],
    *,
    key: str,
    payload: dict[str, Any] | None = None,
):
    return client.post(
        f"{WORKSPACE_PATH}/memos/{memo['id']}/calendar",
        headers=_headers(owner_headers, key, if_match=memo["version"]),
        json=payload or _calendar_payload(memo["version"]),
    )


def _sync_after(
    client: TestClient,
    owner_headers: dict[str, str],
    cursor: int,
) -> list[dict[str, Any]]:
    response = client.get(
        f"{WORKSPACE_PATH}/sync?after={cursor}", headers=owner_headers
    )
    assert response.status_code == 200, response.text
    return response.json()["changes"]


def _create_member(
    client: TestClient,
    owner_headers: dict[str, str],
    *,
    suffix: str,
    role: str = "member",
) -> dict[str, Any]:
    response = client.post(
        f"{WORKSPACE_PATH}/members",
        headers=_headers(owner_headers, f"memo-member-{suffix}"),
        json={
            "kind": "person",
            "role": role,
            "display_name": f"承办人-{suffix}",
            "contact_ref": f"wecom://memo/{suffix}",
            "client_mutation_id": f"member-local-{suffix}",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _generic_task_payload(*, source: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "domain": "work",
        "title": "普通任务",
        "purpose": "验证通用任务入口",
        "objective": "不能旁路备忘物化协议",
        "strategy": "通过专用接口执行。",
        "key_points": ["原子性"],
        "acceptance_criteria": ["旁路被拒绝"],
        "issuer_member_id": OWNER_ID,
        "assignee_member_id": OWNER_ID,
        "acceptance_owner_id": OWNER_ID,
        "priority": "normal",
        "tier": "standard",
        "health": "on_track",
        "source": source or {"source_kind": "manual"},
        "client_mutation_id": "generic-task-local-001",
    }


def _generic_calendar_payload() -> dict[str, Any]:
    return {
        "domain": "work",
        "title": "普通日程",
        "description": "验证通用日程入口。",
        "start_at": "2026-08-06T09:00:00+08:00",
        "end_at": "2026-08-06T10:00:00+08:00",
        "timezone": "Asia/Shanghai",
        "all_day": False,
        "status": "scheduled",
        "kind": "focus",
        "attendees": [],
        "client_mutation_id": "generic-calendar-local-001",
    }


def test_task_materialization_derives_authoritative_fields_etag_and_events_once(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    memo = _create_memo(client, owner_headers, key="memo-task-happy-create")
    cursor = _sync_after(client, owner_headers, 0)[-1]["cursor"]
    payload = _task_payload(memo["version"])
    headers = _headers(
        owner_headers, "memo-task-happy-materialize", if_match=memo["version"]
    )
    path = f"{WORKSPACE_PATH}/memos/{memo['id']}/task"

    created = client.post(path, headers=headers, json=payload)
    assert created.status_code == 201, created.text
    assert created.headers["etag"] == '"2"'
    result = created.json()
    converted = result["memo"]
    task = result["task"]
    assert converted["id"] == memo["id"]
    assert converted["status"] == "converted"
    assert converted["version"] == memo["version"] + 1
    assert task["origin_memo_id"] == memo["id"]
    assert task["domain"] == memo["domain"]
    assert task["source"] == memo["source"]
    assert task["issuer_member_id"] == OWNER_ID
    assert task["acceptance_owner_id"] == OWNER_ID
    assert task["assignee_member_id"] == OWNER_ID
    assert task["issuer_label"] == "主人"
    assert task["acceptance_owner_label"] == "主人"
    assert task["stage"] == "draft"
    assert task["health"] == "on_track"
    assert task["summary"] == payload["purpose"]
    assert task["start_at"] is None
    assert task["requires_alignment"] is False
    assert task["steps"] == []

    replay = client.post(path, headers=headers, json=payload)
    assert replay.status_code == 201, replay.text
    assert replay.headers["etag"] == '"2"'
    assert replay.json() == result

    changed = client.post(
        path,
        headers=headers,
        json={**payload, "title": "同一键下的不同任务"},
    )
    assert changed.status_code == 409

    changes = _sync_after(client, owner_headers, cursor)
    assert [change["event_type"] for change in changes] == [
        "task.created",
        "memo.updated",
    ]
    assert changes[0]["payload"] == task
    assert changes[1]["payload"] == converted
    assert changes[0]["cursor"] < changes[1]["cursor"]

    with client.app.state.workspace_service.database.connect() as connection:
        materialization = connection.execute(
            "SELECT * FROM secretary_memo_materializations WHERE memo_id = ?",
            (memo["id"],),
        ).fetchone()
        snapshot = json.loads(materialization["source_snapshot_json"])
    assert materialization["source_memo_version"] == memo["version"]
    assert materialization["task_id"] == task["id"]
    assert materialization["calendar_entry_id"] is None
    assert snapshot["authority"] == "observed"
    assert snapshot["source_kind"] == "im"
    assert snapshot["source_ref"] == RAW_SOURCE_REF
    assert RAW_CONTENT not in materialization["source_snapshot_json"]
    assert RAW_EXCERPT not in materialization["source_snapshot_json"]
    assert snapshot["source_json_digest"].startswith("sha256:")
    assert snapshot["content_digest"].startswith("sha256:")


def test_calendar_materialization_derives_internal_schedule_and_etag(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    memo = _create_memo(
        client,
        owner_headers,
        key="memo-calendar-happy-create",
        domain="personal",
        title="安排私人材料整理",
    )
    cursor = _sync_after(client, owner_headers, 0)[-1]["cursor"]
    response = _materialize_calendar(
        client,
        owner_headers,
        memo,
        key="memo-calendar-happy-materialize",
    )
    assert response.status_code == 201, response.text
    assert response.headers["etag"] == '"2"'
    result = response.json()
    calendar = result["calendar_entry"]
    assert result["memo"]["status"] == "converted"
    assert result["memo"]["version"] == 2
    assert calendar["memo_id"] == memo["id"]
    assert calendar["task_id"] is None
    assert calendar["step_id"] is None
    assert calendar["domain"] == "personal"
    assert calendar["status"] == "scheduled"
    assert calendar["attendees"] == []
    assert calendar["external_provider"] is None
    assert calendar["external_id"] is None
    assert calendar["start_at"] == "2026-08-04T01:00:00Z"
    assert calendar["end_at"] == "2026-08-04T02:30:00Z"
    assert [change["event_type"] for change in _sync_after(
        client, owner_headers, cursor
    )] == ["calendar.created", "memo.updated"]


@pytest.mark.parametrize(
    ("header_value", "body_version", "expected_status"),
    [
        (None, 1, 428),
        ('"not-a-version"', 1, 400),
        ('"0"', 1, 400),
        ('"1","2"', 1, 400),
        ('"2"', 1, 412),
    ],
)
def test_materialization_rejects_missing_invalid_or_mismatched_if_match(
    client: TestClient,
    owner_headers: dict[str, str],
    header_value: str | None,
    body_version: int,
    expected_status: int,
) -> None:
    memo = _create_memo(client, owner_headers, key="memo-cas-create")
    headers = _headers(owner_headers, "memo-cas-attempt")
    if header_value is not None:
        headers["If-Match"] = header_value
    response = client.post(
        f"{WORKSPACE_PATH}/memos/{memo['id']}/task",
        headers=headers,
        json=_task_payload(body_version),
    )
    assert response.status_code == expected_status, response.text
    persisted = client.get(f"{WORKSPACE_PATH}/memos", headers=owner_headers).json()
    assert persisted["items"][0]["status"] == "active"
    assert persisted["items"][0]["version"] == 1


@pytest.mark.parametrize("bad_version", [True, False, "1", 1.0, 0, -1])
def test_expected_memo_version_is_a_strict_positive_integer(
    client: TestClient,
    owner_headers: dict[str, str],
    bad_version: Any,
) -> None:
    memo = _create_memo(client, owner_headers, key="memo-strict-version-create")
    response = client.post(
        f"{WORKSPACE_PATH}/memos/{memo['id']}/task",
        headers=_headers(owner_headers, "memo-strict-version", if_match=1),
        json=_task_payload(bad_version),
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("bad_confirmation", [0, 1, "true", "false"])
def test_personal_disclosure_confirmation_is_a_strict_boolean(
    client: TestClient,
    owner_headers: dict[str, str],
    bad_confirmation: Any,
) -> None:
    memo = _create_memo(
        client,
        owner_headers,
        key="memo-strict-disclosure-create",
        domain="personal",
    )
    response = client.post(
        f"{WORKSPACE_PATH}/memos/{memo['id']}/task",
        headers=_headers(owner_headers, "memo-strict-disclosure", if_match=1),
        json=_task_payload(1, confirm_personal_disclosure=bad_confirmation),
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("bad_all_day", [0, 1, "true", "false"])
def test_calendar_all_day_is_a_strict_boolean(
    client: TestClient,
    owner_headers: dict[str, str],
    bad_all_day: Any,
) -> None:
    memo = _create_memo(client, owner_headers, key="memo-strict-all-day-create")
    response = client.post(
        f"{WORKSPACE_PATH}/memos/{memo['id']}/calendar",
        headers=_headers(owner_headers, "memo-strict-all-day", if_match=1),
        json=_calendar_payload(1, all_day=bad_all_day),
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("description_case", ["omitted", "null", "empty"])
def test_calendar_description_is_required_and_nonempty(
    client: TestClient,
    owner_headers: dict[str, str],
    description_case: str,
) -> None:
    memo = _create_memo(client, owner_headers, key="memo-description-create")
    payload = _calendar_payload(1)
    if description_case == "omitted":
        payload.pop("description")
    elif description_case == "null":
        payload["description"] = None
    else:
        payload["description"] = ""
    response = client.post(
        f"{WORKSPACE_PATH}/memos/{memo['id']}/calendar",
        headers=_headers(owner_headers, "memo-description-invalid", if_match=1),
        json=payload,
    )
    assert response.status_code == 422, response.text


def test_calendar_description_length_boundary(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    memo = _create_memo(client, owner_headers, key="memo-description-max-create")
    accepted = _calendar_payload(1, description="x" * 200_000)
    response = client.post(
        f"{WORKSPACE_PATH}/memos/{memo['id']}/calendar",
        headers=_headers(owner_headers, "memo-description-max", if_match=1),
        json=accepted,
    )
    assert response.status_code == 201, response.text

    another = _create_memo(client, owner_headers, key="memo-description-over-create")
    rejected = _calendar_payload(1, description="x" * 200_001)
    response = client.post(
        f"{WORKSPACE_PATH}/memos/{another['id']}/calendar",
        headers=_headers(owner_headers, "memo-description-over", if_match=1),
        json=rejected,
    )
    assert response.status_code == 422, response.text


def test_stale_memo_version_and_unknown_or_query_fields_are_rejected(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    stale = _create_memo(client, owner_headers, key="memo-stale-create")
    updated = client.patch(
        f"{WORKSPACE_PATH}/memos/{stale['id']}",
        headers=_headers(owner_headers, "memo-stale-update", if_match=1),
        json={"title": "已在另一端更新"},
    )
    assert updated.status_code == 200, updated.text
    stale_response = client.post(
        f"{WORKSPACE_PATH}/memos/{stale['id']}/task",
        headers=_headers(owner_headers, "memo-stale-materialize", if_match=1),
        json=_task_payload(1),
    )
    assert stale_response.status_code == 412, stale_response.text

    unknown = _create_memo(client, owner_headers, key="memo-unknown-create")
    unknown_response = client.post(
        f"{WORKSPACE_PATH}/memos/{unknown['id']}/task",
        headers=_headers(owner_headers, "memo-unknown-field", if_match=1),
        json={**_task_payload(1), "source": {"source_kind": "manual"}},
    )
    assert unknown_response.status_code == 422, unknown_response.text

    query = _create_memo(client, owner_headers, key="memo-query-create")
    query_response = client.post(
        f"{WORKSPACE_PATH}/memos/{query['id']}/calendar?unsafe=1",
        headers=_headers(owner_headers, "memo-query-reject", if_match=1),
        json=_calendar_payload(1),
    )
    assert query_response.status_code == 400, query_response.text


def test_materialization_is_owner_only_and_workspace_scoped(
    client: TestClient,
    owner_headers: dict[str, str],
    agent_headers: dict[str, str],
) -> None:
    memo = _create_memo(client, owner_headers, key="memo-scope-create")
    path = f"{WORKSPACE_PATH}/memos/{memo['id']}/task"
    no_auth = client.post(
        path,
        headers={"Idempotency-Key": "memo-no-auth", "X-Device-ID": DEVICE_ID},
        json=_task_payload(1),
    )
    assert no_auth.status_code == 401
    agent = client.post(
        path,
        headers=_headers(agent_headers, "memo-agent-denied", if_match=1),
        json=_task_payload(1),
    )
    assert agent.status_code == 401

    missing_workspace = client.post(
        f"/api/v1/workspaces/ws_missing/memos/{memo['id']}/task",
        headers=_headers(owner_headers, "memo-workspace-missing", if_match=1),
        json=_task_payload(1),
    )
    assert missing_workspace.status_code == 404

    with client.app.state.workspace_service.database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO secretary_workspaces(
                id, name, timezone, version, created_at, updated_at
            ) VALUES (
                'ws_other', '其他工作区', 'Asia/Shanghai', 1,
                '2026-08-02T00:00:00Z', '2026-08-02T00:00:00Z'
            )
            """
        )
    cross_workspace = client.post(
        f"/api/v1/workspaces/ws_other/memos/{memo['id']}/task",
        headers=_headers(owner_headers, "memo-workspace-idor", if_match=1),
        json=_task_payload(1),
    )
    assert cross_workspace.status_code == 404
    missing_memo = client.post(
        f"{WORKSPACE_PATH}/memos/memo_missing/task",
        headers=_headers(owner_headers, "memo-id-missing", if_match=1),
        json=_task_payload(1),
    )
    assert missing_memo.status_code == 404


@pytest.mark.parametrize(
    ("status", "confirmation", "deleted", "expected_status"),
    [
        ("active", "confirmed", False, 201),
        ("active", "pending", False, 409),
        ("active", "rejected", False, 409),
        ("inbox", "not_required", False, 409),
        ("archived", "not_required", False, 409),
        ("converted", "not_required", False, 409),
        ("active", "not_required", True, 404),
    ],
)
def test_only_live_active_confirmed_or_not_required_memos_can_materialize(
    client: TestClient,
    owner_headers: dict[str, str],
    status: str,
    confirmation: str,
    deleted: bool,
    expected_status: int,
) -> None:
    memo = _create_memo(client, owner_headers, key="memo-state-create")
    with client.app.state.workspace_service.database.transaction() as connection:
        connection.execute(
            """
            UPDATE secretary_memos
            SET status = ?, confirmation_status = ?, deleted_at = ?
            WHERE id = ?
            """,
            (
                status,
                confirmation,
                "2026-08-02T01:00:00Z" if deleted else None,
                memo["id"],
            ),
        )
    response = _materialize_calendar(
        client, owner_headers, memo, key="memo-state-materialize"
    )
    assert response.status_code == expected_status, response.text


def test_task_assignee_and_personal_disclosure_policy(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    member = _create_member(client, owner_headers, suffix="personal-member")
    viewer = _create_member(
        client, owner_headers, suffix="personal-viewer", role="viewer"
    )

    personal = _create_memo(
        client,
        owner_headers,
        key="memo-personal-create",
        domain="personal",
        content=RAW_CONTENT,
    )
    denied = _materialize_task(
        client,
        owner_headers,
        personal,
        key="memo-personal-unconfirmed",
        payload=_task_payload(1, assignee_id=member["id"]),
    )
    assert denied.status_code == 409, denied.text
    accepted = _materialize_task(
        client,
        owner_headers,
        personal,
        key="memo-personal-confirmed",
        payload=_task_payload(
            1,
            assignee_id=member["id"],
            confirm_personal_disclosure=True,
        ),
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["task"]["source"] == personal["source"]
    assert accepted.json()["task"]["requires_alignment"] is True

    work = _create_memo(client, owner_headers, key="memo-work-confirm-create")
    unnecessary_work_confirmation = _materialize_task(
        client,
        owner_headers,
        work,
        key="memo-work-confirm-reject",
        payload=_task_payload(
            1,
            assignee_id=member["id"],
            confirm_personal_disclosure=True,
        ),
    )
    assert unnecessary_work_confirmation.status_code == 422

    self_owned = _create_memo(
        client,
        owner_headers,
        key="memo-self-confirm-create",
        domain="personal",
    )
    unnecessary_self_confirmation = _materialize_task(
        client,
        owner_headers,
        self_owned,
        key="memo-self-confirm-reject",
        payload=_task_payload(1, confirm_personal_disclosure=True),
    )
    assert unnecessary_self_confirmation.status_code == 422

    viewer_memo = _create_memo(client, owner_headers, key="memo-viewer-create")
    viewer_response = _materialize_task(
        client,
        owner_headers,
        viewer_memo,
        key="memo-viewer-reject",
        payload=_task_payload(1, assignee_id=viewer["id"]),
    )
    assert viewer_response.status_code == 422, viewer_response.text


def test_generic_create_routes_cannot_launder_memo_links(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    memo = _create_memo(client, owner_headers, key="memo-bypass-create")
    task_payload = {
        **_generic_task_payload(source=memo["source"]),
        "origin_memo_id": memo["id"],
    }
    task_response = client.post(
        f"{WORKSPACE_PATH}/tasks",
        headers=_headers(owner_headers, "memo-bypass-task"),
        json=task_payload,
    )
    assert task_response.status_code == 409, task_response.text

    calendar_response = client.post(
        f"{WORKSPACE_PATH}/calendar",
        headers=_headers(owner_headers, "memo-bypass-calendar"),
        json={**_generic_calendar_payload(), "memo_id": memo["id"]},
    )
    assert calendar_response.status_code == 409, calendar_response.text
    persisted = client.get(f"{WORKSPACE_PATH}/memos", headers=owner_headers).json()
    assert persisted["items"][0]["status"] == "active"
    assert persisted["items"][0]["version"] == 1


def test_unmaterialized_memo_still_supports_normal_update(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    memo = _create_memo(client, owner_headers, key="memo-update-regression-create")
    response = client.patch(
        f"{WORKSPACE_PATH}/memos/{memo['id']}",
        headers=_headers(owner_headers, "memo-update-regression", if_match=1),
        json={"title": "普通备忘仍可修改"},
    )
    assert response.status_code == 200, response.text
    assert response.headers["etag"] == '"2"'
    assert response.json()["title"] == "普通备忘仍可修改"


def _linked_target_counts(client: TestClient, memo_id: str) -> tuple[int, int, int]:
    with client.app.state.workspace_service.database.connect() as connection:
        task_count = connection.execute(
            "SELECT COUNT(*) FROM secretary_business_tasks WHERE origin_memo_id = ?",
            (memo_id,),
        ).fetchone()[0]
        calendar_count = connection.execute(
            "SELECT COUNT(*) FROM secretary_calendar_entries WHERE memo_id = ?",
            (memo_id,),
        ).fetchone()[0]
        materialization_count = connection.execute(
            "SELECT COUNT(*) FROM secretary_memo_materializations WHERE memo_id = ?",
            (memo_id,),
        ).fetchone()[0]
    return int(task_count), int(calendar_count), int(materialization_count)


def test_different_keys_concurrently_create_only_one_task(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    memo = _create_memo(client, owner_headers, key="memo-concurrent-task-create")
    cursor = _sync_after(client, owner_headers, 0)[-1]["cursor"]
    barrier = Barrier(2)

    def submit(index: int):
        barrier.wait()
        return client.post(
            f"{WORKSPACE_PATH}/memos/{memo['id']}/task",
            headers=_headers(
                owner_headers,
                f"memo-concurrent-task-{index}",
                if_match=1,
            ),
            json=_task_payload(1, title=f"并发任务-{index}"),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(submit, (1, 2)))

    assert sorted(response.status_code for response in responses) == [201, 409]
    assert _linked_target_counts(client, memo["id"]) == (1, 0, 1)
    assert [change["event_type"] for change in _sync_after(
        client, owner_headers, cursor
    )] == ["task.created", "memo.updated"]


def test_task_and_calendar_compete_for_one_cross_route_materialization(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    memo = _create_memo(client, owner_headers, key="memo-concurrent-cross-create")
    cursor = _sync_after(client, owner_headers, 0)[-1]["cursor"]
    barrier = Barrier(2)

    def submit_task():
        barrier.wait()
        return client.post(
            f"{WORKSPACE_PATH}/memos/{memo['id']}/task",
            headers=_headers(
                owner_headers, "memo-cross-route-task", if_match=1
            ),
            json=_task_payload(1),
        )

    def submit_calendar():
        barrier.wait()
        return client.post(
            f"{WORKSPACE_PATH}/memos/{memo['id']}/calendar",
            headers=_headers(
                owner_headers, "memo-cross-route-calendar", if_match=1
            ),
            json=_calendar_payload(1),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        task_future = executor.submit(submit_task)
        calendar_future = executor.submit(submit_calendar)
        responses = [task_future.result(), calendar_future.result()]

    assert sorted(response.status_code for response in responses) == [201, 409]
    task_count, calendar_count, materialization_count = _linked_target_counts(
        client, memo["id"]
    )
    assert task_count + calendar_count == 1
    assert materialization_count == 1
    changes = _sync_after(client, owner_headers, cursor)
    assert len(changes) == 2
    assert changes[0]["event_type"] in {"task.created", "calendar.created"}
    assert changes[1]["event_type"] == "memo.updated"


def test_materialized_memo_and_ledger_links_are_immutable(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    memo = _create_memo(client, owner_headers, key="memo-immutable-create")
    created = _materialize_task(
        client, owner_headers, memo, key="memo-immutable-materialize"
    )
    assert created.status_code == 201, created.text
    result = created.json()
    task = result["task"]

    patch = client.patch(
        f"{WORKSPACE_PATH}/memos/{memo['id']}",
        headers=_headers(owner_headers, "memo-immutable-patch", if_match=2),
        json={"title": "不得修改"},
    )
    assert patch.status_code == 409, patch.text
    delete = client.post(
        f"{WORKSPACE_PATH}/memos/{memo['id']}/delete",
        headers=_headers(owner_headers, "memo-immutable-delete"),
        json={"expected_version": 2},
    )
    assert delete.status_code == 409, delete.text

    database = client.app.state.workspace_service.database
    with (
        pytest.raises(sqlite3.IntegrityError, match="materialization is immutable"),
        database.transaction() as connection,
    ):
        connection.execute(
            """
            UPDATE secretary_memo_materializations
            SET created_at = created_at WHERE memo_id = ?
            """,
            (memo["id"],),
        )
    with (
        pytest.raises(sqlite3.IntegrityError, match="materialization is immutable"),
        database.transaction() as connection,
    ):
        connection.execute(
            "DELETE FROM secretary_memo_materializations WHERE memo_id = ?",
            (memo["id"],),
        )
    with (
        pytest.raises(sqlite3.IntegrityError, match="materialized memo is immutable"),
        database.transaction() as connection,
    ):
        connection.execute(
            "UPDATE secretary_memos SET title = title WHERE id = ?", (memo["id"],)
        )
    with (
        pytest.raises(sqlite3.IntegrityError, match="materialized memo is immutable"),
        database.transaction() as connection,
    ):
        connection.execute("DELETE FROM secretary_memos WHERE id = ?", (memo["id"],))
    with (
        pytest.raises(sqlite3.IntegrityError, match="task link is immutable"),
        database.transaction() as connection,
    ):
        connection.execute(
            "UPDATE secretary_business_tasks SET origin_memo_id = NULL WHERE id = ?",
            (task["id"],),
        )


def _snapshot_for_row(
    memo: sqlite3.Row,
    *,
    source_memo_version: int,
) -> str:
    source = json.loads(memo["source_json"])
    snapshot = {
        "schema": "centaur.memo-source-snapshot.v1",
        "memo_id": memo["id"],
        "workspace_id": memo["workspace_id"],
        "source_memo_version": source_memo_version,
        "domain": memo["domain"],
        "authority": memo["authority"],
        "source_kind": source.get("source_kind"),
        "source_ref": source.get("source_ref"),
        "source_json_digest": "sha256:"
        + hashlib.sha256(memo["source_json"].encode()).hexdigest(),
        "content_digest": "sha256:"
        + hashlib.sha256(memo["content"].encode()).hexdigest(),
    }
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@pytest.mark.parametrize("invalid_kind", ["wrong_version", "empty_snapshot"])
def test_materialization_binding_rejects_invalid_version_or_snapshot(
    client: TestClient,
    owner_headers: dict[str, str],
    invalid_kind: str,
) -> None:
    memo = _create_memo(client, owner_headers, key="memo-binding-create")
    generic = client.post(
        f"{WORKSPACE_PATH}/tasks",
        headers=_headers(owner_headers, "memo-binding-target-create"),
        json=_generic_task_payload(source=memo["source"]),
    )
    assert generic.status_code == 201, generic.text
    task = generic.json()
    database = client.app.state.workspace_service.database
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE secretary_memos SET status = 'converted', version = 2
            WHERE id = ?
            """,
            (memo["id"],),
        )
        connection.execute(
            "UPDATE secretary_business_tasks SET origin_memo_id = ? WHERE id = ?",
            (memo["id"], task["id"]),
        )
        memo_row = connection.execute(
            "SELECT * FROM secretary_memos WHERE id = ?", (memo["id"],)
        ).fetchone()
        source_version = 2 if invalid_kind == "wrong_version" else 1
        snapshot = (
            _snapshot_for_row(memo_row, source_memo_version=source_version)
            if invalid_kind == "wrong_version"
            else "{}"
        )

    with (
        pytest.raises(sqlite3.IntegrityError, match="invalid materialization memo"),
        database.transaction() as connection,
    ):
        connection.execute(
            """
            INSERT INTO secretary_memo_materializations(
                memo_id, workspace_id, source_memo_version,
                source_snapshot_json, task_id, calendar_entry_id, created_at
            ) VALUES (?, ?, ?, ?, ?, NULL, '2026-08-02T00:00:00Z')
            """,
            (
                memo["id"],
                WORKSPACE_ID,
                source_version,
                snapshot,
                task["id"],
            ),
        )


def test_failure_after_target_and_ledger_writes_rolls_back_everything(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    memo = _create_memo(client, owner_headers, key="memo-rollback-create")
    cursor = _sync_after(client, owner_headers, 0)[-1]["cursor"]
    database = client.app.state.workspace_service.database
    with database.connect() as connection:
        before_counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "secretary_business_tasks",
                "secretary_calendar_entries",
                "secretary_memo_materializations",
                "secretary_workspace_events",
                "secretary_workspace_idempotency",
            )
        )
    with database.transaction() as connection:
        connection.execute(
            """
            CREATE TRIGGER test_force_memo_materialization_rollback
            BEFORE INSERT ON secretary_workspace_events
            WHEN NEW.event_type = 'memo.updated'
            BEGIN
                SELECT RAISE(ABORT, 'forced materialization rollback');
            END
            """
        )
    try:
        with pytest.raises(sqlite3.IntegrityError, match="forced materialization"):
            _materialize_task(
                client,
                owner_headers,
                memo,
                key="memo-rollback-materialize",
            )
    finally:
        with database.transaction() as connection:
            connection.execute(
                "DROP TRIGGER IF EXISTS test_force_memo_materialization_rollback"
            )

    with database.connect() as connection:
        after_counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "secretary_business_tasks",
                "secretary_calendar_entries",
                "secretary_memo_materializations",
                "secretary_workspace_events",
                "secretary_workspace_idempotency",
            )
        )
        persisted = connection.execute(
            "SELECT status, version FROM secretary_memos WHERE id = ?", (memo["id"],)
        ).fetchone()
    assert after_counts == before_counts
    assert dict(persisted) == {"status": "active", "version": 1}
    assert _sync_after(client, owner_headers, cursor) == []


def _create_mobile_session(
    client: TestClient,
    owner_headers: dict[str, str],
    *,
    device_id: str,
) -> dict[str, Any]:
    pairing = client.post("/api/v1/mobile/pairings", headers=owner_headers)
    assert pairing.status_code == 201, pairing.text
    claimed = client.post(
        "/api/v1/mobile/pairings/claim",
        json={
            "code": pairing.json()["code"],
            "device_id": device_id,
            "display_name": "物化测试手机",
            "platform": "android",
            "app_version": "1.2.3",
        },
    )
    assert claimed.status_code == 200, claimed.text
    return claimed.json()


def test_paired_mobile_device_can_materialize_only_with_matching_device_id(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    session = _create_mobile_session(
        client, owner_headers, device_id="memo-materialization-phone"
    )
    memo = _create_memo(client, owner_headers, key="memo-mobile-success-create")
    device_headers = {
        "Authorization": f"Bearer {session['access_token']}",
        "Idempotency-Key": "memo-mobile-success",
        "X-Device-ID": session["device"]["device_id"],
        "If-Match": '"1"',
    }
    success = client.post(
        f"{WORKSPACE_PATH}/memos/{memo['id']}/calendar",
        headers=device_headers,
        json=_calendar_payload(1),
    )
    assert success.status_code == 201, success.text
    assert success.headers["etag"] == '"2"'

    another = _create_memo(client, owner_headers, key="memo-mobile-wrong-create")
    wrong_device = client.post(
        f"{WORKSPACE_PATH}/memos/{another['id']}/task",
        headers={
            **device_headers,
            "Idempotency-Key": "memo-mobile-wrong-device",
            "X-Device-ID": "forged-device-id",
        },
        json=_task_payload(1),
    )
    assert wrong_device.status_code == 403
    other_workspace = client.post(
        f"/api/v1/workspaces/ws_other/memos/{another['id']}/task",
        headers={
            **device_headers,
            "Idempotency-Key": "memo-mobile-wrong-workspace",
        },
        json=_task_payload(1),
    )
    assert other_workspace.status_code == 403
    assert _linked_target_counts(client, another["id"]) == (0, 0, 0)


@pytest.mark.parametrize(
    "encoded_id",
    [
        "memo%255Cescape",
        "%E5%A4%87%E5%BF%98",
        "memo%252Fescape",
        ".%2E",
        "m" * 201,
    ],
)
def test_materialization_paths_reject_unsafe_identifiers_without_writes(
    client: TestClient,
    owner_headers: dict[str, str],
    encoded_id: str,
) -> None:
    before = _create_memo(client, owner_headers, key="memo-path-safe-baseline")
    response = client.post(
        f"{WORKSPACE_PATH}/memos/{encoded_id}/task",
        headers=_headers(owner_headers, "memo-path-unsafe", if_match=1),
        json=_task_payload(1),
        follow_redirects=False,
    )
    assert response.status_code in {400, 404}, response.text
    assert _linked_target_counts(client, before["id"]) == (0, 0, 0)


@pytest.mark.parametrize("suffix", ["task/", "calendar/"])
def test_materialization_paths_reject_trailing_slashes(
    client: TestClient,
    owner_headers: dict[str, str],
    suffix: str,
) -> None:
    memo = _create_memo(client, owner_headers, key="memo-trailing-create")
    payload = _task_payload(1) if suffix.startswith("task") else _calendar_payload(1)
    response = client.post(
        f"{WORKSPACE_PATH}/memos/{memo['id']}/{suffix}",
        headers=_headers(owner_headers, "memo-trailing-reject", if_match=1),
        json=payload,
        follow_redirects=False,
    )
    assert response.status_code == 400, response.text
    assert _linked_target_counts(client, memo["id"]) == (0, 0, 0)


def test_external_assignee_agreement_never_exposes_raw_personal_memo_source(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    member = _create_member(client, owner_headers, suffix="raw-source-assignee")
    memo = _create_memo(
        client,
        owner_headers,
        key="memo-raw-source-create",
        domain="personal",
        content=RAW_CONTENT,
    )
    materialized = _materialize_task(
        client,
        owner_headers,
        memo,
        key="memo-raw-source-materialize",
        payload=_task_payload(
            1,
            assignee_id=member["id"],
            confirm_personal_disclosure=True,
        ),
    )
    assert materialized.status_code == 201, materialized.text
    task = materialized.json()["task"]
    # The owner-only response retains provenance; the scoped assignee view below must not.
    assert task["source"]["excerpt"] == RAW_EXCERPT

    issued = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/transitions",
        headers=_headers(owner_headers, "memo-raw-source-issue"),
        json={"target_stage": "issued", "expected_version": task["version"]},
    )
    assert issued.status_code == 200, issued.text
    invitation = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/alignment-invitations",
        headers=_headers(owner_headers, "memo-raw-source-invite"),
        json={"expected_version": issued.json()["version"]},
    )
    assert invitation.status_code == 201, invitation.text
    invite = invitation.json()
    exchange = client.post(
        "/api/v1/task-alignments/exchange",
        headers={"Idempotency-Key": "memo-raw-source-exchange"},
        json={
            "invitation_id": invite["invitation_id"],
            "code": invite["code"],
            "client_device_id": "memo-raw-source-assignee-device",
        },
    )
    assert exchange.status_code == 200, exchange.text
    assert RAW_CONTENT not in exchange.text
    assert RAW_EXCERPT not in exchange.text
    assert RAW_SOURCE_REF not in exchange.text
    document = exchange.json()["agreement"]["current_revision"]["document"]
    assert "source" not in document
    assert "origin_memo_id" not in document


V5_TRIGGER_NAMES = (
    "trg_secretary_memo_materialization_binding_insert",
    "trg_secretary_memo_materialization_immutable_update",
    "trg_secretary_memo_materialization_immutable_delete",
    "trg_secretary_materialized_memo_immutable_update",
    "trg_secretary_materialized_memo_immutable_delete",
    "trg_secretary_materialized_task_link_immutable",
    "trg_secretary_materialized_calendar_link_immutable",
)


def _rewind_workspace_v5(client: TestClient) -> None:
    database = client.app.state.workspace_service.database
    with database.transaction() as connection:
        for trigger_name in TASK_CHANGE_V6_TRIGGERS:
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
        for index_name in TASK_CHANGE_V6_INDEXES:
            connection.execute(f"DROP INDEX IF EXISTS {index_name}")
        for table_name in (
            "secretary_task_change_decisions",
            "secretary_task_change_sessions",
            "secretary_task_change_invitations",
            "secretary_task_change_proposals",
        ):
            connection.execute(f"DROP TABLE IF EXISTS {table_name}")
        connection.execute(
            "DELETE FROM secretary_workspace_schema_migrations WHERE version = 6"
        )
        for trigger_name in V5_TRIGGER_NAMES:
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
        connection.execute(
            "DROP INDEX IF EXISTS idx_secretary_tasks_active_origin_memo_unique"
        )
        connection.execute(
            "DROP INDEX IF EXISTS idx_secretary_calendar_active_memo_unique"
        )
        connection.execute("DROP TABLE IF EXISTS secretary_memo_materializations")
        connection.execute(
            "DELETE FROM secretary_workspace_schema_migrations WHERE version = 5"
        )


def _create_generic_task(
    client: TestClient,
    owner_headers: dict[str, str],
    *,
    key: str,
    source: dict[str, Any],
) -> dict[str, Any]:
    response = client.post(
        f"{WORKSPACE_PATH}/tasks",
        headers=_headers(owner_headers, key),
        json=_generic_task_payload(source=source),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_generic_calendar(
    client: TestClient,
    owner_headers: dict[str, str],
    *,
    key: str,
    title: str,
) -> dict[str, Any]:
    payload = {**_generic_calendar_payload(), "title": title}
    response = client.post(
        f"{WORKSPACE_PATH}/calendar",
        headers=_headers(owner_headers, key),
        json=payload,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _legacy_rows(client: TestClient) -> dict[str, list[dict[str, Any]]]:
    with client.app.state.workspace_service.database.connect() as connection:
        result: dict[str, list[dict[str, Any]]] = {}
        for label, table, columns in (
            (
                "memos",
                "secretary_memos",
                (
                    "id, workspace_id, status, confirmation_status, version, "
                    "source_json, authority, deleted_at"
                ),
            ),
            (
                "tasks",
                "secretary_business_tasks",
                "id, workspace_id, origin_memo_id, source_json, deleted_at",
            ),
            (
                "calendar",
                "secretary_calendar_entries",
                "id, workspace_id, memo_id, deleted_at",
            ),
        ):
            rows = connection.execute(
                f"SELECT {columns} FROM {table} ORDER BY id"
            ).fetchall()
            result[label] = [dict(row) for row in rows]
        return result


def test_clean_v4_to_v5_creates_empty_ledger_and_is_replayable(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    _create_memo(client, owner_headers, key="memo-v5-clean-create")
    _create_generic_task(
        client,
        owner_headers,
        key="memo-v5-clean-task",
        source={
            "source_kind": "manual",
            "source_ref": "clean-v4-task",
            "excerpt": "普通任务，不关联备忘",
            "authority": "user_provided",
        },
    )
    _create_generic_calendar(
        client,
        owner_headers,
        key="memo-v5-clean-calendar",
        title="普通日程，不关联备忘",
    )
    _rewind_workspace_v5(client)
    database = client.app.state.workspace_service.database
    before = _legacy_rows(client)

    service = client.app.state.workspace_service
    service.initialize()
    with database.connect() as connection:
        markers = [
            row["version"]
            for row in connection.execute(
                """
                SELECT version FROM secretary_workspace_schema_migrations
                ORDER BY version
                """
            )
        ]
        ledger = connection.execute(
            "SELECT * FROM secretary_memo_materializations ORDER BY memo_id"
        ).fetchall()
        trigger_names = {
            row["name"]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'trigger' AND (
                    name LIKE 'trg_secretary_memo_materialization_%'
                    OR name LIKE 'trg_secretary_materialized_%'
                )
                """
            )
        }
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert markers == [1, 2, 3, 4, 5, 6, 7]
    assert ledger == []
    assert set(V5_TRIGGER_NAMES) <= trigger_names
    assert _legacy_rows(client) == before

    service.initialize()
    with database.connect() as connection:
        after_replay = connection.execute(
            "SELECT * FROM secretary_memo_materializations ORDER BY memo_id"
        ).fetchall()
        assert [
            row["version"]
            for row in connection.execute(
                """
                SELECT version FROM secretary_workspace_schema_migrations
                ORDER BY version
                """
            )
        ] == [1, 2, 3, 4, 5, 6, 7]
    assert after_replay == []
    assert _legacy_rows(client) == before


def _prepare_ambiguous_v4_state(
    client: TestClient,
    owner_headers: dict[str, str],
    scenario: str,
) -> None:
    memo = _create_memo(client, owner_headers, key="memo-v5-ambiguous-create")
    task: dict[str, Any] | None = None
    calendars: list[dict[str, Any]] = []
    if scenario not in {"calendar", "multi_calendar", "orphan_converted"}:
        task = _create_generic_task(
            client,
            owner_headers,
            key="memo-v5-ambiguous-task",
            source=memo["source"],
        )
    if scenario in {"calendar", "multi_calendar", "cross_type"}:
        calendars.append(
            _create_generic_calendar(
                client,
                owner_headers,
                key="memo-v5-ambiguous-calendar-1",
                title="历史日程一",
            )
        )
    if scenario == "multi_calendar":
        calendars.append(
            _create_generic_calendar(
                client,
                owner_headers,
                key="memo-v5-ambiguous-calendar-2",
                title="历史日程二",
            )
        )

    _rewind_workspace_v5(client)
    database = client.app.state.workspace_service.database
    if scenario == "missing_memo":
        assert task is not None
        with database.connect() as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                "UPDATE secretary_business_tasks SET origin_memo_id = ? WHERE id = ?",
                ("memo_missing_v5", task["id"]),
            )
            connection.execute("PRAGMA foreign_keys = ON")
        return

    with database.transaction() as connection:
        version = 1 if scenario == "version_one" else 2
        status = "active" if scenario == "wrong_status" else "converted"
        confirmation = "pending" if scenario == "pending_confirmation" else "not_required"
        source_deleted_at = (
            "2026-08-02T01:00:00Z" if scenario == "source_deleted" else None
        )
        connection.execute(
            """
            UPDATE secretary_memos
            SET status = ?, confirmation_status = ?, version = ?, deleted_at = ?
            WHERE id = ?
            """,
            (status, confirmation, version, source_deleted_at, memo["id"]),
        )
        if task is not None:
            connection.execute(
                "UPDATE secretary_business_tasks SET origin_memo_id = ? WHERE id = ?",
                (memo["id"], task["id"]),
            )
        for calendar in calendars:
            connection.execute(
                "UPDATE secretary_calendar_entries SET memo_id = ? WHERE id = ?",
                (memo["id"], calendar["id"]),
            )
        if scenario == "target_deleted":
            assert task is not None
            connection.execute(
                """
                UPDATE secretary_business_tasks
                SET deleted_at = '2026-08-02T01:00:00Z' WHERE id = ?
                """,
                (task["id"],),
            )
        elif scenario == "cross_workspace":
            assert task is not None
            connection.execute(
                """
                INSERT INTO secretary_workspaces(
                    id, name, timezone, version, created_at, updated_at
                ) VALUES (
                    'ws_legacy_other', '旧工作区', 'Asia/Shanghai', 1,
                    '2026-08-02T00:00:00Z', '2026-08-02T00:00:00Z'
                )
                """
            )
            connection.execute(
                """
                UPDATE secretary_business_tasks SET workspace_id = 'ws_legacy_other'
                WHERE id = ?
                """,
                (task["id"],),
            )
        elif scenario == "source_mismatch":
            assert task is not None
            connection.execute(
                "UPDATE secretary_business_tasks SET source_json = '{}' WHERE id = ?",
                (task["id"],),
            )
        elif scenario == "authority_mismatch":
            connection.execute(
                "UPDATE secretary_memos SET authority = 'authoritative' WHERE id = ?",
                (memo["id"],),
            )


@pytest.mark.parametrize(
    ("scenario", "diagnostic"),
    [
        ("task", "缺少可验证的原子转换与披露审计证明"),
        ("calendar", "历史日程链接无法证明"),
        ("multi_calendar", "多个物化目标"),
        ("cross_type", "多个物化目标"),
        ("cross_workspace", "跨工作区"),
        ("missing_memo", "缺少来源备忘"),
        ("source_deleted", "来源备忘已软删除"),
        ("target_deleted", "已软删除"),
        ("wrong_status", "状态不是 converted"),
        ("pending_confirmation", "未完成主人确认"),
        ("version_one", "缺少转换前版本"),
        ("source_mismatch", "来源与备忘不一致"),
        ("authority_mismatch", "authority 元数据不一致"),
        ("orphan_converted", "无物化账本的 converted 备忘"),
    ],
)
def test_v5_ambiguous_history_fails_closed_and_rolls_back(
    client: TestClient,
    owner_headers: dict[str, str],
    scenario: str,
    diagnostic: str,
) -> None:
    _prepare_ambiguous_v4_state(client, owner_headers, scenario)
    before = _legacy_rows(client)
    service = client.app.state.workspace_service
    with pytest.raises(RuntimeError, match=diagnostic):
        service.initialize()

    assert _legacy_rows(client) == before
    with service.database.connect() as connection:
        markers = [
            row["version"]
            for row in connection.execute(
                """
                SELECT version FROM secretary_workspace_schema_migrations
                ORDER BY version
                """
            )
        ]
        materialization_table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'secretary_memo_materializations'
            """
        ).fetchone()
        v5_triggers = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'trigger' AND (
                name LIKE 'trg_secretary_memo_materialization_%'
                OR name LIKE 'trg_secretary_materialized_%'
            )
            """
        ).fetchall()
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert markers == [1, 2, 3, 4, 7]
    assert materialization_table is None
    assert v5_triggers == []
