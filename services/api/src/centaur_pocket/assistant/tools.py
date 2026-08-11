"""Assistant MCP tool surface (§3.3): read-only tier plus proposal tier.

The read-only tier needs no confirmation and only projects governed state.
The proposal tier writes exclusively into :class:`ProposalStore` — every tool
here returns the unified proposal structure and nothing else. There is no
third tier: capabilities the agent must never have are absent, not guarded.
"""

from __future__ import annotations

from typing import Any

from ..mcp import MCPTool, ToolArgumentError
from ..service import PocketError
from .proposals import ProposalStore, validate_evidence, validate_provenance

WORKSPACE_ID = "ws_default"

_READ_ONLY_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

# Proposal tools are idempotent in effect (each call parks one inert pending
# row) and destructive of nothing: they never touch business data.
_PROPOSAL_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}

_EVIDENCE_SCHEMA = {
    "type": "array",
    "maxItems": 20,
    "default": [],
    "items": {
        "type": "object",
        "properties": {
            "source": {"type": "string", "minLength": 1, "maxLength": 200},
            "ref": {"type": "string", "maxLength": 500},
            "at": {"type": "string", "maxLength": 500},
            "excerpt": {"type": "string", "minLength": 1, "maxLength": 2000},
            "trust": {"enum": ["verified", "unverified", "governed"]},
        },
        "required": ["source", "excerpt"],
        "additionalProperties": False,
    },
}

_PROVENANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "channel": {"enum": ["local", "cloud"], "default": "local"},
        "provider": {"type": "string", "maxLength": 200},
        "model": {"type": "string", "maxLength": 200},
        "retrieval_count": {"type": "integer", "minimum": 0},
        "tool_rounds": {"type": "integer", "minimum": 0},
        "duration_ms": {"type": "integer", "minimum": 0},
        "ticket_id": {"type": "string", "maxLength": 200},
    },
    "additionalProperties": False,
}


def _require_string(
    arguments: dict[str, Any],
    key: str,
    *,
    maximum: int,
    required: bool = True,
) -> str | None:
    value = arguments.get(key)
    if value is None:
        if required:
            raise ToolArgumentError(f"arguments.{key} is required")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ToolArgumentError(f"arguments.{key} must be a non-empty string")
    if len(value) > maximum:
        raise ToolArgumentError(f"arguments.{key} must be at most {maximum} characters")
    return value.strip()


