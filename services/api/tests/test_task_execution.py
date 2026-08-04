from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import centaur_pocket.workspace.service as workspace_service_module
from centaur_pocket.config import Settings
from centaur_pocket.main import create_app

WORKSPACE_ID = "ws_default"
WORKSPACE_PATH = f"/api/v1/workspaces/{WORKSPACE_ID}"
OWNER_ID = "member_owner"

EXECUTION_VIEW_KEYS = {
    "id",
    "title",
    "purpose",
    "objective",
    "strategy",
    "key_points",
    "acceptance_criteria",
    "start_at",
    "due_at",
    "stage",
    "health",
    "priority",
    "progress",
    "change_pending",
    "own_checkins",
    "steps",
    "version",
    "updated_at",
}
EXECUTION_STEP_KEYS = {
    "id",
    "parent_step_id",
    "step_type",
    "title",
    "description",
    "status",
    "position",
    "due_at",
    "success_metric",
    "depends_on_step_ids",
    "completed_at",
    "version",
    "editable",
}
INVITATION_KEYS = {
    "invitation_id",
    "task_id",
    "task_version",
    "assignment_epoch",
    "assignee_member_id",
    "expires_at",
    "capability_expires_at",
    "code",
    "assignee_label",
    "confirmation_path",
}
STRONG_EXECUTION_ETAG = re.compile(r'^"task-execution-v1-[0-9a-f]{64}"$')


def _owner_headers(
    owner_headers: dict[str, str],
    key: str,
    *,
    version: int | None = None,
    device_id: str = "execution-owner-device",
) -> dict[str, str]:
    headers = {
        **owner_headers,
        "Idempotency-Key": key,
        "X-Device-ID": device_id,
    }
    if version is not None:
        headers["If-Match"] = f'"{version}"'
    return headers


def _create_external_member(
    client: TestClient,
    owner_headers: dict[str, str],
    suffix: str,
) -> dict[str, Any]:
    response = client.post(
        f"{WORKSPACE_PATH}/members",
        headers=_owner_headers(owner_headers, f"execution-member-{suffix}"),
        json={
            "kind": "external",
            "role": "member",
            "display_name": f"执行承办人-{suffix}",
            "contact_ref": f"wecom://execution/{suffix}",
            "client_mutation_id": f"execution-member-local-{suffix}",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_aligned_task(
    client: TestClient,
    owner_headers: dict[str, str],
    suffix: str,
    *,
    with_steps: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    assignee = _create_external_member(client, owner_headers, suffix)
    steps: list[dict[str, Any]] = []
    if with_steps:
        steps = [
            {
                "step_type": "action",
                "title": "承办人步骤",
                "assignee_member_id": assignee["id"],
                "position": 0,
            },
            {
                "step_type": "action",
                "title": "主人步骤",
                "assignee_member_id": OWNER_ID,
                "position": 1,
            },
        ]
    created = client.post(
        f"{WORKSPACE_PATH}/tasks",
        headers=_owner_headers(owner_headers, f"execution-task-{suffix}"),
        json={
            "domain": "work",
            "title": f"外部执行任务-{suffix}",
            "purpose": "形成可验证的业务价值",
            "objective": "按协议完成并提交验收",
            "strategy": "先启动，再持续回报并完成步骤。",
            "key_points": ["资源已确认", "风险须每日暴露"],
            "acceptance_criteria": ["交付物完整", "主人验收通过"],
            "issuer_member_id": OWNER_ID,
            "assignee_member_id": assignee["id"],
            "acceptance_owner_id": OWNER_ID,
            "priority": "high",
            "tier": "strategic",
            "health": "on_track",
            "due_at": "2035-08-20T18:00:00+08:00",
            "steps": steps,
            "client_mutation_id": f"execution-task-local-{suffix}",
        },
    )
    assert created.status_code == 201, created.text
    task = created.json()
    issued = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/transitions",
        headers=_owner_headers(owner_headers, f"execution-issue-{suffix}"),
        json={"target_stage": "issued", "expected_version": task["version"]},
    )
    assert issued.status_code == 200, issued.text
    task = issued.json()

    invitation = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/alignment-invitations",
        headers=_owner_headers(owner_headers, f"execution-align-invite-{suffix}"),
        json={"expected_version": task["version"]},
    )
    assert invitation.status_code == 201, invitation.text
    alignment_invitation = invitation.json()
    exchange = client.post(
        "/api/v1/task-alignments/exchange",
        headers={"Idempotency-Key": f"execution-align-exchange-{suffix}"},
        json={
            "invitation_id": alignment_invitation["invitation_id"],
            "code": alignment_invitation["code"],
            "client_device_id": f"alignment-device-{suffix}",
        },
    )
    assert exchange.status_code == 200, exchange.text
    alignment = exchange.json()
    agreement = alignment["agreement"]
    revision = agreement["current_revision"]
    accepted = client.post(
        f"/api/v1/task-agreements/{agreement['id']}/responses",
        headers={
            "Authorization": f"Bearer {alignment['access_token']}",
            "X-Device-ID": alignment["session"]["client_device_id"],
            "Idempotency-Key": f"execution-align-accept-{suffix}",
            "If-Match": f'"{agreement["version"]}"',
        },
        json={
            "expected_agreement_version": agreement["version"],
            "revision_id": revision["id"],
            "expected_digest": revision["digest"],
            "action": "accept",
            "reason": None,
            "counter_document": None,
            "client_mutation_id": f"execution-align-accept-local-{suffix}",
        },
    )
    assert accepted.status_code == 200, accepted.text
    listed = client.get(f"{WORKSPACE_PATH}/tasks", headers=owner_headers)
    assert listed.status_code == 200, listed.text
    aligned = next(item for item in listed.json()["items"] if item["id"] == task["id"])
    assert aligned["stage"] == "aligned"
    return aligned, assignee


def _issue_execution_invitation(
    client: TestClient,
    owner_headers: dict[str, str],
    task: dict[str, Any],
    suffix: str,
) -> tuple[dict[str, Any], Any]:
    response = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/execution-invitations",
        headers=_owner_headers(
            owner_headers,
            f"execution-invite-{suffix}",
            version=task["version"],
        ),
        json={"expected_task_version": task["version"]},
    )
    assert response.status_code == 201, response.text
    return response.json(), response


def _exchange_execution(
    client: TestClient,
    invitation: dict[str, Any],
    suffix: str,
    *,
    code: str | None = None,
    key: str | None = None,
    device_id: str | None = None,
) -> tuple[dict[str, Any], Any]:
    response = client.post(
        "/api/v1/task-executions/exchange",
        headers={"Idempotency-Key": key or f"execution-exchange-{suffix}"},
        json={
            "invitation_id": invitation["invitation_id"],
            "code": code or invitation["code"],
            "client_device_id": device_id or f"execution-device-{suffix}",
        },
    )
    assert response.status_code == 200, response.text
    return response.json(), response


