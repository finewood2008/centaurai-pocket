from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from .reliable_sources_schemas import (
    ReliableCollectionPlanPatch,
    ReliableSourceCandidateConfirm,
    ReliableSourceCandidateCreate,
    ReliableSourceCandidateDismiss,
    ReliableSourceCollect,
)
from .service import PocketError, PocketService

IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=200),
]
DeviceId = Annotated[
    str,
    Header(alias="X-Device-ID", min_length=1, max_length=200),
]
IfMatch = Annotated[str | None, Header(alias="If-Match")]


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


def _etag(response: Response, entity: dict[str, Any], *, nested: str | None = None) -> None:
    target = entity.get(nested, {}) if nested else entity
    version = target.get("version") if isinstance(target, dict) else None
    if isinstance(version, int):
        response.headers["ETag"] = f'"{version}"'


def get_service(request: Request) -> PocketService:
    return request.app.state.service


def create_reliable_sources_router(
    secretary_access_dependency: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["reliable-sources"])
    access = Depends(secretary_access_dependency)

    @router.get("/reliable-sources")
    def list_reliable_sources(
        _access: Any = access,
        service: PocketService = Depends(get_service),
    ) -> dict[str, Any]:
        return service.reliable_sources.list_sources()

    @router.get("/reliable-source-candidates")
    def list_reliable_source_candidates(
        candidate_status: Annotated[
            Literal["pending", "confirmed", "dismissed"],
            Query(alias="status"),
        ] = "pending",
        _access: Any = access,
        service: PocketService = Depends(get_service),
    ) -> dict[str, Any]:
        return service.reliable_sources.list_candidates(candidate_status)

    @router.post(
        "/reliable-source-candidates",
        status_code=status.HTTP_201_CREATED,
    )
    def create_reliable_source_candidate(
        payload: ReliableSourceCandidateCreate,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        _access: Any = access,
        service: PocketService = Depends(get_service),
    ) -> dict[str, Any]:
        entity = service.reliable_sources.create_candidate(
            payload.model_dump(mode="json"),
            actor_id=device_id,
            idempotency_key=idempotency_key,
        )
        _etag(response, entity)
        return entity

    @router.post("/reliable-source-candidates/{candidate_id}/confirm")
    def confirm_reliable_source_candidate(
        candidate_id: str,
        payload: ReliableSourceCandidateConfirm,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _access: Any = access,
        service: PocketService = Depends(get_service),
    ) -> dict[str, Any]:
        entity = service.reliable_sources.confirm_candidate(
            candidate_id,
            payload.model_dump(mode="json"),
            expected_version=_expected_version(if_match),
            actor_id=device_id,
            idempotency_key=idempotency_key,
        )
        _etag(response, entity, nested="candidate")
        return entity

    @router.post("/reliable-source-candidates/{candidate_id}/dismiss")
    def dismiss_reliable_source_candidate(
        candidate_id: str,
        payload: ReliableSourceCandidateDismiss,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _access: Any = access,
        service: PocketService = Depends(get_service),
    ) -> dict[str, Any]:
        entity = service.reliable_sources.dismiss_candidate(
            candidate_id,
            payload.model_dump(mode="json"),
            expected_version=_expected_version(if_match),
            actor_id=device_id,
            idempotency_key=idempotency_key,
        )
        _etag(response, entity)
        return entity

    @router.get("/reliable-sources/{reliable_source_id}/collection-plan")
    def get_reliable_collection_plan(
        reliable_source_id: str,
        response: Response,
        _access: Any = access,
        service: PocketService = Depends(get_service),
    ) -> dict[str, Any]:
        entity = service.reliable_sources.get_plan(reliable_source_id)
        _etag(response, entity)
        return entity

    @router.patch("/reliable-sources/{reliable_source_id}/collection-plan")
    def update_reliable_collection_plan(
        reliable_source_id: str,
        payload: ReliableCollectionPlanPatch,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _access: Any = access,
        service: PocketService = Depends(get_service),
    ) -> dict[str, Any]:
        entity = service.reliable_sources.update_plan(
            reliable_source_id,
            payload.model_dump(mode="json", exclude_unset=True),
            expected_version=_expected_version(if_match),
            actor_id=device_id,
            idempotency_key=idempotency_key,
        )
        _etag(response, entity)
        return entity

    @router.post("/reliable-sources/{reliable_source_id}/collect")
    def collect_reliable_source(
        reliable_source_id: str,
        payload: ReliableSourceCollect,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _access: Any = access,
        service: PocketService = Depends(get_service),
    ) -> dict[str, Any]:
        expected = _expected_version(if_match)
        if expected != payload.expected_version:
            raise PocketError(412, "If-Match 与 expected_version 不一致")
        entity = service.reliable_sources.collect(
            reliable_source_id,
            expected_version=expected,
            actor_id=device_id,
            idempotency_key=idempotency_key,
        )
        _etag(response, entity, nested="source")
        return entity

    @router.get("/reliable-sources/{reliable_source_id}/entries")
    def list_reliable_source_entries(
        reliable_source_id: str,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        _access: Any = access,
        service: PocketService = Depends(get_service),
    ) -> dict[str, Any]:
        return service.reliable_sources.list_entries(
            reliable_source_id,
            limit=limit,
        )

    return router
