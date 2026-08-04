from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)


def _require_timezone(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime 必须包含时区")
    return value


AwareDateTime = Annotated[datetime, AfterValidator(_require_timezone)]


def _require_datetime_text(value: object) -> object:
    if isinstance(value, str):
        return value
    raise ValueError("datetime 必须使用带时区的 ISO 8601 文本")


AgreementAwareDateTime = Annotated[
    datetime,
    BeforeValidator(_require_datetime_text),
    AfterValidator(_require_timezone),
]
Identifier = Annotated[str, Field(min_length=1, max_length=200)]
Title = Annotated[str, Field(min_length=1, max_length=500)]
ShortText = Annotated[str, Field(min_length=1, max_length=2_000)]
LongText = Annotated[str, Field(min_length=1, max_length=200_000)]
ReasonText = Annotated[str, Field(min_length=1, max_length=4_000)]
TimezoneName = Annotated[str, Field(min_length=1, max_length=255)]

Domain = Literal["work", "personal"]
SourceKind = Literal["manual", "im", "email", "meeting", "document", "system"]
SourceAuthority = Literal["authoritative", "observed", "user_provided", "inferred"]
MemoRecordType = Literal["note", "task_candidate"]
MemoHorizon = Literal["short_term", "long_term"]
Urgency = Literal["low", "normal", "high", "critical"]
TaskStage = Literal[
    "draft",
    "issued",
    "aligned",
    "in_progress",
    "submitted",
    "accepted",
    "abnormal_closed",
]
TaskTransitionTarget = Literal[
    "issued", "aligned", "in_progress", "submitted", "accepted"
]
TaskHealth = Literal["on_track", "at_risk", "blocked", "overdue"]
TaskPriority = Literal["low", "normal", "high", "critical"]
TaskStepType = Literal["key_result", "milestone", "action"]
TaskStepStatus = Literal["pending", "in_progress", "blocked", "done", "canceled"]
TaskChangeType = Literal["assignee", "due_at", "acceptance_criteria", "abnormal_close"]
CalendarStatus = Literal["scheduled", "completed", "canceled"]
MeetingStatus = Literal[
    "planned",
    "in_progress",
    "ended",
    "minutes_pending",
    "minutes_confirmed",
    "canceled",
]
ParticipantRole = Literal["organizer", "required", "optional"]
ParticipantRsvp = Literal["pending", "accepted", "declined", "tentative"]
DocumentKind = Literal["general", "contract", "work_report", "template"]
DocumentStatus = Literal["draft", "review_pending", "reviewed", "archived"]
DocumentAccessScope = Literal["owner_only", "workspace", "restricted"]
DocumentReviewType = Literal["contract", "work_report"]
DocumentReviewConclusion = Literal[
    "approved", "approved_with_changes", "changes_required", "rejected"
]
DocumentFindingSeverity = Literal["info", "low", "medium", "high", "critical"]


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class WorkspaceMemberCreate(_StrictInput):
    kind: Literal["person", "team", "external"]
    role: Literal["member", "viewer"]
    display_name: Title
    contact_ref: str | None = Field(default=None, min_length=1, max_length=2_000)
    client_mutation_id: Identifier | None = None


def _validate_timezone_name(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("timezone 必须是有效的 IANA 时区名称") from error
    return value


def _validate_range(start_at: datetime, end_at: datetime, label: str) -> None:
    if end_at <= start_at:
        raise ValueError(f"{label}结束时间必须晚于开始时间")


def _deduplicate_identifiers(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


class SourceReference(_StrictInput):
    source_kind: SourceKind = "manual"
    source_ref: str | None = Field(default=None, min_length=1, max_length=4_000)
    excerpt: str | None = Field(default=None, min_length=1, max_length=20_000)
    authority: SourceAuthority = "user_provided"
    observed_at: AwareDateTime | None = None

    @model_validator(mode="after")
    def require_non_manual_reference(self) -> Self:
        if self.source_kind != "manual" and self.source_ref is None:
            raise ValueError("非手工来源必须提供 source_ref")
        return self


class MemoCreate(_StrictInput):
    record_type: MemoRecordType = "note"
    domain: Domain
    horizon: MemoHorizon = "short_term"
    urgency: Urgency = "normal"
    title: Title
    content: LongText
    due_at: AwareDateTime | None = None
    source: SourceReference = Field(default_factory=SourceReference)
    tags: list[ShortText] = Field(default_factory=list, max_length=100)
    pinned: bool = False
    client_mutation_id: Identifier | None = None


class MemoPatch(_StrictInput):
    title: Title | None = None
    content: LongText | None = None
    domain: Domain | None = None
    horizon: MemoHorizon | None = None
    urgency: Urgency | None = None
    due_at: AwareDateTime | None = None

    @field_validator("title", "content", "domain", "horizon", "urgency")
    @classmethod
    def reject_null_required_values(cls, value: object) -> object:
        if value is None:
            raise ValueError("该字段不能为 null")
        return value

    @model_validator(mode="after")
    def require_patch_field(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("PATCH 至少需要提供一个实际字段")
        return self


class VersionCommand(_StrictInput):
    expected_version: int = Field(ge=1)


class TaskStepCreate(_StrictInput):
    parent_step_id: Identifier | None = None
    step_type: TaskStepType = "action"
    title: Title
    description: str | None = Field(default=None, max_length=20_000)
    assignee_member_id: Identifier | None = None
    status: Literal["pending"] = "pending"
    position: int = Field(default=0, ge=0)
    due_at: AwareDateTime | None = None
    success_metric: dict[str, JsonValue] | None = None
    depends_on_step_ids: list[Identifier] = Field(default_factory=list, max_length=100)

    @field_validator("depends_on_step_ids")
    @classmethod
    def normalize_dependencies(cls, values: list[str]) -> list[str]:
        return _deduplicate_identifiers(values)

    @model_validator(mode="after")
    def reject_parent_dependency(self) -> Self:
        if self.parent_step_id in self.depends_on_step_ids:
            raise ValueError("父步骤不能同时作为直接依赖")
        if self.step_type == "key_result" and not self.success_metric:
            raise ValueError("关键结果步骤必须提供非空 success_metric")
        return self


class BusinessTaskCreate(_StrictInput):
    domain: Domain
    title: Title
    summary: str | None = Field(default=None, max_length=20_000)
    purpose: ShortText
    objective: ShortText
    strategy: LongText
    key_points: list[ShortText] = Field(default_factory=list, max_length=100)
    acceptance_criteria: list[ShortText] = Field(min_length=1, max_length=100)
    issuer_member_id: Identifier
    assignee_member_id: Identifier
    acceptance_owner_id: Identifier
    origin_memo_id: Identifier | None = None
    priority: TaskPriority = "normal"
    health: TaskHealth = "on_track"
    tier: Literal["quick", "standard", "strategic"] = "standard"
    stage: Literal["draft"] = "draft"
    start_at: AwareDateTime | None = None
    due_at: AwareDateTime | None = None
    steps: list[TaskStepCreate] = Field(default_factory=list, max_length=500)
    source: SourceReference | None = None
    client_mutation_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_task_dates(self) -> Self:
        if self.start_at is not None and self.due_at is not None:
            _validate_range(self.start_at, self.due_at, "任务")
        return self


class MemoTaskMaterializationCreate(_StrictInput):
    expected_memo_version: int = Field(ge=1, strict=True)
    title: Title
    purpose: ShortText
    objective: ShortText
    strategy: LongText
    key_points: list[ShortText] = Field(default_factory=list, max_length=100)
    acceptance_criteria: list[ShortText] = Field(min_length=1, max_length=100)
    assignee_member_id: Identifier
    priority: TaskPriority = "normal"
    tier: Literal["quick", "standard", "strategic"] = "standard"
    due_at: AwareDateTime | None = None
    confirm_personal_disclosure: bool = Field(default=False, strict=True)
    client_mutation_id: Identifier


class TaskPatch(_StrictInput):
    domain: Domain | None = None
    title: Title | None = None
    purpose: ShortText | None = None
    objective: ShortText | None = None
    strategy: LongText | None = None
    key_points: list[ShortText] | None = Field(default=None, max_length=100)
    priority: TaskPriority | None = None
    health: TaskHealth | None = None
    start_at: AwareDateTime | None = None

    @field_validator(
        "domain", "title", "purpose", "objective", "strategy", "priority", "health"
    )
    @classmethod
    def reject_null_required_values(cls, value: object) -> object:
        if value is None:
            raise ValueError("该字段不能为 null")
        return value

    @model_validator(mode="after")
    def require_patch_field(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("PATCH 至少需要提供一个实际字段")
        return self


class TaskTransition(VersionCommand):
    target_stage: TaskTransitionTarget
    note: str | None = Field(default=None, max_length=4_000)


class TaskAlignmentInvitationCreate(VersionCommand):
    pass


class TaskAlignmentPreview(_StrictInput):
    invitation_id: Identifier
    # Authentication failures are handled in the service so rejected secrets
    # are never reflected by validation error details.
    code: str = Field(max_length=128)


class TaskAlignmentConfirm(_StrictInput):
    invitation_id: Identifier
    confirmation_token: str = Field(max_length=256)


class TaskAlignmentExchange(_StrictInput):
    invitation_id: Identifier
    # Keep authentication failures in the service so a rejected secret is not
    # reflected in a validation error payload.
    code: str = Field(max_length=128)
    client_device_id: Identifier


class TaskAgreementDocument(_StrictInput):
    schema_: Literal["centaur.task-agreement.v1"] = Field(alias="schema")
    workspace_id: Identifier
    task_id: Identifier
    agreement_id: Identifier
    revision_no: int = Field(ge=1, strict=True)
    parent_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    proposer_role: Literal["issuer", "assignee"]
    proposer_member_id: Identifier
    responder_role: Literal["issuer", "assignee"]
    responder_member_id: Identifier
    issuer_member_id: Identifier
    assignee_member_id: Identifier
    acceptance_owner_id: Identifier
    domain: Domain
    tier: Literal["quick", "standard", "strategic"]
    priority: TaskPriority
    title: Title
    purpose: ShortText
    objective: ShortText
    strategy: LongText
    key_points: list[ShortText] = Field(max_length=100)
    acceptance_criteria: list[ShortText] = Field(min_length=1, max_length=100)
    due_at: AgreementAwareDateTime | None

    @model_validator(mode="after")
    def validate_roles_and_parent(self) -> Self:
        if self.proposer_role == self.responder_role:
            raise ValueError("提议方和回应方角色必须不同")
        if self.proposer_member_id == self.responder_member_id:
            raise ValueError("提议方和回应方成员必须不同")
        if self.revision_no == 1 and self.parent_digest is not None:
            raise ValueError("首个修订不能包含 parent_digest")
        if self.revision_no > 1 and self.parent_digest is None:
            raise ValueError("后续修订必须包含 parent_digest")
        return self


class TaskAgreementResponse(_StrictInput):
    expected_agreement_version: int = Field(ge=1, strict=True)
    revision_id: Identifier
    expected_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    action: Literal["accept", "reject", "counter"]
    reason: ReasonText | None = None
    counter_document: TaskAgreementDocument | None = None
    client_mutation_id: Identifier

    @model_validator(mode="after")
    def validate_action_payload(self) -> Self:
        if self.reason is not None:
            normalized_reason = unicodedata.normalize(
                "NFC", self.reason.replace("\r\n", "\n").replace("\r", "\n")
            ).strip()
            if not normalized_reason:
                raise ValueError("reason 不能只包含空白")
            self.reason = normalized_reason
        if self.action == "accept" and self.reason is not None:
            raise ValueError("接受协议时 reason 必须为 null")
        if self.action in {"reject", "counter"} and self.reason is None:
            raise ValueError("拒绝或反提案必须提供 reason")
        if self.action == "counter" and self.counter_document is None:
            raise ValueError("反提案必须提供完整 counter_document")
        if self.action != "counter" and self.counter_document is not None:
            raise ValueError("只有反提案可以提供 counter_document")
        return self


class TaskStepSet(VersionCommand):
    status: TaskStepStatus
    note: str | None = Field(default=None, max_length=4_000)


class TaskStepAppend(VersionCommand):
    parent_step_id: Identifier | None = None
    step_type: TaskStepType = "action"
    title: Title
    description: str = Field(default="", max_length=20_000)
    due_at: AwareDateTime | None = None
    success_metric: dict[str, JsonValue] = Field(default_factory=dict)
    depends_on_step_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    client_mutation_id: Identifier | None = None

    @field_validator("depends_on_step_ids")
    @classmethod
    def normalize_dependencies(cls, values: list[str]) -> list[str]:
        return _deduplicate_identifiers(values)

    @model_validator(mode="after")
    def validate_step(self) -> Self:
        if self.parent_step_id in self.depends_on_step_ids:
            raise ValueError("父步骤不能同时作为直接依赖")
        if self.step_type == "key_result" and not self.success_metric:
            raise ValueError("关键结果步骤必须提供非空 success_metric")
        return self


class TaskStepPatch(_StrictInput):
    parent_step_id: Identifier | None = None
    step_type: TaskStepType | None = None
    title: Title | None = None
    description: str | None = Field(default=None, max_length=20_000)
    due_at: AwareDateTime | None = None
    success_metric: dict[str, JsonValue] | None = None
    depends_on_step_ids: list[Identifier] | None = Field(default=None, max_length=100)

    @field_validator("depends_on_step_ids")
    @classmethod
    def normalize_dependencies(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            raise ValueError("depends_on_step_ids 不能为 null，请使用空数组清除")
        return _deduplicate_identifiers(values)

    @field_validator("step_type", "title", "description", "success_metric")
    @classmethod
    def reject_null_required_values(cls, value: object) -> object:
        if value is None:
            raise ValueError("该字段不能为 null")
        return value

    @model_validator(mode="after")
    def validate_patch(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("PATCH 至少需要提供一个实际字段")
        if (
            "parent_step_id" in self.model_fields_set
            and "depends_on_step_ids" in self.model_fields_set
            and self.parent_step_id in (self.depends_on_step_ids or [])
        ):
            raise ValueError("父步骤不能同时作为直接依赖")
        return self


class TaskStepsReorder(VersionCommand):
    step_ids: list[Identifier] = Field(min_length=1, max_length=500)

    @field_validator("step_ids")
    @classmethod
    def reject_duplicate_steps(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("step_ids 不能包含重复步骤")
        return values


class TaskStepScheduleUpsert(VersionCommand):
    title: Title
    description: str = Field(default="", max_length=20_000)
    start_at: AwareDateTime
    end_at: AwareDateTime
    timezone: TimezoneName
    all_day: bool = False
    kind: Literal["focus", "reminder"] = "focus"
    client_mutation_id: Identifier | None = None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        return _validate_timezone_name(value)

    @model_validator(mode="after")
    def validate_schedule(self) -> Self:
        _validate_range(self.start_at, self.end_at, "日程")
        return self


class TaskStepScheduleStatus(VersionCommand):
    target_status: Literal["completed", "canceled"]


class TaskCheckInCreate(VersionCommand):
    summary: ReasonText
    reported_progress: int = Field(ge=0, le=100)
    risks: list[ShortText] = Field(default_factory=list, max_length=50)
    blockers: list[ShortText] = Field(default_factory=list, max_length=50)
    next_actions: list[ShortText] = Field(default_factory=list, max_length=50)
    forecast_at: AwareDateTime | None = None
    client_mutation_id: Identifier | None = None


class _AssigneeChangePatch(_StrictInput):
    assignee_member_id: Identifier


class _DueAtChangePatch(_StrictInput):
    due_at: AwareDateTime


class _AcceptanceCriteriaChangePatch(_StrictInput):
    acceptance_criteria: list[ShortText] = Field(min_length=1, max_length=100)


class _AbnormalCloseChangePatch(_StrictInput):
    abnormal_close_reason: ReasonText


TaskChangePatch = (
    _AssigneeChangePatch
    | _DueAtChangePatch
    | _AcceptanceCriteriaChangePatch
    | _AbnormalCloseChangePatch
)


class TaskChangeCreate(_StrictInput):
    change_type: TaskChangeType
    base_version: int = Field(ge=1, strict=True)
    reason: ReasonText
    patch: TaskChangePatch
    client_mutation_id: Identifier | None = None

    @model_validator(mode="after")
    def require_matching_patch(self) -> Self:
        actual_type = {
            _AssigneeChangePatch: "assignee",
            _DueAtChangePatch: "due_at",
            _AcceptanceCriteriaChangePatch: "acceptance_criteria",
            _AbnormalCloseChangePatch: "abnormal_close",
        }.get(type(self.patch))
        if actual_type != self.change_type:
            raise ValueError("patch 与 change_type 不匹配")
        return self


class TaskChangeDecision(VersionCommand):
    decision: Literal["accept", "reject", "cancel"]
    reason: str | None = Field(default=None, max_length=4_000)

    @model_validator(mode="after")
    def require_negative_decision_reason(self) -> Self:
        if self.decision in {"reject", "cancel"} and not self.reason:
            raise ValueError("拒绝或取消变更必须提供 reason")
        return self


class TaskChangeInvitationCreate(_StrictInput):
    expected_change_version: int = Field(ge=1, strict=True)
    expected_task_version: int = Field(ge=1, strict=True)


class TaskChangeExchange(_StrictInput):
    invitation_id: Identifier
    code: str = Field(max_length=128)
    client_device_id: Identifier


class TaskChangeProtocolDecision(_StrictInput):
    expected_change_version: int = Field(ge=1, strict=True)
    expected_task_version: int = Field(ge=1, strict=True)
    proposal_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    decision: Literal["accept", "reject"]
    reason: ReasonText | None = None
    client_mutation_id: Identifier

    @model_validator(mode="after")
    def validate_decision_payload(self) -> Self:
        if self.reason is not None:
            normalized_reason = unicodedata.normalize(
                "NFC", self.reason.replace("\r\n", "\n").replace("\r", "\n")
            ).strip()
            if not normalized_reason:
                raise ValueError("reason 不能只包含空白")
            self.reason = normalized_reason
        if self.decision == "accept" and self.reason is not None:
            raise ValueError("接受任务变更时 reason 必须为 null")
        if self.decision == "reject" and self.reason is None:
            raise ValueError("拒绝任务变更必须提供 reason")
        return self


class TaskExecutionInvitationCreate(_StrictInput):
    expected_task_version: int = Field(ge=1, strict=True)


class TaskExecutionExchange(_StrictInput):
    invitation_id: Identifier
    code: str = Field(max_length=128)
    client_device_id: Identifier


class TaskExecutionRefresh(_StrictInput):
    refresh_token: str = Field(min_length=32, max_length=256)
    client_device_id: Identifier


class TaskExecutionCommand(_StrictInput):
    expected_task_version: int = Field(ge=1, strict=True)
    client_mutation_id: Identifier
    note: str | None = Field(default=None, max_length=4_000)


class TaskExecutionCheckInCreate(_StrictInput):
    expected_task_version: int = Field(ge=1, strict=True)
    summary: ReasonText
    reported_progress: int = Field(ge=0, le=100)
    risks: list[ShortText] = Field(default_factory=list, max_length=50)
    blockers: list[ShortText] = Field(default_factory=list, max_length=50)
    next_actions: list[ShortText] = Field(default_factory=list, max_length=50)
    forecast_at: AwareDateTime | None = None
    client_mutation_id: Identifier


class TaskExecutionStepStatus(_StrictInput):
    expected_task_version: int = Field(ge=1, strict=True)
    expected_step_version: int = Field(ge=1, strict=True)
    status: Literal["pending", "in_progress", "blocked", "done"]
    note: str | None = Field(default=None, max_length=4_000)
    client_mutation_id: Identifier

class CalendarEntryCreate(_StrictInput):
    domain: Domain
    title: Title
    description: str | None = Field(default=None, max_length=20_000)
    start_at: AwareDateTime
    end_at: AwareDateTime
    timezone: TimezoneName
    all_day: bool = False
    status: Literal["scheduled"] = "scheduled"
    kind: Literal["focus", "meeting", "reminder"] = "focus"
    attendees: list[ShortText] = Field(default_factory=list, max_length=500)
    memo_id: Identifier | None = None
    task_id: Identifier | None = None
    external_provider: str | None = Field(default=None, min_length=1, max_length=100)
    external_id: str | None = Field(default=None, min_length=1, max_length=500)
    client_mutation_id: Identifier | None = None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        return _validate_timezone_name(value)

    @model_validator(mode="after")
    def validate_calendar_entry(self) -> Self:
        _validate_range(self.start_at, self.end_at, "日程")
        links = (self.memo_id, self.task_id)
        if sum(value is not None for value in links) > 1:
            raise ValueError("memo_id、task_id 最多只能提供一个")
        if (self.external_provider is None) != (self.external_id is None):
            raise ValueError("external_provider 与 external_id 必须同时提供")
        return self


class MemoCalendarMaterializationCreate(_StrictInput):
    expected_memo_version: int = Field(ge=1, strict=True)
    title: Title
    description: LongText
    start_at: AwareDateTime
    end_at: AwareDateTime
    timezone: TimezoneName
    all_day: bool = Field(default=False, strict=True)
    kind: Literal["focus", "meeting", "reminder"] = "focus"
    client_mutation_id: Identifier

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        return _validate_timezone_name(value)

    @model_validator(mode="after")
    def validate_calendar_entry(self) -> Self:
        _validate_range(self.start_at, self.end_at, "日程")
        return self


class CalendarEntryPatch(_StrictInput):
    domain: Domain | None = None
    title: Title | None = None
    description: str | None = Field(default=None, max_length=20_000)
    start_at: AwareDateTime | None = None
    end_at: AwareDateTime | None = None
    timezone: TimezoneName | None = None
    all_day: bool | None = None
    status: CalendarStatus | None = None
    external_provider: str | None = Field(default=None, min_length=1, max_length=100)
    external_id: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        return None if value is None else _validate_timezone_name(value)

    @field_validator(
        "domain", "title", "start_at", "end_at", "timezone", "all_day", "status"
    )
    @classmethod
    def reject_null_required_values(cls, value: object) -> object:
        if value is None:
            raise ValueError("该字段不能为 null")
        return value

    @model_validator(mode="after")
    def validate_calendar_patch(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("PATCH 至少需要提供一个实际字段")
        if self.start_at is not None and self.end_at is not None:
            _validate_range(self.start_at, self.end_at, "日程")
        return self


class MeetingParticipantCreate(_StrictInput):
    member_id: Identifier
    role: ParticipantRole = "required"
    rsvp: ParticipantRsvp = "pending"
    minutes_confirmation_required: bool = True


class MeetingCreate(_StrictInput):
    domain: Domain
    calendar_entry_id: Identifier | None = None
    related_task_id: Identifier | None = None
    title: Title
    purpose: ShortText
    agenda: list[ShortText] = Field(default_factory=list, max_length=100)
    organizer_member_id: Identifier
    start_at: AwareDateTime
    end_at: AwareDateTime
    timezone: TimezoneName
    status: Literal["planned"] = "planned"
    location: str | None = Field(default=None, max_length=1_000)
    provider: str | None = Field(default=None, min_length=1, max_length=100)
    external_id: str | None = Field(default=None, min_length=1, max_length=500)
    participants: list[MeetingParticipantCreate] = Field(
        default_factory=list, max_length=500
    )
    client_mutation_id: Identifier | None = None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        return _validate_timezone_name(value)

    @model_validator(mode="after")
    def validate_meeting(self) -> Self:
        _validate_range(self.start_at, self.end_at, "会议")
        member_ids = [participant.member_id for participant in self.participants]
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("会议参与人不能重复")
        for participant in self.participants:
            if participant.role == "organizer" and (
                participant.member_id != self.organizer_member_id
            ):
                raise ValueError("organizer 角色必须对应 organizer_member_id")
        if (self.provider is None) != (self.external_id is None):
            raise ValueError("provider 与 external_id 必须同时提供")
        return self


class MeetingPatch(_StrictInput):
    domain: Domain | None = None
    calendar_entry_id: Identifier | None = None
    title: Title | None = None
    purpose: ShortText | None = None
    agenda: list[ShortText] | None = Field(default=None, max_length=100)
    organizer_member_id: Identifier | None = None
    start_at: AwareDateTime | None = None
    end_at: AwareDateTime | None = None
    timezone: TimezoneName | None = None
    status: MeetingStatus | None = None
    location: str | None = Field(default=None, max_length=1_000)
    provider: str | None = Field(default=None, min_length=1, max_length=100)
    external_id: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        return None if value is None else _validate_timezone_name(value)

    @field_validator(
        "domain",
        "title",
        "purpose",
        "organizer_member_id",
        "start_at",
        "end_at",
        "timezone",
        "status",
    )
    @classmethod
    def reject_null_required_values(cls, value: object) -> object:
        if value is None:
            raise ValueError("该字段不能为 null")
        return value

    @model_validator(mode="after")
    def validate_meeting_patch(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("PATCH 至少需要提供一个实际字段")
        if self.start_at is not None and self.end_at is not None:
            _validate_range(self.start_at, self.end_at, "会议")
        return self


class MeetingMinutesCreate(_StrictInput):
    content: LongText
    status: Literal["draft"] = "draft"
    required_confirmer_member_ids: list[Identifier] = Field(
        default_factory=list, max_length=500
    )
    client_mutation_id: Identifier | None = None

    @field_validator("required_confirmer_member_ids")
    @classmethod
    def normalize_confirmers(cls, values: list[str]) -> list[str]:
        return _deduplicate_identifiers(values)


class MeetingMinutesDecision(VersionCommand):
    decision: Literal["confirm", "dispute"]
    comment: str | None = Field(default=None, max_length=20_000)

    @model_validator(mode="after")
    def require_dispute_comment(self) -> Self:
        if self.decision == "dispute" and not self.comment:
            raise ValueError("质疑会议纪要必须提供 comment")
        return self


class _DocumentAudience(_StrictInput):
    access_scope: DocumentAccessScope = "owner_only"
    viewer_member_ids: list[Identifier] = Field(default_factory=list, max_length=500)

    @field_validator("viewer_member_ids")
    @classmethod
    def normalize_viewers(cls, values: list[str]) -> list[str]:
        return _deduplicate_identifiers(values)

    @model_validator(mode="after")
    def require_explicit_restricted_audience(self) -> Self:
        if self.access_scope == "restricted" and not self.viewer_member_ids:
            raise ValueError("restricted 文档必须指定至少一个可看成员")
        if self.access_scope != "restricted" and self.viewer_member_ids:
            raise ValueError("只有 restricted 文档可以指定 viewer_member_ids")
        return self


class DocumentCreate(_DocumentAudience):
    domain: Domain
    kind: DocumentKind = "general"
    title: Title
    content: LongText
    mime_type: str = Field(default="text/markdown", min_length=1, max_length=255)
    storage_ref: str | None = Field(default=None, min_length=1, max_length=4_000)
    source_item_id: Identifier | None = None
    source: SourceReference = Field(default_factory=SourceReference)
    tags: list[ShortText] = Field(default_factory=list, max_length=100)
    client_mutation_id: Identifier | None = None


class DocumentPatch(_StrictInput):
    domain: Domain | None = None
    title: Title | None = None
    content: LongText | None = None
    mime_type: str | None = Field(default=None, min_length=1, max_length=255)
    storage_ref: str | None = Field(default=None, min_length=1, max_length=4_000)
    source_item_id: Identifier | None = None
    source: SourceReference | None = None
    tags: list[ShortText] | None = Field(default=None, max_length=100)
    access_scope: DocumentAccessScope | None = None
    viewer_member_ids: list[Identifier] | None = Field(default=None, max_length=500)

    @field_validator("viewer_member_ids")
    @classmethod
    def normalize_viewers(cls, values: list[str] | None) -> list[str] | None:
        return None if values is None else _deduplicate_identifiers(values)

    @field_validator(
        "domain",
        "title",
        "content",
        "mime_type",
        "source",
        "tags",
        "access_scope",
        "viewer_member_ids",
    )
    @classmethod
    def reject_null_required_values(cls, value: object) -> object:
        if value is None:
            raise ValueError("该字段不能为 null")
        return value

    @model_validator(mode="after")
    def require_patch_field(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("PATCH 至少需要提供一个实际字段")
        return self


class DocumentFinding(_StrictInput):
    severity: DocumentFindingSeverity
    title: Title
    detail: LongText
    recommendation: str | None = Field(default=None, max_length=20_000)


class DocumentReviewCreate(VersionCommand):
    review_type: DocumentReviewType
    summary: LongText
    conclusion: DocumentReviewConclusion
    findings: list[DocumentFinding] = Field(default_factory=list, max_length=100)
    client_mutation_id: Identifier | None = None

    @model_validator(mode="after")
    def require_findings_for_non_approval(self) -> Self:
        if self.conclusion != "approved" and not self.findings:
            raise ValueError("非完全通过的审阅必须至少包含一项 finding")
        return self


class DocumentGenerate(VersionCommand):
    title: Title
    kind: Literal["general", "contract", "work_report"] = "general"
    variables: dict[Identifier, ShortText] = Field(default_factory=dict, max_length=100)
    domain: Domain | None = None
    access_scope: DocumentAccessScope = "owner_only"
    viewer_member_ids: list[Identifier] = Field(default_factory=list, max_length=500)
    storage_ref: str | None = Field(default=None, min_length=1, max_length=4_000)
    tags: list[ShortText] = Field(default_factory=list, max_length=100)
    client_mutation_id: Identifier | None = None

    @field_validator("viewer_member_ids")
    @classmethod
    def normalize_viewers(cls, values: list[str]) -> list[str]:
        return _deduplicate_identifiers(values)

    @model_validator(mode="after")
    def require_explicit_restricted_audience(self) -> Self:
        if self.access_scope == "restricted" and not self.viewer_member_ids:
            raise ValueError("restricted 文档必须指定至少一个可看成员")
        if self.access_scope != "restricted" and self.viewer_member_ids:
            raise ValueError("只有 restricted 文档可以指定 viewer_member_ids")
        return self


class DocumentExcerptCreate(VersionCommand):
    title: Title
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    viewer_member_ids: list[Identifier] = Field(min_length=1, max_length=500)
    client_mutation_id: Identifier | None = None

    @field_validator("viewer_member_ids")
    @classmethod
    def normalize_viewers(cls, values: list[str]) -> list[str]:
        return _deduplicate_identifiers(values)

    @model_validator(mode="after")
    def validate_offsets(self) -> Self:
        if self.end_offset <= self.start_offset:
            raise ValueError("end_offset 必须大于 start_offset")
        return self


class SyncCursorAck(_StrictInput):
    last_sequence: int = Field(ge=0)
