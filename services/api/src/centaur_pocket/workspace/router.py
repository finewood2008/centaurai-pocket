from __future__ import annotations

import html
import re
import secrets
from collections.abc import Callable
from datetime import date
from typing import Annotated, Any
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse

from ..service import PocketError
from .schemas import (
    BusinessTaskCreate,
    CalendarEntryCreate,
    CalendarEntryPatch,
    DocumentCreate,
    DocumentExcerptCreate,
    DocumentGenerate,
    DocumentPatch,
    DocumentReviewCreate,
    MeetingCreate,
    MeetingMinutesCreate,
    MeetingMinutesDecision,
    MeetingPatch,
    MemoCalendarMaterializationCreate,
    MemoCreate,
    MemoPatch,
    MemoTaskMaterializationCreate,
    SyncCursorAck,
    TaskAgreementResponse,
    TaskAlignmentConfirm,
    TaskAlignmentExchange,
    TaskAlignmentInvitationCreate,
    TaskAlignmentPreview,
    TaskChangeCreate,
    TaskChangeDecision,
    TaskChangeExchange,
    TaskChangeInvitationCreate,
    TaskChangeProtocolDecision,
    TaskCheckInCreate,
    TaskExecutionCheckInCreate,
    TaskExecutionCommand,
    TaskExecutionExchange,
    TaskExecutionInvitationCreate,
    TaskExecutionRefresh,
    TaskExecutionStepStatus,
    TaskPatch,
    TaskStepAppend,
    TaskStepPatch,
    TaskStepScheduleStatus,
    TaskStepScheduleUpsert,
    TaskStepSet,
    TaskStepsReorder,
    TaskTransition,
    VersionCommand,
    WorkspaceMemberCreate,
)
from .service import DEFAULT_OWNER_ID, WorkspaceService

IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=200),
]
DeviceId = Annotated[
    str,
    Header(alias="X-Device-ID", min_length=1, max_length=200),
]
IfMatch = Annotated[str | None, Header(alias="If-Match")]
MATERIALIZATION_PATH_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}\Z")


def get_workspace_service(request: Request) -> WorkspaceService:
    return request.app.state.workspace_service


def _expected_version(if_match: str | None) -> int:
    if if_match is None:
        raise PocketError(428, "修改资源必须提供 If-Match 版本")
    value = if_match.strip()
    if value.startswith("W/"):
        value = value[2:].strip()
    value = value.strip('"')
    try:
        parsed = int(value)
    except ValueError as error:
        raise PocketError(400, "If-Match 必须是资源版本号") from error
    if parsed < 1:
        raise PocketError(400, "If-Match 必须是正整数")
    return parsed


def _set_etag(response: Response, entity: dict[str, Any]) -> dict[str, Any]:
    version = entity.get("version")
    if isinstance(version, int):
        response.headers["ETag"] = f'"{version}"'
    return entity


def _matching_body_version(if_match: str | None, expected_version: int) -> int:
    header_version = _expected_version(if_match)
    if header_version != expected_version:
        raise PocketError(412, "If-Match 与 expected_version 必须一致")
    return header_version


def _matching_strong_body_version(
    if_match: str | None, expected_version: int
) -> int:
    if if_match is None:
        raise PocketError(428, "修改资源必须提供 If-Match 版本")
    if re.fullmatch(r'"[1-9][0-9]*"', if_match) is None:
        raise PocketError(400, "If-Match 必须是单个带引号的强版本 ETag")
    header_version = int(if_match[1:-1])
    if header_version != expected_version:
        raise PocketError(412, "If-Match 与 expected_version 必须一致")
    return header_version


def _reject_query(request: Request) -> None:
    if request.url.query:
        raise PocketError(400, "该接口不接受 query 参数")


def _require_materialization_path_id(value: str, label: str) -> None:
    if MATERIALIZATION_PATH_ID.fullmatch(value) is None:
        raise PocketError(400, f"{label}格式无效")


