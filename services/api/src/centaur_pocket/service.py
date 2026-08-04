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
from .im_knowledge import extract_knowledge_candidates
from .schemas import FolderSourceConfig, WechatVisibleWebConfig

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

COLLECTOR_REQUESTS_PER_MINUTE = 120
OBSERVER_HEARTBEAT_GAP_SECONDS = 60
MOBILE_PAIRING_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
MOBILE_PAIRING_TTL_SECONDS = 10 * 60
MOBILE_ACCESS_TTL_SECONDS = 15 * 60
MOBILE_REFRESH_TTL_SECONDS = 30 * 24 * 60 * 60


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


def utc_after(seconds: int) -> str:
    return (
        (datetime.now(UTC) + timedelta(seconds=seconds))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def constant_time_text_equal(left: str, right: str) -> bool:
    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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
        desktop_session_token: str | None = None,
    ):
        self.database = database
        self.owner_token = owner_token
        self.agent_token = agent_token
        self.desktop_session_token = desktop_session_token
        self.max_file_bytes = max_file_bytes
        # Imported lazily to keep the reliable-feed network/parser module able
        # to reuse PocketError without creating a module import cycle.
        from .reliable_sources import ReliableSourceService

        self.reliable_sources = ReliableSourceService(database)
        self.outlook_mail: Any | None = None

    def initialize(self) -> None:
        self.database.initialize()

    def attach_outlook_mail(self, outlook_mail: Any) -> None:
        self.outlook_mail = outlook_mail

    def owner_token_matches(self, candidate: str) -> bool:
        if constant_time_text_equal(candidate, self.owner_token):
            return True
        return bool(
            self.desktop_session_token
            and constant_time_text_equal(candidate, self.desktop_session_token)
        )

    def agent_token_matches(self, candidate: str) -> bool:
        return constant_time_text_equal(candidate, self.agent_token)

    def replace_agent_token(self, token: str) -> None:
        if not token.startswith("cp_live_"):
            raise ValueError("invalid CentaurAI Pocket Agent token")
        self.agent_token = token

    # Mobile pairing and short-lived device sessions

    @staticmethod
    def _new_mobile_pairing_code() -> str:
        compact = "".join(secrets.choice(MOBILE_PAIRING_ALPHABET) for _ in range(12))
        return "-".join(compact[index : index + 4] for index in range(0, 12, 4))

    @staticmethod
    def _normalize_mobile_pairing_code(value: str) -> str:
        compact = "".join(
            character
            for character in value.upper()
            if character not in "- \t\r\n"
        )
        if len(compact) != 12 or any(
            character not in MOBILE_PAIRING_ALPHABET for character in compact
        ):
            raise PocketError(401, "配对码无效或已失效")
        return "-".join(compact[index : index + 4] for index in range(0, 12, 4))

    @staticmethod
    def _new_mobile_tokens() -> tuple[str, str]:
        return (
            f"cp_device_{secrets.token_urlsafe(32)}",
            f"cp_refresh_{secrets.token_urlsafe(32)}",
        )

    @staticmethod
    def _mobile_device_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "device_id": row["device_id"],
            "display_name": row["display_name"],
            "platform": row["platform"],
            "app_version": row["app_version"],
            "status": "revoked" if row["revoked_at"] is not None else "active",
            "last_seen_at": row["last_seen_at"],
            "created_at": row["created_at"],
        }

    def _mobile_session_payload(
        self,
        device: sqlite3.Row,
        *,
        access_token: str,
        access_expires_at: str,
        refresh_token: str,
        refresh_expires_at: str,
    ) -> dict[str, Any]:
        return {
            "token_type": "Bearer",
            "access_token": access_token,
            "access_expires_at": access_expires_at,
            "refresh_token": refresh_token,
            "refresh_expires_at": refresh_expires_at,
            "device": self._mobile_device_to_dict(device),
        }

    def create_mobile_pairing(self) -> dict[str, str]:
        with self.database.transaction() as connection:
            created_at_dt = datetime.now(UTC)
            created_at = format_utc(created_at_dt)
            expires_at = format_utc(
                created_at_dt + timedelta(seconds=MOBILE_PAIRING_TTL_SECONDS)
            )
            for _attempt in range(5):
                pairing_id = new_id("mpair")
                code = self._new_mobile_pairing_code()
                try:
                    connection.execute(
                        """
                        INSERT INTO mobile_pairings(
                            id, code_hash, created_at, expires_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            pairing_id,
                            self._secret_hash(code),
                            created_at,
                            expires_at,
                        ),
                    )
                except sqlite3.IntegrityError:
                    continue
                break
            else:
                raise PocketError(503, "暂时无法创建手机配对码")
            self._record_activity(
                connection,
                kind="mobile.pairing_created",
                message="已创建一次性手机配对码",
                resource_type="mobile_pairing",
                resource_id=pairing_id,
            )
        return {
            "pairing_id": pairing_id,
            "code": code,
            "expires_at": expires_at,
        }

    def claim_mobile_pairing(self, payload: dict[str, str]) -> dict[str, Any]:
        code = self._normalize_mobile_pairing_code(payload["code"])
        code_hash = self._secret_hash(code)
        access_token, refresh_token = self._new_mobile_tokens()
        access_hash = self._secret_hash(access_token)
        refresh_hash = self._secret_hash(refresh_token)

        with self.database.transaction() as connection:
            now_dt = datetime.now(UTC)
            now = format_utc(now_dt)
            access_expires_at = format_utc(
                now_dt + timedelta(seconds=MOBILE_ACCESS_TTL_SECONDS)
            )
            refresh_expires_at = format_utc(
                now_dt + timedelta(seconds=MOBILE_REFRESH_TTL_SECONDS)
            )
            pairing = connection.execute(
                "SELECT * FROM mobile_pairings WHERE code_hash = ?",
                (code_hash,),
            ).fetchone()
            if (
                pairing is None
                or not secrets.compare_digest(pairing["code_hash"], code_hash)
                or pairing["claimed_at"] is not None
                or parse_utc(pairing["expires_at"]) <= now_dt
            ):
                raise PocketError(401, "配对码无效或已失效")

            device = connection.execute(
                "SELECT * FROM mobile_devices WHERE device_id = ?",
                (payload["device_id"],),
            ).fetchone()
            if device is None:
                mobile_device_id = new_id("mdev")
                connection.execute(
                    """
                    INSERT INTO mobile_devices(
                        id, device_id, display_name, platform, app_version,
                        created_at, updated_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mobile_device_id,
                        payload["device_id"],
                        payload["display_name"],
                        payload["platform"],
                        payload["app_version"],
                        now,
                        now,
                        now,
                    ),
                )
            else:
                mobile_device_id = device["id"]
                connection.execute(
                    """
                    UPDATE mobile_sessions
                    SET revoked_at = COALESCE(revoked_at, ?)
                    WHERE mobile_device_id = ?
                    """,
                    (now, mobile_device_id),
                )
                connection.execute(
                    """
                    UPDATE mobile_devices
                    SET display_name = ?, platform = ?, app_version = ?,
                        updated_at = ?, last_seen_at = ?, revoked_at = NULL
                    WHERE id = ?
                    """,
                    (
                        payload["display_name"],
                        payload["platform"],
                        payload["app_version"],
                        now,
                        now,
                        mobile_device_id,
                    ),
                )

            claimed = connection.execute(
                """
                UPDATE mobile_pairings
                SET claimed_at = ?, claimed_device_id = ?
                WHERE id = ? AND claimed_at IS NULL
                """,
                (now, mobile_device_id, pairing["id"]),
            )
            if claimed.rowcount != 1:
                raise PocketError(401, "配对码无效或已失效")

            connection.execute(
                """
                INSERT INTO mobile_sessions(
                    id, mobile_device_id, access_token_hash,
                    access_expires_at, refresh_token_hash,
                    refresh_expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("msess"),
                    mobile_device_id,
                    access_hash,
                    access_expires_at,
                    refresh_hash,
                    refresh_expires_at,
                    now,
                ),
            )
            self._record_activity(
                connection,
                kind="mobile.device_paired",
                message=f"手机设备“{payload['display_name']}”已配对",
                resource_type="mobile_device",
                resource_id=mobile_device_id,
            )
            device = connection.execute(
                "SELECT * FROM mobile_devices WHERE id = ?",
                (mobile_device_id,),
            ).fetchone()
            assert device is not None

        return self._mobile_session_payload(
            device,
            access_token=access_token,
            access_expires_at=access_expires_at,
            refresh_token=refresh_token,
            refresh_expires_at=refresh_expires_at,
        )

    def refresh_mobile_session(
        self, refresh_token: str, device_id: str
    ) -> dict[str, Any]:
        if (
            not refresh_token.startswith("cp_refresh_")
            or len(refresh_token) > 512
        ):
            raise PocketError(401, "手机会话凭据无效或已过期")
        refresh_hash = self._secret_hash(refresh_token)
        new_access_token, new_refresh_token = self._new_mobile_tokens()

        with self.database.transaction() as connection:
            now_dt = datetime.now(UTC)
            now = format_utc(now_dt)
            access_expires_at = format_utc(
                now_dt + timedelta(seconds=MOBILE_ACCESS_TTL_SECONDS)
            )
            refresh_expires_at = format_utc(
                now_dt + timedelta(seconds=MOBILE_REFRESH_TTL_SECONDS)
            )
            session = connection.execute(
                """
                SELECT session.*
                FROM mobile_sessions session
                WHERE session.refresh_token_hash = ?
                """,
                (refresh_hash,),
            ).fetchone()
            if (
                session is None
                or not secrets.compare_digest(
                    session["refresh_token_hash"], refresh_hash
                )
                or session["revoked_at"] is not None
                or parse_utc(session["refresh_expires_at"]) <= now_dt
            ):
                raise PocketError(401, "手机会话凭据无效或已过期")
            device = connection.execute(
                "SELECT * FROM mobile_devices WHERE id = ?",
                (session["mobile_device_id"],),
            ).fetchone()
            if (
                device is None
                or device["revoked_at"] is not None
                or not constant_time_text_equal(device["device_id"], device_id)
            ):
                raise PocketError(401, "手机会话凭据无效或已过期")

            revoked = connection.execute(
                """
                UPDATE mobile_sessions
                SET revoked_at = ?, last_used_at = ?
                WHERE id = ? AND revoked_at IS NULL
                """,
                (now, now, session["id"]),
            )
            if revoked.rowcount != 1:
                raise PocketError(401, "手机会话凭据无效或已过期")
            connection.execute(
                """
                INSERT INTO mobile_sessions(
                    id, mobile_device_id, access_token_hash,
                    access_expires_at, refresh_token_hash,
                    refresh_expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("msess"),
                    device["id"],
                    self._secret_hash(new_access_token),
                    access_expires_at,
                    self._secret_hash(new_refresh_token),
                    refresh_expires_at,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE mobile_devices
                SET last_seen_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, device["id"]),
            )
            device = connection.execute(
                "SELECT * FROM mobile_devices WHERE id = ?",
                (device["id"],),
            ).fetchone()
            assert device is not None

        return self._mobile_session_payload(
            device,
            access_token=new_access_token,
            access_expires_at=access_expires_at,
            refresh_token=new_refresh_token,
            refresh_expires_at=refresh_expires_at,
        )

    def authenticate_mobile_access(self, access_token: str) -> dict[str, Any]:
        if not access_token.startswith("cp_device_") or len(access_token) > 512:
            raise PocketError(401, "手机访问凭据无效或已过期")
        access_hash = self._secret_hash(access_token)
        with self.database.transaction() as connection:
            now_dt = datetime.now(UTC)
            now = format_utc(now_dt)
            session = connection.execute(
                "SELECT * FROM mobile_sessions WHERE access_token_hash = ?",
                (access_hash,),
            ).fetchone()
            if (
                session is None
                or not secrets.compare_digest(
                    session["access_token_hash"], access_hash
                )
                or session["revoked_at"] is not None
                or parse_utc(session["access_expires_at"]) <= now_dt
            ):
                raise PocketError(401, "手机访问凭据无效或已过期")
            device = connection.execute(
                "SELECT * FROM mobile_devices WHERE id = ?",
                (session["mobile_device_id"],),
            ).fetchone()
            if device is None or device["revoked_at"] is not None:
                raise PocketError(401, "手机访问凭据无效或已过期")
            connection.execute(
                "UPDATE mobile_sessions SET last_used_at = ? WHERE id = ?",
                (now, session["id"]),
            )
            connection.execute(
                """
                UPDATE mobile_devices
                SET last_seen_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, device["id"]),
            )
            device = connection.execute(
                "SELECT * FROM mobile_devices WHERE id = ?",
                (device["id"],),
            ).fetchone()
            assert device is not None
            return {
                "token_type": "Bearer",
                "access_expires_at": session["access_expires_at"],
                "device": self._mobile_device_to_dict(device),
            }

    def list_mobile_devices(self) -> dict[str, Any]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM mobile_devices ORDER BY created_at DESC, id DESC"
            ).fetchall()
        return {
            "items": [self._mobile_device_to_dict(row) for row in rows],
            "total": len(rows),
        }

    def revoke_mobile_device(self, mobile_device_id: str) -> None:
        now = utc_now()
        with self.database.transaction() as connection:
            device = connection.execute(
                "SELECT * FROM mobile_devices WHERE id = ?",
                (mobile_device_id,),
            ).fetchone()
            if device is None:
                raise PocketError(404, "手机设备不存在")
            connection.execute(
                """
                UPDATE mobile_devices
                SET revoked_at = COALESCE(revoked_at, ?), updated_at = ?
                WHERE id = ?
                """,
                (now, now, mobile_device_id),
            )
            connection.execute(
                """
                UPDATE mobile_sessions
                SET revoked_at = COALESCE(revoked_at, ?)
                WHERE mobile_device_id = ?
                """,
                (now, mobile_device_id),
            )
            self._record_activity(
                connection,
                kind="mobile.device_revoked",
                message=f"已吊销手机设备“{device['display_name']}”",
                resource_type="mobile_device",
                resource_id=mobile_device_id,
            )

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

            kind = payload["kind"]
            if kind == "folder":
                normalized_config = FolderSourceConfig.model_validate(
                    payload["config"]
                ).model_dump()
                self._assert_unique_folder_path(
                    connection, normalized_config["path"]
                )
                provider = None
            elif kind == "wechat_visible_web":
                normalized_config = WechatVisibleWebConfig.model_validate(
                    payload["config"]
                ).model_dump()
                provider = "wechat_visible_web"
            else:
                raise PocketError(422, "不支持的数据源类型")
            source_id = new_id("src")
            now = utc_now()
            connection.execute(
                """
                INSERT INTO sources(
                    id, kind, provider, name, config_json, schedule, enabled,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    kind,
                    provider,
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
            if current["kind"] == "rss":
                raise PocketError(
                    409,
                    "RSS 可靠信源只能通过 collection-plan 接口修改计划",
                )
            values: dict[str, Any] = {}
            if "display_name" in updates:
                values["name"] = updates["display_name"].strip()
            if "config" in updates:
                if current["kind"] == "folder":
                    if "path" not in updates["config"]:
                        raise PocketError(422, "文件夹数据源配置缺少 path")
                    config = FolderSourceConfig.model_validate(
                        updates["config"]
                    ).model_dump()
                    self._assert_unique_folder_path(
                        connection, config["path"], exclude_source_id=source_id
                    )
                else:
                    if "path" in updates["config"]:
                        raise PocketError(422, "微信网页观察器不能使用文件夹配置")
                    config = WechatVisibleWebConfig.model_validate(
                        updates["config"]
                    ).model_dump()
                values["config_json"] = json.dumps(config, ensure_ascii=False)
            if "schedule" in updates:
                expected_schedules = (
                    {"manual", "hourly", "daily"}
                    if current["kind"] == "folder"
                    else {"continuous"}
                )
                if updates["schedule"] not in expected_schedules:
                    raise PocketError(422, "该数据源不支持此同步计划")
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
            if source["kind"] == "rss":
                raise PocketError(409, "RSS 可靠信源不能通过通用数据源接口删除")
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
            "provider": row["provider"],
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

    # Visible Web IM observer

    @staticmethod
    def _secret_hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _iso_utc(value: str | datetime | None, *, fallback: str | None = None) -> str:
        if value is None:
            return fallback or utc_now()
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        parsed = parsed.astimezone(UTC)
        if parsed > datetime.now(UTC) + timedelta(minutes=5):
            raise PocketError(422, "客户端时间不能晚于服务器时间 5 分钟以上")
        return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def _require_observer_source(
        connection: sqlite3.Connection,
        source_id: str,
        *,
        require_enabled: bool = False,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM sources WHERE id = ?",
            (source_id,),
        ).fetchone()
        if row is None:
            raise PocketError(404, "数据源不存在")
        if row["kind"] != "wechat_visible_web":
            raise PocketError(409, "该接口只适用于微信网页观察器")
        if require_enabled and not row["enabled"]:
            raise PocketError(409, "观察器已暂停")
        return row

    def create_observer_pairing(
        self, source_id: str, *, expires_in_seconds: int = 600
    ) -> dict[str, Any]:
        pairing_code = f"cp_pair_{secrets.token_urlsafe(32)}"
        pairing_id = new_id("pair")
        created_at = utc_now()
        expires_at = (
            datetime.now(UTC) + timedelta(seconds=expires_in_seconds)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.database.transaction() as connection:
            self._require_observer_source(
                connection, source_id, require_enabled=True
            )
            connection.execute(
                """
                UPDATE collector_pairings
                SET revoked_at = ?
                WHERE source_id = ? AND used_at IS NULL AND revoked_at IS NULL
                """,
                (created_at, source_id),
            )
            connection.execute(
                """
                INSERT INTO collector_pairings(
                    id, source_id, code_hash, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    pairing_id,
                    source_id,
                    self._secret_hash(pairing_code),
                    created_at,
                    expires_at,
                ),
            )
            self._record_activity(
                connection,
                kind="observer.pairing_created",
                message="已创建微信网页观察器配对码",
                resource_type="source",
                resource_id=source_id,
            )
        # The plaintext is deliberately returned once and is never persisted.
        return {
            "id": pairing_id,
            "source_id": source_id,
            "pairing_code": pairing_code,
            "created_at": created_at,
            "expires_at": expires_at,
        }

    def revoke_observer_pairing(self, source_id: str, pairing_id: str) -> None:
        now = utc_now()
        with self.database.transaction() as connection:
            self._require_observer_source(connection, source_id)
            result = connection.execute(
                """
                UPDATE collector_pairings
                SET revoked_at = COALESCE(revoked_at, ?)
                WHERE id = ? AND source_id = ?
                """,
                (now, pairing_id, source_id),
            )
            if result.rowcount == 0:
                raise PocketError(404, "配对记录不存在")

    def collector_handshake(
        self,
        source_id: str,
        pairing_code: str,
        client: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now()
        collector_token = f"cp_collector_{secrets.token_urlsafe(32)}"
        token_id = new_id("ctok")
        with self.database.transaction() as connection:
            self._require_observer_source(
                connection, source_id, require_enabled=True
            )
            pairing = connection.execute(
                """
                SELECT * FROM collector_pairings
                WHERE source_id = ? AND code_hash = ?
                """,
                (source_id, self._secret_hash(pairing_code)),
            ).fetchone()
            if (
                pairing is None
                or pairing["used_at"] is not None
                or pairing["revoked_at"] is not None
            ):
                raise PocketError(401, "配对凭据无效")
            if datetime.fromisoformat(pairing["expires_at"]) <= datetime.now(UTC):
                raise PocketError(401, "配对凭据已过期")
            connection.execute(
                "UPDATE collector_pairings SET used_at = ? WHERE id = ?",
                (now, pairing["id"]),
            )
            # One source has one active local observer. Re-pairing is an
            # intentional credential rotation and revokes every older token.
            connection.execute(
                """
                UPDATE collector_tokens SET revoked_at = ?
                WHERE source_id = ? AND revoked_at IS NULL
                """,
                (now, source_id),
            )
            connection.execute(
                """
                INSERT INTO collector_tokens(
                    id, source_id, token_hash, client_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    token_id,
                    source_id,
                    self._secret_hash(collector_token),
                    json.dumps(client, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO collector_rate_limits(
                    token_id, window_started_at, request_count
                ) VALUES (?, ?, 0)
                """,
                (token_id, now),
            )
            self._record_activity(
                connection,
                kind="observer.paired",
                message="微信网页观察器已完成本机配对",
                resource_type="source",
                resource_id=source_id,
            )
        # Like the pairing code, this bearer credential is returned exactly
        # once. Status/list endpoints never expose it or its stored hash.
        return {
            "source_id": source_id,
            "collector_token": collector_token,
            "token_type": "Bearer",
        }

    def authenticate_collector(
        self, source_id: str, collector_token: str
    ) -> dict[str, str]:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.database.transaction() as connection:
            token = connection.execute(
                """
                SELECT token.id, token.source_id, token.revoked_at, source.enabled,
                       source.kind
                FROM collector_tokens token
                JOIN sources source ON source.id = token.source_id
                WHERE token.token_hash = ?
                """,
                (self._secret_hash(collector_token),),
            ).fetchone()
            if (
                token is None
                or token["source_id"] != source_id
                or token["revoked_at"] is not None
                or token["kind"] != "wechat_visible_web"
            ):
                raise PocketError(401, "Collector 凭据无效")
            if not token["enabled"]:
                raise PocketError(409, "观察器已暂停")

            rate = connection.execute(
                "SELECT * FROM collector_rate_limits WHERE token_id = ?",
                (token["id"],),
            ).fetchone()
            if rate is None:
                count = 1
                window_started_at = now
            else:
                window = datetime.fromisoformat(rate["window_started_at"])
                if now_dt - window >= timedelta(minutes=1):
                    count = 1
                    window_started_at = now
                else:
                    count = int(rate["request_count"]) + 1
                    window_started_at = rate["window_started_at"]
            if count > COLLECTOR_REQUESTS_PER_MINUTE:
                raise PocketError(429, "Collector 请求过于频繁，请稍后重试")
            connection.execute(
                """
                INSERT INTO collector_rate_limits(
                    token_id, window_started_at, request_count
                ) VALUES (?, ?, ?)
                ON CONFLICT(token_id) DO UPDATE SET
                    window_started_at = excluded.window_started_at,
                    request_count = excluded.request_count
                """,
                (token["id"], window_started_at, count),
            )
            connection.execute(
                "UPDATE collector_tokens SET last_used_at = ? WHERE id = ?",
                (now, token["id"]),
            )
            return {"token_id": token["id"], "source_id": source_id}

    def record_observer_heartbeat(
        self,
        source_id: str,
        collector: dict[str, str],
        heartbeat: dict[str, Any],
    ) -> dict[str, Any]:
        del collector  # Authentication is intentionally separate from payload data.
        received_at = utc_now()
        self._iso_utc(heartbeat.get("observed_at"), fallback=received_at)
        session_key = heartbeat["browser_session_id"]
        state = heartbeat["state"]
        with self.database.transaction() as connection:
            self._require_observer_source(
                connection, source_id, require_enabled=True
            )
            previous = connection.execute(
                """
                SELECT * FROM source_coverage_sessions
                WHERE source_id = ? AND browser_session_id = ?
                """,
                (source_id, session_key),
            ).fetchone()
            session_id = previous["id"] if previous else new_id("cov")
            if previous is not None:
                previous_heartbeat = datetime.fromisoformat(
                    previous["last_heartbeat_at"]
                )
                current_received = datetime.fromisoformat(received_at)
                if (
                    current_received - previous_heartbeat
                    > timedelta(seconds=OBSERVER_HEARTBEAT_GAP_SECONDS)
                ):
                    self._insert_gap(
                        connection,
                        source_id=source_id,
                        session_id=session_id,
                        kind="heartbeat_missing",
                        started_at=previous["last_heartbeat_at"],
                        ended_at=received_at,
                    )
            connection.execute(
                """
                INSERT INTO source_coverage_sessions(
                    id, source_id, browser_session_id, state, browser_version,
                    extension_version, parser_version,
                    current_conversation_id, current_conversation_name,
                    unread_conversation_count, started_at, last_heartbeat_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, browser_session_id) DO UPDATE SET
                    state = excluded.state,
                    browser_version = excluded.browser_version,
                    extension_version = excluded.extension_version,
                    parser_version = excluded.parser_version,
                    current_conversation_id = excluded.current_conversation_id,
                    current_conversation_name = excluded.current_conversation_name,
                    unread_conversation_count = excluded.unread_conversation_count,
                    last_heartbeat_at = excluded.last_heartbeat_at,
                    ended_at = NULL
                """,
                (
                    session_id,
                    source_id,
                    session_key,
                    state,
                    heartbeat.get("browser_version"),
                    heartbeat["extension_version"],
                    heartbeat["parser_version"],
                    heartbeat.get("current_conversation_id"),
                    heartbeat.get("current_conversation_name"),
                    heartbeat.get("unread_conversation_count", 0),
                    received_at,
                    received_at,
                ),
            )
            self._update_state_gaps(
                connection,
                source_id=source_id,
                session_id=session_id,
                state=state,
                unread_count=heartbeat.get("unread_conversation_count", 0),
                now=received_at,
            )
            row = connection.execute(
                "SELECT * FROM source_coverage_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            return self._coverage_session_to_dict(row)

    @staticmethod
    def _insert_gap(
        connection: sqlite3.Connection,
        *,
        source_id: str,
        session_id: str | None,
        kind: str,
        started_at: str,
        ended_at: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO source_gaps(
                id, source_id, coverage_session_id, kind, started_at,
                ended_at, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("gap"),
                source_id,
                session_id,
                kind,
                started_at,
                ended_at,
                json.dumps(details or {}, ensure_ascii=False),
                utc_now(),
            ),
        )

    def _update_state_gaps(
        self,
        connection: sqlite3.Connection,
        *,
        source_id: str,
        session_id: str,
        state: str,
        unread_count: int,
        now: str,
    ) -> None:
        tracked_states = {
            "login_required",
            "awaiting_phone_confirm",
            "capture_paused",
            "browser_offline",
            "parser_degraded",
            "account_rejected",
        }
        open_rows = connection.execute(
            """
            SELECT id, kind FROM source_gaps
            WHERE source_id = ? AND coverage_session_id = ? AND ended_at IS NULL
            """,
            (source_id, session_id),
        ).fetchall()
        desired = ({state} if state in tracked_states else set()) | (
            {"unopened_conversations"} if unread_count else set()
        )
        open_kinds = {row["kind"] for row in open_rows}
        for row in open_rows:
            if row["kind"] not in desired:
                connection.execute(
                    "UPDATE source_gaps SET ended_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
        for kind in desired - open_kinds:
            details = (
                {"unread_conversation_count": unread_count}
                if kind == "unopened_conversations"
                else {}
            )
            self._insert_gap(
                connection,
                source_id=source_id,
                session_id=session_id,
                kind=kind,
                started_at=now,
                details=details,
            )
        if "unopened_conversations" in desired & open_kinds:
            connection.execute(
                """
                UPDATE source_gaps SET details_json = ?
                WHERE source_id = ? AND coverage_session_id = ?
                  AND kind = 'unopened_conversations' AND ended_at IS NULL
                """,
                (
                    json.dumps(
                        {"unread_conversation_count": unread_count},
                        ensure_ascii=False,
                    ),
                    source_id,
                    session_id,
                ),
            )

    def ingest_observer_events(
        self,
        source_id: str,
        collector: dict[str, str],
        batch: dict[str, Any],
    ) -> dict[str, Any]:
        canonical_payload = json.dumps(
            batch, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        payload_hash = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
        received_at = utc_now()
        with self.database.transaction() as connection:
            self._require_observer_source(
                connection, source_id, require_enabled=True
            )
            cached = connection.execute(
                """
                SELECT payload_hash, response_json FROM collector_batches
                WHERE source_id = ? AND batch_id = ?
                """,
                (source_id, batch["batch_id"]),
            ).fetchone()
            if cached is not None:
                if not secrets.compare_digest(cached["payload_hash"], payload_hash):
                    raise PocketError(409, "同一 batch_id 不能提交不同内容")
                return json_loads(cached["response_json"], {})

            session = connection.execute(
                """
                SELECT id FROM source_coverage_sessions
                WHERE source_id = ? AND browser_session_id = ?
                """,
                (source_id, batch["browser_session_id"]),
            ).fetchone()
            if session is None:
                raise PocketError(409, "发送消息前必须先提交该浏览器会话的 heartbeat")

            accepted = 0
            duplicates = 0
            for event in batch["events"]:
                observed_at = self._iso_utc(event["observed_at"])
                sent_at = (
                    self._iso_utc(event["sent_at"])
                    if event.get("sent_at") is not None
                    else None
                )
                event_id = new_id("ing")
                inserted = connection.execute(
                    """
                    INSERT OR IGNORE INTO ingest_events(
                        id, source_id, collector_token_id, provider_event_key,
                        event_type, payload_json, observed_at, received_at
                    ) VALUES (?, ?, ?, ?, 'message', ?, ?, ?)
                    """,
                    (
                        event_id,
                        source_id,
                        collector["token_id"],
                        f"message:{event['provider_msgid']}",
                        json.dumps(event, ensure_ascii=False, sort_keys=True),
                        observed_at,
                        received_at,
                    ),
                )
                if inserted.rowcount == 0:
                    duplicates += 1
                    continue

                conversation = connection.execute(
                    """
                    SELECT id FROM im_conversations
                    WHERE source_id = ? AND provider_conversation_id = ?
                    """,
                    (source_id, event["provider_conversation_id"]),
                ).fetchone()
                if conversation is None:
                    conversation_id = new_id("conv")
                    connection.execute(
                        """
                        INSERT INTO im_conversations(
                            id, source_id, provider_conversation_id, display_name,
                            conversation_type, first_observed_at, last_observed_at,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            conversation_id,
                            source_id,
                            event["provider_conversation_id"],
                            event.get("conversation_name"),
                            event["conversation_type"],
                            observed_at,
                            observed_at,
                            received_at,
                            received_at,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO conversation_policies(
                            conversation_id, created_at, updated_at
                        ) VALUES (?, ?, ?)
                        """,
                        (conversation_id, received_at, received_at),
                    )
                else:
                    conversation_id = conversation["id"]
                    connection.execute(
                        """
                        UPDATE im_conversations
                        SET display_name = COALESCE(?, display_name),
                            conversation_type = CASE
                                WHEN ? = 'unknown' THEN conversation_type
                                ELSE ?
                            END,
                            last_observed_at = CASE
                                WHEN last_observed_at < ? THEN ?
                                ELSE last_observed_at
                            END,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            event.get("conversation_name"),
                            event["conversation_type"],
                            event["conversation_type"],
                            observed_at,
                            observed_at,
                            received_at,
                            conversation_id,
                        ),
                    )

                text_content = event.get("text")
                content_hash = (
                    hashlib.sha256(text_content.encode("utf-8")).hexdigest()
                    if text_content is not None
                    else None
                )
                message_id = new_id("msg")
                connection.execute(
                    """
                    INSERT INTO im_messages(
                        id, source_id, conversation_id, ingest_event_id,
                        provider_msgid, sender_provider_id, sender_display_name,
                        direction, message_type, text_content, content_hash,
                        displayed_time_text, sent_at, observed_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        source_id,
                        conversation_id,
                        event_id,
                        event["provider_msgid"],
                        event.get("sender_provider_id"),
                        event.get("sender_display_name"),
                        event["direction"],
                        event["message_type"],
                        text_content,
                        content_hash,
                        event.get("displayed_time_text"),
                        sent_at,
                        observed_at,
                        received_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO im_message_versions(
                        id, message_id, event_type, text_content,
                        payload_json, observed_at, created_at
                    ) VALUES (?, ?, 'created', ?, ?, ?, ?)
                    """,
                    (
                        new_id("mver"),
                        message_id,
                        text_content,
                        json.dumps(event, ensure_ascii=False, sort_keys=True),
                        observed_at,
                        received_at,
                    ),
                )
                sender_provider_id = event.get("sender_provider_id")
                if sender_provider_id:
                    identity = connection.execute(
                        """
                        SELECT id FROM im_identities
                        WHERE source_id = ? AND provider_identity_id = ?
                        """,
                        (source_id, sender_provider_id),
                    ).fetchone()
                    identity_id = identity["id"] if identity else new_id("ident")
                    connection.execute(
                        """
                        INSERT INTO im_identities(
                            id, source_id, provider_identity_id, display_name,
                            first_observed_at, last_observed_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source_id, provider_identity_id) DO UPDATE SET
                            display_name = COALESCE(excluded.display_name, display_name),
                            last_observed_at = excluded.last_observed_at,
                            updated_at = excluded.updated_at
                        """,
                        (
                            identity_id,
                            source_id,
                            sender_provider_id,
                            event.get("sender_display_name"),
                            observed_at,
                            observed_at,
                            received_at,
                            received_at,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO im_conversation_members(
                            conversation_id, identity_id, display_name,
                            first_observed_at, last_observed_at
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(conversation_id, identity_id) DO UPDATE SET
                            display_name = COALESCE(excluded.display_name, display_name),
                            last_observed_at = excluded.last_observed_at
                        """,
                        (
                            conversation_id,
                            identity_id,
                            event.get("sender_display_name"),
                            observed_at,
                            observed_at,
                        ),
                    )

                knowledge_messages = [
                    {
                        "id": message_id,
                        "conversation_id": conversation_id,
                        "sender_display_name": event.get("sender_display_name"),
                        "text_content": text_content,
                    }
                ]
                for candidate in extract_knowledge_candidates(knowledge_messages):
                    candidate_id = new_id("claim")
                    inserted_candidate = connection.execute(
                        """
                        INSERT OR IGNORE INTO knowledge_candidates(
                            id, idempotency_key, conversation_id, claim_type,
                            text_content, speaker, explicitness, authority,
                            confidence, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'observed', ?,
                                  'provisional', ?, ?)
                        """,
                        (
                            candidate_id,
                            candidate.idempotency_key,
                            candidate.conversation_id,
                            candidate.claim_type,
                            candidate.text,
                            candidate.speaker,
                            candidate.explicitness,
                            candidate.confidence,
                            received_at,
                            received_at,
                        ),
                    )
                    if inserted_candidate.rowcount:
                        connection.execute(
                            """
                            INSERT INTO knowledge_evidence(
                                candidate_id, message_id, evidence_role, excerpt
                            ) VALUES (?, ?, 'primary', ?)
                            """,
                            (candidate_id, message_id, candidate.text[:500]),
                        )
                accepted += 1

            response = {
                "batch_id": batch["batch_id"],
                "accepted_count": accepted,
                "duplicate_count": duplicates,
                "total_count": len(batch["events"]),
                "received_at": received_at,
            }
            connection.execute(
                """
                INSERT INTO collector_batches(
                    source_id, batch_id, payload_hash, response_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    batch["batch_id"],
                    payload_hash,
                    json.dumps(response, ensure_ascii=False),
                    received_at,
                ),
            )
            connection.execute(
                "UPDATE sources SET last_sync_at = ?, updated_at = ? WHERE id = ?",
                (received_at, received_at, source_id),
            )
            return response

    def set_observer_enabled(self, source_id: str, *, enabled: bool) -> dict[str, Any]:
        now = utc_now()
        with self.database.transaction() as connection:
            self._require_observer_source(connection, source_id)
            connection.execute(
                "UPDATE sources SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), now, source_id),
            )
            latest = connection.execute(
                """
                SELECT id FROM source_coverage_sessions
                WHERE source_id = ? ORDER BY last_heartbeat_at DESC LIMIT 1
                """,
                (source_id,),
            ).fetchone()
            if latest is not None:
                if enabled:
                    connection.execute(
                        """
                        UPDATE source_gaps SET ended_at = ?
                        WHERE source_id = ? AND kind = 'capture_paused'
                          AND ended_at IS NULL
                        """,
                        (now, source_id),
                    )
                else:
                    existing = connection.execute(
                        """
                        SELECT 1 FROM source_gaps
                        WHERE source_id = ? AND kind = 'capture_paused'
                          AND ended_at IS NULL
                        """,
                        (source_id,),
                    ).fetchone()
                    if existing is None:
                        self._insert_gap(
                            connection,
                            source_id=source_id,
                            session_id=latest["id"],
                            kind="capture_paused",
                            started_at=now,
                        )
            self._record_activity(
                connection,
                kind="observer.resumed" if enabled else "observer.paused",
                message="已恢复微信网页观察器" if enabled else "已暂停微信网页观察器",
                resource_type="source",
                resource_id=source_id,
            )
            return self._get_source(connection, source_id)

    def observer_status(self, source_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            source = self._require_observer_source(connection, source_id)
            latest = connection.execute(
                """
                SELECT * FROM source_coverage_sessions
                WHERE source_id = ? ORDER BY last_heartbeat_at DESC LIMIT 1
                """,
                (source_id,),
            ).fetchone()
            active_pairing = connection.execute(
                """
                SELECT 1 FROM collector_pairings
                WHERE source_id = ? AND used_at IS NULL AND revoked_at IS NULL
                  AND expires_at > ? LIMIT 1
                """,
                (source_id, utc_now()),
            ).fetchone()
            active_token = connection.execute(
                """
                SELECT 1 FROM collector_tokens
                WHERE source_id = ? AND revoked_at IS NULL LIMIT 1
                """,
                (source_id,),
            ).fetchone()
            counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM im_conversations WHERE source_id = ?) AS conversations,
                    (SELECT COUNT(*) FROM im_messages WHERE source_id = ?) AS messages,
                    (SELECT COUNT(*) FROM source_gaps
                        WHERE source_id = ? AND ended_at IS NULL) AS open_gaps
                """,
                (source_id, source_id, source_id),
            ).fetchone()

        stale = False
        session = self._coverage_session_to_dict(latest) if latest else None
        if latest is not None:
            stale = datetime.now(UTC) - datetime.fromisoformat(
                latest["last_heartbeat_at"]
            ) > timedelta(seconds=OBSERVER_HEARTBEAT_GAP_SECONDS)
        if not source["enabled"]:
            state = "capture_paused"
        elif latest is not None and not stale:
            state = latest["state"]
        elif latest is not None:
            state = "browser_offline"
        elif active_token is not None:
            state = "login_required"
        elif active_pairing is not None:
            state = "awaiting_pairing"
        else:
            state = "extension_missing"
        return {
            "source_id": source_id,
            "state": state,
            "enabled": bool(source["enabled"]),
            "paired": active_token is not None,
            "heartbeat_stale": stale,
            "last_event_at": source["last_sync_at"],
            "last_session": session,
            "conversation_count": counts["conversations"],
            "message_count": counts["messages"],
            "open_gap_count": counts["open_gaps"],
            "coverage_notice": "仅覆盖微信网页已经实际渲染的内容",
        }

    def list_observer_gaps(
        self, source_id: str, *, limit: int, offset: int
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            self._require_observer_source(connection, source_id)
            total = connection.execute(
                "SELECT COUNT(*) FROM source_gaps WHERE source_id = ?",
                (source_id,),
            ).fetchone()[0]
            rows = connection.execute(
                """
                SELECT * FROM source_gaps WHERE source_id = ?
                ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?
                """,
                (source_id, limit, offset),
            ).fetchall()
            items = [
                {
                    "id": row["id"],
                    "source_id": row["source_id"],
                    "kind": row["kind"],
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "details": json_loads(row["details_json"], {}),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
            return {"items": items, "total": total, "limit": limit, "offset": offset}

    @staticmethod
    def _coverage_session_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "browser_session_id": row["browser_session_id"],
            "state": row["state"],
            "browser_version": row["browser_version"],
            "extension_version": row["extension_version"],
            "parser_version": row["parser_version"],
            "current_conversation_id": row["current_conversation_id"],
            "current_conversation_name": row["current_conversation_name"],
            "unread_conversation_count": row["unread_conversation_count"],
            "started_at": row["started_at"],
            "last_heartbeat_at": row["last_heartbeat_at"],
            "ended_at": row["ended_at"],
        }

    def list_im_conversations(
        self, *, source_id: str | None, limit: int, offset: int
    ) -> dict[str, Any]:
        where = "WHERE conversation.source_id = ?" if source_id else ""
        params: list[Any] = [source_id] if source_id else []
        with self.database.connect() as connection:
            if source_id:
                self._require_observer_source(connection, source_id)
            total = connection.execute(
                f"SELECT COUNT(*) FROM im_conversations conversation {where}",
                params,
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT conversation.*, policy.agent_enabled, policy.retention_days,
                       (SELECT COUNT(*) FROM im_messages message
                        WHERE message.conversation_id = conversation.id) AS message_count
                FROM im_conversations conversation
                JOIN conversation_policies policy
                  ON policy.conversation_id = conversation.id
                {where}
                ORDER BY conversation.last_observed_at DESC, conversation.id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
            return {
                "items": [self._conversation_to_dict(row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    def get_im_messages(
        self, conversation_id: str, *, limit: int, offset: int
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            conversation = connection.execute(
                "SELECT id FROM im_conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if conversation is None:
                raise PocketError(404, "会话不存在")
            total = connection.execute(
                "SELECT COUNT(*) FROM im_messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()[0]
            rows = connection.execute(
                """
                SELECT * FROM im_messages WHERE conversation_id = ?
                ORDER BY observed_at DESC, id DESC LIMIT ? OFFSET ?
                """,
                (conversation_id, limit, offset),
            ).fetchall()
            items = [
                {
                    "id": row["id"],
                    "conversation_id": row["conversation_id"],
                    "provider_msgid": row["provider_msgid"],
                    "sender_provider_id": row["sender_provider_id"],
                    "sender_display_name": row["sender_display_name"],
                    "direction": row["direction"],
                    "message_type": row["message_type"],
                    "text": row["text_content"],
                    "displayed_time_text": row["displayed_time_text"],
                    "sent_at": row["sent_at"],
                    "observed_at": row["observed_at"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
            return {"items": items, "total": total, "limit": limit, "offset": offset}

    def update_conversation_policy(
        self, conversation_id: str, updates: dict[str, Any]
    ) -> dict[str, Any]:
        with self.database.transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM im_conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if exists is None:
                raise PocketError(404, "会话不存在")
            values: dict[str, Any] = {}
            if "agent_enabled" in updates:
                values["agent_enabled"] = int(updates["agent_enabled"])
            if "retention_days" in updates:
                values["retention_days"] = updates["retention_days"]
            values["updated_at"] = utc_now()
            assignments = ", ".join(f"{key} = ?" for key in values)
            connection.execute(
                f"UPDATE conversation_policies SET {assignments} WHERE conversation_id = ?",
                (*values.values(), conversation_id),
            )
            row = connection.execute(
                """
                SELECT conversation.*, policy.agent_enabled, policy.retention_days,
                       (SELECT COUNT(*) FROM im_messages message
                        WHERE message.conversation_id = conversation.id) AS message_count
                FROM im_conversations conversation
                JOIN conversation_policies policy
                  ON policy.conversation_id = conversation.id
                WHERE conversation.id = ?
                """,
                (conversation_id,),
            ).fetchone()
            return self._conversation_to_dict(row)

    def list_knowledge_candidates(
        self,
        *,
        status: str | None,
        conversation_id: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if status:
            clauses.append("candidate.status = ?")
            parameters.append(status)
        if conversation_id:
            clauses.append("candidate.conversation_id = ?")
            parameters.append(conversation_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.database.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM knowledge_candidates candidate {where}",
                parameters,
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT candidate.*, conversation.display_name AS conversation_name,
                       conversation.source_id, source.name AS source_name,
                       policy.agent_enabled
                FROM knowledge_candidates candidate
                JOIN im_conversations conversation
                  ON conversation.id = candidate.conversation_id
                JOIN sources source ON source.id = conversation.source_id
                JOIN conversation_policies policy
                  ON policy.conversation_id = conversation.id
                {where}
                ORDER BY candidate.updated_at DESC, candidate.id DESC
                LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
            items = [
                self._knowledge_candidate_to_dict(connection, row) for row in rows
            ]
            return {"items": items, "total": total, "limit": limit, "offset": offset}

    def resolve_knowledge_candidate(
        self, candidate_id: str, *, action: str
    ) -> dict[str, Any]:
        if action not in {"confirm", "dismiss"}:
            raise PocketError(422, "不支持的知识确认动作")
        target_status = "confirmed" if action == "confirm" else "dismissed"
        now = utc_now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise PocketError(404, "知识候选不存在")
            if row["status"] != "provisional":
                if row["status"] == target_status:
                    return self._get_knowledge_candidate(connection, candidate_id)
                raise PocketError(409, "该知识候选已经处理")
            connection.execute(
                """
                UPDATE knowledge_candidates
                SET status = ?, resolved_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (target_status, now, now, candidate_id),
            )
            self._record_activity(
                connection,
                kind=f"knowledge.{target_status}",
                message=(
                    "已确认一条聊天知识结论"
                    if target_status == "confirmed"
                    else "已忽略一条聊天知识候选"
                ),
                resource_type="knowledge_candidate",
                resource_id=candidate_id,
            )
            return self._get_knowledge_candidate(connection, candidate_id)

    def retention_preview(self) -> dict[str, Any]:
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT conversation.id AS conversation_id,
                       conversation.display_name AS conversation_name,
                       conversation.source_id, source.name AS source_name,
                       policy.retention_days, COUNT(*) AS eligible_count
                {self._retention_candidate_sql()}
                GROUP BY conversation.id
                ORDER BY eligible_count DESC, conversation.id
                """
            ).fetchall()
            protected_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM im_messages message
                JOIN im_conversations conversation
                  ON conversation.id = message.conversation_id
                JOIN conversation_policies policy
                  ON policy.conversation_id = conversation.id
                WHERE julianday(COALESCE(message.sent_at, message.observed_at))
                      < julianday('now', '-' || policy.retention_days || ' days')
                  AND EXISTS (
                      SELECT 1
                      FROM knowledge_evidence evidence
                      JOIN knowledge_candidates candidate
                        ON candidate.id = evidence.candidate_id
                      WHERE evidence.message_id = message.id
                        AND candidate.status = 'confirmed'
                  )
                """
            ).fetchone()[0]
        conversations = [
            {
                "conversation_id": row["conversation_id"],
                "conversation_name": row["conversation_name"],
                "source_id": row["source_id"],
                "source_name": row["source_name"],
                "retention_days": row["retention_days"],
                "eligible_count": row["eligible_count"],
            }
            for row in rows
        ]
        return {
            "eligible_message_count": sum(
                item["eligible_count"] for item in conversations
            ),
            "protected_evidence_count": protected_count,
            "conversations": conversations,
            "mode": "preview",
        }

    def apply_retention(self) -> dict[str, Any]:
        preview = self.retention_preview()
        now = utc_now()
        with self.database.transaction() as connection:
            deleted_messages = connection.execute(
                f"""
                DELETE FROM im_messages
                WHERE id IN (
                    SELECT message.id {self._retention_candidate_sql()}
                )
                """
            ).rowcount
            deleted_candidates = connection.execute(
                """
                DELETE FROM knowledge_candidates
                WHERE status != 'confirmed'
                  AND NOT EXISTS (
                      SELECT 1 FROM knowledge_evidence evidence
                      WHERE evidence.candidate_id = knowledge_candidates.id
                  )
                """
            ).rowcount
            deleted_events = connection.execute(
                """
                DELETE FROM ingest_events
                WHERE event_type = 'message'
                  AND NOT EXISTS (
                      SELECT 1 FROM im_messages message
                      WHERE message.ingest_event_id = ingest_events.id
                  )
                """
            ).rowcount
            deleted_memberships = connection.execute(
                """
                DELETE FROM im_conversation_members
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM im_identities identity
                    JOIN im_messages message
                      ON message.source_id = identity.source_id
                     AND message.sender_provider_id = identity.provider_identity_id
                    WHERE identity.id = im_conversation_members.identity_id
                      AND message.conversation_id =
                          im_conversation_members.conversation_id
                )
                """
            ).rowcount
            deleted_identities = connection.execute(
                """
                DELETE FROM im_identities
                WHERE NOT EXISTS (
                    SELECT 1 FROM im_conversation_members member
                    WHERE member.identity_id = im_identities.id
                )
                """
            ).rowcount
            self._record_activity(
                connection,
                kind="im.retention_applied",
                message=f"已按会话保留策略清理 {deleted_messages} 条过期消息",
                resource_type="im_retention",
                details={
                    "deleted_messages": deleted_messages,
                    "deleted_candidates": deleted_candidates,
                    "deleted_ingest_events": deleted_events,
                    "deleted_memberships": deleted_memberships,
                    "deleted_identities": deleted_identities,
                    "protected_evidence_count": preview["protected_evidence_count"],
                },
            )
        return {
            **preview,
            "mode": "applied",
            "deleted_message_count": deleted_messages,
            "deleted_candidate_count": deleted_candidates,
            "deleted_ingest_event_count": deleted_events,
            "deleted_membership_count": deleted_memberships,
            "deleted_identity_count": deleted_identities,
            "applied_at": now,
        }

    @staticmethod
    def _retention_candidate_sql() -> str:
        return """
            FROM im_messages message
            JOIN im_conversations conversation
              ON conversation.id = message.conversation_id
            JOIN conversation_policies policy
              ON policy.conversation_id = conversation.id
            JOIN sources source ON source.id = conversation.source_id
            WHERE julianday(COALESCE(message.sent_at, message.observed_at))
                  < julianday('now', '-' || policy.retention_days || ' days')
              AND NOT EXISTS (
                  SELECT 1
                  FROM knowledge_evidence evidence
                  JOIN knowledge_candidates candidate
                    ON candidate.id = evidence.candidate_id
                  WHERE evidence.message_id = message.id
                    AND candidate.status = 'confirmed'
              )
        """

    def _get_knowledge_candidate(
        self, connection: sqlite3.Connection, candidate_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT candidate.*, conversation.display_name AS conversation_name,
                   conversation.source_id, source.name AS source_name,
                   policy.agent_enabled
            FROM knowledge_candidates candidate
            JOIN im_conversations conversation
              ON conversation.id = candidate.conversation_id
            JOIN sources source ON source.id = conversation.source_id
            JOIN conversation_policies policy
              ON policy.conversation_id = conversation.id
            WHERE candidate.id = ?
            """,
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise PocketError(404, "知识候选不存在")
        return self._knowledge_candidate_to_dict(connection, row)

    @staticmethod
    def _knowledge_candidate_to_dict(
        connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        evidence_rows = connection.execute(
            """
            SELECT evidence.message_id, evidence.evidence_role, evidence.excerpt,
                   message.provider_msgid, message.sender_display_name,
                   message.sent_at, message.observed_at, message.authority
            FROM knowledge_evidence evidence
            JOIN im_messages message ON message.id = evidence.message_id
            WHERE evidence.candidate_id = ?
            ORDER BY message.observed_at, message.id
            """,
            (row["id"],),
        ).fetchall()
        return {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "conversation_name": row["conversation_name"],
            "source_id": row["source_id"],
            "source_name": row["source_name"],
            "claim_type": row["claim_type"],
            "text": row["text_content"],
            "speaker": row["speaker"],
            "explicitness": row["explicitness"],
            "authority": row["authority"],
            "confidence": row["confidence"],
            "status": row["status"],
            "agent_enabled": bool(row["agent_enabled"]),
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
            "evidence": [
                {
                    "message_id": evidence["message_id"],
                    "provider_msgid": evidence["provider_msgid"],
                    "role": evidence["evidence_role"],
                    "excerpt": evidence["excerpt"],
                    "speaker": evidence["sender_display_name"],
                    "sent_at": evidence["sent_at"],
                    "observed_at": evidence["observed_at"],
                    "authority": evidence["authority"],
                }
                for evidence in evidence_rows
            ],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "resolved_at": row["resolved_at"],
        }

    @staticmethod
    def _conversation_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "source_id": row["source_id"],
            "provider_conversation_id": row["provider_conversation_id"],
            "display_name": row["display_name"],
            "conversation_type": row["conversation_type"],
            "message_count": row["message_count"],
            "first_observed_at": row["first_observed_at"],
            "last_observed_at": row["last_observed_at"],
            "policy": {
                "agent_enabled": bool(row["agent_enabled"]),
                "retention_days": row["retention_days"],
            },
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
        if source["kind"] != "folder":
            raise PocketError(409, "网页观察器通过 Collector 持续接收事件，不能目录同步")
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
                WHERE source.enabled = 1
                  AND source.kind = 'folder'
                  AND source.schedule != 'manual'
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
        source_ids: list[str] | None = None,
        conversation_ids: list[str] | None = None,
        participant_ids: list[str] | None = None,
        sent_from: datetime | None = None,
        sent_to: datetime | None = None,
        item_kinds: list[str] | None = None,
    ) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise PocketError(422, "query 不能为空")
        source_ids = source_ids or []
        conversation_ids = conversation_ids or []
        participant_ids = participant_ids or []
        requested_kinds = set(item_kinds or ["document", "im_message", "knowledge"])
        im_specific_filters = bool(
            conversation_ids or participant_ids or sent_from is not None or sent_to is not None
        )
        include_documents = "document" in requested_kinds and not im_specific_filters
        include_im = not tags and category is None
        tokens = re.findall(r"\w+", query, flags=re.UNICODE)
        candidates: list[dict[str, Any]] = []
        with self.database.connect() as connection:
            if include_documents and tokens:
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
                        active_link.source_id AS active_source_id,
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

            if include_documents and not candidates:
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
                        active_link.source_id AS active_source_id,
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

            if source_ids:
                allowed_source_ids = set(source_ids)
                candidates = [
                    candidate
                    for candidate in candidates
                    if candidate.get("source_id") in allowed_source_ids
                ]

            if include_im and "im_message" in requested_kinds:
                candidates.extend(
                    self._search_im_messages(
                        connection,
                        query=query,
                        tokens=tokens,
                        limit=min(limit * 8, 200),
                        source_ids=source_ids,
                        conversation_ids=conversation_ids,
                        participant_ids=participant_ids,
                        sent_from=sent_from,
                        sent_to=sent_to,
                    )
                )
            if include_im and "knowledge" in requested_kinds:
                candidates.extend(
                    self._search_confirmed_knowledge(
                        connection,
                        query=query,
                        tokens=tokens,
                        limit=min(limit * 8, 200),
                        source_ids=source_ids,
                        conversation_ids=conversation_ids,
                        participant_ids=participant_ids,
                        sent_from=sent_from,
                        sent_to=sent_to,
                    )
                )

        required_tags = {tag.casefold() for tag in tags if tag.strip()}
        if required_tags:
            candidates = [
                candidate
                for candidate in candidates
                if required_tags.issubset({tag.casefold() for tag in candidate["tags"]})
            ]
        candidates.sort(
            key=lambda candidate: (
                candidate["score"],
                candidate.get("updated_at")
                or candidate.get("sent_at")
                or candidate.get("observed_at")
                or "",
            ),
            reverse=True,
        )
        results = candidates[:limit]
        includes_opted_in_im = any(
            result.get("kind") in {"im_message", "knowledge"} for result in results
        )
        return {
            "query": query,
            "results": results,
            "count": len(results),
            "visibility": (
                "ready_and_opted_in_im" if includes_opted_in_im else "ready_only"
            ),
        }

    def _search_im_messages(
        self,
        connection: sqlite3.Connection,
        *,
        query: str,
        tokens: list[str],
        limit: int,
        source_ids: list[str],
        conversation_ids: list[str],
        participant_ids: list[str],
        sent_from: datetime | None,
        sent_to: datetime | None,
    ) -> list[dict[str, Any]]:
        clauses, filter_parameters = self._im_filter_clauses(
            source_ids=source_ids,
            conversation_ids=conversation_ids,
            participant_ids=participant_ids,
            sent_from=sent_from,
            sent_to=sent_to,
            message_alias="message",
            conversation_alias="conversation",
        )
        where_filters = " ".join(f"AND {clause}" for clause in clauses)
        rows: list[sqlite3.Row] = []
        if tokens:
            match_query = " OR ".join(
                f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens
            )
            rows = connection.execute(
                f"""
                SELECT message.*, conversation.display_name AS conversation_name,
                       source.name AS source_name,
                       bm25(im_message_fts) AS fts_rank,
                       snippet(im_message_fts, 1, '', '', ' … ', 28)
                           AS search_snippet
                FROM im_message_fts
                JOIN im_messages message ON message.id = im_message_fts.message_id
                JOIN im_conversations conversation
                  ON conversation.id = message.conversation_id
                JOIN conversation_policies policy
                  ON policy.conversation_id = conversation.id
                JOIN sources source ON source.id = conversation.source_id
                WHERE im_message_fts MATCH ?
                  AND policy.agent_enabled = 1
                  {where_filters}
                ORDER BY bm25(im_message_fts), message.observed_at DESC
                LIMIT ?
                """,
                (match_query, *filter_parameters, limit),
            ).fetchall()
        if not rows:
            like = f"%{query}%"
            rows = connection.execute(
                f"""
                SELECT message.*, conversation.display_name AS conversation_name,
                       source.name AS source_name, NULL AS fts_rank,
                       substr(message.text_content, 1, 320) AS search_snippet
                FROM im_messages message
                JOIN im_conversations conversation
                  ON conversation.id = message.conversation_id
                JOIN conversation_policies policy
                  ON policy.conversation_id = conversation.id
                JOIN sources source ON source.id = conversation.source_id
                WHERE policy.agent_enabled = 1
                  AND message.text_content LIKE ?
                  {where_filters}
                ORDER BY message.observed_at DESC
                LIMIT ?
                """,
                (like, *filter_parameters, limit),
            ).fetchall()
        return [self._agent_im_message_result(row) for row in rows]

    def _search_confirmed_knowledge(
        self,
        connection: sqlite3.Connection,
        *,
        query: str,
        tokens: list[str],
        limit: int,
        source_ids: list[str],
        conversation_ids: list[str],
        participant_ids: list[str],
        sent_from: datetime | None,
        sent_to: datetime | None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            clauses.append(f"conversation.source_id IN ({placeholders})")
            parameters.extend(source_ids)
        if conversation_ids:
            placeholders = ",".join("?" for _ in conversation_ids)
            clauses.append(f"candidate.conversation_id IN ({placeholders})")
            parameters.extend(conversation_ids)

        evidence_clauses: list[str] = []
        evidence_parameters: list[Any] = []
        if participant_ids:
            placeholders = ",".join("?" for _ in participant_ids)
            evidence_clauses.append(f"evidence_message.sender_provider_id IN ({placeholders})")
            evidence_parameters.extend(participant_ids)
        if sent_from is not None:
            evidence_clauses.append(
                "COALESCE(evidence_message.sent_at, evidence_message.observed_at) >= ?"
            )
            evidence_parameters.append(self._search_time(sent_from))
        if sent_to is not None:
            evidence_clauses.append(
                "COALESCE(evidence_message.sent_at, evidence_message.observed_at) <= ?"
            )
            evidence_parameters.append(self._search_time(sent_to))
        if evidence_clauses:
            clauses.append(
                "EXISTS (SELECT 1 FROM knowledge_evidence evidence "
                "JOIN im_messages evidence_message "
                "ON evidence_message.id = evidence.message_id "
                "WHERE evidence.candidate_id = candidate.id AND "
                + " AND ".join(evidence_clauses)
                + ")"
            )
            parameters.extend(evidence_parameters)

        where_filters = " ".join(f"AND {clause}" for clause in clauses)
        rows: list[sqlite3.Row] = []
        if tokens:
            match_query = " OR ".join(
                f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens
            )
            rows = connection.execute(
                f"""
                SELECT candidate.*, conversation.display_name AS conversation_name,
                       conversation.source_id, source.name AS source_name,
                       bm25(knowledge_fts) AS fts_rank,
                       snippet(knowledge_fts, 1, '', '', ' … ', 28)
                           AS search_snippet
                FROM knowledge_fts
                JOIN knowledge_candidates candidate
                  ON candidate.id = knowledge_fts.candidate_id
                JOIN im_conversations conversation
                  ON conversation.id = candidate.conversation_id
                JOIN conversation_policies policy
                  ON policy.conversation_id = conversation.id
                JOIN sources source ON source.id = conversation.source_id
                WHERE knowledge_fts MATCH ?
                  AND candidate.status = 'confirmed'
                  AND policy.agent_enabled = 1
                  {where_filters}
                ORDER BY bm25(knowledge_fts), candidate.updated_at DESC
                LIMIT ?
                """,
                (match_query, *parameters, limit),
            ).fetchall()
        if not rows:
            like = f"%{query}%"
            rows = connection.execute(
                f"""
                SELECT candidate.*, conversation.display_name AS conversation_name,
                       conversation.source_id, source.name AS source_name,
                       NULL AS fts_rank,
                       substr(candidate.text_content, 1, 320) AS search_snippet
                FROM knowledge_candidates candidate
                JOIN im_conversations conversation
                  ON conversation.id = candidate.conversation_id
                JOIN conversation_policies policy
                  ON policy.conversation_id = conversation.id
                JOIN sources source ON source.id = conversation.source_id
                WHERE candidate.status = 'confirmed'
                  AND policy.agent_enabled = 1
                  AND candidate.text_content LIKE ?
                  {where_filters}
                ORDER BY candidate.updated_at DESC
                LIMIT ?
                """,
                (like, *parameters, limit),
            ).fetchall()
        return [self._agent_knowledge_result(connection, row) for row in rows]

    @staticmethod
    def _im_filter_clauses(
        *,
        source_ids: list[str],
        conversation_ids: list[str],
        participant_ids: list[str],
        sent_from: datetime | None,
        sent_to: datetime | None,
        message_alias: str,
        conversation_alias: str,
    ) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        for values, expression in (
            (source_ids, f"{conversation_alias}.source_id"),
            (conversation_ids, f"{message_alias}.conversation_id"),
            (participant_ids, f"{message_alias}.sender_provider_id"),
        ):
            if values:
                placeholders = ",".join("?" for _ in values)
                clauses.append(f"{expression} IN ({placeholders})")
                parameters.extend(values)
        if sent_from is not None:
            clauses.append(
                f"COALESCE({message_alias}.sent_at, {message_alias}.observed_at) >= ?"
            )
            parameters.append(PocketService._search_time(sent_from))
        if sent_to is not None:
            clauses.append(
                f"COALESCE({message_alias}.sent_at, {message_alias}.observed_at) <= ?"
            )
            parameters.append(PocketService._search_time(sent_to))
        return clauses, parameters

    @staticmethod
    def _search_time(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def _agent_result(row: sqlite3.Row, *, fallback_score: bool) -> dict[str, Any]:
        rank = row["fts_rank"]
        score = 0.5 if fallback_score or rank is None else 1 / (1 + abs(rank))
        snippet = re.sub(r"\s+", " ", row["search_snippet"] or "").strip()
        source_id = row["active_source_id"] or row["first_source_id"]
        source = PocketService._agent_source_label(row)
        metadata = json_loads(row["metadata_json"], {})
        news = metadata.get("news_citation")
        if isinstance(news, dict) and news.get("type") == "web_snapshot":
            evidence = news.get("evidence")
            safe_evidence = []
            if isinstance(evidence, list):
                for point in evidence[:4]:
                    if not isinstance(point, dict):
                        continue
                    safe_evidence.append(
                        {
                            "snapshot_id": point.get("snapshot_id"),
                            "snapshot_hash": point.get("snapshot_hash"),
                            "field": point.get("field"),
                            "start_offset": point.get("start_offset"),
                            "end_offset": point.get("end_offset"),
                            "offset_unit": point.get("offset_unit"),
                            "excerpt": point.get("excerpt"),
                        }
                    )
            citation = {
                "type": "web_snapshot",
                "entry_id": news.get("entry_id"),
                "reliable_source_id": news.get("reliable_source_id"),
                "publisher": news.get("publisher"),
                "url": news.get("url"),
                "url_trust": news.get("url_trust"),
                "published_at": news.get("published_at"),
                "collected_at": news.get("collected_at"),
                "snapshot_id": news.get("snapshot_id"),
                "snapshot_hash": news.get("snapshot_hash"),
                "evidence": safe_evidence,
            }
            return {
                "kind": "document",
                "content_type": "news",
                "item_id": row["id"],
                "title": row["title"],
                "snippet": snippet,
                "source": news.get("publisher") or source,
                "source_id": source_id,
                "score": round(score, 6),
                "authority": "governed",
                "category": row["category"],
                "tags": json_loads(row["tags_json"], []),
                "updated_at": row["updated_at"],
                "citations": [citation],
            }
        return {
            "kind": "document",
            "item_id": row["id"],
            "title": row["title"],
            "snippet": snippet,
            "source": source,
            "source_id": source_id,
            "score": round(score, 6),
            "authority": "governed",
            "category": row["category"],
            "tags": json_loads(row["tags_json"], []),
            "updated_at": row["updated_at"],
            "citations": [
                {
                    "type": "document",
                    "item_id": row["id"],
                    "source": source,
                }
            ],
        }

    @staticmethod
    def _agent_im_message_result(row: sqlite3.Row) -> dict[str, Any]:
        rank = row["fts_rank"]
        score = 0.55 if rank is None else 1 / (1 + abs(rank))
        snippet = re.sub(r"\s+", " ", row["search_snippet"] or "").strip()
        conversation_name = row["conversation_name"] or "未命名会话"
        source = f"{row['source_name']}/{conversation_name}"
        citation = {
            "type": "im_message",
            "message_id": row["id"],
            "provider_msgid": row["provider_msgid"],
            "source_id": row["source_id"],
            "conversation_id": row["conversation_id"],
            "conversation_name": conversation_name,
            "speaker": row["sender_display_name"],
            "sent_at": row["sent_at"],
            "observed_at": row["observed_at"],
            "authority": row["authority"],
        }
        return {
            "kind": "im_message",
            "message_id": row["id"],
            "title": conversation_name,
            "snippet": snippet,
            "source": source,
            "source_id": row["source_id"],
            "conversation_id": row["conversation_id"],
            "conversation_name": conversation_name,
            "speaker": row["sender_display_name"],
            "sent_at": row["sent_at"],
            "observed_at": row["observed_at"],
            "authority": row["authority"],
            "acquisition": row["acquisition"],
            "score": round(score, 6),
            "citations": [citation],
        }

    @staticmethod
    def _agent_knowledge_result(
        connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        rank = row["fts_rank"]
        score = 0.6 if rank is None else 1 / (1 + abs(rank))
        snippet = re.sub(r"\s+", " ", row["search_snippet"] or "").strip()
        conversation_name = row["conversation_name"] or "未命名会话"
        evidence_rows = connection.execute(
            """
            SELECT message.id, message.provider_msgid,
                   message.sender_display_name, message.sent_at,
                   message.observed_at, message.authority,
                   evidence.evidence_role, evidence.excerpt
            FROM knowledge_evidence evidence
            JOIN im_messages message ON message.id = evidence.message_id
            WHERE evidence.candidate_id = ?
            ORDER BY COALESCE(message.sent_at, message.observed_at), message.id
            """,
            (row["id"],),
        ).fetchall()
        citations = [
            {
                "type": "im_message",
                "message_id": evidence["id"],
                "provider_msgid": evidence["provider_msgid"],
                "source_id": row["source_id"],
                "conversation_id": row["conversation_id"],
                "conversation_name": conversation_name,
                "speaker": evidence["sender_display_name"],
                "sent_at": evidence["sent_at"],
                "observed_at": evidence["observed_at"],
                "authority": evidence["authority"],
                "role": evidence["evidence_role"],
                "excerpt": evidence["excerpt"],
            }
            for evidence in evidence_rows
        ]
        return {
            "kind": "knowledge",
            "candidate_id": row["id"],
            "title": {
                "decision": "已确认决策",
                "commitment": "已确认承诺",
                "task": "已确认任务",
            }.get(row["claim_type"], "已确认知识"),
            "snippet": snippet,
            "source": f"{row['source_name']}/{conversation_name}",
            "source_id": row["source_id"],
            "conversation_id": row["conversation_id"],
            "conversation_name": conversation_name,
            "claim_type": row["claim_type"],
            "speaker": row["speaker"],
            "authority": row["authority"],
            "status": row["status"],
            "score": round(score, 6),
            "updated_at": row["updated_at"],
            "citations": citations,
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
