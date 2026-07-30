from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from centaur_pocket.config import Settings
from centaur_pocket.main import create_app
from centaur_pocket.service import utc_now


def create_folder_source(
    client: TestClient,
    owner_headers: dict[str, str],
    path: Path,
    *,
    name: str = "个人文档",
    idempotency_key: str | None = None,
) -> dict:
    headers = dict(owner_headers)
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    response = client.post(
        "/api/v1/sources",
        headers=headers,
        json={
            "kind": "folder",
            "display_name": name,
            "config": {
                "path": str(path),
                "recursive": True,
            },
            "schedule": "manual",
            "enabled": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_health_and_owner_boundary(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "service": "centaurai-pocket",
        "version": "0.1.0",
    }

    assert client.get("/api/v1/dashboard").status_code == 401
    dashboard = client.get("/api/v1/dashboard", headers=owner_headers)
    assert dashboard.status_code == 200
    assert dashboard.json()["items"]["total"] == 0
    assert dashboard.json()["quality_score"] == 100
    x_header = client.get(
        "/api/v1/dashboard",
        headers={"X-Owner-Token": owner_headers["Authorization"].split(" ", 1)[1]},
    )
    assert x_header.status_code == 200


def test_source_crud_and_idempotency(
    client: TestClient,
    owner_headers: dict[str, str],
    watched_folder: Path,
) -> None:
    first = create_folder_source(
        client,
        owner_headers,
        watched_folder,
        idempotency_key="create-folder-1",
    )
    repeated = create_folder_source(
        client,
        owner_headers,
        watched_folder,
        idempotency_key="create-folder-1",
    )
    assert repeated["id"] == first["id"]
    assert first["config"]["path"] == str(watched_folder.resolve())
    assert first["status"] == "unknown"
    initial_dashboard = client.get(
        "/api/v1/dashboard",
        headers=owner_headers,
    ).json()
    assert initial_dashboard["sources"] == {
        "total": 1,
        "healthy": 0,
        "attention": 1,
    }

    duplicate = client.post(
        "/api/v1/sources",
        headers=owner_headers,
        json={
            "display_name": "重复目录",
            "config": {"path": str(watched_folder)},
        },
    )
    assert duplicate.status_code == 409

    updated = client.patch(
        f"/api/v1/sources/{first['id']}",
        headers=owner_headers,
        json={"display_name": "家庭资料", "schedule": "hourly"},
    )
    assert updated.status_code == 200
    assert updated.json()["display_name"] == "家庭资料"
    assert updated.json()["schedule"] == "hourly"
    service = client.app.state.service
    assert first["id"] in service.due_source_ids()
    synced = client.post(
        f"/api/v1/sources/{first['id']}/sync",
        headers=owner_headers,
    )
    assert synced.status_code == 200
    assert first["id"] not in service.due_source_ids()

    listing = client.get("/api/v1/sources", headers=owner_headers).json()
    assert listing["total"] == 1
    assert listing["items"][0]["type"] == "folder"

    deleted = client.delete(f"/api/v1/sources/{first['id']}", headers=owner_headers)
    assert deleted.status_code == 204
    assert client.get("/api/v1/sources", headers=owner_headers).json()["total"] == 0


def test_sync_governance_agent_visibility_and_undo(
    client: TestClient,
    owner_headers: dict[str, str],
    agent_headers: dict[str, str],
    watched_folder: Path,
) -> None:
    content = "Private insurance policy coverage limit is 5000 yuan."
    (watched_folder / "insurance-policy.md").write_text(content, encoding="utf-8")
    (watched_folder / "same-content.txt").write_text(content, encoding="utf-8")
    hidden = watched_folder / ".hidden.txt"
    hidden.write_text("must remain hidden", encoding="utf-8")

    source = create_folder_source(client, owner_headers, watched_folder)
    sync_headers = {**owner_headers, "Idempotency-Key": "sync-folder-1"}
    sync = client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=sync_headers,
    )
    assert sync.status_code == 200, sync.text
    run = sync.json()
    assert run["status"] == "completed"
    assert run["scanned_count"] == 2
    assert run["imported_count"] == 1
    assert run["duplicate_count"] == 1
    assert run["task_count"] == 1

    repeated = client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=sync_headers,
    )
    assert repeated.json()["id"] == run["id"]
    runs = client.get(
        f"/api/v1/sync-runs?source_id={source['id']}",
        headers=owner_headers,
    ).json()
    assert runs["total"] == 1

    items = client.get("/api/v1/items", headers=owner_headers).json()
    assert items["total"] == 1
    item_id = items["items"][0]["id"]
    assert items["items"][0]["state"] == "needs_review"

    assert (
        client.post(
            "/api/v1/agent/search",
            json={"query": "coverage"},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/agent/search",
            headers=owner_headers,
            json={"query": "coverage"},
        ).status_code
        == 401
    )
    before_ready = client.post(
        "/api/v1/agent/search",
        headers=agent_headers,
        json={"query": "coverage"},
    )
    assert before_ready.status_code == 200
    assert before_ready.json()["results"] == []

    task_list = client.get(
        "/api/v1/governance/tasks?status=pending",
        headers=owner_headers,
    ).json()
    assert task_list["total"] == 1
    task_id = task_list["items"][0]["id"]
    assert task_list["items"][0]["kind"] == "review"
    assert task_list["items"][0]["source_name"] == "个人文档"

    applied = client.post(
        f"/api/v1/governance/tasks/{task_id}/apply",
        headers={**owner_headers, "Idempotency-Key": "apply-task-1"},
        json={
            "patch": {
                "title": "家庭医疗保险",
                "tags": ["保险", "家庭"],
            }
        },
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["task"]["status"] == "applied"
    assert applied.json()["task"]["item"]["state"] == "ready"
    assert applied.json()["next_task"] is None

    search = client.post(
        "/api/v1/agent/search",
        headers=agent_headers,
        json={
            "query": "coverage",
            "filters": {"tags": ["保险"]},
            "limit": 8,
        },
    )
    assert search.status_code == 200, search.text
    assert search.json()["visibility"] == "ready_only"
    assert search.json()["results"][0]["item_id"] == item_id
    assert search.json()["results"][0]["title"] == "家庭医疗保险"

    undone = client.post(
        f"/api/v1/governance/tasks/{task_id}/undo",
        headers=owner_headers,
    )
    assert undone.status_code == 200, undone.text
    assert undone.json()["task"]["status"] == "pending"
    assert undone.json()["task"]["item"]["state"] == "needs_review"
    assert (
        client.post(
            "/api/v1/agent/search",
            headers=agent_headers,
            json={"query": "coverage"},
        ).json()["results"]
        == []
    )

    reapplied = client.post(
        f"/api/v1/governance/tasks/{task_id}/actions",
        headers=owner_headers,
        json={
            "action": "apply",
            "idempotency_key": "apply-task-2",
            "patch": {"title": "家庭医疗保险（已确认）"},
        },
    )
    assert reapplied.status_code == 200
    assert reapplied.json()["task"]["item"]["state"] == "ready"

    archived = client.patch(
        f"/api/v1/items/{item_id}",
        headers=owner_headers,
        json={"state": "archived"},
    )
    assert archived.status_code == 200
    assert archived.json()["state"] == "archived"
    assert (
        client.post(
            "/api/v1/agent/search",
            headers=agent_headers,
            json={"query": "coverage"},
        ).json()["count"]
        == 0
    )

    ready_again = client.patch(
        f"/api/v1/items/{item_id}",
        headers=owner_headers,
        json={"state": "ready"},
    )
    assert ready_again.status_code == 200
    assert (
        client.post(
            "/api/v1/agent/search",
            headers=agent_headers,
            json={"query": "coverage"},
        ).json()["count"]
        == 1
    )


def test_skip_and_undo_task(
    client: TestClient,
    owner_headers: dict[str, str],
    watched_folder: Path,
) -> None:
    (watched_folder / "note.txt").write_text("a note to review", encoding="utf-8")
    source = create_folder_source(client, owner_headers, watched_folder)
    client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )
    task = client.get("/api/v1/governance/tasks", headers=owner_headers).json()[
        "items"
    ][0]

    skipped = client.post(
        f"/api/v1/governance/tasks/{task['id']}/skip",
        headers=owner_headers,
        json={"action": "skip", "idempotency_key": "skip-task-1"},
    )
    assert skipped.status_code == 200
    assert skipped.json()["task"]["status"] == "skipped"

    undone = client.post(
        f"/api/v1/governance/tasks/{task['id']}/undo",
        headers=owner_headers,
        json={"action": "undo", "idempotency_key": "undo-task-1"},
    )
    assert undone.status_code == 200
    assert undone.json()["task"]["status"] == "pending"


