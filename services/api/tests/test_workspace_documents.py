from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

WORKSPACE_PATH = "/api/v1/workspaces/ws_default"
OWNER_MEMBER_ID = "member_owner"
OWNER_DEVICE_ID = "pytest-document-owner"


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _write_headers(
    auth_headers: dict[str, str],
    idempotency_key: str,
    *,
    device_id: str = OWNER_DEVICE_ID,
    if_match: int | None = None,
) -> dict[str, str]:
    headers = {
        **auth_headers,
        "Idempotency-Key": idempotency_key,
        "X-Device-ID": device_id,
    }
    if if_match is not None:
        headers["If-Match"] = f'"{if_match}"'
    return headers


def _add_member(client: TestClient, member_id: str, display_name: str) -> None:
    with client.app.state.workspace_service.database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO secretary_workspace_members(
                id, workspace_id, kind, role, display_name, active,
                created_at, updated_at
            ) VALUES (?, 'ws_default', 'person', 'viewer', ?, 1, ?, ?)
            """,
            (
                member_id,
                display_name,
                "2026-08-02T00:00:00Z",
                "2026-08-02T00:00:00Z",
            ),
        )


def _document_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "domain": "work",
        "kind": "contract",
        "title": "供应协议 2026",
        "content": "第一条 服务范围。第二条 退出机制。第三条 违约责任。",
        "mime_type": "text/markdown",
        "source": {
            "source_kind": "manual",
            "authority": "user_provided",
        },
        "tags": ["合同", "供应商"],
    }
    payload.update(overrides)
    return payload


def _create_document(
    client: TestClient,
    auth_headers: dict[str, str],
    *,
    key: str,
    payload: dict[str, Any],
    device_id: str = OWNER_DEVICE_ID,
) -> dict[str, Any]:
    response = client.post(
        f"{WORKSPACE_PATH}/documents",
        headers=_write_headers(auth_headers, key, device_id=device_id),
        json=payload,
    )
    assert response.status_code == 201, response.text
    assert response.headers["etag"] == '"1"'
    return response.json()


def _mobile_session(
    client: TestClient, owner_headers: dict[str, str], *, device_id: str
) -> dict[str, Any]:
    pairing = client.post("/api/v1/mobile/pairings", headers=owner_headers)
    assert pairing.status_code == 201, pairing.text
    claimed = client.post(
        "/api/v1/mobile/pairings/claim",
        json={
            "code": pairing.json()["code"],
            "device_id": device_id,
            "display_name": "文档测试手机",
            "platform": "android",
            "app_version": "1.0.0",
        },
    )
    assert claimed.status_code == 200, claimed.text
    return claimed.json()


def test_contract_review_excerpt_archive_and_sync_are_versioned(
    client: TestClient,
    owner_headers: dict[str, str],
    agent_headers: dict[str, str],
) -> None:
    viewer_id = "member-document-viewer"
    outsider_id = "member-document-outsider"
    _add_member(client, viewer_id, "法务同事")
    _add_member(client, outsider_id, "无权限同事")

    payload = _document_payload(
        access_scope="restricted",
        viewer_member_ids=[viewer_id],
        storage_ref="vault://contracts/supplier-2026.md",
    )
    create_headers = _write_headers(owner_headers, "document-contract-create-001")
    created_response = client.post(
        f"{WORKSPACE_PATH}/documents", headers=create_headers, json=payload
    )
    assert created_response.status_code == 201, created_response.text
    document = created_response.json()
    assert document["status"] == "review_pending"
    assert document["access_scope"] == "restricted"
    assert document["viewer_member_ids"] == [viewer_id]
    assert document["reviews"] == []
    assert document["excerpts"] == []
    with client.app.state.workspace_service.database.connect() as connection:
        cached_response = connection.execute(
            """
            SELECT response_json FROM secretary_workspace_idempotency
            WHERE workspace_id = 'ws_default' AND actor_id = ?
              AND operation = 'document.create' AND idempotency_key = ?
            """,
            (OWNER_MEMBER_ID, "document-contract-create-001"),
        ).fetchone()["response_json"]
    assert document["content"] not in cached_response
    assert document["storage_ref"] not in cached_response
    assert json.loads(cached_response) == {
        "__centaur_document_reference_v1": {
            "document_id": document["id"],
            "version": document["version"],
        }
    }
    summary = client.get(
        f"{WORKSPACE_PATH}/documents", headers=owner_headers
    ).json()["items"][0]
    assert {
        "content",
        "storage_ref",
        "source",
        "template_variables",
        "reviews",
        "excerpts",
    }.isdisjoint(summary)

    replay = client.post(
        f"{WORKSPACE_PATH}/documents", headers=create_headers, json=payload
    )
    assert replay.status_code == 201, replay.text
    assert replay.json() == document
    changed_replay = client.post(
        f"{WORKSPACE_PATH}/documents",
        headers=create_headers,
        json={**payload, "title": "不能复用幂等键"},
    )
    assert changed_replay.status_code == 409

    agent_read = client.get(
        f"{WORKSPACE_PATH}/documents", headers=agent_headers
    )
    assert agent_read.status_code == 401
    missing_precondition = client.patch(
        f"{WORKSPACE_PATH}/documents/{document['id']}",
        headers=_write_headers(owner_headers, "document-patch-no-etag-001"),
        json={"title": "缺少版本条件"},
    )
    assert missing_precondition.status_code == 428

    selected_text = "第二条 退出机制"
    start = document["content"].index(selected_text)
    excerpt_response = client.post(
        f"{WORKSPACE_PATH}/documents/{document['id']}/excerpts",
        headers=_write_headers(owner_headers, "document-excerpt-create-001"),
        json={
            "expected_version": document["version"],
            "title": "退出机制",
            "start_offset": start,
            "end_offset": start + len(selected_text),
            "viewer_member_ids": [viewer_id],
        },
    )
    assert excerpt_response.status_code == 201, excerpt_response.text
    document = excerpt_response.json()
    assert document["version"] == 2
    assert document["excerpts"][0]["content"] == selected_text
    assert document["excerpts"][0]["source_document_version"] == 1

    allowed_preview = client.get(
        f"{WORKSPACE_PATH}/documents/{document['id']}/excerpts",
        headers=owner_headers,
        params={"viewer_member_id": viewer_id},
    )
    assert allowed_preview.status_code == 200, allowed_preview.text
    assert allowed_preview.json()["items"][0]["content"] == selected_text
    denied_preview = client.get(
        f"{WORKSPACE_PATH}/documents/{document['id']}/excerpts",
        headers=owner_headers,
        params={"viewer_member_id": outsider_id},
    )
    assert denied_preview.status_code == 403
    with client.app.state.workspace_service.database.transaction() as connection:
        connection.execute(
            """
            UPDATE secretary_workspace_members SET active = 0
            WHERE id = ? AND workspace_id = 'ws_default'
            """,
            (viewer_id,),
        )
    inactive_preview = client.get(
        f"{WORKSPACE_PATH}/documents/{document['id']}/excerpts",
        headers=owner_headers,
        params={"viewer_member_id": viewer_id},
    )
    assert inactive_preview.status_code == 422
    assert "已停用" in inactive_preview.json()["detail"]
    with client.app.state.workspace_service.database.transaction() as connection:
        connection.execute(
            """
            UPDATE secretary_workspace_members SET active = 1
            WHERE id = ? AND workspace_id = 'ws_default'
            """,
            (viewer_id,),
        )

    stale_excerpt = client.post(
        f"{WORKSPACE_PATH}/documents/{document['id']}/excerpts",
        headers=_write_headers(owner_headers, "document-excerpt-stale-001"),
        json={
            "expected_version": 1,
            "title": "陈旧片段",
            "start_offset": 0,
            "end_offset": 2,
            "viewer_member_ids": [viewer_id],
        },
    )
    assert stale_excerpt.status_code == 412

    reviewed_response = client.post(
        f"{WORKSPACE_PATH}/documents/{document['id']}/reviews",
        headers=_write_headers(owner_headers, "document-contract-review-001"),
        json={
            "expected_version": document["version"],
            "review_type": "contract",
            "summary": "退出机制存在通知期限缺口，需要修改后签署。",
            "conclusion": "changes_required",
            "findings": [
                {
                    "severity": "high",
                    "title": "退出通知期限缺失",
                    "detail": "退出条款未规定提前通知天数。",
                    "recommendation": "补充至少提前三十日书面通知。",
                }
            ],
        },
    )
    assert reviewed_response.status_code == 201, reviewed_response.text
    document = reviewed_response.json()
    assert document["status"] == "reviewed"
    assert document["version"] == 3
    assert document["reviews"][0]["document_version"] == 2
    assert document["reviews"][0]["conclusion"] == "changes_required"

    revised = client.patch(
        f"{WORKSPACE_PATH}/documents/{document['id']}",
        headers=_write_headers(
            owner_headers,
            "document-contract-revise-001",
            if_match=document["version"],
        ),
        json={"content": f"{document['content']} 退出须提前三十日书面通知。"},
    )
    assert revised.status_code == 200, revised.text
    document = revised.json()
    assert document["status"] == "review_pending"
    assert document["version"] == 4
    assert document["excerpts"] == []
    assert document["reviews"][0]["document_version"] == 2

    revoked_preview = client.get(
        f"{WORKSPACE_PATH}/documents/{document['id']}/excerpts",
        headers=owner_headers,
        params={"viewer_member_id": viewer_id},
    )
    assert revoked_preview.status_code == 200, revoked_preview.text
    assert revoked_preview.json()["items"] == []

    archived = client.post(
        f"{WORKSPACE_PATH}/documents/{document['id']}/archive",
        headers=_write_headers(owner_headers, "document-archive-001"),
        json={"expected_version": document["version"]},
    )
    assert archived.status_code == 200, archived.text
    document = archived.json()
    assert document["status"] == "archived"
    assert document["version"] == 5
    archive_replay = client.post(
        f"{WORKSPACE_PATH}/documents/{document['id']}/archive",
        headers=_write_headers(owner_headers, "document-archive-001"),
        json={"expected_version": 4},
    )
    assert archive_replay.status_code == 200, archive_replay.text
    assert archive_replay.json() == document
    archive_changed_replay = client.post(
        f"{WORKSPACE_PATH}/documents/{document['id']}/archive",
        headers=_write_headers(owner_headers, "document-archive-001"),
        json={"expected_version": document["version"]},
    )
    assert archive_changed_replay.status_code == 409
    stale_create_replay = client.post(
        f"{WORKSPACE_PATH}/documents", headers=create_headers, json=payload
    )
    assert stale_create_replay.status_code == 409
    assert "同步最新版本" in stale_create_replay.json()["detail"]
    immutable = client.patch(
        f"{WORKSPACE_PATH}/documents/{document['id']}",
        headers=_write_headers(
            owner_headers,
            "document-archived-patch-001",
            if_match=document["version"],
        ),
        json={"title": "归档后不得修改"},
    )
    assert immutable.status_code == 409

    bootstrap = client.get(
        f"{WORKSPACE_PATH}/bootstrap", headers=owner_headers
    ).json()
    assert "documents" not in bootstrap
    fetched = client.get(
        f"{WORKSPACE_PATH}/documents/{document['id']}", headers=owner_headers
    )
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["status"] == "archived"
    sync = client.get(f"{WORKSPACE_PATH}/sync?after=0", headers=owner_headers)
    assert sync.status_code == 200, sync.text
    changes = sync.json()["changes"]
    assert [change["event_type"] for change in changes] == [
        "document.created",
        "document.excerpt_created",
        "document.reviewed",
        "document.updated",
        "document.archived",
    ]
    assert [change["cursor"] for change in changes] == list(range(1, 6))
    assert all(change["device_id"] == OWNER_DEVICE_ID for change in changes)
    event_forbidden_fields = {
        "content",
        "storage_ref",
        "source",
        "template_variables",
        "reviews",
        "excerpts",
    }
    assert all(
        event_forbidden_fields.isdisjoint(change["payload"]) for change in changes
    )


def test_template_generation_is_strict_idempotent_and_mobile_device_bound(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    template = _create_document(
        client,
        owner_headers,
        key="document-template-create-001",
        payload=_document_payload(
            kind="template",
            title="工作汇报模板",
            content="# {{姓名}} 周报\n目标：{{目标}}\n进展：{{进展}}",
            tags=["模板", "周报"],
        ),
    )
    assert template["kind"] == "template"
    assert template["status"] == "draft"

    missing_variable = client.post(
        f"{WORKSPACE_PATH}/documents/{template['id']}/generate",
        headers=_write_headers(owner_headers, "document-template-missing-001"),
        json={
            "expected_version": template["version"],
            "title": "张三周报",
            "kind": "work_report",
            "variables": {"姓名": "张三", "目标": "完成上线"},
        },
    )
    assert missing_variable.status_code == 422
    assert "进展" in missing_variable.json()["detail"]

    phone_device_id = "document-phone-001"
    session = _mobile_session(client, owner_headers, device_id=phone_device_id)
    phone_auth = {"Authorization": f"Bearer {session['access_token']}"}
    generation_payload = {
        "expected_version": template["version"],
        "title": "张三 2026-W31 周报",
        "kind": "work_report",
        "variables": {
            "姓名": "张三",
            "目标": "完成上线",
            "进展": "关键功能已验收",
        },
        "tags": ["周报", "2026-W31"],
    }
    generation_headers = _write_headers(
        phone_auth,
        "document-template-generate-001",
        device_id=phone_device_id,
    )
    generated_response = client.post(
        f"{WORKSPACE_PATH}/documents/{template['id']}/generate",
        headers=generation_headers,
        json=generation_payload,
    )
    assert generated_response.status_code == 201, generated_response.text
    generated = generated_response.json()
    assert generated["kind"] == "work_report"
    assert generated["status"] == "review_pending"
    assert generated["origin_template_id"] == template["id"]
    assert generated["origin_template_version"] == template["version"]
    assert generated["template_variables"] == generation_payload["variables"]
    assert generated["content"] == (
        "# 张三 周报\n目标：完成上线\n进展：关键功能已验收"
    )
    assert "{{" not in generated["content"]

    replay = client.post(
        f"{WORKSPACE_PATH}/documents/{template['id']}/generate",
        headers=generation_headers,
        json=generation_payload,
    )
    assert replay.status_code == 201, replay.text
    assert replay.json() == generated
    changed_replay = client.post(
        f"{WORKSPACE_PATH}/documents/{template['id']}/generate",
        headers=generation_headers,
        json={**generation_payload, "title": "不同标题"},
    )
    assert changed_replay.status_code == 409

    forged_device = client.post(
        f"{WORKSPACE_PATH}/documents/{template['id']}/generate",
        headers=_write_headers(
            phone_auth,
            "document-template-forged-device-001",
            device_id="forged-phone",
        ),
        json=generation_payload,
    )
    assert forged_device.status_code == 403

    reviewed = client.post(
        f"{WORKSPACE_PATH}/documents/{generated['id']}/reviews",
        headers=_write_headers(owner_headers, "document-report-review-001"),
        json={
            "expected_version": generated["version"],
            "review_type": "work_report",
            "summary": "目标、进展与结果口径一致。",
            "conclusion": "approved",
            "findings": [],
        },
    )
    assert reviewed.status_code == 201, reviewed.text
    assert reviewed.json()["status"] == "reviewed"

    listed = client.get(f"{WORKSPACE_PATH}/documents", headers=phone_auth)
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 2
    assert {item["id"] for item in listed.json()["items"]} == {
        template["id"],
        generated["id"],
    }
    sync = client.get(f"{WORKSPACE_PATH}/sync?after=0", headers=phone_auth)
    assert sync.status_code == 200, sync.text
    assert [change["event_type"] for change in sync.json()["changes"]] == [
        "document.created",
        "document.generated",
        "document.reviewed",
    ]
    assert sync.json()["changes"][1]["device_id"] == phone_device_id


def test_document_schema_rejects_invalid_audience_review_and_source_item(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    invalid_audience = client.post(
        f"{WORKSPACE_PATH}/documents",
        headers=_write_headers(owner_headers, "document-invalid-audience-001"),
        json=_document_payload(access_scope="restricted", viewer_member_ids=[]),
    )
    assert invalid_audience.status_code == 422

    missing_source = client.post(
        f"{WORKSPACE_PATH}/documents",
        headers=_write_headers(owner_headers, "document-missing-source-item-001"),
        json=_document_payload(source_item_id="item-does-not-exist"),
    )
    assert missing_source.status_code == 422

    with client.app.state.workspace_service.database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO items(
                id, content_hash, origin_uri, file_name, mime_type, title,
                text_content, size_bytes, state, tags_json, metadata_json,
                version, created_at, updated_at
            ) VALUES (
                'item-ready-global', ?, 'file:///global.md', 'global.md',
                'text/markdown', '全库可见文件', 'Agent 可检索正文', 18, 'ready',
                '[]', '{}', 1, ?, ?
            )
            """,
            (
                "a" * 64,
                "2026-08-02T00:00:00Z",
                "2026-08-02T00:00:00Z",
            ),
        )
    acl_bypass = client.post(
        f"{WORKSPACE_PATH}/documents",
        headers=_write_headers(owner_headers, "document-ready-acl-bypass-001"),
        json=_document_payload(source_item_id="item-ready-global"),
    )
    assert acl_bypass.status_code == 409
    assert "Agent" in acl_bypass.json()["detail"]

    with client.app.state.workspace_service.database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO items(
                id, content_hash, origin_uri, file_name, mime_type, title,
                text_content, size_bytes, state, tags_json, metadata_json,
                version, created_at, updated_at
            ) VALUES (
                'item-inbox-global', ?, 'file:///future-global.md',
                'future-global.md', 'text/markdown', '待治理文件',
                '稍后可能进入 Agent 全库检索', 32, 'inbox', '[]', '{}', 1, ?, ?
            )
            """,
            (
                "b" * 64,
                "2026-08-02T00:00:00Z",
                "2026-08-02T00:00:00Z",
            ),
        )
    future_acl_bypass = client.post(
        f"{WORKSPACE_PATH}/documents",
        headers=_write_headers(owner_headers, "document-future-acl-bypass-001"),
        json=_document_payload(
            source_item_id="item-inbox-global",
            access_scope="owner_only",
        ),
    )
    assert future_acl_bypass.status_code == 409
    assert "workspace" in future_acl_bypass.json()["detail"]

    workspace_library_document = _create_document(
        client,
        owner_headers,
        key="document-workspace-source-item-001",
        payload=_document_payload(
            kind="general",
            title="工作区文档库文件",
            source_item_id="item-ready-global",
            access_scope="workspace",
        ),
    )
    assert workspace_library_document["source_item_id"] == "item-ready-global"

    general = _create_document(
        client,
        owner_headers,
        key="document-general-create-001",
        payload=_document_payload(kind="general", title="普通说明文档"),
    )
    mismatched_review = client.post(
        f"{WORKSPACE_PATH}/documents/{general['id']}/reviews",
        headers=_write_headers(owner_headers, "document-review-mismatch-001"),
        json={
            "expected_version": general["version"],
            "review_type": "contract",
            "summary": "普通文档不能冒充合同审阅。",
            "conclusion": "approved",
            "findings": [],
        },
    )
    assert mismatched_review.status_code == 422

    invalid_negative_review = client.post(
        f"{WORKSPACE_PATH}/documents/{general['id']}/reviews",
        headers=_write_headers(owner_headers, "document-review-no-findings-001"),
        json={
            "expected_version": general["version"],
            "review_type": "contract",
            "summary": "缺少发现项。",
            "conclusion": "rejected",
            "findings": [],
        },
    )
    assert invalid_negative_review.status_code == 422

    assert (
        client.get(f"{WORKSPACE_PATH}/documents", headers=owner_headers).json()[
            "total"
        ]
        == 2
    )
    assert (
        client.get(
            f"{WORKSPACE_PATH}/documents/{general['id']}", headers=owner_headers
        ).json()["id"]
        == general["id"]
    )


def test_document_excerpt_offsets_follow_javascript_utf16_and_reject_split_pairs(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    viewer_id = "member-unicode-viewer"
    _add_member(client, viewer_id, "Unicode 审阅人")
    content = "😀前缀｜第二条 退出机制｜结尾"
    document = _create_document(
        client,
        owner_headers,
        key="document-unicode-create-001",
        payload=_document_payload(
            kind="general",
            content=content,
            access_scope="restricted",
            viewer_member_ids=[viewer_id],
        ),
    )

    split_surrogate = client.post(
        f"{WORKSPACE_PATH}/documents/{document['id']}/excerpts",
        headers=_write_headers(owner_headers, "document-unicode-split-001"),
        json={
            "expected_version": document["version"],
            "title": "非法半个字符",
            "start_offset": 1,
            "end_offset": 3,
            "viewer_member_ids": [viewer_id],
        },
    )
    assert split_surrogate.status_code == 422
    assert "Unicode" in split_surrogate.json()["detail"]

    selected = "第二条 退出机制"
    prefix = content[: content.index(selected)]
    start = _utf16_length(prefix)
    end = start + _utf16_length(selected)
    excerpt = client.post(
        f"{WORKSPACE_PATH}/documents/{document['id']}/excerpts",
        headers=_write_headers(owner_headers, "document-unicode-excerpt-001"),
        json={
            "expected_version": document["version"],
            "title": "正确 Unicode 片段",
            "start_offset": start,
            "end_offset": end,
            "viewer_member_ids": [viewer_id],
        },
    )
    assert excerpt.status_code == 201, excerpt.text
    stored = excerpt.json()["excerpts"][0]
    assert stored["content"] == selected
    assert stored["start_offset"] == start
    assert stored["end_offset"] == end


def test_template_rejects_malformed_tokens_and_placeholder_injection(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    malformed = _create_document(
        client,
        owner_headers,
        key="document-malformed-template-create-001",
        payload=_document_payload(
            kind="template",
            title="畸形模板",
            content="姓名：{{姓名}}；待填：{{未闭合",
        ),
    )
    malformed_generation = client.post(
        f"{WORKSPACE_PATH}/documents/{malformed['id']}/generate",
        headers=_write_headers(owner_headers, "document-malformed-generate-001"),
        json={
            "expected_version": malformed["version"],
            "title": "不应生成",
            "variables": {"姓名": "张三"},
        },
    )
    assert malformed_generation.status_code == 422
    assert "未解析" in malformed_generation.json()["detail"]

    regular = _create_document(
        client,
        owner_headers,
        key="document-injection-template-create-001",
        payload=_document_payload(
            kind="template",
            title="变量注入测试模板",
            content="姓名：{{姓名}}",
        ),
    )
    injected = client.post(
        f"{WORKSPACE_PATH}/documents/{regular['id']}/generate",
        headers=_write_headers(owner_headers, "document-injection-generate-001"),
        json={
            "expected_version": regular["version"],
            "title": "不应包含二次变量",
            "variables": {"姓名": "{{管理员}}"},
        },
    )
    assert injected.status_code == 422
    assert "未解析" in injected.json()["detail"]


def test_workspace_initialize_scrubs_legacy_document_idempotency_bodies(
    client: TestClient,
    owner_headers: dict[str, str],
) -> None:
    payload = _document_payload(
        storage_ref="vault://legacy/secret-contract.md",
        content="旧版本幂等缓存中的敏感合同正文",
    )
    document = _create_document(
        client,
        owner_headers,
        key="document-legacy-cache-create-001",
        payload=payload,
    )
    service = client.app.state.workspace_service
    with service.database.transaction() as connection:
        connection.execute(
            """
            UPDATE secretary_workspace_idempotency SET response_json = ?
            WHERE workspace_id = 'ws_default' AND actor_id = ?
              AND operation = 'document.create' AND idempotency_key = ?
            """,
            (
                json.dumps(document, ensure_ascii=False),
                OWNER_MEMBER_ID,
                "document-legacy-cache-create-001",
            ),
        )

    service.initialize()
    with service.database.connect() as connection:
        scrubbed = connection.execute(
            """
            SELECT response_json FROM secretary_workspace_idempotency
            WHERE workspace_id = 'ws_default' AND actor_id = ?
              AND operation = 'document.create' AND idempotency_key = ?
            """,
            (OWNER_MEMBER_ID, "document-legacy-cache-create-001"),
        ).fetchone()["response_json"]
    assert payload["content"] not in scrubbed
    assert payload["storage_ref"] not in scrubbed
    assert json.loads(scrubbed) == {
        "__centaur_document_reference_v1": {
            "document_id": document["id"],
            "version": document["version"],
        }
    }

    replay = client.post(
        f"{WORKSPACE_PATH}/documents",
        headers=_write_headers(
            owner_headers, "document-legacy-cache-create-001"
        ),
        json=payload,
    )
    assert replay.status_code == 201, replay.text
    assert replay.json() == document
