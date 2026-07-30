from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any

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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .config import Settings
from .database import Database
from .mcp import PROTOCOL_VERSION, MCPServer
from .schemas import (
    AgentSearchRequest,
    CaptureCreate,
    GovernanceAction,
    GovernanceApply,
    GovernanceSkip,
    GovernanceUndo,
    ItemPatch,
    ItemState,
    SourceCreate,
    SourceUpdate,
    TaskStatus,
)
from .service import PocketError, PocketService

API_PREFIX = "/api/v1"
LOGGER = logging.getLogger(__name__)


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
        candidates.append(_bearer_token(authorization))
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
        )
        service.initialize()
        app.state.service = service
        scheduler_task: asyncio.Task[None] | None = None

        async def scheduler() -> None:
            while True:
                await asyncio.sleep(runtime_settings.scheduler_poll_seconds)
                try:
                    source_ids = await asyncio.to_thread(service.due_source_ids)
                    for source_id in source_ids:
                        await asyncio.to_thread(service.sync_source, source_id)
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
    )

    @application.exception_handler(PocketError)
    async def handle_pocket_error(_request: Request, exc: PocketError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )

    @application.get(f"{API_PREFIX}/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "service": "centaurai-pocket",
            "version": __version__,
        }

    @application.get(f"{API_PREFIX}/dashboard")
    def dashboard(
        service: PocketService = Depends(require_owner),
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
        service: PocketService = Depends(require_owner),
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
        task_status: TaskStatus | None = Query(default="pending", alias="status"),
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        service: PocketService = Depends(require_owner),
    ) -> dict:
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
        service: PocketService = Depends(require_owner),
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
        service: PocketService = Depends(require_owner),
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
                    "不支持的 MCP-Protocol-Version；"
                    f"当前版本为 {PROTOCOL_VERSION}"
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
