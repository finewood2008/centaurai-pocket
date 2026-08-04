from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from statistics import median_low
from typing import Any, Literal
from zoneinfo import ZoneInfo

from ..service import PocketError, json_loads, parse_utc

ASSURANCE_POLICY_ID = "centaur.task-attribution-assurance"
ASSURANCE_POLICY_VERSION = 1
TASK_ANALYSIS_SCHEMA = "centaur.task-analysis.v1"
MAX_ANALYSIS_EVENTS = 50_000
MAX_ANALYSIS_TASKS = 50_000
MAX_ANALYSIS_MEMBERS = 50_000
MAX_ANALYSIS_DOMAIN_ROWS = 50_000

AnalysisAction = Literal[
    "start",
    "checkin",
    "step_status",
    "submit",
    "agreement_response",
    "change_response",
]
AssuranceTier = Literal["a2_owner_control", "a1_capability", "a0_unknown"]

OWNER_ASSURANCE_METHODS = frozenset({"owner_token", "owner_device_session"})
CAPABILITY_ASSURANCE_METHODS = frozenset(
    {
        "dual_channel_capability",
        "task_session",
        "task_change_session",
        "dual_channel_task_execution",
        "task_execution_session",
        "task_execution_refresh",
    }
)

ACTION_BY_EVENT_TYPE: dict[str, AnalysisAction] = {
    "task.in_progress": "start",
    "task.execution_started": "start",
    "task.checkin_recorded": "checkin",
    "task.execution_checkin_recorded": "checkin",
    "task.step_status_updated": "step_status",
    "task.execution_step_updated": "step_status",
    "task.submitted": "submit",
    "task.execution_submitted": "submit",
    "task.agreement_accept": "agreement_response",
    "task.agreement_reject": "agreement_response",
    "task.agreement_counter": "agreement_response",
    "task.change_accepted": "change_response",
    "task.change_rejected": "change_response",
    "task.change_canceled": "change_response",
}

SECURITY_EVENT_TYPES = frozenset(
    {
        "task.alignment_invitation_created",
        "task.agreement_session_issued",
        "task.change_invitation_created",
        "task.change_session_issued",
        "task.execution_invitation_created",
        "task.execution_session_issued",
        "task.execution_refresh_rotated",
        "task.execution_security_revoked",
    }
)
EXECUTION_EVENT_TYPES = frozenset(
    {
        "task.execution_started",
        "task.execution_checkin_recorded",
        "task.execution_step_updated",
        "task.execution_submitted",
    }
)
AGREEMENT_EVENT_TYPES = frozenset(
    {
        "task.agreement_accept",
        "task.agreement_reject",
        "task.agreement_counter",
    }
)
CHANGE_EVENT_TYPES = frozenset(
    {
        "task.change_accepted",
        "task.change_rejected",
        "task.change_canceled",
    }
)


def _empty_action_bucket(action: AnalysisAction) -> dict[str, Any]:
    return {
        "action": action,
        "raw_event_count": 0,
        "classified_event_count": 0,
        "weighted_event_basis_points": 0,
        "by_tier": {
            "a2_owner_control": 0,
            "a1_capability": 0,
            "a0_unknown": 0,
        },
        "assignment_epoch_count": 0,
        "_assignment_epochs": set(),
    }


def _empty_action_map() -> dict[AnalysisAction, dict[str, Any]]:
    return {
        action: _empty_action_bucket(action)
        for action in (
            "start",
            "checkin",
            "step_status",
            "submit",
            "agreement_response",
            "change_response",
        )
    }


def _record_action(
    actions: dict[AnalysisAction, dict[str, Any]],
    *,
    action: AnalysisAction,
    tier: AssuranceTier,
    weight_basis_points: int,
    assignment_epoch: int | None,
) -> None:
    bucket = actions[action]
    bucket["raw_event_count"] += 1
    bucket["by_tier"][tier] += 1
    bucket["weighted_event_basis_points"] += weight_basis_points
    if tier != "a0_unknown":
        bucket["classified_event_count"] += 1
    if assignment_epoch is not None:
        bucket["_assignment_epochs"].add(assignment_epoch)


def _finish_actions(
    actions: dict[AnalysisAction, dict[str, Any]],
) -> list[dict[str, Any]]:
    finished: list[dict[str, Any]] = []
    for bucket in actions.values():
        epochs = bucket.pop("_assignment_epochs")
        bucket["assignment_epoch_count"] = len(epochs)
        finished.append(bucket)
    return finished