def create_workspace_router(
    owner_dependency: Callable[..., Any],
    *,
    task_execution_public_enabled: bool = False,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/workspaces", tags=["secretary-workspace"])
    owner = Depends(owner_dependency)

    @router.get("/{workspace_id}")
    def workspace(
        workspace_id: str,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        return service.workspace(workspace_id)

    @router.post(
        "/{workspace_id}/members",
        status_code=status.HTTP_201_CREATED,
    )
    def create_workspace_member(
        workspace_id: str,
        payload: WorkspaceMemberCreate,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        entity = service.create_workspace_member(
            workspace_id,
            payload.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        return _set_etag(response, entity)

    @router.get("/{workspace_id}/bootstrap")
    def bootstrap(
        workspace_id: str,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        return service.bootstrap(workspace_id)

    @router.get("/{workspace_id}/sync")
    def sync(
        workspace_id: str,
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        return service.sync(workspace_id, after, limit)

    @router.put("/{workspace_id}/sync/cursor")
    def acknowledge_cursor(
        workspace_id: str,
        payload: SyncCursorAck,
        device_id: DeviceId,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        return service.acknowledge_cursor(
            workspace_id, device_id, payload.last_sequence
        )

    @router.get("/{workspace_id}/audit")
    def audit(
        workspace_id: str,
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        return service.sync(workspace_id, after, limit)

    @router.get("/{workspace_id}/memos")
    def list_memos(
        workspace_id: str,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        return service.list_memos(workspace_id)

    @router.post("/{workspace_id}/memos", status_code=status.HTTP_201_CREATED)
    def create_memo(
        workspace_id: str,
        payload: MemoCreate,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        entity = service.create_memo(
            workspace_id,
            payload.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        return _set_etag(response, entity)

    @router.patch("/{workspace_id}/memos/{memo_id}")
    def update_memo(
        workspace_id: str,
        memo_id: str,
        payload: MemoPatch,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        values = payload.model_dump(mode="json", exclude_unset=True)
        values["expected_version"] = _expected_version(if_match)
        entity = service.update_memo(
            workspace_id,
            memo_id,
            values,
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        return _set_etag(response, entity)

    @router.post("/{workspace_id}/memos/{memo_id}/delete")
    def delete_memo(
        workspace_id: str,
        memo_id: str,
        payload: VersionCommand,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        entity = service.delete_memo(
            workspace_id,
            memo_id,
            payload.expected_version,
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        return _set_etag(response, entity)

    @router.post(
        "/{workspace_id}/memos/{memo_id}/task",
        status_code=status.HTTP_201_CREATED,
    )
    def materialize_memo_as_task(
        workspace_id: str,
        memo_id: str,
        payload: MemoTaskMaterializationCreate,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        _require_materialization_path_id(workspace_id, "workspace_id")
        _require_materialization_path_id(memo_id, "memo_id")
        _matching_body_version(if_match, payload.expected_memo_version)
        result = service.materialize_memo_as_task(
            workspace_id,
            memo_id,
            payload.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        _set_etag(response, result["memo"])
        return result

    @router.post(
        "/{workspace_id}/memos/{memo_id}/calendar",
        status_code=status.HTTP_201_CREATED,
    )
    def materialize_memo_as_calendar(
        workspace_id: str,
        memo_id: str,
        payload: MemoCalendarMaterializationCreate,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        _require_materialization_path_id(workspace_id, "workspace_id")
        _require_materialization_path_id(memo_id, "memo_id")
        _matching_body_version(if_match, payload.expected_memo_version)
        result = service.materialize_memo_as_calendar(
            workspace_id,
            memo_id,
            payload.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        _set_etag(response, result["memo"])
        return result

    @router.post(
        "/{workspace_id}/memos/{memo_id}/task/",
        include_in_schema=False,
    )
    def reject_task_materialization_trailing_slash(
        workspace_id: str,
        memo_id: str,
        request: Request,
        _owner: Any = owner,
    ) -> None:
        _reject_query(request)
        raise PocketError(400, "备忘物化接口不接受尾随斜线")

    @router.post(
        "/{workspace_id}/memos/{memo_id}/calendar/",
        include_in_schema=False,
    )
    def reject_calendar_materialization_trailing_slash(
        workspace_id: str,
        memo_id: str,
        request: Request,
        _owner: Any = owner,
    ) -> None:
        _reject_query(request)
        raise PocketError(400, "备忘物化接口不接受尾随斜线")

    @router.get("/{workspace_id}/tasks")
    def list_tasks(
        workspace_id: str,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        return service.list_tasks(workspace_id)

    @router.get("/{workspace_id}/tasks/{task_id}/agreement")
    def task_agreement_by_task(
        workspace_id: str,
        task_id: str,
        request: Request,
        response: Response,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        entity = service.task_agreement_by_task(workspace_id, task_id)
        return _set_etag(response, entity)

    @router.post(
        "/{workspace_id}/tasks/{task_id}/check-ins",
        status_code=status.HTTP_201_CREATED,
    )
    def create_task_checkin(
        workspace_id: str,
        task_id: str,
        payload: TaskCheckInCreate,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        _matching_body_version(if_match, payload.expected_version)
        entity = service.create_task_checkin(
            workspace_id,
            task_id,
            payload.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        return _set_etag(response, entity)

    @router.get("/{workspace_id}/tasks/{task_id}/check-ins")
    def list_task_checkins(
        workspace_id: str,
        task_id: str,
        request: Request,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        return service.list_task_checkins(workspace_id, task_id)

    @router.get("/{workspace_id}/task-attention")
    def task_attention(
        workspace_id: str,
        request: Request,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        return service.task_attention(workspace_id)

    @router.get("/{workspace_id}/task-analysis")
    def task_analysis(
        workspace_id: str,
        request: Request,
        from_date: Annotated[date, Query(alias="from")],
        to_date: Annotated[date, Query(alias="to")],
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        query_items = list(request.query_params.multi_items())
        if len(query_items) != 2 or {key for key, _value in query_items} != {
            "from",
            "to",
        }:
            raise PocketError(400, "任务分析只接受单一 from 和 to 日期")
        return service.task_analysis(workspace_id, from_date, to_date)

    @router.post("/{workspace_id}/tasks", status_code=status.HTTP_201_CREATED)
    def create_task(
        workspace_id: str,
        payload: BusinessTaskCreate,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        entity = service.create_task(
            workspace_id,
            payload.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        return _set_etag(response, entity)

    @router.patch("/{workspace_id}/tasks/{task_id}")
    def update_task(
        workspace_id: str,
        task_id: str,
        payload: TaskPatch,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        entity = service.update_task(
            workspace_id,
            task_id,
            _expected_version(if_match),
            payload.model_dump(mode="json", exclude_unset=True),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        return _set_etag(response, entity)

    @router.post("/{workspace_id}/tasks/{task_id}/transitions")
    def transition_task(
        workspace_id: str,
        task_id: str,
        payload: TaskTransition,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        entity = service.transition_task(
            workspace_id,
            task_id,
            payload.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        return _set_etag(response, entity)

    @router.post(
        "/{workspace_id}/tasks/{task_id}/alignment-invitations",
        status_code=status.HTTP_201_CREATED,
    )
    def create_task_alignment_invitation(
        workspace_id: str,
        task_id: str,
        payload: TaskAlignmentInvitationCreate,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        result = service.create_task_alignment_invitation(
            workspace_id,
            task_id,
            payload.expected_version,
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        for key, value in _alignment_security_headers().items():
            response.headers[key] = value
        return result

    @router.post(
        "/{workspace_id}/tasks/{task_id}/execution-invitations",
        status_code=status.HTTP_201_CREATED,
    )
    def create_task_execution_invitation(
        workspace_id: str,
        task_id: str,
        payload: TaskExecutionInvitationCreate,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        if not task_execution_public_enabled:
            raise PocketError(503, "任务执行公开工作台尚未启用")
        _require_materialization_path_id(workspace_id, "workspace_id")
        _require_materialization_path_id(task_id, "task_id")
        _matching_strong_body_version(if_match, payload.expected_task_version)
        result = service.create_task_execution_invitation(
            workspace_id,
            task_id,
            payload.expected_task_version,
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        for key, value in _alignment_security_headers().items():
            response.headers[key] = value
        response.headers["ETag"] = f'"{payload.expected_task_version}"'
        return result

    @router.post(
        "/{workspace_id}/tasks/{task_id}/steps",
        status_code=status.HTTP_201_CREATED,
    )
    def append_task_step(
        workspace_id: str,
        task_id: str,
        payload: TaskStepAppend,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        _matching_body_version(if_match, payload.expected_version)
        entity = service.append_task_step(
            workspace_id,
            task_id,
            payload.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        return _set_etag(response, entity)

    # This static route must be registered before the dynamic step_id route.
    @router.post("/{workspace_id}/tasks/{task_id}/steps/reorder")
    def reorder_task_steps(
        workspace_id: str,
        task_id: str,
        payload: TaskStepsReorder,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        _matching_body_version(if_match, payload.expected_version)
        entity = service.reorder_task_steps(
            workspace_id,
            task_id,
            payload.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        return _set_etag(response, entity)

    @router.patch("/{workspace_id}/tasks/{task_id}/steps/{step_id}")
    def patch_task_step(
        workspace_id: str,
        task_id: str,
        step_id: str,
        payload: TaskStepPatch,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        entity = service.patch_task_step(
            workspace_id,
            task_id,
            step_id,
            _expected_version(if_match),
            payload.model_dump(mode="json", exclude_unset=True),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        return _set_etag(response, entity)

    @router.put("/{workspace_id}/tasks/{task_id}/steps/{step_id}/schedule")
    def upsert_task_step_schedule(
        workspace_id: str,
        task_id: str,
        step_id: str,
        payload: TaskStepScheduleUpsert,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        _matching_body_version(if_match, payload.expected_version)
        result = service.upsert_task_step_schedule(
            workspace_id,
            task_id,
            step_id,
            payload.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        _set_etag(response, result["task"])
        return result

    @router.post("/{workspace_id}/tasks/{task_id}/steps/{step_id}/schedule/status")
    def set_task_step_schedule_status(
        workspace_id: str,
        task_id: str,
        step_id: str,
        payload: TaskStepScheduleStatus,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        _matching_body_version(if_match, payload.expected_version)
        result = service.set_task_step_schedule_status(
            workspace_id,
            task_id,
            step_id,
            payload.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        _set_etag(response, result["task"])
        return result

    @router.post("/{workspace_id}/tasks/{task_id}/steps/{step_id}")
    def set_task_step(
        workspace_id: str,
        task_id: str,
        step_id: str,
        payload: TaskStepSet,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        _matching_body_version(if_match, payload.expected_version)
        entity = service.set_task_step(
            workspace_id,
            task_id,
            step_id,
            payload.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        return _set_etag(response, entity)

    @router.post(
        "/{workspace_id}/tasks/{task_id}/changes",
        status_code=status.HTTP_201_CREATED,
    )
    def create_task_change(
        workspace_id: str,
        task_id: str,
        payload: TaskChangeCreate,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        _require_materialization_path_id(workspace_id, "workspace_id")
        _require_materialization_path_id(task_id, "task_id")
        entity = service.create_task_change(
            workspace_id,
            task_id,
            payload.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        return _set_etag(response, entity)

    @router.post(
        "/{workspace_id}/changes/{change_id}/invitations",
        status_code=status.HTTP_201_CREATED,
    )
    def create_task_change_invitation(
        workspace_id: str,
        change_id: str,
        payload: TaskChangeInvitationCreate,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        _require_materialization_path_id(workspace_id, "workspace_id")
        _require_materialization_path_id(change_id, "change_id")
        _matching_body_version(if_match, payload.expected_change_version)
        result = service.create_task_change_invitation(
            workspace_id,
            change_id,
            payload.expected_change_version,
            payload.expected_task_version,
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        for key, value in _alignment_security_headers().items():
            response.headers[key] = value
        response.headers["ETag"] = f'"{payload.expected_change_version}"'
        return result

    @router.post("/{workspace_id}/changes/{change_id}/decision")
    def decide_task_change(
        workspace_id: str,
        change_id: str,
        payload: TaskChangeDecision,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        _require_materialization_path_id(workspace_id, "workspace_id")
        _require_materialization_path_id(change_id, "change_id")
        result = service.decide_task_change(
            workspace_id,
            change_id,
            payload.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            device_id=device_id,
            assurance_method=(
                "owner_device_session"
                if getattr(request.state, "secretary_access_kind", None) == "device"
                else "owner_token"
            ),
        )
        _set_etag(response, result["change"])
        return result

    @router.get("/{workspace_id}/calendar")
    def list_calendar(
        workspace_id: str,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        return service.list_calendar(workspace_id)

    @router.post("/{workspace_id}/calendar", status_code=status.HTTP_201_CREATED)
    def create_calendar_entry(
        workspace_id: str,
        payload: CalendarEntryCreate,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        entity = service.create_calendar_entry(
            workspace_id,
            payload.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        return _set_etag(response, entity)

    @router.patch("/{workspace_id}/calendar/{entry_id}")
    def update_calendar_entry(
        workspace_id: str,
        entry_id: str,
        payload: CalendarEntryPatch,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        entity = service.update_calendar_entry(
            workspace_id,
            entry_id,
            _expected_version(if_match),
            payload.model_dump(mode="json", exclude_unset=True),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        return _set_etag(response, entity)

    @router.post("/{workspace_id}/calendar/{entry_id}/cancel")
    def cancel_calendar_entry(
        workspace_id: str,
        entry_id: str,
        payload: VersionCommand,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        entity = service.update_calendar_entry(
            workspace_id,
            entry_id,
            payload.expected_version,
            {"status": "canceled"},
            idempotency_key=idempotency_key,
            device_id=device_id,
            event_type="calendar.canceled",
        )
        return _set_etag(response, entity)

    @router.get("/{workspace_id}/meetings")
    def list_meetings(
        workspace_id: str,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        return service.list_meetings(workspace_id)

    @router.post("/{workspace_id}/meetings", status_code=status.HTTP_201_CREATED)
    def create_meeting(
        workspace_id: str,
        payload: MeetingCreate,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        entity = service.create_meeting(
            workspace_id,
            payload.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        return _set_etag(response, entity)

    @router.patch("/{workspace_id}/meetings/{meeting_id}")
    def update_meeting(
        workspace_id: str,
        meeting_id: str,
        payload: MeetingPatch,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        entity = service.update_meeting(
            workspace_id,
            meeting_id,
            _expected_version(if_match),
            payload.model_dump(mode="json", exclude_unset=True),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        return _set_etag(response, entity)

    @router.post(
        "/{workspace_id}/meetings/{meeting_id}/minutes",
        status_code=status.HTTP_201_CREATED,
    )
    def create_meeting_minutes(
        workspace_id: str,
        meeting_id: str,
        payload: MeetingMinutesCreate,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        entity = service.create_meeting_minutes(
            workspace_id,
            meeting_id,
            _expected_version(if_match),
            payload.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        return _set_etag(response, entity)

    @router.post("/{workspace_id}/minutes/{minutes_id}/decision")
    def decide_meeting_minutes(
        workspace_id: str,
        minutes_id: str,
        payload: MeetingMinutesDecision,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        entity = service.decide_meeting_minutes(
            workspace_id,
            minutes_id,
            payload.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        return _set_etag(response, entity)

    @router.get("/{workspace_id}/documents")
    def list_documents(
        workspace_id: str,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        return service.list_documents(workspace_id)

    @router.post("/{workspace_id}/documents", status_code=status.HTTP_201_CREATED)
    def create_document(
        workspace_id: str,
        payload: DocumentCreate,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        entity = service.create_document(
            workspace_id,
            payload.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        return _set_etag(response, entity)

    @router.get("/{workspace_id}/documents/{document_id}")
    def get_document(
        workspace_id: str,
        document_id: str,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        return service.get_document(workspace_id, document_id)

    @router.patch("/{workspace_id}/documents/{document_id}")
    def update_document(
        workspace_id: str,
        document_id: str,
        payload: DocumentPatch,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        entity = service.update_document(
            workspace_id,
            document_id,
            _expected_version(if_match),
            payload.model_dump(mode="json", exclude_unset=True),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        return _set_etag(response, entity)

    @router.post(
        "/{workspace_id}/documents/{document_id}/reviews",
        status_code=status.HTTP_201_CREATED,
    )
    def review_document(
        workspace_id: str,
        document_id: str,
        payload: DocumentReviewCreate,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        entity = service.review_document(
            workspace_id,
            document_id,
            payload.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        return _set_etag(response, entity)

    @router.post(
        "/{workspace_id}/documents/{template_id}/generate",
        status_code=status.HTTP_201_CREATED,
    )
    def generate_document(
        workspace_id: str,
        template_id: str,
        payload: DocumentGenerate,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        entity = service.generate_document(
            workspace_id,
            template_id,
            payload.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        return _set_etag(response, entity)

    @router.post("/{workspace_id}/documents/{document_id}/archive")
    def archive_document(
        workspace_id: str,
        document_id: str,
        payload: VersionCommand,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        entity = service.archive_document(
            workspace_id,
            document_id,
            payload.expected_version,
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        return _set_etag(response, entity)

    @router.post(
        "/{workspace_id}/documents/{document_id}/excerpts",
        status_code=status.HTTP_201_CREATED,
    )
    def create_document_excerpt(
        workspace_id: str,
        document_id: str,
        payload: DocumentExcerptCreate,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        entity = service.create_document_excerpt(
            workspace_id,
            document_id,
            payload.model_dump(mode="json"),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        return _set_etag(response, entity)

    @router.get("/{workspace_id}/documents/{document_id}/excerpts")
    def list_document_excerpts(
        workspace_id: str,
        document_id: str,
        viewer_member_id: Annotated[str, Query(min_length=1, max_length=200)] = (
            DEFAULT_OWNER_ID
        ),
        _owner: Any = owner,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        return service.list_document_excerpts(
            workspace_id, document_id, viewer_member_id
        )

    return router


def _alignment_security_headers(nonce: str | None = None) -> dict[str, str]:
    headers = {
        "Cache-Control": "no-store, max-age=0",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    }
    if nonce is not None:
        headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            f"style-src 'nonce-{nonce}'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
    return headers


def _alignment_html(
    *,
    title: str,
    content: str,
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
    main {{ box-sizing: border-box; max-width: 720px; min-height: 100vh;
            margin: 0 auto; padding: 28px 18px 48px; }}
    section {{ background: white; border: 1px solid #dfe3e8; border-radius: 16px;
               padding: 22px; box-shadow: 0 8px 28px rgba(24,33,47,.08); }}
    h1 {{ margin: 0 0 12px; font-size: 1.55rem; }}
    h2 {{ margin: 24px 0 8px; font-size: 1.05rem; }}
    p, li, dd {{ line-height: 1.65; overflow-wrap: anywhere; }}
    .muted {{ color: #667085; }}
    .content {{ white-space: pre-wrap; }}
    label {{ display: block; margin: 18px 0 8px; font-weight: 650; }}
    input, textarea {{ box-sizing: border-box; width: 100%; padding: 13px 14px;
             border: 1px solid #98a2b3; border-radius: 10px; font: inherit;
             }}
    input[autocomplete="one-time-code"] {{ letter-spacing: .08em;
             text-transform: uppercase; }}
    textarea {{ min-height: 110px; resize: vertical; }}
    button {{ width: 100%; margin-top: 18px; padding: 13px 16px; border: 0;
              border-radius: 10px; background: #2349d8; color: white;
              font: inherit; font-weight: 700; }}
    dl {{ margin: 0; }} dt {{ margin-top: 16px; font-weight: 700; }}
    dd {{ margin: 4px 0 0; }} ul {{ padding-left: 22px; }}
    .warning {{ padding: 10px 12px; border-radius: 10px; background: #fff5df; }}
  </style>
</head>
<body><main><section>{content}</section></main></body>
</html>"""
    return HTMLResponse(
        document,
        status_code=status_code,
        headers=_alignment_security_headers(nonce),
    )


def _alignment_error_json(error: PocketError) -> JSONResponse:
    return JSONResponse(
        {"detail": error.detail},
        status_code=error.status_code,
        headers=_alignment_security_headers(),
    )


def _reject_owner_context(request: Request) -> None:
    if request.headers.get("Authorization") or request.headers.get("X-Owner-Token"):
        raise PocketError(403, "承办人对齐端点不接受 Owner 凭据")


async def _form_field(request: Request, field: str) -> str:
    content_type = request.headers.get("Content-Type", "").partition(";")[0].strip()
    if content_type != "application/x-www-form-urlencoded":
        raise PocketError(422, "请求格式无效")
    content_length = request.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > 1_024:
                raise PocketError(413, "请求内容过大")
        except ValueError as error:
            raise PocketError(422, "请求格式无效") from error
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 1_024:
            raise PocketError(413, "请求内容过大")
        chunks.append(chunk)
    try:
        values = parse_qs(
            b"".join(chunks).decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=2,
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise PocketError(422, "请求格式无效") from error
    if set(values) != {field} or len(values[field]) != 1:
        raise PocketError(422, "请求格式无效")
    return values[field][0]


async def _form_fields(
    request: Request,
    expected_fields: frozenset[str],
    *,
    max_bytes: int = 16 * 1024,
) -> dict[str, str]:
    content_type = request.headers.get("Content-Type", "").partition(";")[0].strip()
    if content_type != "application/x-www-form-urlencoded":
        raise PocketError(422, "请求格式无效")
    content_length = request.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                raise PocketError(413, "请求内容过大")
        except ValueError as error:
            raise PocketError(422, "请求格式无效") from error
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            raise PocketError(413, "请求内容过大")
        chunks.append(chunk)
    try:
        values = parse_qs(
            b"".join(chunks).decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=len(expected_fields),
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise PocketError(422, "请求格式无效") from error
    if set(values) != expected_fields or any(len(items) != 1 for items in values.values()):
        raise PocketError(422, "请求格式无效")
    return {key: items[0] for key, items in values.items()}


def _alignment_list(values: list[Any]) -> str:
    if not values:
        return '<p class="muted">未填写</p>'
    return (
        "<ul>"
        + "".join(f"<li>{html.escape(str(value))}</li>" for value in values)
        + "</ul>"
    )


def _task_change_value(value: Any) -> str:
    if value is None:
        return '<p class="muted">未设定</p>'
    if isinstance(value, list):
        return _alignment_list(value)
    return f'<p class="content">{html.escape(str(value))}</p>'


def _hidden_field(name: str, value: Any) -> str:
    return (
        f'<input type="hidden" name="{html.escape(name, quote=True)}" '
        f'value="{html.escape(str(value), quote=True)}">'
    )


def _task_change_preview_content(
    invitation_id: str,
    exchange: dict[str, Any],
) -> str:
    change = exchange["change"]
    proposal = change["proposal"]
    document = proposal["document"]
    patch_value = next(iter(document["patch"].values()))
    type_label = {
        "assignee": "更换承办人",
        "due_at": "变更完成期限",
        "acceptance_criteria": "变更验收标准",
        "abnormal_close": "非正常关闭",
    }.get(change["change_type"], change["change_type"])
    action = (
        f"/api/v1/task-change-invitations/"
        f"{quote(invitation_id, safe='')}/decide"
    )
    shared_fields = "".join(
        (
            _hidden_field("access_token", exchange["access_token"]),
            _hidden_field(
                "client_device_id", exchange["session"]["client_device_id"]
            ),
            _hidden_field("change_id", change["id"]),
            _hidden_field("change_version", change["version"]),
            _hidden_field("task_version", change["task"]["version"]),
            _hidden_field("proposal_digest", proposal["digest"]),
        )
    )
    accept_mutation = f"browser-accept-{secrets.token_hex(12)}"
    reject_mutation = f"browser-reject-{secrets.token_hex(12)}"
    return f"""
<h1>请核对任务变更提案</h1>
<p class="warning">请逐项核对。任务或成员绑定一旦变化，本次提交会被拒绝。</p>
<p class="warning">本流程证明的是双渠道凭据持有能力，不证明自然人或企业实名身份，也不构成电子签名。</p>
<dl>
  <dt>任务</dt><dd>{html.escape(change["task"]["title"])}</dd>
  <dt>变更类型</dt><dd>{html.escape(type_label)}</dd>
  <dt>当前值</dt><dd>{_task_change_value(document["before"])}</dd>
  <dt>提议值</dt><dd>{_task_change_value(patch_value)}</dd>
  <dt>变更原因</dt><dd class="content">{html.escape(document["reason"])}</dd>
  <dt>提案摘要</dt><dd>{html.escape(proposal["digest"])}</dd>
</dl>
<p class="muted">本确认会话截止：{html.escape(exchange["expires_at"])}</p>
<form method="post" action="{html.escape(action, quote=True)}">
  {shared_fields}
  {_hidden_field("decision", "accept")}
  {_hidden_field("reason", "")}
  {_hidden_field("client_mutation_id", accept_mutation)}
  {_hidden_field("idempotency_key", accept_mutation)}
  <button type="submit">接受这项变更</button>
</form>
<form method="post" action="{html.escape(action, quote=True)}">
  {shared_fields}
  {_hidden_field("decision", "reject")}
  {_hidden_field("client_mutation_id", reject_mutation)}
  {_hidden_field("idempotency_key", reject_mutation)}
  <label for="reason">拒绝原因</label>
  <textarea id="reason" name="reason" maxlength="4000" required></textarea>
  <button type="submit">拒绝这项变更</button>
</form>
"""


def _alignment_preview_content(invitation_id: str, preview: dict[str, Any]) -> str:
    alignment = preview["alignment"]
    action = f"/api/v1/task-alignments/{quote(invitation_id, safe='')}/confirm"
    token = html.escape(preview["confirmation_token"], quote=True)
    return f"""
<h1>请核对完整任务对齐包</h1>
<p class="warning">确认码已一次性使用。请核对下列内容；任务若已变更，本次提交会被拒绝。</p>
<p class="warning">提交仅证明本次两段凭据的持有能力。系统会将结果映射到邀请中指定的承办人记录，但不证明自然人或企业身份，也不构成电子签名。</p>
<dl>
  <dt>任务</dt><dd>{html.escape(alignment["title"])}</dd>
  <dt>承办人</dt><dd>{html.escape(alignment["assignee_label"])}</dd>
  <dt>目的（价值）</dt><dd class="content">{html.escape(alignment["purpose"])}</dd>
  <dt>目标</dt><dd class="content">{html.escape(alignment["objective"])}</dd>
  <dt>完成策略</dt><dd class="content">{html.escape(alignment["strategy"])}</dd>
  <dt>完成期限</dt><dd>{html.escape(alignment["due_at"] or "未设定")}</dd>
</dl>
<h2>关键点</h2>{_alignment_list(alignment["key_points"])}
<h2>验收标准</h2>{_alignment_list(alignment["acceptance_criteria"])}
<p class="muted">本确认会话截止：{html.escape(preview["confirmation_expires_at"])}</p>
<form method="post" action="{html.escape(action, quote=True)}">
  <input type="hidden" name="confirmation_token" value="{token}">
  <button type="submit">以本次凭据持有者身份确认</button>
</form>
"""


def create_task_alignment_router() -> APIRouter:
    router = APIRouter(tags=["task-alignment"])

    @router.post("/api/v1/task-alignments/exchange")
    def exchange_task_alignment_json(
        payload: TaskAlignmentExchange,
        request: Request,
        idempotency_key: IdempotencyKey,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> JSONResponse:
        try:
            _reject_query(request)
            _reject_owner_context(request)
            result = service.exchange_task_alignment(
                payload.model_dump(mode="json"),
                idempotency_key=idempotency_key,
            )
        except PocketError as error:
            return _alignment_error_json(error)
        return JSONResponse(result, headers=_alignment_security_headers())

    @router.get(
        "/api/v1/task-alignments/{invitation_id}",
        response_class=HTMLResponse,
    )
    def task_alignment_page(
        invitation_id: str,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> HTMLResponse:
        try:
            shell = service.task_alignment_invitation_shell(invitation_id)
        except PocketError as error:
            return _alignment_html(
                title="任务对齐邀请不可用",
                content=(
                    f"<h1>任务对齐邀请不可用</h1><p>{html.escape(error.detail)}</p>"
                ),
                status_code=error.status_code,
            )
        action = f"/api/v1/task-alignments/{quote(invitation_id, safe='')}/preview"
        return _alignment_html(
            title="任务对齐确认",
            content=f"""
<h1>任务对齐确认</h1>
<p>请输入下达人单独提供的一次性确认码。任务正文只会在确认码验证成功后显示。</p>
<p class="warning">下达人生成邀请时会同时看到邀请链接和确认码；请通过相互独立的渠道接收这两段凭据。</p>
<p class="muted">邀请截止：{html.escape(shell["expires_at"])}</p>
<form method="post" action="{html.escape(action, quote=True)}">
  <label for="code">一次性确认码</label>
  <input id="code" name="code" type="text" maxlength="32" required
         autocomplete="one-time-code" autocapitalize="characters"
         spellcheck="false" inputmode="text">
  <button type="submit">验证并查看对齐内容</button>
</form>
""",
        )

    @router.post("/api/v1/task-alignments/preview")
    def preview_task_alignment_json(
        payload: TaskAlignmentPreview,
        request: Request,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> JSONResponse:
        try:
            _reject_owner_context(request)
            result = service.preview_task_alignment(payload.invitation_id, payload.code)
        except PocketError as error:
            return _alignment_error_json(error)
        return JSONResponse(result, headers=_alignment_security_headers())

    @router.post("/api/v1/task-alignments/confirm")
    def confirm_task_alignment_json(
        payload: TaskAlignmentConfirm,
        request: Request,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> JSONResponse:
        try:
            _reject_owner_context(request)
            result = service.confirm_task_alignment(
                payload.invitation_id, payload.confirmation_token
            )
        except PocketError as error:
            return _alignment_error_json(error)
        return JSONResponse(result, headers=_alignment_security_headers())

    @router.post(
        "/api/v1/task-alignments/{invitation_id}/preview",
        response_class=HTMLResponse,
    )
    async def preview_task_alignment_page(
        invitation_id: str,
        request: Request,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> HTMLResponse:
        try:
            _reject_owner_context(request)
            code = await _form_field(request, "code")
            preview = service.preview_task_alignment(invitation_id, code)
        except PocketError as error:
            return _alignment_html(
                title="无法查看任务对齐内容",
                content=(
                    f"<h1>无法查看任务对齐内容</h1><p>{html.escape(error.detail)}</p>"
                ),
                status_code=error.status_code,
            )
        return _alignment_html(
            title="核对任务对齐内容",
            content=_alignment_preview_content(invitation_id, preview),
        )

    @router.post(
        "/api/v1/task-alignments/{invitation_id}/confirm",
        response_class=HTMLResponse,
    )
    async def confirm_task_alignment_page(
        invitation_id: str,
        request: Request,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> HTMLResponse:
        try:
            _reject_owner_context(request)
            confirmation_token = await _form_field(request, "confirmation_token")
            result = service.confirm_task_alignment(invitation_id, confirmation_token)
        except PocketError as error:
            return _alignment_html(
                title="任务对齐未完成",
                content=(f"<h1>任务对齐未完成</h1><p>{html.escape(error.detail)}</p>"),
                status_code=error.status_code,
            )
        return _alignment_html(
            title="任务已完成对齐",
            content=(
                "<h1>任务已完成对齐</h1>"
                "<p>持有本次双渠道邀请凭据的一方已完成确认；"
                "系统按邀请映射到承办人记录 "
                f"{html.escape(result['assignee_label'])}。</p>"
                '<p class="warning">此结果证明的是凭据持有能力，'
                "不构成实名身份、电子签名或授权代理证明。</p>"
                f'<p class="muted">确认时间：{html.escape(result["confirmed_at"])}</p>'
            ),
        )

    return router


def create_task_agreement_router(
    access_dependency: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/task-agreements", tags=["task-agreement"])
    access = Depends(access_dependency)

    @router.get("/{case_id}")
    def task_agreement(
        case_id: str,
        request: Request,
        response: Response,
        principal: dict[str, Any] = access,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        entity = service.task_agreement(case_id, principal)
        return _set_etag(response, entity)

    @router.post("/{case_id}/responses")
    def respond_task_agreement(
        case_id: str,
        payload: TaskAgreementResponse,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        principal: dict[str, Any] = access,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        _matching_body_version(if_match, payload.expected_agreement_version)
        result = service.respond_task_agreement(
            case_id,
            payload.model_dump(mode="json", by_alias=True),
            principal,
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        response.headers["ETag"] = f'"{result["agreement"]["version"]}"'
        return result

    return router


def create_task_execution_router(
    access_dependency: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/task-executions", tags=["task-execution"])
    access = Depends(access_dependency)

    @router.post("/exchange")
    def exchange_task_execution(
        payload: TaskExecutionExchange,
        request: Request,
        idempotency_key: IdempotencyKey,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> JSONResponse:
        try:
            _reject_query(request)
            _reject_owner_context(request)
            result = service.exchange_task_execution(
                payload.model_dump(mode="json"),
                idempotency_key=idempotency_key,
            )
        except PocketError as error:
            return _alignment_error_json(error)
        return JSONResponse(result, headers=_alignment_security_headers())

    @router.post("/refresh")
    def refresh_task_execution(
        payload: TaskExecutionRefresh,
        request: Request,
        idempotency_key: IdempotencyKey,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> JSONResponse:
        try:
            _reject_query(request)
            _reject_owner_context(request)
            result, etag = service.refresh_task_execution(
                payload.model_dump(mode="json"),
                idempotency_key=idempotency_key,
            )
        except PocketError as error:
            return _alignment_error_json(error)
        headers = _alignment_security_headers()
        headers["ETag"] = f'"{etag}"'
        return JSONResponse(result, headers=headers)

    @router.get("/{task_id}")
    def task_execution_view(
        task_id: str,
        request: Request,
        response: Response,
        principal: dict[str, Any] = access,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        _require_materialization_path_id(task_id, "task_id")
        projection, etag = service.task_execution_view(task_id, principal)
        response.headers["ETag"] = f'"{etag}"'
        return projection

    @router.post("/{task_id}/start")
    def start_task_execution(
        task_id: str,
        payload: TaskExecutionCommand,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        principal: dict[str, Any] = access,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        _require_materialization_path_id(task_id, "task_id")
        result, etag = service.start_task_execution(
            task_id,
            payload.model_dump(mode="json"),
            principal,
            idempotency_key=idempotency_key,
            device_id=device_id,
            if_match=if_match,
        )
        response.headers["ETag"] = f'"{etag}"'
        return result

    @router.post(
        "/{task_id}/check-ins",
        status_code=status.HTTP_201_CREATED,
    )
    def create_task_execution_checkin(
        task_id: str,
        payload: TaskExecutionCheckInCreate,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        principal: dict[str, Any] = access,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        _require_materialization_path_id(task_id, "task_id")
        result, etag = service.create_task_execution_checkin(
            task_id,
            payload.model_dump(mode="json"),
            principal,
            idempotency_key=idempotency_key,
            device_id=device_id,
            if_match=if_match,
        )
        response.headers["ETag"] = f'"{etag}"'
        return result

    @router.put("/{task_id}/steps/{step_id}/status")
    def set_task_execution_step_status(
        task_id: str,
        step_id: str,
        payload: TaskExecutionStepStatus,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        principal: dict[str, Any] = access,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        _require_materialization_path_id(task_id, "task_id")
        _require_materialization_path_id(step_id, "step_id")
        result, etag = service.set_task_execution_step_status(
            task_id,
            step_id,
            payload.model_dump(mode="json"),
            principal,
            idempotency_key=idempotency_key,
            device_id=device_id,
            if_match=if_match,
        )
        response.headers["ETag"] = f'"{etag}"'
        return result

    @router.post("/{task_id}/submit")
    def submit_task_execution(
        task_id: str,
        payload: TaskExecutionCommand,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        principal: dict[str, Any] = access,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        _require_materialization_path_id(task_id, "task_id")
        result, etag = service.submit_task_execution(
            task_id,
            payload.model_dump(mode="json"),
            principal,
            idempotency_key=idempotency_key,
            device_id=device_id,
            if_match=if_match,
        )
        response.headers["ETag"] = f'"{etag}"'
        return result

    return router


def create_task_change_router(
    access_dependency: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/task-changes", tags=["task-change"])
    access = Depends(access_dependency)

    @router.post("/exchange")
    def exchange_task_change(
        payload: TaskChangeExchange,
        request: Request,
        idempotency_key: IdempotencyKey,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> JSONResponse:
        try:
            _reject_query(request)
            _reject_owner_context(request)
            result = service.exchange_task_change(
                payload.model_dump(mode="json"),
                idempotency_key=idempotency_key,
            )
        except PocketError as error:
            return _alignment_error_json(error)
        return JSONResponse(result, headers=_alignment_security_headers())

    @router.get("/{change_id}")
    def task_change(
        change_id: str,
        request: Request,
        response: Response,
        principal: dict[str, Any] = access,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        _require_materialization_path_id(change_id, "change_id")
        entity = service.task_change_protocol(change_id, principal)
        return _set_etag(response, entity)

    @router.post("/{change_id}/decisions")
    def respond_task_change(
        change_id: str,
        payload: TaskChangeProtocolDecision,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        principal: dict[str, Any] = access,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> dict[str, Any]:
        _reject_query(request)
        _require_materialization_path_id(change_id, "change_id")
        _matching_body_version(if_match, payload.expected_change_version)
        result = service.respond_task_change(
            change_id,
            payload.model_dump(mode="json"),
            principal,
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        response.headers["ETag"] = f'"{result["change"]["version"]}"'
        return result

    return router


def create_task_change_invitation_router() -> APIRouter:
    router = APIRouter(tags=["task-change-invitation"])
    prefix = "/api/v1/task-change-invitations"

    @router.get(f"{prefix}/{{invitation_id}}", response_class=HTMLResponse)
    def task_change_invitation_page(
        invitation_id: str,
        request: Request,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> HTMLResponse:
        try:
            _reject_query(request)
            _require_materialization_path_id(invitation_id, "invitation_id")
            shell = service.task_change_invitation_shell(invitation_id)
        except PocketError as error:
            return _alignment_html(
                title="任务变更邀请不可用",
                content=(
                    "<h1>任务变更邀请不可用</h1>"
                    f"<p>{html.escape(error.detail)}</p>"
                ),
                status_code=error.status_code,
            )
        action = f"{prefix}/{quote(invitation_id, safe='')}/preview"
        client_device_id = f"change-browser-device-{secrets.token_hex(12)}"
        exchange_key = f"change-browser-exchange-{secrets.token_hex(12)}"
        return _alignment_html(
            title="任务变更确认",
            content=f"""
<h1>任务变更确认</h1>
<p>请输入下达人通过另一渠道提供的一次性确认码。在验证成功前，本页不会显示任务或变更内容。</p>
<p class="warning">邀请链接和确认码应通过两个相互独立的渠道接收。</p>
<p class="muted">邀请截止：{html.escape(shell["expires_at"])}</p>
<form method="post" action="{html.escape(action, quote=True)}">
  {_hidden_field("client_device_id", client_device_id)}
  {_hidden_field("exchange_idempotency_key", exchange_key)}
  <label for="code">一次性确认码</label>
  <input id="code" name="code" type="text" maxlength="32" required
         autocomplete="one-time-code" autocapitalize="characters"
         spellcheck="false" inputmode="text">
  <button type="submit">验证并查看变更内容</button>
</form>
""",
        )

    @router.post(
        f"{prefix}/{{invitation_id}}/preview",
        response_class=HTMLResponse,
    )
    async def preview_task_change_invitation(
        invitation_id: str,
        request: Request,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> HTMLResponse:
        try:
            _reject_query(request)
            _reject_owner_context(request)
            _require_materialization_path_id(invitation_id, "invitation_id")
            fields = await _form_fields(
                request,
                frozenset(
                    {"code", "client_device_id", "exchange_idempotency_key"}
                ),
            )
            _require_materialization_path_id(
                fields["client_device_id"], "client_device_id"
            )
            if not 8 <= len(fields["exchange_idempotency_key"]) <= 200:
                raise PocketError(422, "请求格式无效")
            exchange = service.exchange_task_change(
                {
                    "invitation_id": invitation_id,
                    "code": fields["code"],
                    "client_device_id": fields["client_device_id"],
                },
                idempotency_key=fields["exchange_idempotency_key"],
            )
        except PocketError as error:
            return _alignment_html(
                title="无法查看任务变更",
                content=(
                    "<h1>无法查看任务变更</h1>"
                    f"<p>{html.escape(error.detail)}</p>"
                ),
                status_code=error.status_code,
            )
        return _alignment_html(
            title="核对任务变更",
            content=_task_change_preview_content(invitation_id, exchange),
        )

    @router.post(
        f"{prefix}/{{invitation_id}}/decide",
        response_class=HTMLResponse,
    )
    async def decide_task_change_invitation(
        invitation_id: str,
        request: Request,
        service: WorkspaceService = Depends(get_workspace_service),
    ) -> HTMLResponse:
        try:
            _reject_query(request)
            _reject_owner_context(request)
            _require_materialization_path_id(invitation_id, "invitation_id")
            fields = await _form_fields(
                request,
                frozenset(
                    {
                        "access_token",
                        "client_device_id",
                        "change_id",
                        "change_version",
                        "task_version",
                        "proposal_digest",
                        "decision",
                        "reason",
                        "client_mutation_id",
                        "idempotency_key",
                    }
                ),
            )
            for key in ("client_device_id", "change_id", "client_mutation_id"):
                _require_materialization_path_id(fields[key], key)
            if not 8 <= len(fields["idempotency_key"]) <= 200:
                raise PocketError(422, "请求格式无效")
            if (
                fields["decision"] not in {"accept", "reject"}
                or re.fullmatch(r"sha256:[0-9a-f]{64}", fields["proposal_digest"])
                is None
                or not fields["change_version"].isdigit()
                or not fields["task_version"].isdigit()
            ):
                raise PocketError(422, "请求格式无效")
            change_version = int(fields["change_version"])
            task_version = int(fields["task_version"])
            if change_version < 1 or task_version < 1:
                raise PocketError(422, "请求格式无效")
            reason = fields["reason"].strip() or None
            if (
                (fields["decision"] == "accept" and reason is not None)
                or (fields["decision"] == "reject" and reason is None)
                or (reason is not None and len(reason) > 4_000)
            ):
                raise PocketError(422, "请求格式无效")
            principal = service.authenticate_task_change_session(
                fields["access_token"],
                requested_device_id=fields["client_device_id"],
                allow_closed_replay=True,
            )
            if (
                principal.get("invitation_id") != invitation_id
                or principal.get("change_id") != fields["change_id"]
            ):
                raise PocketError(404, "任务变更不存在")
            result = service.respond_task_change(
                fields["change_id"],
                {
                    "expected_change_version": change_version,
                    "expected_task_version": task_version,
                    "proposal_digest": fields["proposal_digest"],
                    "decision": fields["decision"],
                    "reason": reason,
                    "client_mutation_id": fields["client_mutation_id"],
                },
                principal,
                idempotency_key=fields["idempotency_key"],
                device_id=fields["client_device_id"],
            )
        except PocketError as error:
            return _alignment_html(
                title="任务变更未完成",
                content=(
                    "<h1>任务变更未完成</h1>"
                    f"<p>{html.escape(error.detail)}</p>"
                ),
                status_code=error.status_code,
            )
        outcome = "已接受" if fields["decision"] == "accept" else "已拒绝"
        return _alignment_html(
            title=f"任务变更{outcome}",
            content=(
                f"<h1>任务变更{outcome}</h1>"
                "<p>系统已记录本次决定并关闭作用域会话。</p>"
                '<p class="warning">本结果证明凭据持有能力，'
                "不构成实名身份、电子签名或授权代理证明。</p>"
                f'<p class="muted">记录时间：{html.escape(result["decision"]["created_at"])}</p>'
            ),
        )

    return router
