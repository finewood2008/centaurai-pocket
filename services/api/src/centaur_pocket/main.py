from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any, Literal

import uvicorn
from fastapi import (
    Body,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .assistant import (
    AssistantLoop,
    CloudTicketStore,
    ProposalStore,
    ProviderError,
    build_assistant_tools,
    build_cloud_provider,
    build_local_provider,
)
from .config import Settings
from .database import Database
from .mail_router import create_mail_router
from .mcp import KNOWLEDGE_RETRIEVE_TOOL, PROTOCOL_VERSION, MCPServer, MCPTool
from .outlook_mail import OutlookMailService
from .reliable_sources_router import create_reliable_sources_router
from .schemas import (
    AgentSearchRequest,
    CaptureCreate,
    CollectorEventBatch,
    CollectorHandshake,
    CollectorHeartbeat,
    ConversationPolicyUpdate,
    GovernanceAction,
    GovernanceApply,
    GovernanceSkip,
    GovernanceUndo,
    ItemPatch,
    ItemState,
    MobileDeviceList,
    MobilePairingClaim,
    MobilePairingCreate,
    MobilePairingCreated,
    MobileSessionRefresh,
    MobileSessionTokens,
    MobileSessionView,
    RetentionApply,
    SourceCreate,
    SourceUpdate,
    TaskStatus,
)
from .service import PocketError, PocketService
from .workspace.router import (
    create_task_agreement_router,
    create_task_alignment_router,
    create_task_change_invitation_router,
    create_task_change_router,
    create_task_execution_router,
    create_workspace_router,
)
from .workspace.service import DEFAULT_OWNER_ID, WorkspaceService
from .workspace.task_execution_browser import install_task_execution_browser

API_PREFIX = "/api/v1"
LOGGER = logging.getLogger(__name__)
SENSITIVE_JSON_BODY_LIMIT = 8 * 1024 * 1024
TASK_SESSION_HMAC_KEY_DOMAIN = b"centaur-pocket/task-session-hmac-key/v1\x00"
SCOPED_TASK_TOKEN_PREFIXES = (
    "cp_task_at_",
    "cp_task_ch_",
    "cp_task_ex_",
    "cp_task_er_",
)


def _derive_task_session_hmac_key(owner_token: str) -> bytes:
    """Derive a stable, purpose-bound key without reusing the Owner token."""
    return hashlib.sha256(
        TASK_SESSION_HMAC_KEY_DOMAIN + owner_token.encode("utf-8")
    ).digest()


class SensitiveJsonBodyLimitMiddleware:
    """Bound secret-bearing JSON bodies before validation reads them."""

    def __init__(self, app: Any, limit: int = SENSITIVE_JSON_BODY_LIMIT):
        self.app = app
        self.limit = limit

    @staticmethod
    def _is_protected(scope: dict[str, Any]) -> bool:
        if scope.get("type") != "http" or scope.get("method") not in {
            "POST",
            "PUT",
        }:
            return False
        path = str(scope.get("path", ""))
        if path in {
            "/api/v1/task-alignments/preview",
            "/api/v1/task-alignments/confirm",
            "/api/v1/task-alignments/exchange",
            "/api/v1/task-changes/exchange",
            "/api/v1/task-executions/exchange",
            "/api/v1/task-executions/refresh",
        }:
            return True
        return (
            path.startswith("/api/v1/task-agreements/")
            and path.endswith("/responses")
        ) or (
            path.startswith("/api/v1/task-changes/")
            and path.endswith("/decisions")
        ) or (
            path.startswith("/api/v1/task-executions/")
            and path.endswith(("/start", "/check-ins", "/status", "/submit"))
        )

    @staticmethod
    async def _send_too_large(send: Any) -> None:
        body = json.dumps(
            {"detail": "请求体过大"}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status.HTTP_413_CONTENT_TOO_LARGE,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store, max-age=0"),
                    (b"pragma", b"no-cache"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-content-type-options", b"nosniff"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if not self._is_protected(scope):
            await self.app(scope, receive, send)
            return
        content_length: int | None = None
        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    content_length = int(value.decode("ascii"))
                except (UnicodeDecodeError, ValueError):
                    content_length = None
                break
        if content_length is not None and content_length > self.limit:
            await self._send_too_large(send)
            return

        messages: list[dict[str, Any]] = []
        total = 0
        while True:
            message = await receive()
            messages.append(message)
            if message.get("type") != "http.request":
                break
            total += len(message.get("body", b""))
            if total > self.limit:
                await self._send_too_large(send)
                return
            if not message.get("more_body", False):
                break

        index = 0

        async def replay_receive() -> dict[str, Any]:
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)


def _collect_due_reliable_source_safely(
    service: PocketService,
    reliable_source_id: str,
) -> None:
    try:
        service.reliable_sources.collect_due(reliable_source_id)
    except Exception:
        LOGGER.exception(
            "Scheduled reliable-source collection failed",
            extra={"reliable_source_id": reliable_source_id},
        )


def _sync_due_outlook_account_safely(
    service: OutlookMailService,
    account_id: str,
) -> None:
    try:
        service.sync_due_account(account_id)
    except Exception:
        LOGGER.exception(
            "Scheduled Outlook metadata sync failed",
            extra={"outlook_account_id": account_id},
        )


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 Bearer 凭据",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.casefold() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer 凭据格式不正确",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token.strip()


def get_service(request: Request) -> PocketService:
    return request.app.state.service


def _item_patch_values(payload: ItemPatch) -> dict[str, Any]:
    """Preserve an explicit null category while ignoring other null fields."""

    values = payload.model_dump(exclude_unset=True, exclude_none=True)
    if "category" in payload.model_fields_set:
        values["category"] = payload.category
    return values


def require_owner(
    authorization: Annotated[str | None, Header()] = None,
    x_owner_token: Annotated[str | None, Header(alias="X-Owner-Token")] = None,
    service: PocketService = Depends(get_service),
) -> PocketService:
    candidates: list[str] = []
    if authorization:
        bearer_token = _bearer_token(authorization)
        if bearer_token.startswith(SCOPED_TASK_TOKEN_PREFIXES):
            raise HTTPException(
                status_code=(
                    status.HTTP_403_FORBIDDEN
                    if x_owner_token
                    else status.HTTP_401_UNAUTHORIZED
                ),
                detail="任务范围凭据不能与 Owner 认证混用",
            )
        candidates.append(bearer_token)
    if x_owner_token and x_owner_token.strip():
        candidates.append(x_owner_token.strip())
    if any(service.owner_token_matches(candidate) for candidate in candidates):
        return service
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Owner 凭据无效" if candidates else "缺少 Owner 凭据",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_agent(
    authorization: Annotated[str | None, Header()] = None,
    service: PocketService = Depends(get_service),
) -> PocketService:
    if not service.agent_token_matches(_bearer_token(authorization)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Agent 凭据无效",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return service


def require_mobile_device(
    authorization: Annotated[str | None, Header()] = None,
    service: PocketService = Depends(get_service),
) -> dict[str, Any]:
    bearer_token = _bearer_token(authorization)
    if bearer_token.startswith(SCOPED_TASK_TOKEN_PREFIXES):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="任务范围凭据不能访问手机设备接口",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return service.authenticate_mobile_access(bearer_token)


def require_secretary_access(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_owner_token: Annotated[str | None, Header(alias="X-Owner-Token")] = None,
    service: PocketService = Depends(get_service),
) -> PocketService:
    bearer_token: str | None = None
    if authorization:
        try:
            bearer_token = _bearer_token(authorization)
        except HTTPException:
            bearer_token = None
    if bearer_token is not None and bearer_token.startswith(
        SCOPED_TASK_TOKEN_PREFIXES
    ):
        raise HTTPException(
            status_code=(
                status.HTTP_403_FORBIDDEN
                if x_owner_token
                else status.HTTP_401_UNAUTHORIZED
            ),
            detail="任务范围凭据不能访问 Owner 工作区接口",
            headers={"WWW-Authenticate": "Bearer"},
        )
    owner_candidates = [
        candidate
        for candidate in (
            bearer_token,
            x_owner_token.strip() if x_owner_token else None,
        )
        if candidate
    ]
    if any(service.owner_token_matches(candidate) for candidate in owner_candidates):
        request.state.secretary_access_kind = "owner"
        return service
    if bearer_token:
        mobile_session = service.authenticate_mobile_access(bearer_token)
        request.state.secretary_access_kind = "device"
        request.state.mobile_session = mobile_session
        requested_device_id = request.headers.get("X-Device-ID")
        if requested_device_id is not None and not secrets.compare_digest(
            requested_device_id.encode("utf-8"),
            mobile_session["device"]["device_id"].encode("utf-8"),
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="手机设备标识与访问凭据不匹配",
            )
        workspace_id = request.path_params.get("workspace_id")
        if workspace_id is not None and workspace_id != "ws_default":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="手机设备只能访问默认秘书工作区",
            )
        return service
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="缺少秘书访问凭据",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_task_agreement_access(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_owner_token: Annotated[str | None, Header(alias="X-Owner-Token")] = None,
    x_device_id: Annotated[str | None, Header(alias="X-Device-ID")] = None,
    service: PocketService = Depends(get_service),
) -> dict[str, Any]:
    bearer_token = _bearer_token(authorization) if authorization else None
    workspace_service: WorkspaceService = request.app.state.workspace_service

    # Prefix dispatch is intentional: a malformed scoped token must never fall
    # through to Owner or mobile-device authentication.
    if bearer_token is not None and bearer_token.startswith("cp_task_at_"):
        if x_owner_token:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="任务会话不能与 Owner 凭据混用",
            )
        if x_device_id is None:
            raise HTTPException(
                status_code=status.HTTP_428_PRECONDITION_REQUIRED,
                detail="任务会话请求必须提供 X-Device-ID",
            )
        return workspace_service.authenticate_task_session(
            bearer_token,
            requested_device_id=x_device_id,
            allow_closed_replay=(
                request.method == "POST" and request.url.path.endswith("/responses")
            ),
        )

    if bearer_token is not None and bearer_token.startswith(
        SCOPED_TASK_TOKEN_PREFIXES
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="该任务范围凭据不能访问任务协议",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if bearer_token is not None and bearer_token.startswith("cp_device_"):
        if x_owner_token:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Owner 设备会话不能与 Owner token 混用",
            )
        if x_device_id is None:
            raise HTTPException(
                status_code=status.HTTP_428_PRECONDITION_REQUIRED,
                detail="Owner 设备会话必须提供 X-Device-ID",
            )
        mobile_session = service.authenticate_mobile_access(bearer_token)
        stored_device_id = mobile_session["device"]["device_id"]
        if not secrets.compare_digest(
            x_device_id.encode("utf-8"), stored_device_id.encode("utf-8")
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Owner 设备标识与访问凭据不匹配",
            )
        return {
            "auth_kind": "owner_device_session",
            "assurance_method": "owner_device_session",
            "member_id": DEFAULT_OWNER_ID,
            "session_id": None,
            "device_id": stored_device_id,
            "idempotency_actor_id": f"owner-device:{stored_device_id}",
            "replay_only": False,
        }

    owner_candidates = [
        candidate
        for candidate in (
            bearer_token,
            x_owner_token.strip() if x_owner_token else None,
        )
        if candidate
    ]
    if any(service.owner_token_matches(candidate) for candidate in owner_candidates):
        return {
            "auth_kind": "owner_token",
            "assurance_method": "owner_token",
            "member_id": DEFAULT_OWNER_ID,
            "session_id": None,
            "device_id": x_device_id or "owner-token",
            "idempotency_actor_id": DEFAULT_OWNER_ID,
            "replay_only": False,
        }
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="任务协议访问凭据无效" if owner_candidates else "缺少任务协议凭据",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_task_change_access(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_owner_token: Annotated[str | None, Header(alias="X-Owner-Token")] = None,
    x_device_id: Annotated[str | None, Header(alias="X-Device-ID")] = None,
    service: PocketService = Depends(get_service),
) -> dict[str, Any]:
    bearer_token = _bearer_token(authorization) if authorization else None
    workspace_service: WorkspaceService = request.app.state.workspace_service

    # Scoped-token prefixes are dispatched before every broader credential.
    # A malformed or wrong-scope token must never fall through as an Owner or
    # paired-device credential.
    if bearer_token is not None and bearer_token.startswith("cp_task_ch_"):
        if x_owner_token:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="任务变更会话不能与 Owner 凭据混用",
            )
        if x_device_id is None:
            raise HTTPException(
                status_code=status.HTTP_428_PRECONDITION_REQUIRED,
                detail="任务变更会话请求必须提供 X-Device-ID",
            )
        return workspace_service.authenticate_task_change_session(
            bearer_token,
            requested_device_id=x_device_id,
            allow_closed_replay=(
                request.method == "POST" and request.url.path.endswith("/decisions")
            ),
        )

    if bearer_token is not None and bearer_token.startswith(
        SCOPED_TASK_TOKEN_PREFIXES
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="该任务范围凭据不能访问任务变更协议",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if bearer_token is not None and bearer_token.startswith("cp_device_"):
        if x_owner_token:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Owner 设备会话不能与 Owner token 混用",
            )
        if x_device_id is None:
            raise HTTPException(
                status_code=status.HTTP_428_PRECONDITION_REQUIRED,
                detail="Owner 设备会话必须提供 X-Device-ID",
            )
        mobile_session = service.authenticate_mobile_access(bearer_token)
        stored_device_id = mobile_session["device"]["device_id"]
        if not secrets.compare_digest(
            x_device_id.encode("utf-8"), stored_device_id.encode("utf-8")
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Owner 设备标识与访问凭据不匹配",
            )
        return {
            "auth_kind": "owner_device_session",
            "assurance_method": "owner_device_session",
            "member_id": DEFAULT_OWNER_ID,
            "session_id": None,
            "device_id": stored_device_id,
            "idempotency_actor_id": f"owner-device:{stored_device_id}",
            "replay_only": False,
        }

    owner_candidates = [
        candidate
        for candidate in (
            bearer_token,
            x_owner_token.strip() if x_owner_token else None,
        )
        if candidate
    ]
    if any(service.owner_token_matches(candidate) for candidate in owner_candidates):
        return {
            "auth_kind": "owner_token",
            "assurance_method": "owner_token",
            "member_id": DEFAULT_OWNER_ID,
            "session_id": None,
            "device_id": x_device_id or "owner-token",
            "idempotency_actor_id": DEFAULT_OWNER_ID,
            "replay_only": False,
        }
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=(
            "任务变更访问凭据无效"
            if owner_candidates
            else "缺少任务变更凭据"
        ),
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_task_execution_access(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_owner_token: Annotated[str | None, Header(alias="X-Owner-Token")] = None,
    x_device_id: Annotated[str | None, Header(alias="X-Device-ID")] = None,
) -> dict[str, Any]:
    bearer_token = _bearer_token(authorization) if authorization else None
    workspace_service: WorkspaceService = request.app.state.workspace_service
    if bearer_token is not None and bearer_token.startswith("cp_task_ex_"):
        if x_owner_token:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="任务执行会话不能与 Owner 凭据混用",
            )
        if x_device_id is None:
            raise HTTPException(
                status_code=status.HTTP_428_PRECONDITION_REQUIRED,
                detail="任务执行会话请求必须提供 X-Device-ID",
            )
        return workspace_service.authenticate_task_execution_session(
            bearer_token,
            requested_device_id=x_device_id,
            allow_closed_replay=request.method in {"POST", "PUT"},
        )
    if bearer_token is not None and bearer_token.startswith(
        SCOPED_TASK_TOKEN_PREFIXES
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="该任务范围凭据不能访问任务执行接口",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if bearer_token is not None or x_owner_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner 或设备凭据不能代替外部承办人执行任务",
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="缺少任务执行凭据",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _is_mobile_secretary(request: Request) -> bool:
    return getattr(request.state, "secretary_access_kind", None) == "device"


def require_collector(
    source_id: str,
    authorization: Annotated[str | None, Header()] = None,
    service: PocketService = Depends(get_service),
) -> dict[str, str]:
    return service.authenticate_collector(source_id, _bearer_token(authorization))


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owner_token, agent_token = runtime_settings.prepare()
        service = PocketService(
            Database(runtime_settings.database_path),
            owner_token=owner_token,
            agent_token=agent_token,
            max_file_bytes=runtime_settings.max_file_bytes,
            desktop_session_token=runtime_settings.desktop_session_token,
        )
        service.initialize()
        app.state.service = service
        workspace_service = WorkspaceService(
            service.database,
            task_session_hmac_key=(
                runtime_settings.resolve_task_session_hmac_key(
                    _derive_task_session_hmac_key(owner_token)
                )
            ),
        )
        workspace_service.initialize()
        app.state.workspace_service = workspace_service
        mail_service = OutlookMailService(
            service.database,
            data_root=runtime_settings.data_root,
            client_id=runtime_settings.outlook_client_id,
            tenant=runtime_settings.outlook_tenant,
            workspace_service=workspace_service,
            max_file_bytes=runtime_settings.max_file_bytes,
        )
        mail_service.initialize()
        service.attach_outlook_mail(mail_service)
        app.state.mail_service = mail_service
        proposal_store = ProposalStore(service.database)
        proposal_store.initialize()
        app.state.proposals = proposal_store
        ticket_store = CloudTicketStore(service.database)
        ticket_store.initialize()
        app.state.assistant_tickets = ticket_store
        # §3.4：模型编排在 Pocket 服务端；provider 未配置时保持 None，
        # /assistant/chat 返回 503，秘书端按 §3.5 降级为确定性指令
        app.state.assistant_local_provider = build_local_provider(runtime_settings)
        app.state.assistant_cloud_provider = build_cloud_provider(runtime_settings)
        scheduler_task: asyncio.Task[None] | None = None

        async def scheduler() -> None:
            while True:
                await asyncio.sleep(runtime_settings.scheduler_poll_seconds)
                try:
                    source_ids = await asyncio.to_thread(service.due_source_ids)
                    for source_id in source_ids:
                        await asyncio.to_thread(service.sync_source, source_id)
                    reliable_source_ids = await asyncio.to_thread(
                        service.reliable_sources.due_source_ids
                    )
                    for reliable_source_id in reliable_source_ids:
                        await asyncio.to_thread(
                            _collect_due_reliable_source_safely,
                            service,
                            reliable_source_id,
                        )
                    outlook_account_ids = await asyncio.to_thread(
                        mail_service.due_account_ids
                    )
                    for outlook_account_id in outlook_account_ids:
                        await asyncio.to_thread(
                            _sync_due_outlook_account_safely,
                            mail_service,
                            outlook_account_id,
                        )
                except Exception:
                    # Keep the scheduler alive when a database read or one
                    # connector fails unexpectedly. Failed runs have their own
                    # retry backoff and are safe to revisit on a later poll.
                    LOGGER.exception("Scheduled sync cycle failed")

        if runtime_settings.scheduler_poll_seconds > 0:
            scheduler_task = asyncio.create_task(
                scheduler(), name="centaurai-pocket-sync-scheduler"
            )
        try:
            yield
        finally:
            if scheduler_task is not None:
                scheduler_task.cancel()
                with suppress(asyncio.CancelledError):
                    await scheduler_task

    application = FastAPI(
        title="CentaurAI Pocket API",
        description="单用户、私有、移动优先的数据治理与 Agent 数据服务",
        version=__version__,
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(runtime_settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["ETag"],
    )
    application.add_middleware(SensitiveJsonBodyLimitMiddleware)
    mail_prefix = f"{API_PREFIX}/mail"
    mobile_prefix = f"{API_PREFIX}/mobile"
    workspace_prefix = f"{API_PREFIX}/workspaces"
    alignment_prefix = f"{API_PREFIX}/task-alignments"
    agreement_prefix = f"{API_PREFIX}/task-agreements"
    task_change_prefix = f"{API_PREFIX}/task-changes"
    task_execution_prefix = f"{API_PREFIX}/task-executions"
    task_change_invitation_prefix = f"{API_PREFIX}/task-change-invitations"

    @application.middleware("http")
    async def protect_sensitive_responses(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        if (
            request.url.path == mail_prefix
            or request.url.path.startswith(f"{mail_prefix}/")
            or request.url.path == mobile_prefix
            or request.url.path.startswith(f"{mobile_prefix}/")
            or request.url.path == workspace_prefix
            or request.url.path.startswith(f"{workspace_prefix}/")
            or request.url.path == alignment_prefix
            or request.url.path.startswith(f"{alignment_prefix}/")
            or request.url.path == agreement_prefix
            or request.url.path.startswith(f"{agreement_prefix}/")
            or request.url.path == task_change_prefix
            or request.url.path.startswith(f"{task_change_prefix}/")
            or request.url.path == task_change_invitation_prefix
            or request.url.path.startswith(f"{task_change_invitation_prefix}/")
            or request.url.path == task_execution_prefix
            or request.url.path.startswith(f"{task_execution_prefix}/")
        ):
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    public_task_execution_origin = runtime_settings.task_execution_public_origin
    application.include_router(
        create_workspace_router(
            require_secretary_access,
            task_execution_public_enabled=(
                public_task_execution_origin is not None
            ),
        )
    )
    application.include_router(create_task_alignment_router())
    application.include_router(
        create_task_agreement_router(require_task_agreement_access)
    )
    application.include_router(create_task_change_router(require_task_change_access))
    application.include_router(
        create_task_execution_router(require_task_execution_access)
    )
    application.include_router(create_task_change_invitation_router())
    application.include_router(create_reliable_sources_router(require_secretary_access))
    application.include_router(create_mail_router(require_secretary_access))
    if public_task_execution_origin is not None:
        # Installed last so its fail-safe boundary is the outermost user
        # middleware, including outside CORSMiddleware. Browser paths never
        # receive cross-origin preflight handling or CORS response headers.
        install_task_execution_browser(
            application,
            canonical_origin=public_task_execution_origin,
        )

    @application.exception_handler(PocketError)
    async def handle_pocket_error(_request: Request, exc: PocketError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )

    @application.exception_handler(RequestValidationError)
    async def handle_request_validation(
        request: Request,
        exc: RequestValidationError,
    ) -> Response:
        if request.url.path == mail_prefix or request.url.path.startswith(
            f"{mail_prefix}/"
        ):
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content={"detail": "邮件请求格式无效"},
                headers={
                    "Cache-Control": "no-store, max-age=0",
                    "Pragma": "no-cache",
                    "Referrer-Policy": "no-referrer",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        if request.url.path in {
            f"{API_PREFIX}/mobile/pairings/claim",
            f"{API_PREFIX}/mobile/sessions/refresh",
            f"{API_PREFIX}/task-alignments/preview",
            f"{API_PREFIX}/task-alignments/confirm",
            f"{API_PREFIX}/task-alignments/exchange",
            f"{API_PREFIX}/task-changes/exchange",
            f"{API_PREFIX}/task-executions/exchange",
            f"{API_PREFIX}/task-executions/refresh",
        } or request.url.path.startswith(
            f"{agreement_prefix}/"
        ) or request.url.path.startswith(
            f"{task_change_prefix}/"
        ) or request.url.path.startswith(f"{task_execution_prefix}/"):
            sensitive_task_request = (
                "/task-alignments/" in request.url.path
                or request.url.path.startswith(f"{agreement_prefix}/")
                or request.url.path.startswith(f"{task_change_prefix}/")
                or request.url.path.startswith(f"{task_execution_prefix}/")
            )
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content={"detail": "请求格式无效"},
                headers=(
                    {
                        "Cache-Control": "no-store, max-age=0",
                        "Pragma": "no-cache",
                        "Referrer-Policy": "no-referrer",
                        "X-Content-Type-Options": "nosniff",
                    }
                    if sensitive_task_request
                    else None
                ),
            )
        return await request_validation_exception_handler(request, exc)

    @application.get(f"{API_PREFIX}/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "service": "centaurai-pocket",
            "version": __version__,
        }

    @application.post(
        f"{API_PREFIX}/mobile/pairings",
        response_model=MobilePairingCreated,
        status_code=status.HTTP_201_CREATED,
    )
    def create_mobile_pairing(
        _payload: MobilePairingCreate | None = Body(default=None),
        service: PocketService = Depends(require_owner),
    ) -> dict[str, Any]:
        return service.create_mobile_pairing()

    @application.post(
        f"{API_PREFIX}/mobile/pairings/claim",
        response_model=MobileSessionTokens,
    )
    def claim_mobile_pairing(
        payload: MobilePairingClaim,
        service: PocketService = Depends(get_service),
    ) -> dict[str, Any]:
        return service.claim_mobile_pairing(payload.model_dump())

    @application.post(
        f"{API_PREFIX}/mobile/sessions/refresh",
        response_model=MobileSessionTokens,
    )
    def refresh_mobile_session(
        payload: MobileSessionRefresh,
        service: PocketService = Depends(get_service),
    ) -> dict[str, Any]:
        return service.refresh_mobile_session(
            payload.refresh_token,
            payload.device_id,
        )

    @application.get(
        f"{API_PREFIX}/mobile/session",
        response_model=MobileSessionView,
    )
    def mobile_session(
        session: dict[str, Any] = Depends(require_mobile_device),
    ) -> dict[str, Any]:
        return session

    @application.get(
        f"{API_PREFIX}/mobile/devices",
        response_model=MobileDeviceList,
    )
    def list_mobile_devices(
        service: PocketService = Depends(require_owner),
    ) -> dict[str, Any]:
        return service.list_mobile_devices()

    @application.delete(
        f"{API_PREFIX}/mobile/devices/{{mobile_device_id}}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def revoke_mobile_device(
        mobile_device_id: str,
        service: PocketService = Depends(require_owner),
    ) -> Response:
        service.revoke_mobile_device(mobile_device_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.get(f"{API_PREFIX}/dashboard")
    def dashboard(
        service: PocketService = Depends(require_secretary_access),
    ) -> dict:
        return service.dashboard()

    @application.post(
        f"{API_PREFIX}/sources",
        status_code=status.HTTP_201_CREATED,
    )
    def create_source(
        payload: SourceCreate,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        service: PocketService = Depends(require_owner),
    ) -> dict:
        return service.create_source(
            payload.model_dump(),
            idempotency_key=idempotency_key,
        )

    @application.get(f"{API_PREFIX}/sources")
    def list_sources(
        service: PocketService = Depends(require_owner),
    ) -> dict:
        return service.list_sources()

    @application.get(f"{API_PREFIX}/sources/{{source_id}}")
    def get_source(
        source_id: str,
        service: PocketService = Depends(require_owner),
    ) -> dict:
        return service.get_source(source_id)

    @application.patch(f"{API_PREFIX}/sources/{{source_id}}")
    def update_source(
        source_id: str,
        payload: SourceUpdate,
        service: PocketService = Depends(require_owner),
    ) -> dict:
        return service.update_source(
            source_id,
            payload.model_dump(exclude_unset=True, exclude_none=True),
        )

    @application.delete(
        f"{API_PREFIX}/sources/{{source_id}}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def delete_source(
        source_id: str,
        service: PocketService = Depends(require_owner),
    ) -> Response:
        service.delete_source(source_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.post(f"{API_PREFIX}/sources/{{source_id}}/sync")
    def sync_source(
        source_id: str,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        service: PocketService = Depends(require_owner),
    ) -> Any:
        result = service.sync_source(
            source_id,
            idempotency_key=idempotency_key,
        )
        if result["status"] == "failed":
            return JSONResponse(
                status_code=status.HTTP_502_BAD_GATEWAY,
                content={
                    "detail": result["error"] or "数据源同步失败",
                    "sync_run": result,
                },
            )
        return result

    @application.post(
        f"{API_PREFIX}/sources/{{source_id}}/pairings",
        status_code=status.HTTP_201_CREATED,
    )
    def create_observer_pairing(
        source_id: str,
        service: PocketService = Depends(require_owner),
    ) -> dict:
        return service.create_observer_pairing(source_id)

    @application.delete(
        f"{API_PREFIX}/sources/{{source_id}}/pairings/{{pairing_id}}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def revoke_observer_pairing(
        source_id: str,
        pairing_id: str,
        service: PocketService = Depends(require_owner),
    ) -> Response:
        service.revoke_observer_pairing(source_id, pairing_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.post(f"{API_PREFIX}/sources/{{source_id}}/pause")
    def pause_observer(
        source_id: str,
        service: PocketService = Depends(require_owner),
    ) -> dict:
        return service.set_observer_enabled(source_id, enabled=False)

    @application.post(f"{API_PREFIX}/sources/{{source_id}}/resume")
    def resume_observer(
        source_id: str,
        service: PocketService = Depends(require_owner),
    ) -> dict:
        return service.set_observer_enabled(source_id, enabled=True)

    @application.get(f"{API_PREFIX}/sources/{{source_id}}/observer-status")
    def observer_status(
        source_id: str,
        service: PocketService = Depends(require_owner),
    ) -> dict:
        return service.observer_status(source_id)

    @application.get(f"{API_PREFIX}/sources/{{source_id}}/coverage-gaps")
    def observer_coverage_gaps(
        source_id: str,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        service: PocketService = Depends(require_owner),
    ) -> dict:
        return service.list_observer_gaps(source_id, limit=limit, offset=offset)

    @application.post(
        f"{API_PREFIX}/collectors/v1/sources/{{source_id}}/handshake",
        status_code=status.HTTP_201_CREATED,
    )
    def collector_handshake(
        source_id: str,
        payload: CollectorHandshake,
        authorization: Annotated[str | None, Header()] = None,
        service: PocketService = Depends(get_service),
    ) -> dict:
        return service.collector_handshake(
            source_id,
            _bearer_token(authorization),
            payload.model_dump(mode="json"),
        )

    @application.post(f"{API_PREFIX}/collectors/v1/sources/{{source_id}}/heartbeat")
    def collector_heartbeat(
        source_id: str,
        payload: CollectorHeartbeat,
        collector: dict[str, str] = Depends(require_collector),
        service: PocketService = Depends(get_service),
    ) -> dict:
        return service.record_observer_heartbeat(
            source_id,
            collector,
            payload.model_dump(mode="json"),
        )

    @application.post(
        f"{API_PREFIX}/collectors/v1/sources/{{source_id}}/events",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def collector_events(
        source_id: str,
        payload: CollectorEventBatch,
        collector: dict[str, str] = Depends(require_collector),
        service: PocketService = Depends(get_service),
    ) -> dict:
        return service.ingest_observer_events(
            source_id,
            collector,
            payload.model_dump(mode="json"),
        )

    @application.get(f"{API_PREFIX}/im/conversations")
    def list_im_conversations(
        source_id: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        service: PocketService = Depends(require_owner),
    ) -> dict:
        return service.list_im_conversations(
            source_id=source_id, limit=limit, offset=offset
        )

    @application.get(f"{API_PREFIX}/im/conversations/{{conversation_id}}/messages")
    def list_im_messages(
        conversation_id: str,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        service: PocketService = Depends(require_owner),
    ) -> dict:
        return service.get_im_messages(conversation_id, limit=limit, offset=offset)

    @application.patch(f"{API_PREFIX}/im/conversations/{{conversation_id}}/policy")
    def update_im_conversation_policy(
        conversation_id: str,
        payload: ConversationPolicyUpdate,
        service: PocketService = Depends(require_owner),
    ) -> dict:
        return service.update_conversation_policy(
            conversation_id,
            payload.model_dump(exclude_unset=True, exclude_none=True),
        )

    @application.get(f"{API_PREFIX}/knowledge/candidates")
    def list_knowledge_candidates(
        request: Request,
        candidate_status: Literal["provisional", "confirmed", "dismissed", "superseded"]
        | None = Query(default=None, alias="status"),
        conversation_id: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        service: PocketService = Depends(require_secretary_access),
    ) -> dict:
        if _is_mobile_secretary(request):
            if candidate_status not in (None, "provisional"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="手机设备只能读取待确认知识候选",
                )
            candidate_status = "provisional"
        return service.list_knowledge_candidates(
            status=candidate_status,
            conversation_id=conversation_id,
            limit=limit,
            offset=offset,
        )

    @application.post(f"{API_PREFIX}/knowledge/candidates/{{candidate_id}}/confirm")
    def confirm_knowledge_candidate(
        candidate_id: str,
        service: PocketService = Depends(require_secretary_access),
    ) -> dict:
        return service.resolve_knowledge_candidate(candidate_id, action="confirm")

    @application.post(f"{API_PREFIX}/knowledge/candidates/{{candidate_id}}/dismiss")
    def dismiss_knowledge_candidate(
        candidate_id: str,
        service: PocketService = Depends(require_secretary_access),
    ) -> dict:
        return service.resolve_knowledge_candidate(candidate_id, action="dismiss")

    @application.get(f"{API_PREFIX}/maintenance/retention-preview")
    def retention_preview(
        service: PocketService = Depends(require_owner),
    ) -> dict:
        return service.retention_preview()

    @application.post(f"{API_PREFIX}/maintenance/retention-apply")
    def retention_apply(
        _payload: RetentionApply,
        service: PocketService = Depends(require_owner),
    ) -> dict:
        return service.apply_retention()

    @application.get(f"{API_PREFIX}/sync-runs")
    def list_sync_runs(
        source_id: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        service: PocketService = Depends(require_owner),
    ) -> dict:
        return service.list_sync_runs(source_id=source_id, limit=limit)

    @application.get(f"{API_PREFIX}/sync-runs/{{run_id}}")
    def get_sync_run(
        run_id: str,
        service: PocketService = Depends(require_owner),
    ) -> dict:
        return service.get_sync_run(run_id)

    @application.get(f"{API_PREFIX}/items")
    def list_items(
        item_state: ItemState | None = Query(default=None, alias="state"),
        query: str | None = Query(default=None, max_length=1000),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        service: PocketService = Depends(require_owner),
    ) -> dict:
        return service.list_items(
            state=item_state,
            query=query,
            limit=limit,
            offset=offset,
        )

    @application.get(f"{API_PREFIX}/items/{{item_id}}")
    def get_item(
        item_id: str,
        service: PocketService = Depends(require_owner),
    ) -> dict:
        return service.get_item(item_id)

    @application.patch(f"{API_PREFIX}/items/{{item_id}}")
    def update_item(
        item_id: str,
        payload: ItemPatch,
        service: PocketService = Depends(require_owner),
    ) -> dict:
        return service.update_item(item_id, _item_patch_values(payload))

    def create_capture_response(
        payload: CaptureCreate,
        idempotency_key_header: str | None,
        service: PocketService,
    ) -> dict:
        values = payload.model_dump(by_alias=False)
        body_key = values.pop("idempotency_key", None)
        return service.capture_text(
            values,
            idempotency_key=idempotency_key_header or body_key,
        )

    @application.post(
        f"{API_PREFIX}/captures",
        status_code=status.HTTP_201_CREATED,
    )
    def create_capture(
        payload: CaptureCreate,
        idempotency_key_header: Annotated[
            str | None, Header(alias="Idempotency-Key")
        ] = None,
        service: PocketService = Depends(require_secretary_access),
    ) -> dict:
        return create_capture_response(payload, idempotency_key_header, service)

    @application.post(
        f"{API_PREFIX}/imports/text",
        status_code=status.HTTP_201_CREATED,
        include_in_schema=True,
    )
    def import_text(
        payload: CaptureCreate,
        idempotency_key_header: Annotated[
            str | None, Header(alias="Idempotency-Key")
        ] = None,
        service: PocketService = Depends(require_owner),
    ) -> dict:
        return create_capture_response(payload, idempotency_key_header, service)

    @application.get(f"{API_PREFIX}/governance/tasks")
    def list_tasks(
        request: Request,
        task_status: TaskStatus | None = Query(default="pending", alias="status"),
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        service: PocketService = Depends(require_secretary_access),
    ) -> dict:
        if _is_mobile_secretary(request) and task_status != "pending":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="手机设备只能读取待处理治理任务",
            )
        return service.list_tasks(
            status=task_status,
            limit=limit,
            offset=offset,
        )

    @application.get(f"{API_PREFIX}/governance/tasks/{{task_id}}")
    def get_task(
        task_id: str,
        service: PocketService = Depends(require_owner),
    ) -> dict:
        return service.get_task(task_id)

    @application.post(f"{API_PREFIX}/governance/tasks/{{task_id}}/apply")
    def apply_task(
        task_id: str,
        payload: GovernanceApply | None = Body(default=None),
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        service: PocketService = Depends(require_secretary_access),
    ) -> dict:
        patch = (
            _item_patch_values(payload.patch)
            if payload is not None
            else {"state": "ready"}
        )
        return service.apply_task(
            task_id,
            patch,
            idempotency_key=idempotency_key
            or (payload.idempotency_key if payload else None),
        )

    @application.post(f"{API_PREFIX}/governance/tasks/{{task_id}}/skip")
    def skip_task(
        task_id: str,
        payload: GovernanceSkip | None = Body(default=None),
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        service: PocketService = Depends(require_secretary_access),
    ) -> dict:
        return service.skip_task(
            task_id,
            idempotency_key=idempotency_key
            or (payload.idempotency_key if payload else None),
        )

    @application.post(f"{API_PREFIX}/governance/tasks/{{task_id}}/undo")
    def undo_task(
        task_id: str,
        payload: GovernanceUndo | None = Body(default=None),
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        service: PocketService = Depends(require_owner),
    ) -> dict:
        return service.undo_task(
            task_id,
            idempotency_key=idempotency_key
            or (payload.idempotency_key if payload else None),
        )

    @application.post(f"{API_PREFIX}/governance/tasks/{{task_id}}/actions")
    def task_action(
        task_id: str,
        payload: GovernanceAction,
        idempotency_key_header: Annotated[
            str | None, Header(alias="Idempotency-Key")
        ] = None,
        service: PocketService = Depends(require_owner),
    ) -> dict:
        idempotency_key = idempotency_key_header or payload.idempotency_key
        if payload.action == "apply":
            patch = (
                _item_patch_values(payload.patch)
                if payload.patch is not None
                else {"state": "ready"}
            )
            return service.apply_task(
                task_id,
                patch,
                idempotency_key=idempotency_key,
            )
        if payload.action == "skip":
            return service.skip_task(
                task_id,
                idempotency_key=idempotency_key,
            )
        return service.undo_task(
            task_id,
            idempotency_key=idempotency_key,
        )

    # ------------------------------------------------------------------
    # Assistant proposals (§3.1/§3.3): the owner-gated half of the proposal
    # tools. Reads list the queue; apply is the only path into the workspace
    # and rides the same service methods, device id and idempotency contract
    # as the owner's own manual writes.
    # ------------------------------------------------------------------

    def _materialize_assistant_proposal(
        request: Request,
        kind: str,
        fields: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> str:
        from pydantic import ValidationError

        from .workspace.schemas import (
            BusinessTaskCreate,
            CalendarEntryCreate,
            MemoCreate,
            TaskChangeCreate,
        )
        from .workspace.service import DEFAULT_OWNER_ID

        workspace_service = request.app.state.workspace_service
        mail_service = request.app.state.mail_service
        workspace_id = "ws_default"
        try:
            if kind == "memo":
                payload = MemoCreate.model_validate({
                    "domain": fields.get("domain", "work"),
                    "urgency": fields.get("urgency", "normal"),
                    "title": fields["title"],
                    "content": fields["content"],
                    "source": {
                        "source_kind": "system",
                        "source_ref": "assistant-proposal",
                        "authority": "user_provided",
                    },
                })
                entity = workspace_service.create_memo(
                    workspace_id,
                    payload.model_dump(mode="json"),
                    idempotency_key=idempotency_key,
                    device_id=device_id,
                )
                return f"memo:{entity['id']}"
            if kind == "task":
                payload = BusinessTaskCreate.model_validate({
                    "domain": fields.get("domain", "work"),
                    "title": fields["title"],
                    "purpose": fields["purpose"],
                    "objective": fields["objective"],
                    "strategy": fields["strategy"],
                    "acceptance_criteria": fields["acceptance_criteria"],
                    "issuer_member_id": DEFAULT_OWNER_ID,
                    "assignee_member_id": fields["assignee_member_id"],
                    "acceptance_owner_id": DEFAULT_OWNER_ID,
                    **({"start_at": fields["start_at"]} if fields.get("start_at") else {}),
                    **({"due_at": fields["due_at"]} if fields.get("due_at") else {}),
                })
                entity = workspace_service.create_task(
                    workspace_id,
                    payload.model_dump(mode="json"),
                    idempotency_key=idempotency_key,
                    device_id=device_id,
                )
                return f"task:{entity['id']}"
            if kind == "calendar":
                workspace = workspace_service.bootstrap(workspace_id)["workspace"]
                payload = CalendarEntryCreate.model_validate({
                    "domain": fields.get("domain", "work"),
                    "title": fields["title"],
                    **({"description": fields["description"]} if fields.get("description") else {}),
                    "start_at": fields["start_at"],
                    "end_at": fields["end_at"],
                    "timezone": workspace["timezone"],
                    "kind": fields.get("kind", "focus"),
                })
                entity = workspace_service.create_calendar_entry(
                    workspace_id,
                    payload.model_dump(mode="json"),
                    idempotency_key=idempotency_key,
                    device_id=device_id,
                )
                return f"calendar:{entity['id']}"
            if kind == "task_change":
                task_id = fields["task_id"]
                tasks = workspace_service.list_tasks(workspace_id)["items"]
                task = next((item for item in tasks if item["id"] == task_id), None)
                if task is None:
                    raise PocketError(409, "提议指向的任务已不存在")
                payload = TaskChangeCreate.model_validate({
                    "change_type": fields["change_type"],
                    "base_version": task["version"],
                    "reason": fields["reason"],
                    "patch": fields["patch"],
                })
                entity = workspace_service.create_task_change(
                    workspace_id,
                    task_id,
                    payload.model_dump(mode="json"),
                    idempotency_key=idempotency_key,
                    device_id=device_id,
                )
                change = entity.get("change") if isinstance(entity.get("change"), dict) else entity
                return f"task_change:{change.get('id', task_id)}"
            if kind == "mail_reply":
                message = mail_service.get_message(fields["message_id"])
                draft = mail_service.create_reply_draft(
                    fields["message_id"],
                    int(message["version"]),
                    fields["body_text"],
                    idempotency_key=idempotency_key,
                    device_id=device_id,
                )
                draft_view = draft.get("draft") if isinstance(draft.get("draft"), dict) else draft
                return f"mail_draft:{draft_view.get('id', fields['message_id'])}"
        except ValidationError as error:
            raise PocketError(422, f"提议字段无效：{error.errors()[0]['msg']}") from error
        except KeyError as error:
            raise PocketError(422, f"提议缺少字段：{error.args[0]}") from error
        raise PocketError(422, f"未知提议类型：{kind}")

    @application.get(f"{API_PREFIX}/assistant/proposals")
    def list_assistant_proposals(
        request: Request,
        proposal_status: Literal["pending", "applied", "dismissed"] = Query(
            default="pending", alias="status"
        ),
        service: PocketService = Depends(require_owner),
    ) -> dict:
        return request.app.state.proposals.list(proposal_status)

    @application.post(f"{API_PREFIX}/assistant/proposals/{{proposal_id}}/apply")
    def apply_assistant_proposal(
        proposal_id: str,
        request: Request,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
        ],
        device_id: Annotated[
            str, Header(alias="X-Device-ID", min_length=1, max_length=200)
        ],
        payload: dict[str, Any] | None = Body(default=None),
        service: PocketService = Depends(require_owner),
    ) -> dict:
        if payload is not None and set(payload) - {"fields"}:
            raise PocketError(422, "apply 请求只接受 fields 字段")
        overrides = (payload or {}).get("fields") or {}
        if not isinstance(overrides, dict):
            raise PocketError(422, "fields 必须是对象")
        proposals: ProposalStore = request.app.state.proposals
        proposal = proposals.get(proposal_id)
        if proposal["status"] != "pending":
            raise PocketError(409, "提议已被处理，不能重复决定")
        allowed_override_keys = set(proposal["fields"])
        unknown = set(overrides) - allowed_override_keys
        if unknown:
            raise PocketError(422, "只能修改提议中已有的字段")
        merged = {**proposal["fields"], **overrides}
        result_ref = _materialize_assistant_proposal(
            request,
            proposal["kind"],
            merged,
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        decided = proposals.decide(proposal_id, status="applied", result_ref=result_ref)
        return {"proposal": decided, "result_ref": result_ref}

    @application.post(f"{API_PREFIX}/assistant/proposals/{{proposal_id}}/dismiss")
    def dismiss_assistant_proposal(
        proposal_id: str,
        request: Request,
        service: PocketService = Depends(require_owner),
    ) -> dict:
        proposals: ProposalStore = request.app.state.proposals
        return {"proposal": proposals.decide(proposal_id, status="dismissed")}

    def _assistant_tools(
        request: Request,
        service: PocketService,
        *,
        include_knowledge: bool = True,
    ) -> list[MCPTool]:
        """§3.3 工具面 ＋ knowledge_retrieve，供编排循环直接调用。

        云端通道未授权 documents 正文时不给 knowledge_retrieve：
        检索摘录就是正文内容，默认剔除，需主人在票据里额外勾选。
        """

        tools = build_assistant_tools(
            request.app.state.workspace_service,
            service,
            request.app.state.mail_service,
            request.app.state.proposals,
        )
        if include_knowledge:

            def knowledge_handler(arguments: dict) -> dict:
                query, limit, filters = MCPServer._knowledge_arguments(  # noqa: SLF001
                    dict(arguments)
                )
                return service.agent_search(
                    query=query,
                    limit=limit,
                    tags=filters["tags"],
                    category=filters["category"],
                )

            tools = [MCPTool(KNOWLEDGE_RETRIEVE_TOOL, knowledge_handler), *tools]
        return tools

    @application.get(f"{API_PREFIX}/assistant/status")
    def assistant_status(
        request: Request,
        service: PocketService = Depends(require_owner),
    ) -> dict:
        local = request.app.state.assistant_local_provider
        cloud = request.app.state.assistant_cloud_provider
        tickets: CloudTicketStore = request.app.state.assistant_tickets
        stats = tickets.stats()
        return {
            "local": (
                {"provider": local.name, "model": local.model} if local else None
            ),
            "cloud": (
                {"provider": cloud.name, "model": cloud.model} if cloud else None
            ),
            "limits": {
                "max_rounds": 6,
                "max_seconds": 30,
                "max_response_bytes": 64 * 1024,
                "ticket_ttl_seconds": 300,
            },
            "calls_total": stats["calls_total"],
            "tool_calls_total": stats["tool_calls_total"],
            "last_call": stats["last_call"],
            "active_tickets": tickets.active()["total"],
            "proposals_pending": request.app.state.proposals.list("pending")["total"],
        }

    @application.post(f"{API_PREFIX}/assistant/chat")
    def assistant_chat(
        request: Request,
        device_id: Annotated[
            str, Header(alias="X-Device-ID", min_length=1, max_length=200)
        ],
        payload: dict[str, Any] = Body(...),
        service: PocketService = Depends(require_owner),
    ) -> dict:
        if set(payload) - {"message", "channel", "ticket_id"}:
            raise PocketError(422, "chat 请求含未知字段")
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            raise PocketError(422, "message 不能为空")
        if len(message) > 8000:
            raise PocketError(422, "message 不能超过 8000 字")
        channel = payload.get("channel", "local")
        if channel not in {"local", "cloud"}:
            raise PocketError(422, "channel 只能是 local 或 cloud")

        ticket_id: str | None = None
        if channel == "cloud":
            provider = request.app.state.assistant_cloud_provider
            if provider is None:
                raise PocketError(503, "云端模型 Provider 未配置")
            raw_ticket = payload.get("ticket_id")
            if not isinstance(raw_ticket, str) or not raw_ticket:
                raise PocketError(422, "云端调用必须携带授权票据 ID")
            tickets: CloudTicketStore = request.app.state.assistant_tickets
            ticket = tickets.consume(raw_ticket)
            ticket_id = ticket["ticket_id"]
            scope_items = ticket["scope"].get("items", [])
            include_knowledge = any(
                item.get("category") == "documents" and item.get("include_body")
                for item in scope_items
            )
            # §3.4：每次云端调用写一条 assistant.cloud_call 审计事件
            request.app.state.workspace_service.record_assistant_cloud_call(
                "ws_default",
                device_id=device_id,
                ticket_id=ticket_id,
                payload={
                    "provider": provider.name,
                    "model": provider.model,
                    "scope": ticket["scope"],
                    "message_chars": len(message),
                },
            )
        else:
            provider = request.app.state.assistant_local_provider
            if provider is None:
                raise PocketError(503, "本地模型 Provider 未配置")
            include_knowledge = True

        loop = AssistantLoop(
            provider,
            _assistant_tools(
                request, service, include_knowledge=include_knowledge
            ),
        )
        try:
            result = loop.run(message.strip(), ticket_id=ticket_id)
        except ProviderError as error:
            raise PocketError(503, f"模型调用失败：{str(error)[:300]}") from error
        tickets_store: CloudTicketStore = request.app.state.assistant_tickets
        tickets_store.record_call(
            channel=channel,
            provider=provider.name,
            model=provider.model,
            rounds=result["provenance"]["tool_rounds"],
            tool_calls=result["tool_calls"],
            retrieval_count=result["provenance"]["retrieval_count"],
            duration_ms=result["provenance"]["duration_ms"],
            stopped=result["stopped"],
            ticket_id=ticket_id,
        )
        return result

    @application.post(
        f"{API_PREFIX}/assistant/cloud-tickets",
        status_code=status.HTTP_201_CREATED,
    )
    def issue_cloud_ticket(
        request: Request,
        device_id: Annotated[
            str, Header(alias="X-Device-ID", min_length=1, max_length=200)
        ],
        payload: dict[str, Any] = Body(...),
        service: PocketService = Depends(require_owner),
    ) -> dict:
        if set(payload) - {"scope"}:
            raise PocketError(422, "cloud-tickets 请求只接受 scope 字段")
        tickets: CloudTicketStore = request.app.state.assistant_tickets
        return tickets.issue(payload.get("scope"))

    @application.get(f"{API_PREFIX}/assistant/cloud-tickets")
    def list_cloud_tickets(
        request: Request,
        service: PocketService = Depends(require_owner),
    ) -> dict:
        tickets: CloudTicketStore = request.app.state.assistant_tickets
        return tickets.active()

    @application.post(f"{API_PREFIX}/agent/search")
    def agent_search(
        payload: AgentSearchRequest,
        service: PocketService = Depends(require_agent),
    ) -> dict:
        return service.agent_search(
            query=payload.query,
            limit=payload.limit,
            tags=payload.filters.tags,
            category=payload.filters.category,
            source_ids=payload.filters.source_ids,
            conversation_ids=payload.filters.conversation_ids,
            participant_ids=payload.filters.participant_ids,
            sent_from=payload.filters.sent_from,
            sent_to=payload.filters.sent_to,
            item_kinds=payload.filters.item_kinds,
        )

    @application.get(f"{API_PREFIX}/agent/token")
    def agent_token_metadata(
        service: PocketService = Depends(require_owner),
    ) -> dict[str, str]:
        return {
            "prefix": service.agent_token[:16],
            "mode": (
                "environment"
                if runtime_settings.agent_token is not None
                else "generated"
            ),
        }

    @application.post(f"{API_PREFIX}/agent/token/rotate")
    def rotate_agent_token(
        service: PocketService = Depends(require_owner),
    ) -> dict[str, str]:
        try:
            token = runtime_settings.rotate_agent_token()
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        service.replace_agent_token(token)
        return {
            "token": token,
            "prefix": token[:16],
        }

    @application.post(f"{API_PREFIX}/mcp")
    async def mcp_endpoint(
        request: Request,
        origin: Annotated[str | None, Header(alias="Origin")] = None,
        protocol_version: Annotated[
            str | None, Header(alias="MCP-Protocol-Version")
        ] = None,
        service: PocketService = Depends(require_agent),
    ) -> Response:
        if origin is not None and origin.rstrip("/") not in set(
            runtime_settings.cors_origins
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="MCP Origin 不在允许列表中",
            )
        if protocol_version is not None and protocol_version != PROTOCOL_VERSION:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"不支持的 MCP-Protocol-Version；当前版本为 {PROTOCOL_VERSION}"
                ),
            )

        body = await request.body()
        if protocol_version is None:
            try:
                decoded = json.loads(body)
            except (UnicodeDecodeError, ValueError, TypeError):
                decoded = None
            if isinstance(decoded, dict) and decoded.get("method") != "initialize":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="初始化后的 MCP 请求必须携带 MCP-Protocol-Version",
                )

        handler = MCPServer(
            lambda *, query, limit, filters: service.agent_search(
                query=query,
                limit=limit,
                tags=filters["tags"],
                category=filters["category"],
            ),
            server_version=__version__,
            # §3.3: read-only projections plus proposal tools. Forbidden
            # capabilities are absent from this list, not guarded by prompts.
            extra_tools=build_assistant_tools(
                request.app.state.workspace_service,
                service,
                request.app.state.mail_service,
                request.app.state.proposals,
            ),
        )
        message = handler.handle_json(body)
        if message is None:
            return Response(
                status_code=status.HTTP_202_ACCEPTED,
                headers={"MCP-Protocol-Version": PROTOCOL_VERSION},
            )
        return JSONResponse(
            content=message,
            headers={"MCP-Protocol-Version": PROTOCOL_VERSION},
        )

    return application


app = create_app()


def run() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
    )