def _payload_identifier(value: Any, key: str) -> str | None:
    if not isinstance(value, dict):
        return None
    candidate = value.get(key)
    return candidate if isinstance(candidate, str) and candidate else None


def _payload_integer(value: Any, key: str) -> int | None:
    if not isinstance(value, dict):
        return None
    candidate = value.get(key)
    return candidate if isinstance(candidate, int) and not isinstance(candidate, bool) else None


def _event_payload(row: sqlite3.Row) -> dict[str, Any]:
    payload = json_loads(row["payload_json"], {})
    return payload if isinstance(payload, dict) else {}


def _event_task_id(row: sqlite3.Row, payload: dict[str, Any]) -> str | None:
    if row["aggregate_type"] == "task":
        return str(row["aggregate_id"])
    direct = _payload_identifier(payload, "task_id")
    if direct is not None:
        return direct
    task = payload.get("task")
    task_id = _payload_identifier(task, "id") or _payload_identifier(task, "task_id")
    if task_id is not None:
        return task_id
    for key in (
        "agreement",
        "change",
        "check_in",
        "checkin",
        "step",
        "session",
    ):
        nested = _payload_identifier(payload.get(key), "task_id")
        if nested is not None:
            return nested
    return None


def _payload_task_ids(payload: dict[str, Any]) -> set[str]:
    task_ids: set[str] = set()
    direct = _payload_identifier(payload, "task_id")
    if direct is not None:
        task_ids.add(direct)
    task = payload.get("task")
    for key in ("id", "task_id"):
        task_id = _payload_identifier(task, key)
        if task_id is not None:
            task_ids.add(task_id)
    for key in (
        "agreement",
        "change",
        "check_in",
        "checkin",
        "step",
        "session",
    ):
        task_id = _payload_identifier(payload.get(key), "task_id")
        if task_id is not None:
            task_ids.add(task_id)
    return task_ids


def _legacy_actor_member_id(row: sqlite3.Row) -> str | None:
    # Untyped events have no binding that would authenticate payload claims.
    # Preserve only the event envelope's logical actor, and keep it at A0.
    actor = row["actor_member_id"]
    return str(actor) if isinstance(actor, str) and actor else None


def _tier_for_method(method: str) -> tuple[AssuranceTier, int]:
    if method in OWNER_ASSURANCE_METHODS:
        return "a2_owner_control", 10_000
    if method in CAPABILITY_ASSURANCE_METHODS:
        return "a1_capability", 5_000
    return "a0_unknown", 0


def _transaction_timestamps_match(left: str, right: str) -> bool:
    # Older writers sampled the event timestamp immediately after the domain
    # insert. Keep the one-second rollover tolerance for those rows; new
    # writers pass the exact transaction timestamp to _append_event.
    return abs((parse_utc(left) - parse_utc(right)).total_seconds()) <= 1


def _active_at(row: sqlite3.Row, occurred_at: datetime, expires_key: str) -> bool:
    revoked_at = row["revoked_at"]
    return (
        parse_utc(str(row["created_at"])) <= occurred_at
        and occurred_at < parse_utc(str(row[expires_key]))
        and (revoked_at is None or occurred_at < parse_utc(str(revoked_at)))
    )


