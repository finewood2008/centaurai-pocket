"""Minimal MCP 2025-06-18 JSON-RPC dispatcher.

The protocol layer is intentionally independent from FastAPI and the database.
Callers provide a read-only ``search`` callback and may expose ``handle_json``
through any HTTP transport they choose.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
TOOL_NAME = "knowledge_retrieve"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

SearchCallback = Callable[..., Mapping[str, Any]]
JSONRPCResponse = dict[str, Any]


KNOWLEDGE_RETRIEVE_TOOL: dict[str, Any] = {
    "name": TOOL_NAME,
    "title": "Retrieve governed personal knowledge",
    "description": (
        "Search the owner's governed private data. Ready documents, explicitly "
        "opted-in IM messages, and confirmed IM knowledge are visible. IM results "
        "include message-level citations; no dataset_ids argument is required."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1000,
                "description": "Natural-language or keyword search query.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 8,
                "description": "Maximum number of results.",
            },
            "filters": {
                "type": "object",
                "properties": {
                    "tags": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 64,
                        },
                        "maxItems": 30,
                        "uniqueItems": True,
                        "default": [],
                    },
                    "category": {
                        "type": ["string", "null"],
                        "minLength": 1,
                        "maxLength": 120,
                        "default": None,
                    },
                },
                "additionalProperties": False,
                "default": {},
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "results": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
            },
            "count": {"type": "integer", "minimum": 0},
            "visibility": {"type": "string"},
        },
        "required": ["query", "results", "count", "visibility"],
        "additionalProperties": True,
    },
    "annotations": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
}


class _InvalidParams(ValueError):
    """A request used valid JSON-RPC but invalid method parameters."""


class _InvalidRequest(ValueError):
    """A decoded value was not a valid MCP JSON-RPC request."""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _object_without_duplicates(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object member: {key}")
        result[key] = value
    return result


def _error(
    request_id: str | int | None,
    code: int,
    message: str,
    *,
    detail: str | None = None,
) -> JSONRPCResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if detail:
        error["data"] = {"detail": detail}
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _success(request_id: str | int, result: dict[str, Any]) -> JSONRPCResponse:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _require_object(
    value: Any,
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _InvalidParams(f"{label} must be an object")
    return value


def _validate_keys(
    value: Mapping[str, Any],
    *,
    allowed: set[str],
    required: set[str] = frozenset(),
    label: str,
) -> None:
    missing = required.difference(value)
    if missing:
        raise _InvalidParams(
            f"{label} is missing required field(s): {', '.join(sorted(missing))}"
        )
    extra = set(value).difference(allowed)
    if extra:
        raise _InvalidParams(
            f"{label} contains unsupported field(s): {', '.join(sorted(extra))}"
        )


def _validate_meta(params: Mapping[str, Any]) -> None:
    if "_meta" in params and not isinstance(params["_meta"], dict):
        raise _InvalidParams("params._meta must be an object")


def _non_empty_string(
    value: Any,
    *,
    label: str,
    maximum: int | None = None,
) -> str:
    if not isinstance(value, str):
        raise _InvalidParams(f"{label} must be a string")
    normalized = value.strip()
    if not normalized:
        raise _InvalidParams(f"{label} must not be empty")
    if maximum is not None and len(normalized) > maximum:
        raise _InvalidParams(f"{label} must contain at most {maximum} characters")
    return normalized


class MCPServer:
    """Dispatch the minimal MCP surface used by CentaurAI Pocket."""

    def __init__(
        self,
        search: SearchCallback,
        *,
        server_name: str = "centaurai-pocket-mcp",
        server_title: str = "CentaurAI Pocket MCP",
        server_version: str = "0.1.0",
    ) -> None:
        if not callable(search):
            raise TypeError("search must be callable")
        self._search = search
        self._server_info = {
            "name": _non_empty_string(server_name, label="server_name"),
            "title": _non_empty_string(server_title, label="server_title"),
            "version": _non_empty_string(server_version, label="server_version"),
        }

    def handle_json(
        self,
        body: str | bytes | bytearray,
    ) -> JSONRPCResponse | None:
        """Parse and dispatch one JSON-RPC message.

        MCP 2025-06-18 removed JSON-RPC batching, so a decoded array is an
        invalid request rather than a batch.
        """

        try:
            message = json.loads(
                body,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_object_without_duplicates,
            )
        except (UnicodeDecodeError, ValueError, TypeError):
            return _error(None, PARSE_ERROR, "Parse error")
        return self.handle(message)

    def handle(self, message: Any) -> JSONRPCResponse | None:
        """Dispatch one already-decoded JSON-RPC message."""

        try:
            request_id, is_notification, method, params = self._request(message)
        except _InvalidRequest as exc:
            return _error(None, INVALID_REQUEST, "Invalid Request", detail=str(exc))

        try:
            if not isinstance(params, dict):
                raise _InvalidParams("params must be an object")
            result = self._dispatch(method, params, is_notification)
        except _InvalidParams as exc:
            if is_notification:
                return None
            return _error(request_id, INVALID_PARAMS, "Invalid params", detail=str(exc))
        # JSON-RPC must convert unexpected dispatcher failures into -32603
        # instead of leaking server internals to a remote client.
        except Exception:  # noqa: BLE001
            if is_notification:
                return None
            return _error(request_id, INTERNAL_ERROR, "Internal error")

        if is_notification:
            return None
        if result is None:
            return _error(request_id, METHOD_NOT_FOUND, "Method not found")
        return _success(request_id, result)

    @staticmethod
    def _request(
        message: Any,
    ) -> tuple[str | int | None, bool, str, Any]:
        if not isinstance(message, dict):
            raise _InvalidRequest("message must be an object")
        extra = set(message).difference({"jsonrpc", "id", "method", "params"})
        if extra:
            raise _InvalidRequest(
                f"unsupported top-level field(s): {', '.join(sorted(extra))}"
            )
        if message.get("jsonrpc") != "2.0":
            raise _InvalidRequest("jsonrpc must equal '2.0'")
        method = message.get("method")
        if not isinstance(method, str) or not method:
            raise _InvalidRequest("method must be a non-empty string")

        is_notification = "id" not in message
        request_id = message.get("id")
        if not is_notification and (
            isinstance(request_id, bool) or not isinstance(request_id, (str, int))
        ):
            raise _InvalidRequest("id must be a string or integer")

        return request_id, is_notification, method, message.get("params", {})

    def _dispatch(
        self,
        method: str,
        params: dict[str, Any],
        is_notification: bool,
    ) -> dict[str, Any] | None:
        if method == "notifications/initialized":
            _validate_meta(params)
            if is_notification:
                return {}
            return None
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return self._ping(params)
        if method == "tools/list":
            return self._list_tools(params)
        if method == "tools/call":
            return self._call_tool(params)
        return None

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        _validate_keys(
            params,
            allowed={"protocolVersion", "capabilities", "clientInfo", "_meta"},
            required={"protocolVersion", "capabilities", "clientInfo"},
            label="initialize params",
        )
        _validate_meta(params)
        _non_empty_string(params["protocolVersion"], label="params.protocolVersion")
        capabilities = _require_object(
            params["capabilities"], label="params.capabilities"
        )
        self._validate_capabilities(capabilities)
        client_info = _require_object(params["clientInfo"], label="params.clientInfo")
        _validate_keys(
            client_info,
            allowed={"name", "title", "version"},
            required={"name", "version"},
            label="params.clientInfo",
        )
        _non_empty_string(client_info["name"], label="params.clientInfo.name")
        _non_empty_string(client_info["version"], label="params.clientInfo.version")
        if "title" in client_info:
            _non_empty_string(client_info["title"], label="params.clientInfo.title")
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": dict(self._server_info),
            "instructions": (
                "Use knowledge_retrieve to search only owner-reviewed, ready data."
            ),
        }

    @staticmethod
    def _validate_capabilities(capabilities: dict[str, Any]) -> None:
        for key in ("sampling", "elicitation"):
            if key in capabilities and not isinstance(capabilities[key], dict):
                raise _InvalidParams(f"params.capabilities.{key} must be an object")
        if "roots" in capabilities:
            roots = _require_object(
                capabilities["roots"], label="params.capabilities.roots"
            )
            _validate_keys(
                roots,
                allowed={"listChanged"},
                label="params.capabilities.roots",
            )
            if "listChanged" in roots and not isinstance(roots["listChanged"], bool):
                raise _InvalidParams(
                    "params.capabilities.roots.listChanged must be a boolean"
                )
        if "experimental" in capabilities:
            experimental = _require_object(
                capabilities["experimental"],
                label="params.capabilities.experimental",
            )
            if any(not isinstance(value, dict) for value in experimental.values()):
                raise _InvalidParams(
                    "params.capabilities.experimental values must be objects"
                )

    @staticmethod
    def _ping(params: dict[str, Any]) -> dict[str, Any]:
        _validate_keys(params, allowed={"_meta"}, label="ping params")
        _validate_meta(params)
        return {}

    @staticmethod
    def _list_tools(params: dict[str, Any]) -> dict[str, Any]:
        _validate_keys(
            params,
            allowed={"cursor", "_meta"},
            label="tools/list params",
        )
        _validate_meta(params)
        if "cursor" in params:
            _non_empty_string(params["cursor"], label="params.cursor")
            raise _InvalidParams(
                "params.cursor is not valid because this tool list has one page"
            )
        return {"tools": [copy.deepcopy(KNOWLEDGE_RETRIEVE_TOOL)]}

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        _validate_keys(
            params,
            allowed={"name", "arguments", "_meta"},
            required={"name"},
            label="tools/call params",
        )
        _validate_meta(params)
        name = _non_empty_string(params["name"], label="params.name")
        if name != TOOL_NAME:
            raise _InvalidParams(f"unknown tool: {name}")

        arguments = _require_object(
            params.get("arguments", {}), label="params.arguments"
        )
        query, limit, filters = self._knowledge_arguments(arguments)
        try:
            callback_result = self._search(
                query=query,
                limit=limit,
                filters=filters,
            )
        # Tool callbacks are an extension boundary; any provider failure is
        # represented as an MCP tool error rather than crashing the transport.
        except Exception as exc:  # noqa: BLE001
            message = str(exc).strip() or exc.__class__.__name__
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"{TOOL_NAME} failed: {message[:500]}",
                    }
                ],
                "isError": True,
            }

        if not isinstance(callback_result, Mapping):
            raise TypeError("search callback must return a mapping")
        try:
            text = json.dumps(
                dict(callback_result),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            structured = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise TypeError("search callback returned non-JSON data") from exc
        self._validate_search_result(structured)
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": structured,
            "isError": False,
        }

    @staticmethod
    def _knowledge_arguments(
        arguments: dict[str, Any],
    ) -> tuple[str, int, dict[str, Any]]:
        _validate_keys(
            arguments,
            allowed={"query", "limit", "filters"},
            required={"query"},
            label="params.arguments",
        )
        query = _non_empty_string(
            arguments["query"],
            label="params.arguments.query",
            maximum=1000,
        )

        limit = arguments.get("limit", 8)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise _InvalidParams("params.arguments.limit must be an integer")
        if not 1 <= limit <= 50:
            raise _InvalidParams("params.arguments.limit must be between 1 and 50")

        raw_filters = _require_object(
            arguments.get("filters", {}),
            label="params.arguments.filters",
        )
        _validate_keys(
            raw_filters,
            allowed={"tags", "category"},
            label="params.arguments.filters",
        )

        raw_tags = raw_filters.get("tags", [])
        if not isinstance(raw_tags, list):
            raise _InvalidParams("params.arguments.filters.tags must be an array")
        if len(raw_tags) > 30:
            raise _InvalidParams(
                "params.arguments.filters.tags must contain at most 30 items"
            )
        tags: list[str] = []
        seen: set[str] = set()
        for index, value in enumerate(raw_tags):
            tag = _non_empty_string(
                value,
                label=f"params.arguments.filters.tags[{index}]",
                maximum=64,
            )
            folded = tag.casefold()
            if folded in seen:
                raise _InvalidParams(
                    "params.arguments.filters.tags must contain unique items"
                )
            seen.add(folded)
            tags.append(tag)

        raw_category = raw_filters.get("category")
        category = (
            None
            if raw_category is None
            else _non_empty_string(
                raw_category,
                label="params.arguments.filters.category",
                maximum=120,
            )
        )
        return query, limit, {"tags": tags, "category": category}

    @staticmethod
    def _validate_search_result(value: Any) -> None:
        if not isinstance(value, dict):
            raise TypeError("search result must be an object")
        required = {"query", "results", "count", "visibility"}
        if not required.issubset(value):
            raise TypeError("search result does not match the output schema")
        if not isinstance(value["query"], str):
            raise TypeError("search result query must be a string")
        if not isinstance(value["results"], list) or any(
            not isinstance(item, dict) for item in value["results"]
        ):
            raise TypeError("search result results must be an array of objects")
        if (
            isinstance(value["count"], bool)
            or not isinstance(value["count"], int)
            or value["count"] < 0
        ):
            raise TypeError("search result count must be a non-negative integer")
        if not isinstance(value["visibility"], str):
            raise TypeError("search result visibility must be a string")


__all__ = [
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "KNOWLEDGE_RETRIEVE_TOOL",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "PROTOCOL_VERSION",
    "TOOL_NAME",
    "MCPServer",
]
