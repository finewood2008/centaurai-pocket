"""Assistant tool surface and proposal queue (DESIGN_SYSTEM_V2.md §3).

The contract under test: the agent's tool surface has exactly two tiers —
read-only projections and proposal tools that park inert pending rows — and
only the owner's apply (device id + idempotency key) writes business data.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from centaur_pocket.mcp import PROTOCOL_VERSION

READ_ONLY_TOOLS = {
    "knowledge_retrieve",
    "today_brief",
    "list_tasks",
    "task_attention",
    "list_schedule",
    "list_memos",
    "mail_summary",
}
PROPOSAL_TOOLS = {
    "propose_memo",
    "propose_task",
    "propose_calendar",
    "propose_task_change",
    "propose_mail_reply",
}
FORBIDDEN_NAME_FRAGMENTS = (
    "send",
    "issue",
    "accept",
    "close",
    "rotate",
    "grant",
    "confirm",
)


def _mcp_headers(agent_headers: dict[str, str]) -> dict[str, str]:
    return {**agent_headers, "MCP-Protocol-Version": PROTOCOL_VERSION}


def _call(client: TestClient, agent_headers: dict[str, str], name: str, arguments: dict) -> dict:
    response = client.post(
        "/api/v1/mcp",
        headers=_mcp_headers(agent_headers),
        json={
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200
    return response.json()


def _write_headers(owner_headers: dict[str, str], key: str) -> dict[str, str]:
    return {
        **owner_headers,
        "Idempotency-Key": key,
        "X-Device-ID": "device-owner-test",
    }


def test_tool_surface_has_exactly_two_tiers(
    client: TestClient,
    agent_headers: dict[str, str],
) -> None:
    response = client.post(
        "/api/v1/mcp",
        headers=_mcp_headers(agent_headers),
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert names == READ_ONLY_TOOLS | PROPOSAL_TOOLS

    for tool in tools:
        annotations = tool["annotations"]
        if tool["name"] in READ_ONLY_TOOLS:
            assert annotations["readOnlyHint"] is True, tool["name"]
        else:
            assert annotations["readOnlyHint"] is False, tool["name"]
            assert annotations["destructiveHint"] is False, tool["name"]
        # The forbidden tier is absence: nothing on the surface can send,
        # issue, accept, close, rotate or widen anything.
        assert not any(
            fragment in tool["name"] for fragment in FORBIDDEN_NAME_FRAGMENTS
        ), tool["name"]


def test_read_only_tools_project_state_without_writing(
    client: TestClient,
    agent_headers: dict[str, str],
) -> None:
    brief = _call(client, agent_headers, "today_brief", {})["result"]
    assert brief["isError"] is False
    structured = brief["structuredContent"]
    assert set(structured) >= {
        "tasks_total",
        "tasks_active",
        "schedule_scheduled",
        "attention_total",
        "governance_pending",
    }

    tasks = _call(client, agent_headers, "list_tasks", {"limit": 5})["result"]
    assert tasks["isError"] is False
    assert tasks["structuredContent"]["count"] == len(
        tasks["structuredContent"]["items"]
    )

    bad_limit = _call(client, agent_headers, "list_memos", {"limit": 0})
    assert bad_limit["error"]["code"] == -32602


def test_proposal_tools_park_pending_rows_and_never_write(
    client: TestClient,
    owner_headers: dict[str, str],
    agent_headers: dict[str, str],
) -> None:
    result = _call(
        client,
        agent_headers,
        "propose_memo",
        {
            "title": "记录供应商付款周期",
            "content": "华辰的付款周期改为月结 45 天。",
            "urgency": "high",
            "evidence": [
                {
                    "source": "outlook",
                    "ref": "msg_123",
                    "excerpt": "自 9 月起付款周期调整为月结 45 天",
                    "trust": "verified",
                }
            ],
            "provenance": {"channel": "local", "model": "qwen2.5:14b", "tool_rounds": 2},
        },
    )["result"]
    assert result["isError"] is False
    proposal = result["structuredContent"]
    assert proposal["proposal_id"].startswith("prop")
    assert proposal["kind"] == "memo"
    assert proposal["status"] == "pending"
    assert proposal["impact"]["writes"] == ["workspace.memo"]
    assert proposal["evidence"][0]["trust"] == "verified"
    assert proposal["provenance"]["model"] == "qwen2.5:14b"

    # A cloud call must carry its authorization ticket (§3.4).
    no_ticket = _call(
        client,
        agent_headers,
        "propose_memo",
        {
            "title": "x",
            "content": "y",
            "provenance": {"channel": "cloud"},
        },
    )
    assert no_ticket["error"]["code"] == -32602

    # The proposal parked nothing in the workspace: memo list is untouched.
    memos = client.get("/api/v1/workspaces/ws_default/memos", headers=owner_headers)
    assert memos.status_code == 200
    assert memos.json()["total"] == 0

    listed = client.get("/api/v1/assistant/proposals", headers=owner_headers)
    assert listed.status_code == 200
    assert listed.json()["total"] == 1

    # The queue is owner-gated: agent credentials cannot read or decide it.
    assert (
        client.get("/api/v1/assistant/proposals", headers=agent_headers).status_code
        == 401
    )
    proposal_id = proposal["proposal_id"]
    assert (
        client.post(
            f"/api/v1/assistant/proposals/{proposal_id}/dismiss",
            headers=agent_headers,
        ).status_code
        == 401
    )


def test_owner_apply_materializes_with_field_edits_and_is_terminal(
    client: TestClient,
    owner_headers: dict[str, str],
    agent_headers: dict[str, str],
) -> None:
    proposal = _call(
        client,
        agent_headers,
        "propose_memo",
        {"title": "原始标题", "content": "正文内容。"},
    )["result"]["structuredContent"]
    proposal_id = proposal["proposal_id"]
    assert proposal["evidence"] == []

    # Apply without the write contract headers is rejected.
    naked = client.post(
        f"/api/v1/assistant/proposals/{proposal_id}/apply",
        headers=owner_headers,
    )
    assert naked.status_code == 422

    # Owner-edited confirm (§3.2「修改」) merges fields before materializing.
    applied = client.post(
        f"/api/v1/assistant/proposals/{proposal_id}/apply",
        headers=_write_headers(owner_headers, "assistant-apply-1"),
        json={"fields": {"title": "改后的标题"}},
    )
    assert applied.status_code == 200
    body = applied.json()
    assert body["proposal"]["status"] == "applied"
    assert body["result_ref"].startswith("memo:")

    memos = client.get(
        "/api/v1/workspaces/ws_default/memos", headers=owner_headers
    ).json()
    assert memos["total"] == 1
    assert memos["items"][0]["title"] == "改后的标题"
    assert memos["items"][0]["source"]["source_ref"] == "assistant-proposal"

    # Decisions are terminal.
    again = client.post(
        f"/api/v1/assistant/proposals/{proposal_id}/apply",
        headers=_write_headers(owner_headers, "assistant-apply-2"),
    )
    assert again.status_code == 409

    # Unknown override fields are rejected, not silently widened.
    other = _call(
        client,
        agent_headers,
        "propose_memo",
        {"title": "第二条", "content": "内容"},
    )["result"]["structuredContent"]
    widened = client.post(
        f"/api/v1/assistant/proposals/{other['proposal_id']}/apply",
        headers=_write_headers(owner_headers, "assistant-apply-3"),
        json={"fields": {"pinned": True}},
    )
    assert widened.status_code == 422

    dismissed = client.post(
        f"/api/v1/assistant/proposals/{other['proposal_id']}/dismiss",
        headers=owner_headers,
    )
    assert dismissed.status_code == 200
    assert dismissed.json()["proposal"]["status"] == "dismissed"


def test_calendar_proposal_applies_in_workspace_timezone(
    client: TestClient,
    owner_headers: dict[str, str],
    agent_headers: dict[str, str],
) -> None:
    proposal = _call(
        client,
        agent_headers,
        "propose_calendar",
        {
            "title": "季度经营复盘",
            "start_at": "2026-08-12T09:00:00+08:00",
            "end_at": "2026-08-12T10:00:00+08:00",
            "kind": "focus",
        },
    )["result"]["structuredContent"]
    applied = client.post(
        f"/api/v1/assistant/proposals/{proposal['proposal_id']}/apply",
        headers=_write_headers(owner_headers, "assistant-apply-cal-1"),
    )
    assert applied.status_code == 200
    assert applied.json()["result_ref"].startswith("calendar:")
    calendar = client.get(
        "/api/v1/workspaces/ws_default/calendar", headers=owner_headers
    ).json()
    assert calendar["total"] == 1
    assert calendar["items"][0]["title"] == "季度经营复盘"


def test_task_proposal_requires_existing_member_on_apply(
    client: TestClient,
    owner_headers: dict[str, str],
    agent_headers: dict[str, str],
) -> None:
    proposal = _call(
        client,
        agent_headers,
        "propose_task",
        {
            "title": "输出华东区供应商年审报告",
            "purpose": "确保供应商白名单可信。",
            "objective": "9 月中旬前给出年审结论。",
            "strategy": "先汇总资质材料，再逐家复核。",
            "acceptance_criteria": ["覆盖全部 3 家供应商", "结论有书面依据"],
            "assignee_member_id": "member_missing",
        },
    )["result"]["structuredContent"]
    assert proposal["impact"]["requires_next"] == ["承办人对齐确认"]

    applied = client.post(
        f"/api/v1/assistant/proposals/{proposal['proposal_id']}/apply",
        headers=_write_headers(owner_headers, "assistant-apply-task-1"),
    )
    # The assignee does not exist; the workspace write fails closed and the
    # proposal stays pending for a corrected confirm.
    assert applied.status_code in (404, 422)
    still_pending = client.get(
        "/api/v1/assistant/proposals", headers=owner_headers
    ).json()
    assert any(
        item["proposal_id"] == proposal["proposal_id"]
        and item["status"] == "pending"
        for item in still_pending["items"]
    )
