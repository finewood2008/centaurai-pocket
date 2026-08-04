from __future__ import annotations

import html
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import centaur_pocket.workspace.service as workspace_service_module
import centaur_pocket.workspace.task_execution_browser as browser_module
from centaur_pocket.config import Settings
from centaur_pocket.main import create_app
from centaur_pocket.workspace.task_execution_browser import (
    ACCESS_COOKIE,
    BOOT_COOKIE,
    FORM_CONTENT_TYPE,
    REFRESH_COOKIE,
)

ORIGIN = "https://pocket.example"
WORKSPACE_ID = "ws_default"
WORKSPACE_PATH = f"/api/v1/workspaces/{WORKSPACE_ID}"
OWNER_ID = "member_owner"
OWNER_TOKEN = "cp_owner_browser-test-token"
AGENT_TOKEN = "cp_live_browser-test-token"


@pytest.fixture
def browser_client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        data_root=tmp_path / "runtime",
        owner_token=OWNER_TOKEN,
        agent_token=AGENT_TOKEN,
        scheduler_poll_seconds=0,
        task_execution_public_origin=ORIGIN,
    )
    application = create_app(settings)
    with TestClient(application, base_url=ORIGIN) as client:
        yield client


@pytest.fixture
def owner_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {OWNER_TOKEN}"}


def _owner_write_headers(
    owner_headers: dict[str, str],
    key: str,
    *,
    version: int | None = None,
) -> dict[str, str]:
    headers = {
        **owner_headers,
        "Idempotency-Key": key,
        "X-Device-ID": "browser-owner-device",
    }
    if version is not None:
        headers["If-Match"] = f'"{version}"'
    return headers


def _create_aligned_task(
    client: TestClient,
    owner_headers: dict[str, str],
    suffix: str,
    *,
    hostile_title: bool = False,
    with_step: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    member_response = client.post(
        f"{WORKSPACE_PATH}/members",
        headers=_owner_write_headers(owner_headers, f"browser-member-{suffix}"),
        json={
            "kind": "external",
            "role": "member",
            "display_name": f"浏览器承办人-{suffix}",
            "contact_ref": f"wecom://browser/{suffix}",
            "client_mutation_id": f"browser-member-local-{suffix}",
        },
    )
    assert member_response.status_code == 201, member_response.text
    assignee = member_response.json()
    steps = (
        [
            {
                "step_type": "action",
                "title": "承办人叶子步骤",
                "assignee_member_id": assignee["id"],
                "position": 0,
            }
        ]
        if with_step
        else []
    )
    title = "<script>alert('xss')</script>" if hostile_title else f"浏览器任务-{suffix}"
    created = client.post(
        f"{WORKSPACE_PATH}/tasks",
        headers=_owner_write_headers(owner_headers, f"browser-task-{suffix}"),
        json={
            "domain": "work",
            "title": title,
            "purpose": "形成可验证价值 <img src=x onerror=alert(1)>",
            "objective": "按期完成并提交验收",
            "strategy": "逐步执行、每日回报。",
            "key_points": ["资源", "风险"],
            "acceptance_criteria": ["完成交付", "验收通过"],
            "issuer_member_id": OWNER_ID,
            "assignee_member_id": assignee["id"],
            "acceptance_owner_id": OWNER_ID,
            "priority": "high",
            "tier": "strategic",
            "health": "on_track",
            "due_at": "2035-08-20T18:00:00+08:00",
            "steps": steps,
            "client_mutation_id": f"browser-task-local-{suffix}",
        },
    )
    assert created.status_code == 201, created.text
    task = created.json()
    issued = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/transitions",
        headers=_owner_write_headers(owner_headers, f"browser-issue-{suffix}"),
        json={"target_stage": "issued", "expected_version": task["version"]},
    )
    assert issued.status_code == 200, issued.text
    task = issued.json()
    invitation = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/alignment-invitations",
        headers=_owner_write_headers(owner_headers, f"browser-align-{suffix}"),
        json={"expected_version": task["version"]},
    )
    assert invitation.status_code == 201, invitation.text
    alignment_invitation = invitation.json()
    exchanged = client.post(
        "/api/v1/task-alignments/exchange",
        headers={"Idempotency-Key": f"browser-align-exchange-{suffix}"},
        json={
            "invitation_id": alignment_invitation["invitation_id"],
            "code": alignment_invitation["code"],
            "client_device_id": f"browser-align-device-{suffix}",
        },
    )
    assert exchanged.status_code == 200, exchanged.text
    alignment = exchanged.json()
    agreement = alignment["agreement"]
    revision = agreement["current_revision"]
    accepted = client.post(
        f"/api/v1/task-agreements/{agreement['id']}/responses",
        headers={
            "Authorization": f"Bearer {alignment['access_token']}",
            "X-Device-ID": alignment["session"]["client_device_id"],
            "Idempotency-Key": f"browser-align-accept-{suffix}",
            "If-Match": f'"{agreement["version"]}"',
        },
        json={
            "expected_agreement_version": agreement["version"],
            "revision_id": revision["id"],
            "expected_digest": revision["digest"],
            "action": "accept",
            "reason": None,
            "counter_document": None,
            "client_mutation_id": f"browser-align-accept-local-{suffix}",
        },
    )
    assert accepted.status_code == 200, accepted.text
    listed = client.get(f"{WORKSPACE_PATH}/tasks", headers=owner_headers)
    assert listed.status_code == 200, listed.text
    task = next(item for item in listed.json()["items"] if item["id"] == task["id"])
    return task, assignee