def _resolve_execution_evidence(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    payload: dict[str, Any],
) -> tuple[str | None, str | None, int | None, str | None]:
    session_id = _payload_identifier(payload, "actor_session_id")
    family_id = _payload_identifier(payload, "refresh_family_id")
    subject_type = payload.get("actor_subject_type")
    subject_id = _payload_identifier(payload, "actor_subject_id")
    member_id = _payload_identifier(payload, "on_behalf_of_member_id")
    method = payload.get("assurance_method")
    assignment_epoch = _payload_integer(payload, "assignment_epoch")
    claimed_task_id = _event_task_id(row, payload)
    if not all(
        (
            session_id,
            family_id,
            member_id,
            isinstance(method, str) and method,
            assignment_epoch is not None,
            claimed_task_id,
        )
    ) or subject_type != "task_execution_capability" or subject_id != session_id:
        return claimed_task_id, None, None, None
    event_type = str(row["event_type"])
    if event_type == "task.execution_checkin_recorded":
        aggregate_valid = row["aggregate_type"] == "task_checkin"
    else:
        aggregate_valid = (
            row["aggregate_type"] == "task"
            and str(row["aggregate_id"]) == claimed_task_id
        )
    payload_task_ids = _payload_task_ids(payload)
    if not aggregate_valid or (
        payload_task_ids and payload_task_ids != {claimed_task_id}
    ):
        return claimed_task_id, None, None, None
    session = connection.execute(
        "SELECT * FROM secretary_task_execution_sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    family = connection.execute(
        "SELECT * FROM secretary_task_execution_refresh_families WHERE id = ?",
        (family_id,),
    ).fetchone()
    if session is None or family is None:
        return claimed_task_id, None, None, None
    occurred_at = parse_utc(str(row["occurred_at"]))
    domain_binding_valid = True
    if event_type == "task.execution_checkin_recorded":
        check_in = payload.get("check_in")
        checkin_id = _payload_identifier(check_in, "id")
        checkin = connection.execute(
            "SELECT * FROM secretary_task_checkins WHERE id = ?",
            (checkin_id,),
        ).fetchone()
        domain_binding_valid = bool(
            checkin_id
            and checkin is not None
            and str(row["aggregate_id"]) == checkin_id
            and checkin["workspace_id"] == row["workspace_id"]
            and checkin["task_id"] == claimed_task_id
            and checkin["created_by"] == member_id
            and checkin["device_id"] == row["device_id"]
            and str(checkin["created_at"]) == str(row["occurred_at"])
            and _payload_identifier(check_in, "task_id") == claimed_task_id
            and _payload_integer(check_in, "version") == checkin["version"]
            and _payload_integer(check_in, "task_version")
            == checkin["task_version"]
        )
    elif event_type == "task.execution_step_updated":
        step_id = _payload_identifier(payload.get("step"), "id")
        step = connection.execute(
            "SELECT task_id, workspace_id FROM secretary_task_steps WHERE id = ?",
            (step_id,),
        ).fetchone()
        domain_binding_valid = bool(
            step_id
            and step is not None
            and step["workspace_id"] == row["workspace_id"]
            and step["task_id"] == claimed_task_id
        )
    valid = all(
        (
            domain_binding_valid,
            session["workspace_id"] == row["workspace_id"],
            family["workspace_id"] == row["workspace_id"],
            session["task_id"] == claimed_task_id,
            family["task_id"] == claimed_task_id,
            session["refresh_family_id"] == family_id,
            session["assignee_member_id"] == member_id,
            family["assignee_member_id"] == member_id,
            session["assignment_epoch"] == assignment_epoch,
            family["assignment_epoch"] == assignment_epoch,
            session["assurance_method"] == method,
            method == "dual_channel_task_execution",
            session["invitation_id"] == family["invitation_id"],
            session["client_device_id"] == row["device_id"],
            family["client_device_id"] == row["device_id"],
            _active_at(session, occurred_at, "expires_at"),
            _active_at(family, occurred_at, "absolute_expires_at"),
            row["actor_type"] == "system",
            row["actor_member_id"] is None,
        )
    )
    return (
        claimed_task_id,
        member_id if valid else None,
        assignment_epoch if valid else None,
        method if valid else None,
    )


def _resolve_agreement_evidence(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    payload: dict[str, Any],
    *,
    decision_reference_count: int,
) -> tuple[str | None, str | None, int | None, str | None]:
    decision_payload = payload.get("decision")
    decision_id = _payload_identifier(decision_payload, "id")
    claimed_task_id = _event_task_id(row, payload)
    if decision_id is None:
        return claimed_task_id, None, None, None
    decision = connection.execute(
        """
        SELECT decision.*, agreement.task_id, agreement.workspace_id,
               agreement.version AS agreement_version
        FROM secretary_task_alignment_decisions decision
        JOIN secretary_task_alignment_cases agreement
          ON agreement.id = decision.case_id
        WHERE decision.id = ?
        """,
        (decision_id,),
    ).fetchone()
    if decision is None:
        return claimed_task_id, None, None, None
    method = str(decision["assurance_method"])
    expected_actor_type = "owner" if method in OWNER_ASSURANCE_METHODS else "member"
    agreement_payload = payload.get("agreement")
    decision_fields = (
        "id",
        "case_id",
        "revision_id",
        "revision_digest",
        "action",
        "actor_role",
        "actor_member_id",
        "actor_session_id",
        "assurance_method",
        "reason",
        "counter_revision_id",
        "version",
        "created_at",
    )
    valid = all(
        (
            decision_reference_count == 1,
            decision["workspace_id"] == row["workspace_id"],
            decision["task_id"] == claimed_task_id,
            decision["actor_member_id"] == row["actor_member_id"],
            isinstance(decision_payload, dict),
            all(
                decision[field] == decision_payload.get(field)
                for field in decision_fields
            ),
            row["aggregate_type"] == "task_agreement",
            str(row["aggregate_id"]) == decision["case_id"],
            row["event_type"] == f"task.agreement_{decision['action']}",
            _payload_identifier(agreement_payload, "id") == decision["case_id"],
            _payload_identifier(agreement_payload, "task_id")
            == decision["task_id"],
            _payload_integer(agreement_payload, "version")
            == row["aggregate_version"],
            row["actor_type"] == expected_actor_type,
            _transaction_timestamps_match(
                str(decision["created_at"]), str(row["occurred_at"])
            ),
        )
    )
    return (
        claimed_task_id,
        str(decision["actor_member_id"]) if valid else None,
        None,
        method if valid else None,
    )


