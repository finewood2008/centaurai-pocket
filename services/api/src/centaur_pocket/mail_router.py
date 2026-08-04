from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from .mail_schemas import (
    DeviceAuthorizationCreate,
    DraftPatch,
    ReplyDraftCreate,
    SendIntentConfirm,
    VersionCommand,
)
from .outlook_mail import OutlookMailService
from .service import PocketError

IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=200),
]
DeviceId = Annotated[
    str,
    Header(alias="X-Device-ID", min_length=1, max_length=200),
]
IfMatch = Annotated[str | None, Header(alias="If-Match")]


def get_mail_service(request: Request) -> OutlookMailService:
    return request.app.state.mail_service


def _expected_version(if_match: str | None, payload_version: int) -> int:
    if if_match is None:
        raise PocketError(428, "修改邮件资源必须提供 If-Match 版本")
    value = if_match.strip()
    if value.startswith("W/"):
        value = value[2:].strip()
    try:
        parsed = int(value.strip('"'))
    except ValueError as error:
        raise PocketError(400, "If-Match 必须是资源版本号") from error
    if parsed != payload_version:
        raise PocketError(400, "If-Match 必须与 expected_version 一致")
    return parsed


def _etag(response: Response, entity: dict[str, Any]) -> dict[str, Any]:
    version = entity.get("version")
    if isinstance(version, int):
        response.headers["ETag"] = f'"{version}"'
    return entity


