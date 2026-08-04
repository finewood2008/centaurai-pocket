from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from http.cookies import CookieError, SimpleCookie
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import ValidationError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from ..service import PocketError
from .schemas import (
    TaskExecutionCheckInCreate,
    TaskExecutionCommand,
    TaskExecutionExchange,
    TaskExecutionRefresh,
    TaskExecutionStepStatus,
)
from .service import WorkspaceService

BROWSER_PREFIX = "/api/v1/task-execution-invitations"
BOOT_COOKIE = "__Secure-cp_task_exec_boot"
ACCESS_COOKIE = "__Secure-cp_task_exec_at"
REFRESH_COOKIE = "__Secure-cp_task_exec_rt"
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
MAX_FORM_BYTES = 32 * 1024
BOOT_TTL = timedelta(minutes=10)
ACTION_TTL = timedelta(minutes=30)
REFRESH_FORM_TTL = timedelta(hours=24)
ACCESS_COOKIE_MAX_AGE = 10 * 60
REFRESH_COOKIE_MAX_AGE = 24 * 60 * 60
CSRF_DOMAIN = b"centaur-pocket/task-execution-browser-csrf/v1\x00"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}\Z")
BAD_PERCENT_ESCAPE = re.compile(rb"%(?![0-9A-Fa-f]{2})")
CSRF_KEYS = {
    "schema",
    "invitation_id",
    "boot_hash",
    "family_id",
    "task_id",
    "assignment_epoch",
    "credential_generation",
    "action",
    "view_etag",
    "task_version",
    "step_id",
    "step_version",
    "idempotency_key",
    "expires_at",
}
LOGGER = logging.getLogger(__name__)


