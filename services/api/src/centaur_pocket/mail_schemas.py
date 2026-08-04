from __future__ import annotations

import unicodedata
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictMailInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DeviceAuthorizationCreate(StrictMailInput):
    account_label: str = Field(min_length=1, max_length=120)

    @field_validator("account_label", mode="before")
    @classmethod
    def validate_account_label(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = unicodedata.normalize("NFC", value)
        if (
            not normalized
            or len(normalized) > 120
            or normalized != normalized.strip()
            or any(
                character in {"\r", "\n", "\t"}
                or unicodedata.category(character).startswith("C")
                for character in normalized
            )
        ):
            raise ValueError("account_label 必须是安全的单行文本")
        return normalized


class VersionCommand(StrictMailInput):
    expected_version: int = Field(ge=1)


class ReplyDraftCreate(VersionCommand):
    body_text: str | None = Field(default=None, min_length=1, max_length=100_000)

    @field_validator("body_text", mode="before")
    @classmethod
    def reject_explicit_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("body_text 显式提供时不能为 null")
        return value


class DraftPatch(VersionCommand):
    body_text: str = Field(min_length=1, max_length=100_000)

    @model_validator(mode="after")
    def require_update(self) -> Self:
        if self.model_fields_set == {"expected_version"}:
            raise ValueError("PATCH 至少需要提供一个草稿字段")
        return self


class SendIntentConfirm(VersionCommand):
    preview_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation: Literal["send_exact_preview"]


TaskCandidateStatus = Literal["pending", "confirmed", "dismissed"]
