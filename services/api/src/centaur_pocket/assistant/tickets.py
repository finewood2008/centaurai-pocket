"""Cloud authorization tickets and the assistant call log (§3.4).

云端逐次授权：票据一次性、有效期固定 5 分钟、逐条列出要送出的内容类别。
联系人方式是硬约束——scope 里出现 contacts 直接拒绝，界面上它也没有勾选框。
调用日志按次追加，供「我的」页展示真实运行状态（最近调用、总次数）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from ..database import Database
from ..service import PocketError, json_loads, new_id, utc_now

TICKET_TTL_SECONDS = 300

ALLOWED_CATEGORIES = (
    "tasks",
    "schedule",
    "memos",
    "mail_subjects",
    "documents",
)

# 永不出盒：不是"默认不勾选"，而是根本不存在这个选项
FORBIDDEN_CATEGORIES = ("contacts",)


def validate_scope(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PocketError(422, "scope 必须是对象")
    extra = set(value) - {"items", "note"}
    if extra:
        raise PocketError(422, "scope 含未知字段")
    items = value.get("items")
    if not isinstance(items, list) or not items or len(items) > 20:
        raise PocketError(422, "scope.items 必须是 1-20 项的数组")
    normalized_items: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise PocketError(422, f"scope.items[{index}] 必须是对象")
        if set(item) - {"category", "count", "include_body"}:
            raise PocketError(422, f"scope.items[{index}] 含未知字段")
        category = item.get("category")
        if category in FORBIDDEN_CATEGORIES:
            raise PocketError(403, "联系人方式永不发送，不能出现在授权范围里")
        if category not in ALLOWED_CATEGORIES:
            raise PocketError(422, f"scope.items[{index}].category 无效")
        count = item.get("count", 0)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise PocketError(422, f"scope.items[{index}].count 必须是非负整数")
        include_body = item.get("include_body", False)
        if not isinstance(include_body, bool):
            raise PocketError(422, f"scope.items[{index}].include_body 必须是布尔值")
        normalized_items.append(
            {"category": category, "count": count, "include_body": include_body}
        )
    normalized: dict[str, Any] = {"items": normalized_items}
    note = value.get("note")
    if note is not None:
        if not isinstance(note, str) or len(note) > 500:
            raise PocketError(422, "scope.note 必须是不超过 500 字的字符串")
        normalized["note"] = note.strip()
    return normalized


class CloudTicketStore:
    """一次性云端授权票据，SQLite 落库。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def initialize(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS assistant_cloud_tickets (
                    id TEXT PRIMARY KEY,
                    scope_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS assistant_calls (
                    id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    rounds INTEGER NOT NULL,
                    tool_calls INTEGER NOT NULL,
                    retrieval_count INTEGER NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    stopped TEXT NOT NULL,
                    ticket_id TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _stamp(moment: datetime) -> str:
        return moment.isoformat(timespec="seconds").replace("+00:00", "Z")

    def issue(self, scope: Any) -> dict[str, Any]:
        normalized = validate_scope(scope)
        now = self._now()
        ticket = {
            "ticket_id": new_id("tkt"),
            "scope": normalized,
            "created_at": self._stamp(now),
            "expires_at": self._stamp(now + timedelta(seconds=TICKET_TTL_SECONDS)),
            "ttl_seconds": TICKET_TTL_SECONDS,
        }
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO assistant_cloud_tickets (
                    id, scope_json, created_at, expires_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    ticket["ticket_id"],
                    json.dumps(normalized, ensure_ascii=False),
                    ticket["created_at"],
                    ticket["expires_at"],
                ),
            )
        return ticket

    def consume(self, ticket_id: str) -> dict[str, Any]:
        """一次性核销：成功返回 scope；过期、已用或不存在都拒绝。"""

        now_stamp = self._stamp(self._now())
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM assistant_cloud_tickets WHERE id = ?",
                (ticket_id,),
            ).fetchone()
            if row is None:
                raise PocketError(403, "授权票据不存在")
            if row["used_at"] is not None:
                raise PocketError(403, "授权票据已使用过，票据是一次性的")
            if row["expires_at"] <= now_stamp:
                raise PocketError(403, "授权票据已过期（有效期 5 分钟）")
            connection.execute(
                "UPDATE assistant_cloud_tickets SET used_at = ? WHERE id = ?",
                (now_stamp, ticket_id),
            )
            return {
                "ticket_id": row["id"],
                "scope": json_loads(row["scope_json"], {}),
                "expires_at": row["expires_at"],
            }

    def active(self) -> dict[str, Any]:
        now = self._now()
        now_stamp = self._stamp(now)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM assistant_cloud_tickets
                WHERE used_at IS NULL AND expires_at > ?
                ORDER BY expires_at
                """,
                (now_stamp,),
            ).fetchall()
        items = []
        for row in rows:
            expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
            items.append(
                {
                    "ticket_id": row["id"],
                    "scope": json_loads(row["scope_json"], {}),
                    "created_at": row["created_at"],
                    "expires_at": row["expires_at"],
                    "remaining_seconds": max(0, int((expires - now).total_seconds())),
                }
            )
        return {"items": items, "total": len(items)}

    def record_call(
        self,
        *,
        channel: str,
        provider: str,
        model: str,
        rounds: int,
        tool_calls: int,
        retrieval_count: int,
        duration_ms: int,
        stopped: str,
        ticket_id: str | None,
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO assistant_calls (
                    id, channel, provider, model, rounds, tool_calls,
                    retrieval_count, duration_ms, stopped, ticket_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("acall"),
                    channel,
                    provider,
                    model,
                    rounds,
                    tool_calls,
                    retrieval_count,
                    duration_ms,
                    stopped,
                    ticket_id,
                    utc_now(),
                ),
            )

    def stats(self) -> dict[str, Any]:
        with self.database.connect() as connection:
            totals = connection.execute(
                """
                SELECT COUNT(*) AS calls_total,
                       COALESCE(SUM(tool_calls), 0) AS tool_calls_total
                FROM assistant_calls
                """
            ).fetchone()
            last = connection.execute(
                "SELECT * FROM assistant_calls ORDER BY created_at DESC, id DESC LIMIT 1"
            ).fetchone()
        result: dict[str, Any] = {
            "calls_total": int(totals["calls_total"]),
            "tool_calls_total": int(totals["tool_calls_total"]),
            "last_call": None,
        }
        if last is not None:
            result["last_call"] = {
                "channel": last["channel"],
                "provider": last["provider"],
                "model": last["model"],
                "rounds": last["rounds"],
                "duration_ms": last["duration_ms"],
                "stopped": last["stopped"],
                "at": last["created_at"],
            }
        return result