def _resolve_change_evidence(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    payload: dict[str, Any],
    *,
    decision_reference_count: int,
) -> tuple[str | None, str | None, int | None, str | None]:
    decision_payload = payload.get("decision")
    decision_id = _payload_identifier(decision_payload, "id")
    claimed_task_id = _event_task_id(row, payload)
    if decision_id is None:
        return claimed_task_id, None, None, None
    decision = connection.execute(
        """
        SELECT decision.*, change_record.task_id, change_record.workspace_id,
               change_record.version AS change_version
        FROM secretary_task_change_decisions decision
        JOIN secretary_task_changes change_record
          ON change_record.id = decision.change_id
        WHERE decision.id = ?
        """,
        (decision_id,),
    ).fetchone()
    if decision is None:
        return claimed_task_id, None, None, None
    method = str(decision["assurance_method"])
    expected_actor_type = "owner" if method in OWNER_ASSURANCE_METHODS else "member"
    change_payload = payload.get("change")
    decision_fields = (
        "id",
        "change_id",
        "proposal_digest",
        "action",
        "actor_member_id",
        "actor_session_id",
        "assurance_method",
        "reason",
        "version",
        "created_at",
    )
    expected_status = {
        "accept": "accepted",
        "reject": "rejected",
        "cancel": "canceled",
    }[str(decision["action"])]
    valid = all(
        (
            decision_reference_count == 1,
            decision["workspace_id"] == row["workspace_id"],
            decision["task_id"] == claimed_task_id,
            decision["actor_member_id"] == row["actor_member_id"],
            isinstance(decision_payload, dict),
            all(
                decision[field] == decision_payload.get(field)
                for field in decision_fields
            ),
            row["aggregate_type"] == "task_change",
            str(row["aggregate_id"]) == decision["change_id"],
            row["event_type"] == f"task.change_{expected_status}",
            _payload_identifier(change_payload, "id") == decision["change_id"],
            _payload_identifier(change_payload, "task_id") == decision["task_id"],
            _payload_integer(change_payload, "version")
            == row["aggregate_version"],
            row["actor_type"] == expected_actor_type,
            _transaction_timestamps_match(
                str(decision["created_at"]), str(row["occurred_at"])
            ),
        )
    )
    return (
        claimed_task_id,
        str(decision["actor_member_id"]) if valid else None,
        None,
        method if valid else None,
    )


def _resolve_event_evidence(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    payload: dict[str, Any],
    decision_reference_counts: Counter[tuple[str, str]],
) -> tuple[str | None, str | None, int | None, str | None, bool]:
    event_type = str(row["event_type"])
    if event_type in EXECUTION_EVENT_TYPES:
        task_id, member_id, epoch, method = _resolve_execution_evidence(
            connection, row, payload
        )
        return task_id, member_id, epoch, method, method is None
    if event_type in AGREEMENT_EVENT_TYPES:
        decision_id = _payload_identifier(payload.get("decision"), "id")
        task_id, member_id, epoch, method = _resolve_agreement_evidence(
            connection,
            row,
            payload,
            decision_reference_count=(
                decision_reference_counts[("agreement", decision_id)]
                if decision_id is not None
                else 0
            ),
        )
        return task_id, member_id, epoch, method, method is None
    if event_type in CHANGE_EVENT_TYPES:
        decision_id = _payload_identifier(payload.get("decision"), "id")
        task_id, member_id, epoch, method = _resolve_change_evidence(
            connection,
            row,
            payload,
            decision_reference_count=(
                decision_reference_counts[("change", decision_id)]
                if decision_id is not None
                else 0
            ),
        )
        return task_id, member_id, epoch, method, method is None
    # Legacy task/check-in/step events have no typed assurance. Their actor
    # columns remain useful as a logical claim, but never upgrade above A0.
    return (
        _event_task_id(row, payload),
        _legacy_actor_member_id(row),
        None,
        None,
        False,
    )


