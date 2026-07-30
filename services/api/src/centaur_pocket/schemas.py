from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NonEmptyText = Annotated[str, Field(min_length=1, max_length=500)]
ItemState = Literal["inbox", "needs_review", "ready", "archived"]
TaskStatus = Literal["pending", "applied", "skipped"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


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


class SourceCreate(StrictModel):
    kind: Literal["folder"] = "folder"
    display_name: NonEmptyText
    config: FolderSourceConfig
    schedule: Literal["manual", "hourly", "daily"] = "manual"
    enabled: bool = True


class SourceUpdate(StrictModel):
    display_name: NonEmptyText | None = None
    config: FolderSourceConfig | None = None
    schedule: Literal["manual", "hourly", "daily"] | None = None
    enabled: bool | None = None


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
    tags: list[str] = Field(default_factory=list)
    category: str | None = None


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