def _ready_execution(
    client: TestClient,
    owner_headers: dict[str, str],
    suffix: str,
    *,
    with_steps: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    task, assignee = _create_aligned_task(
        client, owner_headers, suffix, with_steps=with_steps
    )
    invitation, _response = _issue_execution_invitation(
        client, owner_headers, task, suffix
    )
    exchange, _exchange_response = _exchange_execution(client, invitation, suffix)
    return task, assignee, invitation, exchange


def _access_headers(
    exchange: dict[str, Any],
    *,
    key: str | None = None,
    etag: str | None = None,
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {exchange['access_token']}",
        "X-Device-ID": exchange["session"]["client_device_id"],
    }
    if key is not None:
        headers["Idempotency-Key"] = key
    if etag is not None:
        headers["If-Match"] = etag
    return headers


def _get_execution_view(
    client: TestClient,
    task_id: str,
    exchange: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    response = client.get(
        f"/api/v1/task-executions/{task_id}",
        headers=_access_headers(exchange),
    )
    assert response.status_code == 200, response.text
    assert STRONG_EXECUTION_ETAG.fullmatch(response.headers["etag"])
    return response.json(), response.headers["etag"]


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def test_public_workbench_is_opt_in_and_disabled_issue_fails_without_write(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_root=tmp_path / "disabled-public-workbench",
        owner_token="cp_owner_execution-disabled",
        agent_token="cp_live_execution-disabled",
        scheduler_poll_seconds=0,
    )
    with TestClient(create_app(settings)) as disabled_client:
        response = disabled_client.post(
            f"{WORKSPACE_PATH}/tasks/task_disabled/execution-invitations",
            headers={
                "Authorization": "Bearer cp_owner_execution-disabled",
                "Idempotency-Key": "execution-disabled-invitation",
                "X-Device-ID": "execution-disabled-device",
                "If-Match": '"1"',
            },
            json={"expected_task_version": 1},
        )
        assert response.status_code == 503, response.text
        assert "尚未启用" in response.text
        assert response.headers["cache-control"].startswith("no-store")
        with disabled_client.app.state.workspace_service.database.connect() as connection:
            invitation_count = connection.execute(
                "SELECT COUNT(*) FROM secretary_task_execution_invitations"
            ).fetchone()[0]
        browser_route = disabled_client.get(
            "https://tasks.example.test/api/v1/"
            "task-execution-invitations/execution_invite_missing"
        )

    assert invitation_count == 0
    assert browser_route.status_code == 404


def test_public_workbench_boundary_is_outside_cors_and_adds_security_headers(
    client: TestClient,
) -> None:
    response = client.options(
        "/api/v1/task-execution-invitations/execution_invite_probe",
        headers={
            "Origin": "http://localhost:8081",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 405
    assert response.headers["cache-control"].startswith("no-store")
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["strict-transport-security"].startswith(
        "max-age=31536000"
    )
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert not any(
        name.lower().startswith("access-control-") for name in response.headers
    )


def test_owner_invitation_dual_channel_exchange_and_minimal_projection(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    task, _assignee = _create_aligned_task(
        client, owner_headers, "owner-projection", with_steps=True
    )
    invitation, issued = _issue_execution_invitation(
        client, owner_headers, task, "owner-projection"
    )
    assert set(invitation) == INVITATION_KEYS
    assert invitation["task_version"] == task["version"]
    assert invitation["assignment_epoch"] >= 1
    assert re.fullmatch(
        r"[0-9A-HJKMNP-TV-Z]{4}(?:-[0-9A-HJKMNP-TV-Z]{4}){2}", invitation["code"]
    )
    assert issued.headers["etag"] == f'"{task["version"]}"'
    assert issued.headers["cache-control"].startswith("no-store")

    replayed_issue = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/execution-invitations",
        headers=_owner_headers(
            owner_headers,
            "execution-invite-owner-projection",
            version=task["version"],
        ),
        json={"expected_task_version": task["version"]},
    )
    assert replayed_issue.status_code == 409
    assert invitation["confirmation_path"].endswith(invitation["invitation_id"])

    owner_mixed = client.post(
        "/api/v1/task-executions/exchange",
        headers={
            **owner_headers,
            "Idempotency-Key": "execution-owner-mixed-exchange",
        },
        json={
            "invitation_id": invitation["invitation_id"],
            "code": invitation["code"],
            "client_device_id": "execution-owner-mixed-device",
        },
    )
    assert owner_mixed.status_code == 403

    exchange, exchange_response = _exchange_execution(
        client, invitation, "owner-projection"
    )
    assert set(exchange) == {
        "token_type",
        "access_token",
        "access_expires_at",
        "refresh_token",
        "refresh_expires_at",
        "session",
    }
    assert exchange["token_type"] == "Bearer"
    assert exchange["access_token"].startswith("cp_task_ex_")
    assert exchange["refresh_token"].startswith("cp_task_er_")
    assert exchange_response.headers["cache-control"].startswith("no-store")
    assert (
        599
        <= (
            _parse(exchange["access_expires_at"])
            - _parse(exchange["session"]["created_at"])
        ).total_seconds()
        <= 600
    )
    assert (
        604799
        <= (
            _parse(exchange["refresh_expires_at"])
            - _parse(exchange["session"]["created_at"])
        ).total_seconds()
        <= 604800
    )

    exact_replay, _ = _exchange_execution(
        client,
        invitation,
        "owner-projection-replay",
        key="execution-exchange-owner-projection",
        device_id="execution-device-owner-projection",
    )
    assert exact_replay == exchange

    projection, etag = _get_execution_view(client, task["id"], exchange)
    assert set(projection) == EXECUTION_VIEW_KEYS
    assert all(set(step) == EXECUTION_STEP_KEYS for step in projection["steps"])
    assert [step["editable"] for step in projection["steps"]] == [False, False]
    assert projection["own_checkins"] == []
    assert etag.startswith('"task-execution-v1-')
    forbidden = {
        "workspace_id",
        "issuer_member_id",
        "assignee_member_id",
        "acceptance_owner_id",
        "source",
        "contact_ref",
        "evidence",
        "events",
    }
    assert forbidden.isdisjoint(projection)

    service = client.app.state.workspace_service
    with service.database.connect() as connection:
        dump = "\n".join(connection.iterdump())
    assert invitation["code"] not in dump
    assert exchange["access_token"] not in dump
    assert exchange["refresh_token"] not in dump


def test_exchange_failure_budget_expiry_and_token_prefix_fail_closed(
    client: TestClient,
    owner_headers: dict[str, str],
    monkeypatch: Any,
) -> None:
    task, _assignee = _create_aligned_task(client, owner_headers, "fail-budget")
    invitation, _ = _issue_execution_invitation(
        client, owner_headers, task, "fail-budget"
    )
    replacement = "1" if invitation["code"][0] != "1" else "2"
    wrong_code = replacement + invitation["code"][1:]
    for attempt in range(5):
        failed = client.post(
            "/api/v1/task-executions/exchange",
            headers={"Idempotency-Key": f"execution-wrong-{attempt}"},
            json={
                "invitation_id": invitation["invitation_id"],
                "code": wrong_code,
                "client_device_id": "execution-failure-device",
            },
        )
        assert failed.status_code == 401
        assert invitation["code"] not in failed.text
        assert failed.headers["cache-control"].startswith("no-store")
    exhausted = client.post(
        "/api/v1/task-executions/exchange",
        headers={"Idempotency-Key": "execution-after-exhaustion"},
        json={
            "invitation_id": invitation["invitation_id"],
            "code": invitation["code"],
            "client_device_id": "execution-failure-device",
        },
    )
    assert exhausted.status_code == 401
    service = client.app.state.workspace_service
    with service.database.connect() as connection:
        exhausted_row = connection.execute(
            "SELECT failed_attempts, revoke_reason "
            "FROM secretary_task_execution_invitations WHERE id = ?",
            (invitation["invitation_id"],),
        ).fetchone()
    assert exhausted_row["failed_attempts"] == 5
    assert exhausted_row["revoke_reason"] == "attempts_exhausted"

    expiring_task, _ = _create_aligned_task(client, owner_headers, "expired")
    expired_invitation, _ = _issue_execution_invitation(
        client, owner_headers, expiring_task, "expired"
    )
    real_datetime = datetime
    expired_now = _parse(expired_invitation["expires_at"]) + timedelta(seconds=1)

    class ExpiredDateTime(real_datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return expired_now if tz is None else expired_now.astimezone(tz)

    monkeypatch.setattr(workspace_service_module, "datetime", ExpiredDateTime)
    expired = client.post(
        "/api/v1/task-executions/exchange",
        headers={"Idempotency-Key": "execution-expired-exchange"},
        json={
            "invitation_id": expired_invitation["invitation_id"],
            "code": expired_invitation["code"],
            "client_device_id": "execution-expired-device",
        },
    )
    assert expired.status_code == 401
    monkeypatch.setattr(workspace_service_module, "datetime", real_datetime)

    live_task, _member, _live_invitation, exchange = _ready_execution(
        client, owner_headers, "prefixes"
    )
    path = f"/api/v1/task-executions/{live_task['id']}"
    for token, expected in (
        ("cp_task_at_" + "a" * 43, 401),
        ("cp_task_ch_" + "a" * 43, 401),
        (exchange["refresh_token"], 401),
        (owner_headers["Authorization"].removeprefix("Bearer "), 403),
    ):
        response = client.get(
            path,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Device-ID": exchange["session"]["client_device_id"],
            },
        )
        assert response.status_code == expected
    mixed = client.get(
        path,
        headers={
            **_access_headers(exchange),
            "X-Owner-Token": owner_headers["Authorization"].removeprefix("Bearer "),
        },
    )
    assert mixed.status_code == 403
    refresh_with_access = client.post(
        "/api/v1/task-executions/refresh",
        headers={"Idempotency-Key": "execution-refresh-access-prefix"},
        json={
            "refresh_token": exchange["access_token"],
            "client_device_id": exchange["session"]["client_device_id"],
        },
    )
    assert refresh_with_access.status_code == 401
    refresh_with_owner = client.post(
        "/api/v1/task-executions/refresh",
        headers={
            **owner_headers,
            "Idempotency-Key": "execution-refresh-owner-context",
        },
        json={
            "refresh_token": exchange["refresh_token"],
            "client_device_id": exchange["session"]["client_device_id"],
        },
    )
    assert refresh_with_owner.status_code == 403


def test_refresh_rotates_without_business_version_and_reuse_revokes_family(
    client: TestClient,
    owner_headers: dict[str, str],
    monkeypatch: Any,
) -> None:
    task, _member, _invitation, exchange = _ready_execution(
        client, owner_headers, "refresh"
    )
    service = client.app.state.workspace_service
    with service.database.connect() as connection:
        family = connection.execute(
            "SELECT * FROM secretary_task_execution_refresh_families "
            "WHERE invitation_id = ?",
            (_invitation["invitation_id"],),
        ).fetchone()
        first_refresh = connection.execute(
            "SELECT * FROM secretary_task_execution_refresh_tokens "
            "WHERE family_id = ? AND generation = 1",
            (family["id"],),
        ).fetchone()
    assert (
        86399
        <= (
            _parse(first_refresh["idle_expires_at"])
            - _parse(first_refresh["created_at"])
        ).total_seconds()
        <= 86400
    )

    body = {
        "refresh_token": exchange["refresh_token"],
        "client_device_id": exchange["session"]["client_device_id"],
    }
    rotated = client.post(
        "/api/v1/task-executions/refresh",
        headers={"Idempotency-Key": "execution-refresh-rotate"},
        json=body,
    )
    assert rotated.status_code == 200, rotated.text
    result = rotated.json()
    assert set(result) == {
        "token_type",
        "access_token",
        "access_expires_at",
        "refresh_token",
        "refresh_expires_at",
        "session",
        "task",
    }
    assert set(result["task"]) == EXECUTION_VIEW_KEYS
    assert STRONG_EXECUTION_ETAG.fullmatch(rotated.headers["etag"])
    assert result["access_token"] != exchange["access_token"]
    assert result["refresh_token"] != exchange["refresh_token"]
    assert result["session"]["access_generation"] == 2
    assert (
        client.get(
            f"/api/v1/task-executions/{task['id']}",
            headers=_access_headers(exchange),
        ).status_code
        == 401
    )

    exact_replay = client.post(
        "/api/v1/task-executions/refresh",
        headers={"Idempotency-Key": "execution-refresh-rotate"},
        json=body,
    )
    assert exact_replay.status_code == 200, exact_replay.text
    assert exact_replay.json() == result
    assert exact_replay.headers["etag"] == rotated.headers["etag"]
    with service.database.connect() as connection:
        rotation_events = connection.execute(
            """
            SELECT actor_type, actor_member_id, payload_json
            FROM secretary_workspace_events
            WHERE aggregate_id = ?
              AND event_type = 'task.execution_refresh_rotated'
            """,
            (task["id"],),
        ).fetchall()
    assert len(rotation_events) == 1
    assert rotation_events[0]["actor_type"] == "system"
    assert rotation_events[0]["actor_member_id"] is None
    rotation_payload = json.loads(rotation_events[0]["payload_json"])
    assert rotation_payload["generation"] == 2
    assert rotation_payload["session_id"] == result["session"]["id"]
    assert rotation_payload["refresh_family_id"] == family["id"]
    assert "refresh_token" not in rotation_payload

    real_datetime = datetime
    clock = [real_datetime.now(UTC) + timedelta(seconds=31)]

    class FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            value = clock[0]
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(workspace_service_module, "datetime", FrozenDateTime)
    reused = client.post(
        "/api/v1/task-executions/refresh",
        headers={"Idempotency-Key": "execution-refresh-rotate"},
        json={**body, "client_device_id": "execution-used-wrong-device"},
    )
    assert reused.status_code == 401
    assert "重用" in reused.text
    new_access_headers = {
        "Authorization": f"Bearer {result['access_token']}",
        "X-Device-ID": result["session"]["client_device_id"],
    }
    assert (
        client.get(
            f"/api/v1/task-executions/{task['id']}", headers=new_access_headers
        ).status_code
        == 401
    )
    with service.database.connect() as connection:
        revoked = connection.execute(
            "SELECT revoke_reason FROM secretary_task_execution_refresh_families "
            "WHERE id = ?",
            (family["id"],),
        ).fetchone()
        security_events = connection.execute(
            """
            SELECT actor_type, actor_member_id, payload_json
            FROM secretary_workspace_events
            WHERE aggregate_id = ?
              AND event_type = 'task.execution_security_revoked'
            """,
            (task["id"],),
        ).fetchall()
    assert revoked["revoke_reason"] == "refresh_reuse_detected"
    assert len(security_events) == 1
    assert security_events[0]["actor_type"] == "system"
    assert security_events[0]["actor_member_id"] is None
    security_payload = json.loads(security_events[0]["payload_json"])
    assert security_payload["reason"] == "refresh_reuse_detected"
    assert security_payload["refresh_family_id"] == family["id"]
    assert "refresh_token" not in security_payload
    assert "token_hash" not in security_payload


def test_refresh_idle_and_absolute_expiry_are_enforced(
    client: TestClient,
    owner_headers: dict[str, str],
    monkeypatch: Any,
) -> None:
    _idle_task, _idle_member, _idle_invitation, idle_exchange = _ready_execution(
        client, owner_headers, "idle-expiry"
    )
    _absolute_task, _absolute_member, _absolute_invitation, absolute_exchange = (
        _ready_execution(client, owner_headers, "absolute-expiry")
    )
    real_datetime = datetime
    started = real_datetime.now(UTC)
    clock = [started + timedelta(hours=24, seconds=1)]

    class FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            value = clock[0]
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(workspace_service_module, "datetime", FrozenDateTime)
    idle = client.post(
        "/api/v1/task-executions/refresh",
        headers={"Idempotency-Key": "execution-idle-expired"},
        json={
            "refresh_token": idle_exchange["refresh_token"],
            "client_device_id": idle_exchange["session"]["client_device_id"],
        },
    )
    assert idle.status_code == 401

    clock[0] = started + timedelta(days=7, seconds=1)
    absolute = client.post(
        "/api/v1/task-executions/refresh",
        headers={"Idempotency-Key": "execution-absolute-expired"},
        json={
            "refresh_token": absolute_exchange["refresh_token"],
            "client_device_id": absolute_exchange["session"]["client_device_id"],
        },
    )
    assert absolute.status_code == 401
    service = client.app.state.workspace_service
    with service.database.connect() as connection:
        reasons = {
            row["task_id"]: row["revoke_reason"]
            for row in connection.execute(
                "SELECT task_id, revoke_reason "
                "FROM secretary_task_execution_refresh_families"
            ).fetchall()
        }
    assert reasons[_idle_task["id"]] == "refresh_expired"
    assert reasons[_absolute_task["id"]] == "refresh_expired"


def test_refresh_security_revocations_are_ordered_audited_and_deduplicated(
    client: TestClient,
    owner_headers: dict[str, str],
    monkeypatch: Any,
) -> None:
    mismatch_task, _member, _invitation, mismatch_exchange = _ready_execution(
        client, owner_headers, "refresh-mismatch-audit"
    )
    expiry_task, _member, _invitation, expiry_exchange = _ready_execution(
        client, owner_headers, "refresh-expiry-audit"
    )
    integrity_task, _member, _invitation, integrity_exchange = _ready_execution(
        client, owner_headers, "refresh-integrity-audit"
    )
    binding_task, _member, _invitation, binding_exchange = _ready_execution(
        client, owner_headers, "refresh-binding-audit"
    )
    service = client.app.state.workspace_service

    mismatch_body = {
        "refresh_token": mismatch_exchange["refresh_token"],
        "client_device_id": "execution-unused-wrong-device",
    }
    mismatch = client.post(
        "/api/v1/task-executions/refresh",
        headers={"Idempotency-Key": "execution-refresh-mismatch-audit"},
        json=mismatch_body,
    )
    assert mismatch.status_code == 403
    mismatch_repeat = client.post(
        "/api/v1/task-executions/refresh",
        headers={"Idempotency-Key": "execution-refresh-mismatch-repeat"},
        json=mismatch_body,
    )
    assert mismatch_repeat.status_code == 401

    integrity_body = {
        "refresh_token": integrity_exchange["refresh_token"],
        "client_device_id": integrity_exchange["session"]["client_device_id"],
    }
    integrity_rotation = client.post(
        "/api/v1/task-executions/refresh",
        headers={"Idempotency-Key": "execution-refresh-integrity-audit"},
        json=integrity_body,
    )
    assert integrity_rotation.status_code == 200, integrity_rotation.text
    original_hmac_key = service._task_session_hmac_key
    try:
        service._task_session_hmac_key = b"z" * 32
        integrity_failure = client.post(
            "/api/v1/task-executions/refresh",
            headers={"Idempotency-Key": "execution-refresh-integrity-audit"},
            json=integrity_body,
        )
    finally:
        service._task_session_hmac_key = original_hmac_key
    assert integrity_failure.status_code == 409

    with service.database.transaction() as connection:
        connection.execute(
            """
            UPDATE secretary_business_tasks
            SET due_at = '2000-01-01T00:00:00Z', updated_at = ?
            WHERE id = ?
            """,
            (datetime.now(UTC).isoformat(), binding_task["id"]),
        )
    binding = client.post(
        "/api/v1/task-executions/refresh",
        headers={"Idempotency-Key": "execution-refresh-binding-audit"},
        json={
            "refresh_token": binding_exchange["refresh_token"],
            "client_device_id": binding_exchange["session"]["client_device_id"],
        },
    )
    assert binding.status_code == 401

    real_datetime = datetime
    clock = [real_datetime.now(UTC) + timedelta(hours=24, seconds=1)]

    class FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            value = clock[0]
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(workspace_service_module, "datetime", FrozenDateTime)
    expired_wrong_device = client.post(
        "/api/v1/task-executions/refresh",
        headers={"Idempotency-Key": "execution-refresh-expired-wrong-device"},
        json={
            "refresh_token": expiry_exchange["refresh_token"],
            "client_device_id": "execution-expired-wrong-device",
        },
    )
    assert expired_wrong_device.status_code == 401
    expired_repeat = client.post(
        "/api/v1/task-executions/refresh",
        headers={"Idempotency-Key": "execution-refresh-expired-repeat"},
        json={
            "refresh_token": expiry_exchange["refresh_token"],
            "client_device_id": "execution-expired-wrong-device",
        },
    )
    assert expired_repeat.status_code == 401

    expected_reasons = {
        mismatch_task["id"]: "refresh_device_mismatch",
        expiry_task["id"]: "refresh_expired",
        integrity_task["id"]: "token_integrity_failure",
        binding_task["id"]: "binding_not_current",
    }
    with service.database.connect() as connection:
        rows = connection.execute(
            """
            SELECT aggregate_id, actor_type, actor_member_id, payload_json
            FROM secretary_workspace_events
            WHERE event_type = 'task.execution_security_revoked'
              AND aggregate_id IN (?, ?, ?, ?)
            ORDER BY sequence
            """,
            tuple(expected_reasons),
        ).fetchall()
    assert len(rows) == 4
    for row in rows:
        event_payload = json.loads(row["payload_json"])
        assert row["actor_type"] == "system"
        assert row["actor_member_id"] is None
        assert event_payload["reason"] == expected_reasons[row["aggregate_id"]]
        assert event_payload["actor_subject_type"] == "task_execution_capability"
        assert event_payload["on_behalf_of_member_id"]
        assert isinstance(event_payload["generation"], int)
        assert "refresh_token" not in event_payload
        assert "token_hash" not in event_payload
        assert "request_body" not in event_payload


def test_execution_commands_etag_idempotency_pending_change_and_submit(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    task, assignee, _invitation, exchange = _ready_execution(
        client, owner_headers, "commands", with_steps=True
    )
    projection, etag = _get_execution_view(client, task["id"], exchange)
    stale = client.post(
        f"/api/v1/task-executions/{task['id']}/start",
        headers=_access_headers(
            exchange,
            key="execution-start-stale",
            etag='"task-execution-v1-' + "0" * 64 + '"',
        ),
        json={
            "expected_task_version": projection["version"],
            "client_mutation_id": "execution-start-stale-local",
        },
    )
    assert stale.status_code == 412

    start_headers = _access_headers(exchange, key="execution-start", etag=etag)
    start_body = {
        "expected_task_version": projection["version"],
        "client_mutation_id": "execution-start-local",
        "note": "现在启动",
    }
    started = client.post(
        f"/api/v1/task-executions/{task['id']}/start",
        headers=start_headers,
        json=start_body,
    )
    assert started.status_code == 200, started.text
    assert started.json()["task"]["stage"] == "in_progress"
    assert STRONG_EXECUTION_ETAG.fullmatch(started.headers["etag"])
    replay = client.post(
        f"/api/v1/task-executions/{task['id']}/start",
        headers=start_headers,
        json=start_body,
    )
    assert replay.status_code == 200
    assert replay.json() == started.json()
    assert replay.headers["etag"] == started.headers["etag"]
    conflict = client.post(
        f"/api/v1/task-executions/{task['id']}/start",
        headers=start_headers,
        json={**start_body, "client_mutation_id": "execution-start-conflict"},
    )
    assert conflict.status_code == 409

    current = started.json()["task"]
    checkin_headers = _access_headers(
        exchange,
        key="execution-checkin",
        etag=started.headers["etag"],
    )
    checkin_body = {
        "expected_task_version": current["version"],
        "summary": "已完成首轮验证",
        "reported_progress": 35,
        "risks": ["外部依赖可能延期"],
        "blockers": [],
        "next_actions": ["完成承办人步骤"],
        "forecast_at": "2035-08-18T18:00:00+08:00",
        "client_mutation_id": "execution-checkin-local",
    }
    checkin = client.post(
        f"/api/v1/task-executions/{task['id']}/check-ins",
        headers=checkin_headers,
        json=checkin_body,
    )
    assert checkin.status_code == 201, checkin.text
    assert len(checkin.json()["task"]["own_checkins"]) == 1
    assert (
        checkin.json()["task"]["own_checkins"][0]["summary"] == checkin_body["summary"]
    )
    checkin_replay = client.post(
        f"/api/v1/task-executions/{task['id']}/check-ins",
        headers=checkin_headers,
        json=checkin_body,
    )
    assert checkin_replay.status_code == 201
    assert checkin_replay.json() == checkin.json()

    checkin_task = checkin.json()["task"]
    own_step = next(
        step for step in checkin_task["steps"] if step["title"] == "承办人步骤"
    )
    owner_step = next(
        step for step in checkin_task["steps"] if step["title"] == "主人步骤"
    )
    assert own_step["editable"] is True
    assert owner_step["editable"] is False
    own_update = client.put(
        f"/api/v1/task-executions/{task['id']}/steps/{own_step['id']}/status",
        headers=_access_headers(
            exchange,
            key="execution-own-step",
            etag=checkin.headers["etag"],
        ),
        json={
            "expected_task_version": checkin_task["version"],
            "expected_step_version": own_step["version"],
            "status": "in_progress",
            "note": "开始处理",
            "client_mutation_id": "execution-own-step-local",
        },
    )
    assert own_update.status_code == 200, own_update.text
    assert own_update.json()["step"]["status"] == "in_progress"

    latest = own_update.json()["task"]
    forbidden = client.put(
        f"/api/v1/task-executions/{task['id']}/steps/{owner_step['id']}/status",
        headers=_access_headers(
            exchange,
            key="execution-owner-step",
            etag=own_update.headers["etag"],
        ),
        json={
            "expected_task_version": latest["version"],
            "expected_step_version": owner_step["version"],
            "status": "in_progress",
            "client_mutation_id": "execution-owner-step-local",
        },
    )
    assert forbidden.status_code == 403

    proposed = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/changes",
        headers=_owner_headers(owner_headers, "execution-pending-change"),
        json={
            "change_type": "due_at",
            "base_version": latest["version"],
            "reason": "等待承办人确认延期",
            "patch": {"due_at": "2035-08-30T18:00:00+08:00"},
            "client_mutation_id": "execution-pending-change-local",
        },
    )
    assert proposed.status_code == 201, proposed.text
    pending_view, pending_etag = _get_execution_view(client, task["id"], exchange)
    assert pending_view["change_pending"] is True
    pending_own_step = next(
        step for step in pending_view["steps"] if step["id"] == own_step["id"]
    )
    assert pending_own_step["editable"] is False
    blocked = client.put(
        f"/api/v1/task-executions/{task['id']}/steps/{own_step['id']}/status",
        headers=_access_headers(
            exchange,
            key="execution-step-pending-change",
            etag=pending_etag,
        ),
        json={
            "expected_task_version": pending_view["version"],
            "expected_step_version": own_update.json()["step"]["version"],
            "status": "blocked",
            "client_mutation_id": "execution-step-pending-change-local",
        },
    )
    assert blocked.status_code == 409
    assert "待确认变更" in blocked.text

    clean_task, _clean_member, _clean_invitation, clean_exchange = _ready_execution(
        client, owner_headers, "submit"
    )
    clean_view, clean_etag = _get_execution_view(
        client, clean_task["id"], clean_exchange
    )
    owner_start = client.post(
        f"{WORKSPACE_PATH}/tasks/{clean_task['id']}/transitions",
        headers=_owner_headers(owner_headers, "execution-owner-cannot-start"),
        json={
            "target_stage": "in_progress",
            "expected_version": clean_view["version"],
        },
    )
    assert owner_start.status_code == 409
    clean_start = client.post(
        f"/api/v1/task-executions/{clean_task['id']}/start",
        headers=_access_headers(
            clean_exchange, key="execution-submit-start", etag=clean_etag
        ),
        json={
            "expected_task_version": clean_view["version"],
            "client_mutation_id": "execution-submit-start-local",
        },
    )
    assert clean_start.status_code == 200, clean_start.text
    owner_submit = client.post(
        f"{WORKSPACE_PATH}/tasks/{clean_task['id']}/transitions",
        headers=_owner_headers(owner_headers, "execution-owner-cannot-submit"),
        json={
            "target_stage": "submitted",
            "expected_version": clean_start.json()["task"]["version"],
        },
    )
    assert owner_submit.status_code == 409
    owner_abnormal_close = client.post(
        f"{WORKSPACE_PATH}/tasks/{clean_task['id']}/transitions",
        headers=_owner_headers(owner_headers, "execution-owner-cannot-close"),
        json={
            "target_stage": "abnormal_closed",
            "expected_version": clean_start.json()["task"]["version"],
            "note": "不能绕过正式变更",
        },
    )
    assert owner_abnormal_close.status_code == 422
    submit = client.post(
        f"/api/v1/task-executions/{clean_task['id']}/submit",
        headers=_access_headers(
            clean_exchange,
            key="execution-submit",
            etag=clean_start.headers["etag"],
        ),
        json={
            "expected_task_version": clean_start.json()["task"]["version"],
            "client_mutation_id": "execution-submit-local",
            "note": "请主人验收",
        },
    )
    assert submit.status_code == 200, submit.text
    assert submit.json()["task"]["stage"] == "submitted"
    assert submit.json()["task"]["progress"] == 100
    missing_rework_note = client.post(
        f"{WORKSPACE_PATH}/tasks/{clean_task['id']}/transitions",
        headers=_owner_headers(owner_headers, "execution-rework-missing-note"),
        json={
            "target_stage": "in_progress",
            "expected_version": submit.json()["task"]["version"],
        },
    )
    assert missing_rework_note.status_code == 422
    returned = client.post(
        f"{WORKSPACE_PATH}/tasks/{clean_task['id']}/transitions",
        headers=_owner_headers(owner_headers, "execution-return-for-rework"),
        json={
            "target_stage": "in_progress",
            "expected_version": submit.json()["task"]["version"],
            "note": "验收材料需要补充说明",
        },
    )
    assert returned.status_code == 200, returned.text
    assert returned.json()["stage"] == "in_progress"
    assert returned.json()["submitted_at"] is None
    resumed_view, resumed_etag = _get_execution_view(
        client, clean_task["id"], clean_exchange
    )
    resumed_submit = client.post(
        f"/api/v1/task-executions/{clean_task['id']}/submit",
        headers=_access_headers(
            clean_exchange,
            key="execution-resumed-submit",
            etag=resumed_etag,
        ),
        json={
            "expected_task_version": resumed_view["version"],
            "client_mutation_id": "execution-resumed-submit-local",
            "note": "已补充材料，再次提交",
        },
    )
    assert resumed_submit.status_code == 200, resumed_submit.text
    assert resumed_submit.json()["task"]["stage"] == "submitted"
    assert assignee["id"] == task["assignee_member_id"]


def test_assignment_due_member_and_terminal_binding_revocations(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    service = client.app.state.workspace_service

    assignment_task, _member, _invitation, assignment_exchange = _ready_execution(
        client, owner_headers, "binding-assignment"
    )
    replacement = _create_external_member(client, owner_headers, "replacement")
    with service.database.transaction() as connection:
        connection.execute(
            "UPDATE secretary_business_tasks "
            "SET assignee_member_id = ?, assignment_epoch = assignment_epoch + 1, "
            "updated_at = ? WHERE id = ?",
            (replacement["id"], datetime.now(UTC).isoformat(), assignment_task["id"]),
        )
    assert (
        client.get(
            f"/api/v1/task-executions/{assignment_task['id']}",
            headers=_access_headers(assignment_exchange),
        ).status_code
        == 401
    )

    due_task, _member, _invitation, due_exchange = _ready_execution(
        client, owner_headers, "binding-due"
    )
    with service.database.transaction() as connection:
        connection.execute(
            "UPDATE secretary_business_tasks SET due_at = ?, updated_at = ? "
            "WHERE id = ?",
            (
                "2000-01-01T00:00:00Z",
                datetime.now(UTC).isoformat(),
                due_task["id"],
            ),
        )
    assert (
        client.get(
            f"/api/v1/task-executions/{due_task['id']}",
            headers=_access_headers(due_exchange),
        ).status_code
        == 401
    )

    inactive_task, inactive_member, _invitation, inactive_exchange = _ready_execution(
        client, owner_headers, "binding-inactive"
    )
    with service.database.transaction() as connection:
        connection.execute(
            "UPDATE secretary_workspace_members SET active = 0, updated_at = ? "
            "WHERE id = ?",
            (datetime.now(UTC).isoformat(), inactive_member["id"]),
        )
    assert (
        client.get(
            f"/api/v1/task-executions/{inactive_task['id']}",
            headers=_access_headers(inactive_exchange),
        ).status_code
        == 401
    )

    terminal_task, _member, _invitation, terminal_exchange = _ready_execution(
        client, owner_headers, "binding-terminal"
    )
    with service.database.transaction() as connection:
        connection.execute(
            "UPDATE secretary_business_tasks SET stage = 'accepted', updated_at = ? "
            "WHERE id = ?",
            (datetime.now(UTC).isoformat(), terminal_task["id"]),
        )
    assert (
        client.get(
            f"/api/v1/task-executions/{terminal_task['id']}",
            headers=_access_headers(terminal_exchange),
        ).status_code
        == 401
    )

    with service.database.connect() as connection:
        reasons = {
            row["task_id"]: row["revoke_reason"]
            for row in connection.execute(
                "SELECT task_id, revoke_reason "
                "FROM secretary_task_execution_refresh_families"
            ).fetchall()
        }
    assert reasons[assignment_task["id"]] == "assignment_changed"
    assert reasons[due_task["id"]] == "binding_not_current"
    assert reasons[inactive_task["id"]] == "assignee_deactivated"
    assert reasons[terminal_task["id"]] == "task_terminal"


def test_v7_schema_verifier_rejects_forged_partial_unique_index_despite_marker(
    client: TestClient,
) -> None:
    service = client.app.state.workspace_service
    index_name = "idx_secretary_one_active_execution_session"
    with service.database.transaction() as connection:
        marker = connection.execute(
            "SELECT applied_at FROM secretary_workspace_schema_migrations "
            "WHERE version = 7"
        ).fetchone()
        assert marker is not None
        connection.execute(f"DROP INDEX {index_name}")
        connection.execute(
            f"""
            CREATE UNIQUE INDEX {index_name}
            ON secretary_task_execution_sessions(task_id)
            WHERE revoked_at IS NULL OR 1 = 1
            """
        )
        forged = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (index_name,),
        ).fetchone()
        assert forged is not None
        assert "or 1 = 1" in " ".join(str(forged["sql"]).lower().split())

    # A v7 marker is not sufficient evidence: startup must verify the exact
    # security predicate and fail closed instead of accepting a weaker index.
    with pytest.raises(RuntimeError):
        service.initialize()
    with service.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM secretary_workspace_schema_migrations WHERE version = 7"
            ).fetchone()
            is not None
        )
        still_forged = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (index_name,),
        ).fetchone()
    assert still_forged is not None
    assert "or 1 = 1" in " ".join(str(still_forged["sql"]).lower().split())


def test_v7_schema_verifier_preserves_literal_case_in_trigger_digest(
    client: TestClient,
) -> None:
    service = client.app.state.workspace_service
    trigger_name = "trg_secretary_task_execution_access_revoke"
    with service.database.transaction() as connection:
        original = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (trigger_name,),
        ).fetchone()
        assert original is not None
        forged_sql = str(original["sql"]).replace(
            "'accepted'", "'ACCEPTED'", 1
        )
        assert forged_sql != original["sql"]
        connection.execute(f"DROP TRIGGER {trigger_name}")
        connection.execute(forged_sql)

    # Lower-casing the whole DDL before hashing would accept this trigger even
    # though lowercase persisted stages would no longer match the forged
    # uppercase literal.
    with pytest.raises(RuntimeError, match="schema 定义与规范不一致"):
        service.initialize()