def _period_bounds(
    workspace_timezone: str, from_date: date, to_date: date
) -> tuple[datetime, datetime]:
    if to_date < from_date:
        raise PocketError(422, "分析结束日期不能早于开始日期")
    if (to_date - from_date).days > 365:
        raise PocketError(422, "单次任务分析最多覆盖 366 天")
    timezone = ZoneInfo(workspace_timezone)
    start = datetime.combine(from_date, time.min, timezone).astimezone(UTC)
    end = datetime.combine(
        to_date + timedelta(days=1), time.min, timezone
    ).astimezone(UTC)
    return start, end


def _task_terminal_at(row: sqlite3.Row) -> datetime | None:
    if row["stage"] == "accepted" and row["accepted_at"]:
        return parse_utc(str(row["accepted_at"]))
    if row["stage"] == "abnormal_closed":
        return parse_utc(str(row["updated_at"]))
    return None


def _task_overlaps_period(
    row: sqlite3.Row, start: datetime, end: datetime
) -> bool:
    if row["deleted_at"] is not None:
        return False
    if parse_utc(str(row["created_at"])) >= end:
        return False
    terminal_at = _task_terminal_at(row)
    return terminal_at is None or terminal_at >= start


def _task_period_outcome(row: sqlite3.Row, end: datetime) -> str:
    due_at = parse_utc(str(row["due_at"])) if row["due_at"] else None
    if row["stage"] == "accepted" and row["accepted_at"]:
        accepted_at = parse_utc(str(row["accepted_at"]))
        if accepted_at < end:
            if due_at is None:
                return "accepted_without_due"
            return "accepted_on_time" if accepted_at <= due_at else "accepted_late"
    if row["stage"] == "abnormal_closed" and parse_utc(str(row["updated_at"])) < end:
        return "abnormal_closed"
    if due_at is not None and due_at < end:
        return "open_overdue"
    return "open"


def _empty_task_facts() -> dict[str, Any]:
    return {
        "total_tasks": 0,
        "open_tasks": 0,
        "accepted_tasks": 0,
        "accepted_on_time_tasks": 0,
        "accepted_late_tasks": 0,
        "accepted_without_due_tasks": 0,
        "abnormal_closed_tasks": 0,
        "overdue_open_tasks": 0,
        "current_risk_tasks": 0,
        "rework_event_count": 0,
        "median_start_to_accept_seconds": None,
        "start_to_accept_sample_count": 0,
    }


def _record_task_fact(facts: dict[str, Any], outcome: str, health: str) -> None:
    facts["total_tasks"] += 1
    if outcome.startswith("accepted_"):
        facts["accepted_tasks"] += 1
        facts[f"{outcome}_tasks"] += 1
    elif outcome == "abnormal_closed":
        facts["abnormal_closed_tasks"] += 1
    else:
        facts["open_tasks"] += 1
        if outcome == "open_overdue":
            facts["overdue_open_tasks"] += 1
    if outcome in {"open", "open_overdue"} and health in {
        "at_risk",
        "blocked",
        "overdue",
    }:
        facts["current_risk_tasks"] += 1


def _current_stage_counts() -> dict[str, int]:
    return {
        "draft": 0,
        "issued": 0,
        "aligned": 0,
        "in_progress": 0,
        "submitted": 0,
        "accepted": 0,
        "abnormal_closed": 0,
    }


def _checkin_id(row: sqlite3.Row, payload: dict[str, Any]) -> str | None:
    if row["aggregate_type"] == "task_checkin":
        return str(row["aggregate_id"])
    for key in ("check_in", "checkin"):
        checkin_id = _payload_identifier(payload.get(key), "id")
        if checkin_id is not None:
            return checkin_id
    return None