def _mail_response_headers(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"


def create_mail_router(owner_dependency: Callable[..., Any]) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/mail",
        tags=["outlook-mail"],
        dependencies=[Depends(_mail_response_headers)],
    )
    owner = Depends(owner_dependency)

    @router.get("/accounts")
    def list_accounts(
        _owner: Any = owner,
        service: OutlookMailService = Depends(get_mail_service),
    ) -> dict[str, Any]:
        return service.list_accounts()

    @router.post(
        "/outlook/device-authorizations",
        status_code=status.HTTP_201_CREATED,
    )
    def create_device_authorization(
        payload: DeviceAuthorizationCreate,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        _owner: Any = owner,
        service: OutlookMailService = Depends(get_mail_service),
    ) -> dict[str, Any]:
        return _etag(
            response,
            service.create_device_authorization(
                payload.account_label,
                idempotency_key=idempotency_key,
                device_id=device_id,
            ),
        )

    @router.get("/outlook/device-authorizations/{authorization_id}")
    def get_device_authorization(
        authorization_id: str,
        response: Response,
        _owner: Any = owner,
        service: OutlookMailService = Depends(get_mail_service),
    ) -> dict[str, Any]:
        return _etag(response, service.get_device_authorization(authorization_id))

    @router.post("/outlook/device-authorizations/{authorization_id}/poll")
    def poll_device_authorization(
        authorization_id: str,
        payload: VersionCommand,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: OutlookMailService = Depends(get_mail_service),
    ) -> dict[str, Any]:
        expected = _expected_version(if_match, payload.expected_version)
        return _etag(
            response,
            service.poll_device_authorization(
                authorization_id,
                expected,
                idempotency_key=idempotency_key,
                device_id=device_id,
            ),
        )

    @router.post("/outlook/device-authorizations/{authorization_id}/cancel")
    def cancel_device_authorization(
        authorization_id: str,
        payload: VersionCommand,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: OutlookMailService = Depends(get_mail_service),
    ) -> dict[str, Any]:
        expected = _expected_version(if_match, payload.expected_version)
        return _etag(
            response,
            service.cancel_device_authorization(
                authorization_id,
                expected,
                idempotency_key=idempotency_key,
                device_id=device_id,
            ),
        )

    @router.post("/accounts/{account_id}/inbox/delta")
    def sync_inbox(
        account_id: str,
        payload: VersionCommand,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: OutlookMailService = Depends(get_mail_service),
    ) -> dict[str, Any]:
        expected = _expected_version(if_match, payload.expected_version)
        result = service.sync_inbox(
            account_id,
            expected,
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        response.headers["ETag"] = f'"{result["account"]["version"]}"'
        return result

    @router.get("/accounts/{account_id}/inbox")
    def list_inbox(
        account_id: str,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        _owner: Any = owner,
        service: OutlookMailService = Depends(get_mail_service),
    ) -> dict[str, Any]:
        return service.list_inbox(account_id, limit=limit, offset=offset)

    @router.get("/messages/{message_id}")
    def get_message(
        message_id: str,
        response: Response,
        _owner: Any = owner,
        service: OutlookMailService = Depends(get_mail_service),
    ) -> dict[str, Any]:
        return _etag(response, service.get_message(message_id))

    @router.get("/messages/{message_id}/body")
    def get_message_body(
        message_id: str,
        _owner: Any = owner,
        service: OutlookMailService = Depends(get_mail_service),
    ) -> dict[str, Any]:
        return service.get_message_body(message_id)

    @router.get("/messages/{message_id}/attachments")
    def list_message_attachments(
        message_id: str,
        _owner: Any = owner,
        service: OutlookMailService = Depends(get_mail_service),
    ) -> dict[str, Any]:
        return service.list_message_attachments(message_id)

    @router.post(
        "/messages/{message_id}/attachments/{attachment_id}/archive",
        status_code=status.HTTP_201_CREATED,
    )
    def archive_attachment(
        message_id: str,
        attachment_id: str,
        payload: VersionCommand,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: OutlookMailService = Depends(get_mail_service),
    ) -> dict[str, Any]:
        return service.archive_attachment(
            message_id,
            attachment_id,
            _expected_version(if_match, payload.expected_version),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )

    @router.get("/accounts/{account_id}/task-candidates")
    def list_task_candidates(
        account_id: str,
        candidate_status: Annotated[
            Literal["pending", "confirmed", "dismissed"], Query(alias="status")
        ] = "pending",
        limit: Annotated[int, Query(ge=1, le=50)] = 50,
        _owner: Any = owner,
        service: OutlookMailService = Depends(get_mail_service),
    ) -> dict[str, Any]:
        return service.list_task_candidates(
            account_id, status=candidate_status, limit=limit
        )

    @router.post("/task-candidates/{candidate_id}/confirm")
    def confirm_task_candidate(
        candidate_id: str,
        payload: VersionCommand,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: OutlookMailService = Depends(get_mail_service),
    ) -> dict[str, Any]:
        result = service.confirm_task_candidate(
            candidate_id,
            _expected_version(if_match, payload.expected_version),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        response.headers["ETag"] = f'"{result["candidate"]["version"]}"'
        return result

    @router.post("/task-candidates/{candidate_id}/dismiss")
    def dismiss_task_candidate(
        candidate_id: str,
        payload: VersionCommand,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: OutlookMailService = Depends(get_mail_service),
    ) -> dict[str, Any]:
        result = service.dismiss_task_candidate(
            candidate_id,
            _expected_version(if_match, payload.expected_version),
            idempotency_key=idempotency_key,
            device_id=device_id,
        )
        response.headers["ETag"] = f'"{result["candidate"]["version"]}"'
        return result

    @router.post(
        "/messages/{message_id}/reply-drafts",
        status_code=status.HTTP_201_CREATED,
    )
    def create_reply_draft(
        message_id: str,
        payload: ReplyDraftCreate,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: OutlookMailService = Depends(get_mail_service),
    ) -> dict[str, Any]:
        return _etag(
            response,
            service.create_reply_draft(
                message_id,
                _expected_version(if_match, payload.expected_version),
                payload.body_text,
                idempotency_key=idempotency_key,
                device_id=device_id,
            ),
        )

    @router.get("/drafts/{draft_id}")
    def get_draft(
        draft_id: str,
        response: Response,
        _owner: Any = owner,
        service: OutlookMailService = Depends(get_mail_service),
    ) -> dict[str, Any]:
        return _etag(response, service.get_draft(draft_id))

    @router.patch("/drafts/{draft_id}")
    def update_draft(
        draft_id: str,
        payload: DraftPatch,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: OutlookMailService = Depends(get_mail_service),
    ) -> dict[str, Any]:
        return _etag(
            response,
            service.update_draft(
                draft_id,
                _expected_version(if_match, payload.expected_version),
                payload.body_text,
                idempotency_key=idempotency_key,
                device_id=device_id,
            ),
        )

    @router.post("/drafts/{draft_id}/prepare")
    def prepare_draft(
        draft_id: str,
        payload: VersionCommand,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: OutlookMailService = Depends(get_mail_service),
    ) -> dict[str, Any]:
        return _etag(
            response,
            service.prepare_draft(
                draft_id,
                _expected_version(if_match, payload.expected_version),
                idempotency_key=idempotency_key,
                device_id=device_id,
            ),
        )

    @router.get("/send-intents/{intent_id}")
    def get_send_intent(
        intent_id: str,
        response: Response,
        _owner: Any = owner,
        service: OutlookMailService = Depends(get_mail_service),
    ) -> dict[str, Any]:
        return _etag(response, service.get_send_intent(intent_id))

    @router.post("/send-intents/{intent_id}/confirm")
    def confirm_send_intent(
        intent_id: str,
        payload: SendIntentConfirm,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: OutlookMailService = Depends(get_mail_service),
    ) -> dict[str, Any]:
        return _etag(
            response,
            service.confirm_send_intent(
                intent_id,
                _expected_version(if_match, payload.expected_version),
                payload.preview_hash,
                idempotency_key=idempotency_key,
                device_id=device_id,
            ),
        )

    @router.post("/send-intents/{intent_id}/reconcile")
    def reconcile_send_intent(
        intent_id: str,
        payload: VersionCommand,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: OutlookMailService = Depends(get_mail_service),
    ) -> dict[str, Any]:
        return _etag(
            response,
            service.reconcile_send_intent(
                intent_id,
                _expected_version(if_match, payload.expected_version),
                idempotency_key=idempotency_key,
                device_id=device_id,
            ),
        )

    @router.post("/accounts/{account_id}/disconnect")
    def disconnect_account(
        account_id: str,
        payload: VersionCommand,
        response: Response,
        idempotency_key: IdempotencyKey,
        device_id: DeviceId,
        if_match: IfMatch = None,
        _owner: Any = owner,
        service: OutlookMailService = Depends(get_mail_service),
    ) -> dict[str, Any]:
        return _etag(
            response,
            service.disconnect_account(
                account_id,
                _expected_version(if_match, payload.expected_version),
                idempotency_key=idempotency_key,
                device_id=device_id,
            ),
        )

    return router