class BrowserFailure(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _validate_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("task execution browser origin 必须是规范 HTTPS origin")
    default_port = parsed.port in {None, 443}
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    authority = host if default_port else f"{host}:{parsed.port}"
    canonical = f"https://{authority}"
    if value.rstrip("/") != canonical:
        raise ValueError("task execution browser origin 必须使用规范形式")
    return canonical


def _base_path(invitation_id: str) -> str:
    return f"{BROWSER_PREFIX}/{invitation_id}"


def _workbench_path(invitation_id: str) -> str:
    return f"{_base_path(invitation_id)}/workbench"


def _session_path(invitation_id: str) -> str:
    return f"{_base_path(invitation_id)}/session"


def _security_headers(nonce: str | None = None) -> dict[str, str]:
    token = nonce or secrets.token_urlsafe(18)
    return {
        "Cache-Control": "no-store, max-age=0",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Content-Security-Policy": (
            "default-src 'none'; "
            f"style-src 'nonce-{token}'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        ),
    }


def _secure(response: Response, nonce: str | None = None) -> Response:
    for name, value in _security_headers(nonce).items():
        if name not in response.headers:
            response.headers[name] = value
    for name in list(response.headers):
        if name.lower().startswith("access-control-"):
            del response.headers[name]
    return response


def _page(
    title: str,
    content: str,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    nonce = secrets.token_urlsafe(18)
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style nonce="{nonce}">
    :root {{ color-scheme: light; font-family: system-ui, sans-serif; }}
    body {{ margin: 0; background: #f4f5f7; color: #18212f; }}
    main {{ box-sizing: border-box; max-width: 760px; min-height: 100vh;
            margin: 0 auto; padding: 28px 18px 48px; }}
    section {{ background: white; border: 1px solid #dfe3e8; border-radius: 14px;
               margin: 0 0 16px; padding: 20px; }}
    label {{ display: block; margin: 12px 0; }}
    input, textarea, button {{ box-sizing: border-box; font: inherit; max-width: 100%; }}
    input, textarea {{ display: block; width: 100%; padding: 9px; }}
    button {{ padding: 9px 15px; }}
    dt {{ font-weight: 700; margin-top: 10px; }}
    dd {{ margin-left: 0; white-space: pre-wrap; overflow-wrap: anywhere; }}
    .muted {{ color: #667085; }}
  </style>
</head>
<body><main><h1>{html.escape(title)}</h1>{content}</main></body>
</html>"""
    return _secure(HTMLResponse(document, status_code=status_code), nonce)  # type: ignore[return-value]


def _error(status_code: int, detail: str) -> HTMLResponse:
    safe_detail = html.escape(detail)
    return _page(
        "无法继续",
        f"<section><p>{safe_detail}</p></section>",
        status_code=status_code,
    )


def _redirect(location: str) -> Response:
    response = Response(status_code=303, headers={"Location": location})
    return _secure(response)


def _set_secret_cookie(
    response: Response,
    *,
    name: str,
    value: str,
    path: str,
    max_age: int | None = None,
) -> None:
    response.set_cookie(
        key=name,
        value=value,
        path=path,
        max_age=max_age,
        secure=True,
        httponly=True,
        samesite="strict",
    )


def _clear_secret_cookie(response: Response, *, name: str, path: str) -> None:
    response.delete_cookie(
        key=name,
        path=path,
        secure=True,
        httponly=True,
        samesite="strict",
    )


def _require_https(request: Request) -> None:
    if request.scope.get("scheme") != "https":
        raise BrowserFailure(404, "页面不可用")


def _require_invitation_id(invitation_id: str) -> None:
    if IDENTIFIER.fullmatch(invitation_id) is None:
        raise BrowserFailure(404, "页面不可用")


def _single_header(request: Request, name: bytes) -> str | None:
    values: list[str] = []
    for raw_name, raw_value in request.scope.get("headers", []):
        if raw_name.lower() == name:
            try:
                values.append(raw_value.decode("latin-1"))
            except UnicodeDecodeError:
                raise BrowserFailure(400, "请求格式无效") from None
    if len(values) > 1:
        raise BrowserFailure(400, "请求格式无效")
    return values[0] if values else None


def _reject_owner_context(request: Request) -> None:
    if (
        _single_header(request, b"authorization") is not None
        or _single_header(request, b"x-owner-token") is not None
    ):
        raise BrowserFailure(403, "此页面不接受 Owner 或其他 API 凭据")


async def _validated_form(
    request: Request,
    *,
    exact_fields: set[str],
    canonical_origin: str,
) -> dict[str, str]:
    if request.url.query:
        raise BrowserFailure(400, "请求不能包含查询参数")
    content_type = _single_header(request, b"content-type")
    if content_type is None or content_type.casefold() != FORM_CONTENT_TYPE:
        raise BrowserFailure(415, "仅接受标准表单提交")
    content_length = _single_header(request, b"content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            raise BrowserFailure(400, "请求格式无效") from None
        if declared < 0:
            raise BrowserFailure(400, "请求格式无效")
        if declared > MAX_FORM_BYTES:
            raise BrowserFailure(413, "请求体过大")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_FORM_BYTES:
            raise BrowserFailure(413, "请求体过大")
        chunks.append(chunk)
    body = b"".join(chunks)
    if BAD_PERCENT_ESCAPE.search(body):
        raise BrowserFailure(400, "请求格式无效")
    try:
        encoded = body.decode("ascii")
        pairs = parse_qsl(
            encoded,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=len(exact_fields) + 1,
        )
    except (UnicodeDecodeError, ValueError):
        raise BrowserFailure(400, "请求格式无效") from None
    fields: dict[str, str] = {}
    for name, value in pairs:
        if name in fields:
            raise BrowserFailure(400, "请求字段不能重复")
        fields[name] = value
    if set(fields) != exact_fields:
        raise BrowserFailure(400, "请求字段不完整或包含未知字段")

    origin = _single_header(request, b"origin")
    fetch_site = _single_header(request, b"sec-fetch-site")
    fetch_mode = _single_header(request, b"sec-fetch-mode")
    fetch_dest = _single_header(request, b"sec-fetch-dest")
    if (
        origin is None
        or not secrets.compare_digest(origin, canonical_origin)
        or fetch_site != "same-origin"
        or fetch_mode != "navigate"
        or fetch_dest != "document"
    ):
        raise BrowserFailure(403, "跨站表单提交已拒绝")
    return fields


def _cookies(request: Request) -> dict[str, str]:
    result: dict[str, str] = {}
    raw_headers = [
        value
        for name, value in request.scope.get("headers", [])
        if name.lower() == b"cookie"
    ]
    for raw_header in raw_headers:
        try:
            decoded = raw_header.decode("latin-1")
        except UnicodeDecodeError:
            raise BrowserFailure(400, "Cookie 格式无效") from None
        for segment in decoded.split(";"):
            item = segment.strip()
            if not item:
                continue
            if "=" not in item:
                raise BrowserFailure(400, "Cookie 格式无效")
            name, value = item.split("=", 1)
            if not name or name in result:
                raise BrowserFailure(400, "检测到重复 Cookie")
            try:
                parsed = SimpleCookie()
                parsed.load(f"{name}={value}")
            except CookieError:
                raise BrowserFailure(400, "Cookie 格式无效") from None
            if name not in parsed:
                raise BrowserFailure(400, "Cookie 格式无效")
            result[name] = parsed[name].value
    return result


def _secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not value or len(value) > 8192 or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise BrowserFailure(403, "表单授权无效或已过期")
    padding = "=" * ((4 - len(value) % 4) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, TypeError):
        raise BrowserFailure(403, "表单授权无效或已过期") from None


def _csrf_key(service: WorkspaceService) -> bytes:
    key = getattr(service, "_task_session_hmac_key", None)
    if not isinstance(key, bytes) or len(key) != 32:
        raise BrowserFailure(503, "页面暂时不可用")
    return hmac.digest(key, CSRF_DOMAIN, "sha256")


def _csrf_token(service: WorkspaceService, payload: dict[str, Any]) -> str:
    if set(payload) != CSRF_KEYS:
        raise RuntimeError("task execution browser CSRF payload 不完整")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.digest(_csrf_key(service), encoded, "sha256")
    return f"{_b64encode(encoded)}.{_b64encode(signature)}"


def _decode_csrf(service: WorkspaceService, token: str) -> dict[str, Any]:
    encoded_token, separator, signature_token = token.partition(".")
    if separator != "." or "." in signature_token:
        raise BrowserFailure(403, "表单授权无效或已过期")
    encoded = _b64decode(encoded_token)
    signature = _b64decode(signature_token)
    expected = hmac.digest(_csrf_key(service), encoded, "sha256")
    if not hmac.compare_digest(signature, expected):
        raise BrowserFailure(403, "表单授权无效或已过期")
    try:
        payload = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BrowserFailure(403, "表单授权无效或已过期") from None
    if not isinstance(payload, dict) or set(payload) != CSRF_KEYS:
        raise BrowserFailure(403, "表单授权无效或已过期")
    expires_at = payload.get("expires_at")
    if (
        payload.get("schema") != "centaur.task-execution-browser-csrf.v1"
        or not isinstance(expires_at, int)
        or expires_at <= int(datetime.now(UTC).timestamp())
        or expires_at > int((datetime.now(UTC) + REFRESH_FORM_TTL).timestamp()) + 5
    ):
        raise BrowserFailure(403, "表单授权无效或已过期")
    return payload


def _new_csrf_payload(
    *,
    invitation_id: str,
    action: str,
    expires_in: timedelta,
    boot_hash: str | None = None,
    family_id: str | None = None,
    task_id: str | None = None,
    assignment_epoch: int | None = None,
    credential_generation: int = 0,
    view_etag: str | None = None,
    task_version: int | None = None,
    step_id: str | None = None,
    step_version: int | None = None,
) -> dict[str, Any]:
    return {
        "schema": "centaur.task-execution-browser-csrf.v1",
        "invitation_id": invitation_id,
        "boot_hash": boot_hash,
        "family_id": family_id,
        "task_id": task_id,
        "assignment_epoch": assignment_epoch,
        "credential_generation": credential_generation,
        "action": action,
        "view_etag": view_etag,
        "task_version": task_version,
        "step_id": step_id,
        "step_version": step_version,
        "idempotency_key": f"browser-{secrets.token_urlsafe(24)}",
        "expires_at": int((datetime.now(UTC) + expires_in).timestamp()),
    }


def _access_binding(
    service: WorkspaceService,
    token: str,
    invitation_id: str,
) -> dict[str, Any]:
    if not token.startswith("cp_task_ex_"):
        raise BrowserFailure(401, "执行会话不可用")
    with service.database.connect() as connection:
        row = connection.execute(
            """
            SELECT session.*, family.absolute_expires_at,
                   family.revoked_at AS family_revoked_at,
                   family.revoke_reason AS family_revoke_reason
            FROM secretary_task_execution_sessions session
            JOIN secretary_task_execution_refresh_families family
              ON family.id = session.refresh_family_id
            WHERE session.token_hash = ?
            """,
            (_secret_hash(token),),
        ).fetchone()
    if row is None or row["invitation_id"] != invitation_id:
        raise BrowserFailure(401, "执行会话不可用")
    return dict(row)


def _refresh_binding(
    service: WorkspaceService,
    token: str,
    invitation_id: str,
) -> dict[str, Any]:
    if not token.startswith("cp_task_er_"):
        raise BrowserFailure(401, "执行会话不可用")
    with service.database.connect() as connection:
        row = connection.execute(
            """
            SELECT refresh.id AS refresh_id, refresh.generation,
                   refresh.used_at, refresh.idle_expires_at,
                   refresh.revoked_at AS refresh_revoked_at,
                   family.*
            FROM secretary_task_execution_refresh_tokens refresh
            JOIN secretary_task_execution_refresh_families family
              ON family.id = refresh.family_id
            WHERE refresh.token_hash = ?
            """,
            (_secret_hash(token),),
        ).fetchone()
    if row is None or row["invitation_id"] != invitation_id:
        raise BrowserFailure(401, "执行会话不可用")
    return dict(row)


def _family_binding_from_refresh(binding: dict[str, Any]) -> dict[str, Any]:
    return {
        **binding,
        "refresh_family_id": binding["id"],
        "family_revoked_at": binding["revoked_at"],
    }


def _refreshable_projection(
    service: WorkspaceService,
    binding: dict[str, Any],
) -> tuple[dict[str, Any], str, int]:
    now = datetime.now(UTC)
    with service.database.connect() as connection:
        task = connection.execute(
            """
            SELECT * FROM secretary_business_tasks
            WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
            """,
            (binding["task_id"], binding["workspace_id"]),
        ).fetchone()
        member = connection.execute(
            """
            SELECT * FROM secretary_workspace_members
            WHERE id = ? AND workspace_id = ? AND active = 1
            """,
            (binding["assignee_member_id"], binding["workspace_id"]),
        ).fetchone()
        active_refresh = connection.execute(
            """
            SELECT generation FROM secretary_task_execution_refresh_tokens
            WHERE family_id = ? AND used_at IS NULL AND revoked_at IS NULL
              AND idle_expires_at > ?
            ORDER BY generation DESC LIMIT 1
            """,
            (binding["refresh_family_id"], now.isoformat().replace("+00:00", "Z")),
        ).fetchone()
        current = bool(
            task is not None
            and member is not None
            and member["kind"] == "external"
            and binding.get("family_revoked_at") is None
            and datetime.fromisoformat(binding["absolute_expires_at"]) > now
            and task["stage"] in {"aligned", "in_progress", "submitted"}
            and task["assignee_member_id"] == binding["assignee_member_id"]
            and task["assignment_epoch"] == binding["assignment_epoch"]
            and active_refresh is not None
        )
        if current:
            current = service._task_execution_effective_expiry(task, binding) > now
        if not current:
            raise BrowserFailure(401, "执行会话不可用")
        assert task is not None and active_refresh is not None
        projection, raw_etag = service._task_execution_projection(
            connection,
            task,
            member_id=binding["assignee_member_id"],
        )
    return projection, f'"{raw_etag}"', int(active_refresh["generation"])


def _active_context(
    service: WorkspaceService,
    token: str,
    invitation_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    binding = _access_binding(service, token, invitation_id)
    try:
        principal = service.authenticate_task_execution_session(
            token,
            requested_device_id=binding["client_device_id"],
        )
        projection, raw_etag = service.task_execution_view(
            binding["task_id"], principal
        )
    except PocketError as error:
        if error.status_code in {401, 403, 404}:
            raise BrowserFailure(401, "执行会话不可用") from None
        raise BrowserFailure(error.status_code, "暂时无法读取任务") from None
    return binding, principal, projection, f'"{raw_etag}"'


def _verify_bound_csrf(
    service: WorkspaceService,
    token: str,
    *,
    invitation_id: str,
    binding: dict[str, Any],
    action: str,
    credential_generation: int,
    step_id: str | None = None,
) -> dict[str, Any]:
    payload = _decode_csrf(service, token)
    expected = {
        "invitation_id": invitation_id,
        "boot_hash": None,
        "family_id": binding["refresh_family_id"],
        "task_id": binding["task_id"],
        "assignment_epoch": binding["assignment_epoch"],
        "credential_generation": credential_generation,
        "action": action,
        "step_id": step_id,
    }
    for name, value in expected.items():
        if payload.get(name) != value:
            raise BrowserFailure(403, "表单授权与当前会话不匹配")
    if (
        not isinstance(payload.get("view_etag"), str)
        or re.fullmatch(r'"task-execution-v1-[0-9a-f]{64}"', payload["view_etag"])
        is None
        or not isinstance(payload.get("task_version"), int)
        or payload["task_version"] < 1
        or (step_id is None and payload.get("step_version") is not None)
        or (
            step_id is not None
            and (
                not isinstance(payload.get("step_version"), int)
                or payload["step_version"] < 1
            )
        )
    ):
        raise BrowserFailure(403, "表单授权无效或已过期")
    key = payload.get("idempotency_key")
    if not isinstance(key, str) or not (8 <= len(key) <= 200):
        raise BrowserFailure(403, "表单授权无效或已过期")
    return payload


def _hidden_csrf(value: str) -> str:
    return (
        '<input type="hidden" name="csrf" value="'
        + html.escape(value, quote=True)
        + '">'
    )


def _workbench_csrf(
    service: WorkspaceService,
    invitation_id: str,
    binding: dict[str, Any],
    projection: dict[str, Any],
    etag: str,
    *,
    action: str,
    step: dict[str, Any] | None = None,
) -> str:
    return _csrf_token(
        service,
        _new_csrf_payload(
            invitation_id=invitation_id,
            family_id=binding["refresh_family_id"],
            task_id=binding["task_id"],
            assignment_epoch=binding["assignment_epoch"],
            credential_generation=int(binding["access_generation"]),
            action=action,
            view_etag=etag,
            task_version=int(projection["version"]),
            step_id=step["id"] if step is not None else None,
            step_version=int(step["version"]) if step is not None else None,
            expires_in=ACTION_TTL,
        ),
    )


def _refresh_csrf(
    service: WorkspaceService,
    invitation_id: str,
    binding: dict[str, Any],
    projection: dict[str, Any],
    etag: str,
    generation: int,
) -> str:
    return _csrf_token(
        service,
        _new_csrf_payload(
            invitation_id=invitation_id,
            family_id=binding["refresh_family_id"],
            task_id=binding["task_id"],
            assignment_epoch=binding["assignment_epoch"],
            credential_generation=generation,
            action="refresh",
            view_etag=etag,
            task_version=int(projection["version"]),
            expires_in=REFRESH_FORM_TTL,
        ),
    )


def _render_workbench(
    service: WorkspaceService,
    invitation_id: str,
    binding: dict[str, Any],
    projection: dict[str, Any],
    etag: str,
    refresh_generation: int,
    *,
    refresh_only: bool = False,
) -> HTMLResponse:
    def text(value: Any) -> str:
        return html.escape("" if value is None else str(value))

    facts = "".join(
        f"<dt>{html.escape(label)}</dt><dd>{text(projection.get(name))}</dd>"
        for label, name in (
            ("目的", "purpose"),
            ("目标", "objective"),
            ("策略", "strategy"),
            ("阶段", "stage"),
            ("进度", "progress"),
            ("开始时间", "start_at"),
            ("期限", "due_at"),
        )
    )
    lists = "".join(
        f"<h3>{html.escape(label)}</h3><ul>"
        + "".join(f"<li>{text(item)}</li>" for item in projection.get(name, []))
        + "</ul>"
        for label, name in (
            ("关键点", "key_points"),
            ("验收标准", "acceptance_criteria"),
        )
    )
    refresh_token = _refresh_csrf(
        service,
        invitation_id,
        binding,
        projection,
        etag,
        refresh_generation,
    )
    refresh_form = (
        f'<form method="post" action="{_session_path(invitation_id)}/refresh">'
        f"{_hidden_csrf(refresh_token)}"
        '<button type="submit">刷新安全会话</button></form>'
    )
    if refresh_only:
        return _page(
            "执行会话需要刷新",
            "<section><p>短期访问会话已经过期，请使用安全刷新会话继续。</p>"
            + refresh_form
            + "</section>",
        )

    forms: list[str] = [refresh_form]
    if projection["stage"] == "aligned" and not projection["change_pending"]:
        csrf = _workbench_csrf(
            service,
            invitation_id,
            binding,
            projection,
            etag,
            action="start",
        )
        forms.append(
            f'<form method="post" action="{_workbench_path(invitation_id)}/start">'
            f"{_hidden_csrf(csrf)}"
            '<label>启动说明<textarea name="note" maxlength="4000"></textarea></label>'
            '<button type="submit">启动任务</button></form>'
        )
    if projection["stage"] == "in_progress":
        checkin_csrf = _workbench_csrf(
            service,
            invitation_id,
            binding,
            projection,
            etag,
            action="check-in",
        )
        forms.append(
            f'<form method="post" action="{_workbench_path(invitation_id)}/check-ins">'
            f"{_hidden_csrf(checkin_csrf)}"
            '<label>执行摘要<textarea name="summary" required maxlength="4000"></textarea></label>'
            '<label>进度（0-100）<input name="reported_progress" type="number" min="0" max="100" required></label>'
            '<label>风险（每行一项）<textarea name="risks"></textarea></label>'
            '<label>阻塞（每行一项）<textarea name="blockers"></textarea></label>'
            '<label>下一步（每行一项）<textarea name="next_actions"></textarea></label>'
            "<label>预测完成时间（RFC3339，必须含时区）"
            '<input name="forecast_at" type="text" maxlength="64" '
            'placeholder="2030-08-20T18:00:00+08:00"></label>'
            '<button type="submit">记录执行回报</button></form>'
        )
        if not projection["change_pending"]:
            submit_csrf = _workbench_csrf(
                service,
                invitation_id,
                binding,
                projection,
                etag,
                action="submit",
            )
            forms.append(
                f'<form method="post" action="{_workbench_path(invitation_id)}/submit">'
                f"{_hidden_csrf(submit_csrf)}"
                '<label>提交说明<textarea name="note" maxlength="4000"></textarea></label>'
                '<button type="submit">提交验收</button></form>'
            )

    step_sections: list[str] = []
    for step in projection["steps"]:
        controls: list[str] = []
        if step["editable"] and not projection["change_pending"]:
            for target, label in (
                ("pending", "设为待开始"),
                ("in_progress", "设为进行中"),
                ("blocked", "设为阻塞"),
                ("done", "标记完成"),
            ):
                if target == step["status"]:
                    continue
                csrf = _workbench_csrf(
                    service,
                    invitation_id,
                    binding,
                    projection,
                    etag,
                    action=f"step:{target}",
                    step=step,
                )
                controls.append(
                    f'<form method="post" action="{_workbench_path(invitation_id)}'
                    f'/steps/{html.escape(step["id"], quote=True)}/status">'
                    f'{_hidden_csrf(csrf)}<button type="submit">{label}</button></form>'
                )
        step_sections.append(
            "<section><h3>"
            + text(step["title"])
            + "</h3><p>状态："
            + text(step["status"])
            + "</p>"
            + "".join(controls)
            + "</section>"
        )

    own_checkins = "".join(
        "<li>" + text(item["summary"]) + "</li>" for item in projection["own_checkins"]
    )
    change_notice = (
        "<section><p>任务存在待确认变更；仍可记录执行回报，其他执行写入暂时停用。</p></section>"
        if projection["change_pending"]
        else ""
    )
    content = (
        f"<section><h2>{text(projection['title'])}</h2><dl>{facts}</dl>{lists}</section>"
        + change_notice
        + "".join(step_sections)
        + f"<section><h2>我的执行回报</h2><ul>{own_checkins}</ul></section>"
        + '<section><h2>操作</h2><p class="muted">所有写入均会校验当前任务视图。</p>'
        + "".join(forms)
        + "</section>"
    )
    return _page("任务执行工作台", content)


def _continue_page(invitation_id: str) -> HTMLResponse:
    path = f"{_session_path(invitation_id)}/continue"
    return _page(
        "执行会话需要继续",
        "<section><p>短期访问会话不可用。若安全刷新会话仍有效，可以继续。</p>"
        f'<p><a href="{path}">继续安全会话</a></p></section>',
        status_code=401,
    )


def _form_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _service(request: Request) -> WorkspaceService:
    service = getattr(request.app.state, "workspace_service", None)
    if not isinstance(service, WorkspaceService):
        raise BrowserFailure(503, "页面暂时不可用")
    return service


def _handle_failure(error: Exception) -> HTMLResponse:
    if isinstance(error, BrowserFailure):
        return _error(error.status_code, error.detail)
    if isinstance(error, PocketError):
        if error.status_code in {401, 403, 404}:
            return _error(401, "执行会话不可用")
        if error.status_code == 412:
            return _error(412, "任务视图已经变化，请刷新页面")
        return _error(error.status_code, "操作未完成，请刷新页面后重试")
    if isinstance(error, ValidationError):
        return _error(422, "表单内容格式无效")
    raise error


def create_task_execution_browser_router(canonical_origin: str) -> APIRouter:
    origin = _validate_origin(canonical_origin)
    router = APIRouter(tags=["task-execution-browser"])

    @router.get(f"{BROWSER_PREFIX}/{{invitation_id}}")
    def invitation_entry(invitation_id: str, request: Request) -> HTMLResponse:
        try:
            _require_https(request)
            _require_invitation_id(invitation_id)
            if request.url.query:
                raise BrowserFailure(400, "请求不能包含查询参数")
            _reject_owner_context(request)
            service = _service(request)
            service.task_execution_invitation_shell(invitation_id)
            boot = secrets.token_urlsafe(32)
            payload = _new_csrf_payload(
                invitation_id=invitation_id,
                action="exchange",
                expires_in=BOOT_TTL,
                boot_hash=_secret_hash(boot),
            )
            csrf = _csrf_token(service, payload)
            response = _page(
                "打开任务执行工作台",
                f'<section><form method="post" action="{_base_path(invitation_id)}/exchange">'
                f"{_hidden_csrf(csrf)}"
                '<label>一次性确认码<input name="code" autocomplete="one-time-code" '
                'maxlength="128" required></label>'
                '<button type="submit">确认并打开</button></form></section>',
            )
            _set_secret_cookie(
                response,
                name=BOOT_COOKIE,
                value=boot,
                path=_base_path(invitation_id),
                max_age=int(BOOT_TTL.total_seconds()),
            )
            return response
        except (BrowserFailure, PocketError, ValidationError) as error:
            return _handle_failure(error)

    @router.post(f"{BROWSER_PREFIX}/{{invitation_id}}/exchange")
    async def exchange(invitation_id: str, request: Request) -> Response:
        try:
            _require_https(request)
            _require_invitation_id(invitation_id)
            fields = await _validated_form(
                request,
                exact_fields={"csrf", "code"},
                canonical_origin=origin,
            )
            _reject_owner_context(request)
            service = _service(request)
            boot = _cookies(request).get(BOOT_COOKIE)
            if boot is None:
                raise BrowserFailure(401, "邀请确认会话不可用")
            csrf = _decode_csrf(service, fields["csrf"])
            expected = {
                "invitation_id": invitation_id,
                "boot_hash": _secret_hash(boot),
                "family_id": None,
                "task_id": None,
                "assignment_epoch": None,
                "credential_generation": 0,
                "action": "exchange",
                "view_etag": None,
                "task_version": None,
                "step_id": None,
                "step_version": None,
            }
            if any(csrf.get(name) != value for name, value in expected.items()):
                raise BrowserFailure(403, "邀请确认表单无效或已过期")
            device_id = f"browser-{_secret_hash(boot)[:32]}"
            payload = TaskExecutionExchange.model_validate(
                {
                    "invitation_id": invitation_id,
                    "code": fields["code"],
                    "client_device_id": device_id,
                }
            ).model_dump(mode="json")
            result = service.exchange_task_execution(
                payload,
                idempotency_key=csrf["idempotency_key"],
            )
            response = _redirect(_workbench_path(invitation_id))
            _set_secret_cookie(
                response,
                name=ACCESS_COOKIE,
                value=result["access_token"],
                path=_workbench_path(invitation_id),
                max_age=ACCESS_COOKIE_MAX_AGE,
            )
            _set_secret_cookie(
                response,
                name=REFRESH_COOKIE,
                value=result["refresh_token"],
                path=_session_path(invitation_id),
                max_age=REFRESH_COOKIE_MAX_AGE,
            )
            _clear_secret_cookie(
                response,
                name=BOOT_COOKIE,
                path=_base_path(invitation_id),
            )
            return response
        except (BrowserFailure, PocketError, ValidationError) as error:
            return _handle_failure(error)

    @router.get(f"{BROWSER_PREFIX}/{{invitation_id}}/workbench")
    def workbench(invitation_id: str, request: Request) -> HTMLResponse:
        try:
            _require_https(request)
            _require_invitation_id(invitation_id)
            if request.url.query:
                raise BrowserFailure(400, "请求不能包含查询参数")
            _reject_owner_context(request)
            service = _service(request)
            access = _cookies(request).get(ACCESS_COOKIE)
            if access is None:
                return _continue_page(invitation_id)
            try:
                active_binding, _principal, projection, etag = _active_context(
                    service, access, invitation_id
                )
                _, _, refresh_generation = _refreshable_projection(
                    service, active_binding
                )
                return _render_workbench(
                    service,
                    invitation_id,
                    active_binding,
                    projection,
                    etag,
                    refresh_generation,
                )
            except BrowserFailure as error:
                if error.status_code != 401:
                    raise
                return _continue_page(invitation_id)
        except (BrowserFailure, PocketError, ValidationError) as error:
            return _handle_failure(error)

    @router.get(f"{BROWSER_PREFIX}/{{invitation_id}}/session/continue")
    def continue_session(invitation_id: str, request: Request) -> HTMLResponse:
        try:
            _require_https(request)
            _require_invitation_id(invitation_id)
            if request.url.query:
                raise BrowserFailure(400, "请求不能包含查询参数")
            _reject_owner_context(request)
            service = _service(request)
            refresh_token = _cookies(request).get(REFRESH_COOKIE)
            if refresh_token is None:
                raise BrowserFailure(401, "执行会话不可用")
            refresh_binding = _refresh_binding(service, refresh_token, invitation_id)
            # A used refresh token can only be retried safely with the original
            # form's idempotency key during the service's exact-replay window.
            # Minting a new continue form here would turn a lost response or
            # stale cookie into unsafe reuse and revoke the whole family.
            if refresh_binding["used_at"] is not None:
                raise BrowserFailure(401, "执行会话不可用")
            family_binding = _family_binding_from_refresh(refresh_binding)
            projection, etag, _active_generation = _refreshable_projection(
                service, family_binding
            )
            return _render_workbench(
                service,
                invitation_id,
                family_binding,
                projection,
                etag,
                int(refresh_binding["generation"]),
                refresh_only=True,
            )
        except (BrowserFailure, PocketError, ValidationError) as error:
            return _handle_failure(error)

    async def active_form(
        invitation_id: str,
        request: Request,
        fields: set[str],
    ) -> tuple[
        WorkspaceService,
        dict[str, str],
        str,
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
        str,
    ]:
        _require_https(request)
        _require_invitation_id(invitation_id)
        form = await _validated_form(
            request,
            exact_fields=fields,
            canonical_origin=origin,
        )
        _reject_owner_context(request)
        service = _service(request)
        access = _cookies(request).get(ACCESS_COOKIE)
        if access is None:
            raise BrowserFailure(401, "执行会话不可用")
        binding, principal, projection, etag = _active_context(
            service, access, invitation_id
        )
        return service, form, access, binding, principal, projection, etag

    @router.post(f"{BROWSER_PREFIX}/{{invitation_id}}/workbench/start")
    async def start(invitation_id: str, request: Request) -> Response:
        try:
            (
                service,
                form,
                _access,
                binding,
                principal,
                _projection,
                _etag,
            ) = await active_form(invitation_id, request, {"csrf", "note"})
            csrf = _verify_bound_csrf(
                service,
                form["csrf"],
                invitation_id=invitation_id,
                binding=binding,
                action="start",
                credential_generation=binding["access_generation"],
            )
            payload = TaskExecutionCommand.model_validate(
                {
                    "expected_task_version": csrf["task_version"],
                    "client_mutation_id": csrf["idempotency_key"],
                    "note": form["note"] or None,
                }
            ).model_dump(mode="json")
            service.start_task_execution(
                binding["task_id"],
                payload,
                principal,
                idempotency_key=csrf["idempotency_key"],
                device_id=binding["client_device_id"],
                if_match=csrf["view_etag"],
            )
            return _redirect(_workbench_path(invitation_id))
        except (BrowserFailure, PocketError, ValidationError) as error:
            return _handle_failure(error)

    @router.post(f"{BROWSER_PREFIX}/{{invitation_id}}/workbench/check-ins")
    async def check_in(invitation_id: str, request: Request) -> Response:
        try:
            expected = {
                "csrf",
                "summary",
                "reported_progress",
                "risks",
                "blockers",
                "next_actions",
                "forecast_at",
            }
            (
                service,
                form,
                _access,
                binding,
                principal,
                _projection,
                _etag,
            ) = await active_form(invitation_id, request, expected)
            csrf = _verify_bound_csrf(
                service,
                form["csrf"],
                invitation_id=invitation_id,
                binding=binding,
                action="check-in",
                credential_generation=binding["access_generation"],
            )
            payload = TaskExecutionCheckInCreate.model_validate(
                {
                    "expected_task_version": csrf["task_version"],
                    "summary": form["summary"],
                    "reported_progress": form["reported_progress"],
                    "risks": _form_lines(form["risks"]),
                    "blockers": _form_lines(form["blockers"]),
                    "next_actions": _form_lines(form["next_actions"]),
                    "forecast_at": form["forecast_at"] or None,
                    "client_mutation_id": csrf["idempotency_key"],
                }
            ).model_dump(mode="json")
            service.create_task_execution_checkin(
                binding["task_id"],
                payload,
                principal,
                idempotency_key=csrf["idempotency_key"],
                device_id=binding["client_device_id"],
                if_match=csrf["view_etag"],
            )
            return _redirect(_workbench_path(invitation_id))
        except (BrowserFailure, PocketError, ValidationError) as error:
            return _handle_failure(error)

    @router.post(
        f"{BROWSER_PREFIX}/{{invitation_id}}/workbench/steps/{{step_id}}/status"
    )
    async def step_status(
        invitation_id: str,
        step_id: str,
        request: Request,
    ) -> Response:
        try:
            _require_invitation_id(step_id)
            (
                service,
                form,
                _access,
                binding,
                principal,
                projection,
                _etag,
            ) = await active_form(invitation_id, request, {"csrf"})
            unsigned = _decode_csrf(service, form["csrf"])
            action = unsigned.get("action")
            if action not in {
                "step:pending",
                "step:in_progress",
                "step:blocked",
                "step:done",
            }:
                raise BrowserFailure(403, "表单授权与操作不匹配")
            step = next(
                (item for item in projection["steps"] if item["id"] == step_id),
                None,
            )
            if step is None:
                raise BrowserFailure(404, "任务步骤不可用")
            csrf = _verify_bound_csrf(
                service,
                form["csrf"],
                invitation_id=invitation_id,
                binding=binding,
                action=action,
                credential_generation=binding["access_generation"],
                step_id=step_id,
            )
            payload = TaskExecutionStepStatus.model_validate(
                {
                    "expected_task_version": csrf["task_version"],
                    "expected_step_version": csrf["step_version"],
                    "status": action.removeprefix("step:"),
                    "note": None,
                    "client_mutation_id": csrf["idempotency_key"],
                }
            ).model_dump(mode="json")
            service.set_task_execution_step_status(
                binding["task_id"],
                step_id,
                payload,
                principal,
                idempotency_key=csrf["idempotency_key"],
                device_id=binding["client_device_id"],
                if_match=csrf["view_etag"],
            )
            return _redirect(_workbench_path(invitation_id))
        except (BrowserFailure, PocketError, ValidationError) as error:
            return _handle_failure(error)

    @router.post(f"{BROWSER_PREFIX}/{{invitation_id}}/workbench/submit")
    async def submit(invitation_id: str, request: Request) -> Response:
        try:
            (
                service,
                form,
                _access,
                binding,
                principal,
                _projection,
                _etag,
            ) = await active_form(invitation_id, request, {"csrf", "note"})
            csrf = _verify_bound_csrf(
                service,
                form["csrf"],
                invitation_id=invitation_id,
                binding=binding,
                action="submit",
                credential_generation=binding["access_generation"],
            )
            payload = TaskExecutionCommand.model_validate(
                {
                    "expected_task_version": csrf["task_version"],
                    "client_mutation_id": csrf["idempotency_key"],
                    "note": form["note"] or None,
                }
            ).model_dump(mode="json")
            service.submit_task_execution(
                binding["task_id"],
                payload,
                principal,
                idempotency_key=csrf["idempotency_key"],
                device_id=binding["client_device_id"],
                if_match=csrf["view_etag"],
            )
            return _redirect(_workbench_path(invitation_id))
        except (BrowserFailure, PocketError, ValidationError) as error:
            return _handle_failure(error)

    @router.post(f"{BROWSER_PREFIX}/{{invitation_id}}/session/refresh")
    async def refresh(invitation_id: str, request: Request) -> Response:
        try:
            _require_https(request)
            _require_invitation_id(invitation_id)
            form = await _validated_form(
                request,
                exact_fields={"csrf"},
                canonical_origin=origin,
            )
            _reject_owner_context(request)
            service = _service(request)
            refresh_token = _cookies(request).get(REFRESH_COOKIE)
            if refresh_token is None:
                raise BrowserFailure(401, "执行会话不可用")
            refresh_binding = _refresh_binding(service, refresh_token, invitation_id)
            access_binding = _family_binding_from_refresh(refresh_binding)
            projection, etag, _active_generation = _refreshable_projection(
                service, access_binding
            )
            csrf = _verify_bound_csrf(
                service,
                form["csrf"],
                invitation_id=invitation_id,
                binding=access_binding,
                action="refresh",
                credential_generation=refresh_binding["generation"],
            )
            if (
                csrf["view_etag"] != etag
                or csrf["task_version"] != projection["version"]
            ):
                raise BrowserFailure(412, "任务视图已经变化，请刷新页面")
            payload = TaskExecutionRefresh.model_validate(
                {
                    "refresh_token": refresh_token,
                    "client_device_id": refresh_binding["client_device_id"],
                }
            ).model_dump(mode="json")
            result, _raw_etag = service.refresh_task_execution(
                payload,
                idempotency_key=csrf["idempotency_key"],
            )
            response = _redirect(_workbench_path(invitation_id))
            _set_secret_cookie(
                response,
                name=ACCESS_COOKIE,
                value=result["access_token"],
                path=_workbench_path(invitation_id),
                max_age=ACCESS_COOKIE_MAX_AGE,
            )
            _set_secret_cookie(
                response,
                name=REFRESH_COOKIE,
                value=result["refresh_token"],
                path=_session_path(invitation_id),
                max_age=REFRESH_COOKIE_MAX_AGE,
            )
            return response
        except (BrowserFailure, PocketError, ValidationError) as error:
            return _handle_failure(error)

    return router


class TaskExecutionBrowserBoundaryMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        if request.url.path != BROWSER_PREFIX and not request.url.path.startswith(
            f"{BROWSER_PREFIX}/"
        ):
            return await call_next(request)
        if request.method == "OPTIONS":
            return _error(405, "不支持此请求方法")
        try:
            response = await call_next(request)
        # This namespace must fail with browser-safe headers even when an
        # unexpected downstream exception escapes a route handler.
        except Exception:
            LOGGER.exception("Unhandled task execution browser failure")
            response = _error(500, "页面暂时不可用")
        return _secure(response)


def install_task_execution_browser(
    application: FastAPI,
    *,
    canonical_origin: str,
) -> None:
    origin = _validate_origin(canonical_origin)
    application.include_router(create_task_execution_browser_router(origin))
    application.add_middleware(TaskExecutionBrowserBoundaryMiddleware)


__all__ = [
    "ACCESS_COOKIE",
    "BOOT_COOKIE",
    "BROWSER_PREFIX",
    "REFRESH_COOKIE",
    "TaskExecutionBrowserBoundaryMiddleware",
    "create_task_execution_browser_router",
    "install_task_execution_browser",
]
