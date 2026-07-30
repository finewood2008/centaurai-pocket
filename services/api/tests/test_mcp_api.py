from __future__ import annotations

from fastapi.testclient import TestClient

from centaur_pocket.mcp import PROTOCOL_VERSION


def _request(method: str, params: dict | None = None) -> dict:
    message: dict = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
    }
    if params is not None:
        message["params"] = params
    return message


def test_mcp_requires_agent_and_negotiates_protocol(
    client: TestClient,
    owner_headers: dict[str, str],
    agent_headers: dict[str, str],
) -> None:
    initialize = _request(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "pocket-test", "version": "1.0.0"},
        },
    )

    assert client.post("/api/v1/mcp", json=initialize).status_code == 401
    assert (
        client.post(
            "/api/v1/mcp",
            headers=owner_headers,
            json=initialize,
        ).status_code
        == 401
    )

    response = client.post(
        "/api/v1/mcp",
        headers=agent_headers,
        json=initialize,
    )
    assert response.status_code == 200
    assert response.headers["mcp-protocol-version"] == PROTOCOL_VERSION
    assert response.json()["result"]["protocolVersion"] == PROTOCOL_VERSION

    unsupported = client.post(
        "/api/v1/mcp",
        headers={
            **agent_headers,
            "MCP-Protocol-Version": "2024-11-05",
        },
        json=_request("ping"),
    )
    assert unsupported.status_code == 400

    missing_version = client.post(
        "/api/v1/mcp",
        headers=agent_headers,
        json=_request("ping"),
    )
    assert missing_version.status_code == 400

    untrusted_origin = client.post(
        "/api/v1/mcp",
        headers={
            **agent_headers,
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Origin": "https://evil.example",
        },
        json=_request("ping"),
    )
    assert untrusted_origin.status_code == 403


def test_mcp_lists_and_calls_ready_only_tool(
    client: TestClient,
    owner_headers: dict[str, str],
    agent_headers: dict[str, str],
) -> None:
    captured = client.post(
        "/api/v1/captures",
        headers=owner_headers,
        json={
            "title": "私人旅行计划",
            "text": "九月去成都，预订靠近天府广场的酒店。",
            "idempotency_key": "mcp-capture",
        },
    ).json()

    listed = client.post(
        "/api/v1/mcp",
        headers={
            **agent_headers,
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        },
        json=_request("tools/list"),
    )
    tool = listed.json()["result"]["tools"][0]
    assert tool["name"] == "knowledge_retrieve"
    assert "dataset_ids" not in tool["inputSchema"]["properties"]

    call = _request(
        "tools/call",
        {
            "name": "knowledge_retrieve",
            "arguments": {"query": "天府广场", "limit": 5},
        },
    )
    hidden = client.post(
        "/api/v1/mcp",
        headers={
            **agent_headers,
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        },
        json=call,
    ).json()
    assert hidden["result"]["structuredContent"]["results"] == []

    applied = client.post(
        f"/api/v1/governance/tasks/{captured['task_id']}/apply",
        headers=owner_headers,
    )
    assert applied.status_code == 200

    visible = client.post(
        "/api/v1/mcp",
        headers={
            **agent_headers,
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        },
        json=call,
    ).json()
    results = visible["result"]["structuredContent"]["results"]
    assert results[0]["title"] == "私人旅行计划"
    assert visible["result"]["structuredContent"]["visibility"] == "ready_only"


def test_mcp_notification_returns_accepted(
    client: TestClient,
    agent_headers: dict[str, str],
) -> None:
    response = client.post(
        "/api/v1/mcp",
        headers={
            **agent_headers,
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        },
        json={
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        },
    )
    assert response.status_code == 202
    assert response.content == b""