def test_resharing_a_skipped_capture_reopens_governance(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    payload = {"title": "待确认", "text": "reopen this private note"}
    first = client.post(
        "/api/v1/captures",
        headers=owner_headers,
        json=payload,
    ).json()
    skipped = client.post(
        f"/api/v1/governance/tasks/{first['task_id']}/skip",
        headers=owner_headers,
    )
    assert skipped.status_code == 200

    repeated = client.post(
        "/api/v1/captures",
        headers=owner_headers,
        json=payload,
    )
    assert repeated.status_code == 201
    assert repeated.json()["deduplicated"] is True
    assert repeated.json()["status"] == "needs_review"
    assert repeated.json()["task_id"] not in {None, first["task_id"]}
    pending = client.get(
        "/api/v1/governance/tasks?status=pending",
        headers=owner_headers,
    ).json()
    assert pending["total"] == 1


def test_resyncing_a_skipped_file_reopens_governance(
    client: TestClient,
    owner_headers: dict[str, str],
    watched_folder: Path,
) -> None:
    (watched_folder / "reappear.txt").write_text(
        "this unchanged file should reappear for review",
        encoding="utf-8",
    )
    source = create_folder_source(client, owner_headers, watched_folder)
    client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )
    first_task = client.get(
        "/api/v1/governance/tasks?status=pending",
        headers=owner_headers,
    ).json()["items"][0]
    client.post(
        f"/api/v1/governance/tasks/{first_task['id']}/skip",
        headers=owner_headers,
    )

    repeated = client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )
    assert repeated.status_code == 200
    assert repeated.json()["status"] == "completed"
    assert repeated.json()["unchanged_count"] == 1
    assert repeated.json()["task_count"] == 1
    pending = client.get(
        "/api/v1/governance/tasks?status=pending",
        headers=owner_headers,
    ).json()
    assert pending["total"] == 1
    assert pending["items"][0]["id"] != first_task["id"]


