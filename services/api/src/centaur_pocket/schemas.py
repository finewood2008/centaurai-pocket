from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NonEmptyText = Annotated[str, Field(min_length=1, max_length=500)]
ItemState = Literal["inbox", "needs_review", "ready", "archived"]
TaskStatus = Literal["pending", "applied", "skipped"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


MOBILE_PAIRING_ALPHABET = frozenset("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


class MobilePairingCreate(StrictModel):
    pass


class MobilePairingClaim(StrictModel):
    code: str = Field(min_length=12, max_length=32)
    device_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=120)
    platform: Literal["android", "ios"]
    app_version: str = Field(min_length=1, max_length=80)

    @field_validator("code")
    @classmethod
    def normalize_pairing_code(cls, value: str) -> str:
        compact = "".join(
            character
            for character in value.upper()
            if character not in "- \t\r\n"
        )
        if len(compact) != 12 or any(
            character not in MOBILE_PAIRING_ALPHABET for character in compact
        ):
            raise ValueError("配对码必须为 12 位 Crockford Base32")
        return "-".join(compact[index : index + 4] for index in range(0, 12, 4))

    @field_validator("device_id", "display_name", "app_version")
    @classmethod
    def strip_mobile_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("字段不能为空")
        return cleaned


class MobileSessionRefresh(StrictModel):
    # Authentication failures are handled in the service so validation errors
    # never echo a rejected refresh credential in the response body.
    refresh_token: str
    device_id: str = Field(min_length=1, max_length=200)

    @field_validator("device_id")
    @classmethod
    def strip_refresh_device_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("device_id 不能为空")
        return cleaned


class MobileDeviceView(StrictModel):
    id: str
    device_id: str
    display_name: str
    platform: Literal["android", "ios"]
    app_version: str
    status: Literal["active", "revoked"]
    last_seen_at: str
    created_at: str


class MobilePairingCreated(StrictModel):
    pairing_id: str
    code: str
    expires_at: str


class MobileSessionTokens(StrictModel):
    token_type: Literal["Bearer"]
    access_token: str
    access_expires_at: str
    refresh_token: str
    refresh_expires_at: str
    device: MobileDeviceView


class MobileSessionView(StrictModel):
    token_type: Literal["Bearer"]
    access_expires_at: str
    device: MobileDeviceView


class MobileDeviceList(StrictModel):
    items: list[MobileDeviceView]
    total: int


class FolderSourceConfig(StrictModel):
    path: NonEmptyText
    recursive: bool = True
    include_hidden: bool = False
    extensions: list[str] | None = None

    @field_validator("path")
    @classmethod
    def normalize_path(cls, value: str) -> str:
        return str(Path(value).expanduser().resolve())

    @field_validator("extensions")
    @classmethod
    def normalize_extensions(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized: list[str] = []
        for extension in value:
            extension = extension.strip().lower()
            if not extension:
                continue
            normalized.append(
                extension if extension.startswith(".") else f".{extension}"
            )
        return sorted(set(normalized)) or None


class WechatVisibleWebConfig(StrictModel):
    capture_mode: Literal["visible_dom"] = "visible_dom"


class FolderSourceCreate(StrictModel):
    kind: Literal["folder"] = "folder"
    display_name: NonEmptyText
    config: FolderSourceConfig
    schedule: Literal["manual", "hourly", "daily"] = "manual"
    enabled: bool = True


class WechatVisibleWebSourceCreate(StrictModel):
    kind: Literal["wechat_visible_web"]
    display_name: NonEmptyText
    config: WechatVisibleWebConfig = Field(default_factory=WechatVisibleWebConfig)
    schedule: Literal["continuous"] = "continuous"
    enabled: bool = True


# Keep the legacy ability to omit ``kind`` for folder sources. A discriminated
# union would require the tag to be present and break existing mobile clients.
SourceCreate = FolderSourceCreate | WechatVisibleWebSourceCreate


class SourceUpdate(StrictModel):
    display_name: NonEmptyText | None = None
    config: FolderSourceConfig | WechatVisibleWebConfig | None = None
    schedule: Literal["manual", "hourly", "daily", "continuous"] | None = None
    enabled: bool | None = None


class CollectorHandshake(StrictModel):
    extension_id: str = Field(min_length=1, max_length=200)
    extension_version: str = Field(min_length=1, max_length=80)
    browser_name: str = Field(default="firefox", min_length=1, max_length=64)
    browser_version: str | None = Field(default=None, max_length=80)
    parser_version: str = Field(min_length=1, max_length=80)

    @field_validator("browser_name")
    @classmethod
    def normalize_browser_name(cls, value: str) -> str:
        if value.strip().casefold() != "firefox":
            raise ValueError("首版观察器仅支持 Firefox")
        return "firefox"


ObserverState = Literal[
    "login_required",
    "awaiting_phone_confirm",
    "active",
    "capture_paused",
    "browser_offline",
    "parser_degraded",
    "account_rejected",
]

class CollectorHeartbeat(StrictModel):
    browser_session_id: str = Field(min_length=1, max_length=200)
    state: ObserverState
    observed_at: datetime | None = None
    browser_version: str | None = Field(default=None, max_length=80)
    extension_version: str = Field(min_length=1, max_length=80)
    parser_version: str = Field(min_length=1, max_length=80)
    current_conversation_id: str | None = Field(default=None, max_length=500)
    current_conversation_name: str | None = Field(default=None, max_length=500)
    unread_conversation_count: int = Field(default=0, ge=0, le=100_000)


class CollectorMessageEvent(StrictModel):
    provider_msgid: str = Field(min_length=1, max_length=500)
    provider_conversation_id: str = Field(min_length=1, max_length=500)
    conversation_name: str | None = Field(default=None, max_length=500)
    conversation_type: Literal["direct", "group", "unknown"] = "unknown"
    direction: Literal["incoming", "outgoing", "system", "unknown"]
    message_type: Literal[
        "text", "image", "voice", "file", "video", "system", "other"
    ]
    sender_provider_id: str | None = Field(default=None, max_length=500)
    sender_display_name: str | None = Field(default=None, max_length=500)
    text: str | None = Field(default=None, max_length=200_000)
    displayed_time_text: str | None = Field(default=None, max_length=500)
    sent_at: datetime | None = None
    observed_at: datetime

    @model_validator(mode="after")
    def require_text_content(self) -> CollectorMessageEvent:
        if self.message_type == "text" and self.text is None:
            raise ValueError("文本消息必须包含 text")
        return self


class CollectorEventBatch(StrictModel):
    batch_id: str = Field(min_length=1, max_length=200)
    browser_session_id: str = Field(min_length=1, max_length=200)
    events: list[CollectorMessageEvent] = Field(min_length=1, max_length=100)


class ConversationPolicyUpdate(StrictModel):
    agent_enabled: bool | None = None
    retention_days: int | None = Field(default=None, ge=1, le=3650)

    @model_validator(mode="after")
    def require_change(self) -> ConversationPolicyUpdate:
        if not self.model_fields_set:
            raise ValueError("至少需要提供一项策略修改")
        return self


class RetentionApply(StrictModel):
    confirm: Literal["delete_expired_messages"]


class ItemPatch(StrictModel):
    title: NonEmptyText | None = None
    category: str | None = Field(default=None, max_length=120)
    tags: list[str] | None = None
    state: ItemState | None = None

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        result: list[str] = []
        seen: set[str] = set()
        for raw_tag in value:
            tag = raw_tag.strip()
            if not tag or tag.casefold() in seen:
                continue
            if len(tag) > 64:
                raise ValueError("tag must contain at most 64 characters")
            seen.add(tag.casefold())
            result.append(tag)
        if len(result) > 30:
            raise ValueError("at most 30 tags are allowed")
        return result


class GovernancePatch(ItemPatch):
    state: Literal["ready", "archived"] | None = "ready"


class GovernanceApply(StrictModel):
    action: Literal["apply"] | None = None
    patch: GovernancePatch = Field(default_factory=GovernancePatch)
    idempotency_key: str | None = Field(default=None, max_length=200)


class GovernanceSkip(StrictModel):
    action: Literal["skip"] | None = None
    idempotency_key: str | None = Field(default=None, max_length=200)


class GovernanceUndo(StrictModel):
    action: Literal["undo"] | None = None
    idempotency_key: str | None = Field(default=None, max_length=200)


class GovernanceAction(StrictModel):
    action: Literal["apply", "skip", "undo"]
    patch: GovernancePatch | None = None
    idempotency_key: str | None = Field(default=None, max_length=200)


class AgentSearchFilters(StrictModel):
    tags: list[str] = Field(default_factory=list, max_length=30)
    category: str | None = Field(default=None, max_length=120)
    source_ids: list[str] = Field(default_factory=list, max_length=50)
    conversation_ids: list[str] = Field(default_factory=list, max_length=50)
    participant_ids: list[str] = Field(default_factory=list, max_length=50)
    sent_from: datetime | None = None
    sent_to: datetime | None = None
    item_kinds: list[Literal["document", "im_message", "knowledge"]] = Field(
        default_factory=list,
        max_length=3,
    )

    @field_validator("tags", mode="after")
    @classmethod
    def normalize_search_tags(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            tag = item.strip()
            if not tag or tag.casefold() in seen:
                continue
            if len(tag) > 64:
                raise ValueError("tag 最长为 64 个字符")
            normalized.append(tag)
            seen.add(tag.casefold())
        return normalized

    @field_validator(
        "source_ids", "conversation_ids", "participant_ids", mode="after"
    )
    @classmethod
    def normalize_identifier_filters(cls, value: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        if any(len(item) > 500 for item in normalized):
            raise ValueError("筛选 ID 最长为 500 个字符")
        return normalized

    @field_validator("item_kinds", mode="after")
    @classmethod
    def deduplicate_item_kinds(
        cls, value: list[Literal["document", "im_message", "knowledge"]]
    ) -> list[Literal["document", "im_message", "knowledge"]]:
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def validate_sent_range(self) -> AgentSearchFilters:
        for value in (self.sent_from, self.sent_to):
            if value is not None and value.utcoffset() is None:
                raise ValueError("IM 时间筛选必须包含时区")
        if self.sent_from and self.sent_to and self.sent_from > self.sent_to:
            raise ValueError("sent_from 不能晚于 sent_to")
        return self


class AgentSearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=8, ge=1, le=50)
    filters: AgentSearchFilters = Field(default_factory=AgentSearchFilters)


class CaptureCreate(StrictModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    title: str | None = Field(default=None, max_length=500)
    text: str | None = Field(default=None, max_length=20 * 1024 * 1024)
    url: str | None = Field(default=None, max_length=4000)
    mime_type: str = Field(default="text/plain", alias="mimeType", max_length=200)
    origin: str = Field(default="mobile", max_length=100)
    idempotency_key: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def require_content(self) -> CaptureCreate:
        if not (self.text or "").strip() and not (self.url or "").strip():
            raise ValueError("text 或 url 至少需要填写一项")
        return self