def build_task_analysis(
    connection: sqlite3.Connection,
    workspace: sqlite3.Row,
    from_date: date,
    to_date: date,
    *,
    snapshot_at: str,
) -> dict[str, Any]:
    workspace_id = str(workspace["id"])
    timezone_name = str(workspace["timezone"])
    start, end = _period_bounds(timezone_name, from_date, to_date)
    start_iso = start.isoformat(timespec="seconds").replace("+00:00", "Z")
    end_iso = end.isoformat(timespec="seconds").replace("+00:00", "Z")

    task_rows = connection.execute(
        """
        SELECT * FROM secretary_business_tasks
        WHERE workspace_id = ? AND deleted_at IS NULL AND created_at < ?
          AND (
            stage NOT IN ('accepted', 'abnormal_closed')
            OR (stage = 'accepted'
                AND (accepted_at IS NULL OR accepted_at >= ?))
            OR (stage = 'abnormal_closed' AND updated_at >= ?)
          )
        ORDER BY created_at, id LIMIT ?
        """,
        (workspace_id, end_iso, start_iso, start_iso, MAX_ANALYSIS_TASKS + 1),
    ).fetchall()
    if len(task_rows) > MAX_ANALYSIS_TASKS:
        raise PocketError(413, "所选期间任务过多，请缩短分析范围")
    task_rows = [
        row for row in task_rows if _task_overlaps_period(row, start, end)
    ]
    task_ids = {str(row["id"]) for row in task_rows}

    member_rows = connection.execute(
        """
        SELECT * FROM secretary_workspace_members
        WHERE workspace_id = ? ORDER BY display_name, id LIMIT ?
        """,
        (workspace_id, MAX_ANALYSIS_MEMBERS + 1),
    ).fetchall()
    if len(member_rows) > MAX_ANALYSIS_MEMBERS:
        raise PocketError(413, "工作区成员过多，暂时无法生成任务分析")
    members_by_id = {str(row["id"]): row for row in member_rows}

    task_actions = {task_id: _empty_action_map() for task_id in task_ids}
    member_actions: dict[str, dict[AnalysisAction, dict[str, Any]]] = {}
    observed_checkin_ids: set[str] = set()
    observed_agreement_decision_ids: set[str] = set()
    observed_change_decision_ids: set[str] = set()
    coverage = {
        "events_considered": 0,
        "resolved_typed_events": 0,
        "a0_unknown_events": 0,
        "excluded_security_events": 0,
        "integrity_mismatch_events": 0,
        "domain_rows_without_event": 0,
        "strong_member_identity_supported": False,
        "limitation": (
            "人员归属仅表示受验证的 Owner 控制或任务能力事件。当前成员分配是生成时快照；"
            "A1、on_behalf 和业务成员字段都不证明自然人本人操作。"
        ),
    }
    event_rows = connection.execute(
        """
        SELECT * FROM secretary_workspace_events
        WHERE workspace_id = ? AND occurred_at >= ? AND occurred_at < ?
          AND event_type LIKE 'task.%'
        ORDER BY sequence LIMIT ?
        """,
        (workspace_id, start_iso, end_iso, MAX_ANALYSIS_EVENTS + 1),
    ).fetchall()
    if len(event_rows) > MAX_ANALYSIS_EVENTS:
        raise PocketError(413, "所选期间任务事件过多，请缩短分析范围")

    decision_reference_counts: Counter[tuple[str, str]] = Counter()
    for event in event_rows:
        event_type = str(event["event_type"])
        if event_type not in AGREEMENT_EVENT_TYPES | CHANGE_EVENT_TYPES:
            continue
        payload = _event_payload(event)
        decision_id = _payload_identifier(payload.get("decision"), "id")
        if decision_id is not None:
            kind = "agreement" if event_type in AGREEMENT_EVENT_TYPES else "change"
            decision_reference_counts[(kind, decision_id)] += 1

    for event in event_rows:
        event_type = str(event["event_type"])
        payload = _event_payload(event)
        mapped_task_id = _event_task_id(event, payload)
        if event_type in SECURITY_EVENT_TYPES:
            if mapped_task_id in task_ids:
                coverage["excluded_security_events"] += 1
            continue
        action = ACTION_BY_EVENT_TYPE.get(event_type)
        if action is None:
            continue
        task_id, member_id, assignment_epoch, method, integrity_mismatch = (
            _resolve_event_evidence(
                connection, event, payload, decision_reference_counts
            )
        )
        if task_id not in task_ids:
            continue
        coverage["events_considered"] += 1
        if integrity_mismatch:
            coverage["integrity_mismatch_events"] += 1
            member_id = None
            assignment_epoch = None
        tier: AssuranceTier
        weight: int
        if method is None:
            tier, weight = "a0_unknown", 0
            coverage["a0_unknown_events"] += 1
        else:
            tier, weight = _tier_for_method(method)
            if tier == "a0_unknown":
                coverage["a0_unknown_events"] += 1
            else:
                coverage["resolved_typed_events"] += 1
        if method is None:
            assignment_epoch = None
        _record_action(
            task_actions[task_id],
            action=action,
            tier=tier,
            weight_basis_points=weight,
            assignment_epoch=assignment_epoch,
        )
        if member_id is not None and member_id in members_by_id:
            actions = member_actions.setdefault(member_id, _empty_action_map())
            _record_action(
                actions,
                action=action,
                tier=tier,
                weight_basis_points=weight,
                assignment_epoch=assignment_epoch,
            )
        checkin_id = _checkin_id(event, payload)
        if checkin_id is not None:
            observed_checkin_ids.add(checkin_id)
        decision_id = _payload_identifier(payload.get("decision"), "id")
        if decision_id is not None:
            if event_type in AGREEMENT_EVENT_TYPES:
                observed_agreement_decision_ids.add(decision_id)
            elif event_type in CHANGE_EVENT_TYPES:
                observed_change_decision_ids.add(decision_id)

    checkin_rows = connection.execute(
        """
        SELECT id, task_id FROM secretary_task_checkins
        WHERE workspace_id = ? AND created_at >= ? AND created_at < ?
        ORDER BY created_at, id LIMIT ?
        """,
        (workspace_id, start_iso, end_iso, MAX_ANALYSIS_DOMAIN_ROWS + 1),
    ).fetchall()
    if len(checkin_rows) > MAX_ANALYSIS_DOMAIN_ROWS:
        raise PocketError(413, "所选期间任务业务记录过多，请缩短分析范围")
    missing_checkin_events = sum(
        1
        for row in checkin_rows
        if str(row["task_id"]) in task_ids
        and str(row["id"]) not in observed_checkin_ids
    )
    agreement_decision_rows = connection.execute(
        """
        SELECT decision.id, agreement.task_id
        FROM secretary_task_alignment_decisions decision
        JOIN secretary_task_alignment_cases agreement
          ON agreement.id = decision.case_id
        WHERE agreement.workspace_id = ?
          AND decision.created_at >= ? AND decision.created_at < ?
        ORDER BY decision.created_at, decision.id LIMIT ?
        """,
        (workspace_id, start_iso, end_iso, MAX_ANALYSIS_DOMAIN_ROWS + 1),
    ).fetchall()
    if len(agreement_decision_rows) > MAX_ANALYSIS_DOMAIN_ROWS:
        raise PocketError(413, "所选期间任务业务记录过多，请缩短分析范围")
    missing_agreement_events = sum(
        1
        for row in agreement_decision_rows
        if str(row["task_id"]) in task_ids
        and str(row["id"]) not in observed_agreement_decision_ids
    )
    change_decision_rows = connection.execute(
        """
        SELECT decision.id, change_record.task_id
        FROM secretary_task_change_decisions decision
        JOIN secretary_task_changes change_record
          ON change_record.id = decision.change_id
        WHERE change_record.workspace_id = ?
          AND decision.created_at >= ? AND decision.created_at < ?
        ORDER BY decision.created_at, decision.id LIMIT ?
        """,
        (workspace_id, start_iso, end_iso, MAX_ANALYSIS_DOMAIN_ROWS + 1),
    ).fetchall()
    if len(change_decision_rows) > MAX_ANALYSIS_DOMAIN_ROWS:
        raise PocketError(413, "所选期间任务业务记录过多，请缩短分析范围")
    missing_change_events = sum(
        1
        for row in change_decision_rows
        if str(row["task_id"]) in task_ids
        and str(row["id"]) not in observed_change_decision_ids
    )
    coverage["domain_rows_without_event"] = (
        missing_checkin_events
        + missing_agreement_events
        + missing_change_events
    )

    rework_by_task: dict[str, int] = {}
    for event in event_rows:
        if event["event_type"] != "task.returned_for_rework":
            continue
        task_id = _event_task_id(event, _event_payload(event))
        if task_id in task_ids:
            rework_by_task[task_id] = rework_by_task.get(task_id, 0) + 1

    task_facts = _empty_task_facts()
    cycle_seconds: list[int] = []
    tasks: list[dict[str, Any]] = []
    current_assignment: dict[str, dict[str, Any]] = {}
    for row in task_rows:
        task_id = str(row["id"])
        outcome = _task_period_outcome(row, end)
        _record_task_fact(task_facts, outcome, str(row["health"]))
        task_facts["rework_event_count"] += rework_by_task.get(task_id, 0)
        if row["started_at"] and row["accepted_at"]:
            started_at = parse_utc(str(row["started_at"]))
            accepted_at = parse_utc(str(row["accepted_at"]))
            if start <= accepted_at < end and accepted_at >= started_at:
                cycle_seconds.append(int((accepted_at - started_at).total_seconds()))
        assignee_id = (
            str(row["assignee_member_id"])
            if row["assignee_member_id"] is not None
            else None
        )
        if assignee_id is not None:
            snapshot = current_assignment.setdefault(
                assignee_id,
                {
                    "task_ids": [],
                    "task_count": 0,
                    "current_stage_counts": _current_stage_counts(),
                },
            )
            snapshot["task_ids"].append(task_id)
            snapshot["task_count"] += 1
            snapshot["current_stage_counts"][str(row["stage"])] += 1
        tasks.append(
            {
                "task_id": task_id,
                "title": row["title"],
                "assignee_member_id": assignee_id,
                "assignee_label": row["assignee_label"],
                "assignment_epoch": row["assignment_epoch"],
                "current_stage": row["stage"],
                "current_health": row["health"],
                "progress": row["progress"],
                "due_at": row["due_at"],
                "started_at": row["started_at"],
                "submitted_at": row["submitted_at"],
                "accepted_at": row["accepted_at"],
                "period_outcome": outcome,
                "rework_event_count": rework_by_task.get(task_id, 0),
                "attribution_evidence": {
                    "actions": _finish_actions(task_actions[task_id])
                },
            }
        )

    if cycle_seconds:
        task_facts["median_start_to_accept_seconds"] = int(median_low(cycle_seconds))
        task_facts["start_to_accept_sample_count"] = len(cycle_seconds)

    relevant_member_ids = set(current_assignment) | set(member_actions)
    assignees: list[dict[str, Any]] = []
    for member_id in sorted(
        relevant_member_ids,
        key=lambda item: (
            str(members_by_id[item]["display_name"]) if item in members_by_id else item,
            item,
        ),
    ):
        member = members_by_id.get(member_id)
        snapshot = current_assignment.get(
            member_id,
            {
                "task_ids": [],
                "task_count": 0,
                "current_stage_counts": _current_stage_counts(),
            },
        )
        snapshot["task_ids"].sort()
        assignees.append(
            {
                "member_id": member_id,
                "display_name": (
                    member["display_name"] if member is not None else member_id
                ),
                "kind": member["kind"] if member is not None else "external",
                "active": bool(member["active"]) if member is not None else False,
                "current_assignment_snapshot": snapshot,
                "attribution_evidence": {
                    "actions": _finish_actions(
                        member_actions.get(member_id, _empty_action_map())
                    )
                },
            }
        )

    tasks.sort(
        key=lambda item: (
            item["period_outcome"] not in {"open_overdue", "abnormal_closed"},
            item["due_at"] or "9999-12-31T23:59:59Z",
            item["task_id"],
        )
    )
    return {
        "schema": TASK_ANALYSIS_SCHEMA,
        "generated_at": snapshot_at,
        "period": {
            "from": from_date.isoformat(),
            "to": to_date.isoformat(),
            "start_at": start_iso,
            "end_exclusive_at": end_iso,
            "workspace_timezone": timezone_name,
            "task_scope": "current_assignment_overlapping_period",
        },
        "assurance_policy": {
            "id": ASSURANCE_POLICY_ID,
            "version": ASSURANCE_POLICY_VERSION,
            "weight_unit": "basis_points_per_event",
            "levels": [
                {
                    "level": "A2",
                    "tier": "a2_owner_control",
                    "weight_basis_points": 10_000,
                    "claim": "authenticated_owner_control",
                    "person_identity_verified": False,
                },
                {
                    "level": "A1",
                    "tier": "a1_capability",
                    "weight_basis_points": 5_000,
                    "claim": "capability_only",
                    "person_identity_verified": False,
                },
                {
                    "level": "A0",
                    "tier": "a0_unknown",
                    "weight_basis_points": 0,
                    "claim": "unknown_or_unclassified",
                    "person_identity_verified": False,
                },
            ],
            "warning": (
                "权重是不同保证事件的展示折扣，不是身份概率或绩效分。A1 仅证明任务能力"
                "凭据被持有，不证明指定自然人或企业亲自操作，也不构成电子签名。"
            ),
        },
        "coverage": coverage,
        "task_facts": task_facts,
        "assignees": assignees,
        "tasks": tasks,
    }
