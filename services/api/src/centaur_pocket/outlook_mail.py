from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import stat
import threading
import unicodedata
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from .database import Database
from .outlook_graph import (
    OUTLOOK_SCOPES,
    OutlookGraphClient,
    OutlookRemoteError,
)
from .outlook_security import (
    OutlookSecretBox,
    OutlookSecurityError,
    sanitize_outlook_text,
    validate_graph_delta_url,
)
from .outlook_transport import OutlookHttpResponse, OutlookTransport
from .service import PocketError, format_utc, new_id, parse_utc, utc_now
from .workspace.service import DEFAULT_OWNER_ID, WorkspaceService

DEFAULT_WORKSPACE_ID = "ws_default"
IMMUTABLE_ID_PREFERENCE = 'IdType="ImmutableId"'
MAIL_ACTOR_ID = DEFAULT_OWNER_ID
ACTIVE_INTENT_STATUSES = (
    "preparing",
    "ready",
    "prepare_uncertain",
    "sending",
    "verifying",
    "send_uncertain",
)
ASSOCIATED_INTENT_STATUSES = (*ACTIVE_INTENT_STATUSES, "failed", "expired", "sent")
STRICT_EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
SEND_INTENT_PROPERTY_ID = (
    "String {b70d3f65-1d44-4e31-981d-d35b535d43e8} Name CentaurSendIntentId"
)


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_load(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _request_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _after(*, seconds: int = 0, minutes: int = 0) -> str:
    return format_utc(datetime.now(UTC) + timedelta(seconds=seconds, minutes=minutes))


class OutlookMailService:
    """Single-account Outlook domain with fail-closed local persistence."""

    def __init__(
        self,
        database: Database,
        *,
        data_root: Path,
        client_id: str | None,
        tenant: str,
        workspace_service: WorkspaceService,
        max_file_bytes: int,
        transport: OutlookTransport | None = None,
    ):
        self.database = database
        self.data_root = data_root
        self.max_file_bytes = min(max_file_bytes, 32 * 1024 * 1024)
        self.workspace_service = workspace_service
        self.secret_box = OutlookSecretBox(data_root)
        try:
            self.graph = OutlookGraphClient(
                client_id=client_id,
                tenant=tenant,
                transport=transport,
            )
        except ValueError as error:
            raise RuntimeError("Outlook 连接器配置无效") from error
        self._operation_lock = threading.RLock()

    def initialize(self) -> None:
        """Recover crash-left running markers without touching remote state."""

        now = utc_now()
        with self.database.transaction() as connection:
            running = connection.execute(
                "SELECT id, account_id FROM outlook_sync_runs WHERE status = 'running'"
            ).fetchall()
            for row in running:
                connection.execute(
                    """
                    UPDATE outlook_sync_runs
                    SET status = 'failed', finished_at = ?,
                        error_code = 'service_restarted'
                    WHERE id = ? AND status = 'running'
                    """,
                    (now, row["id"]),
                )
                connection.execute(
                    """
                    UPDATE outlook_accounts
                    SET next_sync_at = ?, last_error_code = 'service_restarted',
                        version = version + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (_after(minutes=5), now, row["account_id"]),
                )

    def replace_transport_for_testing(self, transport: OutlookTransport) -> None:
        self.graph.transport = transport

    # Response allowlists

    @staticmethod
    def _account_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "provider": "outlook",
            "account_label": row["account_label"],
            "status": row["status"],
            "sync_enabled": bool(row["sync_enabled"]),
            "sync_interval_minutes": row["sync_interval_minutes"],
            "version": row["version"],
            "connected_at": row["connected_at"],
            "last_sync_at": row["last_sync_at"],
            "next_sync_at": row["next_sync_at"],
            "last_error_code": row["last_error_code"],
        }

    def _authorization_view(self, row: sqlite3.Row) -> dict[str, Any]:
        user_code: str | None = None
        if row["status"] == "pending" and row["device_flow_ciphertext"]:
            flow = self._decrypt_json(
                "device_flow", row["id"], row["device_flow_ciphertext"]
            )
            raw_code = flow.get("user_code")
            if isinstance(raw_code, str) and len(raw_code) <= 64:
                user_code = raw_code
        return {
            "id": row["id"],
            "account_label": row["account_label"],
            "verification_uri": row["verification_uri"],
            "user_code": user_code,
            "expires_at": row["expires_at"],
            "interval_seconds": row["interval_seconds"],
            "status": row["status"],
            "version": row["version"],
            "account_id": row["account_id"],
            "error_code": row["error_code"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _sync_run_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "account_id": row["account_id"],
            "status": row["status"],
            "page_count": row["page_count"],
            "changed_count": row["changed_count"],
            "deleted_count": row["deleted_count"],
            "candidate_count": row["candidate_count"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "error_code": row["error_code"],
        }

    # Payload-bound idempotency stores only resource references.

    def _idempotent_reference(
        self,
        connection: sqlite3.Connection,
        *,
        operation: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        payload_hash = _request_hash(payload)
        row = connection.execute(
            """
            SELECT payload_hash, response_json
            FROM outlook_domain_idempotency
            WHERE actor_id = ? AND operation = ? AND idempotency_key = ?
            """,
            (MAIL_ACTOR_ID, operation, idempotency_key),
        ).fetchone()
        if row is None:
            return None, payload_hash
        if not secrets.compare_digest(row["payload_hash"], payload_hash):
            raise PocketError(409, "同一 Idempotency-Key 不能提交不同内容")
        reference = _json_load(row["response_json"], {})
        if not isinstance(reference, dict):
            raise PocketError(409, "邮件幂等记录无效")
        return self._resolve_reference(connection, reference), payload_hash

    @staticmethod
    def _store_reference(
        connection: sqlite3.Connection,
        *,
        operation: str,
        idempotency_key: str,
        payload_hash: str,
        kind: str,
        resource_id: str,
        version: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO outlook_domain_idempotency(
                actor_id, operation, idempotency_key,
                payload_hash, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                MAIL_ACTOR_ID,
                operation,
                idempotency_key,
                payload_hash,
                _json({"kind": kind, "id": resource_id, "version": version}),
                utc_now(),
            ),
        )

    def _resolve_reference(
        self, connection: sqlite3.Connection, reference: dict[str, Any]
    ) -> dict[str, Any]:
        kind = reference.get("kind")
        resource_id = reference.get("id")
        version = reference.get("version")
        if not isinstance(resource_id, str) or not isinstance(version, int):
            raise PocketError(409, "邮件幂等记录无效")
        table = {
            "authorization": "outlook_device_authorizations",
            "account": "outlook_accounts",
            "sync_run": "outlook_sync_runs",
            "draft": "outlook_local_drafts",
            "intent": "outlook_send_intents",
            "candidate": "outlook_task_candidates",
            "candidate_confirm": "outlook_task_candidates",
            "archive": "outlook_archived_attachments",
        }.get(kind)
        if table is None:
            raise PocketError(409, "邮件幂等记录无效")
        row = connection.execute(
            f"SELECT * FROM {table} WHERE id = ?", (resource_id,)
        ).fetchone()
        if row is None:
            raise PocketError(409, "请求已完成，但对应邮件资源已不存在")
        if "version" in set(row.keys()) and row["version"] != version:
            raise PocketError(409, "请求已完成，但资源随后发生变化，请重新同步")
        if kind == "authorization":
            return self._authorization_view(row)
        if kind == "account":
            return self._account_view(row)
        if kind == "sync_run":
            account = connection.execute(
                "SELECT * FROM outlook_accounts WHERE id = ?", (row["account_id"],)
            ).fetchone()
            if account is None:
                raise PocketError(409, "请求已完成，但邮箱账户已不存在")
            return {"run": self._sync_run_view(row), "account": self._account_view(account)}
        if kind == "draft":
            return self._draft_view(connection, row)
        if kind == "intent":
            return self._intent_view(connection, row)
        if kind in {"candidate", "candidate_confirm"}:
            result = {"candidate": self._candidate_view(row)}
            if kind == "candidate_confirm":
                memo_id = row["task_id"]
                memo = self._memo_by_id(memo_id) if memo_id else None
                if memo is None:
                    raise PocketError(409, "任务候选已确认，但对应备忘已变化")
                result["memo"] = memo
            return result
        if kind == "archive":
            return self._archive_view(row)
        raise PocketError(409, "邮件幂等记录无效")

    # Device-code OAuth and account lifecycle

    def list_accounts(self) -> dict[str, Any]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM outlook_accounts ORDER BY created_at DESC"
            ).fetchall()
        return {"items": [self._account_view(row) for row in rows], "total": len(rows)}

    def create_device_authorization(
        self,
        account_label: str,
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        account_label = unicodedata.normalize("NFC", account_label)
        if (
            not 1 <= len(account_label) <= 120
            or account_label != account_label.strip()
            or any(
                character in {"\r", "\n", "\t"}
                or unicodedata.category(character).startswith("C")
                for character in account_label
            )
        ):
            raise PocketError(422, "账户显示名称必须是安全的单行文本")
        operation = "device_authorization.create"
        request = {"account_label": account_label, "device_id": device_id}
        if not self.graph.configured:
            raise PocketError(503, "Outlook 连接器尚未配置")
        with self._operation_lock:
            with self.database.transaction() as connection:
                cached, payload_hash = self._idempotent_reference(
                    connection,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    payload=request,
                )
                if cached is not None:
                    return cached
                live = connection.execute(
                    """
                    SELECT id, status FROM outlook_accounts
                    WHERE status IN ('connected', 'action_required') LIMIT 1
                    """
                ).fetchone()
                if live is not None and live["status"] == "connected":
                    raise PocketError(409, "已有 Outlook 账户，请先断开后再授权")
                pending = connection.execute(
                    """
                    SELECT * FROM outlook_device_authorizations
                    WHERE status = 'pending' LIMIT 1
                    """
                ).fetchone()
                if pending is not None:
                    if (
                        pending["created_by_device_id"] == device_id
                        and pending["account_label"] == account_label
                    ):
                        self._store_reference(
                            connection,
                            operation=operation,
                            idempotency_key=idempotency_key,
                            payload_hash=payload_hash,
                            kind="authorization",
                            resource_id=pending["id"],
                            version=pending["version"],
                        )
                        return self._authorization_view(pending)
                    raise PocketError(409, "已有待完成的 Outlook 授权")
                target_account_id = live["id"] if live is not None else None
            try:
                authorization = self.graph.start_device_authorization()
            except OutlookRemoteError as error:
                raise self._pocket_remote_error(error) from error
            authorization_id = new_id("oauth")
            now = utc_now()
            ciphertext = self._encrypt_json(
                "device_flow", authorization_id, authorization.flow
            )
            with self.database.transaction() as connection:
                try:
                    connection.execute(
                        """
                        INSERT INTO outlook_device_authorizations(
                            id, account_label, client_id, tenant, scopes_json,
                            device_flow_ciphertext, verification_uri,
                            status, interval_seconds,
                            next_poll_at, expires_at, version,
                            created_by_device_id, account_id, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, 1,
                                  ?, ?, ?, ?)
                        """,
                        (
                            authorization_id,
                            account_label,
                            self.graph.client_id,
                            self.graph.tenant,
                            _json(list(OUTLOOK_SCOPES)),
                            ciphertext,
                            authorization.verification_uri,
                            authorization.interval_seconds,
                            _after(seconds=authorization.interval_seconds),
                            _after(seconds=authorization.expires_in_seconds),
                            device_id,
                            target_account_id,
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    raise PocketError(409, "已有 Outlook 授权或账户") from error
                row = connection.execute(
                    "SELECT * FROM outlook_device_authorizations WHERE id = ?",
                    (authorization_id,),
                ).fetchone()
                self._store_reference(
                    connection,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                    kind="authorization",
                    resource_id=authorization_id,
                    version=1,
                )
                return self._authorization_view(row)

    def get_device_authorization(self, authorization_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = self._authorization_row(connection, authorization_id)
            return self._authorization_view(row)

    def poll_device_authorization(
        self,
        authorization_id: str,
        expected_version: int,
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"device_authorization.poll:{authorization_id}"
        request = {
            "authorization_id": authorization_id,
            "expected_version": expected_version,
            "device_id": device_id,
        }
        with self._operation_lock:
            with self.database.connect() as connection:
                cached, payload_hash = self._idempotent_reference(
                    connection,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    payload=request,
                )
                if cached is not None:
                    return cached
                row = self._authorization_row(connection, authorization_id)
                self._require_version(row, expected_version)
                if row["status"] != "pending":
                    raise PocketError(409, "Outlook 授权已结束")
                configuration_changed = (
                    row["client_id"] != self.graph.client_id
                    or row["tenant"] != self.graph.tenant
                )
                flow: dict[str, Any] = {}
                if not configuration_changed:
                    now_dt = datetime.now(UTC)
                    if parse_utc(row["expires_at"]) <= now_dt:
                        return self._finish_authorization(
                            authorization_id,
                            expected_version,
                            status="expired",
                            error_code="authorization_expired",
                            operation=operation,
                            idempotency_key=idempotency_key,
                            payload_hash=payload_hash,
                        )
                    if parse_utc(row["next_poll_at"]) > now_dt:
                        raise PocketError(429, "尚未到下一次授权查询时间")
                    flow = self._decrypt_json(
                        "device_flow", authorization_id, row["device_flow_ciphertext"]
                    )
            if configuration_changed:
                return self._finish_authorization(
                    authorization_id,
                    expected_version,
                    status="failed",
                    error_code="connector_configuration_changed",
                    operation=operation,
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                )
            token = flow.get("authorized_token")
            if not isinstance(token, dict):
                try:
                    poll_result = self.graph.poll_device_authorization(flow)
                except OutlookRemoteError as error:
                    raise self._pocket_remote_error(error) from error
                if poll_result.status == "pending":
                    interval = poll_result.interval_seconds or row["interval_seconds"]
                    flow["interval"] = interval
                    with self.database.transaction() as connection:
                        current = self._authorization_row(connection, authorization_id)
                        self._require_version(current, expected_version)
                        connection.execute(
                            """
                            UPDATE outlook_device_authorizations
                            SET device_flow_ciphertext = ?, interval_seconds = ?,
                                next_poll_at = ?, error_code = ?,
                                version = version + 1, updated_at = ?
                            WHERE id = ? AND version = ? AND status = 'pending'
                            """,
                            (
                                self._encrypt_json("device_flow", authorization_id, flow),
                                interval,
                                _after(seconds=interval),
                                poll_result.error_code,
                                utc_now(),
                                authorization_id,
                                expected_version,
                            ),
                        )
                        updated = self._authorization_row(connection, authorization_id)
                        self._store_reference(
                            connection,
                            operation=operation,
                            idempotency_key=idempotency_key,
                            payload_hash=payload_hash,
                            kind="authorization",
                            resource_id=authorization_id,
                            version=updated["version"],
                        )
                        return self._authorization_view(updated)
                if poll_result.status != "authorized":
                    return self._finish_authorization(
                        authorization_id,
                        expected_version,
                        status=poll_result.status,
                        error_code=poll_result.error_code or "authorization_failed",
                        operation=operation,
                        idempotency_key=idempotency_key,
                        payload_hash=payload_hash,
                    )
                token = poll_result.token
                if not isinstance(token, dict):
                    raise PocketError(502, "Outlook 授权响应无效")
                flow["authorized_token"] = token
                with self.database.transaction() as connection:
                    current = self._authorization_row(connection, authorization_id)
                    self._require_version(current, expected_version)
                    connection.execute(
                        """
                        UPDATE outlook_device_authorizations
                        SET device_flow_ciphertext = ?, updated_at = ?
                        WHERE id = ? AND version = ? AND status = 'pending'
                        """,
                        (
                            self._encrypt_json("device_flow", authorization_id, flow),
                            utc_now(),
                            authorization_id,
                            expected_version,
                        ),
                    )
            return self._connect_authorization(
                authorization_id,
                expected_version,
                token,
                operation=operation,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
            )

    def cancel_device_authorization(
        self,
        authorization_id: str,
        expected_version: int,
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"device_authorization.cancel:{authorization_id}"
        request = {
            "authorization_id": authorization_id,
            "expected_version": expected_version,
            "device_id": device_id,
        }
        with self._operation_lock, self.database.transaction() as connection:
            cached, payload_hash = self._idempotent_reference(
                connection,
                operation=operation,
                idempotency_key=idempotency_key,
                payload=request,
            )
            if cached is not None:
                return cached
            row = self._authorization_row(connection, authorization_id)
            self._require_version(row, expected_version)
            if row["status"] != "pending":
                raise PocketError(409, "Outlook 授权已结束")
            now = utc_now()
            connection.execute(
                """
                UPDATE outlook_device_authorizations
                SET status = 'canceled', device_flow_ciphertext = NULL,
                    error_code = NULL, completed_at = ?, updated_at = ?,
                    version = version + 1
                WHERE id = ? AND version = ? AND status = 'pending'
                """,
                (now, now, authorization_id, expected_version),
            )
            updated = self._authorization_row(connection, authorization_id)
            self._store_reference(
                connection,
                operation=operation,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                kind="authorization",
                resource_id=authorization_id,
                version=updated["version"],
            )
            return self._authorization_view(updated)

    def disconnect_account(
        self,
        account_id: str,
        expected_version: int,
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"account.disconnect:{account_id}"
        request = {
            "account_id": account_id,
            "expected_version": expected_version,
            "device_id": device_id,
        }
        with self._operation_lock, self.database.transaction() as connection:
            cached, payload_hash = self._idempotent_reference(
                connection,
                operation=operation,
                idempotency_key=idempotency_key,
                payload=request,
            )
            if cached is not None:
                return cached
            row = self._account_row(connection, account_id)
            self._require_version(row, expected_version)
            if row["status"] == "disconnected":
                raise PocketError(409, "Outlook 账户已经断开")
            placeholders = ",".join("?" for _ in ACTIVE_INTENT_STATUSES)
            active_intent = connection.execute(
                f"""
                SELECT id FROM outlook_send_intents intent
                JOIN outlook_local_drafts draft ON draft.id = intent.draft_id
                WHERE draft.account_id = ? AND intent.status IN ({placeholders})
                LIMIT 1
                """,
                (account_id, *ACTIVE_INTENT_STATUSES),
            ).fetchone()
            if active_intent is not None:
                raise PocketError(409, "存在尚未核验的发送意图，暂不能断开账户")
            now = utc_now()
            connection.execute(
                "DELETE FROM outlook_credentials WHERE account_id = ?", (account_id,)
            )
            connection.execute(
                "DELETE FROM outlook_sync_cursors WHERE account_id = ?", (account_id,)
            )
            connection.execute(
                """
                UPDATE outlook_accounts
                SET status = 'disconnected', sync_enabled = 0,
                    next_sync_at = NULL, last_error_code = NULL,
                    version = version + 1, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (now, account_id, expected_version),
            )
            connection.execute(
                "UPDATE sources SET enabled = 0, updated_at = ? WHERE id = ?",
                (now, row["source_id"]),
            )
            updated = self._account_row(connection, account_id)
            self._store_reference(
                connection,
                operation=operation,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                kind="account",
                resource_id=account_id,
                version=updated["version"],
            )
            return self._account_view(updated)

    def _connect_authorization(
        self,
        authorization_id: str,
        expected_version: int,
        token: dict[str, Any],
        *,
        operation: str,
        idempotency_key: str,
        payload_hash: str,
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            pending = self._authorization_row(connection, authorization_id)
            target_id = pending["account_id"]
        mailbox_fingerprint = self._mailbox_fingerprint(
            token, require_existing_key=target_id is not None
        )
        with self.database.connect() as connection:
            if target_id is not None:
                target = self._account_row(connection, target_id)
                if not secrets.compare_digest(
                    target["mailbox_fingerprint"], mailbox_fingerprint
                ):
                    return self._finish_authorization(
                        authorization_id,
                        expected_version,
                        status="failed",
                        error_code="mailbox_identity_mismatch",
                        operation=operation,
                        idempotency_key=idempotency_key,
                        payload_hash=payload_hash,
                    )
        now = utc_now()
        with self.database.transaction() as connection:
            authorization = self._authorization_row(connection, authorization_id)
            self._require_version(authorization, expected_version)
            if authorization["status"] != "pending":
                raise PocketError(409, "Outlook 授权已结束")
            live = connection.execute(
                """
                SELECT id FROM outlook_accounts
                WHERE status IN ('connected', 'action_required') LIMIT 1
                """
            ).fetchone()
            target_account_id = authorization["account_id"]
            if target_account_id is not None:
                target = self._account_row(connection, target_account_id)
                if target["status"] != "action_required" or (
                    live is not None and live["id"] != target_account_id
                ):
                    raise PocketError(409, "Outlook 重新授权目标已变化")
                account_id = target_account_id
                token_ciphertext = self._encrypt_json("token_cache", account_id, token)
                connection.execute(
                    """
                    INSERT INTO outlook_credentials(account_id, token_ciphertext, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(account_id) DO UPDATE SET
                        token_ciphertext = excluded.token_ciphertext,
                        updated_at = excluded.updated_at
                    """,
                    (account_id, token_ciphertext, now),
                )
                connection.execute(
                    """
                    UPDATE outlook_accounts
                    SET status = 'connected', sync_enabled = 1,
                        last_error_code = NULL, next_sync_at = ?,
                        version = version + 1, updated_at = ?
                    WHERE id = ? AND status = 'action_required'
                    """,
                    (now, now, account_id),
                )
                connection.execute(
                    "UPDATE sources SET enabled = 1, updated_at = ? WHERE id = ?",
                    (now, target["source_id"]),
                )
            else:
                if live is not None:
                    raise PocketError(409, "已有 Outlook 账户，请先断开后再授权")
                account_id = new_id("mailacct")
                source_id = new_id("src")
                token_ciphertext = self._encrypt_json("token_cache", account_id, token)
                connection.execute(
                    """
                    INSERT INTO sources(
                        id, kind, provider, name, config_json, schedule, enabled,
                        created_at, updated_at
                    ) VALUES (?, 'outlook_mail', 'microsoft_graph', ?, ?, 'manual', 1, ?, ?)
                    """,
                    (
                        source_id,
                        f"Outlook · {authorization['account_label']}",
                        _json({"folder": "inbox", "mode": "metadata_only"}),
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO outlook_accounts(
                        id, source_id, account_label, client_id, tenant, scopes_json,
                        mailbox_fingerprint,
                        status, sync_enabled, sync_interval_minutes, version,
                        connected_at, next_sync_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'connected', 1, 15, 1,
                              ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        source_id,
                        authorization["account_label"],
                        self.graph.client_id,
                        self.graph.tenant,
                        _json(list(OUTLOOK_SCOPES)),
                        mailbox_fingerprint,
                        now,
                        now,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO outlook_credentials(
                        account_id, token_ciphertext, updated_at
                    ) VALUES (?, ?, ?)
                    """,
                    (account_id, token_ciphertext, now),
                )
            connection.execute(
                """
                UPDATE outlook_device_authorizations
                SET status = 'connected', device_flow_ciphertext = NULL,
                    account_id = ?, error_code = NULL, completed_at = ?,
                    updated_at = ?, version = version + 1
                WHERE id = ? AND version = ? AND status = 'pending'
                """,
                (account_id, now, now, authorization_id, expected_version),
            )
            updated = self._authorization_row(connection, authorization_id)
            self._store_reference(
                connection,
                operation=operation,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                kind="authorization",
                resource_id=authorization_id,
                version=updated["version"],
            )
            return self._authorization_view(updated)

    def _mailbox_fingerprint(
        self, token: dict[str, Any], *, require_existing_key: bool
    ) -> str:
        access_token = token.get("access_token")
        if not isinstance(access_token, str):
            raise PocketError(502, "Outlook 授权响应无效")
        try:
            response = self.graph.graph_request(
                "GET",
                "/me/mailFolders/inbox?$select=id",
                access_token=access_token,
                max_bytes=128 * 1024,
                prefer=IMMUTABLE_ID_PREFERENCE,
            )
        except OutlookRemoteError as error:
            raise self._pocket_remote_error(error) from error
        if response.status != 200:
            raise self._pocket_remote_error(
                self.graph.http_error(response, "mailbox_identity_failed")
            )
        try:
            folder_id = sanitize_outlook_text(
                self.graph.json_object(response).get("id"), max_chars=2_048
            )
        except OutlookRemoteError as error:
            raise self._pocket_remote_error(error) from error
        if not folder_id:
            raise PocketError(502, "Outlook 邮箱身份响应无效")
        try:
            if require_existing_key:
                return self.secret_box.opaque_reference_existing(
                    "mailbox_identity", folder_id
                )
            return self.secret_box.opaque_reference("mailbox_identity", folder_id)
        except OutlookSecurityError as error:
            raise PocketError(503, "Outlook 本地安全存储不可用") from error

    def _finish_authorization(
        self,
        authorization_id: str,
        expected_version: int,
        *,
        status: str,
        error_code: str,
        operation: str,
        idempotency_key: str,
        payload_hash: str,
    ) -> dict[str, Any]:
        if status not in {"denied", "expired", "failed"}:
            status = "failed"
        now = utc_now()
        with self.database.transaction() as connection:
            row = self._authorization_row(connection, authorization_id)
            self._require_version(row, expected_version)
            connection.execute(
                """
                UPDATE outlook_device_authorizations
                SET status = ?, device_flow_ciphertext = NULL, error_code = ?,
                    completed_at = ?, updated_at = ?, version = version + 1
                WHERE id = ? AND version = ? AND status = 'pending'
                """,
                (status, error_code, now, now, authorization_id, expected_version),
            )
            updated = self._authorization_row(connection, authorization_id)
            self._store_reference(
                connection,
                operation=operation,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                kind="authorization",
                resource_id=authorization_id,
                version=updated["version"],
            )
            return self._authorization_view(updated)

    # Encrypted token cache and one safe 401 refresh retry.

    def _load_token(self, account_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT token_ciphertext FROM outlook_credentials WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        if row is None:
            raise PocketError(409, "Outlook 账户需要重新授权")
        return self._decrypt_json("token_cache", account_id, row["token_ciphertext"])

    def _access_token(self, account_id: str, *, force_refresh: bool = False) -> str:
        with self._operation_lock:
            try:
                token = self._load_token(account_id)
            except PocketError as error:
                if error.status_code != 409:
                    raise
                self._require_reauthorization(account_id)
                raise PocketError(409, "Outlook 账户需要重新授权") from error
            raw_expiry = token.get("expires_at")
            raw_access = token.get("access_token")
            if (
                not force_refresh
                and isinstance(raw_expiry, str)
                and isinstance(raw_access, str)
                and parse_utc(raw_expiry) > datetime.now(UTC) + timedelta(seconds=60)
            ):
                return raw_access
            try:
                refreshed = self.graph.refresh_token(token)
            except OutlookRemoteError as error:
                if error.code == "reauthorization_required":
                    self._require_reauthorization(account_id)
                    raise PocketError(409, "Outlook 账户需要重新授权") from error
                if error.code == "connector_misconfigured":
                    raise PocketError(503, "Outlook 连接器配置无效") from error
                raise self._pocket_remote_error(error) from error
            ciphertext = self._encrypt_json("token_cache", account_id, refreshed)
            with self.database.transaction() as connection:
                result = connection.execute(
                    """
                    UPDATE outlook_credentials
                    SET token_ciphertext = ?, updated_at = ?
                    WHERE account_id = ?
                    """,
                    (ciphertext, utc_now(), account_id),
                )
                if result.rowcount != 1:
                    raise PocketError(409, "Outlook 账户已经断开")
            return str(refreshed["access_token"])

    def _graph_request(
        self,
        account_id: str,
        method: str,
        path_or_url: str,
        *,
        body: dict[str, Any] | None = None,
        max_bytes: int = 2 * 1024 * 1024,
        prefer: str | None = None,
        accept: str = "application/json",
        replay_after_401: bool | None = None,
    ) -> OutlookHttpResponse:
        access_token = self._access_token(account_id)
        response = self.graph.graph_request(
            method,
            path_or_url,
            access_token=access_token,
            body=body,
            max_bytes=max_bytes,
            prefer=prefer,
            accept=accept,
        )
        if response.status != 401:
            return response
        access_token = self._access_token(account_id, force_refresh=True)
        should_replay = method == "GET" if replay_after_401 is None else replay_after_401
        if not should_replay:
            return response
        retried = self.graph.graph_request(
            method,
            path_or_url,
            access_token=access_token,
            body=body,
            max_bytes=max_bytes,
            prefer=prefer,
            accept=accept,
        )
        if retried.status == 401:
            self._require_reauthorization(account_id)
            raise PocketError(409, "Outlook 账户需要重新授权")
        return retried

    def _require_reauthorization(self, account_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE outlook_accounts
                SET status = 'action_required', sync_enabled = 0,
                    next_sync_at = NULL,
                    last_error_code = 'reauthorization_required',
                    version = version + 1, updated_at = ?
                WHERE id = ? AND status = 'connected'
                """,
                (utc_now(), account_id),
            )

    # Shared database/security helpers

    @staticmethod
    def _require_version(row: sqlite3.Row, expected_version: int) -> None:
        if row["version"] != expected_version:
            raise PocketError(409, "资源版本已变化，请重新同步")

    @staticmethod
    def _authorization_row(
        connection: sqlite3.Connection, authorization_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM outlook_device_authorizations WHERE id = ?",
            (authorization_id,),
        ).fetchone()
        if row is None:
            raise PocketError(404, "Outlook 授权不存在")
        return row

    @staticmethod
    def _account_row(connection: sqlite3.Connection, account_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM outlook_accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if row is None:
            raise PocketError(404, "Outlook 账户不存在")
        return row

    def _encrypt_json(self, purpose: str, record_id: str, value: Any) -> str:
        try:
            return self.secret_box.encrypt_text(purpose, record_id, _json(value))
        except OutlookSecurityError as error:
            raise PocketError(503, "Outlook 本地安全存储不可用") from error

    def _decrypt_json(self, purpose: str, record_id: str, value: str) -> dict[str, Any]:
        try:
            decoded = self.secret_box.decrypt_text(purpose, record_id, value)
            payload = json.loads(decoded)
        except (OutlookSecurityError, json.JSONDecodeError) as error:
            raise PocketError(503, "Outlook 本地安全存储不可用") from error
        if not isinstance(payload, dict):
            raise PocketError(503, "Outlook 本地安全存储不可用")
        return payload

    @staticmethod
    def _pocket_remote_error(error: OutlookRemoteError) -> PocketError:
        if error.code == "connector_not_configured":
            return PocketError(503, "Outlook 连接器尚未配置")
        if error.code == "reauthorization_required":
            return PocketError(409, "Outlook 账户需要重新授权")
        if error.code == "connector_misconfigured":
            return PocketError(503, "Outlook 连接器配置无效")
        if error.code in {"throttled", "remote_unavailable"}:
            return PocketError(503, "Outlook 暂时不可用，请稍后再试")
        return PocketError(502, "Outlook 返回了无法处理的响应")

    # Inbox delta metadata and on-demand message reads

    @staticmethod
    def _recipient(value: object) -> dict[str, str | None] | None:
        if not isinstance(value, dict):
            return None
        email = value.get("emailAddress")
        if not isinstance(email, dict):
            return None
        name = sanitize_outlook_text(email.get("name"), max_chars=200) or None
        address = sanitize_outlook_text(email.get("address"), max_chars=320) or None
        if address is None or "@" not in address:
            return None
        return {"name": name, "address": address.casefold()}

    @classmethod
    def _recipients(cls, value: object) -> list[dict[str, str | None]]:
        if not isinstance(value, list):
            return []
        result: list[dict[str, str | None]] = []
        seen: set[str] = set()
        for raw in value[:100]:
            recipient = cls._recipient(raw)
            if recipient is None or recipient["address"] in seen:
                continue
            seen.add(str(recipient["address"]))
            result.append(recipient)
        return result

    @staticmethod
    def _message_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "account_id": row["account_id"],
            "subject": row["subject"],
            "sender": _json_load(row["sender_json"], None),
            "to_recipients": _json_load(row["to_recipients_json"], []),
            "cc_recipients": _json_load(row["cc_recipients_json"], []),
            "body_preview": row["body_preview"],
            "importance": row["importance"],
            "is_read": bool(row["is_read"]),
            "has_attachments": bool(row["has_attachments"]),
            "received_at": row["received_at"],
            "sent_at": row["sent_at"],
            "status": row["status"],
            "version": row["version"],
        }

    def due_account_ids(self) -> list[str]:
        now = utc_now()
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM outlook_accounts
                WHERE status = 'connected' AND sync_enabled = 1
                  AND (next_sync_at IS NULL OR next_sync_at <= ?)
                ORDER BY COALESCE(next_sync_at, connected_at)
                """,
                (now,),
            ).fetchall()
        return [row["id"] for row in rows]

    def sync_due_account(self, account_id: str) -> dict[str, Any]:
        with self._operation_lock:
            try:
                return self._sync_account_core(account_id, expected_version=None)
            except Exception:
                self._fail_running_sync(account_id)
                raise

    def sync_inbox(
        self,
        account_id: str,
        expected_version: int,
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"account.inbox_delta:{account_id}"
        request = {
            "account_id": account_id,
            "expected_version": expected_version,
            "device_id": device_id,
        }
        with self._operation_lock:
            with self.database.connect() as connection:
                cached, payload_hash = self._idempotent_reference(
                    connection,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    payload=request,
                )
                if cached is not None:
                    return cached
            try:
                result = self._sync_account_core(
                    account_id, expected_version=expected_version
                )
            except Exception:
                self._fail_running_sync(account_id)
                raise
            with self.database.transaction() as connection:
                self._store_reference(
                    connection,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                    kind="sync_run",
                    resource_id=result["run"]["id"],
                    version=1,
                )
            return result

    def _fail_running_sync(self, account_id: str) -> None:
        now = utc_now()
        with self.database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT id FROM outlook_sync_runs
                WHERE account_id = ? AND status = 'running'
                """,
                (account_id,),
            ).fetchall()
            if not rows:
                return
            for row in rows:
                connection.execute(
                    """
                    UPDATE outlook_sync_runs
                    SET status = 'failed', finished_at = ?,
                        error_code = 'sync_internal_error'
                    WHERE id = ? AND status = 'running'
                    """,
                    (now, row["id"]),
                )
            connection.execute(
                """
                UPDATE outlook_accounts
                SET next_sync_at = ?, last_error_code = 'sync_internal_error',
                    version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (_after(minutes=5), now, account_id),
            )

    def _sync_account_core(
        self, account_id: str, *, expected_version: int | None
    ) -> dict[str, Any]:
        run_id = new_id("mailrun")
        started_at = utc_now()
        with self.database.transaction() as connection:
            account = self._account_row(connection, account_id)
            if expected_version is not None:
                self._require_version(account, expected_version)
            if account["status"] != "connected" or not bool(account["sync_enabled"]):
                raise PocketError(409, "Outlook 账户未连接或同步已暂停")
            try:
                connection.execute(
                    """
                    INSERT INTO outlook_sync_runs(id, account_id, status, started_at)
                    VALUES (?, ?, 'running', ?)
                    """,
                    (run_id, account_id, started_at),
                )
            except sqlite3.IntegrityError as error:
                raise PocketError(409, "该 Outlook 账户已有同步任务") from error
            cursor = connection.execute(
                """
                SELECT cursor_kind, cursor_ciphertext
                FROM outlook_sync_cursors
                WHERE account_id = ? AND folder_key = 'inbox'
                """,
                (account_id,),
            ).fetchone()
        if cursor is None:
            next_url = (
                "/me/mailFolders/inbox/messages/delta?"
                "$select=id,changeKey,conversationId,internetMessageId,subject,"
                "sender,toRecipients,ccRecipients,bodyPreview,importance,isRead,"
                "hasAttachments,receivedDateTime,sentDateTime&$top=50"
            )
        else:
            decrypted = self.secret_box.decrypt_text(
                "delta_cursor", f"{account_id}:inbox", cursor["cursor_ciphertext"]
            )
            try:
                next_url = validate_graph_delta_url(decrypted)
            except OutlookSecurityError as error:
                self._finish_sync(
                    run_id,
                    account_id,
                    status="failed",
                    counters={},
                    error_code="invalid_delta_cursor",
                )
                raise PocketError(503, "Outlook 增量同步状态不可用") from error
        counters = {"page_count": 0, "changed_count": 0, "deleted_count": 0, "candidate_count": 0}
        final_status = "completed"
        error_code: str | None = None
        for _page in range(10):
            try:
                response = self._graph_request(
                    account_id,
                    "GET",
                    next_url,
                    max_bytes=2 * 1024 * 1024,
                    prefer=IMMUTABLE_ID_PREFERENCE,
                )
            except (OutlookRemoteError, PocketError) as error:
                error_code = (
                    error.code if isinstance(error, OutlookRemoteError) else "mail_sync_failed"
                )
                final_status = "partial" if counters["page_count"] else "failed"
                break
            if response.status == 410:
                with self.database.transaction() as connection:
                    connection.execute(
                        """
                        DELETE FROM outlook_sync_cursors
                        WHERE account_id = ? AND folder_key = 'inbox'
                        """,
                        (account_id,),
                    )
                error_code = "sync_state_expired"
                final_status = "failed"
                break
            if response.status != 200:
                remote = self.graph.http_error(response, "mail_sync_failed")
                error_code = remote.code
                final_status = "partial" if counters["page_count"] else "failed"
                break
            try:
                payload = self.graph.json_object(response)
            except OutlookRemoteError as error:
                error_code = error.code
                final_status = "partial" if counters["page_count"] else "failed"
                break
            values = payload.get("value")
            if not isinstance(values, list) or len(values) > 1_000:
                error_code = "invalid_delta_page"
                final_status = "partial" if counters["page_count"] else "failed"
                break
            continuation = payload.get("@odata.nextLink")
            cursor_kind = "next"
            if continuation is None:
                continuation = payload.get("@odata.deltaLink")
                cursor_kind = "delta"
            if not isinstance(continuation, str):
                error_code = "invalid_delta_page"
                final_status = "partial" if counters["page_count"] else "failed"
                break
            try:
                continuation = validate_graph_delta_url(continuation)
            except OutlookSecurityError:
                error_code = "invalid_delta_cursor"
                final_status = "partial" if counters["page_count"] else "failed"
                break
            page_counts = self._apply_delta_page(
                account_id,
                values,
                cursor_kind=cursor_kind,
                continuation=continuation,
            )
            for key in counters:
                counters[key] += page_counts.get(key, 0)
            if cursor_kind == "delta":
                break
            next_url = continuation
        else:
            final_status = "partial"
            error_code = "page_limit_reached"
        return self._finish_sync(
            run_id,
            account_id,
            status=final_status,
            counters=counters,
            error_code=error_code,
        )

    def _apply_delta_page(
        self,
        account_id: str,
        values: list[Any],
        *,
        cursor_kind: str,
        continuation: str,
    ) -> dict[str, int]:
        counts = {"page_count": 1, "changed_count": 0, "deleted_count": 0, "candidate_count": 0}
        now = utc_now()
        with self.database.transaction() as connection:
            for value in values:
                if not isinstance(value, dict):
                    continue
                graph_id = sanitize_outlook_text(value.get("id"), max_chars=2_048)
                if not graph_id:
                    continue
                existing = connection.execute(
                    """
                    SELECT * FROM outlook_messages
                    WHERE account_id = ? AND graph_message_id = ?
                    """,
                    (account_id, graph_id),
                ).fetchone()
                if "@removed" in value:
                    if existing is not None and existing["status"] != "deleted":
                        connection.execute(
                            """
                            UPDATE outlook_messages
                            SET status = 'deleted', deleted_at = ?, updated_at = ?,
                                version = version + 1
                            WHERE id = ?
                            """,
                            (now, now, existing["id"]),
                        )
                        counts["deleted_count"] += 1
                    continue
                change_key = sanitize_outlook_text(value.get("changeKey"), max_chars=1_000) or None
                if (
                    existing is not None
                    and existing["change_key"] == change_key
                    and existing["status"] == "active"
                ):
                    continue
                sender = self._recipient(value.get("sender"))
                to_recipients = self._recipients(value.get("toRecipients"))
                cc_recipients = self._recipients(value.get("ccRecipients"))
                subject = sanitize_outlook_text(value.get("subject"), max_chars=500) or "（无主题）"
                preview = sanitize_outlook_text(value.get("bodyPreview"), max_chars=2_000)
                importance = value.get("importance")
                if importance not in {"low", "normal", "high"}:
                    importance = "normal"
                columns = (
                    sanitize_outlook_text(value.get("conversationId"), max_chars=2_048) or None,
                    sanitize_outlook_text(value.get("internetMessageId"), max_chars=2_048) or None,
                    subject,
                    _json(sender),
                    _json(to_recipients),
                    _json(cc_recipients),
                    preview,
                    importance,
                    int(bool(value.get("isRead"))),
                    int(bool(value.get("hasAttachments"))),
                    self._remote_datetime(value.get("receivedDateTime")),
                    self._remote_datetime(value.get("sentDateTime")),
                    change_key,
                )
                if existing is None:
                    message_id = new_id("mailmsg")
                    connection.execute(
                        """
                        INSERT INTO outlook_messages(
                            id, account_id, graph_message_id, conversation_id,
                            internet_message_id, subject, sender_json,
                            to_recipients_json, cc_recipients_json, body_preview,
                            importance, is_read, has_attachments, received_at,
                            sent_at, change_key, status, version, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  'active', 1, ?, ?)
                        """,
                        (message_id, account_id, graph_id, *columns, now, now),
                    )
                else:
                    message_id = existing["id"]
                    connection.execute(
                        """
                        UPDATE outlook_messages SET
                            conversation_id = ?, internet_message_id = ?, subject = ?,
                            sender_json = ?, to_recipients_json = ?,
                            cc_recipients_json = ?, body_preview = ?, importance = ?,
                            is_read = ?, has_attachments = ?, received_at = ?, sent_at = ?,
                            change_key = ?, status = 'active', deleted_at = NULL,
                            version = version + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (*columns, now, message_id),
                    )
                counts["changed_count"] += 1
                if self._upsert_task_candidate(connection, message_id, subject, preview, importance, now):
                    counts["candidate_count"] += 1
            encrypted = self.secret_box.encrypt_text(
                "delta_cursor", f"{account_id}:inbox", continuation
            )
            connection.execute(
                """
                INSERT INTO outlook_sync_cursors(
                    account_id, folder_key, cursor_kind, cursor_ciphertext, updated_at
                ) VALUES (?, 'inbox', ?, ?, ?)
                ON CONFLICT(account_id, folder_key) DO UPDATE SET
                    cursor_kind = excluded.cursor_kind,
                    cursor_ciphertext = excluded.cursor_ciphertext,
                    updated_at = excluded.updated_at
                """,
                (account_id, cursor_kind, encrypted, now),
            )
        return counts

    @staticmethod
    def _remote_datetime(value: object) -> str | None:
        if not isinstance(value, str) or len(value) > 64:
            return None
        try:
            return format_utc(parse_utc(value))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _upsert_task_candidate(
        connection: sqlite3.Connection,
        message_id: str,
        subject: str,
        preview: str,
        importance: str,
        now: str,
    ) -> bool:
        searchable = f"{subject}\n{preview}".casefold()
        indicators = ("请", "需要", "务必", "截止", "待办", "please", "action required", "todo", "due")
        if not any(indicator in searchable for indicator in indicators):
            return False
        existing = connection.execute(
            "SELECT id, status FROM outlook_task_candidates WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        priority = "critical" if importance == "high" and "紧急" in searchable else (
            "high" if importance == "high" else "normal"
        )
        if existing is None:
            connection.execute(
                """
                INSERT INTO outlook_task_candidates(
                    id, message_id, title, summary, purpose, objective, strategy,
                    acceptance_criteria_json, priority, status, version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 1, ?, ?)
                """,
                (
                    new_id("mailcand"),
                    message_id,
                    subject,
                    preview or "邮件包含待确认的行动要求。",
                    "响应邮件中经主人确认的工作要求",
                    "由主人确认后纳入任务闭环",
                    "先核对邮件原文、期限和负责人，再安排执行。",
                    _json(["主人确认邮件要求及完成标准"]),
                    priority,
                    now,
                    now,
                ),
            )
            return True
        if existing["status"] == "pending":
            connection.execute(
                """
                UPDATE outlook_task_candidates
                SET title = ?, summary = ?, priority = ?,
                    version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (subject, preview or "邮件包含待确认的行动要求。", priority, now, existing["id"]),
            )
        return False

    def _finish_sync(
        self,
        run_id: str,
        account_id: str,
        *,
        status: str,
        counters: dict[str, int],
        error_code: str | None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.database.transaction() as connection:
            account = self._account_row(connection, account_id)
            connection.execute(
                """
                UPDATE outlook_sync_runs SET status = ?, finished_at = ?,
                    page_count = ?, changed_count = ?, deleted_count = ?,
                    candidate_count = ?, error_code = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    status,
                    now,
                    counters.get("page_count", 0),
                    counters.get("changed_count", 0),
                    counters.get("deleted_count", 0),
                    counters.get("candidate_count", 0),
                    error_code,
                    run_id,
                ),
            )
            retry_minutes = 5 if status == "failed" else account["sync_interval_minutes"]
            connection.execute(
                """
                UPDATE outlook_accounts
                SET last_sync_at = CASE WHEN ? = 'completed' THEN ? ELSE last_sync_at END,
                    next_sync_at = ?, last_error_code = ?, version = version + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, now, _after(minutes=retry_minutes), error_code, now, account_id),
            )
            run = connection.execute(
                "SELECT * FROM outlook_sync_runs WHERE id = ?", (run_id,)
            ).fetchone()
            account = self._account_row(connection, account_id)
            return {"run": self._sync_run_view(run), "account": self._account_view(account)}

    def list_inbox(self, account_id: str, *, limit: int, offset: int) -> dict[str, Any]:
        with self.database.connect() as connection:
            self._account_row(connection, account_id)
            total = connection.execute(
                """
                SELECT COUNT(*) FROM outlook_messages
                WHERE account_id = ? AND status = 'active'
                """,
                (account_id,),
            ).fetchone()[0]
            rows = connection.execute(
                """
                SELECT * FROM outlook_messages
                WHERE account_id = ? AND status = 'active'
                ORDER BY received_at DESC, created_at DESC LIMIT ? OFFSET ?
                """,
                (account_id, limit, offset),
            ).fetchall()
        return {
            "items": [self._message_view(row) for row in rows],
            "total": int(total),
            "limit": limit,
            "offset": offset,
        }

    def get_message(self, message_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            return self._message_view(self._message_row(connection, message_id))

    def get_message_body(self, message_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            message = self._message_row(connection, message_id)
            if message["status"] != "active":
                raise PocketError(409, "邮件已不在远端 Inbox 中")
            account_id = message["account_id"]
            graph_id = message["graph_message_id"]
            message_version = message["version"]
        response = self._graph_request(
            account_id,
            "GET",
            f"/me/messages/{quote(graph_id, safe='')}?$select=body",
            max_bytes=512 * 1024,
            prefer='IdType="ImmutableId", outlook.body-content-type="text"',
        )
        if response.status != 200:
            raise self._pocket_remote_error(
                self.graph.http_error(response, "message_body_failed")
            )
        payload = self.graph.json_object(response)
        body = payload.get("body")
        if not isinstance(body, dict) or str(body.get("contentType", "")).casefold() != "text":
            raise PocketError(502, "Outlook 未返回安全的纯文本正文")
        body_text = sanitize_outlook_text(body.get("content"), max_chars=100_000)
        return {
            "message_id": message_id,
            "body_text": body_text,
            "content_type": "text",
            "message_version": message_version,
            "fetched_at": utc_now(),
        }

    @staticmethod
    def _message_row(connection: sqlite3.Connection, message_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM outlook_messages WHERE id = ?", (message_id,)
        ).fetchone()
        if row is None:
            raise PocketError(404, "邮件不存在")
        return row

    @staticmethod
    def _candidate_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "message_id": row["message_id"],
            "title": row["title"],
            "summary": row["summary"],
            "purpose": row["purpose"],
            "objective": row["objective"],
            "strategy": row["strategy"],
            "acceptance_criteria": _json_load(row["acceptance_criteria_json"], []),
            "priority": row["priority"],
            "status": row["status"],
            "memo_id": row["task_id"],
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "resolved_at": row["resolved_at"],
        }

    @staticmethod
    def _archive_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "message_id": row["message_id"],
            "attachment_id": row["attachment_ref"],
            "file_name": row["file_name"],
            "mime_type": row["mime_type"],
            "size_bytes": row["size_bytes"],
            "content_hash": row["content_hash"],
            "document_id": row["document_id"],
            "status": "quarantined_unscanned",
            "archived_at": row["archived_at"],
        }

    # Attachment metadata is fetched on demand. Graph IDs remain internal.

    def list_message_attachments(self, message_id: str) -> dict[str, Any]:
        attachments = self._fetch_attachments(message_id)
        public_items = [
            {key: value for key, value in item.items() if key != "graph_id"}
            for item in attachments
        ]
        return {"items": public_items, "total": len(public_items)}

    def _fetch_attachments(self, message_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            message = self._message_row(connection, message_id)
            if message["status"] != "active":
                raise PocketError(409, "邮件已不在远端 Inbox 中")
            account_id = message["account_id"]
            graph_message_id = message["graph_message_id"]
        response = self._graph_request(
            account_id,
            "GET",
            f"/me/messages/{quote(graph_message_id, safe='')}/attachments?"
            "$select=id,name,contentType,size,isInline&$top=100",
            max_bytes=512 * 1024,
            prefer=IMMUTABLE_ID_PREFERENCE,
        )
        if response.status != 200:
            raise self._pocket_remote_error(
                self.graph.http_error(response, "attachment_list_failed")
            )
        payload = self.graph.json_object(response)
        raw_items = payload.get("value")
        if not isinstance(raw_items, list) or len(raw_items) > 100:
            raise PocketError(502, "Outlook 附件元数据响应无效")
        result: list[dict[str, Any]] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            graph_id = sanitize_outlook_text(raw.get("id"), max_chars=2_048)
            if not graph_id:
                continue
            odata_type = str(raw.get("@odata.type", "")).casefold()
            attachment_type = (
                "file"
                if odata_type.endswith("fileattachment")
                else "item"
                if odata_type.endswith("itemattachment")
                else "reference"
            )
            try:
                size_bytes = int(raw.get("size", 0))
            except (TypeError, ValueError):
                size_bytes = 0
            result.append(
                {
                    "id": self.secret_box.opaque_reference(
                        "attachment", message_id, graph_id
                    ),
                    "name": self._safe_file_name(raw.get("name")),
                    "mime_type": sanitize_outlook_text(
                        raw.get("contentType"), max_chars=255
                    ).casefold()
                    or "application/octet-stream",
                    "size_bytes": max(size_bytes, 0),
                    "is_inline": bool(raw.get("isInline")),
                    "type": attachment_type,
                    "graph_id": graph_id,
                }
            )
        return result

    def archive_attachment(
        self,
        message_id: str,
        attachment_id: str,
        expected_version: int,
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"attachment.archive:{message_id}:{attachment_id}"
        request = {
            "message_id": message_id,
            "attachment_id": attachment_id,
            "expected_version": expected_version,
            "device_id": device_id,
        }
        with self._operation_lock:
            with self.database.transaction() as connection:
                cached, payload_hash = self._idempotent_reference(
                    connection,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    payload=request,
                )
                if cached is not None:
                    return cached
                message = self._message_row(connection, message_id)
                self._require_version(message, expected_version)
                existing = connection.execute(
                    """
                    SELECT * FROM outlook_archived_attachments
                    WHERE message_id = ? AND attachment_ref = ?
                    """,
                    (message_id, attachment_id),
                ).fetchone()
                if existing is not None:
                    self._store_reference(
                        connection,
                        operation=operation,
                        idempotency_key=idempotency_key,
                        payload_hash=payload_hash,
                        kind="archive",
                        resource_id=existing["id"],
                        version=1,
                    )
                    return self._archive_view(existing)
                account_id = message["account_id"]
                graph_message_id = message["graph_message_id"]
                subject = message["subject"]
            attachments = self._fetch_attachments(message_id)
            attachment = next(
                (item for item in attachments if item["id"] == attachment_id), None
            )
            if attachment is None:
                raise PocketError(404, "附件不存在")
            self._require_archivable_attachment(attachment)
            response = self._graph_request(
                account_id,
                "GET",
                f"/me/messages/{quote(graph_message_id, safe='')}/attachments/"
                f"{quote(attachment['graph_id'], safe='')}/$value",
                max_bytes=self.max_file_bytes,
                prefer=IMMUTABLE_ID_PREFERENCE,
                accept="application/octet-stream",
            )
            if response.status != 200:
                raise self._pocket_remote_error(
                    self.graph.http_error(response, "attachment_download_failed")
                )
            content = response.body
            if attachment["size_bytes"] and len(content) != attachment["size_bytes"]:
                raise PocketError(502, "Outlook 附件大小与元数据不一致")
            self._validate_attachment_content(
                attachment["name"], attachment["mime_type"], content
            )
            content_hash = hashlib.sha256(content).hexdigest()
            archive_id = "oarc_" + self.secret_box.opaque_reference(
                "archive", message_id, attachment_id
            )[:32]
            relative_path = f"outlook-attachments/{archive_id}.bin"
            self._store_archive_blob(archive_id, relative_path, content, content_hash)
            document = self.workspace_service.create_document(
                DEFAULT_WORKSPACE_ID,
                {
                    "domain": "work",
                    "kind": "general",
                    "title": f"邮件附件：{attachment['name']}",
                    "content": (
                        "该 Outlook 附件已加密归档。\n\n"
                        "状态：未扫描、隔离保存；使用前请进行安全检查。\n\n"
                        f"SHA-256：{content_hash}"
                    ),
                    "mime_type": attachment["mime_type"],
                    "storage_ref": f"mailblob://{archive_id}",
                    "source_item_id": None,
                    "source": {
                        "source_kind": "email",
                        "source_ref": f"mail://message/{message_id}/attachment/{attachment_id}",
                        "excerpt": subject[:2_000],
                        "authority": "observed",
                    },
                    "tags": ["outlook", "附件", "未扫描"],
                    "access_scope": "owner_only",
                    "viewer_member_ids": [],
                    "client_mutation_id": archive_id,
                },
                idempotency_key=f"outlook-archive-{archive_id}",
                device_id=device_id,
            )
            with self.database.transaction() as connection:
                current = self._message_row(connection, message_id)
                self._require_version(current, expected_version)
                now = utc_now()
                connection.execute(
                    """
                    INSERT OR IGNORE INTO outlook_archived_attachments(
                        id, message_id, attachment_ref, file_name, mime_type,
                        size_bytes, content_hash, archive_relpath, item_id,
                        document_id, archived_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        archive_id,
                        message_id,
                        attachment_id,
                        attachment["name"],
                        attachment["mime_type"],
                        len(content),
                        content_hash,
                        relative_path,
                        document["id"],
                        now,
                    ),
                )
                archived = connection.execute(
                    "SELECT * FROM outlook_archived_attachments WHERE id = ?",
                    (archive_id,),
                ).fetchone()
                self._store_reference(
                    connection,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                    kind="archive",
                    resource_id=archive_id,
                    version=1,
                )
                return self._archive_view(archived)

    @staticmethod
    def _safe_file_name(value: object) -> str:
        cleaned = sanitize_outlook_text(value, max_chars=200).replace("/", "_").replace("\\", "_")
        cleaned = cleaned.strip(" .")
        return cleaned or "attachment"

    def _require_archivable_attachment(self, attachment: dict[str, Any]) -> None:
        if attachment["type"] != "file" or attachment["is_inline"]:
            raise PocketError(422, "首版只允许归档非内联文件附件")
        if attachment["size_bytes"] <= 0 or attachment["size_bytes"] > self.max_file_bytes:
            raise PocketError(413, "附件大小不在归档范围内")
        extension = Path(attachment["name"]).suffix.casefold()
        allowed = {
            ".pdf": "application/pdf",
            ".txt": "text/plain",
            ".md": "text/markdown",
            ".csv": "text/csv",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        }
        if allowed.get(extension) != attachment["mime_type"]:
            raise PocketError(422, "附件扩展名与允许的 MIME 类型不匹配")

    @staticmethod
    def _validate_attachment_content(name: str, mime_type: str, content: bytes) -> None:
        extension = Path(name).suffix.casefold()
        if mime_type == "application/pdf":
            if not content.startswith(b"%PDF-") or any(
                marker in content for marker in (b"/JavaScript", b"/OpenAction", b"/Launch")
            ):
                raise PocketError(422, "PDF 内容未通过首版安全检查")
            return
        if mime_type in {"text/plain", "text/markdown", "text/csv"}:
            if b"\x00" in content:
                raise PocketError(422, "文本附件包含不允许的二进制内容")
            try:
                content.decode("utf-8-sig")
            except UnicodeDecodeError as error:
                raise PocketError(422, "文本附件必须是 UTF-8") from error
            return
        if extension not in {".docx", ".xlsx", ".pptx"}:
            raise PocketError(422, "附件类型不允许归档")
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                entries = archive.infolist()
                if not entries or len(entries) > 1_000:
                    raise PocketError(422, "Office 附件结构无效")
                names = {entry.filename.casefold() for entry in entries}
                if "[content_types].xml" not in names:
                    raise PocketError(422, "Office 附件结构无效")
                expanded = 0
                for entry in entries:
                    lowered = entry.filename.casefold()
                    expanded += entry.file_size
                    if (
                        entry.flag_bits & 0x1
                        or entry.file_size > 32 * 1024 * 1024
                        or expanded > 64 * 1024 * 1024
                        or "vbaproject" in lowered
                        or lowered.endswith(".bin")
                        or "customui/" in lowered
                        or ".." in Path(lowered).parts
                    ):
                        raise PocketError(422, "Office 附件包含宏、加密或异常内容")
        except (zipfile.BadZipFile, RuntimeError) as error:
            raise PocketError(422, "Office 附件结构无效") from error

    def _store_archive_blob(
        self, archive_id: str, relative_path: str, content: bytes, content_hash: str
    ) -> None:
        directory_name = "outlook-attachments"
        file_name = f"{archive_id}.bin"
        if relative_path != f"{directory_name}/{file_name}":
            raise PocketError(500, "附件归档标识无效")
        root_descriptor: int | None = None
        directory_descriptor: int | None = None
        file_descriptor: int | None = None
        temporary_name = f".{file_name}.{secrets.token_hex(8)}.tmp"
        try:
            root_descriptor = os.open(
                self.data_root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            root_stat = os.fstat(root_descriptor)
            if (
                not stat.S_ISDIR(root_stat.st_mode)
                or root_stat.st_uid != os.getuid()
                or stat.S_IMODE(root_stat.st_mode) & 0o077
            ):
                raise PocketError(503, "Outlook 数据目录权限不安全")
            try:
                os.mkdir(directory_name, mode=0o700, dir_fd=root_descriptor)
            except FileExistsError:
                pass
            directory_descriptor = os.open(
                directory_name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_descriptor,
            )
            directory_stat = os.fstat(directory_descriptor)
            if (
                not stat.S_ISDIR(directory_stat.st_mode)
                or directory_stat.st_uid != os.getuid()
                or stat.S_IMODE(directory_stat.st_mode) & 0o077
            ):
                raise PocketError(503, "Outlook 附件目录权限不安全")
            try:
                file_descriptor = os.open(
                    file_name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_descriptor,
                )
            except FileNotFoundError:
                file_descriptor = None
            if file_descriptor is not None:
                metadata = os.fstat(file_descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) & 0o077
                    or metadata.st_size > self.max_file_bytes + 28
                ):
                    raise PocketError(503, "既有附件归档权限或格式无效")
                chunks: list[bytes] = []
                remaining = metadata.st_size
                while remaining:
                    chunk = os.read(file_descriptor, min(65_536, remaining))
                    if not chunk:
                        raise PocketError(503, "既有附件归档读取不完整")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                existing = self.secret_box.decrypt_bytes(
                    "attachment_archive", archive_id, b"".join(chunks)
                )
                if not secrets.compare_digest(
                    hashlib.sha256(existing).hexdigest(), content_hash
                ):
                    raise PocketError(409, "附件归档标识发生冲突")
                return
            encrypted = self.secret_box.encrypt_bytes(
                "attachment_archive", archive_id, content
            )
            file_descriptor = os.open(
                temporary_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_descriptor,
            )
            written = 0
            while written < len(encrypted):
                written += os.write(file_descriptor, encrypted[written:])
            os.fsync(file_descriptor)
            os.close(file_descriptor)
            file_descriptor = None
            os.replace(
                temporary_name,
                file_name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
            os.fsync(directory_descriptor)
        except OutlookSecurityError as error:
            raise PocketError(503, "既有附件归档无法安全恢复") from error
        except OSError as error:
            raise PocketError(503, "无法写入加密附件归档") from error
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            if directory_descriptor is not None:
                try:
                    os.unlink(temporary_name, dir_fd=directory_descriptor)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
                os.close(directory_descriptor)
            if root_descriptor is not None:
                os.close(root_descriptor)

    # Task candidates require an explicit Owner confirmation before creating a memo.

    def list_task_candidates(
        self, account_id: str, *, status: str, limit: int
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            self._account_row(connection, account_id)
            total = connection.execute(
                """
                SELECT COUNT(*) FROM outlook_task_candidates candidate
                JOIN outlook_messages message ON message.id = candidate.message_id
                WHERE message.account_id = ? AND candidate.status = ?
                """,
                (account_id, status),
            ).fetchone()[0]
            rows = connection.execute(
                """
                SELECT candidate.* FROM outlook_task_candidates candidate
                JOIN outlook_messages message ON message.id = candidate.message_id
                WHERE message.account_id = ? AND candidate.status = ?
                ORDER BY candidate.created_at DESC LIMIT ?
                """,
                (account_id, status, limit),
            ).fetchall()
        return {"items": [self._candidate_view(row) for row in rows], "total": int(total)}

    def confirm_task_candidate(
        self,
        candidate_id: str,
        expected_version: int,
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"candidate.confirm:{candidate_id}"
        request = {
            "candidate_id": candidate_id,
            "expected_version": expected_version,
            "device_id": device_id,
        }
        with self._operation_lock:
            with self.database.connect() as connection:
                cached, payload_hash = self._idempotent_reference(
                    connection,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    payload=request,
                )
                if cached is not None:
                    return cached
                candidate = self._candidate_row(connection, candidate_id)
                self._require_version(candidate, expected_version)
                if candidate["status"] != "pending":
                    raise PocketError(409, "任务候选已经处理")
                message = self._message_row(connection, candidate["message_id"])
            memo = self.workspace_service.create_memo(
                DEFAULT_WORKSPACE_ID,
                {
                    "record_type": "task_candidate",
                    "domain": "work",
                    "horizon": "short_term",
                    "urgency": candidate["priority"],
                    "title": candidate["title"],
                    "content": (
                        f"{candidate['summary']}\n\n目的：{candidate['purpose']}\n"
                        f"目标：{candidate['objective']}\n策略：{candidate['strategy']}"
                    ),
                    "source": {
                        "source_kind": "email",
                        "source_ref": f"mail://message/{message['id']}",
                        "excerpt": candidate["summary"][:20_000],
                        "authority": "observed",
                    },
                    "tags": ["outlook", "待办候选"],
                    "confirmation_status": "confirmed",
                    "client_mutation_id": candidate_id,
                },
                idempotency_key=f"outlook-candidate-{candidate_id}",
                device_id=device_id,
            )
            with self.database.transaction() as connection:
                current = self._candidate_row(connection, candidate_id)
                self._require_version(current, expected_version)
                now = utc_now()
                connection.execute(
                    """
                    UPDATE outlook_task_candidates
                    SET status = 'confirmed', task_id = ?, resolved_at = ?,
                        updated_at = ?, version = version + 1
                    WHERE id = ? AND version = ? AND status = 'pending'
                    """,
                    (memo["id"], now, now, candidate_id, expected_version),
                )
                updated = self._candidate_row(connection, candidate_id)
                self._store_reference(
                    connection,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                    kind="candidate_confirm",
                    resource_id=candidate_id,
                    version=updated["version"],
                )
                return {"candidate": self._candidate_view(updated), "memo": memo}

    def dismiss_task_candidate(
        self,
        candidate_id: str,
        expected_version: int,
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"candidate.dismiss:{candidate_id}"
        request = {
            "candidate_id": candidate_id,
            "expected_version": expected_version,
            "device_id": device_id,
        }
        with self._operation_lock, self.database.transaction() as connection:
            cached, payload_hash = self._idempotent_reference(
                connection,
                operation=operation,
                idempotency_key=idempotency_key,
                payload=request,
            )
            if cached is not None:
                return cached
            row = self._candidate_row(connection, candidate_id)
            self._require_version(row, expected_version)
            if row["status"] != "pending":
                raise PocketError(409, "任务候选已经处理")
            now = utc_now()
            connection.execute(
                """
                UPDATE outlook_task_candidates
                SET status = 'dismissed', resolved_at = ?, updated_at = ?,
                    version = version + 1
                WHERE id = ? AND version = ? AND status = 'pending'
                """,
                (now, now, candidate_id, expected_version),
            )
            updated = self._candidate_row(connection, candidate_id)
            self._store_reference(
                connection,
                operation=operation,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                kind="candidate",
                resource_id=candidate_id,
                version=updated["version"],
            )
            return {"candidate": self._candidate_view(updated)}

    @staticmethod
    def _candidate_row(connection: sqlite3.Connection, candidate_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM outlook_task_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise PocketError(404, "任务候选不存在")
        return row

    def _memo_by_id(self, memo_id: str) -> dict[str, Any] | None:
        memos = self.workspace_service.list_memos(DEFAULT_WORKSPACE_ID)["items"]
        return next((memo for memo in memos if memo["id"] == memo_id), None)

    # Local reply drafts and one-shot send intents

    def _draft_view(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        placeholders = ",".join("?" for _ in ASSOCIATED_INTENT_STATUSES)
        active_intent = connection.execute(
            f"""
            SELECT id FROM outlook_send_intents
            WHERE draft_id = ? AND status IN ({placeholders})
            ORDER BY created_at DESC LIMIT 1
            """,
            (row["id"], *ASSOCIATED_INTENT_STATUSES),
        ).fetchone()
        return {
            "id": row["id"],
            "account_id": row["account_id"],
            "reply_to_message_id": row["reply_to_message_id"],
            "to_recipients": _json_load(row["to_recipients_json"], []),
            "cc_recipients": _json_load(row["cc_recipients_json"], []),
            "subject": row["subject"],
            "body_text": row["body_text"],
            "status": row["status"],
            "active_send_intent_id": (
                active_intent["id"] if active_intent is not None else None
            ),
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "sent_at": row["sent_at"],
        }

    def _intent_view(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        draft = self._draft_row(connection, row["draft_id"])
        account = self._account_row(connection, draft["account_id"])
        preview = self._draft_preview(
            account, draft, from_address=row["sender_address"]
        )
        return {
            "id": row["id"],
            "draft_id": row["draft_id"],
            "draft_version": row["draft_version"],
            "status": (
                "sent_items_verified" if row["status"] == "sent" else row["status"]
            ),
            "preview": preview,
            "preview_hash": self._preview_hash(preview),
            "expires_at": row["expires_at"],
            "version": row["version"],
            "send_started_at": row["send_started_at"],
            "verified_at": row["verified_at"],
            "sent_at": row["sent_at"],
            "last_error_code": row["last_error_code"],
        }

    @staticmethod
    def _draft_preview(
        account: sqlite3.Row,
        draft: sqlite3.Row,
        *,
        from_address: str | None = None,
    ) -> dict[str, Any]:
        return {
            "from_label": account["account_label"],
            "from_address": from_address,
            "to_recipients": _json_load(draft["to_recipients_json"], []),
            "cc_recipients": _json_load(draft["cc_recipients_json"], []),
            "subject": draft["subject"],
            "body_text": draft["body_text"],
        }

    @staticmethod
    def _preview_hash(preview: dict[str, Any]) -> str:
        return hashlib.sha256(_json(preview).encode("utf-8")).hexdigest()

    @staticmethod
    def _content_hash(
        to_recipients: list[dict[str, Any]],
        cc_recipients: list[dict[str, Any]],
        subject: str,
        body_text: str,
    ) -> str:
        return _request_hash(
            {
                "to_recipients": to_recipients,
                "cc_recipients": cc_recipients,
                "subject": subject,
                "body_text": body_text,
            }
        )

    def create_reply_draft(
        self,
        message_id: str,
        expected_version: int,
        body_text: str | None,
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"reply_draft.create:{message_id}"
        request = {
            "message_id": message_id,
            "expected_version": expected_version,
            "body_text": body_text,
            "device_id": device_id,
        }
        with self._operation_lock, self.database.transaction() as connection:
            cached, payload_hash = self._idempotent_reference(
                connection,
                operation=operation,
                idempotency_key=idempotency_key,
                payload=request,
            )
            if cached is not None:
                return cached
            message = self._message_row(connection, message_id)
            self._require_version(message, expected_version)
            account = self._account_row(connection, message["account_id"])
            if account["status"] != "connected":
                raise PocketError(409, "Outlook 账户未连接")
            sender = _json_load(message["sender_json"], None)
            if not isinstance(sender, dict) or not sender.get("address"):
                raise PocketError(422, "原邮件没有可用的回复地址")
            reply_address = self._strict_reply_address(sender["address"])
            recipients = [
                {
                    "name": self._strict_display_name(sender.get("name")),
                    "address": reply_address,
                }
            ]
            subject = self._single_line(message["subject"], maximum=490)
            reply_subject = subject if subject.casefold().startswith("re:") else f"Re: {subject}"
            requested_body = self._canonical_body(body_text) if body_text is not None else ""
            existing = connection.execute(
                """
                SELECT * FROM outlook_local_drafts
                WHERE account_id = ? AND reply_to_message_id = ?
                  AND status IN ('editing', 'preparing', 'prepared', 'sending', 'uncertain')
                LIMIT 1
                """,
                (message["account_id"], message_id),
            ).fetchone()
            if existing is not None:
                if body_text is not None and existing["body_text"] != requested_body:
                    raise PocketError(409, "该邮件已有回复草稿，请打开既有草稿编辑")
                self._store_reference(
                    connection,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                    kind="draft",
                    resource_id=existing["id"],
                    version=existing["version"],
                )
                return self._draft_view(connection, existing)
            draft_id = new_id("maildraft")
            now = utc_now()
            content_hash = self._content_hash(recipients, [], reply_subject, requested_body)
            try:
                connection.execute(
                    """
                    INSERT INTO outlook_local_drafts(
                        id, account_id, reply_to_message_id, to_recipients_json,
                        cc_recipients_json, subject, body_text, content_hash,
                        status, version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, '[]', ?, ?, ?, 'editing', 1, ?, ?)
                    """,
                    (
                        draft_id,
                        message["account_id"],
                        message_id,
                        _json(recipients),
                        reply_subject,
                        requested_body,
                        content_hash,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise PocketError(409, "该邮件已有回复草稿，请打开既有草稿") from error
            draft = self._draft_row(connection, draft_id)
            self._store_reference(
                connection,
                operation=operation,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                kind="draft",
                resource_id=draft_id,
                version=1,
            )
            return self._draft_view(connection, draft)

    @staticmethod
    def _strict_reply_address(value: object) -> str:
        if not isinstance(value, str):
            raise PocketError(422, "原邮件回复地址不安全")
        address = value.strip().casefold()
        if (
            len(address) > 320
            or any(ord(character) < 33 or ord(character) == 127 for character in address)
            or not STRICT_EMAIL_PATTERN.fullmatch(address)
        ):
            raise PocketError(422, "原邮件回复地址不安全")
        return address

    @staticmethod
    def _single_line(value: object, *, maximum: int) -> str:
        cleaned = sanitize_outlook_text(value, max_chars=maximum)
        return " ".join(cleaned.replace("\t", " ").splitlines()).strip()

    @classmethod
    def _strict_display_name(cls, value: object) -> str | None:
        cleaned = cls._single_line(value, maximum=200)
        return cleaned or None

    @staticmethod
    def _canonical_body(value: object) -> str:
        body = sanitize_outlook_text(value, max_chars=100_000)
        if not body:
            raise PocketError(422, "回复正文不能为空")
        return body

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            return self._draft_view(connection, self._draft_row(connection, draft_id))

    def update_draft(
        self,
        draft_id: str,
        expected_version: int,
        body_text: str,
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"draft.update:{draft_id}"
        request = {
            "draft_id": draft_id,
            "expected_version": expected_version,
            "body_text": body_text,
            "device_id": device_id,
        }
        with self._operation_lock, self.database.transaction() as connection:
            cached, payload_hash = self._idempotent_reference(
                connection,
                operation=operation,
                idempotency_key=idempotency_key,
                payload=request,
            )
            if cached is not None:
                return cached
            draft = self._draft_row(connection, draft_id)
            self._require_version(draft, expected_version)
            if draft["status"] != "editing":
                raise PocketError(409, "草稿已进入发送准备，不能继续编辑")
            canonical_body = self._canonical_body(body_text)
            content_hash = self._content_hash(
                _json_load(draft["to_recipients_json"], []),
                _json_load(draft["cc_recipients_json"], []),
                draft["subject"],
                canonical_body,
            )
            now = utc_now()
            connection.execute(
                """
                UPDATE outlook_local_drafts
                SET body_text = ?, content_hash = ?, version = version + 1,
                    updated_at = ?
                WHERE id = ? AND version = ? AND status = 'editing'
                """,
                (canonical_body, content_hash, now, draft_id, expected_version),
            )
            updated = self._draft_row(connection, draft_id)
            self._store_reference(
                connection,
                operation=operation,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                kind="draft",
                resource_id=draft_id,
                version=updated["version"],
            )
            return self._draft_view(connection, updated)

    def prepare_draft(
        self,
        draft_id: str,
        expected_version: int,
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"draft.prepare:{draft_id}"
        request = {
            "draft_id": draft_id,
            "expected_version": expected_version,
            "device_id": device_id,
        }
        with self._operation_lock:
            with self.database.transaction() as connection:
                cached, payload_hash = self._idempotent_reference(
                    connection,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    payload=request,
                )
                if cached is not None:
                    return cached
                draft = self._draft_row(connection, draft_id)
                active = self._active_intent_row(connection, draft_id)
                if active is not None and active["draft_version"] == expected_version:
                    self._store_reference(
                        connection,
                        operation=operation,
                        idempotency_key=idempotency_key,
                        payload_hash=payload_hash,
                        kind="intent",
                        resource_id=active["id"],
                        version=active["version"],
                    )
                    return self._intent_view(connection, active)
                self._require_version(draft, expected_version)
                if draft["status"] != "editing":
                    raise PocketError(409, "草稿当前不能进入发送准备")
                if not draft["body_text"].strip():
                    raise PocketError(422, "回复正文不能为空")
                recipients = _json_load(draft["to_recipients_json"], [])
                if len(recipients) != 1:
                    raise PocketError(422, "回复收件人绑定无效")
                account = self._account_row(connection, draft["account_id"])
                if account["status"] != "connected":
                    raise PocketError(409, "Outlook 账户未连接")
                intent_id = new_id("sendintent")
                marker = secrets.token_hex(32)
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO outlook_send_intents(
                        id, draft_id, draft_version, content_hash, marker_value,
                        status, version, expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'preparing', 1, ?, ?, ?)
                    """,
                    (
                        intent_id,
                        draft_id,
                        expected_version,
                        draft["content_hash"],
                        marker,
                        _after(minutes=15),
                        now,
                        now,
                    ),
                )
                claimed_draft = connection.execute(
                    """
                    UPDATE outlook_local_drafts
                    SET status = 'preparing', version = version + 1, updated_at = ?
                    WHERE id = ? AND version = ? AND status = 'editing'
                    """,
                    (now, draft_id, expected_version),
                )
                if claimed_draft.rowcount != 1:
                    raise PocketError(409, "草稿状态已变化，未创建远端草稿")
                frozen_draft = draft
            remote_payload = self._remote_draft_payload(frozen_draft, marker)
            try:
                response = self._graph_request(
                    frozen_draft["account_id"],
                    "POST",
                    "/me/messages",
                    body=remote_payload,
                    max_bytes=512 * 1024,
                    prefer=IMMUTABLE_ID_PREFERENCE,
                )
            except (OutlookRemoteError, PocketError):
                return self._finish_prepare(
                    intent_id,
                    status="prepare_uncertain",
                    error_code="prepare_transport_unknown",
                    operation=operation,
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                )
            if response.status not in {200, 201}:
                remote = self.graph.http_error(response, "prepare_failed")
                uncertain = response.status in {401, 429} or response.status >= 500
                return self._finish_prepare(
                    intent_id,
                    status="prepare_uncertain" if uncertain else "failed",
                    error_code=remote.code,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                )
            try:
                payload = self.graph.json_object(response)
                remote_id = self._strict_remote_opaque(
                    payload.get("id"), maximum=2_048
                )
                if not remote_id or payload.get("isDraft") is not True:
                    raise OutlookRemoteError("invalid_draft_response")
            except OutlookRemoteError:
                return self._finish_prepare(
                    intent_id,
                    status="prepare_uncertain",
                    error_code="invalid_draft_response",
                    operation=operation,
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                )
            snapshot = self._remote_message_snapshot(
                frozen_draft["account_id"],
                remote_id,
                marker,
                frozen_draft,
                require_draft=True,
            )
            if snapshot is None:
                return self._finish_prepare(
                    intent_id,
                    status="prepare_uncertain",
                    error_code="remote_snapshot_unavailable",
                    remote_id=remote_id,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                )
            if not snapshot["matches"]:
                return self._finish_prepare(
                    intent_id,
                    status="failed",
                    error_code="remote_draft_mismatch",
                    remote_id=remote_id,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                )
            return self._finish_prepare(
                intent_id,
                status="ready",
                error_code=None,
                remote_id=remote_id,
                remote_snapshot_hash=snapshot["snapshot_hash"],
                remote_change_key=snapshot["change_key"],
                sender_address=snapshot["authoritative_sender"],
                operation=operation,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
            )

    @staticmethod
    def _remote_recipients(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "emailAddress": {
                    "name": value.get("name") or "",
                    "address": value["address"],
                }
            }
            for value in values
        ]

    def _remote_draft_payload(
        self, draft: sqlite3.Row, marker: str
    ) -> dict[str, Any]:
        return {
            "subject": draft["subject"],
            "body": {"contentType": "Text", "content": draft["body_text"]},
            "toRecipients": self._remote_recipients(
                _json_load(draft["to_recipients_json"], [])
            ),
            "ccRecipients": [],
            "bccRecipients": [],
            "replyTo": [],
            "importance": "normal",
            "isReadReceiptRequested": False,
            "isDeliveryReceiptRequested": False,
            "singleValueExtendedProperties": [
                {"id": SEND_INTENT_PROPERTY_ID, "value": marker}
            ],
        }

    def _remote_message_snapshot(
        self,
        account_id: str,
        remote_id: str,
        marker: str,
        draft: sqlite3.Row,
        *,
        require_draft: bool,
    ) -> dict[str, Any] | None:
        expand = (
            "singleValueExtendedProperties($filter=id eq "
            f"'{SEND_INTENT_PROPERTY_ID}')"
        )
        query = urlencode(
            {
                "$select": (
                    "id,isDraft,changeKey,subject,body,toRecipients,ccRecipients,"
                    "bccRecipients,replyTo,from,sender,parentFolderId,sentDateTime,"
                    "hasAttachments,isReadReceiptRequested,"
                    "isDeliveryReceiptRequested,importance"
                ),
                "$expand": expand,
            },
            quote_via=quote,
        )
        try:
            response = self._graph_request(
                account_id,
                "GET",
                f"/me/messages/{quote(remote_id, safe='')}?{query}",
                max_bytes=512 * 1024,
                prefer='IdType="ImmutableId", outlook.body-content-type="text"',
            )
        except (OutlookRemoteError, PocketError):
            return None
        if response.status != 200:
            return None
        try:
            payload = self.graph.json_object(response)
        except OutlookRemoteError:
            return None
        actual_is_draft = payload.get("isDraft")
        if actual_is_draft is not require_draft:
            return {
                "matches": False,
                "snapshot_hash": "",
                "change_key": "",
                "actual_is_draft": actual_is_draft,
            }
        attachment_state = self._remote_message_has_no_attachments(
            account_id, remote_id
        )
        if attachment_state is None:
            return None
        body = payload.get("body")
        if (
            not isinstance(body, dict)
            or str(body.get("contentType", "")).casefold() != "text"
        ):
            return {
                "matches": False,
                "snapshot_hash": "",
                "change_key": "",
                "actual_is_draft": actual_is_draft,
            }
        body_text = self._strict_remote_text(
            body.get("content"), max_chars=100_000, multiline=True
        )
        subject = self._strict_remote_text(
            payload.get("subject"), max_chars=500, multiline=False
        )
        to_recipients = self._strict_remote_recipients(payload.get("toRecipients"))
        cc_recipients = self._strict_remote_recipients(payload.get("ccRecipients"))
        bcc_recipients = self._strict_remote_recipients(payload.get("bccRecipients"))
        reply_to_recipients = self._strict_remote_recipients(payload.get("replyTo"))
        from_address = self._strict_remote_party(payload.get("from"))
        sender_address = self._strict_remote_party(payload.get("sender"))
        authoritative_sender = from_address or sender_address
        sender_consistent = (
            from_address is None
            or sender_address is None
            or secrets.compare_digest(from_address, sender_address)
        )
        properties = payload.get("singleValueExtendedProperties")
        marker_values: list[str] = []
        if isinstance(properties, list):
            marker_values = [
                str(value.get("value"))
                for value in properties
                if isinstance(value, dict)
                and value.get("id") == SEND_INTENT_PROPERTY_ID
                and isinstance(value.get("value"), str)
            ]
        change_key = self._strict_remote_opaque(payload.get("changeKey"), maximum=1_000)
        message_id = self._strict_remote_opaque(payload.get("id"), maximum=2_048)
        importance = payload.get("importance")
        read_receipt = payload.get("isReadReceiptRequested")
        delivery_receipt = payload.get("isDeliveryReceiptRequested")
        local_to = [
            {
                "name": value.get("name") or "",
                "address": self._strict_reply_address(value.get("address")),
            }
            for value in _json_load(draft["to_recipients_json"], [])
            if isinstance(value, dict)
        ]
        matches = (
            len(local_to) == 1
            and to_recipients == local_to
            and cc_recipients == []
            and bcc_recipients == []
            and reply_to_recipients == []
            and authoritative_sender is not None
            and sender_consistent
            and subject == draft["subject"]
            and body_text == draft["body_text"]
            and marker_values == [marker]
            and attachment_state is True
            and payload.get("hasAttachments") is False
            and read_receipt is False
            and delivery_receipt is False
            and importance == "normal"
            and message_id is not None
            and secrets.compare_digest(message_id, remote_id)
            and bool(change_key)
        )
        snapshot_payload = {
            "to": to_recipients,
            "cc": cc_recipients,
            "bcc": bcc_recipients,
            "reply_to": reply_to_recipients,
            "authoritative_sender": authoritative_sender,
            "subject": subject,
            "body_text": body_text,
            "marker": marker_values[0] if len(marker_values) == 1 else None,
            "attachments": [],
            "is_read_receipt_requested": read_receipt,
            "is_delivery_receipt_requested": delivery_receipt,
            "importance": importance,
        }
        return {
            "matches": matches,
            "snapshot_hash": _request_hash(snapshot_payload),
            "change_key": change_key,
            "actual_is_draft": actual_is_draft,
            "authoritative_sender": authoritative_sender,
            "sent_at": self._remote_datetime(payload.get("sentDateTime")),
            "parent_folder_id": self._strict_remote_opaque(
                payload.get("parentFolderId"), maximum=2_048
            ),
        }

    def _remote_message_has_no_attachments(
        self, account_id: str, remote_id: str
    ) -> bool | None:
        query = urlencode({"$select": "id", "$top": "1"}, quote_via=quote)
        try:
            response = self._graph_request(
                account_id,
                "GET",
                f"/me/messages/{quote(remote_id, safe='')}/attachments?{query}",
                max_bytes=128 * 1024,
                prefer=IMMUTABLE_ID_PREFERENCE,
            )
        except (OutlookRemoteError, PocketError):
            return None
        if response.status != 200:
            return None
        try:
            payload = self.graph.json_object(response)
        except OutlookRemoteError:
            return None
        values = payload.get("value")
        if not isinstance(values, list):
            return None
        if "@odata.nextLink" in payload or "@odata.deltaLink" in payload:
            return False
        return len(values) == 0

    @staticmethod
    def _strict_remote_text(
        value: object, *, max_chars: int, multiline: bool
    ) -> str | None:
        """Canonicalize only Graph's documented newline/Unicode representation.

        This validator is deliberately non-lossy: evidence is rejected instead of
        truncated, stripped, or silently cleaned before exact-preview comparison.
        """

        if not isinstance(value, str):
            return None
        normalized = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace(
            "\r", "\n"
        )
        if len(normalized) > max_chars:
            return None
        for character in normalized:
            if character == "\n":
                if multiline:
                    continue
                return None
            if character == "\t":
                if multiline:
                    continue
                return None
            if unicodedata.category(character).startswith("C"):
                return None
        return normalized

    @classmethod
    def _strict_remote_opaque(
        cls, value: object, *, maximum: int
    ) -> str | None:
        normalized = cls._strict_remote_text(
            value, max_chars=maximum, multiline=False
        )
        if not normalized or normalized != normalized.strip():
            return None
        return normalized

    def _strict_remote_recipients(
        self, value: object
    ) -> list[dict[str, str]] | None:
        if not isinstance(value, list) or len(value) > 100:
            return None
        result: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, dict):
                return None
            email = item.get("emailAddress")
            if not isinstance(email, dict):
                return None
            raw_address = email.get("address")
            raw_name = email.get("name")
            if (
                not isinstance(raw_address, str)
                or raw_address != raw_address.strip()
                or not isinstance(raw_name, str)
            ):
                return None
            name = self._strict_remote_text(
                raw_name, max_chars=200, multiline=False
            )
            if name is None:
                return None
            try:
                address = self._strict_reply_address(raw_address)
            except PocketError:
                return None
            result.append({"name": name, "address": address})
        return result

    def _strict_remote_party(self, value: object) -> str | None:
        if not isinstance(value, dict):
            return None
        email = value.get("emailAddress")
        if not isinstance(email, dict):
            return None
        raw_address = email.get("address")
        if not isinstance(raw_address, str) or raw_address != raw_address.strip():
            return None
        try:
            return self._strict_reply_address(raw_address)
        except PocketError:
            return None

    def _finish_prepare(
        self,
        intent_id: str,
        *,
        status: str,
        error_code: str | None,
        operation: str,
        idempotency_key: str,
        payload_hash: str,
        remote_id: str | None = None,
        remote_snapshot_hash: str | None = None,
        remote_change_key: str | None = None,
        sender_address: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.database.transaction() as connection:
            intent = self._intent_row(connection, intent_id)
            if intent["status"] != "preparing":
                raise PocketError(409, "发送准备状态已变化")
            connection.execute(
                """
                UPDATE outlook_send_intents
                SET status = ?, remote_graph_id = ?, remote_snapshot_hash = ?,
                    remote_change_key = ?, sender_address = ?, last_error_code = ?,
                    version = version + 1, updated_at = ?
                WHERE id = ? AND status = 'preparing'
                """,
                (
                    status,
                    remote_id,
                    remote_snapshot_hash,
                    remote_change_key,
                    sender_address,
                    error_code,
                    now,
                    intent_id,
                ),
            )
            draft_status = (
                "prepared"
                if status == "ready"
                else "canceled"
                if status == "failed"
                else "uncertain"
            )
            connection.execute(
                """
                UPDATE outlook_local_drafts
                SET status = ?, version = version + 1, updated_at = ?
                WHERE id = ? AND status = 'preparing'
                """,
                (draft_status, now, intent["draft_id"]),
            )
            updated = self._intent_row(connection, intent_id)
            self._store_reference(
                connection,
                operation=operation,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                kind="intent",
                resource_id=intent_id,
                version=updated["version"],
            )
            return self._intent_view(connection, updated)

    def get_send_intent(self, intent_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            return self._intent_view(connection, self._intent_row(connection, intent_id))

    def confirm_send_intent(
        self,
        intent_id: str,
        expected_version: int,
        preview_hash: str,
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"send_intent.confirm:{intent_id}"
        request = {
            "intent_id": intent_id,
            "expected_version": expected_version,
            "preview_hash": preview_hash,
            "confirmation": "send_exact_preview",
            "device_id": device_id,
        }
        with self._operation_lock:
            with self.database.connect() as connection:
                cached, payload_hash = self._idempotent_reference(
                    connection,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    payload=request,
                )
                if cached is not None:
                    return cached
                intent = self._intent_row(connection, intent_id)
                self._require_version(intent, expected_version)
                if intent["status"] != "ready":
                    raise PocketError(409, "发送意图未处于可确认状态；请只执行核验")
                expired = parse_utc(intent["expires_at"]) <= datetime.now(UTC)
                draft = self._draft_row(connection, intent["draft_id"])
                if draft["status"] != "prepared":
                    raise PocketError(409, "本地草稿状态不一致；不会发送")
                sender_address = intent["sender_address"]
                if not isinstance(sender_address, str):
                    raise PocketError(409, "发件邮箱身份尚未核验；不会发送")
                account = self._account_row(connection, draft["account_id"])
                actual_preview_hash = self._preview_hash(
                    self._draft_preview(
                        account, draft, from_address=sender_address
                    )
                )
                if not secrets.compare_digest(preview_hash, actual_preview_hash):
                    raise PocketError(409, "发送预览已变化，请重新核对")
                if not secrets.compare_digest(intent["content_hash"], draft["content_hash"]):
                    raise PocketError(409, "草稿内容已变化，请重新准备")
                remote_id = intent["remote_graph_id"]
                marker = intent["marker_value"]
                account_id = draft["account_id"]
                if not remote_id:
                    raise PocketError(409, "远端草稿尚未确认，只能执行核验")
            if expired:
                with self.database.transaction() as connection:
                    current = self._intent_row(connection, intent_id)
                    self._require_version(current, expected_version)
                    now = utc_now()
                    connection.execute(
                        """
                        UPDATE outlook_send_intents
                        SET status = 'expired', last_error_code = 'confirmation_expired',
                            version = version + 1, updated_at = ?
                        WHERE id = ? AND version = ? AND status = 'ready'
                        """,
                        (now, intent_id, expected_version),
                    )
                    connection.execute(
                        """
                        UPDATE outlook_local_drafts
                        SET status = 'canceled', version = version + 1, updated_at = ?
                        WHERE id = ? AND status = 'prepared'
                        """,
                        (now, current["draft_id"]),
                    )
                    expired = self._intent_row(connection, intent_id)
                    self._store_reference(
                        connection,
                        operation=operation,
                        idempotency_key=idempotency_key,
                        payload_hash=payload_hash,
                        kind="intent",
                        resource_id=intent_id,
                        version=expired["version"],
                    )
                    return self._intent_view(connection, expired)
            snapshot = self._remote_message_snapshot(
                account_id,
                remote_id,
                marker,
                draft,
                require_draft=True,
            )
            if snapshot is None:
                raise PocketError(503, "暂时无法重新核对 Outlook 远端草稿")
            stored_snapshot_hash = intent["remote_snapshot_hash"]
            stored_change_key = intent["remote_change_key"]
            snapshot_matches = bool(
                snapshot["matches"]
                and isinstance(stored_snapshot_hash, str)
                and isinstance(stored_change_key, str)
                and secrets.compare_digest(
                    stored_snapshot_hash, snapshot["snapshot_hash"]
                )
                and secrets.compare_digest(stored_change_key, snapshot["change_key"])
            )
            with self.database.transaction() as connection:
                current = self._intent_row(connection, intent_id)
                self._require_version(current, expected_version)
                if current["status"] != "ready":
                    raise PocketError(409, "发送状态已变化；不会再次发送")
                now = utc_now()
                if not snapshot_matches:
                    connection.execute(
                        """
                        UPDATE outlook_send_intents
                        SET status = 'failed', last_error_code = 'remote_draft_changed',
                            version = version + 1, updated_at = ?
                        WHERE id = ? AND version = ? AND status = 'ready'
                        """,
                        (now, intent_id, expected_version),
                    )
                    connection.execute(
                        """
                        UPDATE outlook_local_drafts
                        SET status = 'canceled', version = version + 1, updated_at = ?
                        WHERE id = ? AND status = 'prepared'
                        """,
                        (now, current["draft_id"]),
                    )
                    failed = self._intent_row(connection, intent_id)
                    self._store_reference(
                        connection,
                        operation=operation,
                        idempotency_key=idempotency_key,
                        payload_hash=payload_hash,
                        kind="intent",
                        resource_id=intent_id,
                        version=failed["version"],
                    )
                    return self._intent_view(connection, failed)
                claimed_intent = connection.execute(
                    """
                    UPDATE outlook_send_intents
                    SET status = 'sending', send_started_at = ?,
                        version = version + 1, updated_at = ?
                    WHERE id = ? AND version = ? AND status = 'ready'
                    """,
                    (now, now, intent_id, expected_version),
                )
                claimed_draft = connection.execute(
                    """
                    UPDATE outlook_local_drafts
                    SET status = 'sending', version = version + 1, updated_at = ?
                    WHERE id = ? AND status = 'prepared'
                    """,
                    (now, current["draft_id"]),
                )
                if claimed_intent.rowcount != 1 or claimed_draft.rowcount != 1:
                    raise PocketError(409, "本地草稿状态已变化；不会发送")
            send_state = "verifying"
            send_error: str | None = None
            try:
                response = self._graph_request(
                    account_id,
                    "POST",
                    f"/me/messages/{quote(remote_id, safe='')}/send",
                    max_bytes=64 * 1024,
                    prefer=IMMUTABLE_ID_PREFERENCE,
                )
                if response.status != 202:
                    remote = self.graph.http_error(response, "send_failed")
                    send_state = "send_uncertain"
                    send_error = remote.code
            except (OutlookRemoteError, PocketError):
                send_state = "send_uncertain"
                send_error = "send_transport_unknown"
            verified_at, sent_at, verify_error = (
                self._verify_sent_item(
                    account_id,
                    remote_id,
                    marker,
                    draft,
                    intent["remote_snapshot_hash"],
                )
                if send_state in {"verifying", "send_uncertain"}
                else (None, None, None)
            )
            if verified_at is not None:
                send_state = "sent"
                send_error = None
            elif verify_error is not None:
                send_error = verify_error
            now = utc_now()
            with self.database.transaction() as connection:
                current = self._intent_row(connection, intent_id)
                if current["status"] != "sending":
                    raise PocketError(409, "发送状态已变化；不会再次发送")
                connection.execute(
                    """
                    UPDATE outlook_send_intents
                    SET status = ?, verified_at = ?, sent_at = ?,
                        last_error_code = ?, version = version + 1, updated_at = ?
                    WHERE id = ? AND status = 'sending'
                    """,
                    (send_state, verified_at, sent_at, send_error, now, intent_id),
                )
                draft_status = "sent" if send_state == "sent" else "uncertain"
                connection.execute(
                    """
                    UPDATE outlook_local_drafts
                    SET status = ?, sent_at = ?, version = version + 1, updated_at = ?
                    WHERE id = ? AND status = 'sending'
                    """,
                    (draft_status, sent_at, now, current["draft_id"]),
                )
                updated = self._intent_row(connection, intent_id)
                self._store_reference(
                    connection,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                    kind="intent",
                    resource_id=intent_id,
                    version=updated["version"],
                )
                return self._intent_view(connection, updated)

    def reconcile_send_intent(
        self,
        intent_id: str,
        expected_version: int,
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"send_intent.reconcile:{intent_id}"
        request = {
            "intent_id": intent_id,
            "expected_version": expected_version,
            "device_id": device_id,
        }
        with self._operation_lock:
            with self.database.connect() as connection:
                cached, payload_hash = self._idempotent_reference(
                    connection,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    payload=request,
                )
                if cached is not None:
                    return cached
                intent = self._intent_row(connection, intent_id)
                self._require_version(intent, expected_version)
                draft = self._draft_row(connection, intent["draft_id"])
                account_id = draft["account_id"]
                status = intent["status"]
                remote_id = intent["remote_graph_id"]
                marker = intent["marker_value"]
            resolved_remote_id: str | None = None
            resolved_snapshot_hash: str | None = None
            resolved_change_key: str | None = None
            resolved_sender_address: str | None = None
            verified_at: str | None = None
            sent_at: str | None = None
            error_code = intent["last_error_code"]
            new_status = status
            if status in {"preparing", "prepare_uncertain"}:
                matches = (
                    [remote_id]
                    if remote_id
                    else self._find_remote_drafts_by_marker(account_id, marker)
                )
                if matches is None:
                    new_status = "prepare_uncertain"
                    error_code = "remote_snapshot_unavailable"
                elif len(matches) == 1:
                    candidate_remote_id = matches[0]
                    snapshot = self._remote_message_snapshot(
                        account_id,
                        candidate_remote_id,
                        marker,
                        draft,
                        require_draft=True,
                    )
                    if snapshot is not None and snapshot["matches"]:
                        resolved_remote_id = candidate_remote_id
                        resolved_snapshot_hash = snapshot["snapshot_hash"]
                        resolved_change_key = snapshot["change_key"]
                        resolved_sender_address = snapshot["authoritative_sender"]
                        new_status = "ready"
                        error_code = None
                    elif (
                        snapshot is not None
                        and snapshot.get("actual_is_draft") is True
                    ):
                        new_status = "failed"
                        error_code = "remote_draft_mismatch"
                    elif snapshot is not None:
                        sent_state, sent_remote_id, sent_snapshot = (
                            self._sent_marker_evidence(account_id, marker, draft)
                        )
                        if sent_state == "verified" and sent_snapshot is not None:
                            resolved_remote_id = sent_remote_id
                            resolved_snapshot_hash = sent_snapshot["snapshot_hash"]
                            resolved_change_key = sent_snapshot["change_key"]
                            resolved_sender_address = sent_snapshot[
                                "authoritative_sender"
                            ]
                            verified_at = utc_now()
                            sent_at = sent_snapshot["sent_at"] or verified_at
                            new_status = "sent"
                            error_code = None
                        else:
                            new_status = "prepare_uncertain"
                            error_code = {
                                "multiple": "multiple_sent_items",
                                "mismatch": "sent_item_content_mismatch",
                                "empty": "remote_message_moved_or_missing",
                            }.get(sent_state, "sent_item_pending")
                    else:
                        new_status = "prepare_uncertain"
                        error_code = "remote_snapshot_unavailable"
                elif len(matches) > 1:
                    new_status = "prepare_uncertain"
                    error_code = "multiple_remote_drafts"
                elif parse_utc(intent["expires_at"]) <= datetime.now(UTC):
                    sent_state, sent_remote_id, sent_snapshot = (
                        self._sent_marker_evidence(account_id, marker, draft)
                    )
                    if sent_state == "verified" and sent_snapshot is not None:
                        resolved_remote_id = sent_remote_id
                        resolved_snapshot_hash = sent_snapshot["snapshot_hash"]
                        resolved_change_key = sent_snapshot["change_key"]
                        resolved_sender_address = sent_snapshot[
                            "authoritative_sender"
                        ]
                        verified_at = utc_now()
                        sent_at = sent_snapshot["sent_at"] or verified_at
                        new_status = "sent"
                        error_code = None
                    elif sent_state == "empty":
                        new_status = "expired"
                        error_code = "remote_draft_not_found"
                    else:
                        new_status = "prepare_uncertain"
                        error_code = {
                            "multiple": "multiple_sent_items",
                            "mismatch": "sent_item_content_mismatch",
                        }.get(sent_state, "sent_item_pending")
                else:
                    new_status = "prepare_uncertain"
                    error_code = "remote_draft_not_found"
            elif status in {"sending", "verifying", "send_uncertain"} and remote_id:
                verified_at, sent_at, verify_error = self._verify_sent_item(
                    account_id,
                    remote_id,
                    marker,
                    draft,
                    intent["remote_snapshot_hash"],
                )
                if verified_at is not None:
                    new_status = "sent"
                    error_code = None
                elif verify_error is not None:
                    error_code = verify_error
            now = utc_now()
            with self.database.transaction() as connection:
                current = self._intent_row(connection, intent_id)
                self._require_version(current, expected_version)
                if (
                    new_status != status
                    or resolved_remote_id is not None
                    or resolved_snapshot_hash is not None
                    or resolved_change_key is not None
                    or resolved_sender_address is not None
                    or verified_at is not None
                    or sent_at is not None
                    or error_code != intent["last_error_code"]
                ):
                    connection.execute(
                        """
                        UPDATE outlook_send_intents
                        SET status = ?, remote_graph_id = COALESCE(?, remote_graph_id),
                            remote_snapshot_hash = COALESCE(?, remote_snapshot_hash),
                            remote_change_key = COALESCE(?, remote_change_key),
                            sender_address = COALESCE(?, sender_address),
                            verified_at = COALESCE(?, verified_at),
                            sent_at = COALESCE(?, sent_at), last_error_code = ?,
                            version = version + 1, updated_at = ?
                        WHERE id = ? AND version = ?
                        """,
                        (
                            new_status,
                            resolved_remote_id,
                            resolved_snapshot_hash,
                            resolved_change_key,
                            resolved_sender_address,
                            verified_at,
                            sent_at,
                            error_code,
                            now,
                            intent_id,
                            expected_version,
                        ),
                    )
                    draft_status = (
                        "prepared"
                        if new_status == "ready"
                        else "sent"
                        if new_status == "sent"
                        else "canceled"
                        if new_status in {"failed", "expired"}
                        else "uncertain"
                    )
                    connection.execute(
                        """
                        UPDATE outlook_local_drafts
                        SET status = ?, sent_at = COALESCE(?, sent_at),
                            version = version + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (draft_status, sent_at, now, current["draft_id"]),
                    )
                updated = self._intent_row(connection, intent_id)
                self._store_reference(
                    connection,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                    kind="intent",
                    resource_id=intent_id,
                    version=updated["version"],
                )
                return self._intent_view(connection, updated)

    def _find_remote_drafts_by_marker(
        self, account_id: str, marker: str
    ) -> list[str] | None:
        filter_value = (
            "singleValueExtendedProperties/Any(ep: ep/id eq "
            f"'{SEND_INTENT_PROPERTY_ID}' and ep/value eq '{marker}')"
        )
        query = urlencode(
            {
                "$filter": filter_value,
                "$select": "id,isDraft",
                "$top": "2",
            },
            quote_via=quote,
        )
        try:
            response = self._graph_request(
                account_id,
                "GET",
                f"/me/mailFolders/drafts/messages?{query}",
                max_bytes=256 * 1024,
                prefer=IMMUTABLE_ID_PREFERENCE,
            )
        except (OutlookRemoteError, PocketError):
            return None
        if response.status != 200:
            return None
        try:
            payload = self.graph.json_object(response)
        except OutlookRemoteError:
            return None
        values = payload.get("value")
        if not isinstance(values, list):
            return None
        if "@odata.nextLink" in payload or "@odata.deltaLink" in payload:
            return ["multiple", "multiple"]
        result: list[str] = []
        for value in values[:2]:
            if not isinstance(value, dict) or value.get("isDraft") is not True:
                return None
            remote_id = self._strict_remote_opaque(
                value.get("id"), maximum=2_048
            )
            if remote_id is None:
                return None
            result.append(remote_id)
        return result

    def _find_remote_sent_by_marker(
        self, account_id: str, marker: str
    ) -> tuple[str, list[str]] | None:
        filter_value = (
            "singleValueExtendedProperties/Any(ep: ep/id eq "
            f"'{SEND_INTENT_PROPERTY_ID}' and ep/value eq '{marker}')"
        )
        query = urlencode(
            {
                "$filter": filter_value,
                "$select": "id,isDraft,parentFolderId,sentDateTime",
                "$top": "2",
            },
            quote_via=quote,
        )
        try:
            folder_response = self._graph_request(
                account_id,
                "GET",
                "/me/mailFolders/sentitems?$select=id",
                max_bytes=128 * 1024,
                prefer=IMMUTABLE_ID_PREFERENCE,
            )
            if folder_response.status != 200:
                return None
            sent_folder_id = self._strict_remote_opaque(
                self.graph.json_object(folder_response).get("id"), maximum=2_048
            )
            if sent_folder_id is None:
                return None
            response = self._graph_request(
                account_id,
                "GET",
                f"/me/mailFolders/sentitems/messages?{query}",
                max_bytes=256 * 1024,
                prefer=IMMUTABLE_ID_PREFERENCE,
            )
            if response.status != 200:
                return None
            payload = self.graph.json_object(response)
        except (OutlookRemoteError, PocketError):
            return None
        values = payload.get("value")
        if not isinstance(values, list):
            return None
        if "@odata.nextLink" in payload or "@odata.deltaLink" in payload:
            return sent_folder_id, ["multiple", "multiple"]
        result: list[str] = []
        for value in values[:2]:
            if not isinstance(value, dict) or value.get("isDraft") is not False:
                return None
            remote_id = self._strict_remote_opaque(
                value.get("id"), maximum=2_048
            )
            if remote_id is None:
                return None
            result.append(remote_id)
        return sent_folder_id, result

    def _sent_marker_evidence(
        self,
        account_id: str,
        marker: str,
        draft: sqlite3.Row,
    ) -> tuple[str, str | None, dict[str, Any] | None]:
        """Return a fail-closed classification for one marker in Sent Items."""

        sent_match = self._find_remote_sent_by_marker(account_id, marker)
        if sent_match is None:
            return "unavailable", None, None
        sent_folder_id, sent_ids = sent_match
        if len(sent_ids) > 1:
            return "multiple", None, None
        if not sent_ids:
            return "empty", None, None
        remote_id = sent_ids[0]
        snapshot = self._remote_message_snapshot(
            account_id,
            remote_id,
            marker,
            draft,
            require_draft=False,
        )
        if snapshot is None:
            return "unavailable", remote_id, None
        parent_folder_id = snapshot["parent_folder_id"]
        if (
            not snapshot["matches"]
            or parent_folder_id is None
            or not secrets.compare_digest(parent_folder_id, sent_folder_id)
        ):
            return "mismatch", remote_id, snapshot
        return "verified", remote_id, snapshot

    def _verify_sent_item(
        self,
        account_id: str,
        remote_id: str,
        marker: str,
        draft: sqlite3.Row,
        expected_snapshot_hash: str | None,
    ) -> tuple[str | None, str | None, str | None]:
        if expected_snapshot_hash is None:
            return None, None, "sent_item_snapshot_missing"
        sent_match = self._find_remote_sent_by_marker(account_id, marker)
        if sent_match is None:
            return None, None, "sent_item_pending"
        sent_folder_id, matched_ids = sent_match
        if len(matched_ids) > 1:
            return None, None, "multiple_sent_items"
        if len(matched_ids) != 1:
            return None, None, "sent_item_pending"
        matched_id = matched_ids[0]
        if not secrets.compare_digest(matched_id, remote_id):
            return None, None, "sent_item_identity_mismatch"
        snapshot = self._remote_message_snapshot(
            account_id,
            matched_id,
            marker,
            draft,
            require_draft=False,
        )
        if snapshot is None:
            return None, None, "sent_item_pending"
        if not snapshot["matches"] or not secrets.compare_digest(
            snapshot["snapshot_hash"], expected_snapshot_hash
        ):
            return None, None, "sent_item_content_mismatch"
        parent_folder_id = snapshot["parent_folder_id"]
        if (
            not sent_folder_id
            or not parent_folder_id
            or not secrets.compare_digest(sent_folder_id, parent_folder_id)
        ):
            return None, None, "sent_item_folder_mismatch"
        verified_at = utc_now()
        return verified_at, snapshot["sent_at"] or verified_at, None

    @staticmethod
    def _draft_row(connection: sqlite3.Connection, draft_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM outlook_local_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        if row is None:
            raise PocketError(404, "回复草稿不存在")
        return row

    @staticmethod
    def _intent_row(connection: sqlite3.Connection, intent_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM outlook_send_intents WHERE id = ?", (intent_id,)
        ).fetchone()
        if row is None:
            raise PocketError(404, "发送意图不存在")
        return row

    @staticmethod
    def _active_intent_row(
        connection: sqlite3.Connection, draft_id: str
    ) -> sqlite3.Row | None:
        placeholders = ",".join("?" for _ in ACTIVE_INTENT_STATUSES)
        return connection.execute(
            f"""
            SELECT * FROM outlook_send_intents
            WHERE draft_id = ? AND status IN ({placeholders})
            ORDER BY created_at DESC LIMIT 1
            """,
            (draft_id, *ACTIVE_INTENT_STATUSES),
        ).fetchone()