def _issue_invitation(
    client: TestClient,
    owner_headers: dict[str, str],
    task: dict[str, Any],
    suffix: str,
) -> dict[str, Any]:
    response = client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/execution-invitations",
        headers=_owner_write_headers(
            owner_headers,
            f"browser-execution-invite-{suffix}",
            version=task["version"],
        ),
        json={"expected_task_version": task["version"]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _browser_path(invitation: dict[str, Any], suffix: str = "") -> str:
    return invitation["confirmation_path"] + suffix


def _post_headers(*, origin: str = ORIGIN) -> dict[str, str]:
    return {
        "Content-Type": FORM_CONTENT_TYPE,
        "Origin": origin,
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
    }


def _csrf_for(document: str, action: str) -> str:
    pattern = re.compile(
        r'<form method="post" action="' + re.escape(action) + r'">(?:(?!</form>).)*?'
        r'<input type="hidden" name="csrf" value="([^"]+)">',
        re.DOTALL,
    )
    match = pattern.search(document)
    assert match is not None, (action, document)
    return html.unescape(match.group(1))


def _cookie_from_response(response: Any, name: str) -> tuple[str, Any]:
    for header in response.headers.get_list("set-cookie"):
        parsed = SimpleCookie()
        parsed.load(header)
        if name in parsed and parsed[name].value:
            return parsed[name].value, parsed[name]
    raise AssertionError(f"missing cookie {name}: {response.headers}")


def _delete_client_cookie(client: TestClient, name: str, path: str) -> None:
    matches = [
        (cookie.domain, cookie.path, cookie.name)
        for cookie in client.cookies.jar
        if cookie.name == name and cookie.path == path
    ]
    assert matches, (name, path, list(client.cookies.jar))
    for domain, cookie_path, cookie_name in matches:
        client.cookies.jar.clear(domain, cookie_path, cookie_name)


def _assert_secret_cookie(morsel: Any, path: str, *, max_age: int) -> None:
    assert morsel["path"] == path
    assert morsel["secure"] is True
    assert morsel["httponly"] is True
    assert morsel["samesite"].casefold() == "strict"
    assert morsel["domain"] == ""
    assert int(morsel["max-age"]) == max_age


def _exchange_browser(
    client: TestClient,
    invitation: dict[str, Any],
    *,
    extra_headers: dict[str, str] | None = None,
) -> tuple[str, str, Any]:
    entry = client.get(_browser_path(invitation))
    assert entry.status_code == 200, entry.text
    csrf = _csrf_for(entry.text, _browser_path(invitation, "/exchange"))
    response = client.post(
        _browser_path(invitation, "/exchange"),
        headers={**_post_headers(), **(extra_headers or {})},
        data={"csrf": csrf, "code": invitation["code"]},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    access, access_cookie = _cookie_from_response(response, ACCESS_COOKIE)
    refresh, refresh_cookie = _cookie_from_response(response, REFRESH_COOKIE)
    _assert_secret_cookie(
        access_cookie,
        _browser_path(invitation, "/workbench"),
        max_age=600,
    )
    _assert_secret_cookie(
        refresh_cookie,
        _browser_path(invitation, "/session"),
        max_age=86400,
    )
    return access, refresh, response


def _assert_security_headers(response: Any) -> None:
    assert response.headers["cache-control"].startswith("no-store")
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert response.headers["strict-transport-security"].startswith("max-age=")
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert not any(
        name.lower().startswith("access-control-") for name in response.headers
    )


def test_browser_exchange_uses_isolated_http_only_cookies_and_escaped_projection(
    browser_client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    task, _assignee = _create_aligned_task(
        browser_client,
        owner_headers,
        "escaped",
        hostile_title=True,
        with_step=True,
    )
    invitation = _issue_invitation(browser_client, owner_headers, task, "escaped")
    entry = browser_client.get(_browser_path(invitation))
    assert entry.status_code == 200, entry.text
    _assert_security_headers(entry)
    assert invitation["code"] not in entry.text
    assert "cp_task_ex_" not in entry.text
    assert "cp_task_er_" not in entry.text
    _boot, boot_cookie = _cookie_from_response(entry, BOOT_COOKIE)
    _assert_secret_cookie(
        boot_cookie,
        _browser_path(invitation),
        max_age=600,
    )

    access, refresh, exchanged = _exchange_browser(
        browser_client,
        invitation,
        extra_headers={
            "Host": "attacker.invalid",
            "X-Forwarded-Host": "attacker.invalid",
            "X-Forwarded-Proto": "http",
        },
    )
    expected_location = _browser_path(invitation, "/workbench")
    assert exchanged.headers["location"] == expected_location
    assert access not in exchanged.text
    assert refresh not in exchanged.text
    assert access not in exchanged.headers["location"]
    assert refresh not in exchanged.headers["location"]
    assert "attacker.invalid" not in exchanged.headers["location"]
    _assert_security_headers(exchanged)

    workbench = browser_client.get(expected_location)
    assert workbench.status_code == 200, workbench.text
    _assert_security_headers(workbench)
    assert "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;" in workbench.text
    assert "<script>" not in workbench.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in workbench.text
    assert access not in workbench.text
    assert refresh not in workbench.text
    assert ACCESS_COOKIE not in workbench.text
    assert REFRESH_COOKIE not in workbench.text
    assert task["assignee_member_id"] not in workbench.text
    assert "wecom://browser/escaped" not in workbench.text


def test_browser_rejects_query_content_origin_csrf_duplicate_cookie_and_cors(
    browser_client: TestClient,
    owner_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    task, _assignee = _create_aligned_task(
        browser_client, owner_headers, "threat-controls"
    )
    invitation = _issue_invitation(
        browser_client, owner_headers, task, "threat-controls"
    )
    entry = browser_client.get(_browser_path(invitation))
    csrf = _csrf_for(entry.text, _browser_path(invitation, "/exchange"))
    exchange_path = _browser_path(invitation, "/exchange")

    query = browser_client.post(
        exchange_path + "?code=leak",
        headers={"Content-Type": FORM_CONTENT_TYPE},
        data={"csrf": csrf, "code": invitation["code"]},
    )
    assert query.status_code == 400
    bad_type = browser_client.post(
        exchange_path,
        headers={"Content-Type": "application/json"},
        content=b"{}",
    )
    assert bad_type.status_code == 415
    extra_before_origin = browser_client.post(
        exchange_path,
        headers={"Content-Type": FORM_CONTENT_TYPE},
        data={"csrf": csrf, "code": invitation["code"], "extra": "x"},
    )
    assert extra_before_origin.status_code == 400
    wrong_origin = browser_client.post(
        exchange_path,
        headers=_post_headers(origin="https://attacker.invalid"),
        data={"csrf": csrf, "code": invitation["code"]},
    )
    assert wrong_origin.status_code == 403
    owner_context = browser_client.post(
        exchange_path,
        headers={**_post_headers(), **owner_headers},
        data={"csrf": csrf, "code": invitation["code"]},
    )
    assert owner_context.status_code == 403
    assert "Owner" in owner_context.text
    assert (
        browser_client.get(_browser_path(invitation), headers=owner_headers).status_code
        == 403
    )
    bad_csrf = browser_client.post(
        exchange_path,
        headers=_post_headers(),
        data={"csrf": "invalid.invalid", "code": invitation["code"]},
    )
    assert bad_csrf.status_code == 403
    boot, _boot_cookie = _cookie_from_response(entry, BOOT_COOKIE)
    duplicate = browser_client.post(
        exchange_path,
        headers={
            **_post_headers(),
            "Cookie": f"{BOOT_COOKIE}={boot}; {BOOT_COOKIE}={boot}",
        },
        data={"csrf": csrf, "code": invitation["code"]},
    )
    assert duplicate.status_code == 400
    assert "重复 Cookie" in duplicate.text
    duplicate_headers = browser_client.request(
        "POST",
        exchange_path,
        headers=[
            ("Content-Type", FORM_CONTENT_TYPE),
            ("Origin", ORIGIN),
            ("Sec-Fetch-Site", "same-origin"),
            ("Sec-Fetch-Mode", "navigate"),
            ("Sec-Fetch-Dest", "document"),
            ("Cookie", f"{BOOT_COOKIE}={boot}"),
            ("Cookie", f"{BOOT_COOKIE}={boot}"),
        ],
        data={"csrf": csrf, "code": invitation["code"]},
    )
    assert duplicate_headers.status_code == 400
    assert "重复 Cookie" in duplicate_headers.text

    options = browser_client.options(
        exchange_path,
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
        },
    )
    assert options.status_code == 405
    _assert_security_headers(options)
    exact_options = browser_client.options(
        "/api/v1/task-execution-invitations",
        headers={
            "Origin": "http://localhost:8081",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert exact_options.status_code == 405
    _assert_security_headers(exact_options)
    exact_get = browser_client.get(
        "/api/v1/task-execution-invitations",
        headers={"Origin": "http://localhost:8081"},
    )
    assert exact_get.status_code == 404
    _assert_security_headers(exact_get)
    insecure = browser_client.get(f"http://pocket.example{_browser_path(invitation)}")
    assert insecure.status_code == 404
    _assert_security_headers(insecure)

    service = browser_client.app.state.workspace_service
    with monkeypatch.context() as scoped:

        def fail_unexpectedly(_invitation_id: str) -> dict[str, Any]:
            raise RuntimeError("forced browser boundary failure")

        scoped.setattr(service, "task_execution_invitation_shell", fail_unexpectedly)
        unexpected = browser_client.get(_browser_path(invitation))
    assert unexpected.status_code == 500
    assert "forced browser boundary failure" not in unexpected.text
    _assert_security_headers(unexpected)
    assert "Unhandled task execution browser failure" in caplog.text

    access, refresh, response = _exchange_browser(browser_client, invitation)
    assert response.headers["location"] == _browser_path(invitation, "/workbench")
    assert access not in response.headers["location"]
    assert refresh not in response.headers["location"]


def test_browser_refresh_rotates_after_access_expiry_and_rejects_cross_task(
    browser_client: TestClient,
    owner_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task, _assignee = _create_aligned_task(browser_client, owner_headers, "refresh-a")
    invitation = _issue_invitation(browser_client, owner_headers, task, "refresh-a")
    other_task, _other_assignee = _create_aligned_task(
        browser_client, owner_headers, "refresh-b"
    )
    other_invitation = _issue_invitation(
        browser_client, owner_headers, other_task, "refresh-b"
    )
    access, refresh, _response = _exchange_browser(browser_client, invitation)

    cross_task = browser_client.get(
        _browser_path(other_invitation, "/workbench"),
        headers={"Cookie": f"{ACCESS_COOKIE}={access}"},
    )
    assert cross_task.status_code == 401
    assert access not in cross_task.text
    assert refresh not in cross_task.text
    _other_access, _other_refresh, _other_response = _exchange_browser(
        browser_client, other_invitation
    )

    before = browser_client.get(_browser_path(invitation, "/workbench"))
    assert before.status_code == 200, before.text
    real_datetime = datetime
    frozen_now = real_datetime.now(UTC) + timedelta(minutes=10, seconds=1)

    class FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return frozen_now if tz is None else frozen_now.astimezone(tz)

    monkeypatch.setattr(browser_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(workspace_service_module, "datetime", FrozenDateTime)
    service = browser_client.app.state.workspace_service
    with service.database.transaction() as connection:
        connection.execute(
            "UPDATE secretary_business_tasks SET due_at = ?, updated_at = ? "
            "WHERE id = ?",
            (
                "2000-01-01T00:00:00Z",
                frozen_now.isoformat(),
                other_task["id"],
            ),
        )
    due_closed = browser_client.get(_browser_path(other_invitation, "/workbench"))
    assert due_closed.status_code == 401
    assert other_task["title"] not in due_closed.text
    due_continue_path = _browser_path(other_invitation, "/session/continue")
    assert due_continue_path in due_closed.text
    due_continue = browser_client.get(due_continue_path)
    assert due_continue.status_code == 401
    assert other_task["title"] not in due_continue.text
    assert "刷新安全会话" not in due_continue.text

    _delete_client_cookie(
        browser_client,
        ACCESS_COOKIE,
        _browser_path(invitation, "/workbench"),
    )
    expired = browser_client.get(_browser_path(invitation, "/workbench"))
    assert expired.status_code == 401, expired.text
    assert task["title"] not in expired.text
    continue_path = _browser_path(invitation, "/session/continue")
    assert continue_path in expired.text
    continued = browser_client.get(continue_path)
    assert continued.status_code == 200, continued.text
    assert task["title"] not in continued.text
    assert access not in continued.text
    assert refresh not in continued.text
    refresh_path = _browser_path(invitation, "/session/refresh")
    refresh_csrf = _csrf_for(continued.text, refresh_path)
    rotated = browser_client.post(
        refresh_path,
        headers=_post_headers(),
        data={"csrf": refresh_csrf},
        follow_redirects=False,
    )
    assert rotated.status_code == 303, rotated.text
    assert rotated.headers["location"] == _browser_path(invitation, "/workbench")
    new_access, access_cookie = _cookie_from_response(rotated, ACCESS_COOKIE)
    new_refresh, refresh_cookie = _cookie_from_response(rotated, REFRESH_COOKIE)
    assert new_access != access
    assert new_refresh != refresh
    _assert_secret_cookie(
        access_cookie,
        _browser_path(invitation, "/workbench"),
        max_age=600,
    )
    _assert_secret_cookie(
        refresh_cookie,
        _browser_path(invitation, "/session"),
        max_age=86400,
    )
    assert access not in rotated.text
    assert refresh not in rotated.text
    assert new_access not in rotated.text
    assert new_refresh not in rotated.text
    resumed = browser_client.get(_browser_path(invitation, "/workbench"))
    assert resumed.status_code == 200, resumed.text
    with service.database.connect() as connection:
        generations = connection.execute(
            """
            SELECT generation, used_at FROM secretary_task_execution_refresh_tokens
            WHERE family_id = (
                SELECT id FROM secretary_task_execution_refresh_families
                WHERE invitation_id = ?
            ) ORDER BY generation
            """,
            (invitation["invitation_id"],),
        ).fetchall()
    assert [(row["generation"], row["used_at"] is not None) for row in generations] == [
        (1, True),
        (2, False),
    ]


def test_browser_used_refresh_cannot_mint_new_form_but_exact_replay_recovers(
    browser_client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    task, _assignee = _create_aligned_task(
        browser_client,
        owner_headers,
        "refresh-exact-replay",
    )
    invitation = _issue_invitation(
        browser_client,
        owner_headers,
        task,
        "refresh-exact-replay",
    )
    _access, old_refresh, _response = _exchange_browser(
        browser_client,
        invitation,
    )
    _delete_client_cookie(
        browser_client,
        ACCESS_COOKIE,
        _browser_path(invitation, "/workbench"),
    )
    continue_path = _browser_path(invitation, "/session/continue")
    refresh_path = _browser_path(invitation, "/session/refresh")
    original_continue = browser_client.get(continue_path)
    assert original_continue.status_code == 200, original_continue.text
    original_csrf = _csrf_for(original_continue.text, refresh_path)

    rotated = browser_client.post(
        refresh_path,
        headers=_post_headers(),
        data={"csrf": original_csrf},
        follow_redirects=False,
    )
    assert rotated.status_code == 303, rotated.text
    new_access, _access_cookie = _cookie_from_response(rotated, ACCESS_COOKIE)
    new_refresh, _refresh_cookie = _cookie_from_response(rotated, REFRESH_COOKIE)
    assert new_refresh != old_refresh

    _delete_client_cookie(
        browser_client,
        REFRESH_COOKIE,
        _browser_path(invitation, "/session"),
    )
    stale_cookie = {"Cookie": f"{REFRESH_COOKIE}={old_refresh}"}
    stale_continue = browser_client.get(continue_path, headers=stale_cookie)
    assert stale_continue.status_code == 401
    assert "刷新安全会话" not in stale_continue.text
    assert refresh_path not in stale_continue.text
    assert task["title"] not in stale_continue.text

    # The original signed form retains its original idempotency key, so an
    # immediate retry still takes the service's 30-second exact-replay path and
    # reconstructs the already-issued replacement credentials.
    exact_replay = browser_client.post(
        refresh_path,
        headers={**_post_headers(), **stale_cookie},
        data={"csrf": original_csrf},
        follow_redirects=False,
    )
    assert exact_replay.status_code == 303, exact_replay.text
    replay_access, _replay_access_cookie = _cookie_from_response(
        exact_replay,
        ACCESS_COOKIE,
    )
    replay_refresh, _replay_refresh_cookie = _cookie_from_response(
        exact_replay,
        REFRESH_COOKIE,
    )
    assert replay_access == new_access
    assert replay_refresh == new_refresh
    resumed = browser_client.get(_browser_path(invitation, "/workbench"))
    assert resumed.status_code == 200, resumed.text


def test_browser_pending_change_keeps_checkin_but_hides_step_and_submit(
    browser_client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    task, _assignee = _create_aligned_task(
        browser_client,
        owner_headers,
        "pending-change-ui",
        with_step=True,
    )
    invitation = _issue_invitation(
        browser_client,
        owner_headers,
        task,
        "pending-change-ui",
    )
    _exchange_browser(browser_client, invitation)
    workbench_path = _browser_path(invitation, "/workbench")
    aligned = browser_client.get(workbench_path)
    start_path = workbench_path + "/start"
    start_csrf = _csrf_for(aligned.text, start_path)
    started = browser_client.post(
        start_path,
        headers=_post_headers(),
        data={"csrf": start_csrf, "note": ""},
        follow_redirects=False,
    )
    assert started.status_code == 303, started.text
    listed = browser_client.get(f"{WORKSPACE_PATH}/tasks", headers=owner_headers)
    running_task = next(
        item for item in listed.json()["items"] if item["id"] == task["id"]
    )
    proposed = browser_client.post(
        f"{WORKSPACE_PATH}/tasks/{task['id']}/changes",
        headers=_owner_write_headers(
            owner_headers,
            "browser-pending-change-ui",
        ),
        json={
            "change_type": "due_at",
            "base_version": running_task["version"],
            "reason": "等待承办人确认延期",
            "patch": {"due_at": "2035-08-30T18:00:00+08:00"},
            "client_mutation_id": "browser-pending-change-ui-local",
        },
    )
    assert proposed.status_code == 201, proposed.text
    pending = browser_client.get(workbench_path)
    assert pending.status_code == 200, pending.text
    assert "任务存在待确认变更" in pending.text
    assert workbench_path + "/check-ins" in pending.text
    assert workbench_path + "/submit" not in pending.text
    assert f'action="{workbench_path}/steps/' not in pending.text


def test_browser_no_script_actions_prg_replay_stale_and_expired_csrf(
    browser_client: TestClient,
    owner_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task, _assignee = _create_aligned_task(
        browser_client, owner_headers, "actions", with_step=True
    )
    invitation = _issue_invitation(browser_client, owner_headers, task, "actions")
    access, refresh, _response = _exchange_browser(browser_client, invitation)
    workbench_path = _browser_path(invitation, "/workbench")
    initial = browser_client.get(workbench_path)
    assert initial.status_code == 200, initial.text
    assert "<script" not in initial.text.casefold()
    start_path = workbench_path + "/start"
    start_csrf = _csrf_for(initial.text, start_path)
    start_form = {"csrf": start_csrf, "note": "开始执行"}
    started = browser_client.post(
        start_path,
        headers=_post_headers(),
        data=start_form,
        follow_redirects=False,
    )
    assert started.status_code == 303, started.text
    assert started.headers["location"] == workbench_path
    replay = browser_client.post(
        start_path,
        headers=_post_headers(),
        data=start_form,
        follow_redirects=False,
    )
    assert replay.status_code == 303, replay.text
    assert replay.headers["location"] == workbench_path

    running = browser_client.get(workbench_path)
    checkin_path = workbench_path + "/check-ins"
    checkin_csrf = _csrf_for(running.text, checkin_path)
    checkin = browser_client.post(
        checkin_path,
        headers=_post_headers(),
        data={
            "csrf": checkin_csrf,
            "summary": "已完成第一轮",
            "reported_progress": "40",
            "risks": "风险一\n风险二",
            "blockers": "",
            "next_actions": "完成叶子步骤",
            "forecast_at": "2035-08-18T10:00:00+08:00",
        },
        follow_redirects=False,
    )
    assert checkin.status_code == 303, checkin.text
    after_checkin = browser_client.get(workbench_path)
    step_match = re.search(
        re.escape(workbench_path)
        + r'/steps/([^/]+)/status">(?:(?!</form>).)*?'
        + r'<input type="hidden" name="csrf" value="([^"]+)">'
        + r'<button type="submit">标记完成</button>',
        after_checkin.text,
        re.DOTALL,
    )
    assert step_match is not None, after_checkin.text
    step_id = html.unescape(step_match.group(1))
    step_csrf = html.unescape(step_match.group(2))
    step_path = f"{workbench_path}/steps/{step_id}/status"
    completed = browser_client.post(
        step_path,
        headers=_post_headers(),
        data={"csrf": step_csrf},
        follow_redirects=False,
    )
    assert completed.status_code == 303, completed.text
    ready = browser_client.get(workbench_path)
    submit_path = workbench_path + "/submit"
    submit_csrf = _csrf_for(ready.text, submit_path)
    submitted = browser_client.post(
        submit_path,
        headers=_post_headers(),
        data={"csrf": submit_csrf, "note": "请验收"},
        follow_redirects=False,
    )
    assert submitted.status_code == 303, submitted.text
    assert submitted.headers["location"] == workbench_path
    assert access not in submitted.text
    assert refresh not in submitted.text

    stale_task, _stale_assignee = _create_aligned_task(
        browser_client, owner_headers, "stale"
    )
    stale_invitation = _issue_invitation(
        browser_client, owner_headers, stale_task, "stale"
    )
    stale_access, _stale_refresh, _stale_response = _exchange_browser(
        browser_client, stale_invitation
    )
    stale_workbench = _browser_path(stale_invitation, "/workbench")
    stale_page = browser_client.get(stale_workbench)
    stale_start_path = stale_workbench + "/start"
    stale_csrf = _csrf_for(stale_page.text, stale_start_path)
    service = browser_client.app.state.workspace_service
    with service.database.connect() as connection:
        session = connection.execute(
            "SELECT * FROM secretary_task_execution_sessions WHERE token_hash = ?",
            (browser_module._secret_hash(stale_access),),
        ).fetchone()
    assert session is not None
    principal = service.authenticate_task_execution_session(
        stale_access,
        requested_device_id=session["client_device_id"],
    )
    projection, raw_etag = service.task_execution_view(stale_task["id"], principal)
    service.start_task_execution(
        stale_task["id"],
        {
            "expected_task_version": projection["version"],
            "client_mutation_id": "browser-stale-out-of-band",
            "note": None,
        },
        principal,
        idempotency_key="browser-stale-out-of-band",
        device_id=session["client_device_id"],
        if_match=f'"{raw_etag}"',
    )
    stale = browser_client.post(
        stale_start_path,
        headers=_post_headers(),
        data={"csrf": stale_csrf, "note": "过期页面"},
    )
    assert stale.status_code == 412

    expiring_task, _expiring_assignee = _create_aligned_task(
        browser_client, owner_headers, "expired-csrf"
    )
    expiring_invitation = _issue_invitation(
        browser_client, owner_headers, expiring_task, "expired-csrf"
    )
    _exchange_browser(browser_client, expiring_invitation)
    expiring_workbench = _browser_path(expiring_invitation, "/workbench")
    expiring_page = browser_client.get(expiring_workbench)
    expiring_start_path = expiring_workbench + "/start"
    expiring_csrf = _csrf_for(expiring_page.text, expiring_start_path)
    real_datetime = datetime
    expired_now = real_datetime.now(UTC) + timedelta(minutes=31)

    class ExpiredDateTime(real_datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return expired_now if tz is None else expired_now.astimezone(tz)

    monkeypatch.setattr(browser_module, "datetime", ExpiredDateTime)
    expired_csrf = browser_client.post(
        expiring_start_path,
        headers=_post_headers(),
        data={"csrf": expiring_csrf, "note": ""},
    )
    assert expired_csrf.status_code == 403
