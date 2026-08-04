from __future__ import annotations

from typing import Final

from ..service import PocketError

TASK_TRANSITIONS: Final[dict[str, set[str]]] = {
    "draft": {"issued", "abnormal_closed"},
    "issued": {"aligned", "abnormal_closed"},
    "aligned": {"in_progress", "abnormal_closed"},
    "in_progress": {"submitted", "abnormal_closed"},
    "submitted": {"in_progress", "accepted", "abnormal_closed"},
    "accepted": set(),
    "abnormal_closed": set(),
}


def require_task_transition(current: str, target: str) -> None:
    if target not in TASK_TRANSITIONS.get(current, set()):
        raise PocketError(409, f"任务不能从 {current} 转换为 {target}")


def transition_timestamp_field(target: str) -> str | None:
    return {
        "in_progress": "started_at",
        "submitted": "submitted_at",
        "accepted": "accepted_at",
    }.get(target)