def test_apply_task_rejects_a_non_ready_final_state(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    captured = client.post(
        "/api/v1/captures",
        headers=owner_headers,
        json={"title": "状态检查", "text": "must become ready"},
    ).json()

    response = client.post(
        f"/api/v1/governance/tasks/{captured['task_id']}/apply",
        headers=owner_headers,
        json={"patch": {"state": "archived"}},
    )
    assert response.status_code == 422
    task = client.get(
        f"/api/v1/governance/tasks/{captured['task_id']}",
        headers=owner_headers,
    ).json()
    assert task["status"] == "pending"


def test_failed_sync_and_paused_source(
    client: TestClient,
    owner_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    missing = tmp_path / "does-not-exist"
    source = create_folder_source(client, owner_headers, missing)
    failed = client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )
    assert failed.status_code == 502
    assert failed.json()["sync_run"]["status"] == "failed"
    assert "不存在" in failed.json()["detail"]
    failed_source = client.get(
        f"/api/v1/sources/{source['id']}",
        headers=owner_headers,
    ).json()
    assert failed_source["last_sync_at"] is None

    paused = client.patch(
        f"/api/v1/sources/{source['id']}",
        headers=owner_headers,
        json={"enabled": False},
    )
    assert paused.json()["status"] == "paused"
    blocked = client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )
    assert blocked.status_code == 409

    dashboard = client.get("/api/v1/dashboard", headers=owner_headers).json()
    assert dashboard["sources"]["attention"] == 1
    assert dashboard["recent_activity"]


def test_failed_sync_idempotency_key_can_retry_after_source_recovers(
    client: TestClient,
    owner_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    folder = tmp_path / "appears-later"
    source = create_folder_source(client, owner_headers, folder)
    headers = {**owner_headers, "Idempotency-Key": "recoverable-sync"}

    failed = client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=headers,
    )
    assert failed.status_code == 502

    folder.mkdir()
    (folder / "recovered.txt").write_text("source recovered", encoding="utf-8")
    recovered = client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=headers,
    )
    assert recovered.status_code == 200
    assert recovered.json()["status"] == "completed"
    assert recovered.json()["imported_count"] == 1


def test_recursive_walk_error_fails_without_pruning_unseen_links(
    client: TestClient,
    owner_headers: dict[str, str],
    watched_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (watched_folder / "visible.txt").write_text("visible", encoding="utf-8")
    restricted = watched_folder / "restricted"
    restricted.mkdir()
    (restricted / "private.txt").write_text("private", encoding="utf-8")
    source = create_folder_source(client, owner_headers, watched_folder)
    first = client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )
    assert first.json()["imported_count"] == 2

    def failing_walk(root: Path, *, followlinks: bool, onerror: object):
        assert followlinks is False
        yield str(root), ["restricted"], ["visible.txt"]
        assert callable(onerror)
        onerror(PermissionError("restricted subtree"))

    monkeypatch.setattr("centaur_pocket.service.os.walk", failing_walk)
    failed = client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )

    assert failed.status_code == 502
    assert "restricted subtree" in failed.json()["detail"]
    after = client.get(
        f"/api/v1/sources/{source['id']}",
        headers=owner_headers,
    ).json()
    assert after["item_count"] == 2
    assert {
        task["kind"]
        for task in client.get(
            "/api/v1/governance/tasks",
            headers=owner_headers,
        ).json()["items"]
    } == {"review"}


def test_unexpected_sync_error_is_finalized(
    client: TestClient,
    owner_headers: dict[str, str],
    watched_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (watched_folder / "failure.txt").write_text("trigger parser", encoding="utf-8")
    source = create_folder_source(client, owner_headers, watched_folder)
    service = client.app.state.service

    def fail_ingest(**_kwargs: object) -> str:
        raise RuntimeError

    monkeypatch.setattr(service, "_ingest_file", fail_ingest)
    response = client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )

    assert response.status_code == 502
    run = response.json()["sync_run"]
    assert run["status"] == "failed"
    assert run["finished_at"] is not None
    assert run["error"] == "RuntimeError"
    retry = client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )
    assert retry.status_code == 502
    assert "正在运行" not in retry.text


