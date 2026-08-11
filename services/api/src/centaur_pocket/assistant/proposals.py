"""Owner-gated proposal queue for assistant tools (§3.1/§3.3).

Proposals share the governance-door mental model: a pending row is inert data,
`apply` is the only path into the workspace, and both decisions are terminal
and auditable via the workspace's own event log (the apply goes through the
exact same service methods the owner's manual writes use).
"""

from __future__ import annotations

import json
from typing import Any

from ..database import Database
from ..service import PocketError, json_loads, new_id, utc_now

PROPOSAL_KINDS = ("memo", "task", "calendar", "task_change", "mail_reply")

_IMPACT: dict[str, dict[str, Any]] = {
    "memo": {
        "writes": ["workspace.memo"],
        "reversible": True,
        "requires_next": [],
    },
    "task": {
        "writes": ["workspace.task"],
        "reversible": True,
        "requires_next": ["承办人对齐确认"],
    },
    "calendar": {
        "writes": ["workspace.calendar_entry"],
        "reversible": True,
        "requires_next": [],
    },
    "task_change": {
        "writes": ["workspace.task_change"],
        "reversible": True,
        "requires_next": ["变更协议双方确认"],
    },
    "mail_reply": {
        "writes": ["mail.reply_draft"],
        "reversible": True,
        "requires_next": ["主人在邮件工作台确认发送"],
    },
}

_ALLOWED_TRUST = {"verified", "unverified", "governed"}


def impact_for(kind: str) -> dict[str, Any]:
    return json.loads(json.dumps(_IMPACT[kind]))


def validate_evidence(value: Any) -> list[dict[str, Any]]:
    """Normalize an evidence list; empty is allowed but is surfaced as-is so
    the client can label the proposal 无依据 and disable one-tap confirm."""

    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 20:
        raise PocketError(422, "evidence 必须是不超过 20 项的数组")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise PocketError(422, f"evidence[{index}] 必须是对象")
        extra = set(item) - {"source", "ref", "at", "excerpt", "trust"}
        if extra:
            raise PocketError(422, f"evidence[{index}] 含未知字段")
        source = item.get("source")
        excerpt = item.get("excerpt")
        if not isinstance(source, str) or not source.strip():
            raise PocketError(422, f"evidence[{index}].source 不能为空")
        if not isinstance(excerpt, str) or not excerpt.strip():
            raise PocketError(422, f"evidence[{index}].excerpt 不能为空")
        trust = item.get("trust", "unverified")
        if trust not in _ALLOWED_TRUST:
            raise PocketError(422, f"evidence[{index}].trust 无效")
        entry: dict[str, Any] = {
            "source": source.strip()[:200],
            "excerpt": excerpt.strip()[:2000],
            "trust": trust,
        }
        for key in ("ref", "at"):
            raw = item.get(key)
            if raw is not None:
                if not isinstance(raw, str) or not raw.strip():
                    raise PocketError(422, f"evidence[{index}].{key} 必须是非空字符串")
                entry[key] = raw.strip()[:500]
        normalized.append(entry)
    return normalized


def validate_provenance(value: Any) -> dict[str, Any]:
    """Provenance is the card footer (§3.2): where the proposal came from."""

    if value is None:
        return {"channel": "local"}
    if not isinstance(value, dict):
        raise PocketError(422, "provenance 必须是对象")
    extra = set(value) - {
        "channel",
        "provider",
        "model",
        "retrieval_count",
        "tool_rounds",
        "duration_ms",
        "ticket_id",
    }
    if extra:
        raise PocketError(422, "provenance 含未知字段")
    channel = value.get("channel", "local")
    if channel not in {"local", "cloud"}:
        raise PocketError(422, "provenance.channel 只能是 local 或 cloud")
    if channel == "cloud" and not value.get("ticket_id"):
        raise PocketError(422, "云端调用必须携带授权票据 ID")
    normalized: dict[str, Any] = {"channel": channel}
    for key in ("provider", "model", "ticket_id"):
        raw = value.get(key)
        if raw is not None:
            if not isinstance(raw, str) or not raw.strip() or len(raw) > 200:
                raise PocketError(422, f"provenance.{key} 无效")
            normalized[key] = raw.strip()
    for key in ("retrieval_count", "tool_rounds", "duration_ms"):
        raw = value.get(key)
        if raw is not None:
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                raise PocketError(422, f"provenance.{key} 必须是非负整数")
            normalized[key] = raw
    return normalized


class ProposalStore:
    """SQLite-backed pending/applied/dismissed proposal rows."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def initialize(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS assistant_proposals (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    fields_json TEXT NOT NULL,
                    current_json TEXT,
                    evidence_json TEXT NOT NULL,
                    impact_json TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    decided_at TEXT,
                    result_ref TEXT
                )
                """
            )

    def create(
        self,
        kind: str,
        fields: dict[str, Any],
        *,
        current: dict[str, Any] | None = None,
        evidence: list[dict[str, Any]],
        provenance: dict[str, Any],
    ) -> dict[str, Any]:
        if kind not in PROPOSAL_KINDS:
            raise PocketError(422, f"未知提议类型：{kind}")
        proposal_id = new_id("prop")
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO assistant_proposals (
                    id, kind, fields_json, current_json, evidence_json,
                    impact_json, provenance_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    proposal_id,
                    kind,
                    json.dumps(fields, ensure_ascii=False),
                    None if current is None else json.dumps(current, ensure_ascii=False),
                    json.dumps(evidence, ensure_ascii=False),
                    json.dumps(impact_for(kind), ensure_ascii=False),
                    json.dumps(provenance, ensure_ascii=False),
                    now,
                ),
            )
        return self.get(proposal_id)

    def list(self, status: str | None = "pending") -> dict[str, Any]:
        query = "SELECT * FROM assistant_proposals"
        parameters: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            parameters.append(status)
        query += " ORDER BY created_at DESC, id DESC LIMIT 200"
        with self.database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        items = [self._to_dict(row) for row in rows]
        return {"items": items, "total": len(items)}

    def get(self, proposal_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM assistant_proposals WHERE id = ?",
                (proposal_id,),
            ).fetchone()
        if row is None:
            raise PocketError(404, "提议不存在")
        return self._to_dict(row)

    def decide(
        self,
        proposal_id: str,
        *,
        status: str,
        result_ref: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"applied", "dismissed"}:
            raise PocketError(422, "提议决定无效")
        now = utc_now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM assistant_proposals WHERE id = ?",
                (proposal_id,),
            ).fetchone()
            if row is None:
                raise PocketError(404, "提议不存在")
            if row["status"] != "pending":
                raise PocketError(409, "提议已被处理，不能重复决定")
            connection.execute(
                """
                UPDATE assistant_proposals
                SET status = ?, decided_at = ?, result_ref = ?
                WHERE id = ?
                """,
                (status, now, result_ref, proposal_id),
            )
        return self.get(proposal_id)

    @staticmethod
    def _to_dict(row: Any) -> dict[str, Any]:
        return {
            "proposal_id": row["id"],
            "kind": row["kind"],
            "fields": json_loads(row["fields_json"], {}),
            "current": json_loads(row["current_json"], {}) if row["current_json"] else {},
            "evidence": json_loads(row["evidence_json"], []),
            "impact": json_loads(row["impact_json"], {}),
            "provenance": json_loads(row["provenance_json"], {}),
            "status": row["status"],
            "created_at": row["created_at"],
            "decided_at": row["decided_at"],
            "result_ref": row["result_ref"],
        }
