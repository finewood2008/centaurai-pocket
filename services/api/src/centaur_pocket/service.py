from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import secrets
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import uuid4

from .database import Database
from .schemas import FolderSourceConfig

TEXT_EXTENSIONS = {
    ".c",
    ".csv",
    ".cpp",
    ".css",
    ".go",
    ".htm",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".jsx",
    ".json",
    ".log",
    ".md",
    ".markdown",
    ".py",
    ".rs",
    ".rst",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsv",
    ".tsx",
    ".txt",
    ".vue",
    ".xml",
    ".yaml",
    ".yml",
}


class PocketError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def utc_from_timestamp(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, tz=UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


class PocketService:
    def __init__(
        self,
        database: Database,
        *,
        owner_token: str,
        agent_token: str,
        max_file_bytes: int,
    ):
        self.database = database
        self.owner_token = owner_token
        self.agent_token = agent_token
        self.max_file_bytes = max_file_bytes

    def initialize(self) -> None:
        self.database.initialize()

    def owner_token_matches(self, candidate: str) -> bool:
        return secrets.compare_digest(candidate, self.owner_token)

    def agent_token_matches(self, candidate: str) -> bool:
        return secrets.compare_digest(candidate, self.agent_token)

    def replace_agent_token(self, token: str) -> None:
        if not token.startswith("cp_live_"):
            raise ValueError("invalid CentaurAI Pocket Agent token")
        self.agent_token = token

    # Dashboard

    def dashboard(self) -> dict[str, Any]:
        today = datetime.now(UTC).date().isoformat()
        with self.database.connect() as connection:
            item_counts = {
                row["state"]: row["count"]
                for row in connection.execute(
                    "SELECT state, COUNT(*) AS count FROM items GROUP BY state"
                ).fetchall()
            }
            total_items = sum(item_counts.values())
            pending_tasks = connection.execute(
                "SELECT COUNT(*) FROM governance_tasks WHERE status = 'pending'"
            ).fetchone()[0]
            source_rows = connection.execute(
                """
                SELECT
                    s.enabled,
                    (
                        SELECT sr.status
                        FROM sync_runs sr
                        WHERE sr.source_id = s.id
                        ORDER BY sr.started_at DESC
                        LIMIT 1
                    ) AS last_status
                FROM sources s
                """
            ).fetchall()
            healthy_sources = sum(
                1
                for row in source_rows
                if row["enabled"] and row["last_status"] == "completed"
            )
            discovered_today = connection.execute(
                """
                SELECT COALESCE(SUM(imported_count), 0)
                FROM sync_runs
                WHERE status = 'completed' AND substr(finished_at, 1, 10) = ?
                """,
                (today,),
            ).fetchone()[0]
            deduplicated_today = connection.execute(
                """
                SELECT COALESCE(SUM(duplicate_count), 0)
                FROM sync_runs
                WHERE status = 'completed' AND substr(finished_at, 1, 10) = ?
                """,
                (today,),
            ).fetchone()[0]
            processed_today = connection.execute(
                """
                SELECT COUNT(*)
                FROM governance_tasks
                WHERE status != 'pending' AND substr(resolved_at, 1, 10) = ?
                """,
                (today,),
            ).fetchone()[0]
            last_sync_at = connection.execute(
                "SELECT MAX(finished_at) FROM sync_runs WHERE status = 'completed'"
            ).fetchone()[0]
            next_task = self._next_task(connection)
            recent_activity = [
                self._activity_to_dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM activity_events
                    ORDER BY created_at DESC
                    LIMIT 10
                    """
                ).fetchall()
            ]

        ready = item_counts.get("ready", 0)
        needs_review = item_counts.get("needs_review", 0)
        quality_denominator = ready + needs_review
        quality_score = (
            round(ready * 100 / quality_denominator) if quality_denominator else 100
        )
        attention = len(source_rows) - healthy_sources
        return {
            "items": {
                "total": total_items,
                "ready": ready,
                "needs_review": needs_review,
                "inbox": item_counts.get("inbox", 0),
                "archived": item_counts.get("archived", 0),
            },
            "sources": {
                "total": len(source_rows),
                "healthy": healthy_sources,
                "attention": attention,
            },
            "sync": {
                "discovered_today": discovered_today,
                "deduplicated_today": deduplicated_today,
            },
            "pending_tasks": pending_tasks,
            "ready_items": ready,
            "total_items": total_items,
            "source_count": len(source_rows),
            "healthy_sources": healthy_sources,
            "quality_score": quality_score,
            "processed_today": processed_today,
            "last_sync_at": last_sync_at,
            "next_task": next_task,
            "recent_activity": recent_activity,
        }

    # Sources

    def create_source(
        self,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        operation = "source:create"
        with self.database.transaction() as connection:
            cached = self._idempotent_response(connection, operation, idempotency_key)
            if cached is not None:
                return cached

            normalized_config = FolderSourceConfig.model_validate(
                payload["config"]
            ).model_dump()
            self._assert_unique_folder_path(connection, normalized_config["path"])
            source_id = new_id("src")
            now = utc_now()
            connection.execute(
                """
                INSERT INTO sources(
                    id, kind, name, config_json, schedule, enabled,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    payload["kind"],
                    payload["display_name"].strip(),
                    json.dumps(normalized_config, ensure_ascii=False),
                    payload["schedule"],
                    int(payload["enabled"]),
                    now,
                    now,
                ),
            )
            self._record_activity(
                connection,
                kind="source.created",
                message=f"已添加数据源“{payload['display_name'].strip()}”",
                resource_type="source",
                resource_id=source_id,
            )
            response = self._get_source(connection, source_id)
            self._store_idempotent_response(
                connection, operation, idempotency_key, response
            )
            return response

    def list_sources(self) -> dict[str, Any]:
        with self.database.connect() as connection:
            rows = connection.execute(
                f"{self._source_select_sql()} ORDER BY s.created_at DESC"
            ).fetchall()
            items = [self._source_to_dict(row) for row in rows]
            return {"items": items, "sources": items, "total": len(items)}

    def get_source(self, source_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            return self._get_source(connection, source_id)

    def update_source(self, source_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        if not updates:
            return self.get_source(source_id)
        with self.database.transaction() as connection:
            current = self._get_source(connection, source_id)
            values: dict[str, Any] = {}
            if "display_name" in updates:
                values["name"] = updates["display_name"].strip()
            if "config" in updates:
                config = FolderSourceConfig.model_validate(
                    updates["config"]
                ).model_dump()
                self._assert_unique_folder_path(
                    connection, config["path"], exclude_source_id=source_id
                )
                values["config_json"] = json.dumps(config, ensure_ascii=False)
            if "schedule" in updates:
                values["schedule"] = updates["schedule"]
            if "enabled" in updates:
                values["enabled"] = int(updates["enabled"])
            values["updated_at"] = utc_now()
            assignments = ", ".join(f"{column} = ?" for column in values)
            connection.execute(
                f"UPDATE sources SET {assignments} WHERE id = ?",
                (*values.values(), source_id),
            )
            self._record_activity(
                connection,
                kind="source.updated",
                message=f"已更新数据源“{values.get('name', current['display_name'])}”",
                resource_type="source",
                resource_id=source_id,
            )
            return self._get_source(connection, source_id)

    def delete_source(self, source_id: str) -> None:
        with self.database.transaction() as connection:
            source = self._get_source(connection, source_id)
            linked_item_ids = [
                row["item_id"]
                for row in connection.execute(
                    """
                    SELECT DISTINCT item_id
                    FROM item_sources
                    WHERE source_id = ?
                    """,
                    (source_id,),
                ).fetchall()
            ]
            connection.execute("DELETE FROM sources WHERE id = ?", (source_id,))
            deletion_task_count = self._create_deletion_tasks(
                connection,
                item_ids=linked_item_ids,
                now=utc_now(),
            )
            self._record_activity(
                connection,
                kind="source.deleted",
                message=f"已移除数据源“{source['display_name']}”",
                resource_type="source",
                resource_id=source_id,
                details={"deletion_task_count": deletion_task_count},
            )

    def _assert_unique_folder_path(
        self,
        connection: sqlite3.Connection,
        path: str,
        *,
        exclude_source_id: str | None = None,
    ) -> None:
        rows = connection.execute(
            "SELECT id, config_json FROM sources WHERE kind = 'folder'"
        ).fetchall()
        for row in rows:
            if exclude_source_id and row["id"] == exclude_source_id:
                continue
            config = json_loads(row["config_json"], {})
            if config.get("path") == path:
                raise PocketError(409, "该文件夹已经配置为数据源")

    @staticmethod
    def _source_select_sql() -> str:
        return """
            SELECT
                s.*,
                (
                    SELECT COUNT(DISTINCT item_id)
                    FROM item_sources linked
                    WHERE linked.source_id = s.id
                ) AS item_count,
                (
                    SELECT COUNT(DISTINCT task.id)
                    FROM governance_tasks task
                    JOIN item_sources linked ON linked.item_id = task.item_id
                    WHERE linked.source_id = s.id AND task.status = 'pending'
                ) AS pending_count,
                (
                    SELECT sr.status
                    FROM sync_runs sr
                    WHERE sr.source_id = s.id
                    ORDER BY sr.started_at DESC
                    LIMIT 1
                ) AS sync_status,
                (
                    SELECT sr.error
                    FROM sync_runs sr
                    WHERE sr.source_id = s.id
                    ORDER BY sr.started_at DESC
                    LIMIT 1
                ) AS last_error
            FROM sources s
        """

    def _get_source(
        self, connection: sqlite3.Connection, source_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            f"{self._source_select_sql()} WHERE s.id = ?", (source_id,)
        ).fetchone()
        if row is None:
            raise PocketError(404, "数据源不存在")
        return self._source_to_dict(row)

    @staticmethod
    def _source_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        if not row["enabled"]:
            status = "paused"
        elif row["sync_status"] == "running":
            status = "syncing"
        elif row["sync_status"] == "failed":
            status = "error"
        elif row["sync_status"] == "completed":
            status = "healthy"
        else:
            status = "unknown"
        return {
            "id": row["id"],
            "kind": row["kind"],
            "type": row["kind"],
            "display_name": row["name"],
            "name": row["name"],
            "config": json_loads(row["config_json"], {}),
            "schedule": row["schedule"],
            "enabled": bool(row["enabled"]),
            "status": status,
            "item_count": row["item_count"],
            "pending_count": row["pending_count"],
            "last_sync_at": row["last_sync_at"],
            "last_error": row["last_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # Sync

    def sync_source(
        self,
        source_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        operation = f"source:{source_id}:sync"
        if idempotency_key:
            with self.database.connect() as connection:
                cached = self._idempotent_response(
                    connection, operation, idempotency_key
                )
                if cached is not None:
                    return cached

        source = self.get_source(source_id)
        if not source["enabled"]:
            raise PocketError(409, "数据源已暂停，启用后才能同步")

        run_id = new_id("run")
        started_at = utc_now()
        with self.database.transaction() as connection:
            running = connection.execute(
                """
                SELECT id, started_at
                FROM sync_runs
                WHERE source_id = ? AND status = 'running'
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (source_id,),
            ).fetchone()
            if running is not None:
                try:
                    running_since = datetime.fromisoformat(running["started_at"])
                except ValueError:
                    running_since = datetime.now(UTC)
                if datetime.now(UTC) - running_since < timedelta(hours=6):
                    raise PocketError(409, "该数据源已有同步任务正在运行")
                connection.execute(
                    """
                    UPDATE sync_runs
                    SET status = 'failed', finished_at = ?,
                        error = '服务重启后清理超时同步任务'
                    WHERE id = ?
                    """,
                    (started_at, running["id"]),
                )
            connection.execute(
                """
                INSERT INTO sync_runs(id, source_id, status, started_at)
                VALUES (?, ?, 'running', ?)
                """,
                (run_id, source_id, started_at),
            )

        counters = {
            "scanned_count": 0,
            "imported_count": 0,
            "duplicate_count": 0,
            "unchanged_count": 0,
            "skipped_count": 0,
            "task_count": 0,
        }
        seen_origin_uris: set[str] = set()
        error: str | None = None
        try:
            config = FolderSourceConfig.model_validate(source["config"])
            root = Path(config.path)
            if not root.is_dir():
                raise ValueError(f"文件夹不存在或不可读取：{root}")
            for file_path in self._iter_source_files(root, config):
                counters["scanned_count"] += 1
                # Retain links for discovered files even when the file is too
                # large or temporarily unreadable. Only a successful full scan
                # is allowed to prune paths that disappeared from the source.
                seen_origin_uris.add(file_path.resolve().as_uri())
                try:
                    if not self._is_supported_text_path(file_path):
                        counters["skipped_count"] += 1
                        continue
                    stat = file_path.stat()
                    if stat.st_size > self.max_file_bytes:
                        counters["skipped_count"] += 1
                        continue
                    raw = file_path.read_bytes()
                    outcome = self._ingest_file(
                        source_id=source_id,
                        source_name=source["display_name"],
                        root=root,
                        file_path=file_path,
                        raw=raw,
                        stat_size=stat.st_size,
                        modified_at=utc_from_timestamp(stat.st_mtime),
                    )
                    creates_review = outcome.endswith("_review")
                    base_outcome = (
                        outcome.removesuffix("_review")
                        if creates_review
                        else outcome
                    )
                    counters[f"{base_outcome}_count"] += 1
                    if outcome == "imported" or creates_review:
                        counters["task_count"] += 1
                except (OSError, UnicodeError):
                    counters["skipped_count"] += 1
            counters["task_count"] += self._prune_missing_source_links(
                source_id,
                seen_origin_uris,
            )
        # A sync run must always leave the "running" state. Ingestion also
        # touches SQLite and future parsers may raise errors other than the
        # filesystem/validation exceptions handled above, so finalize every
        # ordinary failure and surface it as a failed run to the API.
        except Exception as exc:  # noqa: BLE001 - every run must be finalized
            error = str(exc) or type(exc).__name__

        finished_at = utc_now()
        status = "failed" if error else "completed"
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE sync_runs
                SET status = ?, finished_at = ?,
                    scanned_count = ?, imported_count = ?,
                    duplicate_count = ?, unchanged_count = ?,
                    skipped_count = ?, task_count = ?, error = ?
                WHERE id = ?
                """,
                (
                    status,
                    finished_at,
                    counters["scanned_count"],
                    counters["imported_count"],
                    counters["duplicate_count"],
                    counters["unchanged_count"],
                    counters["skipped_count"],
                    counters["task_count"],
                    error,
                    run_id,
                ),
            )
            if error:
                connection.execute(
                    """
                    UPDATE sources
                    SET updated_at = ?
                    WHERE id = ?
                    """,
                    (finished_at, source_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE sources
                    SET last_sync_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (finished_at, finished_at, source_id),
                )
            if error:
                message = f"数据源“{source['display_name']}”同步失败：{error}"
            else:
                message = (
                    f"数据源“{source['display_name']}”同步完成，"
                    f"新增 {counters['imported_count']} 项"
                )
            self._record_activity(
                connection,
                kind=f"sync.{status}",
                message=message,
                resource_type="sync_run",
                resource_id=run_id,
                details=counters,
            )
            response = self._get_sync_run(connection, run_id)
            if not error:
                self._store_idempotent_response(
                    connection, operation, idempotency_key, response
                )
        return response

    def _prune_missing_source_links(
        self,
        source_id: str,
        seen_origin_uris: set[str],
    ) -> int:
        """Remove stale path links after one successful complete source scan.

        The content item itself is retained. This preserves personal data when
        a source file is deleted while ensuring renamed paths do not keep an
        obsolete generation artificially active. A non-archived item that
        loses its final source link gets one explicit deletion governance task.
        """

        with self.database.transaction() as connection:
            existing = connection.execute(
                """
                SELECT origin_uri, item_id
                FROM item_sources
                WHERE source_id = ?
                """,
                (source_id,),
            ).fetchall()
            missing = [
                row
                for row in existing
                if row["origin_uri"] not in seen_origin_uris
            ]
            connection.executemany(
                """
                DELETE FROM item_sources
                WHERE source_id = ? AND origin_uri = ?
                """,
                ((source_id, row["origin_uri"]) for row in missing),
            )
            return self._create_deletion_tasks(
                connection,
                item_ids=list({row["item_id"] for row in missing}),
                now=utc_now(),
            )

    def _create_deletion_tasks(
        self,
        connection: sqlite3.Connection,
        *,
        item_ids: list[str],
        now: str,
    ) -> int:
        """Ask before archiving knowledge that lost its final source."""

        created = 0
        for item_id in sorted(set(item_ids)):
            item = connection.execute(
                "SELECT * FROM items WHERE id = ?",
                (item_id,),
            ).fetchone()
            if item is None or item["state"] == "archived":
                continue
            active_link_count = connection.execute(
                "SELECT COUNT(*) FROM item_sources WHERE item_id = ?",
                (item_id,),
            ).fetchone()[0]
            if active_link_count:
                continue

            # A changed file creates a new generation that explicitly
            # supersedes the last-ready item. That review already owns the
            # decision; a second deletion card would be misleading.
            has_superseding_generation = connection.execute(
                """
                SELECT 1
                FROM items
                WHERE (
                    json_extract(metadata_json, '$.supersedes_item_id') = ?
                    OR EXISTS (
                        SELECT 1
                        FROM json_each(
                            items.metadata_json,
                            '$.supersedes_item_ids'
                        ) predecessor
                        WHERE predecessor.value = ?
                    )
                )
                  AND state != 'archived'
                LIMIT 1
                """,
                (item_id, item_id),
            ).fetchone()
            if has_superseding_generation is not None:
                continue

            pending = connection.execute(
                """
                SELECT 1
                FROM governance_tasks
                WHERE item_id = ? AND kind = 'deletion' AND status = 'pending'
                LIMIT 1
                """,
                (item_id,),
            ).fetchone()
            if pending is not None:
                continue

            # A first-time review becomes stale as soon as its only source
            # disappears. Resolve it before presenting the explicit deletion
            # decision so an old review cannot later make an orphan ready.
            stale_review_ids = [
                row["id"]
                for row in connection.execute(
                    """
                    SELECT id
                    FROM governance_tasks
                    WHERE item_id = ?
                      AND kind != 'deletion'
                      AND status = 'pending'
                    """,
                    (item_id,),
                ).fetchall()
            ]
            if stale_review_ids:
                placeholders = ",".join("?" for _ in stale_review_ids)
                connection.execute(
                    f"""
                    UPDATE governance_tasks
                    SET status = 'skipped', resolved_at = ?, updated_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    (now, now, *stale_review_ids),
                )

            metadata = json_loads(item["metadata_json"], {})
            relative_path = str(
                metadata.get("relative_path") or item["file_name"] or item["title"]
            )
            keep_effect = (
                "继续保留给 Agent"
                if item["state"] == "ready"
                else "继续保留在私人库中且不会开放给 Agent"
            )
            proposal = {
                "patch": {"state": "archived"},
                "suggestion": (
                    f"来源中已找不到此内容；接受后归档，跳过则{keep_effect}"
                ),
                "reason": f"完整同步未再发现“{relative_path}”，需要由本人判断是否保留",
                "confidence": 1.0,
                "superseded_task_ids": stale_review_ids,
            }
            task_id = new_id("task")
            connection.execute(
                """
                INSERT INTO governance_tasks(
                    id, item_id, kind, status, proposal_json,
                    created_at, updated_at
                )
                VALUES (?, ?, 'deletion', 'pending', ?, ?, ?)
                """,
                (
                    task_id,
                    item_id,
                    json.dumps(proposal, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            self._record_activity(
                connection,
                kind="item.source_missing",
                message=f"来源中已找不到“{item['title']}”，等待本人确认",
                resource_type="governance_task",
                resource_id=task_id,
                details={"item_id": item_id},
            )
            created += 1
        return created

    def due_source_ids(self) -> list[str]:
        now = datetime.now(UTC)
        result: list[str] = []
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    source.id,
                    source.schedule,
                    source.last_sync_at,
                    (
                        SELECT run.status
                        FROM sync_runs run
                        WHERE run.source_id = source.id
                        ORDER BY run.started_at DESC
                        LIMIT 1
                    ) AS latest_status,
                    (
                        SELECT run.started_at
                        FROM sync_runs run
                        WHERE run.source_id = source.id
                        ORDER BY run.started_at DESC
                        LIMIT 1
                    ) AS latest_started_at
                FROM sources source
                WHERE source.enabled = 1 AND source.schedule != 'manual'
                """
            ).fetchall()
        for row in rows:
            interval = timedelta(hours=1 if row["schedule"] == "hourly" else 24)
            latest_started_at = row["latest_started_at"]
            if row["latest_status"] == "failed" and latest_started_at:
                try:
                    latest_attempt = datetime.fromisoformat(latest_started_at)
                except ValueError:
                    latest_attempt = None
                if (
                    latest_attempt is not None
                    and now - latest_attempt < timedelta(minutes=5)
                ):
                    continue
            last_sync_at = row["last_sync_at"]
            if not last_sync_at:
                result.append(row["id"])
                continue
            try:
                last = datetime.fromisoformat(last_sync_at)
            except ValueError:
                result.append(row["id"])
                continue
            if now - last >= interval:
                result.append(row["id"])
        return result

    def list_sync_runs(self, *, source_id: str | None, limit: int) -> dict[str, Any]:
        where = ""
        parameters: list[Any] = []
        if source_id:
            where = "WHERE source_id = ?"
            parameters.append(source_id)
        parameters.append(limit)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM sync_runs
                {where}
                ORDER BY started_at DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            items = [self._sync_run_to_dict(row) for row in rows]
            return {"items": items, "sync_runs": items, "total": len(items)}

    def get_sync_run(self, run_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            return self._get_sync_run(connection, run_id)

    def _get_sync_run(
        self, connection: sqlite3.Connection, run_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM sync_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise PocketError(404, "同步运行不存在")
        return self._sync_run_to_dict(row)

    @staticmethod
    def _sync_run_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "source_id": row["source_id"],
            "status": row["status"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "scanned_count": row["scanned_count"],
            "imported_count": row["imported_count"],
            "duplicate_count": row["duplicate_count"],
            "unchanged_count": row["unchanged_count"],
            "skipped_count": row["skipped_count"],
            "task_count": row["task_count"],
            "error": row["error"],
        }

    @staticmethod
    def _iter_source_files(root: Path, config: FolderSourceConfig) -> Iterable[Path]:
        allowed_extensions = (
            set(config.extensions) if config.extensions is not None else None
        )
        if not config.recursive:
            entries = sorted(root.iterdir(), key=lambda path: path.name.casefold())
            for path in entries:
                if path.is_symlink() or not path.is_file():
                    continue
                if not config.include_hidden and path.name.startswith("."):
                    continue
                if (
                    allowed_extensions is not None
                    and path.suffix.lower() not in allowed_extensions
                ):
                    continue
                yield path
            return

        def raise_walk_error(error: OSError) -> None:
            raise error

        for directory, directory_names, file_names in os.walk(
            root,
            followlinks=False,
            onerror=raise_walk_error,
        ):
            directory_names[:] = sorted(
                (
                    name
                    for name in directory_names
                    if config.include_hidden or not name.startswith(".")
                ),
                key=str.casefold,
            )
            for file_name in sorted(file_names, key=str.casefold):
                if not config.include_hidden and file_name.startswith("."):
                    continue
                path = Path(directory) / file_name
                if path.is_symlink() or not path.is_file():
                    continue
                if (
                    allowed_extensions is not None
                    and path.suffix.lower() not in allowed_extensions
                ):
                    continue
                yield path

    def _ingest_file(
        self,
        *,
        source_id: str,
        source_name: str,
        root: Path,
        file_path: Path,
        raw: bytes,
        stat_size: int,
        modified_at: str,
    ) -> str:
        origin_uri = file_path.resolve().as_uri()
        now = utc_now()
        relative_path = file_path.relative_to(root).as_posix()
        mime_type = mimetypes.guess_type(file_path.name)[0] or (
            "text/plain"
            if file_path.suffix.lower() in TEXT_EXTENSIONS
            else "application/octet-stream"
        )
        text_content = self._normalize_text(
            self._extract_text(raw, file_path, mime_type)
        )
        # Use one content fingerprint for every text ingress path. Exact raw
        # bytes remain useful transport details, but BOM/newline differences
        # must not defeat folder-vs-mobile deduplication.
        content_hash = hashlib.sha256(text_content.encode("utf-8")).hexdigest()

        with self.database.transaction() as connection:
            existing_origin = connection.execute(
                """
                SELECT linked.item_id, item.content_hash
                FROM item_sources linked
                JOIN items item ON item.id = linked.item_id
                WHERE linked.source_id = ? AND linked.origin_uri = ?
                """,
                (source_id, origin_uri),
            ).fetchone()
            if (
                existing_origin is not None
                and existing_origin["content_hash"] == content_hash
            ):
                connection.execute(
                    """
                    UPDATE item_sources
                    SET source_modified_at = ?, last_seen_at = ?
                    WHERE source_id = ? AND origin_uri = ?
                    """,
                    (modified_at, now, source_id, origin_uri),
                )
                _, review_created = self._ensure_review_task(
                    connection,
                    item_id=existing_origin["item_id"],
                    now=now,
                    reason="此前未确认的内容再次出现在自动同步源中",
                )
                return "unchanged_review" if review_created else "unchanged"

            supersedes_item_id = (
                existing_origin["item_id"] if existing_origin is not None else None
            )
            if supersedes_item_id is None:
                historical_origin = connection.execute(
                    """
                    SELECT id
                    FROM items
                    WHERE origin_uri = ?
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT 1
                    """,
                    (origin_uri,),
                ).fetchone()
                if historical_origin is not None:
                    supersedes_item_id = historical_origin["id"]
            existing_item = connection.execute(
                "SELECT id FROM items WHERE content_hash = ?", (content_hash,)
            ).fetchone()
            if existing_item is not None:
                self._link_item_source(
                    connection,
                    source_id=source_id,
                    origin_uri=origin_uri,
                    item_id=existing_item["id"],
                    modified_at=modified_at,
                    now=now,
                )
                self._resolve_pending_deletion_tasks(
                    connection,
                    item_id=existing_item["id"],
                    now=now,
                )
                if supersedes_item_id and supersedes_item_id != existing_item["id"]:
                    self._connect_existing_supersession(
                        connection,
                        item_id=existing_item["id"],
                        supersedes_item_id=supersedes_item_id,
                        now=now,
                    )
                _, review_created = self._ensure_review_task(
                    connection,
                    item_id=existing_item["id"],
                    now=now,
                    reason="此前未确认的重复内容再次出现在自动同步源中",
                )
                return "duplicate_review" if review_created else "duplicate"

            item_id = new_id("item")
            title = self._title_from_path(file_path)
            category = self._category_for(file_path)
            tags = self._tags_for(file_path, root)
            metadata = {
                "relative_path": relative_path,
                "source_name": source_name,
                "extension": file_path.suffix.lower(),
            }
            if supersedes_item_id:
                metadata["supersedes_item_id"] = supersedes_item_id
                metadata["supersedes_item_ids"] = [supersedes_item_id]
            connection.execute(
                """
                INSERT INTO items(
                    id, content_hash, first_source_id, origin_uri, file_name,
                    mime_type, title, text_content, size_bytes,
                    source_modified_at, state, category, tags_json,
                    metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'needs_review', ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    content_hash,
                    source_id,
                    origin_uri,
                    file_path.name,
                    mime_type,
                    title,
                    text_content,
                    stat_size,
                    modified_at,
                    category,
                    json.dumps(tags, ensure_ascii=False),
                    json.dumps(metadata, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            self._link_item_source(
                connection,
                source_id=source_id,
                origin_uri=origin_uri,
                item_id=item_id,
                modified_at=modified_at,
                now=now,
            )
            self._refresh_fts(
                connection,
                item_id=item_id,
                title=title,
                body=text_content,
                tags=tags,
                category=category,
            )
            task_id = new_id("task")
            proposal = {
                "patch": {
                    "title": title,
                    "category": category,
                    "tags": tags,
                    "state": "ready",
                },
                "suggestion": "确认标题、分类和标签后加入 Agent 可用区",
                "reason": "新同步内容需要本人确认后才能向 Agent 开放",
                "confidence": 0.9,
            }
            if supersedes_item_id:
                proposal["supersedes_item_id"] = supersedes_item_id
                proposal["supersedes_item_ids"] = [supersedes_item_id]
            connection.execute(
                """
                INSERT INTO governance_tasks(
                    id, item_id, kind, status, proposal_json,
                    created_at, updated_at
                )
                VALUES (?, ?, 'review', 'pending', ?, ?, ?)
                """,
                (
                    task_id,
                    item_id,
                    json.dumps(proposal, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            if supersedes_item_id:
                self._resolve_pending_deletion_tasks(
                    connection,
                    item_id=supersedes_item_id,
                    now=now,
                    restored=False,
                )
            self._record_activity(
                connection,
                kind="item.discovered",
                message=f"发现新内容“{title}”，等待确认",
                resource_type="item",
                resource_id=item_id,
                details={"source_id": source_id, "task_id": task_id},
            )
            return "imported"

    def _ensure_review_task(
        self,
        connection: sqlite3.Connection,
        *,
        item_id: str,
        now: str,
        reason: str,
    ) -> tuple[str | None, bool]:
        pending = connection.execute(
            """
            SELECT id
            FROM governance_tasks
            WHERE item_id = ? AND status = 'pending'
            ORDER BY created_at
            LIMIT 1
            """,
            (item_id,),
        ).fetchone()
        if pending is not None:
            return pending["id"], False

        item = self._get_item(connection, item_id)
        if item["state"] == "ready":
            return None, False

        if item["state"] != "needs_review":
            connection.execute(
                """
                UPDATE items
                SET state = 'needs_review', version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (now, item_id),
            )
        task_id = new_id("task")
        proposal = {
            "patch": {
                "title": item["title"],
                "category": item["category"],
                "tags": item["tags"],
                "state": "ready",
            },
            "suggestion": "重新确认内容后加入 Agent 可用区",
            "reason": reason,
            "confidence": 0.9,
        }
        connection.execute(
            """
            INSERT INTO governance_tasks(
                id, item_id, kind, status, proposal_json,
                created_at, updated_at
            )
            VALUES (?, ?, 'review', 'pending', ?, ?, ?)
            """,
            (
                task_id,
                item_id,
                json.dumps(proposal, ensure_ascii=False),
                now,
                now,
            ),
        )
        return task_id, True

    def _connect_existing_supersession(
        self,
        connection: sqlite3.Connection,
        *,
        item_id: str,
        supersedes_item_id: str,
        now: str,
    ) -> None:
        target = self._get_item(connection, item_id)
        metadata = dict(target["metadata"])
        superseded_item_ids = self._superseded_item_ids(metadata)
        if (
            supersedes_item_id != item_id
            and supersedes_item_id not in superseded_item_ids
        ):
            superseded_item_ids.append(supersedes_item_id)
        if superseded_item_ids:
            metadata.setdefault("supersedes_item_id", superseded_item_ids[0])
            metadata["supersedes_item_ids"] = superseded_item_ids
        connection.execute(
            """
            UPDATE items
            SET metadata_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (json.dumps(metadata, ensure_ascii=False), now, item_id),
        )
        pending_tasks = connection.execute(
            """
            SELECT id, proposal_json
            FROM governance_tasks
            WHERE item_id = ? AND status = 'pending'
            """,
            (item_id,),
        ).fetchall()
        for task in pending_tasks:
            proposal = json_loads(task["proposal_json"], {})
            proposal_superseded = self._superseded_item_ids(proposal)
            if (
                supersedes_item_id != item_id
                and supersedes_item_id not in proposal_superseded
            ):
                proposal_superseded.append(supersedes_item_id)
            if proposal_superseded:
                proposal.setdefault("supersedes_item_id", proposal_superseded[0])
                proposal["supersedes_item_ids"] = proposal_superseded
            connection.execute(
                """
                UPDATE governance_tasks
                SET proposal_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(proposal, ensure_ascii=False), now, task["id"]),
            )
        self._resolve_pending_deletion_tasks(
            connection,
            item_id=supersedes_item_id,
            now=now,
            restored=False,
        )
        if target["state"] == "ready":
            target["metadata"] = metadata
            self._archive_superseded_generations(
                connection,
                item=target,
                now=now,
            )

    @staticmethod
    def _link_item_source(
        connection: sqlite3.Connection,
        *,
        source_id: str,
        origin_uri: str,
        item_id: str,
        modified_at: str,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO item_sources(
                source_id, origin_uri, item_id, source_modified_at,
                first_seen_at, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, origin_uri) DO UPDATE SET
                item_id = excluded.item_id,
                source_modified_at = excluded.source_modified_at,
                last_seen_at = excluded.last_seen_at
            """,
            (source_id, origin_uri, item_id, modified_at, now, now),
        )

    def _resolve_pending_deletion_tasks(
        self,
        connection: sqlite3.Connection,
        *,
        item_id: str,
        now: str,
        restored: bool = True,
    ) -> None:
        pending_ids = [
            row["id"]
            for row in connection.execute(
                """
                SELECT id
                FROM governance_tasks
                WHERE item_id = ? AND kind = 'deletion' AND status = 'pending'
                """,
                (item_id,),
            ).fetchall()
        ]
        if not pending_ids:
            return
        placeholders = ",".join("?" for _ in pending_ids)
        connection.execute(
            f"""
            UPDATE governance_tasks
            SET status = 'skipped', resolved_at = ?, updated_at = ?
            WHERE id IN ({placeholders})
            """,
            (now, now, *pending_ids),
        )
        item = self._get_item(connection, item_id)
        self._record_activity(
            connection,
            kind=(
                "item.source_restored"
                if restored
                else "item.deletion_superseded"
            ),
            message=(
                f"“{item['title']}”已重新出现在同步源中"
                if restored
                else f"“{item['title']}”已有新版本，删除建议已由版本治理接管"
            ),
            resource_type="item",
            resource_id=item_id,
            details={"resolved_deletion_tasks": pending_ids},
        )

    @staticmethod
    def _title_from_path(path: Path) -> str:
        title = re.sub(r"[_-]+", " ", path.stem).strip()
        return title or path.name

    @staticmethod
    def _category_for(path: Path) -> str:
        extension = path.suffix.lower()
        if extension in {".csv", ".tsv", ".xls", ".xlsx"}:
            return "table"
        if extension in {
            ".c",
            ".cpp",
            ".css",
            ".go",
            ".java",
            ".js",
            ".py",
            ".rs",
            ".sql",
            ".ts",
            ".tsx",
        }:
            return "code"
        if extension in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"}:
            return "image"
        if extension in {".mp3", ".m4a", ".wav", ".mp4", ".mov", ".mkv"}:
            return "media"
        if extension in TEXT_EXTENSIONS or extension in {
            ".doc",
            ".docx",
            ".odt",
            ".pdf",
        }:
            return "document"
        return "file"

    @staticmethod
    def _tags_for(path: Path, root: Path) -> list[str]:
        relative = path.relative_to(root)
        values = [part for part in relative.parent.parts[-3:] if part not in {"", "."}]
        if path.suffix:
            values.append(path.suffix.lower().lstrip("."))
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value.casefold() in seen:
                continue
            seen.add(value.casefold())
            result.append(value)
        return result

    @staticmethod
    def _extract_text(raw: bytes, path: Path, mime_type: str) -> str:
        if not (
            mime_type.startswith("text/")
            or path.suffix.lower() in TEXT_EXTENSIONS
            or mime_type in {"application/json", "application/xml"}
        ):
            return ""
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _normalize_text(value: str) -> str:
        return value.replace("\r\n", "\n").replace("\r", "\n").strip()

    @staticmethod
    def _is_supported_text_path(path: Path) -> bool:
        mime_type = mimetypes.guess_type(path.name)[0] or ""
        return (
            path.suffix.lower() in TEXT_EXTENSIONS
            or mime_type.startswith("text/")
            or mime_type in {"application/json", "application/xml"}
        )

    # Mobile capture

    def capture_text(
        self,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        operation = "capture:create"
        content = (payload.get("text") or "").strip()
        url = (payload.get("url") or "").strip() or None
        normalized_content = content or url or ""
        # Provenance is part of a mobile capture's identity. Two pages can have
        # the same shared annotation without being the same personal record.
        canonical_capture = (
            f"{normalized_content}\0{url}"
            if url
            else self._normalize_text(normalized_content)
        )
        content_hash = hashlib.sha256(canonical_capture.encode("utf-8")).hexdigest()
        with self.database.transaction() as connection:
            cached = self._idempotent_response(connection, operation, idempotency_key)
            if cached is not None:
                return cached

            existing = connection.execute(
                "SELECT id, state FROM items WHERE content_hash = ?", (content_hash,)
            ).fetchone()
            capture_id = new_id("cap")
            if existing is not None:
                task_id, review_created = self._ensure_review_task(
                    connection,
                    item_id=existing["id"],
                    now=utc_now(),
                    reason="此前跳过或归档的手机内容被再次分享",
                )
                if review_created:
                    self._record_activity(
                        connection,
                        kind="capture.reopened",
                        message="再次收到未确认内容，已重新加入治理收件箱",
                        resource_type="item",
                        resource_id=existing["id"],
                        details={"capture_id": capture_id, "task_id": task_id},
                    )
                response = {
                    "id": capture_id,
                    "item_id": existing["id"],
                    "task_id": task_id,
                    "status": (
                        "deduplicated"
                        if existing["state"] == "ready"
                        else "needs_review"
                    ),
                    "deduplicated": True,
                }
                self._store_idempotent_response(
                    connection, operation, idempotency_key, response
                )
                return response

            title = (payload.get("title") or "").strip()
            if not title:
                title = self._capture_title(normalized_content, url)
            origin = (payload.get("origin") or "mobile").strip() or "mobile"
            mime_type = payload.get("mime_type") or "text/plain"
            now = utc_now()
            item_id = new_id("item")
            task_id = new_id("task")
            tags = ["手机采集"]
            if origin.casefold() not in {"mobile", "share", "manual"}:
                tags.append(origin)
            metadata = {
                "capture_id": capture_id,
                "source_name": "手机采集",
                "origin": origin,
                "url": url,
            }
            connection.execute(
                """
                INSERT INTO items(
                    id, content_hash, first_source_id, origin_uri, file_name,
                    mime_type, title, text_content, size_bytes,
                    state, category, tags_json, metadata_json,
                    created_at, updated_at
                )
                VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, 'needs_review', ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    content_hash,
                    url or f"capture://{capture_id}",
                    f"{title}.txt",
                    mime_type,
                    title,
                    normalized_content,
                    len(normalized_content.encode("utf-8")),
                    "link" if url and not content else "note",
                    json.dumps(tags, ensure_ascii=False),
                    json.dumps(metadata, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            self._refresh_fts(
                connection,
                item_id=item_id,
                title=title,
                body=normalized_content,
                tags=tags,
                category="link" if url and not content else "note",
            )
            proposal = {
                "patch": {
                    "title": title,
                    "category": "link" if url and not content else "note",
                    "tags": tags,
                    "state": "ready",
                },
                "suggestion": "确认手机采集内容后加入 Agent 可用区",
                "reason": "分享或手动采集的内容需要本人确认",
                "confidence": 0.9,
            }
            connection.execute(
                """
                INSERT INTO governance_tasks(
                    id, item_id, kind, status, proposal_json,
                    created_at, updated_at
                )
                VALUES (?, ?, 'review', 'pending', ?, ?, ?)
                """,
                (
                    task_id,
                    item_id,
                    json.dumps(proposal, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            self._record_activity(
                connection,
                kind="capture.created",
                message=f"已从手机采集“{title}”，等待确认",
                resource_type="item",
                resource_id=item_id,
                details={"capture_id": capture_id, "task_id": task_id},
            )
            response = {
                "id": capture_id,
                "item_id": item_id,
                "task_id": task_id,
                "status": "needs_review",
                "deduplicated": False,
            }
            self._store_idempotent_response(
                connection, operation, idempotency_key, response
            )
            return response

    @staticmethod
    def _capture_title(content: str, url: str | None) -> str:
        if url:
            without_scheme = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", url)
            host_or_path = without_scheme.split("/", 1)[0].strip()
            if host_or_path:
                return host_or_path[:120]
        first_line = next(
            (line.strip() for line in content.splitlines() if line.strip()),
            "手机采集",
        )
        return first_line[:120]

    # Items

    def list_items(
        self,
        *,
        state: str | None,
        query: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if state:
            clauses.append("state = ?")
            parameters.append(state)
        if query:
            clauses.append("(title LIKE ? OR text_content LIKE ? OR tags_json LIKE ?)")
            pattern = f"%{query.strip()}%"
            parameters.extend((pattern, pattern, pattern))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.database.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM items {where}", parameters
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT * FROM items
                {where}
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
            return {
                "items": [self._item_to_dict(row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    def get_item(self, item_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            return self._get_item(connection, item_id, include_sources=True)

    def update_item(self, item_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        if not updates:
            return self.get_item(item_id)
        with self.database.transaction() as connection:
            current = self._get_item(connection, item_id)
            after = {
                "title": current["title"],
                "category": current["category"],
                "tags": current["tags"],
                "state": current["state"],
            }
            after.update(updates)
            self._validate_ready(
                {**after, "text_content": current["text_content"]}
            )
            if after["state"] == "ready" and current["state"] != "ready":
                pending_task = connection.execute(
                    """
                    SELECT id
                    FROM governance_tasks
                    WHERE item_id = ? AND status = 'pending'
                    LIMIT 1
                    """,
                    (item_id,),
                ).fetchone()
                if pending_task is not None:
                    raise PocketError(
                        409,
                        "该条目仍有待处理治理任务，请通过任务接受操作进入 ready",
                    )
            now = utc_now()
            connection.execute(
                """
                UPDATE items
                SET title = ?, category = ?, tags_json = ?, state = ?,
                    version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (
                    after["title"].strip(),
                    after["category"],
                    json.dumps(after["tags"], ensure_ascii=False),
                    after["state"],
                    now,
                    item_id,
                ),
            )
            self._refresh_fts(
                connection,
                item_id=item_id,
                title=after["title"].strip(),
                body=current["text_content"],
                tags=after["tags"],
                category=after["category"],
            )
            superseded: list[dict[str, Any]] = []
            if after["state"] == "ready":
                superseded = self._archive_superseded_generations(
                    connection,
                    item=current,
                    now=now,
                )
            self._record_activity(
                connection,
                kind="item.updated",
                message=f"已更新“{after['title'].strip()}”",
                resource_type="item",
                resource_id=item_id,
                details={
                    "fields": sorted(updates),
                    "archived_generations": [
                        entry["item_id"] for entry in superseded
                    ],
                },
            )
            return self._get_item(connection, item_id, include_sources=True)

    def _get_item(
        self,
        connection: sqlite3.Connection,
        item_id: str,
        *,
        include_sources: bool = False,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            raise PocketError(404, "数据条目不存在")
        item = self._item_to_dict(row)
        if include_sources:
            item["sources"] = [
                {
                    "source_id": linked["source_id"],
                    "source_name": linked["source_name"],
                    "origin_uri": linked["origin_uri"],
                    "source_modified_at": linked["source_modified_at"],
                    "first_seen_at": linked["first_seen_at"],
                    "last_seen_at": linked["last_seen_at"],
                }
                for linked in connection.execute(
                    """
                    SELECT linked.*, source.name AS source_name
                    FROM item_sources linked
                    JOIN sources source ON source.id = linked.source_id
                    WHERE linked.item_id = ?
                    ORDER BY linked.first_seen_at
                    """,
                    (item_id,),
                ).fetchall()
            ]
        return item

    @staticmethod
    def _item_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        text_content = row["text_content"]
        preview = re.sub(r"\s+", " ", text_content).strip()[:280]
        return {
            "id": row["id"],
            "content_hash": row["content_hash"],
            "first_source_id": row["first_source_id"],
            "origin_uri": row["origin_uri"],
            "file_name": row["file_name"],
            "mime_type": row["mime_type"],
            "title": row["title"],
            "text_content": text_content,
            "preview": preview,
            "size_bytes": row["size_bytes"],
            "source_modified_at": row["source_modified_at"],
            "state": row["state"],
            "category": row["category"],
            "tags": json_loads(row["tags_json"], []),
            "metadata": json_loads(row["metadata_json"], {}),
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # Governance

    def list_tasks(
        self,
        *,
        status: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        where = "WHERE task.status = ?" if status else ""
        parameters: list[Any] = [status] if status else []
        with self.database.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM governance_tasks task {where}", parameters
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                {self._task_select_sql()}
                {where}
                ORDER BY task.created_at DESC
                LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
            items = [self._task_to_dict(row) for row in rows]
            return {
                "items": items,
                "tasks": items,
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            return self._get_task(connection, task_id)

    def apply_task(
        self,
        task_id: str,
        patch: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        operation = f"task:{task_id}:apply"
        with self.database.transaction() as connection:
            cached = self._idempotent_response(connection, operation, idempotency_key)
            if cached is not None:
                return cached
            task = self._get_task(connection, task_id)
            if task["status"] == "skipped":
                raise PocketError(409, "已跳过的任务需先撤销，才能再次应用")
            if task["status"] == "applied":
                response = {
                    "task": task,
                    "next_task": self._next_task(connection, exclude_id=task_id),
                }
                self._store_idempotent_response(
                    connection, operation, idempotency_key, response
                )
                return response

            item = self._get_item(connection, task["item_id"])
            before = self._editable_snapshot(item)
            proposal_patch = task["proposal"].get("patch", {})
            after = {**before, **proposal_patch, **patch}
            is_deletion = task["kind"] == "deletion"
            if is_deletion:
                # Deletion cards have one unambiguous meaning. Do not let a
                # stale or generic client accidentally turn "archive" into
                # an ordinary ready-state review.
                after["state"] = "archived"
            elif after.get("state") != "ready":
                raise PocketError(422, "接受治理任务时条目必须进入 ready 状态")
            self._validate_ready({**after, "text_content": item["text_content"]})
            now = utc_now()
            connection.execute(
                """
                UPDATE items
                SET title = ?, category = ?, tags_json = ?, state = ?,
                    version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (
                    after["title"].strip(),
                    after.get("category"),
                    json.dumps(after.get("tags", []), ensure_ascii=False),
                    after.get("state", "ready"),
                    now,
                    task["item_id"],
                ),
            )
            self._refresh_fts(
                connection,
                item_id=task["item_id"],
                title=after["title"].strip(),
                body=item["text_content"],
                tags=after.get("tags", []),
                category=after.get("category"),
            )
            superseded: list[dict[str, Any]] = []
            if after.get("state", "ready") == "ready":
                superseded = self._archive_superseded_generations(
                    connection,
                    item=item,
                    now=now,
                )
            connection.execute(
                """
                UPDATE governance_tasks
                SET status = 'applied', resolved_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, task_id),
            )
            action_after = dict(after)
            if superseded:
                action_after["_superseded"] = superseded
            connection.execute(
                """
                INSERT INTO governance_actions(
                    id, task_id, item_id, action, before_json, after_json, created_at
                )
                VALUES (?, ?, ?, 'apply', ?, ?, ?)
                """,
                (
                    new_id("act"),
                    task_id,
                    task["item_id"],
                    json.dumps(before, ensure_ascii=False),
                    json.dumps(action_after, ensure_ascii=False),
                    now,
                ),
            )
            self._record_activity(
                connection,
                kind="governance.applied",
                message=(
                    f"已归档来源中消失的“{after['title'].strip()}”"
                    if is_deletion
                    else f"已确认“{after['title'].strip()}”并开放给 Agent"
                ),
                resource_type="governance_task",
                resource_id=task_id,
                details={
                    "item_id": task["item_id"],
                    "archived_generations": [entry["item_id"] for entry in superseded],
                },
            )
            response = {
                "task": self._get_task(connection, task_id),
                "next_task": self._next_task(connection, exclude_id=task_id),
            }
            self._store_idempotent_response(
                connection, operation, idempotency_key, response
            )
            return response

    def skip_task(
        self,
        task_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        operation = f"task:{task_id}:skip"
        with self.database.transaction() as connection:
            cached = self._idempotent_response(connection, operation, idempotency_key)
            if cached is not None:
                return cached
            task = self._get_task(connection, task_id)
            if task["status"] == "applied":
                raise PocketError(409, "已应用的任务需先撤销，才能跳过")
            if task["status"] == "pending":
                now = utc_now()
                connection.execute(
                    """
                    UPDATE governance_tasks
                    SET status = 'skipped', resolved_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, task_id),
                )
                connection.execute(
                    """
                    INSERT INTO governance_actions(
                        id, task_id, item_id, action, created_at
                    )
                    VALUES (?, ?, ?, 'skip', ?)
                    """,
                    (new_id("act"), task_id, task["item_id"], now),
                )
                self._record_activity(
                    connection,
                    kind="governance.skipped",
                    message=f"已暂时跳过“{task['title']}”",
                    resource_type="governance_task",
                    resource_id=task_id,
                    details={"item_id": task["item_id"]},
                )
            response = {
                "task": self._get_task(connection, task_id),
                "next_task": self._next_task(connection, exclude_id=task_id),
            }
            self._store_idempotent_response(
                connection, operation, idempotency_key, response
            )
            return response

    def undo_task(
        self,
        task_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        operation = f"task:{task_id}:undo"
        with self.database.transaction() as connection:
            cached = self._idempotent_response(connection, operation, idempotency_key)
            if cached is not None:
                return cached
            task = self._get_task(connection, task_id)
            if task["status"] == "pending":
                response = {
                    "task": task,
                    "next_task": self._next_task(connection, exclude_id=task_id),
                }
                self._store_idempotent_response(
                    connection, operation, idempotency_key, response
                )
                return response

            action = connection.execute(
                """
                SELECT *
                FROM governance_actions
                WHERE task_id = ? AND action IN ('apply', 'skip')
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if action is None:
                raise PocketError(409, "没有可撤销的治理动作")

            now = utc_now()
            if action["action"] == "apply":
                before = json_loads(action["before_json"], {})
                action_after = json_loads(action["after_json"], {})
                item = self._get_item(connection, task["item_id"])
                connection.execute(
                    """
                    UPDATE items
                    SET title = ?, category = ?, tags_json = ?, state = ?,
                        version = version + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        before["title"],
                        before.get("category"),
                        json.dumps(before.get("tags", []), ensure_ascii=False),
                        before["state"],
                        now,
                        task["item_id"],
                    ),
                )
                self._refresh_fts(
                    connection,
                    item_id=task["item_id"],
                    title=before["title"],
                    body=item["text_content"],
                    tags=before.get("tags", []),
                    category=before.get("category"),
                )
                for superseded in action_after.get("_superseded", []):
                    connection.execute(
                        """
                        UPDATE items
                        SET state = ?, version = version + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            superseded["previous_state"],
                            now,
                            superseded["item_id"],
                        ),
                    )
                    pending_task_ids = superseded.get("pending_task_ids", [])
                    if pending_task_ids:
                        placeholders = ",".join("?" for _ in pending_task_ids)
                        connection.execute(
                            f"""
                            UPDATE governance_tasks
                            SET status = 'pending', resolved_at = NULL, updated_at = ?
                            WHERE id IN ({placeholders})
                            """,
                            (now, *pending_task_ids),
                        )

            connection.execute(
                """
                UPDATE governance_tasks
                SET status = 'pending', resolved_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, task_id),
            )
            connection.execute(
                """
                INSERT INTO governance_actions(
                    id, task_id, item_id, action, before_json, after_json, created_at
                )
                VALUES (?, ?, ?, 'undo', ?, ?, ?)
                """,
                (
                    new_id("act"),
                    task_id,
                    task["item_id"],
                    action["after_json"],
                    action["before_json"],
                    now,
                ),
            )
            self._record_activity(
                connection,
                kind="governance.undone",
                message=f"已撤销“{task['title']}”的上一步操作",
                resource_type="governance_task",
                resource_id=task_id,
                details={"item_id": task["item_id"]},
            )
            response = {
                "task": self._get_task(connection, task_id),
                "next_task": self._next_task(connection, exclude_id=task_id),
            }
            self._store_idempotent_response(
                connection, operation, idempotency_key, response
            )
            return response

    def _archive_superseded_generations(
        self,
        connection: sqlite3.Connection,
        *,
        item: dict[str, Any],
        now: str,
    ) -> list[dict[str, Any]]:
        archived: list[dict[str, Any]] = []
        seen: set[str] = {item["id"]}
        pending_ids = list(
            reversed(self._superseded_item_ids(item.get("metadata", {})))
        )
        while pending_ids:
            supersedes_item_id = pending_ids.pop()
            if supersedes_item_id in seen:
                continue
            seen.add(supersedes_item_id)
            old = connection.execute(
                "SELECT * FROM items WHERE id = ?", (supersedes_item_id,)
            ).fetchone()
            if old is None:
                continue
            old_metadata = json_loads(old["metadata_json"], {})
            active_links = connection.execute(
                "SELECT COUNT(*) FROM item_sources WHERE item_id = ?",
                (supersedes_item_id,),
            ).fetchone()[0]
            if active_links:
                continue
            if old["state"] == "archived":
                pending_ids.extend(
                    reversed(self._superseded_item_ids(old_metadata))
                )
                continue
            pending_task_ids = [
                row["id"]
                for row in connection.execute(
                    """
                    SELECT id
                    FROM governance_tasks
                    WHERE item_id = ? AND status = 'pending'
                    """,
                    (supersedes_item_id,),
                ).fetchall()
            ]
            archived.append(
                {
                    "item_id": supersedes_item_id,
                    "previous_state": old["state"],
                    "pending_task_ids": pending_task_ids,
                }
            )
            connection.execute(
                """
                UPDATE items
                SET state = 'archived', version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (now, supersedes_item_id),
            )
            if pending_task_ids:
                placeholders = ",".join("?" for _ in pending_task_ids)
                connection.execute(
                    f"""
                    UPDATE governance_tasks
                    SET status = 'skipped', resolved_at = ?, updated_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    (now, now, *pending_task_ids),
                )
            pending_ids.extend(
                reversed(self._superseded_item_ids(old_metadata))
            )
        return archived

    @staticmethod
    def _superseded_item_ids(metadata: dict[str, Any]) -> list[str]:
        result: list[str] = []
        values = metadata.get("supersedes_item_ids")
        if isinstance(values, list):
            for value in values:
                if isinstance(value, str) and value and value not in result:
                    result.append(value)
        legacy = metadata.get("supersedes_item_id")
        if isinstance(legacy, str) and legacy and legacy not in result:
            result.insert(0, legacy)
        return result

    @staticmethod
    def _task_select_sql() -> str:
        return """
            SELECT
                task.*,
                item.title,
                item.text_content,
                item.state AS item_state,
                item.category,
                item.tags_json,
                item.updated_at AS item_updated_at,
                COALESCE(source.name, json_extract(item.metadata_json, '$.source_name'))
                    AS source_name
            FROM governance_tasks task
            JOIN items item ON item.id = task.item_id
            LEFT JOIN sources source ON source.id = item.first_source_id
        """

    def _get_task(self, connection: sqlite3.Connection, task_id: str) -> dict[str, Any]:
        row = connection.execute(
            f"{self._task_select_sql()} WHERE task.id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise PocketError(404, "治理任务不存在")
        return self._task_to_dict(row)

    def _next_task(
        self,
        connection: sqlite3.Connection,
        *,
        exclude_id: str | None = None,
    ) -> dict[str, Any] | None:
        where = "WHERE task.status = 'pending'"
        parameters: list[Any] = []
        if exclude_id:
            where += " AND task.id != ?"
            parameters.append(exclude_id)
        row = connection.execute(
            f"""
            {self._task_select_sql()}
            {where}
            ORDER BY task.created_at
            LIMIT 1
            """,
            parameters,
        ).fetchone()
        return self._task_to_dict(row) if row else None

    @staticmethod
    def _task_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        proposal = json_loads(row["proposal_json"], {})
        preview = re.sub(r"\s+", " ", row["text_content"]).strip()[:280]
        return {
            "id": row["id"],
            "task_id": row["id"],
            "item_id": row["item_id"],
            "kind": row["kind"],
            "status": row["status"],
            "title": row["title"],
            "preview": preview,
            "source_name": row["source_name"] or "个人数据",
            "suggestion": proposal.get("suggestion", ""),
            "reason": proposal.get("reason", ""),
            "confidence": proposal.get("confidence"),
            "proposal": proposal,
            "item": {
                "id": row["item_id"],
                "title": row["title"],
                "state": row["item_state"],
                "category": row["category"],
                "tags": json_loads(row["tags_json"], []),
                "updated_at": row["item_updated_at"],
            },
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _editable_snapshot(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": item["title"],
            "category": item["category"],
            "tags": item["tags"],
            "state": item["state"],
        }

    @staticmethod
    def _validate_ready(item: dict[str, Any]) -> None:
        if item.get("state") != "ready":
            return
        if not str(item.get("title", "")).strip():
            raise PocketError(422, "进入 ready 前必须填写标题")
        if not str(item.get("text_content", "")).strip():
            raise PocketError(422, "空内容不能进入 ready")

    # Read-only Agent search

    def agent_search(
        self,
        *,
        query: str,
        limit: int,
        tags: list[str],
        category: str | None,
    ) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise PocketError(422, "query 不能为空")
        tokens = re.findall(r"\w+", query, flags=re.UNICODE)
        candidates: list[dict[str, Any]] = []
        with self.database.connect() as connection:
            if tokens:
                match_query = " OR ".join(
                    f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens
                )
                category_clause = "AND item.category = ?" if category else ""
                parameters: list[Any] = [match_query]
                if category:
                    parameters.append(category)
                parameters.append(min(limit * 8, 200))
                rows = connection.execute(
                    f"""
                    SELECT
                        item.*,
                        bm25(item_fts) AS fts_rank,
                        snippet(item_fts, 2, '', '', ' … ', 28) AS search_snippet,
                        active_link.origin_uri AS active_origin_uri,
                        source.name AS active_source_name,
                        source.config_json AS active_source_config
                    FROM item_fts
                    JOIN items item ON item.id = item_fts.item_id
                    LEFT JOIN item_sources active_link
                      ON active_link.rowid = (
                        SELECT candidate.rowid
                        FROM item_sources candidate
                        WHERE candidate.item_id = item.id
                        ORDER BY candidate.last_seen_at DESC, candidate.origin_uri
                        LIMIT 1
                      )
                    LEFT JOIN sources source ON source.id = active_link.source_id
                    WHERE item_fts MATCH ?
                      AND item.state = 'ready'
                      {category_clause}
                    ORDER BY bm25(item_fts), item.updated_at DESC
                    LIMIT ?
                    """,
                    parameters,
                ).fetchall()
                candidates = [
                    self._agent_result(row, fallback_score=False) for row in rows
                ]

            if not candidates:
                category_clause = "AND item.category = ?" if category else ""
                like = f"%{query}%"
                parameters = [like, like, like]
                if category:
                    parameters.append(category)
                parameters.append(min(limit * 8, 200))
                rows = connection.execute(
                    f"""
                    SELECT
                        item.*,
                        NULL AS fts_rank,
                        substr(item.text_content, 1, 320) AS search_snippet,
                        active_link.origin_uri AS active_origin_uri,
                        source.name AS active_source_name,
                        source.config_json AS active_source_config
                    FROM items item
                    LEFT JOIN item_sources active_link
                      ON active_link.rowid = (
                        SELECT candidate.rowid
                        FROM item_sources candidate
                        WHERE candidate.item_id = item.id
                        ORDER BY candidate.last_seen_at DESC, candidate.origin_uri
                        LIMIT 1
                      )
                    LEFT JOIN sources source ON source.id = active_link.source_id
                    WHERE item.state = 'ready'
                      AND (
                        item.title LIKE ?
                        OR item.text_content LIKE ?
                        OR item.tags_json LIKE ?
                      )
                      {category_clause}
                    ORDER BY item.updated_at DESC
                    LIMIT ?
                    """,
                    parameters,
                ).fetchall()
                candidates = [
                    self._agent_result(row, fallback_score=True) for row in rows
                ]

        required_tags = {tag.casefold() for tag in tags if tag.strip()}
        if required_tags:
            candidates = [
                candidate
                for candidate in candidates
                if required_tags.issubset({tag.casefold() for tag in candidate["tags"]})
            ]
        results = candidates[:limit]
        return {
            "query": query,
            "results": results,
            "count": len(results),
            "visibility": "ready_only",
        }

    @staticmethod
    def _agent_result(row: sqlite3.Row, *, fallback_score: bool) -> dict[str, Any]:
        rank = row["fts_rank"]
        score = 0.5 if fallback_score or rank is None else 1 / (1 + abs(rank))
        snippet = re.sub(r"\s+", " ", row["search_snippet"] or "").strip()
        return {
            "item_id": row["id"],
            "title": row["title"],
            "snippet": snippet,
            "source": PocketService._agent_source_label(row),
            "score": round(score, 6),
            "category": row["category"],
            "tags": json_loads(row["tags_json"], []),
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _agent_source_label(row: sqlite3.Row) -> str:
        active_origin_uri = row["active_origin_uri"]
        source_name = row["active_source_name"]
        if not active_origin_uri or not source_name:
            origin_uri = row["origin_uri"]
            if urlparse(origin_uri).scheme != "file":
                return origin_uri

            # Ready knowledge is retained when its folder source disappears,
            # but the immutable file URI contains the server's absolute path.
            # Use the logical provenance saved at ingestion for Agent-facing
            # citations instead of exposing private directory topology.
            metadata = json_loads(row["metadata_json"], {})
            logical_source = str(metadata.get("source_name") or "文件来源").strip()
            relative_path = str(
                metadata.get("relative_path") or row["file_name"] or "已移除内容"
            )
            logical_path = relative_path.replace("\\", "/").lstrip("/")
            return f"{logical_source}/{logical_path}"

        origin_uri = active_origin_uri
        config = json_loads(row["active_source_config"], {})
        root_value = config.get("path")
        parsed = urlparse(origin_uri)
        if parsed.scheme == "file":
            source_path = Path(unquote(parsed.path))
            if root_value:
                try:
                    relative = source_path.relative_to(Path(root_value))
                    return f"{source_name}/{relative.as_posix()}"
                except ValueError:
                    pass
            return f"{source_name}/{source_path.name}"
        return f"{source_name}/{origin_uri}"

    # Shared persistence helpers

    @staticmethod
    def _refresh_fts(
        connection: sqlite3.Connection,
        *,
        item_id: str,
        title: str,
        body: str,
        tags: list[str],
        category: str | None,
    ) -> None:
        connection.execute("DELETE FROM item_fts WHERE item_id = ?", (item_id,))
        connection.execute(
            """
            INSERT INTO item_fts(item_id, title, body, tags, category)
            VALUES (?, ?, ?, ?, ?)
            """,
            (item_id, title, body, " ".join(tags), category or ""),
        )

    @staticmethod
    def _record_activity(
        connection: sqlite3.Connection,
        *,
        kind: str,
        message: str,
        resource_type: str | None = None,
        resource_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO activity_events(
                id, kind, message, resource_type, resource_id,
                details_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("evt"),
                kind,
                message,
                resource_type,
                resource_id,
                json.dumps(details or {}, ensure_ascii=False),
                utc_now(),
            ),
        )

    @staticmethod
    def _activity_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "message": row["message"],
            "resource_type": row["resource_type"],
            "resource_id": row["resource_id"],
            "details": json_loads(row["details_json"], {}),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _idempotent_response(
        connection: sqlite3.Connection,
        operation: str,
        idempotency_key: str | None,
    ) -> dict[str, Any] | None:
        if not idempotency_key:
            return None
        row = connection.execute(
            """
            SELECT response_json
            FROM idempotency_records
            WHERE operation = ? AND idempotency_key = ?
            """,
            (operation, idempotency_key),
        ).fetchone()
        return json_loads(row["response_json"], {}) if row else None

    @staticmethod
    def _store_idempotent_response(
        connection: sqlite3.Connection,
        operation: str,
        idempotency_key: str | None,
        response: dict[str, Any],
    ) -> None:
        if not idempotency_key:
            return
        connection.execute(
            """
            INSERT OR IGNORE INTO idempotency_records(
                operation, idempotency_key, response_json, created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                operation,
                idempotency_key,
                json.dumps(response, ensure_ascii=False),
                utc_now(),
            ),
        )