def test_recent_failure_backs_off_even_after_an_earlier_success(
    client: TestClient,
    owner_headers: dict[str, str],
    watched_folder: Path,
) -> None:
    source = create_folder_source(client, owner_headers, watched_folder)
    source_id = source["id"]
    service = client.app.state.service
    old_success = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    recent_failure = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()

    with service.database.transaction() as connection:
        connection.execute(
            "UPDATE sources SET schedule = 'hourly', last_sync_at = ? WHERE id = ?",
            (old_success, source_id),
        )
        connection.execute(
            """
            INSERT INTO sync_runs(
                id, source_id, status, started_at, finished_at, error
            )
            VALUES ('run_recent_failure', ?, 'failed', ?, ?, 'offline')
            """,
            (source_id, recent_failure, recent_failure),
        )

    assert source_id not in service.due_source_ids()

    with service.database.transaction() as connection:
        connection.execute(
            "UPDATE sync_runs SET started_at = ? WHERE id = 'run_recent_failure'",
            ((datetime.now(UTC) - timedelta(minutes=6)).isoformat(),),
        )
    assert source_id in service.due_source_ids()


def test_source_rejects_a_concurrent_sync(
    client: TestClient,
    owner_headers: dict[str, str],
    watched_folder: Path,
) -> None:
    source = create_folder_source(client, owner_headers, watched_folder)
    service = client.app.state.service
    with service.database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO sync_runs(id, source_id, status, started_at)
            VALUES ('run_in_progress', ?, 'running', ?)
            """,
            (source["id"], utc_now()),
        )

    response = client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )
    assert response.status_code == 409
    assert "正在运行" in response.json()["detail"]


def test_mobile_capture_is_idempotent_and_requires_governance(
    client: TestClient,
    owner_headers: dict[str, str],
    agent_headers: dict[str, str],
) -> None:
    payload = {
        "title": "需要稍后整理的网页",
        "text": "Pocket capture contains a private research insight.",
        "url": "https://example.test/research",
        "mimeType": "text/plain",
        "origin": "share-sheet",
        "idempotency_key": "mobile-capture-1",
    }
    created = client.post(
        "/api/v1/captures",
        headers=owner_headers,
        json=payload,
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "needs_review"
    assert created.json()["deduplicated"] is False

    repeated = client.post(
        "/api/v1/captures",
        headers=owner_headers,
        json=payload,
    )
    assert repeated.status_code == 201
    assert repeated.json() == created.json()

    hidden = client.post(
        "/api/v1/agent/search",
        headers=agent_headers,
        json={"query": "research insight"},
    )
    assert hidden.json()["results"] == []

    applied = client.post(
        f"/api/v1/governance/tasks/{created.json()['task_id']}/apply",
        headers=owner_headers,
    )
    assert applied.status_code == 200
    visible = client.post(
        "/api/v1/agent/search",
        headers=agent_headers,
        json={"query": "research insight"},
    )
    assert visible.json()["results"][0]["item_id"] == created.json()["item_id"]


def test_mobile_capture_keeps_distinct_url_provenance(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    first = client.post(
        "/api/v1/captures",
        headers=owner_headers,
        json={
            "title": "网页一",
            "text": "稍后阅读",
            "url": "https://first.example/article",
        },
    ).json()
    second = client.post(
        "/api/v1/captures",
        headers=owner_headers,
        json={
            "title": "网页二",
            "text": "稍后阅读",
            "url": "https://second.example/article",
        },
    ).json()

    assert first["item_id"] != second["item_id"]
    assert second["deduplicated"] is False


def test_agent_token_rotation_revokes_the_old_token_immediately(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_root=tmp_path / "rotation-runtime",
        owner_token="cp_owner_rotation-test",
        scheduler_poll_seconds=0,
    )
    owner = {"Authorization": "Bearer cp_owner_rotation-test"}
    with TestClient(create_app(settings)) as rotation_client:
        old_token = settings.agent_token_path.read_text(encoding="utf-8").strip()
        old_headers = {"Authorization": f"Bearer {old_token}"}
        assert (
            rotation_client.post(
                "/api/v1/agent/search",
                headers=old_headers,
                json={"query": "anything"},
            ).status_code
            == 200
        )

        rotated = rotation_client.post(
            "/api/v1/agent/token/rotate",
            headers=owner,
        )
        assert rotated.status_code == 200
        new_token = rotated.json()["token"]
        assert new_token != old_token
        assert settings.agent_token_path.read_text(encoding="utf-8").strip() == new_token

        assert (
            rotation_client.post(
                "/api/v1/agent/search",
                headers=old_headers,
                json={"query": "anything"},
            ).status_code
            == 401
        )
        assert (
            rotation_client.post(
                "/api/v1/agent/search",
                headers={"Authorization": f"Bearer {new_token}"},
                json={"query": "anything"},
            ).status_code
            == 200
        )


def test_changed_file_keeps_last_ready_until_new_generation_is_confirmed(
    client: TestClient,
    owner_headers: dict[str, str],
    agent_headers: dict[str, str],
    watched_folder: Path,
) -> None:
    document = watched_folder / "policy.txt"
    document.write_text(
        "The legacy reimbursement ceiling is 5000 credits.",
        encoding="utf-8",
    )
    source = create_folder_source(client, owner_headers, watched_folder)
    first_run = client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )
    assert first_run.json()["imported_count"] == 1
    first_task = client.get(
        "/api/v1/governance/tasks",
        headers=owner_headers,
    ).json()["items"][0]
    client.post(
        f"/api/v1/governance/tasks/{first_task['id']}/apply",
        headers=owner_headers,
    )
    old_item_id = first_task["item_id"]

    document.write_text(
        "The current reimbursement ceiling is 9000 credits.",
        encoding="utf-8",
    )
    second_run = client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )
    assert second_run.json()["imported_count"] == 1
    second_task = client.get(
        "/api/v1/governance/tasks",
        headers=owner_headers,
    ).json()["items"][0]
    new_item_id = second_task["item_id"]
    assert new_item_id != old_item_id
    assert second_task["proposal"]["supersedes_item_id"] == old_item_id

    bypass = client.patch(
        f"/api/v1/items/{new_item_id}",
        headers=owner_headers,
        json={"state": "ready"},
    )
    assert bypass.status_code == 409
    assert (
        client.get(
            f"/api/v1/governance/tasks/{second_task['id']}",
            headers=owner_headers,
        ).json()["status"]
        == "pending"
    )

    old_before_confirmation = client.post(
        "/api/v1/agent/search",
        headers=agent_headers,
        json={"query": "5000"},
    ).json()
    new_before_confirmation = client.post(
        "/api/v1/agent/search",
        headers=agent_headers,
        json={"query": "9000"},
    ).json()
    assert old_before_confirmation["results"][0]["item_id"] == old_item_id
    assert new_before_confirmation["results"] == []

    applied = client.post(
        f"/api/v1/governance/tasks/{second_task['id']}/apply",
        headers=owner_headers,
    )
    assert applied.status_code == 200
    assert (
        client.get(
            f"/api/v1/items/{old_item_id}",
            headers=owner_headers,
        ).json()["state"]
        == "archived"
    )
    assert (
        client.post(
            "/api/v1/agent/search",
            headers=agent_headers,
            json={"query": "5000"},
        ).json()["results"]
        == []
    )
    assert (
        client.post(
            "/api/v1/agent/search",
            headers=agent_headers,
            json={"query": "9000"},
        ).json()["results"][0]["item_id"]
        == new_item_id
    )

    undone = client.post(
        f"/api/v1/governance/tasks/{second_task['id']}/undo",
        headers=owner_headers,
    )
    assert undone.status_code == 200
    assert (
        client.get(
            f"/api/v1/items/{old_item_id}",
            headers=owner_headers,
        ).json()["state"]
        == "ready"
    )


def test_renamed_source_path_does_not_pin_an_old_generation(
    client: TestClient,
    owner_headers: dict[str, str],
    agent_headers: dict[str, str],
    watched_folder: Path,
) -> None:
    original = watched_folder / "original.txt"
    original.write_text("OLDMARK personal plan version one.", encoding="utf-8")
    source = create_folder_source(client, owner_headers, watched_folder)
    client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )
    first_task = client.get(
        "/api/v1/governance/tasks",
        headers=owner_headers,
    ).json()["items"][0]
    old_item_id = first_task["item_id"]
    client.post(
        f"/api/v1/governance/tasks/{first_task['id']}/apply",
        headers=owner_headers,
    )

    renamed = watched_folder / "renamed.txt"
    original.rename(renamed)
    rename_run = client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    ).json()
    assert rename_run["duplicate_count"] == 1

    renamed.write_text("NEWMARK personal plan version two.", encoding="utf-8")
    update_run = client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    ).json()
    assert update_run["imported_count"] == 1
    second_task = client.get(
        "/api/v1/governance/tasks",
        headers=owner_headers,
    ).json()["items"][0]
    assert second_task["proposal"]["supersedes_item_id"] == old_item_id

    applied = client.post(
        f"/api/v1/governance/tasks/{second_task['id']}/apply",
        headers=owner_headers,
    )
    assert applied.status_code == 200
    assert (
        client.get(
            f"/api/v1/items/{old_item_id}",
            headers=owner_headers,
        ).json()["state"]
        == "archived"
    )
    assert (
        client.post(
            "/api/v1/agent/search",
            headers=agent_headers,
            json={"query": "OLDMARK"},
        ).json()["results"]
        == []
    )
    assert (
        client.post(
            "/api/v1/agent/search",
            headers=agent_headers,
            json={"query": "NEWMARK"},
        ).json()["count"]
        == 1
    )
    latest = client.post(
        "/api/v1/agent/search",
        headers=agent_headers,
        json={"query": "NEWMARK"},
    ).json()["results"][0]
    assert latest["source"].endswith("/renamed.txt")


def test_agent_citation_hides_absolute_path_after_source_is_removed(
    client: TestClient,
    owner_headers: dict[str, str],
    agent_headers: dict[str, str],
    watched_folder: Path,
) -> None:
    secret_path = watched_folder / "private-plan.txt"
    secret_path.write_text("PATHLEAKCHECK private plan.", encoding="utf-8")
    source = create_folder_source(
        client,
        owner_headers,
        watched_folder,
        name="家庭资料",
    )
    client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )
    task = client.get(
        "/api/v1/governance/tasks",
        headers=owner_headers,
    ).json()["items"][0]
    client.post(
        f"/api/v1/governance/tasks/{task['id']}/apply",
        headers=owner_headers,
    )
    client.delete(f"/api/v1/sources/{source['id']}", headers=owner_headers)

    result = client.post(
        "/api/v1/agent/search",
        headers=agent_headers,
        json={"query": "PATHLEAKCHECK"},
    ).json()["results"][0]
    assert result["source"] == "家庭资料/private-plan.txt"
    assert "file://" not in result["source"]
    assert str(watched_folder) not in result["source"]


def test_missing_file_requires_deletion_governance_before_archiving(
    client: TestClient,
    owner_headers: dict[str, str],
    agent_headers: dict[str, str],
    watched_folder: Path,
) -> None:
    document = watched_folder / "retention.txt"
    document.write_text("RETENTIONCHECK keep until I decide.", encoding="utf-8")
    source = create_folder_source(client, owner_headers, watched_folder)
    client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )
    review = client.get(
        "/api/v1/governance/tasks",
        headers=owner_headers,
    ).json()["items"][0]
    client.post(
        f"/api/v1/governance/tasks/{review['id']}/apply",
        headers=owner_headers,
    )

    document.unlink()
    missing_run = client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    ).json()
    assert missing_run["task_count"] == 1

    pending = client.get(
        "/api/v1/governance/tasks",
        headers=owner_headers,
    ).json()
    assert pending["total"] == 1
    deletion = pending["items"][0]
    assert deletion["kind"] == "deletion"
    assert deletion["proposal"]["patch"]["state"] == "archived"

    still_visible = client.post(
        "/api/v1/agent/search",
        headers=agent_headers,
        json={"query": "RETENTIONCHECK"},
    ).json()
    assert still_visible["count"] == 1

    repeated_run = client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    ).json()
    assert repeated_run["task_count"] == 0
    assert (
        client.get(
            "/api/v1/governance/tasks",
            headers=owner_headers,
        ).json()["total"]
        == 1
    )

    archived = client.post(
        f"/api/v1/governance/tasks/{deletion['id']}/apply",
        headers=owner_headers,
    )
    assert archived.status_code == 200
    assert archived.json()["task"]["item"]["state"] == "archived"
    assert (
        client.post(
            "/api/v1/agent/search",
            headers=agent_headers,
            json={"query": "RETENTIONCHECK"},
        ).json()["results"]
        == []
    )

    undone = client.post(
        f"/api/v1/governance/tasks/{deletion['id']}/undo",
        headers=owner_headers,
    )
    assert undone.status_code == 200
    assert undone.json()["task"]["item"]["state"] == "ready"
    assert (
        client.post(
            "/api/v1/agent/search",
            headers=agent_headers,
            json={"query": "RETENTIONCHECK"},
        ).json()["count"]
        == 1
    )


def test_reappearing_file_resolves_pending_deletion_task(
    client: TestClient,
    owner_headers: dict[str, str],
    watched_folder: Path,
) -> None:
    document = watched_folder / "returns.txt"
    content = "RETURNINGCHECK same personal knowledge."
    document.write_text(content, encoding="utf-8")
    source = create_folder_source(client, owner_headers, watched_folder)
    client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )
    review = client.get(
        "/api/v1/governance/tasks",
        headers=owner_headers,
    ).json()["items"][0]
    client.post(
        f"/api/v1/governance/tasks/{review['id']}/apply",
        headers=owner_headers,
    )

    document.unlink()
    client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )
    deletion = client.get(
        "/api/v1/governance/tasks",
        headers=owner_headers,
    ).json()["items"][0]
    assert deletion["kind"] == "deletion"

    document.write_text(content, encoding="utf-8")
    restored = client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    ).json()
    assert restored["duplicate_count"] == 1
    assert restored["task_count"] == 0
    assert (
        client.get(
            f"/api/v1/governance/tasks/{deletion['id']}",
            headers=owner_headers,
        ).json()["status"]
        == "skipped"
    )
    assert (
        client.get(
            "/api/v1/governance/tasks",
            headers=owner_headers,
        ).json()["total"]
        == 0
    )


def test_missing_unreviewed_file_cannot_become_an_orphan_ready_item(
    client: TestClient,
    owner_headers: dict[str, str],
    agent_headers: dict[str, str],
    watched_folder: Path,
) -> None:
    document = watched_folder / "unreviewed.txt"
    document.write_text("ORPHANCHECK not yet approved.", encoding="utf-8")
    source = create_folder_source(client, owner_headers, watched_folder)
    client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )
    original_review = client.get(
        "/api/v1/governance/tasks",
        headers=owner_headers,
    ).json()["items"][0]

    document.unlink()
    missing = client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    ).json()
    assert missing["task_count"] == 1
    assert (
        client.get(
            f"/api/v1/governance/tasks/{original_review['id']}",
            headers=owner_headers,
        ).json()["status"]
        == "skipped"
    )

    pending = client.get(
        "/api/v1/governance/tasks",
        headers=owner_headers,
    ).json()
    assert pending["total"] == 1
    assert pending["items"][0]["kind"] == "deletion"
    stale_apply = client.post(
        f"/api/v1/governance/tasks/{original_review['id']}/apply",
        headers=owner_headers,
    )
    assert stale_apply.status_code == 409
    assert (
        client.post(
            "/api/v1/agent/search",
            headers=agent_headers,
            json={"query": "ORPHANCHECK"},
        ).json()["results"]
        == []
    )


def test_folder_and_mobile_text_share_one_canonical_fingerprint(
    client: TestClient,
    owner_headers: dict[str, str],
    watched_folder: Path,
) -> None:
    content = "CROSSCHANNELCHECK identical personal text."
    (watched_folder / "shared.txt").write_text(content, encoding="utf-8")
    source = create_folder_source(client, owner_headers, watched_folder)
    client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )
    folder_task = client.get(
        "/api/v1/governance/tasks",
        headers=owner_headers,
    ).json()["items"][0]

    captured = client.post(
        "/api/v1/captures",
        headers=owner_headers,
        json={"title": "手机里的同一段", "text": content},
    )
    assert captured.status_code == 201
    assert captured.json()["deduplicated"] is True
    assert captured.json()["item_id"] == folder_task["item_id"]
    assert captured.json()["task_id"] == folder_task["id"]
    assert (
        client.get(
            "/api/v1/items",
            headers=owner_headers,
        ).json()["total"]
        == 1
    )


def test_binary_files_are_skipped_and_empty_text_cannot_be_ready(
    client: TestClient,
    owner_headers: dict[str, str],
    watched_folder: Path,
) -> None:
    (watched_folder / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\nbinary")
    (watched_folder / "empty.txt").write_text("", encoding="utf-8")
    source = create_folder_source(client, owner_headers, watched_folder)

    synced = client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    ).json()
    assert synced["scanned_count"] == 2
    assert synced["skipped_count"] == 1
    assert synced["imported_count"] == 1
    task = client.get(
        "/api/v1/governance/tasks",
        headers=owner_headers,
    ).json()["items"][0]

    blocked = client.post(
        f"/api/v1/governance/tasks/{task['id']}/apply",
        headers=owner_headers,
    )
    assert blocked.status_code == 422
    assert "空内容" in blocked.json()["detail"]


def test_explicit_null_category_is_preserved_for_apply_and_item_patch(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    first = client.post(
        "/api/v1/captures",
        headers=owner_headers,
        json={"title": "清除分类一", "text": "CATEGORYCLEAR apply path"},
    ).json()
    applied = client.post(
        f"/api/v1/governance/tasks/{first['task_id']}/apply",
        headers=owner_headers,
        json={"patch": {"category": None}},
    )
    assert applied.status_code == 200
    assert applied.json()["task"]["item"]["category"] is None

    second = client.post(
        "/api/v1/captures",
        headers=owner_headers,
        json={"title": "清除分类二", "text": "CATEGORYCLEAR patch path"},
    ).json()
    client.post(
        f"/api/v1/governance/tasks/{second['task_id']}/apply",
        headers=owner_headers,
    )
    cleared = client.patch(
        f"/api/v1/items/{second['item_id']}",
        headers=owner_headers,
        json={"category": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["category"] is None


def test_changed_file_reappearing_after_deletion_keeps_one_ready_generation(
    client: TestClient,
    owner_headers: dict[str, str],
    agent_headers: dict[str, str],
    watched_folder: Path,
) -> None:
    document = watched_folder / "recreated.txt"
    document.write_text("OLDRECREATECHECK version one.", encoding="utf-8")
    source = create_folder_source(client, owner_headers, watched_folder)
    client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )
    first_review = client.get(
        "/api/v1/governance/tasks",
        headers=owner_headers,
    ).json()["items"][0]
    old_item_id = first_review["item_id"]
    client.post(
        f"/api/v1/governance/tasks/{first_review['id']}/apply",
        headers=owner_headers,
    )

    document.unlink()
    client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )
    deletion = client.get(
        "/api/v1/governance/tasks",
        headers=owner_headers,
    ).json()["items"][0]
    assert deletion["kind"] == "deletion"

    document.write_text("NEWRECREATECHECK version two.", encoding="utf-8")
    recreated = client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    ).json()
    assert recreated["imported_count"] == 1
    new_review = client.get(
        "/api/v1/governance/tasks",
        headers=owner_headers,
    ).json()["items"][0]
    assert new_review["kind"] == "review"
    assert new_review["proposal"]["supersedes_item_id"] == old_item_id
    assert (
        client.get(
            f"/api/v1/governance/tasks/{deletion['id']}",
            headers=owner_headers,
        ).json()["status"]
        == "skipped"
    )

    assert (
        client.post(
            "/api/v1/agent/search",
            headers=agent_headers,
            json={"query": "OLDRECREATECHECK"},
        ).json()["count"]
        == 1
    )
    assert (
        client.post(
            "/api/v1/agent/search",
            headers=agent_headers,
            json={"query": "NEWRECREATECHECK"},
        ).json()["results"]
        == []
    )

    applied = client.post(
        f"/api/v1/governance/tasks/{new_review['id']}/apply",
        headers=owner_headers,
    )
    assert applied.status_code == 200
    assert (
        client.get(
            f"/api/v1/items/{old_item_id}",
            headers=owner_headers,
        ).json()["state"]
        == "archived"
    )
    assert (
        client.post(
            "/api/v1/agent/search",
            headers=agent_headers,
            json={"query": "OLDRECREATECHECK"},
        ).json()["results"]
        == []
    )
    assert (
        client.post(
            "/api/v1/agent/search",
            headers=agent_headers,
            json={"query": "NEWRECREATECHECK"},
        ).json()["count"]
        == 1
    )


def test_two_ready_predecessors_converging_to_existing_ready_content(
    client: TestClient,
    owner_headers: dict[str, str],
    agent_headers: dict[str, str],
    watched_folder: Path,
) -> None:
    alpha = watched_folder / "alpha.txt"
    charlie = watched_folder / "charlie.txt"
    alpha.write_text("ALPHACONVERGECHECK original.", encoding="utf-8")
    charlie.write_text("CHARLIECONVERGECHECK original.", encoding="utf-8")
    source = create_folder_source(client, owner_headers, watched_folder)
    client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )
    initial_tasks = client.get(
        "/api/v1/governance/tasks",
        headers=owner_headers,
    ).json()["items"]
    item_ids = {task["title"]: task["item_id"] for task in initial_tasks}
    for task in initial_tasks:
        client.post(
            f"/api/v1/governance/tasks/{task['id']}/apply",
            headers=owner_headers,
        )

    converged_content = "BRAVOCONVERGECHECK shared final content."
    charlie.write_text(converged_content, encoding="utf-8")
    client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )
    bravo_task = client.get(
        "/api/v1/governance/tasks",
        headers=owner_headers,
    ).json()["items"][0]
    client.post(
        f"/api/v1/governance/tasks/{bravo_task['id']}/apply",
        headers=owner_headers,
    )

    alpha.write_text(converged_content, encoding="utf-8")
    converged = client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    ).json()
    assert converged["duplicate_count"] == 1
    assert (
        client.get(
            f"/api/v1/items/{item_ids['alpha']}",
            headers=owner_headers,
        ).json()["state"]
        == "archived"
    )
    assert (
        client.get(
            f"/api/v1/items/{item_ids['charlie']}",
            headers=owner_headers,
        ).json()["state"]
        == "archived"
    )
    assert (
        client.post(
            "/api/v1/agent/search",
            headers=agent_headers,
            json={"query": "ALPHACONVERGECHECK"},
        ).json()["results"]
        == []
    )
    assert (
        client.post(
            "/api/v1/agent/search",
            headers=agent_headers,
            json={"query": "BRAVOCONVERGECHECK"},
        ).json()["count"]
        == 1
    )


def test_pending_generation_can_supersede_multiple_predecessors_and_undo(
    client: TestClient,
    owner_headers: dict[str, str],
    watched_folder: Path,
) -> None:
    first = watched_folder / "first.txt"
    second = watched_folder / "second.txt"
    first.write_text("FIRSTMULTICHECK old.", encoding="utf-8")
    second.write_text("SECONDMULTICHECK old.", encoding="utf-8")
    source = create_folder_source(client, owner_headers, watched_folder)
    client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )
    initial_tasks = client.get(
        "/api/v1/governance/tasks",
        headers=owner_headers,
    ).json()["items"]
    predecessor_ids = {task["item_id"] for task in initial_tasks}
    for task in initial_tasks:
        client.post(
            f"/api/v1/governance/tasks/{task['id']}/apply",
            headers=owner_headers,
        )

    shared = "PENDINGMULTICHECK shared replacement."
    second.write_text(shared, encoding="utf-8")
    client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )
    first.write_text(shared, encoding="utf-8")
    client.post(
        f"/api/v1/sources/{source['id']}/sync",
        headers=owner_headers,
    )
    replacement_task = client.get(
        "/api/v1/governance/tasks",
        headers=owner_headers,
    ).json()["items"][0]
    assert set(replacement_task["proposal"]["supersedes_item_ids"]) == (
        predecessor_ids
    )

    applied = client.post(
        f"/api/v1/governance/tasks/{replacement_task['id']}/apply",
        headers=owner_headers,
    )
    assert applied.status_code == 200
    for item_id in predecessor_ids:
        assert (
            client.get(
                f"/api/v1/items/{item_id}",
                headers=owner_headers,
            ).json()["state"]
            == "archived"
        )

    undone = client.post(
        f"/api/v1/governance/tasks/{replacement_task['id']}/undo",
        headers=owner_headers,
    )
    assert undone.status_code == 200
    for item_id in predecessor_ids:
        assert (
            client.get(
                f"/api/v1/items/{item_id}",
                headers=owner_headers,
            ).json()["state"]
            == "ready"
        )
