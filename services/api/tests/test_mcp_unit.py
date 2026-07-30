from __future__ import annotations

import json
from typing import Any

import pytest

from centaur_pocket.mcp import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    TOOL_NAME,
    MCPServer,
)


def request(
    method: str,
    *,
    params: dict[str, Any] | None = None,
    request_id: str | int = 1,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
    }
    if params is not None:
        message["params"] = params
    return message


def search_result(query: str) -> dict[str, Any]:
    return {
        "query": query,
        "results": [
            {
                "item_id": "item_1",
                "title": "家庭保险",
                "snippet": "医疗保险额度",
                "source": "家庭资料/insurance.md",
                "score": 0.98,
                "category": "document",
                "tags": ["保险"],
                "updated_at": "2026-07-30T00:00:00Z",
            }
        ],
        "count": 1,
        "visibility": "ready_only",
    }


@pytest.fixture
def server() -> MCPServer:
    def search(
        *,
        query: str,
        limit: int,
        filters: dict[str, Any],
    ) -> dict[str, Any]:
        del limit, filters
        return search_result(query)

    return MCPServer(search)


def test_initialize_negotiates_2025_06_18(server: MCPServer) -> None:
    response = server.handle(
        request(
            "initialize",
            params={
                "protocolVersion": "2025-03-26",
                "capabilities": {"roots": {"listChanged": True}},
                "clientInfo": {
                    "name": "unit-client",
                    "title": "Unit Client",
                    "version": "1.0.0",
                },
            },
            request_id="init-1",
        )
    )

    assert response is not None
    assert response["id"] == "init-1"
    result = response["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "centaurai-pocket-mcp"


def test_ping_and_initialized_notification(server: MCPServer) -> None:
    assert server.handle(request("ping", request_id=2)) == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {},
    }
    assert (
        server.handle(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }
        )
        is None
    )


def test_tools_list_exposes_only_dataset_free_retrieval(
    server: MCPServer,
) -> None:
    response = server.handle(request("tools/list"))
    assert response is not None
    tools = response["result"]["tools"]
    assert len(tools) == 1
    tool = tools[0]
    assert tool["name"] == TOOL_NAME
    assert set(tool["inputSchema"]["properties"]) == {
        "query",
        "limit",
        "filters",
    }
    assert "dataset_ids" not in tool["inputSchema"]["properties"]
    assert tool["outputSchema"]["required"] == [
        "query",
        "results",
        "count",
        "visibility",
    ]
    assert tool["annotations"]["readOnlyHint"] is True


def test_tools_call_normalizes_and_forwards_arguments() -> None:
    calls: list[dict[str, Any]] = []

    def search(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return search_result(kwargs["query"])

    server = MCPServer(search)
    response = server.handle(
        request(
            "tools/call",
            params={
                "name": TOOL_NAME,
                "arguments": {
                    "query": "  医疗保险  ",
                    "limit": 5,
                    "filters": {
                        "tags": ["家庭", "保险"],
                        "category": " document ",
                    },
                },
            },
        )
    )

    assert calls == [
        {
            "query": "医疗保险",
            "limit": 5,
            "filters": {
                "tags": ["家庭", "保险"],
                "category": "document",
            },
        }
    ]
    assert response is not None
    result = response["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert result["structuredContent"]["results"][0]["title"] == "家庭保险"


def test_tools_call_uses_defaults() -> None:
    calls: list[dict[str, Any]] = []

    def search(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return search_result(kwargs["query"])

    response = MCPServer(search).handle(
        request(
            "tools/call",
            params={
                "name": TOOL_NAME,
                "arguments": {"query": "ready data"},
            },
        )
    )
    assert response is not None
    assert calls[0]["limit"] == 8
    assert calls[0]["filters"] == {"tags": [], "category": None}


@pytest.mark.parametrize(
    ("message", "expected_code"),
    [
        ([], INVALID_REQUEST),
        ({"jsonrpc": "1.0", "id": 1, "method": "ping"}, INVALID_REQUEST),
        (
            {"jsonrpc": "2.0", "id": True, "method": "ping"},
            INVALID_REQUEST,
        ),
        (
            {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []},
            INVALID_PARAMS,
        ),
        (request("not/a/method"), METHOD_NOT_FOUND),
        (
            request(
                "initialize",
                params={
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                },
            ),
            INVALID_PARAMS,
        ),
        (
            request(
                "tools/call",
                params={
                    "name": TOOL_NAME,
                    "arguments": {"query": "", "limit": 8},
                },
            ),
            INVALID_PARAMS,
        ),
        (
            request(
                "tools/call",
                params={
                    "name": TOOL_NAME,
                    "arguments": {"query": "x", "limit": True},
                },
            ),
            INVALID_PARAMS,
        ),
        (
            request(
                "tools/call",
                params={
                    "name": TOOL_NAME,
                    "arguments": {"query": "x", "dataset_ids": ["legacy"]},
                },
            ),
            INVALID_PARAMS,
        ),
        (
            request(
                "tools/call",
                params={"name": "unknown", "arguments": {"query": "x"}},
            ),
            INVALID_PARAMS,
        ),
    ],
)
def test_json_rpc_and_parameter_errors(
    server: MCPServer,
    message: Any,
    expected_code: int,
) -> None:
    response = server.handle(message)
    assert response is not None
    assert response["error"]["code"] == expected_code
    assert "result" not in response


@pytest.mark.parametrize(
    "body",
    [
        b"{",
        b"\xff",
        b'{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}',
        b'{"jsonrpc":"2.0","id":NaN,"method":"ping"}',
    ],
)
def test_parse_errors_are_strict(server: MCPServer, body: bytes) -> None:
    response = server.handle_json(body)
    assert response is not None
    assert response == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": PARSE_ERROR, "message": "Parse error"},
    }


def test_unknown_notification_never_gets_a_response(server: MCPServer) -> None:
    assert server.handle({"jsonrpc": "2.0", "method": "unknown/notification"}) is None


def test_callback_failure_is_a_tool_error() -> None:
    def failing_search(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("search unavailable")

    response = MCPServer(failing_search).handle(
        request(
            "tools/call",
            params={"name": TOOL_NAME, "arguments": {"query": "anything"}},
        )
    )
    assert response is not None
    assert response["result"]["isError"] is True
    assert "search unavailable" in response["result"]["content"][0]["text"]


def test_invalid_callback_result_is_an_internal_error() -> None:
    def invalid_search(**_kwargs: Any) -> dict[str, Any]:
        return {"results": []}

    response = MCPServer(invalid_search).handle(
        request(
            "tools/call",
            params={"name": TOOL_NAME, "arguments": {"query": "anything"}},
        )
    )
    assert response is not None
    assert response["error"]["code"] == INTERNAL_ERROR
    assert response["error"]["message"] == "Internal error"