def test_v7_schema_verifier_rejects_nullable_assignment_epoch_after_marker(
    client: TestClient,
) -> None:
    service = client.app.state.workspace_service
    table_name = "secretary_business_tasks"
    canonical = (
        "assignment_epoch INTEGER NOT NULL DEFAULT 1 "
        "CHECK(assignment_epoch >= 1)"
    )
    forged = "assignment_epoch INTEGER DEFAULT 1 CHECK(assignment_epoch >= 1)"
    with service.database.transaction() as connection:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        assert row is not None and canonical in str(row["sql"])
        forged_sql = str(row["sql"]).replace(canonical, forged, 1)
        schema_version = int(
            connection.execute("PRAGMA schema_version").fetchone()[0]
        )
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = ?",
            (forged_sql, table_name),
        )
        connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
        connection.execute("PRAGMA writable_schema = OFF")

    with service.database.connect() as connection:
        epoch_column = next(
            row
            for row in connection.execute(
                f"PRAGMA table_info({table_name})"
            ).fetchall()
            if row["name"] == "assignment_epoch"
        )
    assert epoch_column["notnull"] == 0
    with pytest.raises(RuntimeError, match="assignment_epoch"):
        service.initialize()


def test_execution_mutations_are_family_stable_cross_key_and_audited(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    task, _assignee, _invitation, exchange = _ready_execution(
        client, owner_headers, "mutation-family", with_steps=True
    )
    initial, initial_etag = _get_execution_view(client, task["id"], exchange)
    own_step = next(step for step in initial["steps"] if step["title"] == "承办人步骤")

    start_body = {
        "expected_task_version": initial["version"],
        "client_mutation_id": "execution-family-start-mutation",
        "note": "stable family start",
    }
    start = client.post(
        f"/api/v1/task-executions/{task['id']}/start",
        headers=_access_headers(
            exchange,
            key="execution-family-start-first",
            etag=initial_etag,
        ),
        json=start_body,
    )
    assert start.status_code == 200, start.text

    rotated = client.post(
        "/api/v1/task-executions/refresh",
        headers={"Idempotency-Key": "execution-family-rotate"},
        json={
            "refresh_token": exchange["refresh_token"],
            "client_device_id": exchange["session"]["client_device_id"],
        },
    )
    assert rotated.status_code == 200, rotated.text
    family_exchange = rotated.json()
    assert family_exchange["session"]["access_generation"] == 2

    start_cross_key = client.post(
        f"/api/v1/task-executions/{task['id']}/start",
        headers=_access_headers(
            family_exchange,
            key="execution-family-start-second-key",
            etag=initial_etag,
        ),
        json=start_body,
    )
    assert start_cross_key.status_code == 200, start_cross_key.text
    assert start_cross_key.json() == start.json()
    assert start_cross_key.headers["etag"] == start.headers["etag"]

    replacement_invitation, _replacement_issue = _issue_execution_invitation(
        client,
        owner_headers,
        start.json()["task"],
        "mutation-family-reissued",
    )
    replacement_exchange, _replacement_exchange_response = _exchange_execution(
        client,
        replacement_invitation,
        "mutation-family-reissued",
    )
    service = client.app.state.workspace_service
    with service.database.connect() as connection:
        family_ids = {
            row["refresh_family_id"]
            for row in connection.execute(
                """
                SELECT refresh_family_id
                FROM secretary_task_execution_sessions
                WHERE id IN (?, ?)
                """,
                (
                    replacement_exchange["session"]["id"],
                    family_exchange["session"]["id"],
                ),
            ).fetchall()
        }
    assert len(family_ids) == 2
    start_cross_family = client.post(
        f"/api/v1/task-executions/{task['id']}/start",
        headers=_access_headers(
            replacement_exchange,
            # HTTP idempotency remains refresh-family scoped: the same key can
            # be rebound to the exact assignment-level mutation replay.
            key="execution-family-start-first",
            etag=initial_etag,
        ),
        json=start_body,
    )
    assert start_cross_family.status_code == 200, start_cross_family.text
    assert start_cross_family.json() == start.json()
    assert start_cross_family.headers["etag"] == start.headers["etag"]
    family_exchange = replacement_exchange

    start_changed = client.post(
        f"/api/v1/task-executions/{task['id']}/start",
        headers=_access_headers(
            family_exchange,
            key="execution-family-start-changed",
            etag=initial_etag,
        ),
        json={**start_body, "note": "different start content"},
    )
    assert start_changed.status_code == 409

    started_task = start.json()["task"]
    started_step = next(
        step for step in started_task["steps"] if step["id"] == own_step["id"]
    )
    cross_operation = client.put(
        f"/api/v1/task-executions/{task['id']}/steps/{own_step['id']}/status",
        headers=_access_headers(
            family_exchange,
            key="execution-family-start-cross-operation",
            etag=start.headers["etag"],
        ),
        json={
            "expected_task_version": started_task["version"],
            "expected_step_version": started_step["version"],
            "status": "in_progress",
            "client_mutation_id": start_body["client_mutation_id"],
        },
    )
    assert cross_operation.status_code == 409

    checkin_body = {
        "expected_task_version": started_task["version"],
        "summary": "mutation audit check-in",
        "reported_progress": 20,
        "risks": [],
        "blockers": [],
        "next_actions": ["update own step"],
        "forecast_at": None,
        "client_mutation_id": "execution-family-checkin-mutation",
    }
    checkin = client.post(
        f"/api/v1/task-executions/{task['id']}/check-ins",
        headers=_access_headers(
            family_exchange,
            key="execution-family-checkin",
            etag=start.headers["etag"],
        ),
        json=checkin_body,
    )
    assert checkin.status_code == 201, checkin.text

    step_body = {
        "expected_task_version": checkin.json()["task"]["version"],
        "expected_step_version": started_step["version"],
        "status": "done",
        "note": "stable family step",
        "client_mutation_id": "execution-family-step-mutation",
    }
    step = client.put(
        f"/api/v1/task-executions/{task['id']}/steps/{own_step['id']}/status",
        headers=_access_headers(
            family_exchange,
            key="execution-family-step-first",
            etag=checkin.headers["etag"],
        ),
        json=step_body,
    )
    assert step.status_code == 200, step.text
    step_cross_key = client.put(
        f"/api/v1/task-executions/{task['id']}/steps/{own_step['id']}/status",
        headers=_access_headers(
            family_exchange,
            key="execution-family-step-second-key",
            etag=checkin.headers["etag"],
        ),
        json=step_body,
    )
    assert step_cross_key.status_code == 200, step_cross_key.text
    assert step_cross_key.json() == step.json()
    assert step_cross_key.headers["etag"] == step.headers["etag"]
    step_changed = client.put(
        f"/api/v1/task-executions/{task['id']}/steps/{own_step['id']}/status",
        headers=_access_headers(
            family_exchange,
            key="execution-family-step-changed",
            etag=checkin.headers["etag"],
        ),
        json={**step_body, "note": "different step content"},
    )
    assert step_changed.status_code == 409

    submit_body = {
        "expected_task_version": step.json()["task"]["version"],
        "client_mutation_id": "execution-family-submit-mutation",
        "note": "stable family submit",
    }
    submit = client.post(
        f"/api/v1/task-executions/{task['id']}/submit",
        headers=_access_headers(
            family_exchange,
            key="execution-family-submit-first",
            etag=step.headers["etag"],
        ),
        json=submit_body,
    )
    assert submit.status_code == 200, submit.text
    submit_cross_key = client.post(
        f"/api/v1/task-executions/{task['id']}/submit",
        headers=_access_headers(
            family_exchange,
            key="execution-family-submit-second-key",
            etag=step.headers["etag"],
        ),
        json=submit_body,
    )
    assert submit_cross_key.status_code == 200, submit_cross_key.text
    assert submit_cross_key.json() == submit.json()
    assert submit_cross_key.headers["etag"] == submit.headers["etag"]
    submit_changed = client.post(
        f"/api/v1/task-executions/{task['id']}/submit",
        headers=_access_headers(
            family_exchange,
            key="execution-family-submit-changed",
            etag=step.headers["etag"],
        ),
        json={**submit_body, "note": "different submit content"},
    )
    assert submit_changed.status_code == 409
    submit_cross_operation = client.post(
        f"/api/v1/task-executions/{task['id']}/submit",
        headers=_access_headers(
            family_exchange,
            key="execution-family-submit-cross-operation",
            etag=step.headers["etag"],
        ),
        json={
            **submit_body,
            "client_mutation_id": step_body["client_mutation_id"],
        },
    )
    assert submit_cross_operation.status_code == 409

    with service.database.connect() as connection:
        events = connection.execute(
            """
            SELECT event_type, actor_type, actor_member_id, payload_json
            FROM secretary_workspace_events
            WHERE aggregate_id = ?
              AND event_type IN (
                'task.execution_started',
                'task.execution_checkin_recorded',
                'task.execution_step_updated',
                'task.execution_submitted'
              )
            ORDER BY sequence
            """,
            (task["id"],),
        ).fetchall()
    assert [row["event_type"] for row in events] == [
        "task.execution_started",
        "task.execution_step_updated",
        "task.execution_submitted",
    ]
    assert all(row["actor_type"] == "system" for row in events)
    assert all(row["actor_member_id"] is None for row in events)
    task_event_mutations = {
        row["event_type"]: json.loads(row["payload_json"])["client_mutation_id"]
        for row in events
    }
    assert task_event_mutations == {
        "task.execution_started": start_body["client_mutation_id"],
        "task.execution_step_updated": step_body["client_mutation_id"],
        "task.execution_submitted": submit_body["client_mutation_id"],
    }
    for row in events:
        event_payload = json.loads(row["payload_json"])
        assert event_payload["actor_subject_type"] == "task_execution_capability"
        assert event_payload["actor_subject_id"].startswith("execution_session_")
        assert event_payload["on_behalf_of_member_id"] == _assignee["id"]
        assert event_payload["assurance_method"] == "dual_channel_task_execution"
    with service.database.connect() as connection:
        checkin_event = connection.execute(
            """
            SELECT actor_type, actor_member_id, payload_json
            FROM secretary_workspace_events
            WHERE aggregate_id = ?
              AND event_type = 'task.execution_checkin_recorded'
            """,
            (checkin.json()["check_in"]["id"],),
        ).fetchone()
        family_scoped_rows = connection.execute(
            """
            SELECT actor_id FROM secretary_workspace_idempotency
            WHERE operation = ? AND idempotency_key = ?
            ORDER BY actor_id
            """,
            (
                f"task_execution.start:{task['id']}",
                "execution-family-start-first",
            ),
        ).fetchall()
    assert checkin_event is not None
    assert checkin_event["actor_type"] == "system"
    assert checkin_event["actor_member_id"] is None
    assert (
        json.loads(checkin_event["payload_json"])["client_mutation_id"]
        == checkin_body["client_mutation_id"]
    )
    assert len(family_scoped_rows) == 2
    assert len({row["actor_id"] for row in family_scoped_rows}) == 2
    assert all(
        row["actor_id"].startswith("task-execution-family:")
        for row in family_scoped_rows
    )