def _require_limit(arguments: dict[str, Any], default: int = 20, maximum: int = 50) -> int:
    value = arguments.get("limit", default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolArgumentError("arguments.limit must be an integer")
    if not 1 <= value <= maximum:
        raise ToolArgumentError(f"arguments.limit must be between 1 and {maximum}")
    return value


def _reject_unknown(arguments: dict[str, Any], allowed: set[str]) -> None:
    extra = set(arguments) - allowed
    if extra:
        raise ToolArgumentError(f"unknown argument(s): {', '.join(sorted(extra))}")


def _proposal_inputs(
    arguments: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        evidence = validate_evidence(arguments.get("evidence"))
        provenance = validate_provenance(arguments.get("provenance"))
    except PocketError as error:
        raise ToolArgumentError(error.detail) from error
    return evidence, provenance


def _definition(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
    *,
    read_only: bool,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "annotations": dict(
            _READ_ONLY_ANNOTATIONS if read_only else _PROPOSAL_ANNOTATIONS
        ),
    }


def build_assistant_tools(
    workspace_service: Any,
    pocket_service: Any,
    mail_service: Any,
    proposals: ProposalStore,
) -> list[MCPTool]:
    """Assemble the full non-knowledge tool surface."""

    def _task_brief(task: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": task["id"],
            "title": task["title"],
            "stage": task["stage"],
            "health": task["health"],
            "due_at": task.get("due_at"),
            "assignee_label": task.get("assignee_label"),
            "version": task["version"],
        }

    def today_brief(arguments: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown(arguments, set())
        tasks = workspace_service.list_tasks(WORKSPACE_ID)["items"]
        calendar = workspace_service.list_calendar(WORKSPACE_ID)["items"]
        attention = workspace_service.task_attention(WORKSPACE_ID)
        governance = pocket_service.dashboard()
        return {
            "tasks_total": len(tasks),
            "tasks_active": sum(1 for task in tasks if task["stage"] == "active"),
            "schedule_scheduled": sum(
                1 for entry in calendar if entry["status"] == "scheduled"
            ),
            "attention_total": attention.get("total", 0),
            "governance_pending": governance.get("pending_tasks", 0),
            "generated_at": attention.get("generated_at"),
        }

    def list_tasks(arguments: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown(arguments, {"limit"})
        limit = _require_limit(arguments)
        items = workspace_service.list_tasks(WORKSPACE_ID)["items"][:limit]
        return {"items": [_task_brief(task) for task in items], "count": len(items)}

    def task_attention(arguments: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown(arguments, set())
        return workspace_service.task_attention(WORKSPACE_ID)

    def list_schedule(arguments: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown(arguments, {"limit"})
        limit = _require_limit(arguments)
        items = workspace_service.list_calendar(WORKSPACE_ID)["items"][:limit]
        return {
            "items": [
                {
                    "id": entry["id"],
                    "title": entry["title"],
                    "start_at": entry["start_at"],
                    "end_at": entry["end_at"],
                    "status": entry["status"],
                    "kind": entry["kind"],
                }
                for entry in items
            ],
            "count": len(items),
        }

    def list_memos(arguments: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown(arguments, {"limit"})
        limit = _require_limit(arguments)
        items = workspace_service.list_memos(WORKSPACE_ID)["items"][:limit]
        return {
            "items": [
                {
                    "id": memo["id"],
                    "title": memo["title"],
                    "domain": memo["domain"],
                    "urgency": memo["urgency"],
                    "created_at": memo["created_at"],
                }
                for memo in items
            ],
            "count": len(items),
        }

    def mail_summary(arguments: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown(arguments, set())
        accounts = mail_service.list_accounts()["items"]
        return {
            "accounts": [
                {
                    "id": account["id"],
                    "label": account.get("label") or account.get("account_label"),
                    "status": account.get("status"),
                }
                for account in accounts
            ],
            "count": len(accounts),
        }

    def propose_memo(arguments: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown(
            arguments,
            {"title", "content", "domain", "urgency", "evidence", "provenance"},
        )
        fields: dict[str, Any] = {
            "title": _require_string(arguments, "title", maximum=500),
            "content": _require_string(arguments, "content", maximum=200_000),
        }
        domain = arguments.get("domain", "work")
        if domain not in {"work", "personal"}:
            raise ToolArgumentError("arguments.domain must be work or personal")
        fields["domain"] = domain
        urgency = arguments.get("urgency", "normal")
        if urgency not in {"low", "normal", "high", "critical"}:
            raise ToolArgumentError(
                "arguments.urgency must be low, normal, high or critical"
            )
        fields["urgency"] = urgency
        evidence, provenance = _proposal_inputs(arguments)
        return proposals.create(
            "memo", fields, evidence=evidence, provenance=provenance
        )

    def propose_task(arguments: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown(
            arguments,
            {
                "title",
                "purpose",
                "objective",
                "strategy",
                "acceptance_criteria",
                "assignee_member_id",
                "due_at",
                "start_at",
                "evidence",
                "provenance",
            },
        )
        fields: dict[str, Any] = {
            "title": _require_string(arguments, "title", maximum=500),
            "purpose": _require_string(arguments, "purpose", maximum=2000),
            "objective": _require_string(arguments, "objective", maximum=2000),
            "strategy": _require_string(arguments, "strategy", maximum=200_000),
            "assignee_member_id": _require_string(
                arguments, "assignee_member_id", maximum=100
            ),
        }
        criteria = arguments.get("acceptance_criteria")
        if (
            not isinstance(criteria, list)
            or not criteria
            or len(criteria) > 100
            or any(not isinstance(item, str) or not item.strip() for item in criteria)
        ):
            raise ToolArgumentError(
                "arguments.acceptance_criteria must be a non-empty string array"
            )
        fields["acceptance_criteria"] = [item.strip() for item in criteria]
        for key in ("due_at", "start_at"):
            value = _require_string(arguments, key, maximum=64, required=False)
            if value is not None:
                fields[key] = value
        evidence, provenance = _proposal_inputs(arguments)
        return proposals.create(
            "task", fields, evidence=evidence, provenance=provenance
        )

    def propose_calendar(arguments: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown(
            arguments,
            {
                "title",
                "description",
                "start_at",
                "end_at",
                "kind",
                "domain",
                "evidence",
                "provenance",
            },
        )
        fields: dict[str, Any] = {
            "title": _require_string(arguments, "title", maximum=500),
            "start_at": _require_string(arguments, "start_at", maximum=64),
            "end_at": _require_string(arguments, "end_at", maximum=64),
        }
        description = _require_string(
            arguments, "description", maximum=20_000, required=False
        )
        if description is not None:
            fields["description"] = description
        kind = arguments.get("kind", "focus")
        if kind not in {"focus", "meeting", "reminder"}:
            raise ToolArgumentError("arguments.kind must be focus, meeting or reminder")
        fields["kind"] = kind
        domain = arguments.get("domain", "work")
        if domain not in {"work", "personal"}:
            raise ToolArgumentError("arguments.domain must be work or personal")
        fields["domain"] = domain
        evidence, provenance = _proposal_inputs(arguments)
        return proposals.create(
            "calendar", fields, evidence=evidence, provenance=provenance
        )

    def propose_task_change(arguments: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown(
            arguments,
            {"task_id", "change_type", "patch", "reason", "evidence", "provenance"},
        )
        task_id = _require_string(arguments, "task_id", maximum=100)
        change_type = arguments.get("change_type")
        if change_type not in {"assignee", "due_at", "acceptance_criteria"}:
            raise ToolArgumentError(
                "arguments.change_type must be assignee, due_at or acceptance_criteria"
            )
        patch = arguments.get("patch")
        if not isinstance(patch, dict) or not patch:
            raise ToolArgumentError("arguments.patch must be a non-empty object")
        reason = _require_string(arguments, "reason", maximum=2000)
        tasks = workspace_service.list_tasks(WORKSPACE_ID)["items"]
        task = next((item for item in tasks if item["id"] == task_id), None)
        if task is None:
            raise ToolArgumentError("arguments.task_id does not match a task")
        current = {
            "assignee_member_id": task.get("assignee_member_id"),
            "due_at": task.get("due_at"),
            "acceptance_criteria": task.get("acceptance_criteria", []),
            "version": task["version"],
        }
        fields = {
            "task_id": task_id,
            "task_title": task["title"],
            "change_type": change_type,
            "patch": patch,
            "reason": reason,
        }
        evidence, provenance = _proposal_inputs(arguments)
        return proposals.create(
            "task_change",
            fields,
            current=current,
            evidence=evidence,
            provenance=provenance,
        )

    def propose_mail_reply(arguments: dict[str, Any]) -> dict[str, Any]:
        _reject_unknown(
            arguments,
            {"message_id", "body_text", "summary", "evidence", "provenance"},
        )
        fields = {
            "message_id": _require_string(arguments, "message_id", maximum=200),
            "body_text": _require_string(arguments, "body_text", maximum=100_000),
        }
        summary = _require_string(arguments, "summary", maximum=500, required=False)
        if summary is not None:
            fields["summary"] = summary
        evidence, provenance = _proposal_inputs(arguments)
        return proposals.create(
            "mail_reply", fields, evidence=evidence, provenance=provenance
        )

    limit_property = {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}
    proposal_properties = {
        "evidence": _EVIDENCE_SCHEMA,
        "provenance": _PROVENANCE_SCHEMA,
    }

    return [
        MCPTool(
            _definition(
                "today_brief",
                "Read-only counts for today: tasks, schedule, attention and pending governance.",
                {},
                [],
                read_only=True,
            ),
            today_brief,
        ),
        MCPTool(
            _definition(
                "list_tasks",
                "Read-only task list: id, title, stage, health, due date, assignee.",
                {"limit": limit_property},
                [],
                read_only=True,
            ),
            list_tasks,
        ),
        MCPTool(
            _definition(
                "task_attention",
                "Read-only server-side task attention warnings.",
                {},
                [],
                read_only=True,
            ),
            task_attention,
        ),
        MCPTool(
            _definition(
                "list_schedule",
                "Read-only calendar entries: id, title, start, end, status, kind.",
                {"limit": limit_property},
                [],
                read_only=True,
            ),
            list_schedule,
        ),
        MCPTool(
            _definition(
                "list_memos",
                "Read-only memo list: id, title, domain, urgency, created time.",
                {"limit": limit_property},
                [],
                read_only=True,
            ),
            list_memos,
        ),
        MCPTool(
            _definition(
                "mail_summary",
                "Read-only mail account summary; never returns message bodies.",
                {},
                [],
                read_only=True,
            ),
            mail_summary,
        ),
        MCPTool(
            _definition(
                "propose_memo",
                "Park a memo proposal in the owner's confirmation queue. Never writes business data.",
                {
                    "title": {"type": "string", "minLength": 1, "maxLength": 500},
                    "content": {"type": "string", "minLength": 1, "maxLength": 200_000},
                    "domain": {"enum": ["work", "personal"], "default": "work"},
                    "urgency": {"enum": ["low", "normal", "high", "critical"], "default": "normal"},
                    **proposal_properties,
                },
                ["title", "content"],
                read_only=False,
            ),
            propose_memo,
        ),
        MCPTool(
            _definition(
                "propose_task",
                "Park a fully-aligned task proposal in the owner's confirmation queue.",
                {
                    "title": {"type": "string", "minLength": 1, "maxLength": 500},
                    "purpose": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "objective": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "strategy": {"type": "string", "minLength": 1, "maxLength": 200_000},
                    "acceptance_criteria": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                        "maxItems": 100,
                    },
                    "assignee_member_id": {"type": "string", "minLength": 1, "maxLength": 100},
                    "due_at": {"type": "string", "maxLength": 64},
                    "start_at": {"type": "string", "maxLength": 64},
                    **proposal_properties,
                },
                [
                    "title",
                    "purpose",
                    "objective",
                    "strategy",
                    "acceptance_criteria",
                    "assignee_member_id",
                ],
                read_only=False,
            ),
            propose_task,
        ),
        MCPTool(
            _definition(
                "propose_calendar",
                "Park a calendar-entry proposal in the owner's confirmation queue.",
                {
                    "title": {"type": "string", "minLength": 1, "maxLength": 500},
                    "description": {"type": "string", "maxLength": 20_000},
                    "start_at": {"type": "string", "minLength": 1, "maxLength": 64},
                    "end_at": {"type": "string", "minLength": 1, "maxLength": 64},
                    "kind": {"enum": ["focus", "meeting", "reminder"], "default": "focus"},
                    "domain": {"enum": ["work", "personal"], "default": "work"},
                    **proposal_properties,
                },
                ["title", "start_at", "end_at"],
                read_only=False,
            ),
            propose_calendar,
        ),
        MCPTool(
            _definition(
                "propose_task_change",
                "Park a task-change proposal (assignee / due date / acceptance criteria) with the current values for diffing.",
                {
                    "task_id": {"type": "string", "minLength": 1, "maxLength": 100},
                    "change_type": {"enum": ["assignee", "due_at", "acceptance_criteria"]},
                    "patch": {"type": "object"},
                    "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
                    **proposal_properties,
                },
                ["task_id", "change_type", "patch", "reason"],
                read_only=False,
            ),
            propose_task_change,
        ),
        MCPTool(
            _definition(
                "propose_mail_reply",
                "Park a mail reply draft proposal. Sending mail is not part of this tool surface.",
                {
                    "message_id": {"type": "string", "minLength": 1, "maxLength": 200},
                    "body_text": {"type": "string", "minLength": 1, "maxLength": 100_000},
                    "summary": {"type": "string", "maxLength": 500},
                    **proposal_properties,
                },
                ["message_id", "body_text"],
                read_only=False,
            ),
            propose_mail_reply,
        ),
    ]
