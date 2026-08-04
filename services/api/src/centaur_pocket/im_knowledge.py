from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

ClaimType = Literal["decision", "commitment", "task"]


@dataclass(frozen=True, slots=True)
class KnowledgeCandidate:
    idempotency_key: str
    claim_type: ClaimType
    text: str
    speaker: str | None
    message_id: str
    conversation_id: str
    confidence: float
    explicitness: Literal["explicit"] = "explicit"
    status: Literal["provisional"] = "provisional"


_DECISION = re.compile(
    r"(?:^|[，。；;：:\s])(?:决定|确定|就这么定|批准|最终选(?:择)?|同意采用)"
)
_COMMITMENT = re.compile(
    r"(?:^|[，。；;：:\s])(?:我会|我来|我负责|我去|我今天|我明天|我下周)"
)
_TASK = re.compile(
    r"(?:^|[，。；;：:\s])(?:请|麻烦|需要你|请你|跟进一下|安排一下)"
)


def extract_knowledge_candidates(
    messages: Iterable[dict[str, Any]],
) -> list[KnowledgeCandidate]:
    """Extract conservative, review-only candidates from explicit wording.

    This is intentionally a deterministic fallback for installations that do
    not yet have a private LLM configured.  It never turns candidates into
    confirmed facts and never invents a speaker, time or object.
    """

    candidates: list[KnowledgeCandidate] = []
    for message in messages:
        text = message.get("text_content") or message.get("text")
        if not isinstance(text, str):
            continue
        normalized = " ".join(text.split()).strip()
        if len(normalized) < 3 or _looks_negated(normalized):
            continue
        message_id = str(message.get("id") or "").strip()
        conversation_id = str(message.get("conversation_id") or "").strip()
        if not message_id or not conversation_id:
            continue
        speaker_value = message.get("sender_display_name")
        speaker = (
            speaker_value.strip()
            if isinstance(speaker_value, str) and speaker_value.strip()
            else None
        )
        for claim_type, pattern, confidence in (
            ("decision", _DECISION, 0.82),
            ("commitment", _COMMITMENT, 0.78),
            ("task", _TASK, 0.72),
        ):
            if not pattern.search(normalized):
                continue
            digest = hashlib.sha256(
                f"{message_id}\0{claim_type}".encode()
            ).hexdigest()
            candidates.append(
                KnowledgeCandidate(
                    idempotency_key=digest,
                    claim_type=claim_type,  # type: ignore[arg-type]
                    text=normalized,
                    speaker=speaker,
                    message_id=message_id,
                    conversation_id=conversation_id,
                    confidence=confidence,
                )
            )
    return candidates


def _looks_negated(text: str) -> bool:
    return bool(
        re.search(
            r"(?:不|没|未)(?:决定|确定|批准|同意|需要|安排|负责|跟进)",
            text,
        )
    )
