from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReliableSourceCandidateCreate(StrictModel):
    display_name: str = Field(min_length=1, max_length=200)
    organization_origin: str = Field(min_length=1, max_length=300)
    feed_url: str = Field(min_length=1, max_length=2048)
    trust_reason: str = Field(min_length=1, max_length=2000)
    scope: str = Field(min_length=1, max_length=1000)
    review_due_at: datetime | None = None

    @field_validator(
        "display_name",
        "organization_origin",
        "feed_url",
        "trust_reason",
        "scope",
    )
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("字段不能为空")
        return cleaned

    @field_validator("review_due_at")
    @classmethod
    def require_review_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("review_due_at 必须包含时区")
        return value


class ReliableSourceCandidateConfirm(StrictModel):
    expected_version: int = Field(ge=1)
    schedule: Literal["manual", "daily"]


class ReliableSourceCandidateDismiss(StrictModel):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("reason 不能为空")
        return cleaned


class ReliableCollectionPlanPatch(StrictModel):
    schedule: Literal["manual", "daily"] | None = None
    enabled: bool | None = None
    review_due_at: datetime | None = None

    @field_validator("review_due_at")
    @classmethod
    def require_review_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("review_due_at 必须包含时区")
        return value

    @model_validator(mode="after")
    def require_change(self) -> ReliableCollectionPlanPatch:
        if not self.model_fields_set:
            raise ValueError("至少需要提供一项计划修改")
        return self


class ReliableSourceCollect(StrictModel):
    expected_version: int = Field(ge=1)
