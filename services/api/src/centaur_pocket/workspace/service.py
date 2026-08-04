from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import unicodedata
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..database import Database
from ..service import PocketError, json_loads, new_id, parse_utc, utc_now
from .analytics import build_task_analysis
from .state_machine import require_task_transition, transition_timestamp_field

DEFAULT_WORKSPACE_ID = "ws_default"
DEFAULT_OWNER_ID = "member_owner"
ALIGNMENT_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ALIGNMENT_CODE_LENGTH = 12
ALIGNMENT_INVITATION_TTL_SECONDS = 10 * 60
TASK_ACCESS_SESSION_TTL_SECONDS = 10 * 60
TASK_SESSION_ACCESS_TOKEN_DOMAIN = b"centaur-pocket/task-session-token/v1\x00"
TASK_CHANGE_SESSION_ACCESS_TOKEN_DOMAIN = (
    b"centaur-pocket/task-change-session-token/v1\x00"
)
TASK_EXECUTION_SESSION_ACCESS_TOKEN_DOMAIN = (
    b"centaur-pocket/task-execution-session-token/v1\x00"
)
TASK_EXECUTION_REFRESH_TOKEN_DOMAIN = (
    b"centaur-pocket/task-execution-refresh-token/v1\x00"
)
TASK_EXECUTION_ACCESS_TTL_SECONDS = 10 * 60
TASK_EXECUTION_REFRESH_ABSOLUTE_TTL_SECONDS = 7 * 24 * 60 * 60
TASK_EXECUTION_REFRESH_IDLE_TTL_SECONDS = 24 * 60 * 60
TASK_EXECUTION_DUE_GRACE_SECONDS = 7 * 24 * 60 * 60
TASK_AGREEMENT_MAX_REVISION_BYTES = 3 * 1024 * 1024
TASK_AGREEMENT_MAX_CASE_BYTES = 4 * 1024 * 1024
TASK_AGREEMENT_MAX_REVISIONS = 100
ALIGNMENT_MAX_FAILED_ATTEMPTS = 5
TASK_AGREEMENT_SCHEMA = "centaur.task-agreement.v1"
TASK_AGREEMENT_DOCUMENT_KEYS = frozenset(
    {
        "schema",
        "workspace_id",
        "task_id",
        "agreement_id",
        "revision_no",
        "parent_digest",
        "proposer_role",
        "proposer_member_id",
        "responder_role",
        "responder_member_id",
        "issuer_member_id",
        "assignee_member_id",
        "acceptance_owner_id",
        "domain",
        "tier",
        "priority",
        "title",
        "purpose",
        "objective",
        "strategy",
        "key_points",
        "acceptance_criteria",
        "due_at",
    }
)
TASK_CHANGE_SCHEMA = "centaur.task-change.v1"
TASK_CHANGE_DOCUMENT_KEYS = frozenset(
    {
        "schema",
        "workspace_id",
        "task_id",
        "change_id",
        "change_type",
        "base_task_version",
        "proposer_role",
        "proposer_member_id",
        "responder_role",
        "responder_member_id",
        "before",
        "patch",
        "reason",
    }
)
TASK_CHANGE_PATCH_KEYS = {
    "assignee": frozenset({"assignee_member_id"}),
    "due_at": frozenset({"due_at"}),
    "acceptance_criteria": frozenset({"acceptance_criteria"}),
    "abnormal_close": frozenset({"abnormal_close_reason"}),
}
TEMPLATE_PLACEHOLDER = re.compile(r"{{\s*([\w.-]{1,100})\s*}}", re.UNICODE)
TASK_EXECUTION_ETAG_PATTERN = re.compile(
    r'^"task-execution-v1-[0-9a-f]{64}"$'
)
DOCUMENT_IDEMPOTENCY_REFERENCE_KEY = "__centaur_document_reference_v1"
TERMINAL_TASK_STAGES = frozenset({"accepted", "abnormal_closed"})
ATTENTION_TASK_STAGES = frozenset({"aligned", "in_progress"})
WORKSPACE_SCHEMA_VERSION = 7
TASK_CHANGE_V6_TABLES = frozenset(
    {
        "secretary_task_change_proposals",
        "secretary_task_change_invitations",
        "secretary_task_change_sessions",
        "secretary_task_change_decisions",
    }
)
TASK_CHANGE_V6_INDEXES = frozenset(
    {
        "idx_secretary_change_proposal_task",
        "idx_secretary_one_pending_protocol_change_per_task",
        "idx_secretary_one_active_change_invitation",
        "idx_secretary_change_invitation_request",
        "idx_secretary_change_invitation_expiry",
        "idx_secretary_one_active_change_session",
        "idx_secretary_change_session_expiry",
    }
)
TASK_CHANGE_V6_TRIGGERS = frozenset(
    {
        "trg_secretary_change_proposal_binding_insert",
        "trg_secretary_change_proposal_immutable_update",
        "trg_secretary_change_proposal_immutable_delete",
        "trg_secretary_bound_change_fields_immutable",
        "trg_secretary_bound_change_state_transition",
        "trg_secretary_bound_change_immutable_delete",
        "trg_secretary_change_decision_binding_insert",
        "trg_secretary_change_decision_immutable_update",
        "trg_secretary_change_decision_immutable_delete",
        "trg_secretary_change_invitation_binding_insert",
        "trg_secretary_change_invitation_binding_update",
        "trg_secretary_change_session_binding_insert",
        "trg_secretary_change_session_binding_update",
    }
)
TASK_EXECUTION_V7_TABLES = frozenset(
    {
        "secretary_task_execution_invitations",
        "secretary_task_execution_sessions",
        "secretary_task_execution_refresh_families",
        "secretary_task_execution_refresh_tokens",
    }
)
TASK_EXECUTION_V7_INDEXES = frozenset(
    {
        "idx_secretary_one_active_execution_invitation",
        "idx_secretary_execution_invitation_request",
        "idx_secretary_execution_invitation_expiry",
        "idx_secretary_one_active_execution_session",
        "idx_secretary_execution_session_expiry",
        "idx_secretary_one_active_execution_refresh_family",
        "idx_secretary_execution_refresh_family_expiry",
        "idx_secretary_execution_refresh_token_family",
        "idx_secretary_execution_refresh_token_expiry",
    }
)
TASK_EXECUTION_V7_TRIGGERS = frozenset(
    {
        "trg_secretary_execution_invitation_binding_insert",
        "trg_secretary_execution_invitation_binding_update",
        "trg_secretary_execution_session_binding_insert",
        "trg_secretary_execution_session_binding_update",
        "trg_secretary_execution_refresh_family_binding_insert",
        "trg_secretary_execution_refresh_family_binding_update",
        "trg_secretary_execution_refresh_token_binding_insert",
        "trg_secretary_execution_refresh_token_binding_update",
        "trg_secretary_task_assignment_epoch_update",
        "trg_secretary_task_execution_access_revoke",
        "trg_secretary_member_execution_access_revoke",
    }
)
# Exact SHA-256 values of whitespace-collapsed sqlite_master SQL emitted by
# the v7 migration statements below. Literal case is deliberately preserved;
# any DDL change must update its canonical statement and this digest together.
TASK_EXECUTION_V7_SQL_DIGESTS = {
    "idx_secretary_execution_invitation_expiry": (
        "9d0f285f0049c3b6d3a0a178393c92997549642d25fcd92245bfd863f618a858"
    ),
    "idx_secretary_execution_invitation_request": (
        "7cc5d1e5ede232cb631c1c8400d668e208d2ee201ae7ae224f889d28ad440c11"
    ),
    "idx_secretary_execution_refresh_family_expiry": (
        "cd7b2db3eb82d0500df115158e3861765437fcd4ab18e48e1d80c18dce9e4221"
    ),
    "idx_secretary_execution_refresh_token_expiry": (
        "67d99ed08904be4fbebad9e00119b5a0a7875d89ad386c2a916b9d9e28205584"
    ),
    "idx_secretary_execution_refresh_token_family": (
        "8ce4f96cca0904bed430ec0524a47159e47fd41e8ba6f6dabde13a91c82b4cfc"
    ),
    "idx_secretary_execution_session_expiry": (
        "e0fd881d6c8065e2c7f9b7bc4a3e575b5bd0a69d5f3ec67f27a31ccea99308e3"
    ),
    "idx_secretary_one_active_execution_invitation": (
        "1bdc7789395b1a51bac646d8303d42e733e3c2a3f5dae6d262c92f40788a3bbb"
    ),
    "idx_secretary_one_active_execution_refresh_family": (
        "2c6630d3e199cf0b7a74a6ffa761bb614eb297d03bcafb87c2d697edce3fda5a"
    ),
    "idx_secretary_one_active_execution_session": (
        "a4d139d6c97e93efc0fb9f6954279dcb1f60326e28dd50f0d309f73ce3bdde72"
    ),
    "secretary_task_execution_invitations": (
        "c788c9ad7f2c76232cda5d1374b2729c9db3f562cefdeef59d05ede18c56cbaf"
    ),
    "secretary_task_execution_refresh_families": (
        "df6821a0efe0b943d7afa2c8b7b447ff4002ef6af2e7adf7ffc9875985bb4f1d"
    ),
    "secretary_task_execution_refresh_tokens": (
        "37b44d584b76b14afbe9bfefde8c2f813b316f96cf4c6e8efa587d78b58bd49c"
    ),
    "secretary_task_execution_sessions": (
        "18c13c5969a3a176ffee6c4af49db0f1458ca0a24a7079362dc867fcc007e51c"
    ),
    "trg_secretary_execution_invitation_binding_insert": (
        "a81d1e8559d5e8b47fc2ea68f11c785aa2222a75614a91480e13014a878f6151"
    ),
    "trg_secretary_execution_invitation_binding_update": (
        "5da19633b464b29e70f889221d9111dc53baaf446f9953f96eaa880c2df5d5f3"
    ),
    "trg_secretary_execution_refresh_family_binding_insert": (
        "23d09b0ed87c95b7f4c75537f0c8d80ba30da4cdbcec41b01b7daf2617d3d69c"
    ),
    "trg_secretary_execution_refresh_family_binding_update": (
        "c3f91df1cdbdb0949525fc121fae8607349fa861bc80e6a12c1dc127c6a5c596"
    ),
    "trg_secretary_execution_refresh_token_binding_insert": (
        "96476bf0bbfb11eac20eedb3039322dd5d47c7d9a55544c623875abdf281bbcb"
    ),
    "trg_secretary_execution_refresh_token_binding_update": (
        "cf8ac59e7ce4c928006d54c625d156cb882d2c6bc4f5b0737364bc4f418c5bdb"
    ),
    "trg_secretary_execution_session_binding_insert": (
        "ceb8b36fa98fcbd7e1b5edd40b66540d30c696e6a731d2196fbd4dcd2061250c"
    ),
    "trg_secretary_execution_session_binding_update": (
        "6b2e2946cbd79fe37a860614af56dd0ebce9b7f7e233ef5b5d4c600dee276840"
    ),
    "trg_secretary_member_execution_access_revoke": (
        "55ec73174a2503318a393cb8a395cc5c20eb0de7069cc035f5314161ec84f74c"
    ),
    "trg_secretary_task_assignment_epoch_update": (
        "e5137d14f5550c2f1810826758e8da80c5d8288b5216359350ffad8cdcf0444a"
    ),
    "trg_secretary_task_execution_access_revoke": (
        "e87d25a33dc58e0428889d781b53509f6936d0d8c08ffadc12eb00a1bee95985"
    ),
}


WORKSPACE_SCHEMA = """
CREATE TABLE IF NOT EXISTS secretary_workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    timezone TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS secretary_workspace_schema_migrations (
    version INTEGER PRIMARY KEY CHECK(version >= 1),
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS secretary_workspace_members (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES secretary_workspaces(id),
    kind TEXT NOT NULL CHECK(kind IN ('person', 'team', 'external')),
    role TEXT NOT NULL CHECK(role IN ('owner', 'member', 'viewer')),
    display_name TEXT NOT NULL,
    contact_ref TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_secretary_one_owner
ON secretary_workspace_members(workspace_id)
WHERE role = 'owner' AND active = 1;

CREATE TABLE IF NOT EXISTS secretary_memos (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES secretary_workspaces(id),
    record_type TEXT NOT NULL CHECK(record_type IN ('note', 'task_candidate')),
    domain TEXT NOT NULL CHECK(domain IN ('work', 'personal')),
    horizon TEXT NOT NULL CHECK(horizon IN ('short_term', 'long_term', 'ongoing')),
    urgency TEXT NOT NULL CHECK(urgency IN ('low', 'normal', 'high', 'critical')),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    due_at TEXT,
    source_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(source_json)),
    authority TEXT NOT NULL CHECK(authority IN (
        'authoritative', 'observed', 'user_provided', 'inferred'
    )),
    confirmation_status TEXT NOT NULL CHECK(confirmation_status IN (
        'not_required', 'pending', 'confirmed', 'rejected'
    )),
    status TEXT NOT NULL CHECK(status IN ('inbox', 'active', 'converted', 'archived')),
    tags_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(tags_json)),
    pinned INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    created_by TEXT NOT NULL REFERENCES secretary_workspace_members(id),
    updated_by TEXT NOT NULL REFERENCES secretary_workspace_members(id),
    client_mutation_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_secretary_memos_workspace_updated
ON secretary_memos(workspace_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS secretary_business_tasks (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES secretary_workspaces(id),
    origin_memo_id TEXT UNIQUE REFERENCES secretary_memos(id),
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL DEFAULT '',
    objective TEXT NOT NULL DEFAULT '',
    strategy TEXT NOT NULL DEFAULT '',
    key_points_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(key_points_json)),
    acceptance_criteria_json TEXT NOT NULL DEFAULT '[]'
        CHECK(json_valid(acceptance_criteria_json)),
    issuer_member_id TEXT REFERENCES secretary_workspace_members(id),
    assignee_member_id TEXT REFERENCES secretary_workspace_members(id),
    acceptance_owner_id TEXT REFERENCES secretary_workspace_members(id),
    issuer_label TEXT NOT NULL,
    assignee_label TEXT NOT NULL,
    acceptance_owner_label TEXT NOT NULL,
    start_at TEXT,
    due_at TEXT,
    stage TEXT NOT NULL CHECK(stage IN (
        'draft', 'issued', 'aligned', 'in_progress', 'submitted',
        'accepted', 'abnormal_closed'
    )),
    health TEXT NOT NULL CHECK(health IN ('on_track', 'at_risk', 'blocked', 'overdue')),
    tier TEXT NOT NULL CHECK(tier IN ('quick', 'standard', 'strategic')),
    domain TEXT NOT NULL CHECK(domain IN ('work', 'personal')),
    priority TEXT NOT NULL CHECK(priority IN ('low', 'normal', 'high', 'critical')),
    progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
    requires_alignment INTEGER NOT NULL DEFAULT 1 CHECK(requires_alignment IN (0, 1)),
    source_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(source_json)),
    started_at TEXT,
    submitted_at TEXT,
    accepted_at TEXT,
    abnormal_close_reason TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    created_by TEXT NOT NULL REFERENCES secretary_workspace_members(id),
    updated_by TEXT NOT NULL REFERENCES secretary_workspace_members(id),
    client_mutation_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_secretary_tasks_workspace_stage
ON secretary_business_tasks(workspace_id, stage, updated_at DESC);

CREATE TABLE IF NOT EXISTS secretary_task_alignment_invitations (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES secretary_workspaces(id),
    task_id TEXT NOT NULL REFERENCES secretary_business_tasks(id) ON DELETE CASCADE,
    task_version INTEGER NOT NULL CHECK(task_version >= 1),
    assignee_member_id TEXT NOT NULL REFERENCES secretary_workspace_members(id),
    code_hash TEXT NOT NULL UNIQUE CHECK(length(code_hash) = 64),
    failed_attempts INTEGER NOT NULL DEFAULT 0 CHECK(failed_attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 5 CHECK(max_attempts >= 1),
    created_by TEXT NOT NULL REFERENCES secretary_workspace_members(id),
    created_device_id TEXT NOT NULL,
    creation_idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    code_used_at TEXT,
    confirmation_token_hash TEXT UNIQUE
        CHECK(confirmation_token_hash IS NULL OR length(confirmation_token_hash) = 64),
    confirmation_expires_at TEXT,
    confirmation_consumed_at TEXT,
    consumed_at TEXT,
    revoked_at TEXT,
    confirmed_by_member_id TEXT REFERENCES secretary_workspace_members(id),
    confirmed_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_secretary_one_active_alignment_invitation
ON secretary_task_alignment_invitations(task_id)
WHERE consumed_at IS NULL AND revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_secretary_alignment_invitation_expiry
ON secretary_task_alignment_invitations(expires_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_secretary_alignment_creation_request
ON secretary_task_alignment_invitations(
    workspace_id, created_by, task_id, creation_idempotency_key
);

CREATE TABLE IF NOT EXISTS secretary_task_steps (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES secretary_workspaces(id),
    task_id TEXT NOT NULL REFERENCES secretary_business_tasks(id) ON DELETE CASCADE,
    parent_step_id TEXT REFERENCES secretary_task_steps(id),
    step_type TEXT NOT NULL CHECK(step_type IN ('key_result', 'milestone', 'action')),
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    assignee_member_id TEXT REFERENCES secretary_workspace_members(id),
    assignee_label TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'pending', 'in_progress', 'blocked', 'done', 'canceled'
    )),
    position INTEGER NOT NULL CHECK(position >= 0),
    due_at TEXT,
    success_metric_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(success_metric_json)),
    schedule_id TEXT,
    completed_at TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    client_mutation_id TEXT,
    deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_secretary_steps_task_position
ON secretary_task_steps(task_id, position);

CREATE TABLE IF NOT EXISTS secretary_task_checkins (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES secretary_workspaces(id),
    task_id TEXT NOT NULL REFERENCES secretary_business_tasks(id) ON DELETE CASCADE,
    task_version INTEGER NOT NULL CHECK(task_version >= 1),
    report_date TEXT NOT NULL CHECK(
        length(report_date) = 10 AND date(report_date) = report_date
    ),
    summary TEXT NOT NULL CHECK(length(summary) BETWEEN 1 AND 4000),
    reported_progress INTEGER NOT NULL CHECK(reported_progress BETWEEN 0 AND 100),
    risks_json TEXT NOT NULL DEFAULT '[]'
        CHECK(json_valid(risks_json) AND json_type(risks_json) = 'array'),
    blockers_json TEXT NOT NULL DEFAULT '[]'
        CHECK(json_valid(blockers_json) AND json_type(blockers_json) = 'array'),
    next_actions_json TEXT NOT NULL DEFAULT '[]'
        CHECK(json_valid(next_actions_json) AND json_type(next_actions_json) = 'array'),
    forecast_at TEXT,
    created_by TEXT NOT NULL REFERENCES secretary_workspace_members(id),
    device_id TEXT NOT NULL CHECK(length(device_id) BETWEEN 1 AND 200),
    client_mutation_id TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK(version = 1),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_secretary_checkins_task_created
ON secretary_task_checkins(task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS secretary_task_step_dependencies (
    step_id TEXT NOT NULL REFERENCES secretary_task_steps(id) ON DELETE CASCADE,
    depends_on_step_id TEXT NOT NULL REFERENCES secretary_task_steps(id) ON DELETE CASCADE,
    PRIMARY KEY(step_id, depends_on_step_id),
    CHECK(step_id <> depends_on_step_id)
);

CREATE TABLE IF NOT EXISTS secretary_task_changes (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES secretary_workspaces(id),
    task_id TEXT NOT NULL REFERENCES secretary_business_tasks(id) ON DELETE CASCADE,
    change_type TEXT NOT NULL CHECK(change_type IN (
        'assignee', 'due_at', 'acceptance_criteria', 'abnormal_close'
    )),
    base_version INTEGER NOT NULL CHECK(base_version >= 1),
    before_json TEXT NOT NULL CHECK(json_valid(before_json)),
    patch_json TEXT NOT NULL CHECK(json_valid(patch_json)),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('proposed', 'accepted', 'rejected', 'canceled')),
    proposed_by TEXT NOT NULL REFERENCES secretary_workspace_members(id),
    decided_by TEXT REFERENCES secretary_workspace_members(id),
    proposed_at TEXT NOT NULL,
    decided_at TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    client_mutation_id TEXT,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_secretary_one_pending_change
ON secretary_task_changes(task_id, change_type)
WHERE status = 'proposed';

CREATE TABLE IF NOT EXISTS secretary_workspace_evidence (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES secretary_workspaces(id),
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    source_json TEXT NOT NULL CHECK(json_valid(source_json)),
    excerpt TEXT NOT NULL DEFAULT '',
    authority TEXT NOT NULL CHECK(authority IN (
        'authoritative', 'observed', 'user_provided', 'inferred'
    )),
    observed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS secretary_documents (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES secretary_workspaces(id),
    source_item_id TEXT REFERENCES items(id),
    origin_template_id TEXT REFERENCES secretary_documents(id),
    origin_template_version INTEGER CHECK(
        origin_template_version IS NULL OR origin_template_version >= 1
    ),
    domain TEXT NOT NULL CHECK(domain IN ('work', 'personal')),
    kind TEXT NOT NULL CHECK(kind IN (
        'general', 'contract', 'work_report', 'template'
    )),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    storage_ref TEXT,
    source_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(source_json)),
    access_scope TEXT NOT NULL CHECK(access_scope IN (
        'owner_only', 'workspace', 'restricted'
    )),
    viewer_member_ids_json TEXT NOT NULL DEFAULT '[]'
        CHECK(json_valid(viewer_member_ids_json)),
    status TEXT NOT NULL CHECK(status IN (
        'draft', 'review_pending', 'reviewed', 'archived'
    )),
    tags_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(tags_json)),
    template_variables_json TEXT NOT NULL DEFAULT '{}'
        CHECK(json_valid(template_variables_json)),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    created_by TEXT NOT NULL REFERENCES secretary_workspace_members(id),
    updated_by TEXT NOT NULL REFERENCES secretary_workspace_members(id),
    client_mutation_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    CHECK(
        (origin_template_id IS NULL AND origin_template_version IS NULL)
        OR
        (kind <> 'template' AND origin_template_id IS NOT NULL
            AND origin_template_version IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_secretary_documents_workspace_updated
ON secretary_documents(workspace_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS secretary_document_reviews (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES secretary_workspaces(id),
    document_id TEXT NOT NULL REFERENCES secretary_documents(id) ON DELETE CASCADE,
    document_version INTEGER NOT NULL CHECK(document_version >= 1),
    review_type TEXT NOT NULL CHECK(review_type IN ('contract', 'work_report')),
    summary TEXT NOT NULL,
    conclusion TEXT NOT NULL CHECK(conclusion IN (
        'approved', 'approved_with_changes', 'changes_required', 'rejected'
    )),
    findings_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(findings_json)),
    reviewer_member_id TEXT NOT NULL REFERENCES secretary_workspace_members(id),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    client_mutation_id TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_secretary_document_reviews_document_created
ON secretary_document_reviews(document_id, created_at DESC);

CREATE TABLE IF NOT EXISTS secretary_document_excerpts (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES secretary_workspaces(id),
    document_id TEXT NOT NULL REFERENCES secretary_documents(id) ON DELETE CASCADE,
    source_document_version INTEGER NOT NULL CHECK(source_document_version >= 1),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    start_offset INTEGER NOT NULL CHECK(start_offset >= 0),
    end_offset INTEGER NOT NULL CHECK(end_offset > start_offset),
    viewer_member_ids_json TEXT NOT NULL CHECK(json_valid(viewer_member_ids_json)),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    created_by TEXT NOT NULL REFERENCES secretary_workspace_members(id),
    client_mutation_id TEXT,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_secretary_document_excerpts_document_created
ON secretary_document_excerpts(document_id, created_at DESC);

CREATE TABLE IF NOT EXISTS secretary_calendar_entries (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES secretary_workspaces(id),
    memo_id TEXT REFERENCES secretary_memos(id),
    task_id TEXT REFERENCES secretary_business_tasks(id),
    step_id TEXT REFERENCES secretary_task_steps(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    start_at_utc TEXT NOT NULL,
    end_at_utc TEXT NOT NULL,
    timezone TEXT NOT NULL,
    all_day INTEGER NOT NULL DEFAULT 0 CHECK(all_day IN (0, 1)),
    kind TEXT NOT NULL CHECK(kind IN ('focus', 'meeting', 'reminder')),
    domain TEXT NOT NULL CHECK(domain IN ('work', 'personal')),
    status TEXT NOT NULL CHECK(status IN ('scheduled', 'completed', 'canceled')),
    attendees_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(attendees_json)),
    external_provider TEXT,
    external_id TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    created_by TEXT NOT NULL REFERENCES secretary_workspace_members(id),
    updated_by TEXT NOT NULL REFERENCES secretary_workspace_members(id),
    client_mutation_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    CHECK(
        (CASE WHEN memo_id IS NULL THEN 0 ELSE 1 END) +
        (CASE WHEN task_id IS NULL THEN 0 ELSE 1 END) +
        (CASE WHEN step_id IS NULL THEN 0 ELSE 1 END) <= 1
    )
);

CREATE INDEX IF NOT EXISTS idx_secretary_calendar_workspace_start
ON secretary_calendar_entries(workspace_id, start_at_utc);

CREATE UNIQUE INDEX IF NOT EXISTS idx_secretary_calendar_external
ON secretary_calendar_entries(workspace_id, external_provider, external_id)
WHERE external_provider IS NOT NULL AND external_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS secretary_meetings (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES secretary_workspaces(id),
    calendar_entry_id TEXT UNIQUE REFERENCES secretary_calendar_entries(id),
    related_task_id TEXT REFERENCES secretary_business_tasks(id),
    domain TEXT NOT NULL CHECK(domain IN ('work', 'personal')),
    title TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    agenda_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(agenda_json)),
    starts_at_utc TEXT NOT NULL,
    ends_at_utc TEXT NOT NULL,
    timezone TEXT NOT NULL,
    organizer_member_id TEXT REFERENCES secretary_workspace_members(id),
    location TEXT,
    provider TEXT,
    external_id TEXT,
    status TEXT NOT NULL CHECK(status IN (
        'planned', 'in_progress', 'ended', 'minutes_pending',
        'minutes_confirmed', 'canceled'
    )),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    created_by TEXT NOT NULL REFERENCES secretary_workspace_members(id),
    updated_by TEXT NOT NULL REFERENCES secretary_workspace_members(id),
    client_mutation_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_secretary_meetings_workspace_start
ON secretary_meetings(workspace_id, starts_at_utc);

CREATE TABLE IF NOT EXISTS secretary_meeting_participants (
    meeting_id TEXT NOT NULL REFERENCES secretary_meetings(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('organizer', 'required', 'optional')),
    rsvp TEXT NOT NULL CHECK(rsvp IN ('pending', 'accepted', 'declined', 'tentative')),
    minutes_confirmation_required INTEGER NOT NULL DEFAULT 1
        CHECK(minutes_confirmation_required IN (0, 1)),
    PRIMARY KEY(meeting_id, member_id)
);

CREATE TABLE IF NOT EXISTS secretary_meeting_minutes (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES secretary_workspaces(id),
    meeting_id TEXT NOT NULL REFERENCES secretary_meetings(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    content TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'draft', 'confirming', 'confirmed', 'disputed', 'superseded'
    )),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    created_by TEXT NOT NULL REFERENCES secretary_workspace_members(id),
    client_mutation_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(meeting_id, revision)
);

CREATE TABLE IF NOT EXISTS secretary_meeting_minute_confirmations (
    minutes_id TEXT NOT NULL REFERENCES secretary_meeting_minutes(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'confirmed', 'disputed')),
    comment TEXT,
    decided_at TEXT,
    PRIMARY KEY(minutes_id, member_id)
);

CREATE TABLE IF NOT EXISTS secretary_workspace_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    workspace_id TEXT NOT NULL REFERENCES secretary_workspaces(id),
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    aggregate_version INTEGER NOT NULL CHECK(aggregate_version >= 1),
    event_type TEXT NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN ('upsert', 'delete')),
    actor_type TEXT NOT NULL CHECK(actor_type IN ('owner', 'agent', 'system', 'member')),
    actor_member_id TEXT REFERENCES secretary_workspace_members(id),
    device_id TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    occurred_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_secretary_events_workspace_sequence
ON secretary_workspace_events(workspace_id, sequence);

CREATE TABLE IF NOT EXISTS secretary_workspace_sync_cursors (
    workspace_id TEXT NOT NULL REFERENCES secretary_workspaces(id),
    device_id TEXT NOT NULL,
    last_sequence INTEGER NOT NULL DEFAULT 0 CHECK(last_sequence >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(workspace_id, device_id)
);

CREATE TABLE IF NOT EXISTS secretary_workspace_idempotency (
    workspace_id TEXT NOT NULL REFERENCES secretary_workspaces(id),
    actor_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response_json TEXT NOT NULL CHECK(json_valid(response_json)),
    response_headers_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(response_headers_json)),
    created_at TEXT NOT NULL,
    PRIMARY KEY(workspace_id, actor_id, operation, idempotency_key)
);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_request(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _memo_source_snapshot(
    row: sqlite3.Row, *, source_memo_version: int | None = None
) -> str:
    source = json_loads(row["source_json"], {})
    if not isinstance(source, dict):
        source = {}
    source_json_digest = hashlib.sha256(
        str(row["source_json"]).encode("utf-8")
    ).hexdigest()
    content_digest = hashlib.sha256(str(row["content"]).encode("utf-8")).hexdigest()
    return _json(
        {
            "schema": "centaur.memo-source-snapshot.v1",
            "memo_id": row["id"],
            "workspace_id": row["workspace_id"],
            "source_memo_version": source_memo_version or row["version"],
            "domain": row["domain"],
            "authority": row["authority"],
            "source_kind": source.get("source_kind"),
            "source_ref": source.get("source_ref"),
            "source_json_digest": f"sha256:{source_json_digest}",
            "content_digest": f"sha256:{content_digest}",
        }
    )


def _iso_datetime(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
    else:
        parsed = value
    if parsed.utcoffset() is None:
        raise PocketError(422, "时间必须包含时区")
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _as_of_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.utcoffset() is None:
        raise ValueError("as_of 必须包含时区")
    return current.astimezone(UTC)


def _new_alignment_code() -> str:
    compact = "".join(
        secrets.choice(ALIGNMENT_CODE_ALPHABET) for _ in range(ALIGNMENT_CODE_LENGTH)
    )
    return "-".join(
        compact[index : index + 4] for index in range(0, ALIGNMENT_CODE_LENGTH, 4)
    )


def _normalized_alignment_code(value: str) -> str | None:
    compact = "".join(
        character for character in value.upper() if character not in "- \t\r\n"
    )
    if len(compact) != ALIGNMENT_CODE_LENGTH or any(
        character not in ALIGNMENT_CODE_ALPHABET for character in compact
    ):
        return None
    return "-".join(
        compact[index : index + 4] for index in range(0, ALIGNMENT_CODE_LENGTH, 4)
    )


def _secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_agreement_value(value: Any) -> Any:
    """Normalize the closed task-agreement JSON value domain."""
    if isinstance(value, float):
        raise PocketError(422, "任务协议文档不允许浮点数")
    if isinstance(value, datetime):
        return _iso_datetime(value)
    if isinstance(value, str):
        line_normalized = value.replace("\r\n", "\n").replace("\r", "\n")
        normalized_string = unicodedata.normalize("NFC", line_normalized)
        try:
            normalized_string.encode("utf-8")
        except UnicodeEncodeError as error:
            raise PocketError(422, "任务协议文档包含无效 Unicode") from error
        return normalized_string
    if isinstance(value, list):
        return [_normalize_agreement_value(item) for item in value]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise PocketError(422, "任务协议文档对象键必须是字符串")
            normalized_key = unicodedata.normalize("NFC", key)
            try:
                normalized_key.encode("utf-8")
            except UnicodeEncodeError as error:
                raise PocketError(422, "任务协议文档包含无效 Unicode 键") from error
            if normalized_key in normalized:
                raise PocketError(422, "任务协议文档包含重复规范化键")
            normalized[normalized_key] = _normalize_agreement_value(item)
        return normalized
    if value is None or isinstance(value, (bool, int)):
        return value
    raise PocketError(422, "任务协议文档包含不支持的值")


def _canonical_task_agreement_json(document: dict[str, Any]) -> str:
    if set(document) != TASK_AGREEMENT_DOCUMENT_KEYS:
        raise PocketError(422, "任务协议文档字段不完整或包含额外字段")
    normalized = _normalize_agreement_value(document)
    if not isinstance(normalized, dict):
        raise PocketError(422, "任务协议文档必须是对象")
    if normalized.get("schema") != TASK_AGREEMENT_SCHEMA:
        raise PocketError(422, "任务协议文档 schema 无效")
    revision_no = normalized.get("revision_no")
    if (
        isinstance(revision_no, bool)
        or not isinstance(revision_no, int)
        or revision_no < 1
    ):
        raise PocketError(422, "任务协议修订号无效")
    try:
        normalized["due_at"] = _iso_datetime(normalized.get("due_at"))
    except (TypeError, ValueError) as error:
        raise PocketError(422, "任务协议完成期限无效") from error
    return _json(normalized)


def _task_agreement_digest(document: dict[str, Any]) -> tuple[str, str]:
    canonical_json = _canonical_task_agreement_json(document)
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return canonical_json, f"sha256:{digest}"


def _canonical_task_change_json(document: dict[str, Any]) -> str:
    if set(document) != TASK_CHANGE_DOCUMENT_KEYS:
        raise PocketError(422, "任务变更提案字段不完整或包含额外字段")
    normalized = _normalize_agreement_value(document)
    if not isinstance(normalized, dict):
        raise PocketError(422, "任务变更提案必须是对象")
    if normalized.get("schema") != TASK_CHANGE_SCHEMA:
        raise PocketError(422, "任务变更提案 schema 无效")
    for identifier_key in (
        "workspace_id",
        "task_id",
        "change_id",
        "proposer_member_id",
        "responder_member_id",
    ):
        identifier = normalized.get(identifier_key)
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 200:
            raise PocketError(422, f"任务变更 {identifier_key} 无效")
    reason = normalized.get("reason")
    if not isinstance(reason, str) or not 1 <= len(reason) <= 4_000:
        raise PocketError(422, "任务变更原因无效")
    base_version = normalized.get("base_task_version")
    if (
        isinstance(base_version, bool)
        or not isinstance(base_version, int)
        or base_version < 1
    ):
        raise PocketError(422, "任务变更基线版本无效")
    if normalized.get("proposer_role") != "issuer":
        raise PocketError(422, "任务变更提案方角色无效")
    expected_responder_role = (
        "issuer"
        if normalized["responder_member_id"] == normalized["proposer_member_id"]
        else "assignee"
    )
    if normalized.get("responder_role") != expected_responder_role:
        raise PocketError(422, "任务变更回应方角色无效")
    change_type = normalized.get("change_type")
    expected_patch_keys = TASK_CHANGE_PATCH_KEYS.get(str(change_type))
    patch = normalized.get("patch")
    if expected_patch_keys is None or not isinstance(patch, dict):
        raise PocketError(422, "任务变更类型或 patch 无效")
    if set(patch) != expected_patch_keys:
        raise PocketError(422, "任务变更 patch 与类型不匹配")
    if change_type == "assignee":
        before = normalized.get("before")
        target = patch.get("assignee_member_id")
        if (
            not isinstance(before, str)
            or not 1 <= len(before) <= 200
            or not isinstance(target, str)
            or not 1 <= len(target) <= 200
        ):
            raise PocketError(422, "任务变更承办人无效")
    elif change_type == "due_at":
        try:
            normalized["before"] = _iso_datetime(normalized.get("before"))
            patch["due_at"] = _iso_datetime(patch.get("due_at"))
        except (TypeError, ValueError) as error:
            raise PocketError(422, "任务变更完成期限无效") from error
        if patch["due_at"] is None:
            raise PocketError(422, "任务变更完成期限不能为空")
    elif change_type == "acceptance_criteria":
        before = normalized.get("before")
        target = patch.get("acceptance_criteria")
        if (
            not isinstance(before, list)
            or not isinstance(target, list)
            or not 1 <= len(target) <= 100
            or any(
                not isinstance(item, str) or not 1 <= len(item) <= 2_000
                for item in [*before, *target]
            )
        ):
            raise PocketError(422, "任务变更验收标准无效")
    elif change_type == "abnormal_close":
        before = normalized.get("before")
        target = patch.get("abnormal_close_reason")
        if (
            (before is not None and not isinstance(before, str))
            or not isinstance(target, str)
            or not 1 <= len(target) <= 4_000
        ):
            raise PocketError(422, "任务变更非正常关闭原因无效")
    return _json(normalized)


def _task_change_digest(document: dict[str, Any]) -> tuple[str, str]:
    canonical_json = _canonical_task_change_json(document)
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return canonical_json, f"sha256:{digest}"


class WorkspaceService:
    def __init__(self, database: Database, *, task_session_hmac_key: bytes):
        if (
            not isinstance(task_session_hmac_key, bytes)
            or len(task_session_hmac_key) != 32
        ):
            raise ValueError("task_session_hmac_key 必须是 32 字节")
        self.database = database
        self._task_session_hmac_key = task_session_hmac_key

    def _task_session_access_token(
        self,
        *,
        session_id: str,
        exchange_idempotency_hash: str,
        exchange_request_hash: str,
    ) -> str:
        message = TASK_SESSION_ACCESS_TOKEN_DOMAIN + b"\x00".join(
            (
                session_id.encode("utf-8"),
                exchange_idempotency_hash.encode("ascii"),
                exchange_request_hash.encode("ascii"),
            )
        )
        digest = hmac.digest(self._task_session_hmac_key, message, "sha256")
        encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return f"cp_task_at_{encoded}"

    def _task_change_session_access_token(
        self,
        *,
        session_id: str,
        exchange_idempotency_hash: str,
        exchange_request_hash: str,
    ) -> str:
        message = TASK_CHANGE_SESSION_ACCESS_TOKEN_DOMAIN + b"\x00".join(
            (
                session_id.encode("utf-8"),
                exchange_idempotency_hash.encode("ascii"),
                exchange_request_hash.encode("ascii"),
            )
        )
        digest = hmac.digest(self._task_session_hmac_key, message, "sha256")
        encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return f"cp_task_ch_{encoded}"

    def _task_execution_session_access_token(
        self,
        *,
        session_id: str,
        exchange_idempotency_hash: str,
        exchange_request_hash: str,
    ) -> str:
        message = TASK_EXECUTION_SESSION_ACCESS_TOKEN_DOMAIN + b"\x00".join(
            (
                session_id.encode("utf-8"),
                exchange_idempotency_hash.encode("ascii"),
                exchange_request_hash.encode("ascii"),
            )
        )
        digest = hmac.digest(self._task_session_hmac_key, message, "sha256")
        encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return f"cp_task_ex_{encoded}"

    def _task_execution_refresh_token(
        self,
        *,
        token_id: str,
        family_id: str,
        generation: int,
    ) -> str:
        message = TASK_EXECUTION_REFRESH_TOKEN_DOMAIN + b"\x00".join(
            (
                token_id.encode("utf-8"),
                family_id.encode("utf-8"),
                str(generation).encode("ascii"),
            )
        )
        digest = hmac.digest(self._task_session_hmac_key, message, "sha256")
        encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return f"cp_task_er_{encoded}"

    def initialize(self) -> None:
        now = utc_now()
        with self.database.transaction() as connection:
            connection.executescript(WORKSPACE_SCHEMA)
            # sqlite3.executescript() may close the transaction that wrapped it.
            # Numbered workspace migrations must still be all-or-nothing.
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO secretary_workspaces(
                    id, name, timezone, version, created_at, updated_at
                ) VALUES (?, '半人马AI超级秘书', 'Asia/Shanghai', 1, ?, ?)
                """,
                (DEFAULT_WORKSPACE_ID, now, now),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO secretary_workspace_members(
                    id, workspace_id, kind, role, display_name, active,
                    created_at, updated_at
                ) VALUES (?, ?, 'person', 'owner', '主人', 1, ?, ?)
                """,
                (DEFAULT_OWNER_ID, DEFAULT_WORKSPACE_ID, now, now),
            )
            self._apply_workspace_migrations(connection, now=now)
            # Older builds cached complete document responses for idempotent
            # replay.  Scrub those historical body/source/review copies during
            # startup, retaining only the resource version needed for replay.
            legacy_document_responses = connection.execute(
                """
                SELECT rowid, response_json
                FROM secretary_workspace_idempotency
                WHERE operation LIKE 'document.%'
                """
            ).fetchall()
            for cached_row in legacy_document_responses:
                cached = json_loads(cached_row["response_json"], {})
                if (
                    isinstance(cached, dict)
                    and DOCUMENT_IDEMPOTENCY_REFERENCE_KEY in cached
                ):
                    continue
                document_id = cached.get("id") if isinstance(cached, dict) else None
                document_version = (
                    cached.get("version") if isinstance(cached, dict) else None
                )
                reference = {
                    DOCUMENT_IDEMPOTENCY_REFERENCE_KEY: {
                        "document_id": (
                            document_id if isinstance(document_id, str) else None
                        ),
                        "version": (
                            document_version
                            if isinstance(document_version, int)
                            else None
                        ),
                    }
                }
                connection.execute(
                    """
                    UPDATE secretary_workspace_idempotency
                    SET response_json = ? WHERE rowid = ?
                    """,
                    (_json(reference), cached_row["rowid"]),
                )
            self._verify_workspace_integrity(connection)

    def _apply_workspace_migrations(
        self, connection: sqlite3.Connection, *, now: str
    ) -> None:
        applied = {
            int(row["version"])
            for row in connection.execute(
                "SELECT version FROM secretary_workspace_schema_migrations"
            ).fetchall()
        }
        unknown_versions = sorted(
            version for version in applied if version > WORKSPACE_SCHEMA_VERSION
        )
        if unknown_versions:
            raise RuntimeError(
                "workspace 数据库版本高于当前服务支持版本："
                f"{unknown_versions[0]}"
            )
        if 1 not in applied:
            # Legacy builds allowed duplicate positions. Preserve their visible
            # order while normalizing every active task to a dense sequence.
            task_ids = connection.execute(
                """
                SELECT DISTINCT task_id FROM secretary_task_steps
                WHERE deleted_at IS NULL ORDER BY task_id
                """
            ).fetchall()
            for task_row in task_ids:
                steps = connection.execute(
                    """
                    SELECT id FROM secretary_task_steps
                    WHERE task_id = ? AND deleted_at IS NULL
                    ORDER BY position, created_at, id
                    """,
                    (task_row["task_id"],),
                ).fetchall()
                # Move out of the final non-negative range first so a retried
                # migration also works after a partially-created unique index.
                current_max = connection.execute(
                    """
                    SELECT COALESCE(MAX(position), -1)
                    FROM secretary_task_steps
                    WHERE task_id = ? AND deleted_at IS NULL
                    """,
                    (task_row["task_id"],),
                ).fetchone()[0]
                temporary_base = int(current_max) + len(steps) + 1
                for offset, step in enumerate(steps, start=1):
                    connection.execute(
                        "UPDATE secretary_task_steps SET position = ? WHERE id = ?",
                        (temporary_base + offset, step["id"]),
                    )
                for position, step in enumerate(steps):
                    connection.execute(
                        "UPDATE secretary_task_steps SET position = ? WHERE id = ?",
                        (position, step["id"]),
                    )

            # schedule_id was never authoritative, but accept valid historical
            # mirrors when their calendar row is otherwise unlinked.
            affected_task_ids: set[str] = set()
            legacy_links = connection.execute(
                """
                SELECT step.id AS step_id, step.task_id, step.workspace_id,
                       step.schedule_id
                FROM secretary_task_steps step
                WHERE step.schedule_id IS NOT NULL AND step.deleted_at IS NULL
                ORDER BY step.id
                """
            ).fetchall()
            for link in legacy_links:
                updated_link = connection.execute(
                    """
                    UPDATE secretary_calendar_entries
                    SET step_id = ?, version = version + 1,
                        updated_by = ?, updated_at = ?
                    WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                      AND memo_id IS NULL AND task_id IS NULL
                      AND step_id IS NULL
                    """,
                    (
                        link["step_id"],
                        DEFAULT_OWNER_ID,
                        now,
                        link["schedule_id"],
                        link["workspace_id"],
                    ),
                )
                if updated_link.rowcount == 1:
                    calendar = connection.execute(
                        "SELECT * FROM secretary_calendar_entries WHERE id = ?",
                        (link["schedule_id"],),
                    ).fetchone()
                    assert calendar is not None
                    calendar_response = self._calendar_dict(calendar)
                    self._append_event(
                        connection,
                        workspace_id=link["workspace_id"],
                        aggregate_type="calendar_entry",
                        aggregate_id=calendar["id"],
                        aggregate_version=calendar["version"],
                        event_type="calendar.updated",
                        operation="upsert",
                        actor_id=DEFAULT_OWNER_ID,
                        actor_type="system",
                        device_id="workspace-migration:v1",
                        payload=calendar_response,
                    )
                    affected_task_ids.add(link["task_id"])
            connection.execute(
                "UPDATE secretary_task_steps SET schedule_id = NULL "
                "WHERE schedule_id IS NOT NULL"
            )

            # Keep the most recently updated active schedule and retain all
            # older rows as canceled audit history.
            duplicate_step_ids = connection.execute(
                """
                SELECT step_id FROM secretary_calendar_entries
                WHERE step_id IS NOT NULL AND status = 'scheduled'
                  AND deleted_at IS NULL
                GROUP BY step_id HAVING COUNT(*) > 1
                ORDER BY step_id
                """
            ).fetchall()
            for duplicate in duplicate_step_ids:
                schedules = connection.execute(
                    """
                    SELECT entry.id, step.task_id, entry.workspace_id
                    FROM secretary_calendar_entries entry
                    JOIN secretary_task_steps step ON step.id = entry.step_id
                    WHERE entry.step_id = ? AND entry.status = 'scheduled'
                      AND entry.deleted_at IS NULL
                    ORDER BY entry.updated_at DESC, entry.id DESC
                    """,
                    (duplicate["step_id"],),
                ).fetchall()
                for stale in schedules[1:]:
                    connection.execute(
                        """
                        UPDATE secretary_calendar_entries
                        SET status = 'canceled', version = version + 1,
                            updated_by = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (DEFAULT_OWNER_ID, now, stale["id"]),
                    )
                    calendar = connection.execute(
                        "SELECT * FROM secretary_calendar_entries WHERE id = ?",
                        (stale["id"],),
                    ).fetchone()
                    assert calendar is not None
                    calendar_response = self._calendar_dict(calendar)
                    self._append_event(
                        connection,
                        workspace_id=stale["workspace_id"],
                        aggregate_type="calendar_entry",
                        aggregate_id=calendar["id"],
                        aggregate_version=calendar["version"],
                        event_type="calendar.canceled",
                        operation="upsert",
                        actor_id=DEFAULT_OWNER_ID,
                        actor_type="system",
                        device_id="workspace-migration:v1",
                        payload=calendar_response,
                    )
                    affected_task_ids.add(stale["task_id"])

            for task_id in sorted(affected_task_ids):
                task = connection.execute(
                    """
                    SELECT * FROM secretary_business_tasks
                    WHERE id = ? AND deleted_at IS NULL
                    """,
                    (task_id,),
                ).fetchone()
                if task is None:
                    continue
                task = self._bump_task_after_step_write(connection, task, now=now)
                task_response = self._task_dict(connection, task)
                self._append_event(
                    connection,
                    workspace_id=task["workspace_id"],
                    aggregate_type="task",
                    aggregate_id=task_id,
                    aggregate_version=task["version"],
                    event_type="task.step_schedule_status_changed",
                    operation="upsert",
                    actor_id=DEFAULT_OWNER_ID,
                    actor_type="system",
                    device_id="workspace-migration:v1",
                    payload=task_response,
                )

            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_secretary_steps_task_active_position_unique
                ON secretary_task_steps(task_id, position)
                WHERE deleted_at IS NULL
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_secretary_calendar_active_step_unique
                ON secretary_calendar_entries(step_id)
                WHERE step_id IS NOT NULL AND status = 'scheduled'
                  AND deleted_at IS NULL
                """
            )
            connection.execute(
                """
                INSERT INTO secretary_workspace_schema_migrations(version, applied_at)
                VALUES (1, ?)
                """,
                (now,),
            )

        if 2 not in applied:
            step_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(secretary_task_steps)"
                ).fetchall()
            }
            if "client_mutation_id" not in step_columns:
                connection.execute(
                    "ALTER TABLE secretary_task_steps "
                    "ADD COLUMN client_mutation_id TEXT"
                )
            connection.execute(
                """
                INSERT INTO secretary_workspace_schema_migrations(version, applied_at)
                VALUES (2, ?)
                """,
                (now,),
            )

        if 3 not in applied:
            member_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(secretary_workspace_members)"
                ).fetchall()
            }
            if "version" not in member_columns:
                connection.execute(
                    "ALTER TABLE secretary_workspace_members "
                    "ADD COLUMN version INTEGER NOT NULL DEFAULT 1 "
                    "CHECK(version >= 1)"
                )
            connection.execute(
                """
                INSERT INTO secretary_workspace_schema_migrations(version, applied_at)
                VALUES (3, ?)
                """,
                (now,),
            )

        if 4 not in applied:
            # Create referenced tables before extending the legacy invitation
            # table with foreign-key columns.  Every statement is idempotent so
            # startup can safely resume after an interrupted pre-v4 migration.
            core_statements = (
                """
                CREATE TABLE IF NOT EXISTS secretary_task_alignment_cases (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL
                        REFERENCES secretary_workspaces(id),
                    task_id TEXT NOT NULL
                        REFERENCES secretary_business_tasks(id) ON DELETE CASCADE,
                    issuer_member_id TEXT NOT NULL
                        REFERENCES secretary_workspace_members(id),
                    assignee_member_id TEXT NOT NULL
                        REFERENCES secretary_workspace_members(id),
                    status TEXT NOT NULL CHECK(status IN (
                        'pending', 'accepted', 'rejected', 'canceled', 'stale'
                    )),
                    current_revision_no INTEGER NOT NULL
                        CHECK(current_revision_no >= 1),
                    accepted_revision_no INTEGER
                        CHECK(accepted_revision_no IS NULL OR accepted_revision_no >= 1),
                    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT,
                    CHECK(issuer_member_id <> assignee_member_id),
                    CHECK(
                        (status = 'accepted' AND accepted_revision_no IS NOT NULL)
                        OR (status <> 'accepted' AND accepted_revision_no IS NULL)
                    )
                )
                """,
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_secretary_one_pending_alignment_case
                ON secretary_task_alignment_cases(task_id)
                WHERE status = 'pending'
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_secretary_alignment_case_workspace
                ON secretary_task_alignment_cases(workspace_id, updated_at DESC)
                """,
                """
                CREATE TABLE IF NOT EXISTS secretary_task_alignment_revisions (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL
                        REFERENCES secretary_task_alignment_cases(id) ON DELETE CASCADE,
                    revision_no INTEGER NOT NULL CHECK(revision_no >= 1),
                    parent_revision_id TEXT
                        REFERENCES secretary_task_alignment_revisions(id),
                    base_task_version INTEGER NOT NULL CHECK(base_task_version >= 1),
                    schema_version INTEGER NOT NULL DEFAULT 1
                        CHECK(schema_version = 1),
                    proposed_by_role TEXT NOT NULL
                        CHECK(proposed_by_role IN ('issuer', 'assignee')),
                    proposed_by_member_id TEXT NOT NULL
                        REFERENCES secretary_workspace_members(id),
                    required_responder_role TEXT NOT NULL
                        CHECK(required_responder_role IN ('issuer', 'assignee')),
                    required_responder_member_id TEXT NOT NULL
                        REFERENCES secretary_workspace_members(id),
                    digest TEXT NOT NULL UNIQUE
                        CHECK(length(digest) = 71
                              AND substr(digest, 1, 7) = 'sha256:'
                              AND substr(digest, 8) NOT GLOB '*[^0-9a-f]*'),
                    canonical_json TEXT NOT NULL
                        CHECK(json_valid(canonical_json)
                              AND json_type(canonical_json) = 'object'),
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(case_id, revision_no),
                    CHECK(proposed_by_role <> required_responder_role),
                    CHECK(proposed_by_member_id <> required_responder_member_id),
                    CHECK(
                        (revision_no = 1 AND parent_revision_id IS NULL)
                        OR (revision_no > 1 AND parent_revision_id IS NOT NULL)
                    )
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_secretary_alignment_revision_case
                ON secretary_task_alignment_revisions(case_id, revision_no)
                """,
            )
            for statement in core_statements:
                connection.execute(statement)

            invitation_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(secretary_task_alignment_invitations)"
                ).fetchall()
            }
            if "alignment_case_id" not in invitation_columns:
                connection.execute(
                    "ALTER TABLE secretary_task_alignment_invitations "
                    "ADD COLUMN alignment_case_id TEXT REFERENCES "
                    "secretary_task_alignment_cases(id)"
                )
            if "alignment_revision_id" not in invitation_columns:
                connection.execute(
                    "ALTER TABLE secretary_task_alignment_invitations "
                    "ADD COLUMN alignment_revision_id TEXT REFERENCES "
                    "secretary_task_alignment_revisions(id)"
                )
            if "alignment_revision_digest" not in invitation_columns:
                connection.execute(
                    "ALTER TABLE secretary_task_alignment_invitations "
                    "ADD COLUMN alignment_revision_digest TEXT "
                    "CHECK(alignment_revision_digest IS NULL OR "
                    "(length(alignment_revision_digest) = 71 AND "
                    "substr(alignment_revision_digest, 1, 7) = 'sha256:' AND "
                    "substr(alignment_revision_digest, 8) "
                    "NOT GLOB '*[^0-9a-f]*'))"
                )

            statements = (
                """
                CREATE TABLE IF NOT EXISTS secretary_task_assignee_sessions (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL
                        REFERENCES secretary_workspaces(id),
                    task_id TEXT NOT NULL
                        REFERENCES secretary_business_tasks(id) ON DELETE CASCADE,
                    agreement_id TEXT NOT NULL
                        REFERENCES secretary_task_alignment_cases(id) ON DELETE CASCADE,
                    invitation_id TEXT NOT NULL UNIQUE
                        REFERENCES secretary_task_alignment_invitations(id),
                    assignee_member_id TEXT NOT NULL
                        REFERENCES secretary_workspace_members(id),
                    token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash) = 64),
                    client_device_id TEXT NOT NULL
                        CHECK(length(client_device_id) BETWEEN 1 AND 200),
                    exchange_idempotency_hash TEXT NOT NULL
                        CHECK(length(exchange_idempotency_hash) = 64),
                    exchange_request_hash TEXT NOT NULL
                        CHECK(length(exchange_request_hash) = 64),
                    assurance_method TEXT NOT NULL
                        CHECK(assurance_method = 'dual_channel_capability'),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    revoke_reason TEXT
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_secretary_task_session_expiry
                ON secretary_task_assignee_sessions(expires_at)
                """,
                """
                CREATE TABLE IF NOT EXISTS secretary_task_alignment_decisions (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL
                        REFERENCES secretary_task_alignment_cases(id) ON DELETE CASCADE,
                    revision_id TEXT NOT NULL UNIQUE
                        REFERENCES secretary_task_alignment_revisions(id),
                    revision_digest TEXT NOT NULL
                        CHECK(length(revision_digest) = 71
                              AND substr(revision_digest, 1, 7) = 'sha256:'
                              AND substr(revision_digest, 8)
                                  NOT GLOB '*[^0-9a-f]*'),
                    action TEXT NOT NULL
                        CHECK(action IN ('accept', 'reject', 'counter')),
                    actor_role TEXT NOT NULL
                        CHECK(actor_role IN ('issuer', 'assignee')),
                    actor_member_id TEXT NOT NULL
                        REFERENCES secretary_workspace_members(id),
                    actor_session_id TEXT
                        REFERENCES secretary_task_assignee_sessions(id),
                    assurance_method TEXT NOT NULL CHECK(assurance_method IN (
                        'owner_token', 'owner_device_session',
                        'dual_channel_capability', 'task_session'
                    )),
                    reason TEXT,
                    counter_revision_id TEXT
                        REFERENCES secretary_task_alignment_revisions(id),
                    client_mutation_id TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1 CHECK(version = 1),
                    created_at TEXT NOT NULL,
                    UNIQUE(case_id, client_mutation_id),
                    CHECK(
                        (assurance_method = 'task_session'
                         AND actor_session_id IS NOT NULL)
                        OR (assurance_method <> 'task_session'
                            AND actor_session_id IS NULL)
                    ),
                    CHECK(
                        (action = 'counter' AND counter_revision_id IS NOT NULL
                         AND reason IS NOT NULL)
                        OR (action = 'reject' AND counter_revision_id IS NULL
                            AND reason IS NOT NULL)
                        OR (action = 'accept' AND counter_revision_id IS NULL
                            AND reason IS NULL)
                    )
                )
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_alignment_revision_immutable_update
                BEFORE UPDATE ON secretary_task_alignment_revisions
                BEGIN
                    SELECT RAISE(ABORT, 'task agreement revisions are immutable');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_alignment_revision_immutable_delete
                BEFORE DELETE ON secretary_task_alignment_revisions
                BEGIN
                    SELECT RAISE(ABORT, 'task agreement revisions are immutable');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_alignment_decision_immutable_update
                BEFORE UPDATE ON secretary_task_alignment_decisions
                BEGIN
                    SELECT RAISE(ABORT, 'task agreement decisions are immutable');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_alignment_decision_immutable_delete
                BEFORE DELETE ON secretary_task_alignment_decisions
                BEGIN
                    SELECT RAISE(ABORT, 'task agreement decisions are immutable');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_alignment_invitation_binding_insert
                BEFORE INSERT ON secretary_task_alignment_invitations
                WHEN NEW.alignment_case_id IS NOT NULL
                  OR NEW.alignment_revision_id IS NOT NULL
                  OR NEW.alignment_revision_digest IS NOT NULL
                BEGIN
                    SELECT CASE WHEN NEW.alignment_case_id IS NULL
                                      OR NEW.alignment_revision_id IS NULL
                                      OR NEW.alignment_revision_digest IS NULL
                        THEN RAISE(ABORT, 'incomplete alignment binding') END;
                    SELECT CASE WHEN NOT EXISTS (
                        SELECT 1
                        FROM secretary_task_alignment_cases alignment_case
                        JOIN secretary_task_alignment_revisions revision
                          ON revision.case_id = alignment_case.id
                        WHERE alignment_case.id = NEW.alignment_case_id
                          AND alignment_case.task_id = NEW.task_id
                          AND alignment_case.assignee_member_id = NEW.assignee_member_id
                          AND revision.id = NEW.alignment_revision_id
                          AND revision.digest = NEW.alignment_revision_digest
                    ) THEN RAISE(ABORT, 'invalid alignment binding') END;
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_alignment_invitation_binding_update
                BEFORE UPDATE OF alignment_case_id, alignment_revision_id,
                                 alignment_revision_digest
                ON secretary_task_alignment_invitations
                WHEN OLD.alignment_case_id IS NOT NULL
                  OR OLD.alignment_revision_id IS NOT NULL
                  OR OLD.alignment_revision_digest IS NOT NULL
                  OR NEW.alignment_case_id IS NOT NULL
                  OR NEW.alignment_revision_id IS NOT NULL
                  OR NEW.alignment_revision_digest IS NOT NULL
                BEGIN
                    SELECT CASE WHEN OLD.alignment_case_id IS NOT NULL
                                      AND (
                                        NEW.alignment_case_id
                                            IS NOT OLD.alignment_case_id
                                        OR NEW.alignment_revision_id
                                            IS NOT OLD.alignment_revision_id
                                        OR NEW.alignment_revision_digest
                                            IS NOT OLD.alignment_revision_digest
                                      )
                        THEN RAISE(ABORT, 'alignment binding is immutable') END;
                    SELECT CASE WHEN NEW.alignment_case_id IS NULL
                                      OR NEW.alignment_revision_id IS NULL
                                      OR NEW.alignment_revision_digest IS NULL
                        THEN RAISE(ABORT, 'incomplete alignment binding') END;
                    SELECT CASE WHEN NOT EXISTS (
                        SELECT 1
                        FROM secretary_task_alignment_cases alignment_case
                        JOIN secretary_task_alignment_revisions revision
                          ON revision.case_id = alignment_case.id
                        WHERE alignment_case.id = NEW.alignment_case_id
                          AND alignment_case.task_id = NEW.task_id
                          AND alignment_case.assignee_member_id = NEW.assignee_member_id
                          AND revision.id = NEW.alignment_revision_id
                          AND revision.digest = NEW.alignment_revision_digest
                    ) THEN RAISE(ABORT, 'invalid alignment binding') END;
                END
                """,
            )
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO secretary_workspace_schema_migrations(version, applied_at)
                VALUES (4, ?)
                """,
                (now,),
            )

        if 5 not in applied:
            historical_links = connection.execute(
                """
                SELECT 'task' AS target_kind, id AS target_id,
                       workspace_id AS target_workspace_id,
                       origin_memo_id AS memo_id, deleted_at AS target_deleted_at
                FROM secretary_business_tasks
                WHERE origin_memo_id IS NOT NULL
                UNION ALL
                SELECT 'calendar' AS target_kind, id AS target_id,
                       workspace_id AS target_workspace_id,
                       memo_id, deleted_at AS target_deleted_at
                FROM secretary_calendar_entries
                WHERE memo_id IS NOT NULL
                ORDER BY memo_id, target_kind, target_id
                """
            ).fetchall()
            links_by_memo: dict[str, list[sqlite3.Row]] = {}
            for link in historical_links:
                links_by_memo.setdefault(str(link["memo_id"]), []).append(link)

            migration_issues: list[str] = []
            for memo_id, links in links_by_memo.items():
                for link in links:
                    if link["target_kind"] == "task":
                        migration_issues.append(
                            f"memo_id={memo_id} 的历史任务链接缺少可验证的"
                            "原子转换与披露审计证明"
                        )
                    else:
                        migration_issues.append(
                            f"memo_id={memo_id} 的历史日程链接无法证明原子转换版本"
                        )
                memo = connection.execute(
                    "SELECT * FROM secretary_memos WHERE id = ?",
                    (memo_id,),
                ).fetchone()
                if memo is None:
                    target_ids = ",".join(str(link["target_id"]) for link in links)
                    migration_issues.append(
                        f"memo_id={memo_id} 缺少来源备忘(targets={target_ids})"
                    )
                    continue
                if len(links) != 1:
                    targets = ",".join(
                        f"{link['target_kind']}:{link['target_id']}" for link in links
                    )
                    migration_issues.append(
                        f"memo_id={memo_id} 存在多个物化目标({targets})"
                    )
                if memo["deleted_at"] is not None:
                    migration_issues.append(f"memo_id={memo_id} 来源备忘已软删除")
                if memo["status"] != "converted":
                    migration_issues.append(
                        f"memo_id={memo_id} 来源备忘状态不是 converted"
                    )
                if memo["confirmation_status"] not in {"not_required", "confirmed"}:
                    migration_issues.append(
                        f"memo_id={memo_id} 来源备忘未完成主人确认"
                    )
                memo_source = json_loads(memo["source_json"], {})
                nested_authority = (
                    memo_source.get("authority")
                    if isinstance(memo_source, dict)
                    else None
                )
                if (
                    nested_authority is not None
                    and nested_authority != memo["authority"]
                ):
                    migration_issues.append(
                        f"memo_id={memo_id} 来源 authority 元数据不一致"
                    )
                for link in links:
                    if link["target_workspace_id"] != memo["workspace_id"]:
                        migration_issues.append(
                            f"memo_id={memo_id} 与 {link['target_kind']}:"
                            f"{link['target_id']} 跨工作区"
                        )
                    if link["target_deleted_at"] is not None:
                        migration_issues.append(
                            f"memo_id={memo_id} 的 {link['target_kind']}:"
                            f"{link['target_id']} 已软删除"
                        )
                    if link["target_kind"] == "calendar":
                        continue
                    if memo["version"] < 2:
                        migration_issues.append(
                            f"memo_id={memo_id} 的历史任务链接缺少转换前版本"
                        )
                        continue
                    task_source = connection.execute(
                        """
                        SELECT source_json FROM secretary_business_tasks
                        WHERE id = ?
                        """,
                        (link["target_id"],),
                    ).fetchone()
                    if (
                        task_source is None
                        or task_source["source_json"] != memo["source_json"]
                    ):
                        migration_issues.append(
                            f"memo_id={memo_id} 的历史任务来源与备忘不一致"
                        )
            converted_memos = connection.execute(
                """
                SELECT id FROM secretary_memos
                WHERE status = 'converted' AND deleted_at IS NULL
                ORDER BY id
                """
            ).fetchall()
            for memo in converted_memos:
                memo_id = str(memo["id"])
                if memo_id not in links_by_memo:
                    migration_issues.append(
                        f"memo_id={memo_id} 是无物化账本的 converted 备忘"
                    )
            if migration_issues:
                details = "; ".join(migration_issues[:20])
                raise RuntimeError(
                    "workspace v5 备忘物化迁移失败：历史状态无法建立可信物化账本；"
                    f"{details}。请先人工修复后重试"
                )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS secretary_memo_materializations (
                    memo_id TEXT PRIMARY KEY REFERENCES secretary_memos(id),
                    workspace_id TEXT NOT NULL REFERENCES secretary_workspaces(id),
                    source_memo_version INTEGER NOT NULL
                        CHECK(source_memo_version >= 1),
                    source_snapshot_json TEXT NOT NULL
                        CHECK(json_valid(source_snapshot_json)
                              AND json_type(source_snapshot_json) = 'object'),
                    task_id TEXT UNIQUE
                        REFERENCES secretary_business_tasks(id),
                    calendar_entry_id TEXT UNIQUE
                        REFERENCES secretary_calendar_entries(id),
                    created_at TEXT NOT NULL,
                    CHECK(
                        (CASE WHEN task_id IS NULL THEN 0 ELSE 1 END) +
                        (CASE WHEN calendar_entry_id IS NULL THEN 0 ELSE 1 END) = 1
                    )
                )
                """
            )

            # The task table already has a stronger, full-lifecycle UNIQUE
            # constraint on origin_memo_id. Keep it intact and make the scoped
            # invariant explicit for both historical link columns as defense
            # in depth around the materialization ledger.
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_secretary_tasks_active_origin_memo_unique
                ON secretary_business_tasks(workspace_id, origin_memo_id)
                WHERE origin_memo_id IS NOT NULL AND deleted_at IS NULL
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_secretary_calendar_active_memo_unique
                ON secretary_calendar_entries(workspace_id, memo_id)
                WHERE memo_id IS NOT NULL AND deleted_at IS NULL
                """
            )
            for statement in (
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_memo_materialization_binding_insert
                BEFORE INSERT ON secretary_memo_materializations
                BEGIN
                    SELECT CASE WHEN NOT EXISTS (
                        SELECT 1 FROM secretary_memos memo
                        WHERE memo.id = NEW.memo_id
                          AND memo.workspace_id = NEW.workspace_id
                          AND memo.deleted_at IS NULL
                          AND memo.status = 'converted'
                          AND memo.confirmation_status IN ('not_required', 'confirmed')
                          AND memo.version = NEW.source_memo_version + 1
                          AND json_extract(
                                NEW.source_snapshot_json, '$.schema'
                              ) = 'centaur.memo-source-snapshot.v1'
                          AND json_extract(
                                NEW.source_snapshot_json, '$.memo_id'
                              ) = NEW.memo_id
                          AND json_extract(
                                NEW.source_snapshot_json, '$.workspace_id'
                              ) = NEW.workspace_id
                          AND json_extract(
                                NEW.source_snapshot_json, '$.source_memo_version'
                              ) = NEW.source_memo_version
                          AND json_extract(
                                NEW.source_snapshot_json, '$.domain'
                              ) = memo.domain
                          AND json_extract(
                                NEW.source_snapshot_json, '$.authority'
                              ) = memo.authority
                          AND json_extract(
                                NEW.source_snapshot_json, '$.source_kind'
                              ) IS json_extract(memo.source_json, '$.source_kind')
                          AND json_extract(
                                NEW.source_snapshot_json, '$.source_ref'
                              ) IS json_extract(memo.source_json, '$.source_ref')
                          AND (
                                SELECT COUNT(*)
                                FROM json_each(NEW.source_snapshot_json)
                              ) = 10
                          AND NOT EXISTS (
                                SELECT 1
                                FROM json_each(NEW.source_snapshot_json) item
                                WHERE item.key NOT IN (
                                    'schema', 'memo_id', 'workspace_id',
                                    'source_memo_version', 'domain', 'authority',
                                    'source_kind', 'source_ref',
                                    'source_json_digest', 'content_digest'
                                )
                              )
                          AND length(json_extract(
                                NEW.source_snapshot_json, '$.source_json_digest'
                              )) = 71
                          AND substr(json_extract(
                                NEW.source_snapshot_json, '$.source_json_digest'
                              ), 1, 7) = 'sha256:'
                          AND substr(json_extract(
                                NEW.source_snapshot_json, '$.source_json_digest'
                              ), 8) NOT GLOB '*[^0-9a-f]*'
                          AND length(json_extract(
                                NEW.source_snapshot_json, '$.content_digest'
                              )) = 71
                          AND substr(json_extract(
                                NEW.source_snapshot_json, '$.content_digest'
                              ), 1, 7) = 'sha256:'
                          AND substr(json_extract(
                                NEW.source_snapshot_json, '$.content_digest'
                              ), 8) NOT GLOB '*[^0-9a-f]*'
                    ) THEN RAISE(ABORT, 'invalid materialization memo binding') END;
                    SELECT CASE WHEN NEW.task_id IS NOT NULL AND NOT EXISTS (
                        SELECT 1 FROM secretary_business_tasks task
                        WHERE task.id = NEW.task_id
                          AND task.workspace_id = NEW.workspace_id
                          AND task.origin_memo_id = NEW.memo_id
                          AND task.deleted_at IS NULL
                    ) THEN RAISE(ABORT, 'invalid materialization task binding') END;
                    SELECT CASE WHEN NEW.calendar_entry_id IS NOT NULL
                        AND NOT EXISTS (
                            SELECT 1 FROM secretary_calendar_entries entry
                            WHERE entry.id = NEW.calendar_entry_id
                              AND entry.workspace_id = NEW.workspace_id
                              AND entry.memo_id = NEW.memo_id
                              AND entry.deleted_at IS NULL
                        )
                    THEN RAISE(ABORT, 'invalid materialization calendar binding') END;
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_memo_materialization_immutable_update
                BEFORE UPDATE ON secretary_memo_materializations
                BEGIN
                    SELECT RAISE(ABORT, 'memo materialization is immutable');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_memo_materialization_immutable_delete
                BEFORE DELETE ON secretary_memo_materializations
                BEGIN
                    SELECT RAISE(ABORT, 'memo materialization is immutable');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_materialized_memo_immutable_update
                BEFORE UPDATE ON secretary_memos
                WHEN EXISTS (
                    SELECT 1 FROM secretary_memo_materializations materialization
                    WHERE materialization.memo_id = OLD.id
                )
                BEGIN
                    SELECT RAISE(ABORT, 'materialized memo is immutable');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_materialized_memo_immutable_delete
                BEFORE DELETE ON secretary_memos
                WHEN EXISTS (
                    SELECT 1 FROM secretary_memo_materializations materialization
                    WHERE materialization.memo_id = OLD.id
                )
                BEGIN
                    SELECT RAISE(ABORT, 'materialized memo is immutable');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_materialized_task_link_immutable
                BEFORE UPDATE OF origin_memo_id, workspace_id, deleted_at
                ON secretary_business_tasks
                WHEN EXISTS (
                    SELECT 1 FROM secretary_memo_materializations materialization
                    WHERE materialization.task_id = OLD.id
                ) AND (
                    NEW.origin_memo_id IS NOT OLD.origin_memo_id
                    OR NEW.workspace_id IS NOT OLD.workspace_id
                    OR NEW.deleted_at IS NOT OLD.deleted_at
                )
                BEGIN
                    SELECT RAISE(ABORT, 'materialized task link is immutable');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_materialized_calendar_link_immutable
                BEFORE UPDATE OF memo_id, workspace_id, deleted_at
                ON secretary_calendar_entries
                WHEN EXISTS (
                    SELECT 1 FROM secretary_memo_materializations materialization
                    WHERE materialization.calendar_entry_id = OLD.id
                ) AND (
                    NEW.memo_id IS NOT OLD.memo_id
                    OR NEW.workspace_id IS NOT OLD.workspace_id
                    OR NEW.deleted_at IS NOT OLD.deleted_at
                )
                BEGIN
                    SELECT RAISE(ABORT, 'materialized calendar link is immutable');
                END
                """,
            ):
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO secretary_workspace_schema_migrations(version, applied_at)
                VALUES (5, ?)
                """,
                (now,),
            )

        if 6 not in applied:
            v6_object_names = sorted(
                TASK_CHANGE_V6_TABLES
                | TASK_CHANGE_V6_INDEXES
                | TASK_CHANGE_V6_TRIGGERS
            )
            placeholders = ",".join("?" for _ in v6_object_names)
            collisions = connection.execute(
                f"SELECT type, name FROM sqlite_master "
                f"WHERE name IN ({placeholders}) ORDER BY name",
                v6_object_names,
            ).fetchall()
            if collisions:
                names = ",".join(str(row["name"]) for row in collisions)
                raise RuntimeError(
                    "workspace v6 任务变更协议迁移失败："
                    f"发现无迁移标记的同名对象({names})"
                )
            legacy_pending_changes = connection.execute(
                """
                SELECT id FROM secretary_task_changes
                WHERE status = 'proposed' ORDER BY id LIMIT 20
                """
            ).fetchall()
            if legacy_pending_changes:
                identifiers = ",".join(str(row["id"]) for row in legacy_pending_changes)
                raise RuntimeError(
                    "workspace v6 任务变更协议迁移失败：存在无法补造双方确认"
                    f"证明的历史待处理变更({identifiers})。请先用旧版本明确处理后重试"
                )

            statements = (
                """
                CREATE TABLE IF NOT EXISTS secretary_task_change_proposals (
                    change_id TEXT PRIMARY KEY
                        REFERENCES secretary_task_changes(id) ON DELETE CASCADE,
                    workspace_id TEXT NOT NULL
                        REFERENCES secretary_workspaces(id),
                    task_id TEXT NOT NULL
                        REFERENCES secretary_business_tasks(id) ON DELETE CASCADE,
                    proposer_member_id TEXT NOT NULL
                        REFERENCES secretary_workspace_members(id),
                    responder_member_id TEXT NOT NULL
                        REFERENCES secretary_workspace_members(id),
                    base_task_version INTEGER NOT NULL CHECK(base_task_version >= 1),
                    digest TEXT NOT NULL UNIQUE
                        CHECK(length(digest) = 71
                              AND substr(digest, 1, 7) = 'sha256:'
                              AND substr(digest, 8) NOT GLOB '*[^0-9a-f]*'),
                    canonical_json TEXT NOT NULL
                        CHECK(json_valid(canonical_json)
                              AND json_type(canonical_json) = 'object'),
                    created_at TEXT NOT NULL
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_secretary_change_proposal_task
                ON secretary_task_change_proposals(task_id, created_at DESC)
                """,
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_secretary_one_pending_protocol_change_per_task
                ON secretary_task_changes(task_id)
                WHERE status = 'proposed'
                """,
                """
                CREATE TABLE IF NOT EXISTS secretary_task_change_invitations (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL
                        REFERENCES secretary_workspaces(id),
                    change_id TEXT NOT NULL
                        REFERENCES secretary_task_changes(id) ON DELETE CASCADE,
                    task_id TEXT NOT NULL
                        REFERENCES secretary_business_tasks(id) ON DELETE CASCADE,
                    change_version INTEGER NOT NULL CHECK(change_version >= 1),
                    task_version INTEGER NOT NULL CHECK(task_version >= 1),
                    responder_member_id TEXT NOT NULL
                        REFERENCES secretary_workspace_members(id),
                    code_hash TEXT NOT NULL CHECK(length(code_hash) = 64),
                    failed_attempts INTEGER NOT NULL DEFAULT 0
                        CHECK(failed_attempts >= 0),
                    max_attempts INTEGER NOT NULL DEFAULT 5 CHECK(max_attempts >= 1),
                    created_by TEXT NOT NULL
                        REFERENCES secretary_workspace_members(id),
                    created_device_id TEXT NOT NULL,
                    creation_idempotency_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT,
                    revoked_at TEXT
                )
                """,
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_secretary_one_active_change_invitation
                ON secretary_task_change_invitations(change_id)
                WHERE revoked_at IS NULL
                """,
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_secretary_change_invitation_request
                ON secretary_task_change_invitations(
                    workspace_id, created_by, change_id, creation_idempotency_key
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_secretary_change_invitation_expiry
                ON secretary_task_change_invitations(expires_at)
                """,
                """
                CREATE TABLE IF NOT EXISTS secretary_task_change_sessions (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL
                        REFERENCES secretary_workspaces(id),
                    change_id TEXT NOT NULL
                        REFERENCES secretary_task_changes(id) ON DELETE CASCADE,
                    task_id TEXT NOT NULL
                        REFERENCES secretary_business_tasks(id) ON DELETE CASCADE,
                    invitation_id TEXT NOT NULL UNIQUE
                        REFERENCES secretary_task_change_invitations(id),
                    responder_member_id TEXT NOT NULL
                        REFERENCES secretary_workspace_members(id),
                    token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash) = 64),
                    client_device_id TEXT NOT NULL
                        CHECK(length(client_device_id) BETWEEN 1 AND 200),
                    exchange_idempotency_hash TEXT NOT NULL
                        CHECK(length(exchange_idempotency_hash) = 64),
                    exchange_request_hash TEXT NOT NULL
                        CHECK(length(exchange_request_hash) = 64),
                    assurance_method TEXT NOT NULL
                        CHECK(assurance_method = 'dual_channel_capability'),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    revoke_reason TEXT
                )
                """,
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_secretary_one_active_change_session
                ON secretary_task_change_sessions(change_id)
                WHERE revoked_at IS NULL
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_secretary_change_session_expiry
                ON secretary_task_change_sessions(expires_at)
                """,
                """
                CREATE TABLE IF NOT EXISTS secretary_task_change_decisions (
                    id TEXT PRIMARY KEY,
                    change_id TEXT NOT NULL UNIQUE
                        REFERENCES secretary_task_changes(id) ON DELETE CASCADE,
                    proposal_digest TEXT NOT NULL
                        CHECK(length(proposal_digest) = 71
                              AND substr(proposal_digest, 1, 7) = 'sha256:'
                              AND substr(proposal_digest, 8)
                                  NOT GLOB '*[^0-9a-f]*'),
                    action TEXT NOT NULL CHECK(action IN ('accept', 'reject', 'cancel')),
                    actor_member_id TEXT NOT NULL
                        REFERENCES secretary_workspace_members(id),
                    actor_session_id TEXT
                        REFERENCES secretary_task_change_sessions(id),
                    assurance_method TEXT NOT NULL CHECK(assurance_method IN (
                        'owner_token', 'owner_device_session', 'task_change_session'
                    )),
                    reason TEXT,
                    client_mutation_id TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1 CHECK(version = 1),
                    created_at TEXT NOT NULL,
                    UNIQUE(change_id, client_mutation_id),
                    CHECK(
                        (assurance_method = 'task_change_session'
                         AND actor_session_id IS NOT NULL)
                        OR (assurance_method <> 'task_change_session'
                            AND actor_session_id IS NULL)
                    ),
                    CHECK(
                        (action = 'accept' AND reason IS NULL)
                        OR (action IN ('reject', 'cancel') AND reason IS NOT NULL)
                    )
                )
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_change_proposal_binding_insert
                BEFORE INSERT ON secretary_task_change_proposals
                BEGIN
                    SELECT CASE WHEN NOT EXISTS (
                        SELECT 1
                        FROM secretary_task_changes change_record
                        JOIN secretary_business_tasks task
                          ON task.id = change_record.task_id
                        WHERE change_record.id = NEW.change_id
                          AND change_record.workspace_id = NEW.workspace_id
                          AND change_record.task_id = NEW.task_id
                          AND change_record.base_version = NEW.base_task_version
                          AND change_record.proposed_by = NEW.proposer_member_id
                          AND change_record.proposed_at = NEW.created_at
                          AND change_record.status = 'proposed'
                          AND task.workspace_id = NEW.workspace_id
                          AND task.version = NEW.base_task_version
                          AND task.issuer_member_id = NEW.proposer_member_id
                          AND task.assignee_member_id = NEW.responder_member_id
                          AND json_extract(NEW.canonical_json, '$.schema')
                                = 'centaur.task-change.v1'
                          AND json_extract(NEW.canonical_json, '$.workspace_id')
                                = NEW.workspace_id
                          AND json_extract(NEW.canonical_json, '$.task_id')
                                = NEW.task_id
                          AND json_extract(NEW.canonical_json, '$.change_id')
                                = NEW.change_id
                          AND json_extract(NEW.canonical_json, '$.change_type')
                                = change_record.change_type
                          AND json_extract(NEW.canonical_json, '$.base_task_version')
                                = NEW.base_task_version
                          AND json_extract(NEW.canonical_json, '$.proposer_member_id')
                                = NEW.proposer_member_id
                          AND json_extract(NEW.canonical_json, '$.responder_member_id')
                                = NEW.responder_member_id
                          AND json_extract(NEW.canonical_json, '$.proposer_role')
                                = 'issuer'
                          AND json_extract(NEW.canonical_json, '$.responder_role')
                                = CASE
                                    WHEN NEW.responder_member_id
                                         = NEW.proposer_member_id
                                    THEN 'issuer' ELSE 'assignee'
                                  END
                          AND json_extract(NEW.canonical_json, '$.reason')
                                = change_record.reason
                          AND json_extract(NEW.canonical_json, '$.before')
                                IS json_extract(change_record.before_json, '$')
                          AND json_extract(NEW.canonical_json, '$.patch')
                                IS json_extract(change_record.patch_json, '$')
                          AND (SELECT COUNT(*) FROM json_each(NEW.canonical_json)) = 13
                          AND NOT EXISTS (
                              SELECT 1 FROM json_each(NEW.canonical_json)
                              WHERE key NOT IN (
                                  'schema', 'workspace_id', 'task_id',
                                  'change_id', 'change_type',
                                  'base_task_version', 'proposer_role',
                                  'proposer_member_id', 'responder_role',
                                  'responder_member_id', 'before', 'patch',
                                  'reason'
                              )
                          )
                    ) THEN RAISE(ABORT, 'invalid task change proposal binding') END;
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_change_proposal_immutable_update
                BEFORE UPDATE ON secretary_task_change_proposals
                BEGIN
                    SELECT RAISE(ABORT, 'task change proposal is immutable');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_change_proposal_immutable_delete
                BEFORE DELETE ON secretary_task_change_proposals
                BEGIN
                    SELECT RAISE(ABORT, 'task change proposal is immutable');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_bound_change_fields_immutable
                BEFORE UPDATE OF workspace_id, task_id, change_type, base_version,
                                 before_json, patch_json, reason, proposed_by,
                                 proposed_at, client_mutation_id
                ON secretary_task_changes
                WHEN EXISTS (
                    SELECT 1 FROM secretary_task_change_proposals proposal
                    WHERE proposal.change_id = OLD.id
                ) AND (
                    NEW.workspace_id IS NOT OLD.workspace_id
                    OR NEW.task_id IS NOT OLD.task_id
                    OR NEW.change_type IS NOT OLD.change_type
                    OR NEW.base_version IS NOT OLD.base_version
                    OR NEW.before_json IS NOT OLD.before_json
                    OR NEW.patch_json IS NOT OLD.patch_json
                    OR NEW.reason IS NOT OLD.reason
                    OR NEW.proposed_by IS NOT OLD.proposed_by
                    OR NEW.proposed_at IS NOT OLD.proposed_at
                    OR NEW.client_mutation_id IS NOT OLD.client_mutation_id
                )
                BEGIN
                    SELECT RAISE(ABORT, 'bound task change fields are immutable');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_bound_change_state_transition
                BEFORE UPDATE OF status, decided_by, decided_at, version
                ON secretary_task_changes
                WHEN EXISTS (
                    SELECT 1 FROM secretary_task_change_proposals proposal
                    WHERE proposal.change_id = OLD.id
                )
                BEGIN
                    SELECT CASE WHEN NOT (
                        OLD.status = 'proposed'
                        AND NEW.status IN ('accepted', 'rejected', 'canceled')
                        AND NEW.version = OLD.version + 1
                        AND NEW.decided_by IS NOT NULL
                        AND NEW.decided_at IS NOT NULL
                        AND EXISTS (
                            SELECT 1
                            FROM secretary_task_change_proposals proposal
                            JOIN secretary_task_change_decisions decision
                              ON decision.change_id = proposal.change_id
                            WHERE proposal.change_id = OLD.id
                              AND decision.proposal_digest = proposal.digest
                              AND decision.actor_member_id = NEW.decided_by
                              AND NEW.status = CASE decision.action
                                  WHEN 'accept' THEN 'accepted'
                                  WHEN 'reject' THEN 'rejected'
                                  WHEN 'cancel' THEN 'canceled'
                                END
                        )
                    ) THEN RAISE(
                        ABORT, 'invalid bound task change state transition'
                    ) END;
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_bound_change_immutable_delete
                BEFORE DELETE ON secretary_task_changes
                WHEN EXISTS (
                    SELECT 1 FROM secretary_task_change_proposals proposal
                    WHERE proposal.change_id = OLD.id
                )
                BEGIN
                    SELECT RAISE(ABORT, 'bound task change is immutable');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_change_decision_binding_insert
                BEFORE INSERT ON secretary_task_change_decisions
                BEGIN
                    SELECT CASE WHEN NOT EXISTS (
                        SELECT 1
                        FROM secretary_task_change_proposals proposal
                        JOIN secretary_task_changes change_record
                          ON change_record.id = proposal.change_id
                        LEFT JOIN secretary_task_change_sessions session
                          ON session.id = NEW.actor_session_id
                        WHERE proposal.change_id = NEW.change_id
                          AND proposal.digest = NEW.proposal_digest
                          AND change_record.status = 'proposed'
                          AND (
                            (
                              NEW.action IN ('accept', 'reject')
                              AND NEW.actor_member_id
                                  = proposal.responder_member_id
                              AND (
                                (
                                  NEW.assurance_method = 'task_change_session'
                                  AND session.id IS NOT NULL
                                  AND session.change_id = proposal.change_id
                                  AND session.task_id = proposal.task_id
                                  AND session.responder_member_id
                                      = proposal.responder_member_id
                                  AND session.revoked_at IS NULL
                                )
                                OR (
                                  NEW.assurance_method IN (
                                      'owner_token', 'owner_device_session'
                                  )
                                  AND NEW.actor_session_id IS NULL
                                  AND proposal.responder_member_id
                                      = proposal.proposer_member_id
                                )
                              )
                            )
                            OR (
                              NEW.action = 'cancel'
                              AND NEW.actor_member_id
                                  = proposal.proposer_member_id
                              AND NEW.actor_session_id IS NULL
                              AND NEW.assurance_method IN (
                                  'owner_token', 'owner_device_session'
                              )
                            )
                          )
                    ) THEN RAISE(ABORT, 'invalid task change decision binding') END;
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_change_decision_immutable_update
                BEFORE UPDATE ON secretary_task_change_decisions
                BEGIN
                    SELECT RAISE(ABORT, 'task change decision is immutable');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_change_decision_immutable_delete
                BEFORE DELETE ON secretary_task_change_decisions
                BEGIN
                    SELECT RAISE(ABORT, 'task change decision is immutable');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_change_invitation_binding_insert
                BEFORE INSERT ON secretary_task_change_invitations
                BEGIN
                    SELECT CASE WHEN NOT EXISTS (
                        SELECT 1
                        FROM secretary_task_change_proposals proposal
                        JOIN secretary_task_changes change_record
                          ON change_record.id = proposal.change_id
                        WHERE proposal.change_id = NEW.change_id
                          AND proposal.workspace_id = NEW.workspace_id
                          AND proposal.task_id = NEW.task_id
                          AND proposal.responder_member_id = NEW.responder_member_id
                          AND proposal.base_task_version = NEW.task_version
                          AND change_record.version = NEW.change_version
                          AND change_record.status = 'proposed'
                    ) THEN RAISE(ABORT, 'invalid task change invitation binding') END;
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_change_invitation_binding_update
                BEFORE UPDATE OF workspace_id, change_id, task_id,
                                 change_version, task_version,
                                 responder_member_id, code_hash, max_attempts,
                                 created_by, created_device_id,
                                 creation_idempotency_key, created_at, expires_at
                ON secretary_task_change_invitations
                WHEN NEW.workspace_id IS NOT OLD.workspace_id
                  OR NEW.change_id IS NOT OLD.change_id
                  OR NEW.task_id IS NOT OLD.task_id
                  OR NEW.change_version IS NOT OLD.change_version
                  OR NEW.task_version IS NOT OLD.task_version
                  OR NEW.responder_member_id IS NOT OLD.responder_member_id
                  OR NEW.code_hash IS NOT OLD.code_hash
                  OR NEW.max_attempts IS NOT OLD.max_attempts
                  OR NEW.created_by IS NOT OLD.created_by
                  OR NEW.created_device_id IS NOT OLD.created_device_id
                  OR NEW.creation_idempotency_key
                       IS NOT OLD.creation_idempotency_key
                  OR NEW.created_at IS NOT OLD.created_at
                  OR NEW.expires_at IS NOT OLD.expires_at
                BEGIN
                    SELECT RAISE(ABORT, 'task change invitation binding is immutable');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_change_session_binding_insert
                BEFORE INSERT ON secretary_task_change_sessions
                BEGIN
                    SELECT CASE WHEN NOT EXISTS (
                        SELECT 1
                        FROM secretary_task_change_invitations invitation
                        JOIN secretary_task_change_proposals proposal
                          ON proposal.change_id = invitation.change_id
                        JOIN secretary_task_changes change_record
                          ON change_record.id = proposal.change_id
                        WHERE invitation.id = NEW.invitation_id
                          AND invitation.workspace_id = NEW.workspace_id
                          AND invitation.change_id = NEW.change_id
                          AND invitation.task_id = NEW.task_id
                          AND invitation.responder_member_id
                              = NEW.responder_member_id
                          AND proposal.responder_member_id
                              = NEW.responder_member_id
                          AND change_record.status = 'proposed'
                          AND change_record.version
                              = invitation.change_version
                          AND proposal.base_task_version
                              = invitation.task_version
                          AND invitation.revoked_at IS NULL
                          AND invitation.used_at IS NULL
                          AND NEW.expires_at <= invitation.expires_at
                    ) THEN RAISE(ABORT, 'invalid task change session binding') END;
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_change_session_binding_update
                BEFORE UPDATE OF workspace_id, change_id, task_id,
                                 invitation_id, responder_member_id, token_hash,
                                 client_device_id, exchange_idempotency_hash,
                                 exchange_request_hash, assurance_method,
                                 created_at, expires_at
                ON secretary_task_change_sessions
                WHEN NEW.workspace_id IS NOT OLD.workspace_id
                  OR NEW.change_id IS NOT OLD.change_id
                  OR NEW.task_id IS NOT OLD.task_id
                  OR NEW.invitation_id IS NOT OLD.invitation_id
                  OR NEW.responder_member_id IS NOT OLD.responder_member_id
                  OR NEW.token_hash IS NOT OLD.token_hash
                  OR NEW.client_device_id IS NOT OLD.client_device_id
                  OR NEW.exchange_idempotency_hash
                       IS NOT OLD.exchange_idempotency_hash
                  OR NEW.exchange_request_hash IS NOT OLD.exchange_request_hash
                  OR NEW.assurance_method IS NOT OLD.assurance_method
                  OR NEW.created_at IS NOT OLD.created_at
                  OR NEW.expires_at IS NOT OLD.expires_at
                BEGIN
                    SELECT RAISE(ABORT, 'task change session binding is immutable');
                END
                """,
            )
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO secretary_workspace_schema_migrations(version, applied_at)
                VALUES (6, ?)
                """,
                (now,),
            )

        if 7 not in applied:
            v7_object_names = sorted(
                TASK_EXECUTION_V7_TABLES
                | TASK_EXECUTION_V7_INDEXES
                | TASK_EXECUTION_V7_TRIGGERS
            )
            placeholders = ",".join("?" for _ in v7_object_names)
            collisions = connection.execute(
                f"SELECT type, name FROM sqlite_master "
                f"WHERE name IN ({placeholders}) ORDER BY name",
                v7_object_names,
            ).fetchall()
            if collisions:
                names = ",".join(str(row["name"]) for row in collisions)
                raise RuntimeError(
                    "workspace v7 任务执行协议迁移失败："
                    f"发现无迁移标记的同名对象({names})"
                )

            task_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(secretary_business_tasks)"
                ).fetchall()
            }
            if "assignment_epoch" not in task_columns:
                connection.execute(
                    "ALTER TABLE secretary_business_tasks "
                    "ADD COLUMN assignment_epoch INTEGER NOT NULL DEFAULT 1 "
                    "CHECK(assignment_epoch >= 1)"
                )

            statements = (
                """
                CREATE TABLE IF NOT EXISTS secretary_task_execution_invitations (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL
                        REFERENCES secretary_workspaces(id),
                    task_id TEXT NOT NULL
                        REFERENCES secretary_business_tasks(id) ON DELETE CASCADE,
                    agreement_id TEXT NOT NULL
                        REFERENCES secretary_task_alignment_cases(id),
                    assignee_member_id TEXT NOT NULL
                        REFERENCES secretary_workspace_members(id),
                    task_version_at_issue INTEGER NOT NULL
                        CHECK(task_version_at_issue >= 1),
                    assignment_epoch_at_issue INTEGER NOT NULL
                        CHECK(assignment_epoch_at_issue >= 1),
                    code_hash TEXT NOT NULL UNIQUE CHECK(length(code_hash) = 64),
                    failed_attempts INTEGER NOT NULL DEFAULT 0
                        CHECK(failed_attempts >= 0),
                    max_attempts INTEGER NOT NULL DEFAULT 5
                        CHECK(max_attempts >= 1),
                    created_by TEXT NOT NULL
                        REFERENCES secretary_workspace_members(id),
                    created_device_id TEXT NOT NULL
                        CHECK(length(created_device_id) BETWEEN 1 AND 200),
                    creation_idempotency_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    capability_expires_at TEXT NOT NULL,
                    used_at TEXT,
                    revoked_at TEXT,
                    revoke_reason TEXT
                )
                """,
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_secretary_one_active_execution_invitation
                ON secretary_task_execution_invitations(task_id)
                WHERE used_at IS NULL AND revoked_at IS NULL
                """,
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_secretary_execution_invitation_request
                ON secretary_task_execution_invitations(
                    workspace_id, created_by, task_id, creation_idempotency_key
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS
                    idx_secretary_execution_invitation_expiry
                ON secretary_task_execution_invitations(expires_at)
                """,
                """
                CREATE TABLE IF NOT EXISTS
                    secretary_task_execution_refresh_families (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL
                        REFERENCES secretary_workspaces(id),
                    task_id TEXT NOT NULL
                        REFERENCES secretary_business_tasks(id) ON DELETE CASCADE,
                    invitation_id TEXT NOT NULL UNIQUE
                        REFERENCES secretary_task_execution_invitations(id),
                    assignee_member_id TEXT NOT NULL
                        REFERENCES secretary_workspace_members(id),
                    assignment_epoch INTEGER NOT NULL CHECK(assignment_epoch >= 1),
                    client_device_id TEXT NOT NULL
                        CHECK(length(client_device_id) BETWEEN 1 AND 200),
                    created_at TEXT NOT NULL,
                    absolute_expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    revoke_reason TEXT
                )
                """,
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_secretary_one_active_execution_refresh_family
                ON secretary_task_execution_refresh_families(task_id)
                WHERE revoked_at IS NULL
                """,
                """
                CREATE INDEX IF NOT EXISTS
                    idx_secretary_execution_refresh_family_expiry
                ON secretary_task_execution_refresh_families(absolute_expires_at)
                """,
                """
                CREATE TABLE IF NOT EXISTS secretary_task_execution_sessions (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL
                        REFERENCES secretary_workspaces(id),
                    task_id TEXT NOT NULL
                        REFERENCES secretary_business_tasks(id) ON DELETE CASCADE,
                    invitation_id TEXT NOT NULL
                        REFERENCES secretary_task_execution_invitations(id),
                    refresh_family_id TEXT NOT NULL
                        REFERENCES secretary_task_execution_refresh_families(id),
                    access_generation INTEGER NOT NULL
                        CHECK(access_generation >= 1),
                    assignee_member_id TEXT NOT NULL
                        REFERENCES secretary_workspace_members(id),
                    assignment_epoch INTEGER NOT NULL CHECK(assignment_epoch >= 1),
                    token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash) = 64),
                    client_device_id TEXT NOT NULL
                        CHECK(length(client_device_id) BETWEEN 1 AND 200),
                    exchange_idempotency_hash TEXT NOT NULL
                        CHECK(length(exchange_idempotency_hash) = 64),
                    exchange_request_hash TEXT NOT NULL
                        CHECK(length(exchange_request_hash) = 64),
                    assurance_method TEXT NOT NULL
                        CHECK(assurance_method = 'dual_channel_task_execution'),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    revoke_reason TEXT,
                    UNIQUE(refresh_family_id, access_generation)
                )
                """,
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_secretary_one_active_execution_session
                ON secretary_task_execution_sessions(task_id)
                WHERE revoked_at IS NULL
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_secretary_execution_session_expiry
                ON secretary_task_execution_sessions(expires_at)
                """,
                """
                CREATE TABLE IF NOT EXISTS secretary_task_execution_refresh_tokens (
                    id TEXT PRIMARY KEY,
                    family_id TEXT NOT NULL
                        REFERENCES secretary_task_execution_refresh_families(id)
                        ON DELETE CASCADE,
                    generation INTEGER NOT NULL CHECK(generation >= 1),
                    token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash) = 64),
                    created_at TEXT NOT NULL,
                    idle_expires_at TEXT NOT NULL,
                    used_at TEXT,
                    rotation_idempotency_hash TEXT
                        CHECK(rotation_idempotency_hash IS NULL
                              OR length(rotation_idempotency_hash) = 64),
                    rotation_request_hash TEXT
                        CHECK(rotation_request_hash IS NULL
                              OR length(rotation_request_hash) = 64),
                    rotation_response_etag TEXT,
                    rotation_response_json TEXT
                        CHECK(rotation_response_json IS NULL
                              OR json_valid(rotation_response_json)),
                    replacement_token_id TEXT
                        REFERENCES secretary_task_execution_refresh_tokens(id),
                    replacement_session_id TEXT
                        REFERENCES secretary_task_execution_sessions(id),
                    revoked_at TEXT,
                    revoke_reason TEXT,
                    UNIQUE(family_id, generation),
                    CHECK(
                        (used_at IS NULL
                         AND rotation_idempotency_hash IS NULL
                         AND rotation_request_hash IS NULL
                         AND rotation_response_etag IS NULL
                         AND rotation_response_json IS NULL
                         AND replacement_token_id IS NULL
                         AND replacement_session_id IS NULL)
                        OR
                        (used_at IS NOT NULL
                         AND rotation_idempotency_hash IS NOT NULL
                         AND rotation_request_hash IS NOT NULL
                         AND rotation_response_etag IS NOT NULL
                         AND rotation_response_json IS NOT NULL
                         AND replacement_token_id IS NOT NULL
                         AND replacement_session_id IS NOT NULL)
                    )
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS
                    idx_secretary_execution_refresh_token_family
                ON secretary_task_execution_refresh_tokens(family_id, generation)
                """,
                """
                CREATE INDEX IF NOT EXISTS
                    idx_secretary_execution_refresh_token_expiry
                ON secretary_task_execution_refresh_tokens(idle_expires_at)
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_execution_invitation_binding_insert
                BEFORE INSERT ON secretary_task_execution_invitations
                BEGIN
                    SELECT CASE WHEN NOT EXISTS (
                        SELECT 1
                        FROM secretary_business_tasks task
                        JOIN secretary_workspace_members assignee
                          ON assignee.id = task.assignee_member_id
                        JOIN secretary_task_alignment_cases agreement
                          ON agreement.id = NEW.agreement_id
                        WHERE task.id = NEW.task_id
                          AND task.workspace_id = NEW.workspace_id
                          AND task.version = NEW.task_version_at_issue
                          AND task.assignment_epoch
                              = NEW.assignment_epoch_at_issue
                          AND task.deleted_at IS NULL
                          AND task.stage IN ('aligned', 'in_progress')
                          AND task.issuer_member_id = NEW.created_by
                          AND task.issuer_member_id = 'member_owner'
                          AND task.assignee_member_id = NEW.assignee_member_id
                          AND task.assignee_member_id <> task.issuer_member_id
                          AND assignee.workspace_id = NEW.workspace_id
                          AND assignee.kind = 'external'
                          AND assignee.active = 1
                          AND agreement.workspace_id = NEW.workspace_id
                          AND agreement.task_id = NEW.task_id
                          AND agreement.issuer_member_id = task.issuer_member_id
                          AND agreement.assignee_member_id = NEW.assignee_member_id
                          AND agreement.status = 'accepted'
                          AND agreement.accepted_revision_no IS NOT NULL
                          AND NEW.expires_at <= NEW.capability_expires_at
                    ) THEN RAISE(
                        ABORT, 'invalid task execution invitation binding'
                    ) END;
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_execution_invitation_binding_update
                BEFORE UPDATE OF workspace_id, task_id, agreement_id,
                                 assignee_member_id, task_version_at_issue,
                                 assignment_epoch_at_issue,
                                 code_hash, max_attempts, created_by,
                                 created_device_id, creation_idempotency_key,
                                 created_at, expires_at, capability_expires_at
                ON secretary_task_execution_invitations
                WHEN NEW.workspace_id IS NOT OLD.workspace_id
                  OR NEW.task_id IS NOT OLD.task_id
                  OR NEW.agreement_id IS NOT OLD.agreement_id
                  OR NEW.assignee_member_id IS NOT OLD.assignee_member_id
                  OR NEW.task_version_at_issue IS NOT OLD.task_version_at_issue
                  OR NEW.assignment_epoch_at_issue
                       IS NOT OLD.assignment_epoch_at_issue
                  OR NEW.code_hash IS NOT OLD.code_hash
                  OR NEW.max_attempts IS NOT OLD.max_attempts
                  OR NEW.created_by IS NOT OLD.created_by
                  OR NEW.created_device_id IS NOT OLD.created_device_id
                  OR NEW.creation_idempotency_key
                       IS NOT OLD.creation_idempotency_key
                  OR NEW.created_at IS NOT OLD.created_at
                  OR NEW.expires_at IS NOT OLD.expires_at
                  OR NEW.capability_expires_at IS NOT OLD.capability_expires_at
                BEGIN
                    SELECT RAISE(
                        ABORT, 'task execution invitation binding is immutable'
                    );
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_task_assignment_epoch_update
                BEFORE UPDATE OF assignee_member_id, assignment_epoch
                ON secretary_business_tasks
                WHEN (
                    NEW.assignee_member_id IS NOT OLD.assignee_member_id
                    AND NEW.assignment_epoch <> OLD.assignment_epoch + 1
                ) OR (
                    NEW.assignee_member_id IS OLD.assignee_member_id
                    AND NEW.assignment_epoch <> OLD.assignment_epoch
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid task assignment epoch transition');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_execution_refresh_family_binding_insert
                BEFORE INSERT ON secretary_task_execution_refresh_families
                BEGIN
                    SELECT CASE WHEN NOT EXISTS (
                        SELECT 1
                        FROM secretary_task_execution_invitations invitation
                        JOIN secretary_business_tasks task
                          ON task.id = invitation.task_id
                        WHERE invitation.id = NEW.invitation_id
                          AND invitation.workspace_id = NEW.workspace_id
                          AND invitation.task_id = NEW.task_id
                          AND invitation.assignee_member_id
                              = NEW.assignee_member_id
                          AND invitation.assignment_epoch_at_issue
                              = NEW.assignment_epoch
                          AND invitation.revoked_at IS NULL
                          AND task.workspace_id = NEW.workspace_id
                          AND task.deleted_at IS NULL
                          AND task.stage IN ('aligned', 'in_progress')
                          AND task.assignee_member_id = NEW.assignee_member_id
                          AND task.assignment_epoch = NEW.assignment_epoch
                          AND NEW.absolute_expires_at
                              <= invitation.capability_expires_at
                    ) THEN RAISE(
                        ABORT, 'invalid task execution refresh family binding'
                    ) END;
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_execution_refresh_family_binding_update
                BEFORE UPDATE OF workspace_id, task_id, invitation_id,
                                 assignee_member_id, assignment_epoch,
                                 client_device_id, created_at, absolute_expires_at
                ON secretary_task_execution_refresh_families
                WHEN NEW.workspace_id IS NOT OLD.workspace_id
                  OR NEW.task_id IS NOT OLD.task_id
                  OR NEW.invitation_id IS NOT OLD.invitation_id
                  OR NEW.assignee_member_id IS NOT OLD.assignee_member_id
                  OR NEW.assignment_epoch IS NOT OLD.assignment_epoch
                  OR NEW.client_device_id IS NOT OLD.client_device_id
                  OR NEW.created_at IS NOT OLD.created_at
                  OR NEW.absolute_expires_at IS NOT OLD.absolute_expires_at
                BEGIN
                    SELECT RAISE(
                        ABORT, 'task execution refresh family binding is immutable'
                    );
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_execution_session_binding_insert
                BEFORE INSERT ON secretary_task_execution_sessions
                BEGIN
                    SELECT CASE WHEN NOT EXISTS (
                        SELECT 1
                        FROM secretary_task_execution_invitations invitation
                        JOIN secretary_task_execution_refresh_families family
                          ON family.invitation_id = invitation.id
                        JOIN secretary_business_tasks task
                          ON task.id = invitation.task_id
                        JOIN secretary_workspace_members assignee
                          ON assignee.id = invitation.assignee_member_id
                        JOIN secretary_task_alignment_cases agreement
                          ON agreement.id = invitation.agreement_id
                        WHERE invitation.id = NEW.invitation_id
                          AND family.id = NEW.refresh_family_id
                          AND family.workspace_id = NEW.workspace_id
                          AND family.task_id = NEW.task_id
                          AND family.assignee_member_id
                              = NEW.assignee_member_id
                          AND family.assignment_epoch = NEW.assignment_epoch
                          AND family.client_device_id = NEW.client_device_id
                          AND family.revoked_at IS NULL
                          AND invitation.workspace_id = NEW.workspace_id
                          AND invitation.task_id = NEW.task_id
                          AND invitation.assignee_member_id
                              = NEW.assignee_member_id
                          AND invitation.revoked_at IS NULL
                          AND task.workspace_id = NEW.workspace_id
                          AND task.deleted_at IS NULL
                          AND task.stage IN ('aligned', 'in_progress', 'submitted')
                          AND task.assignee_member_id = NEW.assignee_member_id
                          AND task.assignment_epoch = NEW.assignment_epoch
                          AND task.issuer_member_id = 'member_owner'
                          AND task.assignee_member_id <> task.issuer_member_id
                          AND assignee.workspace_id = NEW.workspace_id
                          AND assignee.kind = 'external'
                          AND assignee.active = 1
                          AND agreement.status = 'accepted'
                          AND agreement.task_id = NEW.task_id
                          AND agreement.assignee_member_id
                              = NEW.assignee_member_id
                          AND NEW.expires_at <= family.absolute_expires_at
                    ) THEN RAISE(
                        ABORT, 'invalid task execution session binding'
                    ) END;
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_execution_session_binding_update
                BEFORE UPDATE OF workspace_id, task_id, invitation_id,
                                 refresh_family_id, access_generation,
                                 assignee_member_id, token_hash,
                                 assignment_epoch,
                                 client_device_id, exchange_idempotency_hash,
                                 exchange_request_hash, assurance_method,
                                 created_at, expires_at
                ON secretary_task_execution_sessions
                WHEN NEW.workspace_id IS NOT OLD.workspace_id
                  OR NEW.task_id IS NOT OLD.task_id
                  OR NEW.invitation_id IS NOT OLD.invitation_id
                  OR NEW.refresh_family_id IS NOT OLD.refresh_family_id
                  OR NEW.access_generation IS NOT OLD.access_generation
                  OR NEW.assignee_member_id IS NOT OLD.assignee_member_id
                  OR NEW.assignment_epoch IS NOT OLD.assignment_epoch
                  OR NEW.token_hash IS NOT OLD.token_hash
                  OR NEW.client_device_id IS NOT OLD.client_device_id
                  OR NEW.exchange_idempotency_hash
                       IS NOT OLD.exchange_idempotency_hash
                  OR NEW.exchange_request_hash IS NOT OLD.exchange_request_hash
                  OR NEW.assurance_method IS NOT OLD.assurance_method
                  OR NEW.created_at IS NOT OLD.created_at
                  OR NEW.expires_at IS NOT OLD.expires_at
                BEGIN
                    SELECT RAISE(
                        ABORT, 'task execution session binding is immutable'
                    );
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_execution_refresh_token_binding_insert
                BEFORE INSERT ON secretary_task_execution_refresh_tokens
                BEGIN
                    SELECT CASE WHEN NOT EXISTS (
                        SELECT 1
                        FROM secretary_task_execution_refresh_families family
                        WHERE family.id = NEW.family_id
                          AND family.revoked_at IS NULL
                          AND NEW.idle_expires_at <= family.absolute_expires_at
                    ) THEN RAISE(
                        ABORT, 'invalid task execution refresh token binding'
                    ) END;
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_execution_refresh_token_binding_update
                BEFORE UPDATE OF family_id, generation, token_hash,
                                 created_at, idle_expires_at
                ON secretary_task_execution_refresh_tokens
                WHEN NEW.family_id IS NOT OLD.family_id
                  OR NEW.generation IS NOT OLD.generation
                  OR NEW.token_hash IS NOT OLD.token_hash
                  OR NEW.created_at IS NOT OLD.created_at
                  OR NEW.idle_expires_at IS NOT OLD.idle_expires_at
                BEGIN
                    SELECT RAISE(
                        ABORT, 'task execution refresh token binding is immutable'
                    );
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_task_execution_access_revoke
                AFTER UPDATE OF assignee_member_id, assignment_epoch, stage, deleted_at
                ON secretary_business_tasks
                WHEN NEW.assignee_member_id IS NOT OLD.assignee_member_id
                  OR NEW.assignment_epoch <> OLD.assignment_epoch
                  OR (OLD.stage NOT IN ('accepted', 'abnormal_closed')
                      AND NEW.stage IN ('accepted', 'abnormal_closed'))
                  OR (OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL)
                BEGIN
                    UPDATE secretary_task_execution_invitations
                    SET revoked_at = COALESCE(revoked_at, NEW.updated_at),
                        revoke_reason = COALESCE(
                            revoke_reason,
                            CASE
                              WHEN NEW.deleted_at IS NOT NULL THEN 'task_deleted'
                              WHEN NEW.stage IN ('accepted', 'abnormal_closed')
                                THEN 'task_terminal'
                              ELSE 'assignment_changed'
                            END
                        )
                    WHERE task_id = NEW.id AND revoked_at IS NULL;
                    UPDATE secretary_task_execution_refresh_families
                    SET revoked_at = COALESCE(revoked_at, NEW.updated_at),
                        revoke_reason = COALESCE(
                            revoke_reason,
                            CASE
                              WHEN NEW.deleted_at IS NOT NULL THEN 'task_deleted'
                              WHEN NEW.stage IN ('accepted', 'abnormal_closed')
                                THEN 'task_terminal'
                              ELSE 'assignment_changed'
                            END
                        )
                    WHERE task_id = NEW.id AND revoked_at IS NULL;
                    UPDATE secretary_task_execution_sessions
                    SET revoked_at = COALESCE(revoked_at, NEW.updated_at),
                        revoke_reason = COALESCE(
                            revoke_reason,
                            CASE
                              WHEN NEW.deleted_at IS NOT NULL THEN 'task_deleted'
                              WHEN NEW.stage IN ('accepted', 'abnormal_closed')
                                THEN 'task_terminal'
                              ELSE 'assignment_changed'
                            END
                        )
                    WHERE task_id = NEW.id AND revoked_at IS NULL;
                    UPDATE secretary_task_execution_refresh_tokens
                    SET revoked_at = COALESCE(revoked_at, NEW.updated_at),
                        revoke_reason = COALESCE(
                            revoke_reason,
                            CASE
                              WHEN NEW.deleted_at IS NOT NULL THEN 'task_deleted'
                              WHEN NEW.stage IN ('accepted', 'abnormal_closed')
                                THEN 'task_terminal'
                              ELSE 'assignment_changed'
                            END
                        )
                    WHERE family_id IN (
                        SELECT id FROM secretary_task_execution_refresh_families
                        WHERE task_id = NEW.id
                    ) AND revoked_at IS NULL;
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS
                    trg_secretary_member_execution_access_revoke
                AFTER UPDATE OF active ON secretary_workspace_members
                WHEN OLD.active = 1 AND NEW.active = 0
                BEGIN
                    UPDATE secretary_task_execution_invitations
                    SET revoked_at = COALESCE(revoked_at, NEW.updated_at),
                        revoke_reason = COALESCE(
                            revoke_reason, 'assignee_deactivated'
                        )
                    WHERE workspace_id = NEW.workspace_id
                      AND assignee_member_id = NEW.id AND revoked_at IS NULL;
                    UPDATE secretary_task_execution_refresh_families
                    SET revoked_at = COALESCE(revoked_at, NEW.updated_at),
                        revoke_reason = COALESCE(
                            revoke_reason, 'assignee_deactivated'
                        )
                    WHERE workspace_id = NEW.workspace_id
                      AND assignee_member_id = NEW.id AND revoked_at IS NULL;
                    UPDATE secretary_task_execution_sessions
                    SET revoked_at = COALESCE(revoked_at, NEW.updated_at),
                        revoke_reason = COALESCE(
                            revoke_reason, 'assignee_deactivated'
                        )
                    WHERE workspace_id = NEW.workspace_id
                      AND assignee_member_id = NEW.id AND revoked_at IS NULL;
                    UPDATE secretary_task_execution_refresh_tokens
                    SET revoked_at = COALESCE(revoked_at, NEW.updated_at),
                        revoke_reason = COALESCE(
                            revoke_reason, 'assignee_deactivated'
                        )
                    WHERE family_id IN (
                        SELECT id FROM secretary_task_execution_refresh_families
                        WHERE workspace_id = NEW.workspace_id
                          AND assignee_member_id = NEW.id
                    ) AND revoked_at IS NULL;
                END
                """,
            )
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO secretary_workspace_schema_migrations(version, applied_at)
                VALUES (7, ?)
                """,
                (now,),
            )

        self._verify_task_change_protocol_schema(connection)
        self._verify_task_execution_protocol_schema(connection)

        unknown = connection.execute(
            """
            SELECT version FROM secretary_workspace_schema_migrations
            WHERE version > ? ORDER BY version LIMIT 1
            """,
            (WORKSPACE_SCHEMA_VERSION,),
        ).fetchone()
        if unknown is not None:
            raise RuntimeError(
                f"workspace 数据库版本高于当前服务支持版本：{unknown['version']}"
            )

    @staticmethod
    def _verify_task_change_protocol_schema(connection: sqlite3.Connection) -> None:
        expected_objects = {
            **{name: "table" for name in TASK_CHANGE_V6_TABLES},
            **{name: "index" for name in TASK_CHANGE_V6_INDEXES},
            **{name: "trigger" for name in TASK_CHANGE_V6_TRIGGERS},
        }
        placeholders = ",".join("?" for _ in expected_objects)
        rows = connection.execute(
            f"SELECT type, name, sql FROM sqlite_master "
            f"WHERE name IN ({placeholders})",
            sorted(expected_objects),
        ).fetchall()
        actual = {str(row["name"]): row for row in rows}
        missing_or_wrong = sorted(
            name
            for name, object_type in expected_objects.items()
            if name not in actual or actual[name]["type"] != object_type
        )
        if missing_or_wrong:
            raise RuntimeError(
                "workspace v6 任务变更协议 schema 对象缺失或类型错误："
                + ",".join(missing_or_wrong)
            )

        expected_columns = {
            "secretary_task_change_proposals": (
                "change_id",
                "workspace_id",
                "task_id",
                "proposer_member_id",
                "responder_member_id",
                "base_task_version",
                "digest",
                "canonical_json",
                "created_at",
            ),
            "secretary_task_change_invitations": (
                "id",
                "workspace_id",
                "change_id",
                "task_id",
                "change_version",
                "task_version",
                "responder_member_id",
                "code_hash",
                "failed_attempts",
                "max_attempts",
                "created_by",
                "created_device_id",
                "creation_idempotency_key",
                "created_at",
                "expires_at",
                "used_at",
                "revoked_at",
            ),
            "secretary_task_change_sessions": (
                "id",
                "workspace_id",
                "change_id",
                "task_id",
                "invitation_id",
                "responder_member_id",
                "token_hash",
                "client_device_id",
                "exchange_idempotency_hash",
                "exchange_request_hash",
                "assurance_method",
                "created_at",
                "expires_at",
                "revoked_at",
                "revoke_reason",
            ),
            "secretary_task_change_decisions": (
                "id",
                "change_id",
                "proposal_digest",
                "action",
                "actor_member_id",
                "actor_session_id",
                "assurance_method",
                "reason",
                "client_mutation_id",
                "version",
                "created_at",
            ),
        }
        expected_primary_keys = {
            "secretary_task_change_proposals": "change_id",
            "secretary_task_change_invitations": "id",
            "secretary_task_change_sessions": "id",
            "secretary_task_change_decisions": "id",
        }
        expected_foreign_keys = {
            "secretary_task_change_proposals": {
                ("change_id", "secretary_task_changes", "id"),
                ("workspace_id", "secretary_workspaces", "id"),
                ("task_id", "secretary_business_tasks", "id"),
                ("proposer_member_id", "secretary_workspace_members", "id"),
                ("responder_member_id", "secretary_workspace_members", "id"),
            },
            "secretary_task_change_invitations": {
                ("workspace_id", "secretary_workspaces", "id"),
                ("change_id", "secretary_task_changes", "id"),
                ("task_id", "secretary_business_tasks", "id"),
                ("responder_member_id", "secretary_workspace_members", "id"),
                ("created_by", "secretary_workspace_members", "id"),
            },
            "secretary_task_change_sessions": {
                ("workspace_id", "secretary_workspaces", "id"),
                ("change_id", "secretary_task_changes", "id"),
                ("task_id", "secretary_business_tasks", "id"),
                ("invitation_id", "secretary_task_change_invitations", "id"),
                ("responder_member_id", "secretary_workspace_members", "id"),
            },
            "secretary_task_change_decisions": {
                ("change_id", "secretary_task_changes", "id"),
                ("actor_member_id", "secretary_workspace_members", "id"),
                ("actor_session_id", "secretary_task_change_sessions", "id"),
            },
        }
        required_table_sql = {
            "secretary_task_change_proposals": (
                "check(length(digest) = 71",
                "check(json_valid(canonical_json)",
            ),
            "secretary_task_change_invitations": (
                "check(change_version >= 1)",
                "check(length(code_hash) = 64)",
                "check(max_attempts >= 1)",
            ),
            "secretary_task_change_sessions": (
                "check(length(token_hash) = 64)",
                "check(assurance_method = 'dual_channel_capability')",
            ),
            "secretary_task_change_decisions": (
                "check(action in ('accept', 'reject', 'cancel'))",
                "check(version = 1)",
                "unique(change_id, client_mutation_id)",
            ),
        }
        for table, column_names in expected_columns.items():
            columns = connection.execute(f"PRAGMA table_info({table})").fetchall()
            if tuple(str(row["name"]) for row in columns) != column_names:
                raise RuntimeError(f"workspace v6 表字段结构无效：{table}")
            primary_keys = [
                str(row["name"]) for row in columns if int(row["pk"]) == 1
            ]
            if primary_keys != [expected_primary_keys[table]]:
                raise RuntimeError(f"workspace v6 表主键结构无效：{table}")
            foreign_keys = {
                (str(row["from"]), str(row["table"]), str(row["to"]))
                for row in connection.execute(
                    f"PRAGMA foreign_key_list({table})"
                ).fetchall()
            }
            if foreign_keys != expected_foreign_keys[table]:
                raise RuntimeError(f"workspace v6 表外键结构无效：{table}")
            normalized_sql = " ".join(str(actual[table]["sql"]).lower().split())
            if any(
                " ".join(fragment.lower().split()) not in normalized_sql
                for fragment in required_table_sql[table]
            ):
                raise RuntimeError(f"workspace v6 表约束结构无效：{table}")

        expected_named_indexes = {
            "idx_secretary_change_proposal_task": (
                False,
                False,
                ("task_id", "created_at"),
            ),
            "idx_secretary_one_pending_protocol_change_per_task": (
                True,
                True,
                ("task_id",),
            ),
            "idx_secretary_one_active_change_invitation": (
                True,
                True,
                ("change_id",),
            ),
            "idx_secretary_change_invitation_request": (
                True,
                False,
                (
                    "workspace_id",
                    "created_by",
                    "change_id",
                    "creation_idempotency_key",
                ),
            ),
            "idx_secretary_change_invitation_expiry": (
                False,
                False,
                ("expires_at",),
            ),
            "idx_secretary_one_active_change_session": (
                True,
                True,
                ("change_id",),
            ),
            "idx_secretary_change_session_expiry": (
                False,
                False,
                ("expires_at",),
            ),
        }
        index_metadata: dict[str, tuple[bool, bool, tuple[str, ...]]] = {}
        unique_column_sets: set[tuple[str, tuple[str, ...]]] = set()
        for table in [*expected_columns, "secretary_task_changes"]:
            for index in connection.execute(f"PRAGMA index_list({table})").fetchall():
                index_name = str(index["name"])
                is_unique = bool(index["unique"])
                is_partial = bool(index["partial"])
                index_columns = tuple(
                    str(row["name"])
                    for row in connection.execute(
                        f"PRAGMA index_info({index_name})"
                    ).fetchall()
                )
                index_metadata[index_name] = (
                    is_unique,
                    is_partial,
                    index_columns,
                )
                if is_unique:
                    unique_column_sets.add((table, index_columns))
        if any(
            index_metadata.get(name) != signature
            for name, signature in expected_named_indexes.items()
        ):
            raise RuntimeError("workspace v6 任务变更协议索引结构无效")
        required_index_sql = {
            "idx_secretary_one_pending_protocol_change_per_task": (
                "create unique index "
                "idx_secretary_one_pending_protocol_change_per_task "
                "on secretary_task_changes(task_id) where status = 'proposed'"
            ),
            "idx_secretary_one_active_change_invitation": (
                "create unique index idx_secretary_one_active_change_invitation "
                "on secretary_task_change_invitations(change_id) "
                "where revoked_at is null"
            ),
            "idx_secretary_one_active_change_session": (
                "create unique index idx_secretary_one_active_change_session "
                "on secretary_task_change_sessions(change_id) "
                "where revoked_at is null"
            ),
        }
        if any(
            " ".join(str(actual[name]["sql"]).lower().split()) != expected_sql
            for name, expected_sql in required_index_sql.items()
        ):
            raise RuntimeError("workspace v6 任务变更协议部分唯一索引无效")
        required_unique_columns = {
            ("secretary_task_change_proposals", ("digest",)),
            ("secretary_task_change_sessions", ("invitation_id",)),
            ("secretary_task_change_sessions", ("token_hash",)),
            ("secretary_task_change_decisions", ("change_id",)),
            (
                "secretary_task_change_decisions",
                ("change_id", "client_mutation_id"),
            ),
        }
        if not required_unique_columns.issubset(unique_column_sets):
            raise RuntimeError("workspace v6 任务变更协议唯一性约束无效")

        required_trigger_fragments = {
            "trg_secretary_change_proposal_binding_insert": (
                "task.issuer_member_id = new.proposer_member_id",
                "task.assignee_member_id = new.responder_member_id",
                "from json_each(new.canonical_json)",
            ),
            "trg_secretary_change_proposal_immutable_update": (
                "task change proposal is immutable",
            ),
            "trg_secretary_change_proposal_immutable_delete": (
                "task change proposal is immutable",
            ),
            "trg_secretary_bound_change_fields_immutable": (
                "bound task change fields are immutable",
            ),
            "trg_secretary_bound_change_immutable_delete": (
                "bound task change is immutable",
            ),
            "trg_secretary_change_decision_binding_insert": (
                "proposal.digest = new.proposal_digest",
                "session.change_id = proposal.change_id",
            ),
            "trg_secretary_change_decision_immutable_update": (
                "task change decision is immutable",
            ),
            "trg_secretary_change_decision_immutable_delete": (
                "task change decision is immutable",
            ),
            "trg_secretary_bound_change_state_transition": (
                "decision.actor_member_id = new.decided_by",
                "invalid bound task change state transition",
            ),
            "trg_secretary_change_invitation_binding_insert": (
                "proposal.responder_member_id = new.responder_member_id",
            ),
            "trg_secretary_change_invitation_binding_update": (
                "invitation binding is immutable",
            ),
            "trg_secretary_change_session_binding_insert": (
                "invitation.id = new.invitation_id",
                "invalid task change session binding",
            ),
            "trg_secretary_change_session_binding_update": (
                "session binding is immutable",
            ),
        }
        for trigger_name, fragments in required_trigger_fragments.items():
            normalized_sql = " ".join(
                str(actual[trigger_name]["sql"]).lower().split()
            )
            if any(fragment not in normalized_sql for fragment in fragments):
                raise RuntimeError(
                    f"workspace v6 任务变更触发器绑定无效：{trigger_name}"
                )

        proposal_rows = connection.execute(
            "SELECT * FROM secretary_task_change_proposals ORDER BY change_id"
        ).fetchall()
        for proposal in proposal_rows:
            change = connection.execute(
                "SELECT * FROM secretary_task_changes WHERE id = ?",
                (proposal["change_id"],),
            ).fetchone()
            if change is None:
                raise RuntimeError("workspace v6 任务变更提案缺少主记录")
            try:
                document = WorkspaceService._task_change_proposal_document(proposal)
            except PocketError as error:
                raise RuntimeError(error.detail) from error
            expected_document_fields = {
                "workspace_id": change["workspace_id"],
                "task_id": change["task_id"],
                "change_id": change["id"],
                "change_type": change["change_type"],
                "base_task_version": change["base_version"],
                "proposer_member_id": proposal["proposer_member_id"],
                "responder_member_id": proposal["responder_member_id"],
                "before": json_loads(change["before_json"], None),
                "patch": json_loads(change["patch_json"], None),
                "reason": change["reason"],
            }
            if (
                proposal["workspace_id"] != change["workspace_id"]
                or proposal["task_id"] != change["task_id"]
                or proposal["base_task_version"] != change["base_version"]
                or proposal["created_at"] != change["proposed_at"]
                or any(
                    document.get(key) != value
                    for key, value in expected_document_fields.items()
                )
            ):
                raise RuntimeError("workspace v6 任务变更提案绑定无效")
            decision = connection.execute(
                """
                SELECT * FROM secretary_task_change_decisions WHERE change_id = ?
                """,
                (proposal["change_id"],),
            ).fetchone()
            try:
                WorkspaceService._require_task_change_decision_binding(
                    connection, proposal, change, decision
                )
            except PocketError as error:
                raise RuntimeError(error.detail) from error

    def _verify_task_execution_protocol_schema(
        self, connection: sqlite3.Connection
    ) -> None:
        expected_objects = {
            **{name: "table" for name in TASK_EXECUTION_V7_TABLES},
            **{name: "index" for name in TASK_EXECUTION_V7_INDEXES},
            **{name: "trigger" for name in TASK_EXECUTION_V7_TRIGGERS},
        }
        placeholders = ",".join("?" for _ in expected_objects)
        rows = connection.execute(
            f"SELECT type, name, sql FROM sqlite_master "
            f"WHERE name IN ({placeholders})",
            sorted(expected_objects),
        ).fetchall()
        actual = {str(row["name"]): row for row in rows}
        missing_or_wrong = sorted(
            name
            for name, object_type in expected_objects.items()
            if name not in actual or actual[name]["type"] != object_type
        )
        if missing_or_wrong:
            raise RuntimeError(
                "workspace v7 任务执行协议 schema 对象缺失或类型错误："
                + ",".join(missing_or_wrong)
            )

        if set(TASK_EXECUTION_V7_SQL_DIGESTS) != set(expected_objects):
            raise RuntimeError("workspace v7 规范 schema 摘要清单不完整")
        for name, expected_digest in TASK_EXECUTION_V7_SQL_DIGESTS.items():
            # Preserve quoted literal case. SQLite string comparisons are
            # case-sensitive here, so lower-casing the full DDL would make a
            # forged 'ACCEPTED' literal hash like the canonical 'accepted'.
            normalized_sql = " ".join(str(actual[name]["sql"]).split())
            actual_digest = hashlib.sha256(
                normalized_sql.encode("utf-8")
            ).hexdigest()
            if not secrets.compare_digest(actual_digest, expected_digest):
                raise RuntimeError(
                    f"workspace v7 schema 定义与规范不一致：{name}"
                )

        task_columns = connection.execute(
            "PRAGMA table_info(secretary_business_tasks)"
        ).fetchall()
        assignment_epoch = next(
            (
                row
                for row in task_columns
                if str(row["name"]) == "assignment_epoch"
            ),
            None,
        )
        if (
            assignment_epoch is None
            or str(assignment_epoch["type"]).upper() != "INTEGER"
            or int(assignment_epoch["notnull"]) != 1
            or str(assignment_epoch["dflt_value"]) != "1"
        ):
            raise RuntimeError("workspace v7 任务 assignment_epoch 结构无效")
        task_table_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'secretary_business_tasks'"
        ).fetchone()
        normalized_task_sql = (
            " ".join(str(task_table_sql["sql"]).lower().split())
            if task_table_sql is not None
            else ""
        )
        if not normalized_task_sql.endswith(
            ", assignment_epoch integer not null default 1 "
            "check(assignment_epoch >= 1))"
        ):
            raise RuntimeError("workspace v7 任务 assignment_epoch 约束无效")

        expected_columns = {
            "secretary_task_execution_invitations": (
                "id", "workspace_id", "task_id", "agreement_id",
                "assignee_member_id", "task_version_at_issue",
                "assignment_epoch_at_issue", "code_hash", "failed_attempts",
                "max_attempts", "created_by", "created_device_id",
                "creation_idempotency_key", "created_at", "expires_at",
                "capability_expires_at", "used_at", "revoked_at",
                "revoke_reason",
            ),
            "secretary_task_execution_refresh_families": (
                "id", "workspace_id", "task_id", "invitation_id",
                "assignee_member_id", "assignment_epoch", "client_device_id",
                "created_at", "absolute_expires_at", "revoked_at",
                "revoke_reason",
            ),
            "secretary_task_execution_sessions": (
                "id", "workspace_id", "task_id", "invitation_id",
                "refresh_family_id", "access_generation",
                "assignee_member_id", "assignment_epoch", "token_hash",
                "client_device_id", "exchange_idempotency_hash",
                "exchange_request_hash", "assurance_method", "created_at",
                "expires_at", "revoked_at", "revoke_reason",
            ),
            "secretary_task_execution_refresh_tokens": (
                "id", "family_id", "generation", "token_hash", "created_at",
                "idle_expires_at", "used_at", "rotation_idempotency_hash",
                "rotation_request_hash", "rotation_response_etag",
                "rotation_response_json", "replacement_token_id",
                "replacement_session_id", "revoked_at", "revoke_reason",
            ),
        }
        for table, expected in expected_columns.items():
            columns = connection.execute(f"PRAGMA table_info({table})").fetchall()
            if tuple(str(row["name"]) for row in columns) != expected:
                raise RuntimeError(f"workspace v7 表字段结构无效：{table}")
            primary_keys = [
                str(row["name"]) for row in columns if int(row["pk"]) == 1
            ]
            if primary_keys != ["id"]:
                raise RuntimeError(f"workspace v7 表主键结构无效：{table}")

        expected_foreign_keys = {
            "secretary_task_execution_invitations": {
                ("workspace_id", "secretary_workspaces", "id"),
                ("task_id", "secretary_business_tasks", "id"),
                ("agreement_id", "secretary_task_alignment_cases", "id"),
                ("assignee_member_id", "secretary_workspace_members", "id"),
                ("created_by", "secretary_workspace_members", "id"),
            },
            "secretary_task_execution_sessions": {
                ("workspace_id", "secretary_workspaces", "id"),
                ("task_id", "secretary_business_tasks", "id"),
                (
                    "invitation_id",
                    "secretary_task_execution_invitations",
                    "id",
                ),
                (
                    "refresh_family_id",
                    "secretary_task_execution_refresh_families",
                    "id",
                ),
                ("assignee_member_id", "secretary_workspace_members", "id"),
            },
            "secretary_task_execution_refresh_families": {
                ("workspace_id", "secretary_workspaces", "id"),
                ("task_id", "secretary_business_tasks", "id"),
                (
                    "invitation_id",
                    "secretary_task_execution_invitations",
                    "id",
                ),
                ("assignee_member_id", "secretary_workspace_members", "id"),
            },
            "secretary_task_execution_refresh_tokens": {
                (
                    "family_id",
                    "secretary_task_execution_refresh_families",
                    "id",
                ),
                (
                    "replacement_token_id",
                    "secretary_task_execution_refresh_tokens",
                    "id",
                ),
                (
                    "replacement_session_id",
                    "secretary_task_execution_sessions",
                    "id",
                ),
            },
        }
        for table, expected in expected_foreign_keys.items():
            foreign_keys = {
                (str(row["from"]), str(row["table"]), str(row["to"]))
                for row in connection.execute(
                    f"PRAGMA foreign_key_list({table})"
                ).fetchall()
            }
            if foreign_keys != expected:
                raise RuntimeError(f"workspace v7 表外键结构无效：{table}")

        expected_indexes = {
            "idx_secretary_one_active_execution_invitation": (
                True,
                True,
                ("task_id",),
            ),
            "idx_secretary_execution_invitation_request": (
                True,
                False,
                (
                    "workspace_id",
                    "created_by",
                    "task_id",
                    "creation_idempotency_key",
                ),
            ),
            "idx_secretary_execution_invitation_expiry": (
                False,
                False,
                ("expires_at",),
            ),
            "idx_secretary_one_active_execution_session": (
                True,
                True,
                ("task_id",),
            ),
            "idx_secretary_execution_session_expiry": (
                False,
                False,
                ("expires_at",),
            ),
            "idx_secretary_one_active_execution_refresh_family": (
                True,
                True,
                ("task_id",),
            ),
            "idx_secretary_execution_refresh_family_expiry": (
                False,
                False,
                ("absolute_expires_at",),
            ),
            "idx_secretary_execution_refresh_token_family": (
                False,
                False,
                ("family_id", "generation"),
            ),
            "idx_secretary_execution_refresh_token_expiry": (
                False,
                False,
                ("idle_expires_at",),
            ),
        }
        metadata: dict[str, tuple[bool, bool, tuple[str, ...]]] = {}
        unique_column_sets: dict[str, set[tuple[str, ...]]] = {
            table: set() for table in expected_columns
        }
        for table in expected_columns:
            for index in connection.execute(f"PRAGMA index_list({table})").fetchall():
                name = str(index["name"])
                columns = tuple(
                    str(row["name"])
                    for row in connection.execute(
                        f"PRAGMA index_info({name})"
                    ).fetchall()
                )
                metadata[name] = (
                    bool(index["unique"]),
                    bool(index["partial"]),
                    columns,
                )
                if bool(index["unique"]):
                    unique_column_sets[table].add(columns)
        if any(metadata.get(name) != value for name, value in expected_indexes.items()):
            raise RuntimeError("workspace v7 任务执行协议索引结构无效")
        required_unique_columns = {
            "secretary_task_execution_invitations": {("code_hash",)},
            "secretary_task_execution_refresh_families": {("invitation_id",)},
            "secretary_task_execution_sessions": {
                ("token_hash",),
                ("refresh_family_id", "access_generation"),
            },
            "secretary_task_execution_refresh_tokens": {
                ("token_hash",),
                ("family_id", "generation"),
            },
        }
        if any(
            not required.issubset(unique_column_sets[table])
            for table, required in required_unique_columns.items()
        ):
            raise RuntimeError("workspace v7 任务执行协议唯一性约束无效")

        invitations = connection.execute(
            "SELECT * FROM secretary_task_execution_invitations ORDER BY id"
        ).fetchall()
        for invitation in invitations:
            if (
                len(str(invitation["code_hash"])) != 64
                or parse_utc(invitation["created_at"])
                >= parse_utc(invitation["expires_at"])
                or parse_utc(invitation["expires_at"])
                > parse_utc(invitation["capability_expires_at"])
            ):
                raise RuntimeError("workspace v7 任务执行邀请记录无效")

        families = connection.execute(
            "SELECT * FROM secretary_task_execution_refresh_families ORDER BY id"
        ).fetchall()
        for family in families:
            invitation = connection.execute(
                "SELECT * FROM secretary_task_execution_invitations WHERE id = ?",
                (family["invitation_id"],),
            ).fetchone()
            if (
                invitation is None
                or invitation["workspace_id"] != family["workspace_id"]
                or invitation["task_id"] != family["task_id"]
                or invitation["assignee_member_id"]
                != family["assignee_member_id"]
                or invitation["assignment_epoch_at_issue"]
                != family["assignment_epoch"]
                or parse_utc(family["created_at"])
                >= parse_utc(family["absolute_expires_at"])
                or parse_utc(family["absolute_expires_at"])
                > parse_utc(invitation["capability_expires_at"])
                or (
                    parse_utc(family["absolute_expires_at"])
                    - parse_utc(family["created_at"])
                ).total_seconds() > TASK_EXECUTION_REFRESH_ABSOLUTE_TTL_SECONDS
            ):
                raise RuntimeError("workspace v7 任务执行 refresh family 绑定无效")

        sessions = connection.execute(
            "SELECT * FROM secretary_task_execution_sessions ORDER BY id"
        ).fetchall()
        for session in sessions:
            invitation = connection.execute(
                "SELECT * FROM secretary_task_execution_invitations WHERE id = ?",
                (session["invitation_id"],),
            ).fetchone()
            family = connection.execute(
                "SELECT * FROM secretary_task_execution_refresh_families "
                "WHERE id = ?",
                (session["refresh_family_id"],),
            ).fetchone()
            if (
                invitation is None
                or family is None
                or invitation["workspace_id"] != session["workspace_id"]
                or invitation["task_id"] != session["task_id"]
                or invitation["assignee_member_id"]
                != session["assignee_member_id"]
                or family["invitation_id"] != session["invitation_id"]
                or family["assignment_epoch"] != session["assignment_epoch"]
                or family["client_device_id"] != session["client_device_id"]
                or parse_utc(session["created_at"])
                >= parse_utc(session["expires_at"])
                or parse_utc(session["expires_at"])
                > parse_utc(family["absolute_expires_at"])
                or (
                    parse_utc(session["expires_at"])
                    - parse_utc(session["created_at"])
                ).total_seconds() > TASK_EXECUTION_ACCESS_TTL_SECONDS
            ):
                raise RuntimeError("workspace v7 任务执行会话绑定无效")
            access_token = self._task_execution_session_access_token(
                session_id=session["id"],
                exchange_idempotency_hash=session["exchange_idempotency_hash"],
                exchange_request_hash=session["exchange_request_hash"],
            )
            if not secrets.compare_digest(
                session["token_hash"], _secret_hash(access_token)
            ):
                raise RuntimeError("workspace v7 任务执行会话令牌完整性无效")

        refresh_tokens = connection.execute(
            "SELECT * FROM secretary_task_execution_refresh_tokens ORDER BY id"
        ).fetchall()
        for token in refresh_tokens:
            family = connection.execute(
                "SELECT * FROM secretary_task_execution_refresh_families "
                "WHERE id = ?",
                (token["family_id"],),
            ).fetchone()
            refresh_token = self._task_execution_refresh_token(
                token_id=token["id"],
                family_id=token["family_id"],
                generation=token["generation"],
            )
            if (
                family is None
                or parse_utc(token["created_at"])
                >= parse_utc(token["idle_expires_at"])
                or parse_utc(token["idle_expires_at"])
                > parse_utc(family["absolute_expires_at"])
                or (
                    parse_utc(token["idle_expires_at"])
                    - parse_utc(token["created_at"])
                ).total_seconds() > TASK_EXECUTION_REFRESH_IDLE_TTL_SECONDS
                or not secrets.compare_digest(
                    token["token_hash"], _secret_hash(refresh_token)
                )
            ):
                raise RuntimeError("workspace v7 任务执行 refresh token 绑定无效")
            if token["used_at"] is not None:
                replacement = connection.execute(
                    "SELECT * FROM secretary_task_execution_refresh_tokens "
                    "WHERE id = ?",
                    (token["replacement_token_id"],),
                ).fetchone()
                replacement_session = connection.execute(
                    "SELECT * FROM secretary_task_execution_sessions WHERE id = ?",
                    (token["replacement_session_id"],),
                ).fetchone()
                if (
                    replacement is None
                    or replacement_session is None
                    or replacement["family_id"] != token["family_id"]
                    or replacement["generation"] != token["generation"] + 1
                    or replacement_session["refresh_family_id"]
                    != token["family_id"]
                    or replacement_session["access_generation"]
                    != token["generation"] + 1
                ):
                    raise RuntimeError(
                        "workspace v7 任务执行 refresh rotation 绑定无效"
                    )

    @staticmethod
    def _verify_workspace_integrity(connection: sqlite3.Connection) -> None:
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise RuntimeError("workspace 数据库外键检查失败")
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if not integrity or any(str(row[0]).lower() != "ok" for row in integrity):
            raise RuntimeError("workspace 数据库完整性检查失败")

    @staticmethod
    def _require_workspace(
        connection: sqlite3.Connection, workspace_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM secretary_workspaces WHERE id = ?", (workspace_id,)
        ).fetchone()
        if row is None:
            raise PocketError(404, "工作区不存在")
        return row

    @staticmethod
    def _require_version(row: sqlite3.Row, expected_version: int) -> None:
        if row["version"] != expected_version:
            raise PocketError(
                412,
                f"版本冲突：预期 {expected_version}，当前为 {row['version']}",
            )

    def _idempotent_response(
        self,
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        actor_id: str,
        operation: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        request_hash = _hash_request(request_payload)
        row = connection.execute(
            """
            SELECT request_hash, response_json
            FROM secretary_workspace_idempotency
            WHERE workspace_id = ? AND actor_id = ?
              AND operation = ? AND idempotency_key = ?
            """,
            (workspace_id, actor_id, operation, idempotency_key),
        ).fetchone()
        if row is None:
            return None, request_hash
        if not secrets.compare_digest(row["request_hash"], request_hash):
            raise PocketError(409, "同一 Idempotency-Key 不能提交不同内容")
        cached = json_loads(row["response_json"], {})
        if not isinstance(cached, dict):
            raise PocketError(409, "幂等记录格式无效，请同步后使用新的请求键")
        reference = cached.get(DOCUMENT_IDEMPOTENCY_REFERENCE_KEY)
        if isinstance(reference, dict):
            document_id = reference.get("document_id")
            document_version = reference.get("version")
            document = connection.execute(
                """
                SELECT * FROM secretary_documents
                WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                """,
                (document_id, workspace_id),
            ).fetchone()
            if document is None or document["version"] != document_version:
                raise PocketError(
                    409,
                    "请求已经完成，但文档随后发生变化；请同步最新版本",
                )
            return self._document_dict(connection, document), request_hash
        return cached, request_hash

    @staticmethod
    def _store_idempotent_response(
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        actor_id: str,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        response: dict[str, Any],
        status_code: int = 200,
    ) -> None:
        stored_response = response
        if operation.startswith("document."):
            document_id = response.get("id")
            document_version = response.get("version")
            if isinstance(document_id, str) and isinstance(document_version, int):
                # Document bodies, source metadata, reviews and storage_ref must
                # not be duplicated indefinitely in the idempotency table.  A
                # stable resource reference still permits exact replay while
                # that version is current; a later version requires sync.
                stored_response = {
                    DOCUMENT_IDEMPOTENCY_REFERENCE_KEY: {
                        "document_id": document_id,
                        "version": document_version,
                    }
                }
        connection.execute(
            """
            INSERT INTO secretary_workspace_idempotency(
                workspace_id, actor_id, operation, idempotency_key,
                request_hash, status_code, response_json,
                response_headers_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, '{}', ?)
            """,
            (
                workspace_id,
                actor_id,
                operation,
                idempotency_key,
                request_hash,
                status_code,
                _json(stored_response),
                utc_now(),
            ),
        )

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_version: int,
        event_type: str,
        operation: str,
        actor_id: str | None,
        device_id: str,
        payload: dict[str, Any],
        actor_type: str = "owner",
        occurred_at: str | None = None,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO secretary_workspace_events(
                event_id, workspace_id, aggregate_type, aggregate_id,
                aggregate_version, event_type, operation, actor_type,
                actor_member_id, device_id, payload_json, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("evt"),
                workspace_id,
                aggregate_type,
                aggregate_id,
                aggregate_version,
                event_type,
                operation,
                actor_type,
                actor_id,
                device_id,
                _json(payload),
                occurred_at or utc_now(),
            ),
        ).lastrowid
        return int(cursor)

    @staticmethod
    def _workspace_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "timezone": row["timezone"],
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _member_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "workspace_id": row["workspace_id"],
            "kind": row["kind"],
            "role": row["role"],
            "display_name": row["display_name"],
            "contact_ref": row["contact_ref"],
            "active": bool(row["active"]),
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _memo_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "workspace_id": row["workspace_id"],
            "record_type": row["record_type"],
            "domain": row["domain"],
            "horizon": row["horizon"],
            "urgency": row["urgency"],
            "title": row["title"],
            "content": row["content"],
            "due_at": row["due_at"],
            "source": json_loads(row["source_json"], {}),
            "authority": row["authority"],
            "confirmation_status": row["confirmation_status"],
            "status": row["status"],
            "tags": json_loads(row["tags_json"], []),
            "pinned": bool(row["pinned"]),
            "version": row["version"],
            "created_by": row["created_by"],
            "updated_by": row["updated_by"],
            "client_mutation_id": row["client_mutation_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "deleted_at": row["deleted_at"],
        }

    @staticmethod
    def _step_dict(
        row: sqlite3.Row,
        *,
        depends_on_step_ids: list[str] | None = None,
        schedule_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "id": row["id"],
            "task_id": row["task_id"],
            "parent_step_id": row["parent_step_id"],
            "step_type": row["step_type"],
            "title": row["title"],
            "description": row["description"],
            "assignee_member_id": row["assignee_member_id"],
            "assignee_label": row["assignee_label"],
            "status": row["status"],
            "position": row["position"],
            "due_at": row["due_at"],
            "success_metric": json_loads(row["success_metric_json"], {}),
            "depends_on_step_ids": depends_on_step_ids or [],
            # calendar.step_id is authoritative. The legacy mirror column is
            # intentionally ignored to avoid dual-write drift.
            "schedule_id": schedule_id,
            "completed_at": row["completed_at"],
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _checkin_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "workspace_id": row["workspace_id"],
            "task_id": row["task_id"],
            "task_version": row["task_version"],
            "report_date": row["report_date"],
            "summary": row["summary"],
            "reported_progress": row["reported_progress"],
            "risks": json_loads(row["risks_json"], []),
            "blockers": json_loads(row["blockers_json"], []),
            "next_actions": json_loads(row["next_actions_json"], []),
            "forecast_at": row["forecast_at"],
            "created_by": row["created_by"],
            "version": row["version"],
            "client_mutation_id": row["client_mutation_id"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _change_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "task_id": row["task_id"],
            "change_type": row["change_type"],
            "base_version": row["base_version"],
            "before": json_loads(row["before_json"], None),
            "patch": json_loads(row["patch_json"], None),
            "reason": row["reason"],
            "status": row["status"],
            "proposed_by": row["proposed_by"],
            "decided_by": row["decided_by"],
            "proposed_at": row["proposed_at"],
            "decided_at": row["decided_at"],
            "version": row["version"],
            "client_mutation_id": row["client_mutation_id"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _task_change_proposal_document(row: sqlite3.Row) -> dict[str, Any]:
        document = json_loads(row["canonical_json"], None)
        if not isinstance(document, dict):
            raise PocketError(409, "任务变更提案完整性验证失败")
        canonical_json, digest = _task_change_digest(document)
        if not secrets.compare_digest(
            canonical_json.encode("utf-8"), row["canonical_json"].encode("utf-8")
        ):
            raise PocketError(409, "任务变更提案完整性验证失败")
        if not secrets.compare_digest(digest, row["digest"]):
            raise PocketError(409, "任务变更提案摘要验证失败")
        return document

    @staticmethod
    def _task_change_decision_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "change_id": row["change_id"],
            "proposal_digest": row["proposal_digest"],
            "action": row["action"],
            "actor_member_id": row["actor_member_id"],
            "actor_session_id": row["actor_session_id"],
            "assurance_method": row["assurance_method"],
            "reason": row["reason"],
            "version": row["version"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _task_change_session_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "change_id": row["change_id"],
            "task_id": row["task_id"],
            "responder_member_id": row["responder_member_id"],
            "client_device_id": row["client_device_id"],
            "assurance_method": row["assurance_method"],
            "expires_at": row["expires_at"],
        }

    @staticmethod
    def _require_task_change_decision_binding(
        connection: sqlite3.Connection,
        proposal: sqlite3.Row,
        change: sqlite3.Row,
        decision: sqlite3.Row | None,
    ) -> None:
        if decision is None:
            if change["status"] != "proposed":
                raise PocketError(409, "任务变更缺少不可变决定记录")
            return
        expected_status = {
            "accept": "accepted",
            "reject": "rejected",
            "cancel": "canceled",
        }.get(decision["action"])
        if (
            expected_status is None
            or change["status"] != expected_status
            or decision["change_id"] != proposal["change_id"]
            or not secrets.compare_digest(
                decision["proposal_digest"], proposal["digest"]
            )
        ):
            raise PocketError(409, "任务变更决定绑定验证失败")
        if decision["action"] in {"accept", "reject"}:
            if decision["actor_member_id"] != proposal["responder_member_id"]:
                raise PocketError(409, "任务变更决定回应方验证失败")
            if decision["assurance_method"] == "task_change_session":
                session = connection.execute(
                    """
                    SELECT * FROM secretary_task_change_sessions WHERE id = ?
                    """,
                    (decision["actor_session_id"],),
                ).fetchone()
                if (
                    session is None
                    or session["change_id"] != proposal["change_id"]
                    or session["task_id"] != proposal["task_id"]
                    or session["responder_member_id"]
                    != proposal["responder_member_id"]
                ):
                    raise PocketError(409, "任务变更决定会话绑定验证失败")
            elif (
                decision["assurance_method"]
                not in {"owner_token", "owner_device_session"}
                or decision["actor_session_id"] is not None
                or proposal["responder_member_id"]
                != proposal["proposer_member_id"]
            ):
                raise PocketError(409, "任务变更决定凭据绑定验证失败")
        elif (
            decision["actor_member_id"] != proposal["proposer_member_id"]
            or decision["actor_session_id"] is not None
            or decision["assurance_method"]
            not in {"owner_token", "owner_device_session"}
        ):
            raise PocketError(409, "任务变更取消决定绑定验证失败")

    @classmethod
    def _task_change_protocol_dict(
        cls, connection: sqlite3.Connection, change: sqlite3.Row
    ) -> dict[str, Any]:
        proposal = connection.execute(
            "SELECT * FROM secretary_task_change_proposals WHERE change_id = ?",
            (change["id"],),
        ).fetchone()
        if proposal is None:
            raise PocketError(404, "该历史任务变更没有 P1b-B 双方确认协议")
        decision = connection.execute(
            "SELECT * FROM secretary_task_change_decisions WHERE change_id = ?",
            (change["id"],),
        ).fetchone()
        cls._require_task_change_decision_binding(
            connection, proposal, change, decision
        )
        task = connection.execute(
            """
            SELECT id, workspace_id, title, stage, version, issuer_member_id,
                   assignee_member_id, assignee_label, due_at,
                   acceptance_criteria_json,
                   abnormal_close_reason, deleted_at
            FROM secretary_business_tasks WHERE id = ? AND workspace_id = ?
            """,
            (change["task_id"], change["workspace_id"]),
        ).fetchone()
        proposal_current = cls._task_change_proposal_is_current(
            proposal, change, task
        )
        return {
            "id": change["id"],
            "workspace_id": change["workspace_id"],
            "task_id": change["task_id"],
            "change_type": change["change_type"],
            "base_task_version": change["base_version"],
            "status": change["status"],
            "version": change["version"],
            "proposer_member_id": proposal["proposer_member_id"],
            "responder_member_id": proposal["responder_member_id"],
            "proposal": {
                "digest": proposal["digest"],
                "document": cls._task_change_proposal_document(proposal),
                "created_at": proposal["created_at"],
            },
            "decision": (
                cls._task_change_decision_dict(decision)
                if decision is not None
                else None
            ),
            "actionable": proposal_current,
            "task": (
                {
                    "id": task["id"],
                    "title": task["title"],
                    "stage": task["stage"],
                    "version": task["version"],
                    "assignee_member_id": task["assignee_member_id"],
                    "assignee_label": task["assignee_label"],
                }
                if task is not None and task["deleted_at"] is None
                else None
            ),
            "created_at": change["proposed_at"],
            "updated_at": change["updated_at"],
            "closed_at": change["decided_at"],
        }

    @staticmethod
    def _task_change_before(task: sqlite3.Row, change_type: str) -> Any:
        return {
            "assignee": task["assignee_member_id"],
            "due_at": task["due_at"],
            "acceptance_criteria": json_loads(task["acceptance_criteria_json"], []),
            "abnormal_close": task["abnormal_close_reason"],
        }[change_type]

    @classmethod
    def _task_change_document(
        cls,
        task: sqlite3.Row,
        *,
        change_id: str,
        change_type: str,
        patch: dict[str, Any],
        reason: str,
        proposer_member_id: str,
        responder_member_id: str,
    ) -> dict[str, Any]:
        document = {
            "schema": TASK_CHANGE_SCHEMA,
            "workspace_id": task["workspace_id"],
            "task_id": task["id"],
            "change_id": change_id,
            "change_type": change_type,
            "base_task_version": task["version"],
            "proposer_role": "issuer",
            "proposer_member_id": proposer_member_id,
            "responder_role": (
                "issuer"
                if responder_member_id == proposer_member_id
                else "assignee"
            ),
            "responder_member_id": responder_member_id,
            "before": cls._task_change_before(task, change_type),
            "patch": patch,
            "reason": reason,
        }
        canonical_json, _digest = _task_change_digest(document)
        normalized = json_loads(canonical_json, None)
        if not isinstance(normalized, dict):
            raise PocketError(409, "无法构造任务变更提案")
        return normalized

    @classmethod
    def _task_change_proposal_is_current(
        cls,
        proposal: sqlite3.Row,
        change: sqlite3.Row,
        task: sqlite3.Row | None,
    ) -> bool:
        if task is None or change["status"] != "proposed":
            return False
        try:
            document = cls._task_change_proposal_document(proposal)
        except PocketError:
            return False
        expected = {
            "schema": TASK_CHANGE_SCHEMA,
            "workspace_id": change["workspace_id"],
            "task_id": change["task_id"],
            "change_id": change["id"],
            "change_type": change["change_type"],
            "base_task_version": change["base_version"],
            "proposer_member_id": proposal["proposer_member_id"],
            "responder_member_id": proposal["responder_member_id"],
            "proposer_role": "issuer",
            "responder_role": (
                "issuer"
                if proposal["responder_member_id"] == proposal["proposer_member_id"]
                else "assignee"
            ),
            "before": json_loads(change["before_json"], None),
            "patch": json_loads(change["patch_json"], None),
            "reason": change["reason"],
        }
        if any(document.get(key) != value for key, value in expected.items()):
            return False
        return not (
            proposal["created_at"] != change["proposed_at"]
            or task["issuer_member_id"] != proposal["proposer_member_id"]
            or task["assignee_member_id"] != proposal["responder_member_id"]
            or task["workspace_id"] != change["workspace_id"]
            or task["id"] != change["task_id"]
            or task["version"] != change["base_version"]
            or cls._task_change_before(task, change["change_type"])
            != document["before"]
            or task["deleted_at"] is not None
            or task["stage"] in TERMINAL_TASK_STAGES
        )

    def _task_dict(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        steps = connection.execute(
            """
            SELECT * FROM secretary_task_steps
            WHERE task_id = ? AND deleted_at IS NULL
            ORDER BY position, created_at, id
            """,
            (row["id"],),
        ).fetchall()
        dependency_rows = connection.execute(
            """
            SELECT dependency.step_id, dependency.depends_on_step_id
            FROM secretary_task_step_dependencies dependency
            JOIN secretary_task_steps step ON step.id = dependency.step_id
            WHERE step.task_id = ? AND step.deleted_at IS NULL
            ORDER BY dependency.step_id, dependency.depends_on_step_id
            """,
            (row["id"],),
        ).fetchall()
        dependencies: dict[str, list[str]] = {}
        for dependency in dependency_rows:
            dependencies.setdefault(dependency["step_id"], []).append(
                dependency["depends_on_step_id"]
            )
        schedule_rows = connection.execute(
            """
            SELECT entry.step_id, entry.id
            FROM secretary_calendar_entries entry
            JOIN secretary_task_steps step ON step.id = entry.step_id
            WHERE step.task_id = ? AND step.deleted_at IS NULL
              AND entry.deleted_at IS NULL AND entry.status = 'scheduled'
            ORDER BY entry.step_id, entry.updated_at DESC, entry.id DESC
            """,
            (row["id"],),
        ).fetchall()
        schedules: dict[str, str] = {}
        for schedule in schedule_rows:
            schedules.setdefault(schedule["step_id"], schedule["id"])
        changes = connection.execute(
            """
            SELECT * FROM secretary_task_changes
            WHERE task_id = ? ORDER BY proposed_at DESC
            """,
            (row["id"],),
        ).fetchall()
        evidence = connection.execute(
            """
            SELECT * FROM secretary_workspace_evidence
            WHERE resource_type = 'task' AND resource_id = ?
            ORDER BY created_at
            """,
            (row["id"],),
        ).fetchall()
        return {
            "id": row["id"],
            "workspace_id": row["workspace_id"],
            "origin_memo_id": row["origin_memo_id"],
            "title": row["title"],
            "summary": row["summary"],
            "purpose": row["purpose"],
            "objective": row["objective"],
            "strategy": row["strategy"],
            "key_points": json_loads(row["key_points_json"], []),
            "acceptance_criteria": json_loads(row["acceptance_criteria_json"], []),
            "issuer_member_id": row["issuer_member_id"],
            "assignee_member_id": row["assignee_member_id"],
            "acceptance_owner_id": row["acceptance_owner_id"],
            "issuer_label": row["issuer_label"],
            "assignee_label": row["assignee_label"],
            "acceptance_owner_label": row["acceptance_owner_label"],
            "start_at": row["start_at"],
            "due_at": row["due_at"],
            "stage": row["stage"],
            "health": row["health"],
            "tier": row["tier"],
            "domain": row["domain"],
            "priority": row["priority"],
            "progress": row["progress"],
            "requires_alignment": bool(row["requires_alignment"]),
            "source": json_loads(row["source_json"], {}),
            "started_at": row["started_at"],
            "submitted_at": row["submitted_at"],
            "accepted_at": row["accepted_at"],
            "abnormal_close_reason": row["abnormal_close_reason"],
            "steps": [
                self._step_dict(
                    step,
                    depends_on_step_ids=dependencies.get(step["id"], []),
                    schedule_id=schedules.get(step["id"]),
                )
                for step in steps
            ],
            "changes": [self._change_dict(change) for change in changes],
            "evidence": [
                {
                    "id": item["id"],
                    "source": json_loads(item["source_json"], {}),
                    "excerpt": item["excerpt"],
                    "authority": item["authority"],
                    "observed_at": item["observed_at"],
                    "created_at": item["created_at"],
                }
                for item in evidence
            ],
            "version": row["version"],
            "created_by": row["created_by"],
            "updated_by": row["updated_by"],
            "client_mutation_id": row["client_mutation_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "deleted_at": row["deleted_at"],
        }

    @staticmethod
    def _task_execution_session_dict(
        session: sqlite3.Row, *, refresh_expires_at: str
    ) -> dict[str, Any]:
        return {
            "id": session["id"],
            "workspace_id": session["workspace_id"],
            "task_id": session["task_id"],
            "assignee_member_id": session["assignee_member_id"],
            "client_device_id": session["client_device_id"],
            "access_generation": session["access_generation"],
            "created_at": session["created_at"],
            "access_expires_at": session["expires_at"],
            "refresh_expires_at": refresh_expires_at,
        }

    @staticmethod
    def _task_execution_etag_state(
        connection: sqlite3.Connection, task: sqlite3.Row
    ) -> tuple[str, int]:
        step_versions = [
            [str(row["id"]), int(row["version"])]
            for row in connection.execute(
                """
                SELECT id, version FROM secretary_task_steps
                WHERE task_id = ? AND deleted_at IS NULL
                ORDER BY position, created_at, id
                """,
                (task["id"],),
            ).fetchall()
        ]
        checkin = connection.execute(
            """
            SELECT COUNT(*) AS item_count, COALESCE(MAX(rowid), 0) AS last_rowid
            FROM secretary_task_checkins WHERE task_id = ?
            """,
            (task["id"],),
        ).fetchone()
        pending_changes = [
            [str(row["id"]), int(row["version"])]
            for row in connection.execute(
                """
                SELECT id, version FROM secretary_task_changes
                WHERE task_id = ? AND status = 'proposed' ORDER BY id
                """,
                (task["id"],),
            ).fetchall()
        ]
        cursor = int(checkin["item_count"])
        state = {
            "schema": "centaur.task-execution-view-etag.v1",
            "task_id": task["id"],
            "task_version": int(task["version"]),
            "assignment_epoch": int(task["assignment_epoch"]),
            "step_versions": step_versions,
            "checkin_cursor": [cursor, int(checkin["last_rowid"])],
            "pending_changes": pending_changes,
        }
        digest = hashlib.sha256(_json(state).encode("utf-8")).hexdigest()
        return f"task-execution-v1-{digest}", cursor

    def _task_execution_projection(
        self,
        connection: sqlite3.Connection,
        task: sqlite3.Row,
        *,
        member_id: str,
    ) -> tuple[dict[str, Any], str]:
        dependency_rows = connection.execute(
            """
            SELECT dependency.step_id, dependency.depends_on_step_id
            FROM secretary_task_step_dependencies dependency
            JOIN secretary_task_steps step ON step.id = dependency.step_id
            WHERE step.task_id = ? AND step.deleted_at IS NULL
            ORDER BY dependency.step_id, dependency.depends_on_step_id
            """,
            (task["id"],),
        ).fetchall()
        dependencies: dict[str, list[str]] = {}
        for dependency in dependency_rows:
            dependencies.setdefault(str(dependency["step_id"]), []).append(
                str(dependency["depends_on_step_id"])
            )
        steps = connection.execute(
            """
            SELECT * FROM secretary_task_steps
            WHERE task_id = ? AND deleted_at IS NULL
            ORDER BY position, created_at, id
            """,
            (task["id"],),
        ).fetchall()
        etag, _ = self._task_execution_etag_state(connection, task)
        change_pending = connection.execute(
            """
            SELECT 1 FROM secretary_task_changes
            WHERE task_id = ? AND status = 'proposed' LIMIT 1
            """,
            (task["id"],),
        ).fetchone() is not None
        own_checkin_rows = connection.execute(
            """
            SELECT * FROM secretary_task_checkins
            WHERE task_id = ? AND created_by = ?
            ORDER BY created_at DESC, rowid DESC LIMIT 10
            """,
            (task["id"], member_id),
        ).fetchall()
        projection = {
            "id": task["id"],
            "title": task["title"],
            "purpose": task["purpose"],
            "objective": task["objective"],
            "strategy": task["strategy"],
            "key_points": json_loads(task["key_points_json"], []),
            "acceptance_criteria": json_loads(
                task["acceptance_criteria_json"], []
            ),
            "start_at": task["start_at"],
            "due_at": task["due_at"],
            "stage": task["stage"],
            "health": task["health"],
            "priority": task["priority"],
            "progress": task["progress"],
            "change_pending": change_pending,
            "own_checkins": [
                {
                    "id": row["id"],
                    "task_version": row["task_version"],
                    "report_date": row["report_date"],
                    "summary": row["summary"],
                    "reported_progress": row["reported_progress"],
                    "risks": json_loads(row["risks_json"], []),
                    "blockers": json_loads(row["blockers_json"], []),
                    "next_actions": json_loads(row["next_actions_json"], []),
                    "forecast_at": row["forecast_at"],
                    "version": row["version"],
                    "created_at": row["created_at"],
                }
                for row in own_checkin_rows
            ],
            "steps": [
                {
                    "id": step["id"],
                    "parent_step_id": step["parent_step_id"],
                    "step_type": step["step_type"],
                    "title": step["title"],
                    "description": step["description"],
                    "status": step["status"],
                    "position": step["position"],
                    "due_at": step["due_at"],
                    "success_metric": json_loads(
                        step["success_metric_json"], {}
                    ),
                    "depends_on_step_ids": dependencies.get(step["id"], []),
                    "completed_at": step["completed_at"],
                    "version": step["version"],
                    "editable": bool(
                        task["stage"] == "in_progress"
                        and step["assignee_member_id"] == member_id
                        and step["status"] not in {"done", "canceled"}
                        and not change_pending
                    ),
                }
                for step in steps
            ],
            "version": task["version"],
            "updated_at": task["updated_at"],
        }
        return projection, etag

    @staticmethod
    def _require_task_execution_if_match(
        expected_etag: str | None, current_etag: str
    ) -> None:
        if expected_etag is None:
            raise PocketError(428, "任务执行写入必须提供 If-Match")
        value = expected_etag
        if TASK_EXECUTION_ETAG_PATTERN.fullmatch(value) is None:
            raise PocketError(400, "任务执行 If-Match 必须是单个规范强 ETag")
        presented = value[1:-1]
        if not secrets.compare_digest(
            presented.encode("utf-8"), current_etag.encode("utf-8")
        ):
            raise PocketError(412, "任务执行视图已变化，请重新读取后再提交")

    @staticmethod
    def _task_execution_idempotent_response(
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        actor_id: str,
        operation: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
        conflict_detail: str = "同一 Idempotency-Key 不能提交不同内容",
    ) -> tuple[dict[str, Any] | None, str | None, str]:
        request_hash = _hash_request(request_payload)
        row = connection.execute(
            """
            SELECT request_hash, response_json, response_headers_json
            FROM secretary_workspace_idempotency
            WHERE workspace_id = ? AND actor_id = ?
              AND operation = ? AND idempotency_key = ?
            """,
            (workspace_id, actor_id, operation, idempotency_key),
        ).fetchone()
        if row is None:
            return None, None, request_hash
        if not secrets.compare_digest(row["request_hash"], request_hash):
            raise PocketError(409, conflict_detail)
        response = json_loads(row["response_json"], None)
        headers = json_loads(row["response_headers_json"], None)
        if (
            not isinstance(response, dict)
            or not isinstance(headers, dict)
            or not isinstance(headers.get("etag"), str)
        ):
            raise PocketError(409, "任务执行幂等记录无效")
        return response, headers["etag"], request_hash

    @staticmethod
    def _store_task_execution_idempotent_response(
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        actor_id: str,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        response: dict[str, Any],
        etag: str,
        status_code: int = 200,
    ) -> None:
        connection.execute(
            """
            INSERT INTO secretary_workspace_idempotency(
                workspace_id, actor_id, operation, idempotency_key,
                request_hash, status_code, response_json,
                response_headers_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace_id,
                actor_id,
                operation,
                idempotency_key,
                request_hash,
                status_code,
                _json(response),
                _json({"etag": etag}),
                utc_now(),
            ),
        )

    @classmethod
    def _store_task_execution_command_response(
        cls,
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        actor_id: str,
        mutation_actor_id: str,
        task_id: str,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        client_mutation_id: str,
        mutation_request_hash: str,
        response: dict[str, Any],
        etag: str,
        status_code: int = 200,
    ) -> None:
        cls._store_task_execution_idempotent_response(
            connection,
            workspace_id=workspace_id,
            actor_id=actor_id,
            operation=operation,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            response=response,
            etag=etag,
            status_code=status_code,
        )
        cls._store_task_execution_idempotent_response(
            connection,
            workspace_id=workspace_id,
            actor_id=mutation_actor_id,
            operation=f"task_execution.client_mutation:{task_id}",
            idempotency_key=client_mutation_id,
            request_hash=mutation_request_hash,
            response=response,
            etag=etag,
            status_code=status_code,
        )

    def _task_execution_principal_task(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        principal: dict[str, Any],
        *,
        device_id: str,
        require_active: bool,
    ) -> sqlite3.Row:
        if (
            principal.get("auth_kind") != "task_execution_session"
            or principal.get("task_id") != task_id
            or not isinstance(principal.get("workspace_id"), str)
        ):
            raise PocketError(404, "任务执行资源不存在")
        task = connection.execute(
            """
            SELECT * FROM secretary_business_tasks
            WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
            """,
            (task_id, principal["workspace_id"]),
        ).fetchone()
        if task is None:
            raise PocketError(404, "任务执行资源不存在")
        if not require_active:
            return task
        session = connection.execute(
            """
            SELECT * FROM secretary_task_execution_sessions WHERE id = ?
            """,
            (principal.get("session_id"),),
        ).fetchone()
        family = connection.execute(
            """
            SELECT * FROM secretary_task_execution_refresh_families WHERE id = ?
            """,
            (principal.get("refresh_family_id"),),
        ).fetchone()
        member = connection.execute(
            """
            SELECT * FROM secretary_workspace_members
            WHERE id = ? AND workspace_id = ? AND active = 1
            """,
            (principal.get("member_id"), principal["workspace_id"]),
        ).fetchone()
        current = bool(
            not principal.get("replay_only")
            and session is not None
            and family is not None
            and member is not None
            and member["kind"] == "external"
            and session["revoked_at"] is None
            and family["revoked_at"] is None
            and parse_utc(session["expires_at"]) > datetime.now(UTC)
            and parse_utc(family["absolute_expires_at"]) > datetime.now(UTC)
            and self._task_execution_effective_expiry(task, family)
            > datetime.now(UTC)
            and secrets.compare_digest(
                session["token_hash"], principal.get("presented_token_hash", "")
            )
            and secrets.compare_digest(
                session["client_device_id"].encode("utf-8"),
                device_id.encode("utf-8"),
            )
            and session["task_id"] == task_id
            and session["assignee_member_id"] == principal.get("member_id")
            and session["assignment_epoch"] == task["assignment_epoch"]
            and family["assignment_epoch"] == task["assignment_epoch"]
            and task["assignee_member_id"] == principal.get("member_id")
            and task["stage"] in {"aligned", "in_progress", "submitted"}
        )
        if not current:
            raise PocketError(401, "任务执行会话凭据无效或已失效")
        return task

    @staticmethod
    def _task_has_pending_change(
        connection: sqlite3.Connection, task_id: str
    ) -> bool:
        return connection.execute(
            """
            SELECT 1 FROM secretary_task_changes
            WHERE task_id = ? AND status = 'proposed' LIMIT 1
            """,
            (task_id,),
        ).fetchone() is not None

    @staticmethod
    def _task_execution_actor_metadata(
        principal: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "actor_session_id": principal["session_id"],
            "refresh_family_id": principal["refresh_family_id"],
            "actor_subject_type": "task_execution_capability",
            "actor_subject_id": principal["session_id"],
            "on_behalf_of_member_id": principal["member_id"],
            "assurance_method": "dual_channel_task_execution",
            "assignment_epoch": principal["assignment_epoch"],
        }

    @staticmethod
    def _task_execution_mutation_actor_id(
        task_id: str, principal: dict[str, Any]
    ) -> str:
        return (
            f"task-execution-assignment:{task_id}:"
            f"{principal['member_id']}:{principal['assignment_epoch']}"
        )

    def _task_execution_write_context(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: str,
        payload: dict[str, Any],
        principal: dict[str, Any],
        operation: str,
        idempotency_key: str,
        device_id: str,
        if_match: str | None,
        status_code: int = 200,
    ) -> tuple[
        sqlite3.Row,
        dict[str, Any] | None,
        str | None,
        str,
        str,
        str | None,
    ]:
        task = self._task_execution_principal_task(
            connection,
            task_id,
            principal,
            device_id=device_id,
            require_active=False,
        )
        client_mutation_id = payload.get("client_mutation_id")
        if not isinstance(client_mutation_id, str) or not client_mutation_id:
            raise PocketError(422, "任务执行命令必须提供 client_mutation_id")
        request_payload = {
            "operation": operation,
            "task_id": task_id,
            "if_match": if_match,
            "body": payload,
        }
        cached, cached_etag, request_hash = (
            self._task_execution_idempotent_response(
                connection,
                workspace_id=task["workspace_id"],
                actor_id=principal["idempotency_actor_id"],
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
        )
        mutation_operation = f"task_execution.client_mutation:{task_id}"
        mutation_actor_id = self._task_execution_mutation_actor_id(
            task_id, principal
        )
        mutation_cached, mutation_cached_etag, mutation_request_hash = (
            self._task_execution_idempotent_response(
                connection,
                workspace_id=task["workspace_id"],
                actor_id=mutation_actor_id,
                operation=mutation_operation,
                idempotency_key=client_mutation_id,
                request_payload=request_payload,
                conflict_detail=(
                    "同一 client_mutation_id 不能提交不同内容或用于不同操作"
                ),
            )
        )
        if (
            cached is not None
            and mutation_cached is not None
            and (cached != mutation_cached or cached_etag != mutation_cached_etag)
        ):
            raise PocketError(409, "任务执行幂等记录相互冲突")
        replay = cached if cached is not None else mutation_cached
        replay_etag = cached_etag if cached is not None else mutation_cached_etag
        if replay is not None:
            assert replay_etag is not None
            if cached is None:
                self._store_task_execution_idempotent_response(
                    connection,
                    workspace_id=task["workspace_id"],
                    actor_id=principal["idempotency_actor_id"],
                    operation=operation,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    response=replay,
                    etag=replay_etag,
                    status_code=status_code,
                )
            if mutation_cached is None:
                self._store_task_execution_idempotent_response(
                    connection,
                    workspace_id=task["workspace_id"],
                    actor_id=mutation_actor_id,
                    operation=mutation_operation,
                    idempotency_key=client_mutation_id,
                    request_hash=mutation_request_hash,
                    response=replay,
                    etag=replay_etag,
                    status_code=status_code,
                )
            session = connection.execute(
                """
                SELECT * FROM secretary_task_execution_sessions WHERE id = ?
                """,
                (principal.get("session_id"),),
            ).fetchone()
            family = connection.execute(
                """
                SELECT * FROM secretary_task_execution_refresh_families WHERE id = ?
                """,
                (principal.get("refresh_family_id"),),
            ).fetchone()
            member = connection.execute(
                """
                SELECT * FROM secretary_workspace_members
                WHERE id = ? AND workspace_id = ? AND active = 1
                """,
                (principal.get("member_id"), task["workspace_id"]),
            ).fetchone()
            now_dt = datetime.now(UTC)
            replay_binding_current = bool(
                session is not None
                and family is not None
                and member is not None
                and member["kind"] == "external"
                and family["revoked_at"] is None
                and self._task_execution_effective_expiry(task, family) > now_dt
                and task["stage"] in {"aligned", "in_progress", "submitted"}
                and task["assignee_member_id"] == principal.get("member_id")
                and task["assignment_epoch"] == principal.get("assignment_epoch")
                and family["assignment_epoch"] == task["assignment_epoch"]
                and session["assignment_epoch"] == task["assignment_epoch"]
                and session["refresh_family_id"] == family["id"]
                and secrets.compare_digest(
                    session["token_hash"], principal.get("presented_token_hash", "")
                )
                and secrets.compare_digest(
                    session["client_device_id"].encode("utf-8"),
                    device_id.encode("utf-8"),
                )
                and parse_utc(session["expires_at"]) > now_dt
                and (
                    (
                        session["revoked_at"] is None
                    )
                    or (
                        principal.get("replay_only")
                        and session["revoke_reason"] == "access_rotated"
                    )
                )
            )
            if not replay_binding_current:
                raise PocketError(401, "任务执行会话绑定已变化，不能重放")
            return (
                task,
                replay,
                replay_etag,
                request_hash,
                mutation_request_hash,
                None,
            )
        if principal.get("replay_only"):
            raise PocketError(401, "任务执行 access 会话已经轮换")
        task = self._task_execution_principal_task(
            connection,
            task_id,
            principal,
            device_id=device_id,
            require_active=True,
        )
        self._require_version(task, payload["expected_task_version"])
        _projection, current_etag = self._task_execution_projection(
            connection,
            task,
            member_id=principal["member_id"],
        )
        self._require_task_execution_if_match(if_match, current_etag)
        return (
            task,
            None,
            None,
            request_hash,
            mutation_request_hash,
            current_etag,
        )

    @staticmethod
    def _agreement_revision_document(row: sqlite3.Row) -> dict[str, Any]:
        document = json_loads(row["canonical_json"], None)
        if not isinstance(document, dict):
            raise PocketError(409, "任务协议修订完整性验证失败")
        canonical_json, digest = _task_agreement_digest(document)
        if not secrets.compare_digest(
            canonical_json.encode("utf-8"), row["canonical_json"].encode("utf-8")
        ):
            raise PocketError(409, "任务协议修订完整性验证失败")
        if not secrets.compare_digest(digest, row["digest"]):
            raise PocketError(409, "任务协议修订摘要验证失败")
        return document

    @classmethod
    def _agreement_revision_dict(cls, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "case_id": row["case_id"],
            "revision_no": row["revision_no"],
            "parent_revision_id": row["parent_revision_id"],
            "base_task_version": row["base_task_version"],
            "schema_version": row["schema_version"],
            "proposed_by_role": row["proposed_by_role"],
            "proposed_by_member_id": row["proposed_by_member_id"],
            "required_responder_role": row["required_responder_role"],
            "required_responder_member_id": row["required_responder_member_id"],
            "digest": row["digest"],
            "document": cls._agreement_revision_document(row),
            "reason": row["reason"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _agreement_decision_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "case_id": row["case_id"],
            "revision_id": row["revision_id"],
            "revision_digest": row["revision_digest"],
            "action": row["action"],
            "actor_role": row["actor_role"],
            "actor_member_id": row["actor_member_id"],
            "actor_session_id": row["actor_session_id"],
            "assurance_method": row["assurance_method"],
            "reason": row["reason"],
            "counter_revision_id": row["counter_revision_id"],
            "version": row["version"],
            "created_at": row["created_at"],
        }

    @classmethod
    def _agreement_dict(
        cls, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        revisions = connection.execute(
            """
            SELECT * FROM secretary_task_alignment_revisions
            WHERE case_id = ? ORDER BY revision_no, id
            """,
            (row["id"],),
        ).fetchall()
        if not revisions:
            raise PocketError(409, "任务协议缺少修订记录")
        current = next(
            (
                revision
                for revision in revisions
                if revision["revision_no"] == row["current_revision_no"]
            ),
            None,
        )
        if current is None:
            raise PocketError(409, "任务协议当前修订不存在")
        decisions = connection.execute(
            """
            SELECT * FROM secretary_task_alignment_decisions
            WHERE case_id = ? ORDER BY created_at, id
            """,
            (row["id"],),
        ).fetchall()
        return {
            "id": row["id"],
            "workspace_id": row["workspace_id"],
            "task_id": row["task_id"],
            "issuer_member_id": row["issuer_member_id"],
            "assignee_member_id": row["assignee_member_id"],
            "status": row["status"],
            "current_revision_no": row["current_revision_no"],
            "accepted_revision_no": row["accepted_revision_no"],
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "closed_at": row["closed_at"],
            "current_revision": cls._agreement_revision_dict(current),
            "revisions": [cls._agreement_revision_dict(item) for item in revisions],
            "decisions": [cls._agreement_decision_dict(item) for item in decisions],
        }

    @staticmethod
    def _task_agreement_document(
        task: sqlite3.Row,
        *,
        case_id: str,
        revision_no: int,
        parent_digest: str | None,
        proposer_role: str,
        proposer_member_id: str,
        responder_role: str,
        responder_member_id: str,
    ) -> dict[str, Any]:
        document = {
            "schema": TASK_AGREEMENT_SCHEMA,
            "workspace_id": task["workspace_id"],
            "task_id": task["id"],
            "agreement_id": case_id,
            "revision_no": revision_no,
            "parent_digest": parent_digest,
            "proposer_role": proposer_role,
            "proposer_member_id": proposer_member_id,
            "responder_role": responder_role,
            "responder_member_id": responder_member_id,
            "issuer_member_id": task["issuer_member_id"],
            "assignee_member_id": task["assignee_member_id"],
            "acceptance_owner_id": task["acceptance_owner_id"],
            "domain": task["domain"],
            "tier": task["tier"],
            "priority": task["priority"],
            "title": task["title"],
            "purpose": task["purpose"],
            "objective": task["objective"],
            "strategy": task["strategy"],
            "key_points": json_loads(task["key_points_json"], []),
            "acceptance_criteria": json_loads(task["acceptance_criteria_json"], []),
            "due_at": task["due_at"],
        }
        canonical_json, _digest = _task_agreement_digest(document)
        normalized = json_loads(canonical_json, None)
        if not isinstance(normalized, dict):
            raise PocketError(409, "无法构造任务协议文档")
        return normalized

    @staticmethod
    def _insert_alignment_revision(
        connection: sqlite3.Connection,
        *,
        case_id: str,
        revision_id: str,
        revision_no: int,
        parent_revision_id: str | None,
        base_task_version: int,
        proposer_role: str,
        proposer_member_id: str,
        responder_role: str,
        responder_member_id: str,
        document: dict[str, Any],
        reason: str | None,
        now: str,
    ) -> sqlite3.Row:
        canonical_json, digest = _task_agreement_digest(document)
        canonical_bytes = len(canonical_json.encode("utf-8"))
        if canonical_bytes > TASK_AGREEMENT_MAX_REVISION_BYTES:
            raise PocketError(413, "单个任务协议修订超过 3 MiB 限制")
        usage = connection.execute(
            """
            SELECT COUNT(*) AS revision_count,
                   COALESCE(SUM(length(CAST(canonical_json AS BLOB))), 0)
                       AS canonical_bytes
            FROM secretary_task_alignment_revisions
            WHERE case_id = ?
            """,
            (case_id,),
        ).fetchone()
        assert usage is not None
        if usage["revision_count"] >= TASK_AGREEMENT_MAX_REVISIONS:
            raise PocketError(413, "任务协议修订数量超过 100 条限制")
        if usage["canonical_bytes"] + canonical_bytes > TASK_AGREEMENT_MAX_CASE_BYTES:
            raise PocketError(413, "任务协议修订累计超过 4 MiB 限制")
        connection.execute(
            """
            INSERT INTO secretary_task_alignment_revisions(
                id, case_id, revision_no, parent_revision_id,
                base_task_version, schema_version, proposed_by_role,
                proposed_by_member_id, required_responder_role,
                required_responder_member_id, digest, canonical_json,
                reason, created_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id,
                case_id,
                revision_no,
                parent_revision_id,
                base_task_version,
                proposer_role,
                proposer_member_id,
                responder_role,
                responder_member_id,
                digest,
                canonical_json,
                reason,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM secretary_task_alignment_revisions WHERE id = ?",
            (revision_id,),
        ).fetchone()
        assert row is not None
        return row

    def _create_alignment_case(
        self,
        connection: sqlite3.Connection,
        task: sqlite3.Row,
        *,
        now: str,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        if (
            task["issuer_member_id"] != DEFAULT_OWNER_ID
            or task["acceptance_owner_id"] != task["issuer_member_id"]
            or task["assignee_member_id"] == task["issuer_member_id"]
        ):
            raise PocketError(
                409,
                "P1b-A 任务协议要求主人为下达人和验收人，并由独立承办人回应",
            )
        case_id = new_id("agreement")
        revision_id = new_id("agreement_revision")
        connection.execute(
            """
            INSERT INTO secretary_task_alignment_cases(
                id, workspace_id, task_id, issuer_member_id,
                assignee_member_id, status, current_revision_no,
                accepted_revision_no, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', 1, NULL, 1, ?, ?)
            """,
            (
                case_id,
                task["workspace_id"],
                task["id"],
                task["issuer_member_id"],
                task["assignee_member_id"],
                now,
                now,
            ),
        )
        document = self._task_agreement_document(
            task,
            case_id=case_id,
            revision_no=1,
            parent_digest=None,
            proposer_role="issuer",
            proposer_member_id=task["issuer_member_id"],
            responder_role="assignee",
            responder_member_id=task["assignee_member_id"],
        )
        revision = self._insert_alignment_revision(
            connection,
            case_id=case_id,
            revision_id=revision_id,
            revision_no=1,
            parent_revision_id=None,
            base_task_version=task["version"],
            proposer_role="issuer",
            proposer_member_id=task["issuer_member_id"],
            responder_role="assignee",
            responder_member_id=task["assignee_member_id"],
            document=document,
            reason=None,
            now=now,
        )
        case = connection.execute(
            "SELECT * FROM secretary_task_alignment_cases WHERE id = ?",
            (case_id,),
        ).fetchone()
        assert case is not None
        return case, revision

    @classmethod
    def _alignment_case_is_current(
        cls,
        connection: sqlite3.Connection,
        case: sqlite3.Row,
        revision: sqlite3.Row,
        task: sqlite3.Row | None,
    ) -> bool:
        if task is None or case["status"] != "pending":
            return False
        try:
            document = cls._agreement_revision_document(revision)
        except PocketError:
            return False
        parent_digest: str | None = None
        if revision["parent_revision_id"] is not None:
            parent = connection.execute(
                """
                SELECT case_id, digest FROM secretary_task_alignment_revisions
                WHERE id = ?
                """,
                (revision["parent_revision_id"],),
            ).fetchone()
            if parent is None or parent["case_id"] != case["id"]:
                return False
            parent_digest = parent["digest"]
        expected_envelope = {
            "schema": TASK_AGREEMENT_SCHEMA,
            "workspace_id": case["workspace_id"],
            "task_id": case["task_id"],
            "agreement_id": case["id"],
            "revision_no": revision["revision_no"],
            "parent_digest": parent_digest,
            "proposer_role": revision["proposed_by_role"],
            "proposer_member_id": revision["proposed_by_member_id"],
            "responder_role": revision["required_responder_role"],
            "responder_member_id": revision["required_responder_member_id"],
            "issuer_member_id": case["issuer_member_id"],
            "assignee_member_id": case["assignee_member_id"],
        }
        if any(document.get(key) != value for key, value in expected_envelope.items()):
            return False
        initial_revision = connection.execute(
            """
            SELECT * FROM secretary_task_alignment_revisions
            WHERE case_id = ? AND revision_no = 1
            """,
            (case["id"],),
        ).fetchone()
        if initial_revision is None:
            return False
        try:
            initial_document = cls._agreement_revision_document(initial_revision)
            task_snapshot = cls._task_agreement_document(
                task,
                case_id=case["id"],
                revision_no=1,
                parent_digest=None,
                proposer_role=initial_revision["proposed_by_role"],
                proposer_member_id=initial_revision["proposed_by_member_id"],
                responder_role=initial_revision["required_responder_role"],
                responder_member_id=initial_revision["required_responder_member_id"],
            )
        except PocketError:
            return False
        agreement_fields = (
            "issuer_member_id",
            "assignee_member_id",
            "acceptance_owner_id",
            "domain",
            "tier",
            "priority",
            "title",
            "purpose",
            "objective",
            "strategy",
            "key_points",
            "acceptance_criteria",
            "due_at",
        )
        if any(
            task_snapshot.get(field) != initial_document.get(field)
            for field in agreement_fields
        ):
            return False
        if (
            revision["case_id"] != case["id"]
            or revision["revision_no"] != case["current_revision_no"]
            or revision["base_task_version"] != initial_revision["base_task_version"]
            or task["workspace_id"] != case["workspace_id"]
            or task["id"] != case["task_id"]
            or task["stage"] != "issued"
            or not bool(task["requires_alignment"])
            or task["issuer_member_id"] != case["issuer_member_id"]
            or task["assignee_member_id"] != case["assignee_member_id"]
            or task["acceptance_owner_id"] != case["issuer_member_id"]
        ):
            return False
        for field in (
            "issuer_member_id",
            "assignee_member_id",
            "acceptance_owner_id",
            "domain",
            "tier",
            "priority",
        ):
            if document.get(field) != initial_document.get(field):
                return False
        members = connection.execute(
            """
            SELECT id, active FROM secretary_workspace_members
            WHERE workspace_id = ? AND id IN (?, ?)
            """,
            (
                case["workspace_id"],
                case["issuer_member_id"],
                case["assignee_member_id"],
            ),
        ).fetchall()
        return len(members) == 2 and all(bool(member["active"]) for member in members)

    def _mark_alignment_case_stale(
        self,
        connection: sqlite3.Connection,
        case: sqlite3.Row,
        *,
        now: str,
        device_id: str,
    ) -> sqlite3.Row:
        connection.execute(
            """
            UPDATE secretary_task_alignment_cases
            SET status = 'stale', version = version + 1,
                updated_at = ?, closed_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (now, now, case["id"]),
        )
        connection.execute(
            """
            UPDATE secretary_task_alignment_invitations
            SET revoked_at = COALESCE(revoked_at, ?)
            WHERE alignment_case_id = ? AND revoked_at IS NULL
            """,
            (now, case["id"]),
        )
        connection.execute(
            """
            UPDATE secretary_task_assignee_sessions
            SET revoked_at = COALESCE(revoked_at, ?),
                revoke_reason = COALESCE(revoke_reason, 'agreement_stale')
            WHERE agreement_id = ? AND revoked_at IS NULL
            """,
            (now, case["id"]),
        )
        updated = connection.execute(
            "SELECT * FROM secretary_task_alignment_cases WHERE id = ?",
            (case["id"],),
        ).fetchone()
        assert updated is not None
        projection = self._agreement_dict(connection, updated)
        self._append_event(
            connection,
            workspace_id=case["workspace_id"],
            aggregate_type="task_agreement",
            aggregate_id=case["id"],
            aggregate_version=updated["version"],
            event_type="task.agreement_stale",
            operation="upsert",
            actor_id=DEFAULT_OWNER_ID,
            actor_type="system",
            device_id=device_id,
            payload=projection,
        )
        return updated

    @staticmethod
    def _calendar_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "workspace_id": row["workspace_id"],
            "memo_id": row["memo_id"],
            "task_id": row["task_id"],
            "step_id": row["step_id"],
            "title": row["title"],
            "description": row["description"],
            "start_at": row["start_at_utc"],
            "end_at": row["end_at_utc"],
            "timezone": row["timezone"],
            "all_day": bool(row["all_day"]),
            "kind": row["kind"],
            "domain": row["domain"],
            "status": row["status"],
            "attendees": json_loads(row["attendees_json"], []),
            "external_provider": row["external_provider"],
            "external_id": row["external_id"],
            "version": row["version"],
            "created_by": row["created_by"],
            "updated_by": row["updated_by"],
            "client_mutation_id": row["client_mutation_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "deleted_at": row["deleted_at"],
        }

    def _minutes_dict(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        confirmations = connection.execute(
            """
            SELECT * FROM secretary_meeting_minute_confirmations
            WHERE minutes_id = ? ORDER BY display_name
            """,
            (row["id"],),
        ).fetchall()
        return {
            "id": row["id"],
            "meeting_id": row["meeting_id"],
            "revision": row["revision"],
            "content": row["content"],
            "status": row["status"],
            "version": row["version"],
            "created_by": row["created_by"],
            "client_mutation_id": row["client_mutation_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "confirmations": [
                {
                    "member_id": item["member_id"],
                    "display_name": item["display_name"],
                    "status": item["status"],
                    "comment": item["comment"],
                    "decided_at": item["decided_at"],
                }
                for item in confirmations
            ],
        }

    def _meeting_dict(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        participants = connection.execute(
            """
            SELECT * FROM secretary_meeting_participants
            WHERE meeting_id = ? ORDER BY role, display_name
            """,
            (row["id"],),
        ).fetchall()
        minutes = connection.execute(
            """
            SELECT * FROM secretary_meeting_minutes
            WHERE meeting_id = ? ORDER BY revision DESC
            """,
            (row["id"],),
        ).fetchall()
        return {
            "id": row["id"],
            "workspace_id": row["workspace_id"],
            "calendar_entry_id": row["calendar_entry_id"],
            "related_task_id": row["related_task_id"],
            "domain": row["domain"],
            "title": row["title"],
            "purpose": row["purpose"],
            "agenda": json_loads(row["agenda_json"], []),
            "starts_at": row["starts_at_utc"],
            "ends_at": row["ends_at_utc"],
            "timezone": row["timezone"],
            "organizer_member_id": row["organizer_member_id"],
            "location": row["location"],
            "provider": row["provider"],
            "external_id": row["external_id"],
            "status": row["status"],
            "participants": [
                {
                    "member_id": item["member_id"],
                    "display_name": item["display_name"],
                    "role": item["role"],
                    "rsvp": item["rsvp"],
                    "minutes_confirmation_required": bool(
                        item["minutes_confirmation_required"]
                    ),
                }
                for item in participants
            ],
            "minutes": [self._minutes_dict(connection, item) for item in minutes],
            "version": row["version"],
            "created_by": row["created_by"],
            "updated_by": row["updated_by"],
            "client_mutation_id": row["client_mutation_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "deleted_at": row["deleted_at"],
        }

    @staticmethod
    def _document_review_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "document_id": row["document_id"],
            "document_version": row["document_version"],
            "review_type": row["review_type"],
            "summary": row["summary"],
            "conclusion": row["conclusion"],
            "findings": json_loads(row["findings_json"], []),
            "reviewer_member_id": row["reviewer_member_id"],
            "version": row["version"],
            "client_mutation_id": row["client_mutation_id"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _document_excerpt_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "document_id": row["document_id"],
            "source_document_version": row["source_document_version"],
            "title": row["title"],
            "content": row["content"],
            "start_offset": row["start_offset"],
            "end_offset": row["end_offset"],
            "viewer_member_ids": json_loads(row["viewer_member_ids_json"], []),
            "version": row["version"],
            "created_by": row["created_by"],
            "client_mutation_id": row["client_mutation_id"],
            "created_at": row["created_at"],
            "revoked_at": row["revoked_at"],
        }

    def _document_dict(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        reviews = connection.execute(
            """
            SELECT * FROM secretary_document_reviews
            WHERE document_id = ? ORDER BY created_at DESC, id DESC
            """,
            (row["id"],),
        ).fetchall()
        excerpts = connection.execute(
            """
            SELECT * FROM secretary_document_excerpts
            WHERE document_id = ? AND revoked_at IS NULL
            ORDER BY created_at DESC, id DESC
            """,
            (row["id"],),
        ).fetchall()
        return {
            "id": row["id"],
            "workspace_id": row["workspace_id"],
            "source_item_id": row["source_item_id"],
            "origin_template_id": row["origin_template_id"],
            "origin_template_version": row["origin_template_version"],
            "domain": row["domain"],
            "kind": row["kind"],
            "title": row["title"],
            "content": row["content"],
            "mime_type": row["mime_type"],
            "storage_ref": row["storage_ref"],
            "source": json_loads(row["source_json"], {}),
            "access_scope": row["access_scope"],
            "viewer_member_ids": json_loads(row["viewer_member_ids_json"], []),
            "status": row["status"],
            "tags": json_loads(row["tags_json"], []),
            "template_variables": json_loads(row["template_variables_json"], {}),
            "reviews": [self._document_review_dict(item) for item in reviews],
            "excerpts": [self._document_excerpt_dict(item) for item in excerpts],
            "version": row["version"],
            "created_by": row["created_by"],
            "updated_by": row["updated_by"],
            "client_mutation_id": row["client_mutation_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "deleted_at": row["deleted_at"],
        }

    @staticmethod
    def _document_summary_dict(row: sqlite3.Row) -> dict[str, Any]:
        """Return sync/list metadata without document or review body content."""

        return {
            "id": row["id"],
            "workspace_id": row["workspace_id"],
            "source_item_id": row["source_item_id"],
            "origin_template_id": row["origin_template_id"],
            "origin_template_version": row["origin_template_version"],
            "domain": row["domain"],
            "kind": row["kind"],
            "title": row["title"],
            "mime_type": row["mime_type"],
            "access_scope": row["access_scope"],
            "viewer_member_ids": json_loads(row["viewer_member_ids_json"], []),
            "status": row["status"],
            "tags": json_loads(row["tags_json"], []),
            "version": row["version"],
            "created_by": row["created_by"],
            "updated_by": row["updated_by"],
            "client_mutation_id": row["client_mutation_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "deleted_at": row["deleted_at"],
        }

    @staticmethod
    def _active_rows(
        connection: sqlite3.Connection, table: str, workspace_id: str
    ) -> list[sqlite3.Row]:
        allowed = {
            "secretary_memos",
            "secretary_business_tasks",
            "secretary_calendar_entries",
            "secretary_meetings",
            "secretary_documents",
        }
        if table not in allowed:
            raise ValueError("unsupported workspace table")
        return connection.execute(
            f"SELECT * FROM {table} WHERE workspace_id = ? "
            "AND deleted_at IS NULL ORDER BY updated_at DESC",
            (workspace_id,),
        ).fetchall()

    def workspace(self, workspace_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = self._require_workspace(connection, workspace_id)
            members = connection.execute(
                """
                SELECT *
                FROM secretary_workspace_members
                WHERE workspace_id = ? ORDER BY role, display_name
                """,
                (workspace_id,),
            ).fetchall()
            return {
                **self._workspace_dict(row),
                "members": [self._member_dict(member) for member in members],
                "current_member_id": DEFAULT_OWNER_ID,
                "role": "owner",
            }

    def create_workspace_member(
        self,
        workspace_id: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = "workspace_member.create"
        request_payload = {"workspace_id": workspace_id, **payload}
        now = utc_now()
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached

            member_id = new_id("member")
            connection.execute(
                """
                INSERT INTO secretary_workspace_members(
                    id, workspace_id, kind, role, display_name, contact_ref,
                    active, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, ?)
                """,
                (
                    member_id,
                    workspace_id,
                    payload["kind"],
                    payload["role"],
                    payload["display_name"],
                    payload.get("contact_ref"),
                    now,
                    now,
                ),
            )
            member = connection.execute(
                "SELECT * FROM secretary_workspace_members WHERE id = ?",
                (member_id,),
            ).fetchone()
            assert member is not None
            response = self._member_dict(member)
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="workspace_member",
                aggregate_id=member_id,
                aggregate_version=1,
                event_type="workspace.member_created",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=response,
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
                status_code=201,
            )
            return response

    def bootstrap(self, workspace_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            try:
                workspace = self._require_workspace(connection, workspace_id)
                cursor = connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0)
                    FROM secretary_workspace_events WHERE workspace_id = ?
                    """,
                    (workspace_id,),
                ).fetchone()[0]
                result = {
                    "workspace": self._workspace_dict(workspace),
                    "current_member_id": DEFAULT_OWNER_ID,
                    "cursor": int(cursor),
                    "memos": [
                        self._memo_dict(row)
                        for row in self._active_rows(
                            connection, "secretary_memos", workspace_id
                        )
                    ],
                    "tasks": [
                        self._task_dict(connection, row)
                        for row in self._active_rows(
                            connection, "secretary_business_tasks", workspace_id
                        )
                    ],
                    "calendar": [
                        self._calendar_dict(row)
                        for row in self._active_rows(
                            connection, "secretary_calendar_entries", workspace_id
                        )
                    ],
                    "meetings": [
                        self._meeting_dict(connection, row)
                        for row in self._active_rows(
                            connection, "secretary_meetings", workspace_id
                        )
                    ],
                    "server_time": utc_now(),
                }
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def sync(self, workspace_id: str, after: int, limit: int) -> dict[str, Any]:
        with self.database.connect() as connection:
            self._require_workspace(connection, workspace_id)
            rows = connection.execute(
                """
                SELECT * FROM secretary_workspace_events
                WHERE workspace_id = ? AND sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                (workspace_id, after, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            page = rows[:limit]
            changes = [
                {
                    "cursor": row["sequence"],
                    "event_id": row["event_id"],
                    "aggregate_type": row["aggregate_type"],
                    "aggregate_id": row["aggregate_id"],
                    "aggregate_version": row["aggregate_version"],
                    "event_type": row["event_type"],
                    "operation": row["operation"],
                    "actor_type": row["actor_type"],
                    "actor_member_id": row["actor_member_id"],
                    "device_id": row["device_id"],
                    "payload": json_loads(row["payload_json"], {}),
                    "occurred_at": row["occurred_at"],
                }
                for row in page
            ]
            return {
                "changes": changes,
                "next_cursor": int(page[-1]["sequence"]) if page else after,
                "has_more": has_more,
                "server_time": utc_now(),
            }

    def acknowledge_cursor(
        self, workspace_id: str, device_id: str, cursor: int
    ) -> dict[str, Any]:
        now = utc_now()
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            maximum = connection.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) FROM secretary_workspace_events
                WHERE workspace_id = ?
                """,
                (workspace_id,),
            ).fetchone()[0]
            if cursor > maximum:
                raise PocketError(409, "同步游标超过服务器最新位置")
            previous = connection.execute(
                """
                SELECT last_sequence FROM secretary_workspace_sync_cursors
                WHERE workspace_id = ? AND device_id = ?
                """,
                (workspace_id, device_id),
            ).fetchone()
            if previous is not None and cursor < previous["last_sequence"]:
                raise PocketError(409, "同步游标不能后退")
            connection.execute(
                """
                INSERT INTO secretary_workspace_sync_cursors(
                    workspace_id, device_id, last_sequence, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(workspace_id, device_id) DO UPDATE SET
                    last_sequence = excluded.last_sequence,
                    updated_at = excluded.updated_at
                """,
                (workspace_id, device_id, cursor, now),
            )
            return {"device_id": device_id, "cursor": cursor, "updated_at": now}

    def list_memos(self, workspace_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            self._require_workspace(connection, workspace_id)
            items = [
                self._memo_dict(row)
                for row in self._active_rows(
                    connection, "secretary_memos", workspace_id
                )
            ]
            return {"items": items, "total": len(items)}

    def create_memo(
        self,
        workspace_id: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = "memo.create"
        request_payload = {"workspace_id": workspace_id, **payload}
        now = utc_now()
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            memo_id = new_id("memo")
            source = payload.get("source") or {
                "kind": "owner",
                "label": "主人直接输入",
            }
            if not isinstance(source, dict):
                raise PocketError(422, "备忘来源必须是对象")
            nested_authority = source.get("authority")
            authority = payload.get(
                "authority", nested_authority or "user_provided"
            )
            if authority not in {
                "authoritative",
                "observed",
                "user_provided",
                "inferred",
            }:
                raise PocketError(422, "备忘来源 authority 无效")
            source = {**source, "authority": authority}
            connection.execute(
                """
                INSERT INTO secretary_memos(
                    id, workspace_id, record_type, domain, horizon, urgency,
                    title, content, due_at, source_json, authority,
                    confirmation_status, status, tags_json, pinned, version,
                    created_by, updated_by, client_mutation_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1,
                          ?, ?, ?, ?, ?)
                """,
                (
                    memo_id,
                    workspace_id,
                    payload.get("record_type", "note"),
                    payload.get("domain", "work"),
                    payload.get("horizon", "short_term"),
                    payload.get("urgency", "normal"),
                    payload["title"],
                    payload.get("content", ""),
                    _iso_datetime(payload.get("due_at")),
                    _json(source),
                    authority,
                    payload.get("confirmation_status", "not_required"),
                    payload.get("status", "active"),
                    _json(payload.get("tags", [])),
                    int(payload.get("pinned", False)),
                    DEFAULT_OWNER_ID,
                    DEFAULT_OWNER_ID,
                    payload.get("client_mutation_id"),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM secretary_memos WHERE id = ?", (memo_id,)
            ).fetchone()
            response = self._memo_dict(row)
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="memo",
                aggregate_id=memo_id,
                aggregate_version=1,
                event_type="memo.created",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=response,
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
                status_code=201,
            )
            return response

    def _require_materializable_memo(
        self,
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        memo_id: str,
        expected_version: int,
    ) -> sqlite3.Row:
        memo = connection.execute(
            """
            SELECT * FROM secretary_memos
            WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
            """,
            (memo_id, workspace_id),
        ).fetchone()
        if memo is None:
            raise PocketError(404, "备忘不存在")
        materialized = connection.execute(
            """
            SELECT 1 FROM secretary_memo_materializations
            WHERE memo_id = ? AND workspace_id = ?
            """,
            (memo_id, workspace_id),
        ).fetchone()
        if materialized is not None or memo["status"] == "converted":
            raise PocketError(409, "该备忘已经物化")
        self._require_version(memo, expected_version)
        if memo["status"] != "active":
            raise PocketError(409, "只有 active 状态的备忘可以物化")
        if memo["confirmation_status"] not in {"not_required", "confirmed"}:
            raise PocketError(409, "备忘尚未完成主人确认，不能物化")
        return memo

    @staticmethod
    def _insert_memo_materialization(
        connection: sqlite3.Connection,
        *,
        memo: sqlite3.Row,
        source_snapshot_json: str,
        task_id: str | None,
        calendar_entry_id: str | None,
        created_at: str,
    ) -> None:
        try:
            connection.execute(
                """
                INSERT INTO secretary_memo_materializations(
                    memo_id, workspace_id, source_memo_version,
                    source_snapshot_json, task_id, calendar_entry_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memo["id"],
                    memo["workspace_id"],
                    memo["version"],
                    source_snapshot_json,
                    task_id,
                    calendar_entry_id,
                    created_at,
                ),
            )
        except sqlite3.IntegrityError:
            materialized = connection.execute(
                """
                SELECT 1 FROM secretary_memo_materializations
                WHERE memo_id = ?
                """,
                (memo["id"],),
            ).fetchone()
            if materialized is not None:
                raise PocketError(409, "该备忘已经物化") from None
            raise

    def materialize_memo_as_task(
        self,
        workspace_id: str,
        memo_id: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"memo.materialize_task:{memo_id}"
        request_payload = {
            "workspace_id": workspace_id,
            "memo_id": memo_id,
            **payload,
        }
        now = utc_now()
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached

            memo = self._require_materializable_memo(
                connection,
                workspace_id=workspace_id,
                memo_id=memo_id,
                expected_version=int(payload["expected_memo_version"]),
            )
            assignee_id = str(payload["assignee_member_id"])
            personal_disclosure_required = (
                memo["domain"] == "personal" and assignee_id != DEFAULT_OWNER_ID
            )
            personal_disclosure_confirmed = bool(
                payload.get("confirm_personal_disclosure", False)
            )
            if personal_disclosure_required and not personal_disclosure_confirmed:
                raise PocketError(409, "个人备忘交由他人承办必须明确确认披露")
            if not personal_disclosure_required and personal_disclosure_confirmed:
                raise PocketError(422, "当前任务不需要个人信息披露确认")

            owner_label = self._member_label(
                connection, workspace_id, DEFAULT_OWNER_ID
            )
            assignee = connection.execute(
                """
                SELECT display_name FROM secretary_workspace_members
                WHERE id = ? AND workspace_id = ? AND active = 1
                  AND role IN ('owner', 'member')
                """,
                (assignee_id, workspace_id),
            ).fetchone()
            if assignee is None:
                raise PocketError(422, "任务承办人不存在、已停用或没有承办权限")
            assignee_label = str(assignee["display_name"])
            task_id = new_id("task")
            task_values = {
                "id": task_id,
                "workspace_id": workspace_id,
                "origin_memo_id": memo_id,
                "title": payload["title"],
                "summary": payload["purpose"],
                "purpose": payload["purpose"],
                "objective": payload["objective"],
                "strategy": payload["strategy"],
                "key_points_json": _json(payload.get("key_points", [])),
                "acceptance_criteria_json": _json(
                    payload["acceptance_criteria"]
                ),
                "issuer_member_id": DEFAULT_OWNER_ID,
                "assignee_member_id": assignee_id,
                "acceptance_owner_id": DEFAULT_OWNER_ID,
                "issuer_label": owner_label,
                "assignee_label": assignee_label,
                "acceptance_owner_label": owner_label,
                "start_at": None,
                "due_at": _iso_datetime(payload.get("due_at")),
                "stage": "draft",
                "health": "on_track",
                "tier": payload.get("tier", "standard"),
                "domain": memo["domain"],
                "priority": payload.get("priority", "normal"),
                "progress": 0,
                "requires_alignment": int(assignee_id != DEFAULT_OWNER_ID),
                "source_json": memo["source_json"],
                "version": 1,
                "created_by": DEFAULT_OWNER_ID,
                "updated_by": DEFAULT_OWNER_ID,
                "client_mutation_id": payload["client_mutation_id"],
                "created_at": now,
                "updated_at": now,
            }
            task_columns = list(task_values)
            connection.execute(
                f"INSERT INTO secretary_business_tasks({', '.join(task_columns)}) "
                f"VALUES ({', '.join('?' for _ in task_columns)})",
                tuple(task_values[column] for column in task_columns),
            )

            source_snapshot_json = _memo_source_snapshot(memo)
            updated = connection.execute(
                """
                UPDATE secretary_memos
                SET status = 'converted', version = version + 1,
                    updated_by = ?, updated_at = ?
                WHERE id = ? AND workspace_id = ? AND version = ?
                  AND status = 'active' AND deleted_at IS NULL
                  AND confirmation_status IN ('not_required', 'confirmed')
                """,
                (
                    DEFAULT_OWNER_ID,
                    now,
                    memo_id,
                    workspace_id,
                    memo["version"],
                ),
            )
            if updated.rowcount != 1:
                raise PocketError(412, "备忘版本已变化，请重新同步")
            self._insert_memo_materialization(
                connection,
                memo=memo,
                source_snapshot_json=source_snapshot_json,
                task_id=task_id,
                calendar_entry_id=None,
                created_at=now,
            )

            task_row = connection.execute(
                "SELECT * FROM secretary_business_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            memo_row = connection.execute(
                "SELECT * FROM secretary_memos WHERE id = ?",
                (memo_id,),
            ).fetchone()
            task_response = self._task_dict(connection, task_row)
            memo_response = self._memo_dict(memo_row)
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="task",
                aggregate_id=task_id,
                aggregate_version=1,
                event_type="task.created",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=task_response,
            )
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="memo",
                aggregate_id=memo_id,
                aggregate_version=memo_response["version"],
                event_type="memo.updated",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=memo_response,
            )
            response = {"memo": memo_response, "task": task_response}
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
                status_code=201,
            )
            return response

    def materialize_memo_as_calendar(
        self,
        workspace_id: str,
        memo_id: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"memo.materialize_calendar:{memo_id}"
        request_payload = {
            "workspace_id": workspace_id,
            "memo_id": memo_id,
            **payload,
        }
        now = utc_now()
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached

            memo = self._require_materializable_memo(
                connection,
                workspace_id=workspace_id,
                memo_id=memo_id,
                expected_version=int(payload["expected_memo_version"]),
            )
            start_at = _iso_datetime(payload["start_at"])
            end_at = _iso_datetime(payload["end_at"])
            if end_at <= start_at:
                raise PocketError(422, "日程结束时间必须晚于开始时间")

            entry_id = new_id("calendar")
            entry_values = {
                "id": entry_id,
                "workspace_id": workspace_id,
                "memo_id": memo_id,
                "task_id": None,
                "step_id": None,
                "title": payload["title"],
                "description": payload.get("description") or "",
                "start_at_utc": start_at,
                "end_at_utc": end_at,
                "timezone": payload["timezone"],
                "all_day": int(payload.get("all_day", False)),
                "kind": payload.get("kind", "focus"),
                "domain": memo["domain"],
                "status": "scheduled",
                "attendees_json": "[]",
                "external_provider": None,
                "external_id": None,
                "version": 1,
                "created_by": DEFAULT_OWNER_ID,
                "updated_by": DEFAULT_OWNER_ID,
                "client_mutation_id": payload["client_mutation_id"],
                "created_at": now,
                "updated_at": now,
            }
            entry_columns = list(entry_values)
            connection.execute(
                f"INSERT INTO secretary_calendar_entries({', '.join(entry_columns)}) "
                f"VALUES ({', '.join('?' for _ in entry_columns)})",
                tuple(entry_values[column] for column in entry_columns),
            )

            source_snapshot_json = _memo_source_snapshot(memo)
            updated = connection.execute(
                """
                UPDATE secretary_memos
                SET status = 'converted', version = version + 1,
                    updated_by = ?, updated_at = ?
                WHERE id = ? AND workspace_id = ? AND version = ?
                  AND status = 'active' AND deleted_at IS NULL
                  AND confirmation_status IN ('not_required', 'confirmed')
                """,
                (
                    DEFAULT_OWNER_ID,
                    now,
                    memo_id,
                    workspace_id,
                    memo["version"],
                ),
            )
            if updated.rowcount != 1:
                raise PocketError(412, "备忘版本已变化，请重新同步")
            self._insert_memo_materialization(
                connection,
                memo=memo,
                source_snapshot_json=source_snapshot_json,
                task_id=None,
                calendar_entry_id=entry_id,
                created_at=now,
            )

            entry_row = connection.execute(
                "SELECT * FROM secretary_calendar_entries WHERE id = ?",
                (entry_id,),
            ).fetchone()
            memo_row = connection.execute(
                "SELECT * FROM secretary_memos WHERE id = ?",
                (memo_id,),
            ).fetchone()
            entry_response = self._calendar_dict(entry_row)
            memo_response = self._memo_dict(memo_row)
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="calendar_entry",
                aggregate_id=entry_id,
                aggregate_version=1,
                event_type="calendar.created",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=entry_response,
            )
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="memo",
                aggregate_id=memo_id,
                aggregate_version=memo_response["version"],
                event_type="memo.updated",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=memo_response,
            )
            response = {
                "memo": memo_response,
                "calendar_entry": entry_response,
            }
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
                status_code=201,
            )
            return response

    def update_memo(
        self,
        workspace_id: str,
        memo_id: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"memo.update:{memo_id}"
        request_payload = {"workspace_id": workspace_id, "memo_id": memo_id, **payload}
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            locked = connection.execute(
                """
                SELECT memo.status,
                       EXISTS (
                           SELECT 1 FROM secretary_memo_materializations materialization
                           WHERE materialization.memo_id = memo.id
                       ) AS is_materialized
                FROM secretary_memos memo
                WHERE memo.id = ? AND memo.workspace_id = ?
                  AND memo.deleted_at IS NULL
                """,
                (memo_id, workspace_id),
            ).fetchone()
            if locked is not None and (
                locked["status"] == "converted" or locked["is_materialized"]
            ):
                raise PocketError(409, "已物化的备忘不可修改")
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            row = connection.execute(
                """
                SELECT * FROM secretary_memos
                WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                """,
                (memo_id, workspace_id),
            ).fetchone()
            if row is None:
                raise PocketError(404, "备忘不存在")
            expected_version = int(payload["expected_version"])
            self._require_version(row, expected_version)
            mapping: dict[str, tuple[str, Callable[[Any], Any]]] = {
                "record_type": ("record_type", str),
                "domain": ("domain", str),
                "horizon": ("horizon", str),
                "urgency": ("urgency", str),
                "title": ("title", str),
                "content": ("content", str),
                "due_at": ("due_at", _iso_datetime),
                "source": ("source_json", _json),
                "authority": ("authority", str),
                "confirmation_status": ("confirmation_status", str),
                "status": ("status", str),
                "tags": ("tags_json", _json),
                "pinned": ("pinned", int),
            }
            assignments: list[str] = []
            values: list[Any] = []
            for key, (column, transform) in mapping.items():
                if key in payload:
                    assignments.append(f"{column} = ?")
                    values.append(transform(payload[key]))
            if not assignments:
                raise PocketError(422, "没有可更新的备忘字段")
            now = utc_now()
            assignments.extend(
                ["version = version + 1", "updated_by = ?", "updated_at = ?"]
            )
            values.extend(
                [DEFAULT_OWNER_ID, now, memo_id, workspace_id, expected_version]
            )
            updated = connection.execute(
                f"UPDATE secretary_memos SET {', '.join(assignments)} "
                "WHERE id = ? AND workspace_id = ? AND version = ?",
                values,
            )
            if updated.rowcount != 1:
                raise PocketError(412, "备忘版本已变化，请重新同步")
            row = connection.execute(
                "SELECT * FROM secretary_memos WHERE id = ?", (memo_id,)
            ).fetchone()
            response = self._memo_dict(row)
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="memo",
                aggregate_id=memo_id,
                aggregate_version=response["version"],
                event_type="memo.updated",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=response,
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
            )
            return response

    def delete_memo(
        self,
        workspace_id: str,
        memo_id: str,
        expected_version: int,
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"memo.delete:{memo_id}"
        request_payload = {
            "workspace_id": workspace_id,
            "memo_id": memo_id,
            "expected_version": expected_version,
        }
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            locked = connection.execute(
                """
                SELECT memo.status,
                       EXISTS (
                           SELECT 1 FROM secretary_memo_materializations materialization
                           WHERE materialization.memo_id = memo.id
                       ) AS is_materialized
                FROM secretary_memos memo
                WHERE memo.id = ? AND memo.workspace_id = ?
                  AND memo.deleted_at IS NULL
                """,
                (memo_id, workspace_id),
            ).fetchone()
            if locked is not None and (
                locked["status"] == "converted" or locked["is_materialized"]
            ):
                raise PocketError(409, "已物化的备忘不可删除")
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            row = connection.execute(
                """
                SELECT * FROM secretary_memos
                WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                """,
                (memo_id, workspace_id),
            ).fetchone()
            if row is None:
                raise PocketError(404, "备忘不存在")
            self._require_version(row, expected_version)
            now = utc_now()
            connection.execute(
                """
                UPDATE secretary_memos SET deleted_at = ?, updated_at = ?,
                    updated_by = ?, version = version + 1
                WHERE id = ? AND version = ?
                """,
                (now, now, DEFAULT_OWNER_ID, memo_id, expected_version),
            )
            tombstone = {
                "id": memo_id,
                "workspace_id": workspace_id,
                "version": expected_version + 1,
                "deleted_at": now,
            }
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="memo",
                aggregate_id=memo_id,
                aggregate_version=expected_version + 1,
                event_type="memo.deleted",
                operation="delete",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=tombstone,
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=tombstone,
            )
            return tombstone

    @staticmethod
    def _member_label(
        connection: sqlite3.Connection, workspace_id: str, member_id: str
    ) -> str:
        row = connection.execute(
            """
            SELECT display_name FROM secretary_workspace_members
            WHERE id = ? AND workspace_id = ? AND active = 1
            """,
            (member_id, workspace_id),
        ).fetchone()
        if row is None:
            raise PocketError(422, f"工作区成员不存在或已停用：{member_id}")
        return str(row["display_name"])

    def list_tasks(self, workspace_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            self._require_workspace(connection, workspace_id)
            items = [
                self._task_dict(connection, row)
                for row in self._active_rows(
                    connection, "secretary_business_tasks", workspace_id
                )
            ]
            return {"items": items, "total": len(items)}

    def task_analysis(
        self, workspace_id: str, from_date: date, to_date: date
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            try:
                workspace = self._require_workspace(connection, workspace_id)
                snapshot_at = utc_now()
                result = build_task_analysis(
                    connection,
                    workspace,
                    from_date,
                    to_date,
                    snapshot_at=snapshot_at,
                )
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def create_task_checkin(
        self,
        workspace_id: str,
        task_id: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        operation = f"task.checkin.create:{task_id}"
        request_payload = {
            "workspace_id": workspace_id,
            "task_id": task_id,
            **payload,
        }
        now_dt = _as_of_utc(as_of)
        now = _iso_datetime(now_dt)
        assert now is not None
        with self.database.transaction() as connection:
            workspace = self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            task = connection.execute(
                """
                SELECT * FROM secretary_business_tasks
                WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                """,
                (task_id, workspace_id),
            ).fetchone()
            if task is None:
                raise PocketError(404, "任务不存在")
            expected_version = int(payload["expected_version"])
            self._require_version(task, expected_version)
            if task["stage"] in TERMINAL_TASK_STAGES:
                raise PocketError(409, "已关闭任务不能追加复盘")

            report_date = (
                now_dt.astimezone(ZoneInfo(str(workspace["timezone"])))
                .date()
                .isoformat()
            )
            checkin_id = new_id("checkin")
            connection.execute(
                """
                INSERT INTO secretary_task_checkins(
                    id, workspace_id, task_id, task_version, report_date,
                    summary, reported_progress, risks_json, blockers_json,
                    next_actions_json, forecast_at, created_by, device_id,
                    client_mutation_id, version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    checkin_id,
                    workspace_id,
                    task_id,
                    expected_version,
                    report_date,
                    payload["summary"],
                    payload["reported_progress"],
                    _json(payload.get("risks", [])),
                    _json(payload.get("blockers", [])),
                    _json(payload.get("next_actions", [])),
                    _iso_datetime(payload.get("forecast_at")),
                    DEFAULT_OWNER_ID,
                    device_id,
                    payload.get("client_mutation_id"),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM secretary_task_checkins WHERE id = ?",
                (checkin_id,),
            ).fetchone()
            assert row is not None
            response = self._checkin_dict(row)
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="task_checkin",
                aggregate_id=checkin_id,
                aggregate_version=1,
                event_type="task.checkin_recorded",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=response,
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
                status_code=201,
            )
            return response

    def list_task_checkins(self, workspace_id: str, task_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            self._require_workspace(connection, workspace_id)
            task = connection.execute(
                """
                SELECT 1 FROM secretary_business_tasks
                WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                """,
                (task_id, workspace_id),
            ).fetchone()
            if task is None:
                raise PocketError(404, "任务不存在")
            total = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM secretary_task_checkins
                    WHERE task_id = ? AND workspace_id = ?
                    """,
                    (task_id, workspace_id),
                ).fetchone()[0]
            )
            rows = connection.execute(
                """
                SELECT * FROM secretary_task_checkins
                WHERE task_id = ? AND workspace_id = ?
                ORDER BY created_at DESC, rowid DESC LIMIT 100
                """,
                (task_id, workspace_id),
            ).fetchall()
            return {
                "items": [self._checkin_dict(row) for row in rows],
                "total": total,
            }

    def task_attention(
        self, workspace_id: str, *, as_of: datetime | None = None
    ) -> dict[str, Any]:
        now_dt = _as_of_utc(as_of)
        generated_at = _iso_datetime(now_dt)
        assert generated_at is not None
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            try:
                workspace = self._require_workspace(connection, workspace_id)
                workspace_timezone = str(workspace["timezone"])
                timezone = ZoneInfo(workspace_timezone)
                local_date = now_dt.astimezone(timezone).date().isoformat()
                tasks = connection.execute(
                    """
                    SELECT * FROM secretary_business_tasks
                    WHERE workspace_id = ? AND deleted_at IS NULL
                      AND stage IN ('aligned', 'in_progress')
                    ORDER BY id
                    """,
                    (workspace_id,),
                ).fetchall()
                items: list[dict[str, Any]] = []
                for task in tasks:
                    attention = self._task_attention_item(
                        connection,
                        task,
                        now_dt=now_dt,
                        local_date=local_date,
                        timezone=timezone,
                    )
                    if attention is not None:
                        items.append(attention)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        items.sort(
            key=lambda item: (
                0 if item["severity"] == "critical" else 1,
                item["due_at"] or "9999-12-31T23:59:59Z",
                item["task_id"],
            )
        )
        return {
            "generated_at": generated_at,
            "workspace_timezone": workspace_timezone,
            "items": items,
            "total": len(items),
        }

    def _task_attention_item(
        self,
        connection: sqlite3.Connection,
        task: sqlite3.Row,
        *,
        now_dt: datetime,
        local_date: str,
        timezone: ZoneInfo,
    ) -> dict[str, Any] | None:
        if task["stage"] not in ATTENTION_TASK_STAGES:
            return None
        steps = connection.execute(
            """
            SELECT * FROM secretary_task_steps
            WHERE task_id = ? AND deleted_at IS NULL AND status <> 'canceled'
            ORDER BY position, created_at, id
            """,
            (task["id"],),
        ).fetchall()
        latest = connection.execute(
            """
            SELECT * FROM secretary_task_checkins
            WHERE task_id = ? AND workspace_id = ?
            ORDER BY created_at DESC, rowid DESC LIMIT 1
            """,
            (task["id"], task["workspace_id"]),
        ).fetchone()
        reasons: list[dict[str, Any]] = []

        def add_reason(
            code: str,
            severity: str,
            message: str,
            evidence: dict[str, Any] | None = None,
        ) -> None:
            reasons.append(
                {
                    "code": code,
                    "severity": severity,
                    "message": message,
                    "evidence": evidence or {},
                }
            )

        if not steps:
            add_reason(
                "plan_missing",
                "warning",
                "任务尚未分解为可执行步骤。",
            )

        start_value = task["started_at"] or task["start_at"]
        if task["stage"] == "in_progress" and start_value is not None:
            started_local_date = parse_utc(start_value).astimezone(timezone).date()
            today_checkin = connection.execute(
                """
                SELECT 1 FROM secretary_task_checkins
                WHERE task_id = ? AND workspace_id = ? AND report_date = ?
                LIMIT 1
                """,
                (task["id"], task["workspace_id"], local_date),
            ).fetchone()
            if started_local_date.isoformat() < local_date and today_checkin is None:
                add_reason(
                    "review_due",
                    "warning",
                    "执行中的任务今天尚未记录复盘。",
                    {
                        "local_date": local_date,
                        "latest_check_in_at": latest["created_at"] if latest else None,
                    },
                )

        for step in steps:
            if step["status"] == "done" or step["due_at"] is None:
                continue
            if parse_utc(step["due_at"]) < now_dt:
                add_reason(
                    "step_overdue",
                    "critical",
                    f"任务步骤“{step['title']}”已经超过期限。",
                    {
                        "step_id": step["id"],
                        "step_title": step["title"],
                        "step_due_at": step["due_at"],
                    },
                )

        schedules = connection.execute(
            """
            SELECT entry.id, entry.title, entry.end_at_utc, entry.step_id,
                   step.status AS step_status
            FROM secretary_calendar_entries entry
            LEFT JOIN secretary_task_steps step ON step.id = entry.step_id
            WHERE entry.workspace_id = ? AND entry.deleted_at IS NULL
              AND entry.status = 'scheduled'
              AND (
                    entry.task_id = ?
                    OR (
                        step.task_id = ? AND step.deleted_at IS NULL
                        AND step.status NOT IN ('done', 'canceled')
                    )
              )
            ORDER BY entry.end_at_utc, entry.id
            """,
            (task["workspace_id"], task["id"], task["id"]),
        ).fetchall()
        for schedule in schedules:
            if parse_utc(schedule["end_at_utc"]) < now_dt:
                add_reason(
                    "schedule_missed",
                    "critical",
                    f"日程“{schedule['title']}”已结束，但关联任务事项尚未完成。",
                    {
                        "schedule_id": schedule["id"],
                        "schedule_title": schedule["title"],
                        "schedule_end_at": schedule["end_at_utc"],
                    },
                )

        due_at = task["due_at"]
        due_dt = parse_utc(due_at) if due_at is not None else None
        if due_dt is not None and due_dt < now_dt:
            add_reason(
                "task_overdue",
                "critical",
                "任务已经超过完成期限。",
                {"task_due_at": due_at},
            )

        blocked_steps = [step for step in steps if step["status"] == "blocked"]
        latest_blockers = json_loads(latest["blockers_json"], []) if latest else []
        task_blocked = task["health"] == "blocked"
        if task_blocked or blocked_steps or latest_blockers:
            blocked_evidence: dict[str, Any] = {}
            if blocked_steps:
                first_blocked = blocked_steps[0]
                blocked_evidence.update(
                    {
                        "step_id": first_blocked["id"],
                        "step_title": first_blocked["title"],
                    }
                )
                if first_blocked["due_at"] is not None:
                    blocked_evidence["step_due_at"] = first_blocked["due_at"]
            if latest_blockers:
                blocked_evidence["latest_check_in_at"] = latest["created_at"]
                blocked_evidence["blockers"] = latest_blockers[:3]
            add_reason(
                "blocked",
                "critical",
                "任务、步骤或最新复盘显示存在明确阻塞。",
                blocked_evidence,
            )

        forecast_at = latest["forecast_at"] if latest else None
        if (
            forecast_at is not None
            and due_dt is not None
            and parse_utc(forecast_at) > due_dt
        ):
            add_reason(
                "forecast_slip",
                "warning",
                "最新复盘预计完成时间晚于任务期限。",
                {
                    "task_due_at": due_at,
                    "latest_check_in_at": latest["created_at"],
                    "forecast_at": forecast_at,
                },
            )

        if due_dt is not None:
            seconds_remaining = (due_dt - now_dt).total_seconds()
            if 0 <= seconds_remaining <= 48 * 60 * 60 and task["progress"] < 80:
                add_reason(
                    "due_soon",
                    "warning",
                    "任务将在 48 小时内到期，正式进度仍低于 80%。",
                    {
                        "task_due_at": due_at,
                        "canonical_progress": task["progress"],
                        "threshold_progress": 80,
                        "hours_remaining": round(seconds_remaining / 3600, 2),
                    },
                )

        if not reasons:
            return None
        severity = (
            "critical"
            if any(reason["severity"] == "critical" for reason in reasons)
            else "warning"
        )
        return {
            "task_id": task["id"],
            "task_version": task["version"],
            "title": task["title"],
            "stage": task["stage"],
            "progress": task["progress"],
            "due_at": due_at,
            "latest_check_in_at": latest["created_at"] if latest else None,
            "latest_reported_progress": (
                latest["reported_progress"] if latest else None
            ),
            "severity": severity,
            "reasons": reasons,
        }

    def create_task(
        self,
        workspace_id: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = "task.create"
        request_payload = {"workspace_id": workspace_id, **payload}
        now = utc_now()
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            if payload.get("origin_memo_id") is not None:
                raise PocketError(409, "备忘转任务必须使用专用物化接口")
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached

            issuer_id = payload["issuer_member_id"]
            assignee_id = payload["assignee_member_id"]
            acceptor_id = payload["acceptance_owner_id"]
            issuer_label = self._member_label(connection, workspace_id, issuer_id)
            assignee_label = self._member_label(connection, workspace_id, assignee_id)
            acceptor_label = self._member_label(connection, workspace_id, acceptor_id)
            origin_memo_id = payload.get("origin_memo_id")

            task_id = new_id("task")
            task_values = {
                "id": task_id,
                "workspace_id": workspace_id,
                "origin_memo_id": origin_memo_id,
                "title": payload["title"],
                "summary": payload.get("summary") or payload["purpose"],
                "purpose": payload["purpose"],
                "objective": payload["objective"],
                "strategy": payload["strategy"],
                "key_points_json": _json(payload.get("key_points", [])),
                "acceptance_criteria_json": _json(payload["acceptance_criteria"]),
                "issuer_member_id": issuer_id,
                "assignee_member_id": assignee_id,
                "acceptance_owner_id": acceptor_id,
                "issuer_label": issuer_label,
                "assignee_label": assignee_label,
                "acceptance_owner_label": acceptor_label,
                "start_at": _iso_datetime(payload.get("start_at")),
                "due_at": _iso_datetime(payload.get("due_at")),
                "stage": "draft",
                "health": payload.get("health", "on_track"),
                "tier": payload.get("tier", "standard"),
                "domain": payload["domain"],
                "priority": payload.get("priority", "normal"),
                "progress": 0,
                "requires_alignment": int(issuer_id != assignee_id),
                "source_json": _json(payload.get("source") or {}),
                "version": 1,
                "created_by": DEFAULT_OWNER_ID,
                "updated_by": DEFAULT_OWNER_ID,
                "client_mutation_id": payload.get("client_mutation_id"),
                "created_at": now,
                "updated_at": now,
            }
            columns = list(task_values)
            connection.execute(
                f"INSERT INTO secretary_business_tasks({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                tuple(task_values[column] for column in columns),
            )

            generated_steps: list[tuple[str, dict[str, Any]]] = []
            for position, step in enumerate(payload.get("steps", [])):
                step_id = new_id("step")
                step_assignee = step.get("assignee_member_id") or assignee_id
                step_label = self._member_label(connection, workspace_id, step_assignee)
                connection.execute(
                    """
                    INSERT INTO secretary_task_steps(
                        id, workspace_id, task_id, parent_step_id, step_type,
                        title, description, assignee_member_id, assignee_label,
                        status, position, due_at, success_metric_json, version,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        step_id,
                        workspace_id,
                        task_id,
                        step.get("parent_step_id"),
                        step.get("step_type", "action"),
                        step["title"],
                        step.get("description") or "",
                        step_assignee,
                        step_label,
                        position,
                        _iso_datetime(step.get("due_at")),
                        _json(step.get("success_metric") or {}),
                        now,
                        now,
                    ),
                )
                generated_steps.append((step_id, step))

            # Dependencies can reference pre-existing step IDs. References to
            # client-local IDs are intentionally rejected by the foreign key
            # instead of being guessed by the server.
            for step_id, step in generated_steps:
                for dependency_id in step.get("depends_on_step_ids", []):
                    dependency = connection.execute(
                        """
                        SELECT task_id FROM secretary_task_steps WHERE id = ?
                        """,
                        (dependency_id,),
                    ).fetchone()
                    if dependency is None or dependency["task_id"] != task_id:
                        raise PocketError(422, "步骤依赖必须属于同一任务")
                    connection.execute(
                        """
                        INSERT INTO secretary_task_step_dependencies(
                            step_id, depends_on_step_id
                        ) VALUES (?, ?)
                        """,
                        (step_id, dependency_id),
                    )

            self._validate_task_step_graph(connection, task_id)

            row = connection.execute(
                "SELECT * FROM secretary_business_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            response = self._task_dict(connection, row)
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="task",
                aggregate_id=task_id,
                aggregate_version=1,
                event_type="task.created",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=response,
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
                status_code=201,
            )
            return response

    def update_task(
        self,
        workspace_id: str,
        task_id: str,
        expected_version: int,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"task.update:{task_id}"
        request_payload = {
            "workspace_id": workspace_id,
            "task_id": task_id,
            "expected_version": expected_version,
            **payload,
        }
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            row = connection.execute(
                """
                SELECT * FROM secretary_business_tasks
                WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                """,
                (task_id, workspace_id),
            ).fetchone()
            if row is None:
                raise PocketError(404, "任务不存在")
            self._require_version(row, expected_version)
            agreement_fields = {
                "domain",
                "title",
                "purpose",
                "objective",
                "strategy",
                "key_points",
                "priority",
            }
            if agreement_fields.intersection(payload):
                locked_agreement = connection.execute(
                    """
                    SELECT id, status FROM secretary_task_alignment_cases
                    WHERE task_id = ? AND status IN ('pending', 'accepted')
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (task_id,),
                ).fetchone()
                if locked_agreement is not None:
                    raise PocketError(
                        409,
                        (
                            "任务协议待回应期间不能直接修改协议字段，请发起反提案"
                            if locked_agreement["status"] == "pending"
                            else "已接受协议的字段不能直接修改；当前版本尚未支持变更协议"
                        ),
                    )
            if row["stage"] != "draft":
                immutable_after_issue = {"domain", "title", "start_at"}
                if immutable_after_issue.intersection(payload):
                    raise PocketError(409, "任务下达后标题、范围与开始时间不可直接修改")
                if row["stage"] != "issued" and {"purpose", "objective"}.intersection(
                    payload
                ):
                    raise PocketError(409, "任务完成对齐后，目的和目标不可直接修改")
            mapping: dict[str, tuple[str, Callable[[Any], Any]]] = {
                "domain": ("domain", str),
                "title": ("title", str),
                "purpose": ("purpose", str),
                "objective": ("objective", str),
                "strategy": ("strategy", str),
                "key_points": ("key_points_json", _json),
                "priority": ("priority", str),
                "health": ("health", str),
                "start_at": ("start_at", _iso_datetime),
            }
            assignments: list[str] = []
            values: list[Any] = []
            for key, (column, transform) in mapping.items():
                if key in payload:
                    assignments.append(f"{column} = ?")
                    values.append(transform(payload[key]))
            if not assignments:
                raise PocketError(422, "没有可更新的任务字段")
            now = utc_now()
            assignments.extend(
                ["version = version + 1", "updated_by = ?", "updated_at = ?"]
            )
            values.extend(
                [DEFAULT_OWNER_ID, now, task_id, workspace_id, expected_version]
            )
            connection.execute(
                f"UPDATE secretary_business_tasks SET {', '.join(assignments)} "
                "WHERE id = ? AND workspace_id = ? AND version = ?",
                values,
            )
            row = connection.execute(
                "SELECT * FROM secretary_business_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            response = self._task_dict(connection, row)
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="task",
                aggregate_id=task_id,
                aggregate_version=response["version"],
                event_type="task.updated",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=response,
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
            )
            return response

    def create_task_alignment_invitation(
        self,
        workspace_id: str,
        task_id: str,
        expected_version: int,
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        now_dt = datetime.now(UTC)
        now = _iso_datetime(now_dt)
        expires_at = _iso_datetime(
            now_dt + timedelta(seconds=ALIGNMENT_INVITATION_TTL_SECONDS)
        )
        error: PocketError | None = None
        result: dict[str, Any] | None = None
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            replay = connection.execute(
                """
                SELECT id FROM secretary_task_alignment_invitations
                WHERE workspace_id = ? AND created_by = ?
                  AND task_id = ? AND creation_idempotency_key = ?
                """,
                (workspace_id, DEFAULT_OWNER_ID, task_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                raise PocketError(
                    409,
                    "此请求已创建过邀请；确认码只返回一次，请用新的请求键重新创建",
                )

            task = connection.execute(
                """
                SELECT * FROM secretary_business_tasks
                WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                """,
                (task_id, workspace_id),
            ).fetchone()
            if task is None:
                raise PocketError(404, "任务不存在")
            self._require_version(task, expected_version)
            if task["stage"] != "issued":
                raise PocketError(409, "只有已下达且待对齐的任务才能创建确认邀请")
            if (
                not bool(task["requires_alignment"])
                or task["assignee_member_id"] == DEFAULT_OWNER_ID
            ):
                raise PocketError(409, "该任务不需要独立承办人确认")
            assignee_label = self._member_label(
                connection, workspace_id, task["assignee_member_id"]
            )

            alignment_case = connection.execute(
                """
                SELECT * FROM secretary_task_alignment_cases
                WHERE task_id = ? AND status = 'pending'
                """,
                (task_id,),
            ).fetchone()
            created_case = alignment_case is None
            if alignment_case is None:
                alignment_case, revision = self._create_alignment_case(
                    connection, task, now=now
                )
            else:
                revision = connection.execute(
                    """
                    SELECT * FROM secretary_task_alignment_revisions
                    WHERE case_id = ? AND revision_no = ?
                    """,
                    (
                        alignment_case["id"],
                        alignment_case["current_revision_no"],
                    ),
                ).fetchone()
                if revision is None or not self._alignment_case_is_current(
                    connection, alignment_case, revision, task
                ):
                    self._mark_alignment_case_stale(
                        connection,
                        alignment_case,
                        now=now,
                        device_id=device_id,
                    )
                    error = PocketError(
                        409,
                        "任务或成员绑定已变化；旧协议已标记失效，请重新创建邀请",
                    )
                elif (
                    revision["required_responder_role"] != "assignee"
                    or revision["required_responder_member_id"]
                    != alignment_case["assignee_member_id"]
                ):
                    error = PocketError(
                        409, "当前协议正在等待下达人回应，不能创建承办人邀请"
                    )

            if error is not None:
                revision = None
            else:
                assert alignment_case is not None and revision is not None
                if created_case:
                    agreement_projection = self._agreement_dict(
                        connection, alignment_case
                    )
                    self._append_event(
                        connection,
                        workspace_id=workspace_id,
                        aggregate_type="task_agreement",
                        aggregate_id=alignment_case["id"],
                        aggregate_version=alignment_case["version"],
                        event_type="task.agreement_created",
                        operation="upsert",
                        actor_id=DEFAULT_OWNER_ID,
                        device_id=device_id,
                        payload=agreement_projection,
                    )

                connection.execute(
                    """
                    UPDATE secretary_task_assignee_sessions
                    SET revoked_at = COALESCE(revoked_at, ?),
                        revoke_reason = COALESCE(
                            revoke_reason, 'superseded_by_new_invitation'
                        )
                    WHERE task_id = ? AND agreement_id = ?
                      AND revoked_at IS NULL
                    """,
                    (now, task_id, alignment_case["id"]),
                )
                connection.execute(
                    """
                    UPDATE secretary_task_alignment_invitations
                    SET revoked_at = COALESCE(revoked_at, ?)
                    WHERE task_id = ? AND revoked_at IS NULL
                    """,
                    (now, task_id),
                )

                for _attempt in range(5):
                    invitation_id = new_id("align")
                    code = _new_alignment_code()
                    try:
                        connection.execute(
                            """
                            INSERT INTO secretary_task_alignment_invitations(
                                id, workspace_id, task_id, task_version,
                                assignee_member_id, code_hash, failed_attempts,
                                max_attempts, created_by, created_device_id,
                                creation_idempotency_key, created_at, expires_at,
                                alignment_case_id, alignment_revision_id,
                                alignment_revision_digest
                            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                invitation_id,
                                workspace_id,
                                task_id,
                                expected_version,
                                task["assignee_member_id"],
                                _secret_hash(code),
                                ALIGNMENT_MAX_FAILED_ATTEMPTS,
                                DEFAULT_OWNER_ID,
                                device_id,
                                idempotency_key,
                                now,
                                expires_at,
                                alignment_case["id"],
                                revision["id"],
                                revision["digest"],
                            ),
                        )
                    except sqlite3.IntegrityError:
                        continue
                    break
                else:
                    raise PocketError(503, "暂时无法创建任务对齐邀请")

                task_payload = self._task_dict(connection, task)
                self._append_event(
                    connection,
                    workspace_id=workspace_id,
                    aggregate_type="task",
                    aggregate_id=task_id,
                    aggregate_version=expected_version,
                    event_type="task.alignment_invitation_created",
                    operation="upsert",
                    actor_id=DEFAULT_OWNER_ID,
                    device_id=device_id,
                    payload=task_payload,
                )
                result = {
                    "invitation_id": invitation_id,
                    "code": code,
                    "expires_at": expires_at,
                    "task_id": task_id,
                    "task_version": expected_version,
                    "assignee_member_id": task["assignee_member_id"],
                    "assignee_label": assignee_label,
                    "confirmation_path": f"/api/v1/task-alignments/{invitation_id}",
                }

        if error is not None:
            raise error
        if result is None:
            raise PocketError(503, "暂时无法创建任务对齐邀请")
        return result

    def task_alignment_invitation_shell(self, invitation_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            invitation = connection.execute(
                """
                SELECT * FROM secretary_task_alignment_invitations WHERE id = ?
                """,
                (invitation_id,),
            ).fetchone()
            if invitation is None:
                raise PocketError(404, "任务对齐邀请不可用")
            if (
                invitation["revoked_at"] is not None
                or invitation["consumed_at"] is not None
                or parse_utc(invitation["expires_at"]) <= datetime.now(UTC)
            ):
                raise PocketError(410, "任务对齐邀请已失效")
            if invitation["code_used_at"] is not None:
                raise PocketError(410, "确认码已使用，请让下达人重新创建邀请")
            return {
                "invitation_id": invitation["id"],
                "expires_at": invitation["expires_at"],
            }

    def authenticate_task_session(
        self,
        access_token: str,
        *,
        requested_device_id: str | None = None,
        allow_closed_replay: bool = False,
    ) -> dict[str, Any]:
        if not access_token.startswith("cp_task_at_"):
            raise PocketError(401, "任务会话凭据无效或已失效")
        presented_hash = _secret_hash(access_token)
        now_dt = datetime.now(UTC)
        now = _iso_datetime(now_dt)
        error: PocketError | None = None
        principal: dict[str, Any] | None = None
        with self.database.transaction() as connection:
            session = connection.execute(
                """
                SELECT * FROM secretary_task_assignee_sessions
                WHERE token_hash = ?
                """,
                (presented_hash,),
            ).fetchone()
            token_matches = bool(
                session is not None
                and secrets.compare_digest(session["token_hash"], presented_hash)
            )
            if not token_matches:
                error = PocketError(401, "任务会话凭据无效或已失效")
            elif parse_utc(session["expires_at"]) <= now_dt:
                if session["revoked_at"] is None:
                    connection.execute(
                        """
                        UPDATE secretary_task_assignee_sessions
                        SET revoked_at = ?, revoke_reason = 'expired'
                        WHERE id = ? AND revoked_at IS NULL
                        """,
                        (now, session["id"]),
                    )
                error = PocketError(401, "任务会话凭据无效或已失效")
            elif requested_device_id is not None and not secrets.compare_digest(
                requested_device_id.encode("utf-8"),
                session["client_device_id"].encode("utf-8"),
            ):
                error = PocketError(403, "任务会话与设备标识不匹配")
            elif session["revoked_at"] is not None and not (
                allow_closed_replay
                and session["revoke_reason"]
                in {"agreement_accepted", "agreement_rejected"}
            ):
                error = PocketError(401, "任务会话凭据无效或已失效")
            else:
                principal = {
                    "auth_kind": "task_session",
                    "assurance_method": "task_session",
                    "member_id": session["assignee_member_id"],
                    "session_id": session["id"],
                    "invitation_id": session["invitation_id"],
                    "task_id": session["task_id"],
                    "agreement_id": session["agreement_id"],
                    "device_id": session["client_device_id"],
                    "presented_token_hash": presented_hash,
                    "idempotency_actor_id": f"task-session:{session['id']}",
                    "replay_only": session["revoked_at"] is not None,
                }
        if error is not None:
            raise error
        if principal is None:
            raise PocketError(401, "任务会话凭据无效或已失效")
        return principal

    def authenticate_task_change_session(
        self,
        access_token: str,
        *,
        requested_device_id: str | None = None,
        allow_closed_replay: bool = False,
    ) -> dict[str, Any]:
        if not access_token.startswith("cp_task_ch_"):
            raise PocketError(401, "任务变更会话凭据无效或已失效")
        presented_hash = _secret_hash(access_token)
        now_dt = datetime.now(UTC)
        now = _iso_datetime(now_dt)
        error: PocketError | None = None
        principal: dict[str, Any] | None = None
        with self.database.transaction() as connection:
            session = connection.execute(
                """
                SELECT * FROM secretary_task_change_sessions
                WHERE token_hash = ?
                """,
                (presented_hash,),
            ).fetchone()
            token_matches = bool(
                session is not None
                and secrets.compare_digest(session["token_hash"], presented_hash)
            )
            if not token_matches:
                error = PocketError(401, "任务变更会话凭据无效或已失效")
            elif parse_utc(session["expires_at"]) <= now_dt:
                if session["revoked_at"] is None:
                    connection.execute(
                        """
                        UPDATE secretary_task_change_sessions
                        SET revoked_at = ?, revoke_reason = 'expired'
                        WHERE id = ? AND revoked_at IS NULL
                        """,
                        (now, session["id"]),
                    )
                error = PocketError(401, "任务变更会话凭据无效或已失效")
            elif requested_device_id is not None and not secrets.compare_digest(
                requested_device_id.encode("utf-8"),
                session["client_device_id"].encode("utf-8"),
            ):
                error = PocketError(403, "任务变更会话与设备标识不匹配")
            elif session["revoked_at"] is not None and not (
                allow_closed_replay
                and session["revoke_reason"]
                in {"change_accepted", "change_rejected"}
            ):
                error = PocketError(401, "任务变更会话凭据无效或已失效")
            else:
                principal = {
                    "auth_kind": "task_change_session",
                    "assurance_method": "task_change_session",
                    "member_id": session["responder_member_id"],
                    "session_id": session["id"],
                    "invitation_id": session["invitation_id"],
                    "task_id": session["task_id"],
                    "change_id": session["change_id"],
                    "device_id": session["client_device_id"],
                    "presented_token_hash": presented_hash,
                    "idempotency_actor_id": f"task-change-session:{session['id']}",
                    "replay_only": session["revoked_at"] is not None,
                }
        if error is not None:
            raise error
        if principal is None:
            raise PocketError(401, "任务变更会话凭据无效或已失效")
        return principal

    def authenticate_task_execution_session(
        self,
        access_token: str,
        *,
        requested_device_id: str | None = None,
        allow_closed_replay: bool = False,
    ) -> dict[str, Any]:
        if not access_token.startswith("cp_task_ex_"):
            raise PocketError(401, "任务执行会话凭据无效或已失效")
        presented_hash = _secret_hash(access_token)
        now_dt = datetime.now(UTC)
        now = _iso_datetime(now_dt)
        assert now is not None
        error: PocketError | None = None
        principal: dict[str, Any] | None = None
        with self.database.transaction() as connection:
            session = connection.execute(
                """
                SELECT * FROM secretary_task_execution_sessions
                WHERE token_hash = ?
                """,
                (presented_hash,),
            ).fetchone()
            family = (
                connection.execute(
                    """
                    SELECT * FROM secretary_task_execution_refresh_families
                    WHERE id = ?
                    """,
                    (session["refresh_family_id"],),
                ).fetchone()
                if session is not None
                else None
            )
            task = (
                connection.execute(
                    """
                    SELECT * FROM secretary_business_tasks
                    WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                    """,
                    (session["task_id"], session["workspace_id"]),
                ).fetchone()
                if session is not None
                else None
            )
            member = (
                connection.execute(
                    """
                    SELECT * FROM secretary_workspace_members
                    WHERE id = ? AND workspace_id = ?
                    """,
                    (session["assignee_member_id"], session["workspace_id"]),
                ).fetchone()
                if session is not None
                else None
            )
            if session is None or not secrets.compare_digest(
                session["token_hash"], presented_hash
            ):
                error = PocketError(401, "任务执行会话凭据无效或已失效")
            elif requested_device_id is None:
                error = PocketError(428, "任务执行会话请求必须提供 X-Device-ID")
            elif not secrets.compare_digest(
                requested_device_id.encode("utf-8"),
                session["client_device_id"].encode("utf-8"),
            ):
                error = PocketError(403, "任务执行会话与设备标识不匹配")
            elif parse_utc(session["expires_at"]) <= now_dt:
                if session["revoked_at"] is None:
                    connection.execute(
                        """
                        UPDATE secretary_task_execution_sessions
                        SET revoked_at = ?, revoke_reason = 'access_expired'
                        WHERE id = ? AND revoked_at IS NULL
                        """,
                        (now, session["id"]),
                    )
                error = PocketError(401, "任务执行会话凭据无效或已失效")
            elif (
                family is None
                or family["revoked_at"] is not None
                or parse_utc(family["absolute_expires_at"]) <= now_dt
                or task is None
                or self._task_execution_effective_expiry(task, family) <= now_dt
                or member is None
                or not bool(member["active"])
                or member["kind"] != "external"
                or task["stage"] not in {"aligned", "in_progress", "submitted"}
                or task["assignee_member_id"] != session["assignee_member_id"]
                or task["assignment_epoch"] != session["assignment_epoch"]
                or family["assignment_epoch"] != session["assignment_epoch"]
                or family["client_device_id"] != session["client_device_id"]
            ):
                if family is not None and family["revoked_at"] is None:
                    self._revoke_task_execution_family(
                        connection,
                        family["id"],
                        now=now,
                        reason="binding_not_current",
                    )
                error = PocketError(401, "任务执行会话凭据无效或已失效")
            elif session["revoked_at"] is not None and not (
                allow_closed_replay and session["revoke_reason"] == "access_rotated"
            ):
                error = PocketError(401, "任务执行会话凭据无效或已失效")
            else:
                principal = {
                    "auth_kind": "task_execution_session",
                    "assurance_method": "task_execution_session",
                    "workspace_id": session["workspace_id"],
                    "member_id": session["assignee_member_id"],
                    "session_id": session["id"],
                    "refresh_family_id": session["refresh_family_id"],
                    "task_id": session["task_id"],
                    "assignment_epoch": session["assignment_epoch"],
                    "device_id": session["client_device_id"],
                    "presented_token_hash": presented_hash,
                    "idempotency_actor_id": (
                        f"task-execution-family:{session['refresh_family_id']}"
                    ),
                    "replay_only": session["revoked_at"] is not None,
                }
        if error is not None:
            raise error
        if principal is None:
            raise PocketError(401, "任务执行会话凭据无效或已失效")
        return principal

    @staticmethod
    def _revoke_task_execution_family(
        connection: sqlite3.Connection,
        family_id: str,
        *,
        now: str,
        reason: str,
    ) -> bool:
        family_update = connection.execute(
            """
            UPDATE secretary_task_execution_refresh_families
            SET revoked_at = ?, revoke_reason = ?
            WHERE id = ? AND revoked_at IS NULL
            """,
            (now, reason, family_id),
        )
        connection.execute(
            """
            UPDATE secretary_task_execution_sessions
            SET revoked_at = COALESCE(revoked_at, ?),
                revoke_reason = COALESCE(revoke_reason, ?)
            WHERE refresh_family_id = ? AND revoked_at IS NULL
            """,
            (now, reason, family_id),
        )
        connection.execute(
            """
            UPDATE secretary_task_execution_refresh_tokens
            SET revoked_at = COALESCE(revoked_at, ?),
                revoke_reason = COALESCE(revoke_reason, ?)
            WHERE family_id = ? AND revoked_at IS NULL
            """,
            (now, reason, family_id),
        )
        return family_update.rowcount == 1

    @classmethod
    def _append_task_execution_security_revoked(
        cls,
        connection: sqlite3.Connection,
        *,
        family: sqlite3.Row,
        task: sqlite3.Row | None,
        token: sqlite3.Row,
        reason: str,
        session_id: str | None = None,
    ) -> None:
        if task is None:
            return
        if session_id is None:
            session = connection.execute(
                """
                SELECT id FROM secretary_task_execution_sessions
                WHERE refresh_family_id = ?
                ORDER BY access_generation DESC, created_at DESC LIMIT 1
                """,
                (family["id"],),
            ).fetchone()
            session_id = str(session["id"]) if session is not None else None
        cls._append_event(
            connection,
            workspace_id=family["workspace_id"],
            aggregate_type="task",
            aggregate_id=family["task_id"],
            aggregate_version=task["version"],
            event_type="task.execution_security_revoked",
            operation="upsert",
            actor_id=None,
            actor_type="system",
            device_id="task-execution-security",
            payload={
                "reason": reason,
                "refresh_family_id": family["id"],
                "actor_subject_type": "task_execution_capability",
                "actor_subject_id": session_id or family["id"],
                "actor_session_id": session_id,
                "on_behalf_of_member_id": family["assignee_member_id"],
                "assurance_method": "task_execution_refresh",
                "assignment_epoch": family["assignment_epoch"],
                "generation": token["generation"],
            },
        )
    @staticmethod
    def _task_execution_effective_expiry(
        task: sqlite3.Row, family: sqlite3.Row
    ) -> datetime:
        effective = parse_utc(family["absolute_expires_at"])
        if task["due_at"] is not None:
            effective = min(
                effective,
                parse_utc(task["due_at"])
                + timedelta(seconds=TASK_EXECUTION_DUE_GRACE_SECONDS),
            )
        return effective

    @staticmethod
    def _principal_can_access_task_change(
        proposal: sqlite3.Row, principal: dict[str, Any]
    ) -> bool:
        if principal.get("auth_kind") in {"owner_token", "owner_device_session"}:
            return principal.get("member_id") == proposal["proposer_member_id"]
        if principal.get("auth_kind") == "task_change_session":
            return bool(
                principal.get("change_id") == proposal["change_id"]
                and principal.get("task_id") == proposal["task_id"]
                and principal.get("member_id") == proposal["responder_member_id"]
            )
        return False

    def task_change_protocol(
        self, change_id: str, principal: dict[str, Any]
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            change = connection.execute(
                "SELECT * FROM secretary_task_changes WHERE id = ?",
                (change_id,),
            ).fetchone()
            proposal = connection.execute(
                "SELECT * FROM secretary_task_change_proposals WHERE change_id = ?",
                (change_id,),
            ).fetchone()
            if (
                change is None
                or proposal is None
                or not self._principal_can_access_task_change(proposal, principal)
            ):
                raise PocketError(404, "任务变更不存在")
            return self._task_change_protocol_dict(connection, change)

    @staticmethod
    def _principal_can_access_agreement(
        case: sqlite3.Row, principal: dict[str, Any]
    ) -> bool:
        if principal.get("auth_kind") in {"owner_token", "owner_device_session"}:
            return principal.get("member_id") == case["issuer_member_id"]
        if principal.get("auth_kind") == "task_session":
            return bool(
                principal.get("agreement_id") == case["id"]
                and principal.get("task_id") == case["task_id"]
                and principal.get("member_id") == case["assignee_member_id"]
            )
        return False

    def task_agreement_by_task(self, workspace_id: str, task_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            self._require_workspace(connection, workspace_id)
            task = connection.execute(
                """
                SELECT id FROM secretary_business_tasks
                WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                """,
                (task_id, workspace_id),
            ).fetchone()
            if task is None:
                raise PocketError(404, "任务协议不存在")
            case = connection.execute(
                """
                SELECT * FROM secretary_task_alignment_cases
                WHERE workspace_id = ? AND task_id = ?
                ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,
                         updated_at DESC, id DESC
                LIMIT 1
                """,
                (workspace_id, task_id),
            ).fetchone()
            if case is None:
                raise PocketError(404, "任务协议不存在")
            return self._agreement_dict(connection, case)

    def task_agreement(self, case_id: str, principal: dict[str, Any]) -> dict[str, Any]:
        with self.database.connect() as connection:
            case = connection.execute(
                "SELECT * FROM secretary_task_alignment_cases WHERE id = ?",
                (case_id,),
            ).fetchone()
            if case is None or not self._principal_can_access_agreement(
                case, principal
            ):
                raise PocketError(404, "任务协议不存在")
            return self._agreement_dict(connection, case)

    def exchange_task_alignment(
        self,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        invitation_id = payload["invitation_id"]
        normalized_code = _normalized_alignment_code(payload["code"])
        presented_hash = (
            _secret_hash(normalized_code) if normalized_code is not None else "0" * 64
        )
        exchange_idempotency_hash = _secret_hash(idempotency_key)
        exchange_request_hash = _hash_request(
            {
                "invitation_id": invitation_id,
                "code_hash": presented_hash,
                "client_device_id": payload["client_device_id"],
            }
        )
        now_dt = datetime.now(UTC)
        now = _iso_datetime(now_dt)
        expires_at = _iso_datetime(
            now_dt + timedelta(seconds=TASK_ACCESS_SESSION_TTL_SECONDS)
        )
        error: PocketError | None = None
        response: dict[str, Any] | None = None
        with self.database.transaction() as connection:
            invitation = connection.execute(
                """
                SELECT * FROM secretary_task_alignment_invitations WHERE id = ?
                """,
                (invitation_id,),
            ).fetchone()
            existing_session = connection.execute(
                """
                SELECT * FROM secretary_task_assignee_sessions
                WHERE invitation_id = ?
                """,
                (invitation_id,),
            ).fetchone()
            if invitation is None:
                error = PocketError(401, "任务对齐凭据无效或已失效")
            else:
                code_matches = secrets.compare_digest(
                    invitation["code_hash"], presented_hash
                )
                if not code_matches:
                    if (
                        invitation["revoked_at"] is None
                        and invitation["consumed_at"] is None
                        and invitation["code_used_at"] is None
                        and parse_utc(invitation["expires_at"]) > now_dt
                    ):
                        failed_attempts = min(
                            invitation["failed_attempts"] + 1,
                            invitation["max_attempts"],
                        )
                        revoked_at = (
                            now
                            if failed_attempts >= invitation["max_attempts"]
                            else None
                        )
                        connection.execute(
                            """
                            UPDATE secretary_task_alignment_invitations
                            SET failed_attempts = ?,
                                revoked_at = COALESCE(revoked_at, ?)
                            WHERE id = ?
                            """,
                            (failed_attempts, revoked_at, invitation_id),
                        )
                    error = PocketError(401, "任务对齐凭据无效或已失效")
                elif existing_session is not None:
                    same_idempotency = secrets.compare_digest(
                        existing_session["exchange_idempotency_hash"],
                        exchange_idempotency_hash,
                    )
                    same_request = secrets.compare_digest(
                        existing_session["exchange_request_hash"],
                        exchange_request_hash,
                    )
                    session_live = bool(
                        existing_session["revoked_at"] is None
                        and parse_utc(existing_session["expires_at"]) > now_dt
                    )
                    if existing_session["revoked_at"] is not None:
                        # A completed agreement has a terminal session reason that
                        # must remain stable so the original response can still be
                        # replayed from its idempotency record.  Exchange retries
                        # against any revoked session are therefore read-only.
                        error = PocketError(409, "任务会话已经关闭，不能再次交换")
                    elif parse_utc(existing_session["expires_at"]) <= now_dt:
                        connection.execute(
                            """
                            UPDATE secretary_task_assignee_sessions
                            SET revoked_at = COALESCE(revoked_at, ?),
                                revoke_reason = COALESCE(revoke_reason, 'expired')
                            WHERE id = ? AND revoked_at IS NULL
                            """,
                            (now, existing_session["id"]),
                        )
                        error = PocketError(409, "任务会话已经过期，不能再次交换")
                    elif same_idempotency and same_request and session_live:
                        case = connection.execute(
                            """
                            SELECT * FROM secretary_task_alignment_cases
                            WHERE id = ?
                            """,
                            (existing_session["agreement_id"],),
                        ).fetchone()
                        revision = connection.execute(
                            """
                            SELECT * FROM secretary_task_alignment_revisions
                            WHERE case_id = ? AND revision_no = ?
                            """,
                            (
                                existing_session["agreement_id"],
                                case["current_revision_no"] if case is not None else -1,
                            ),
                        ).fetchone()
                        task = connection.execute(
                            """
                            SELECT * FROM secretary_business_tasks
                            WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                            """,
                            (invitation["task_id"], invitation["workspace_id"]),
                        ).fetchone()
                        case_accessible = bool(
                            case is not None
                            and revision is not None
                            and case["id"] == invitation["alignment_case_id"]
                            and case["id"] == existing_session["agreement_id"]
                            and case["task_id"] == existing_session["task_id"]
                            and case["workspace_id"] == existing_session["workspace_id"]
                            and case["assignee_member_id"]
                            == existing_session["assignee_member_id"]
                            and revision["case_id"] == case["id"]
                            and revision["revision_no"] == case["current_revision_no"]
                            and self._alignment_case_is_current(
                                connection, case, revision, task
                            )
                        )
                        if not case_accessible:
                            connection.execute(
                                """
                                UPDATE secretary_task_assignee_sessions
                                SET revoked_at = COALESCE(revoked_at, ?),
                                    revoke_reason = 'agreement_not_current'
                                WHERE id = ?
                                """,
                                (now, existing_session["id"]),
                            )
                            error = PocketError(409, "任务协议已变化，不能重放会话交换")
                        else:
                            assert case is not None and revision is not None
                            access_token = self._task_session_access_token(
                                session_id=existing_session["id"],
                                exchange_idempotency_hash=existing_session[
                                    "exchange_idempotency_hash"
                                ],
                                exchange_request_hash=existing_session[
                                    "exchange_request_hash"
                                ],
                            )
                            expected_token_hash = _secret_hash(access_token)
                            if not secrets.compare_digest(
                                existing_session["token_hash"], expected_token_hash
                            ):
                                connection.execute(
                                    """
                                    UPDATE secretary_task_assignee_sessions
                                    SET revoked_at = COALESCE(revoked_at, ?),
                                        revoke_reason = COALESCE(
                                            revoke_reason,
                                            'session_token_integrity_failure'
                                        )
                                    WHERE id = ? AND revoked_at IS NULL
                                    """,
                                    (now, existing_session["id"]),
                                )
                                connection.execute(
                                    """
                                    UPDATE secretary_task_alignment_invitations
                                    SET revoked_at = COALESCE(revoked_at, ?)
                                    WHERE id = ?
                                    """,
                                    (now, invitation_id),
                                )
                                error = PocketError(
                                    409, "任务会话完整性校验失败，请创建新邀请"
                                )
                            else:
                                session_projection = {
                                    "id": existing_session["id"],
                                    "task_id": existing_session["task_id"],
                                    "agreement_id": existing_session["agreement_id"],
                                    "assignee_member_id": existing_session[
                                        "assignee_member_id"
                                    ],
                                    "client_device_id": existing_session[
                                        "client_device_id"
                                    ],
                                    "assurance_method": ("dual_channel_capability"),
                                    "expires_at": existing_session["expires_at"],
                                }
                                agreement = self._agreement_dict(connection, case)
                                response = {
                                    "token_type": "Bearer",
                                    "access_token": access_token,
                                    "expires_at": existing_session["expires_at"],
                                    "session": session_projection,
                                    "agreement": agreement,
                                }
                    else:
                        # Correct dual-channel credentials with a different
                        # request are a conflict, not a second session grant.
                        connection.execute(
                            """
                            UPDATE secretary_task_assignee_sessions
                            SET revoked_at = COALESCE(revoked_at, ?),
                                revoke_reason = COALESCE(
                                    revoke_reason, 'unsafe_exchange_replay'
                                )
                            WHERE id = ? AND revoked_at IS NULL
                            """,
                            (now, existing_session["id"]),
                        )
                        connection.execute(
                            """
                            UPDATE secretary_task_alignment_invitations
                            SET revoked_at = COALESCE(revoked_at, ?)
                            WHERE id = ?
                            """,
                            (now, invitation_id),
                        )
                        error = PocketError(
                            409,
                            "令牌交换请求冲突；旧会话已撤销，请创建新邀请",
                        )
                elif (
                    invitation["alignment_case_id"] is None
                    or invitation["alignment_revision_id"] is None
                    or invitation["alignment_revision_digest"] is None
                ):
                    error = PocketError(
                        409, "旧版邀请不能交换任务会话，请创建新的对齐邀请"
                    )
                elif (
                    invitation["revoked_at"] is not None
                    or invitation["consumed_at"] is not None
                    or invitation["code_used_at"] is not None
                    or invitation["failed_attempts"] >= invitation["max_attempts"]
                    or parse_utc(invitation["expires_at"]) <= now_dt
                ):
                    error = PocketError(401, "任务对齐凭据无效或已失效")
                else:
                    case = connection.execute(
                        """
                        SELECT * FROM secretary_task_alignment_cases WHERE id = ?
                        """,
                        (invitation["alignment_case_id"],),
                    ).fetchone()
                    revision = connection.execute(
                        """
                        SELECT * FROM secretary_task_alignment_revisions WHERE id = ?
                        """,
                        (invitation["alignment_revision_id"],),
                    ).fetchone()
                    task = connection.execute(
                        """
                        SELECT * FROM secretary_business_tasks
                        WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                        """,
                        (invitation["task_id"], invitation["workspace_id"]),
                    ).fetchone()
                    binding_current = bool(
                        case is not None
                        and revision is not None
                        and revision["case_id"] == case["id"]
                        and revision["digest"]
                        == invitation["alignment_revision_digest"]
                        and revision["revision_no"] == case["current_revision_no"]
                        and revision["required_responder_role"] == "assignee"
                        and revision["required_responder_member_id"]
                        == invitation["assignee_member_id"]
                        and self._alignment_case_is_current(
                            connection, case, revision, task
                        )
                    )
                    if not binding_current:
                        connection.execute(
                            """
                            UPDATE secretary_task_alignment_invitations
                            SET revoked_at = COALESCE(revoked_at, ?)
                            WHERE id = ?
                            """,
                            (now, invitation_id),
                        )
                        if case is not None and case["status"] == "pending":
                            self._mark_alignment_case_stale(
                                connection,
                                case,
                                now=now,
                                device_id=payload["client_device_id"],
                            )
                        error = PocketError(
                            409,
                            "任务或成员绑定已变化；旧协议已标记失效，请创建新邀请",
                        )
                    else:
                        assert case is not None and revision is not None
                        session_id = new_id("task_session")
                        access_token = self._task_session_access_token(
                            session_id=session_id,
                            exchange_idempotency_hash=exchange_idempotency_hash,
                            exchange_request_hash=exchange_request_hash,
                        )
                        connection.execute(
                            """
                            INSERT INTO secretary_task_assignee_sessions(
                                id, workspace_id, task_id, agreement_id,
                                invitation_id, assignee_member_id, token_hash,
                                client_device_id, exchange_idempotency_hash,
                                exchange_request_hash, assurance_method,
                                created_at, expires_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                      'dual_channel_capability', ?, ?)
                            """,
                            (
                                session_id,
                                invitation["workspace_id"],
                                invitation["task_id"],
                                case["id"],
                                invitation_id,
                                invitation["assignee_member_id"],
                                _secret_hash(access_token),
                                payload["client_device_id"],
                                exchange_idempotency_hash,
                                exchange_request_hash,
                                now,
                                expires_at,
                            ),
                        )
                        consumed = connection.execute(
                            """
                            UPDATE secretary_task_alignment_invitations
                            SET code_used_at = ?, consumed_at = ?
                            WHERE id = ? AND code_used_at IS NULL
                              AND consumed_at IS NULL AND revoked_at IS NULL
                            """,
                            (now, now, invitation_id),
                        )
                        if consumed.rowcount != 1:
                            raise PocketError(409, "任务对齐邀请状态已变化")
                        session_projection = {
                            "id": session_id,
                            "task_id": invitation["task_id"],
                            "agreement_id": case["id"],
                            "assignee_member_id": invitation["assignee_member_id"],
                            "client_device_id": payload["client_device_id"],
                            "assurance_method": "dual_channel_capability",
                            "expires_at": expires_at,
                        }
                        agreement = self._agreement_dict(connection, case)
                        self._append_event(
                            connection,
                            workspace_id=invitation["workspace_id"],
                            aggregate_type="task_agreement",
                            aggregate_id=case["id"],
                            aggregate_version=case["version"],
                            event_type="task.agreement_session_issued",
                            operation="upsert",
                            actor_id=invitation["assignee_member_id"],
                            actor_type="member",
                            device_id=payload["client_device_id"],
                            payload={
                                "session": session_projection,
                                "revision_digest": revision["digest"],
                            },
                        )
                        response = {
                            "token_type": "Bearer",
                            "access_token": access_token,
                            "expires_at": expires_at,
                            "session": session_projection,
                            "agreement": agreement,
                        }
        if error is not None:
            raise error
        if response is None:
            raise PocketError(503, "暂时无法交换任务会话")
        return response

    @staticmethod
    def _counter_document(
        supplied: dict[str, Any],
        *,
        case: sqlite3.Row,
        current_revision: sqlite3.Row,
        current_document: dict[str, Any],
        actor_role: str,
        actor_member_id: str,
    ) -> dict[str, Any]:
        canonical_json, _digest = _task_agreement_digest(supplied)
        document = json_loads(canonical_json, None)
        if not isinstance(document, dict):
            raise PocketError(422, "反提案文档无效")
        responder_role = "assignee" if actor_role == "issuer" else "issuer"
        responder_member_id = (
            case["assignee_member_id"]
            if responder_role == "assignee"
            else case["issuer_member_id"]
        )
        expected_envelope = {
            "schema": TASK_AGREEMENT_SCHEMA,
            "workspace_id": case["workspace_id"],
            "task_id": case["task_id"],
            "agreement_id": case["id"],
            "revision_no": current_revision["revision_no"] + 1,
            "parent_digest": current_revision["digest"],
            "proposer_role": actor_role,
            "proposer_member_id": actor_member_id,
            "responder_role": responder_role,
            "responder_member_id": responder_member_id,
        }
        if any(document.get(key) != value for key, value in expected_envelope.items()):
            raise PocketError(422, "反提案文档的修订信封与当前协议不一致")
        for fixed_field in (
            "issuer_member_id",
            "assignee_member_id",
            "acceptance_owner_id",
            "domain",
            "tier",
            "priority",
        ):
            if document.get(fixed_field) != current_document.get(fixed_field):
                raise PocketError(422, f"反提案不能改变固定字段：{fixed_field}")
        return document

    def respond_task_agreement(
        self,
        case_id: str,
        payload: dict[str, Any],
        principal: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"task_agreement.respond:{case_id}"
        request_payload = {"case_id": case_id, **payload}
        now = _iso_datetime(datetime.now(UTC))
        error: PocketError | None = None
        result: dict[str, Any] | None = None
        with self.database.transaction() as connection:
            case = connection.execute(
                "SELECT * FROM secretary_task_alignment_cases WHERE id = ?",
                (case_id,),
            ).fetchone()
            if case is None or not self._principal_can_access_agreement(
                case, principal
            ):
                raise PocketError(404, "任务协议不存在")
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=case["workspace_id"],
                actor_id=principal["idempotency_actor_id"],
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            if principal.get("auth_kind") == "task_session":
                session = connection.execute(
                    """
                    SELECT * FROM secretary_task_assignee_sessions
                    WHERE id = ?
                    """,
                    (principal.get("session_id"),),
                ).fetchone()
                session_current = bool(
                    session is not None
                    and session["agreement_id"] == case["id"]
                    and session["task_id"] == case["task_id"]
                    and session["assignee_member_id"] == principal.get("member_id")
                    and secrets.compare_digest(
                        session["token_hash"],
                        principal.get("presented_token_hash", ""),
                    )
                    and secrets.compare_digest(
                        session["client_device_id"].encode("utf-8"),
                        device_id.encode("utf-8"),
                    )
                    and session["revoked_at"] is None
                    and parse_utc(session["expires_at"]) > datetime.now(UTC)
                )
                if not session_current:
                    raise PocketError(401, "任务会话凭据无效或已失效")
            if principal.get("replay_only"):
                raise PocketError(401, "任务会话已经撤销")
            if case["status"] != "pending":
                raise PocketError(409, "任务协议已经关闭")
            self._require_version(case, payload["expected_agreement_version"])
            revision = connection.execute(
                """
                SELECT * FROM secretary_task_alignment_revisions
                WHERE case_id = ? AND revision_no = ?
                """,
                (case_id, case["current_revision_no"]),
            ).fetchone()
            if revision is None:
                raise PocketError(409, "任务协议当前修订不存在")
            if revision["id"] != payload["revision_id"] or not secrets.compare_digest(
                revision["digest"], payload["expected_digest"]
            ):
                raise PocketError(409, "任务协议修订或摘要已经变化")
            current_document = self._agreement_revision_document(revision)
            actor_role = (
                "issuer"
                if principal["member_id"] == case["issuer_member_id"]
                else "assignee"
            )
            if (
                revision["required_responder_role"] != actor_role
                or revision["required_responder_member_id"] != principal["member_id"]
            ):
                raise PocketError(403, "只有当前协议回应方可以提交决定")
            if (
                revision["proposed_by_role"] == actor_role
                or revision["proposed_by_member_id"] == principal["member_id"]
            ):
                raise PocketError(403, "提议方不能决定自己的协议修订")
            duplicate_mutation = connection.execute(
                """
                SELECT id FROM secretary_task_alignment_decisions
                WHERE case_id = ? AND client_mutation_id = ?
                """,
                (case_id, payload["client_mutation_id"]),
            ).fetchone()
            if duplicate_mutation is not None:
                raise PocketError(409, "client_mutation_id 已用于其他请求")
            task = connection.execute(
                """
                SELECT * FROM secretary_business_tasks
                WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                """,
                (case["task_id"], case["workspace_id"]),
            ).fetchone()
            if not self._alignment_case_is_current(connection, case, revision, task):
                self._mark_alignment_case_stale(
                    connection, case, now=now, device_id=device_id
                )
                error = PocketError(409, "任务或成员绑定已变化；协议已标记失效")
            else:
                assert task is not None
                action = payload["action"]
                counter_revision: sqlite3.Row | None = None
                if action == "counter":
                    counter_document = self._counter_document(
                        payload["counter_document"],
                        case=case,
                        current_revision=revision,
                        current_document=current_document,
                        actor_role=actor_role,
                        actor_member_id=principal["member_id"],
                    )
                    counter_revision = self._insert_alignment_revision(
                        connection,
                        case_id=case_id,
                        revision_id=new_id("agreement_revision"),
                        revision_no=revision["revision_no"] + 1,
                        parent_revision_id=revision["id"],
                        base_task_version=revision["base_task_version"],
                        proposer_role=actor_role,
                        proposer_member_id=principal["member_id"],
                        responder_role=counter_document["responder_role"],
                        responder_member_id=counter_document["responder_member_id"],
                        document=counter_document,
                        reason=payload["reason"],
                        now=now,
                    )

                decision_id = new_id("agreement_decision")
                connection.execute(
                    """
                    INSERT INTO secretary_task_alignment_decisions(
                        id, case_id, revision_id, revision_digest, action,
                        actor_role, actor_member_id, actor_session_id,
                        assurance_method, reason, counter_revision_id,
                        client_mutation_id, version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        decision_id,
                        case_id,
                        revision["id"],
                        revision["digest"],
                        action,
                        actor_role,
                        principal["member_id"],
                        principal.get("session_id"),
                        principal["assurance_method"],
                        payload.get("reason"),
                        counter_revision["id"]
                        if counter_revision is not None
                        else None,
                        payload["client_mutation_id"],
                        now,
                    ),
                )

                task_result: dict[str, Any] | None = None
                if action == "accept":
                    updated_task = connection.execute(
                        """
                        UPDATE secretary_business_tasks
                        SET title = ?, purpose = ?, objective = ?, strategy = ?,
                            key_points_json = ?, acceptance_criteria_json = ?,
                            due_at = ?, domain = ?, tier = ?, priority = ?,
                            issuer_member_id = ?, assignee_member_id = ?,
                            acceptance_owner_id = ?, stage = 'aligned',
                            version = version + 1, updated_by = ?, updated_at = ?
                        WHERE id = ? AND workspace_id = ? AND version = ?
                          AND stage = 'issued'
                    """,
                        (
                            current_document["title"],
                            current_document["purpose"],
                            current_document["objective"],
                            current_document["strategy"],
                            _json(current_document["key_points"]),
                            _json(current_document["acceptance_criteria"]),
                            current_document["due_at"],
                            current_document["domain"],
                            current_document["tier"],
                            current_document["priority"],
                            current_document["issuer_member_id"],
                            current_document["assignee_member_id"],
                            current_document["acceptance_owner_id"],
                            principal["member_id"],
                            now,
                            task["id"],
                            case["workspace_id"],
                            task["version"],
                        ),
                    )
                    if updated_task.rowcount != 1:
                        raise PocketError(409, "任务状态已变化，请重新同步")
                    updated_case = connection.execute(
                        """
                        UPDATE secretary_task_alignment_cases
                        SET status = 'accepted', accepted_revision_no = ?,
                            version = version + 1, updated_at = ?, closed_at = ?
                        WHERE id = ? AND status = 'pending' AND version = ?
                          AND current_revision_no = ?
                        """,
                        (
                            revision["revision_no"],
                            now,
                            now,
                            case_id,
                            case["version"],
                            revision["revision_no"],
                        ),
                    )
                    if updated_case.rowcount != 1:
                        raise PocketError(409, "任务协议状态已变化，请重新同步")
                    connection.execute(
                        """
                        UPDATE secretary_task_alignment_invitations
                        SET revoked_at = COALESCE(revoked_at, ?)
                        WHERE alignment_case_id = ? AND revoked_at IS NULL
                        """,
                        (now, case_id),
                    )
                    connection.execute(
                        """
                        UPDATE secretary_task_assignee_sessions
                        SET revoked_at = COALESCE(revoked_at, ?),
                            revoke_reason = 'agreement_accepted'
                        WHERE agreement_id = ? AND revoked_at IS NULL
                        """,
                        (now, case_id),
                    )
                    task = connection.execute(
                        "SELECT * FROM secretary_business_tasks WHERE id = ?",
                        (task["id"],),
                    ).fetchone()
                    assert task is not None
                    task_result = {
                        "id": task["id"],
                        "stage": task["stage"],
                        "version": task["version"],
                        "updated_at": task["updated_at"],
                    }
                    self._append_event(
                        connection,
                        workspace_id=case["workspace_id"],
                        aggregate_type="task",
                        aggregate_id=task["id"],
                        aggregate_version=task["version"],
                        event_type="task.aligned_by_agreement",
                        operation="upsert",
                        actor_id=principal["member_id"],
                        actor_type=("owner" if actor_role == "issuer" else "member"),
                        device_id=device_id,
                        payload=self._task_dict(connection, task),
                    )
                elif action == "reject":
                    updated_case = connection.execute(
                        """
                        UPDATE secretary_task_alignment_cases
                        SET status = 'rejected', version = version + 1,
                            updated_at = ?, closed_at = ?
                        WHERE id = ? AND status = 'pending' AND version = ?
                          AND current_revision_no = ?
                        """,
                        (
                            now,
                            now,
                            case_id,
                            case["version"],
                            revision["revision_no"],
                        ),
                    )
                    if updated_case.rowcount != 1:
                        raise PocketError(409, "任务协议状态已变化，请重新同步")
                    connection.execute(
                        """
                        UPDATE secretary_task_alignment_invitations
                        SET revoked_at = COALESCE(revoked_at, ?)
                        WHERE alignment_case_id = ? AND revoked_at IS NULL
                        """,
                        (now, case_id),
                    )
                    connection.execute(
                        """
                        UPDATE secretary_task_assignee_sessions
                        SET revoked_at = COALESCE(revoked_at, ?),
                            revoke_reason = 'agreement_rejected'
                        WHERE agreement_id = ? AND revoked_at IS NULL
                        """,
                        (now, case_id),
                    )
                else:
                    assert counter_revision is not None
                    updated_case = connection.execute(
                        """
                        UPDATE secretary_task_alignment_cases
                        SET current_revision_no = ?, version = version + 1,
                            updated_at = ?
                        WHERE id = ? AND status = 'pending' AND version = ?
                          AND current_revision_no = ?
                        """,
                        (
                            counter_revision["revision_no"],
                            now,
                            case_id,
                            case["version"],
                            revision["revision_no"],
                        ),
                    )
                    if updated_case.rowcount != 1:
                        raise PocketError(409, "任务协议状态已变化，请重新同步")
                    connection.execute(
                        """
                        UPDATE secretary_task_alignment_invitations
                        SET revoked_at = COALESCE(revoked_at, ?)
                        WHERE alignment_case_id = ? AND revoked_at IS NULL
                          AND alignment_revision_id <> ?
                        """,
                        (now, case_id, counter_revision["id"]),
                    )

                case = connection.execute(
                    "SELECT * FROM secretary_task_alignment_cases WHERE id = ?",
                    (case_id,),
                ).fetchone()
                decision = connection.execute(
                    """
                    SELECT * FROM secretary_task_alignment_decisions WHERE id = ?
                    """,
                    (decision_id,),
                ).fetchone()
                assert case is not None and decision is not None
                agreement = self._agreement_dict(connection, case)
                decision_projection = self._agreement_decision_dict(decision)
                result = {
                    "agreement": agreement,
                    "decision": decision_projection,
                    "task": task_result,
                }
                self._append_event(
                    connection,
                    workspace_id=case["workspace_id"],
                    aggregate_type="task_agreement",
                    aggregate_id=case_id,
                    aggregate_version=case["version"],
                    event_type=f"task.agreement_{action}",
                    operation="upsert",
                    actor_id=principal["member_id"],
                    actor_type=("owner" if actor_role == "issuer" else "member"),
                    device_id=device_id,
                    payload=result,
                    occurred_at=now,
                )
                self._store_idempotent_response(
                    connection,
                    workspace_id=case["workspace_id"],
                    actor_id=principal["idempotency_actor_id"],
                    operation=operation,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    response=result,
                )
        if error is not None:
            raise error
        if result is None:
            raise PocketError(503, "暂时无法提交任务协议决定")
        return result

    def preview_task_alignment(self, invitation_id: str, code: str) -> dict[str, Any]:
        normalized_code = _normalized_alignment_code(code)
        presented_hash = (
            _secret_hash(normalized_code) if normalized_code is not None else "0" * 64
        )
        now_dt = datetime.now(UTC)
        now = _iso_datetime(now_dt)
        error: PocketError | None = None
        response: dict[str, Any] | None = None

        with self.database.transaction() as connection:
            invitation = connection.execute(
                """
                SELECT * FROM secretary_task_alignment_invitations WHERE id = ?
                """,
                (invitation_id,),
            ).fetchone()
            if invitation is None:
                error = PocketError(401, "任务对齐凭据无效或已失效")
            else:
                code_matches = secrets.compare_digest(
                    invitation["code_hash"], presented_hash
                )
                if not code_matches:
                    if (
                        invitation["revoked_at"] is None
                        and invitation["consumed_at"] is None
                        and invitation["code_used_at"] is None
                        and parse_utc(invitation["expires_at"]) > now_dt
                    ):
                        failed_attempts = min(
                            invitation["failed_attempts"] + 1,
                            invitation["max_attempts"],
                        )
                        revoked_at = (
                            now
                            if failed_attempts >= invitation["max_attempts"]
                            else None
                        )
                        connection.execute(
                            """
                            UPDATE secretary_task_alignment_invitations
                            SET failed_attempts = ?, revoked_at = COALESCE(revoked_at, ?)
                            WHERE id = ?
                            """,
                            (failed_attempts, revoked_at, invitation_id),
                        )
                    error = PocketError(401, "任务对齐凭据无效或已失效")
                elif invitation["code_used_at"] is not None:
                    error = PocketError(409, "任务对齐确认码已使用")
                elif (
                    invitation["revoked_at"] is not None
                    or invitation["consumed_at"] is not None
                    or invitation["failed_attempts"] >= invitation["max_attempts"]
                    or parse_utc(invitation["expires_at"]) <= now_dt
                ):
                    connection.execute(
                        """
                        UPDATE secretary_task_alignment_invitations
                        SET revoked_at = COALESCE(revoked_at, ?)
                        WHERE id = ?
                        """,
                        (now, invitation_id),
                    )
                    error = PocketError(401, "任务对齐凭据无效或已失效")
                else:
                    task = connection.execute(
                        """
                        SELECT * FROM secretary_business_tasks
                        WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                        """,
                        (invitation["task_id"], invitation["workspace_id"]),
                    ).fetchone()
                    member = connection.execute(
                        """
                        SELECT display_name, active
                        FROM secretary_workspace_members
                        WHERE id = ? AND workspace_id = ?
                        """,
                        (
                            invitation["assignee_member_id"],
                            invitation["workspace_id"],
                        ),
                    ).fetchone()
                    alignment_document: dict[str, Any] | None = None
                    binding_changed = (
                        task is None
                        or member is None
                        or not bool(member["active"])
                        or (
                            invitation["alignment_case_id"] is None
                            and task["version"] != invitation["task_version"]
                        )
                        or task["stage"] != "issued"
                        or task["assignee_member_id"]
                        != invitation["assignee_member_id"]
                    )
                    if (
                        not binding_changed
                        and invitation["alignment_case_id"] is not None
                    ):
                        alignment_case = connection.execute(
                            """
                            SELECT * FROM secretary_task_alignment_cases
                            WHERE id = ?
                            """,
                            (invitation["alignment_case_id"],),
                        ).fetchone()
                        revision = connection.execute(
                            """
                            SELECT * FROM secretary_task_alignment_revisions
                            WHERE id = ?
                            """,
                            (invitation["alignment_revision_id"],),
                        ).fetchone()
                        linked_current = bool(
                            alignment_case is not None
                            and revision is not None
                            and revision["case_id"] == alignment_case["id"]
                            and revision["digest"]
                            == invitation["alignment_revision_digest"]
                            and alignment_case["current_revision_no"]
                            == revision["revision_no"]
                            and revision["required_responder_role"] == "assignee"
                            and revision["required_responder_member_id"]
                            == invitation["assignee_member_id"]
                        )
                        if linked_current and self._alignment_case_is_current(
                            connection, alignment_case, revision, task
                        ):
                            alignment_document = self._agreement_revision_document(
                                revision
                            )
                        else:
                            if (
                                linked_current
                                and alignment_case is not None
                                and alignment_case["status"] == "pending"
                            ):
                                self._mark_alignment_case_stale(
                                    connection,
                                    alignment_case,
                                    now=now,
                                    device_id=f"alignment:{invitation_id}",
                                )
                            binding_changed = True
                    if binding_changed:
                        connection.execute(
                            """
                            UPDATE secretary_task_alignment_invitations
                            SET revoked_at = COALESCE(revoked_at, ?)
                            WHERE id = ?
                            """,
                            (now, invitation_id),
                        )
                        error = PocketError(
                            409,
                            "任务内容或承办人已变化，请让下达人重新创建对齐邀请",
                        )
                    else:
                        confirmation_expires_dt = min(
                            parse_utc(invitation["expires_at"]),
                            now_dt + timedelta(minutes=5),
                        )
                        confirmation_expires_at = _iso_datetime(confirmation_expires_dt)
                        for _attempt in range(5):
                            confirmation_token = (
                                f"cp_align_confirm_{secrets.token_urlsafe(32)}"
                            )
                            try:
                                updated = connection.execute(
                                    """
                                    UPDATE secretary_task_alignment_invitations
                                    SET code_used_at = ?, confirmation_token_hash = ?,
                                        confirmation_expires_at = ?
                                    WHERE id = ? AND code_used_at IS NULL
                                      AND revoked_at IS NULL AND consumed_at IS NULL
                                    """,
                                    (
                                        now,
                                        _secret_hash(confirmation_token),
                                        confirmation_expires_at,
                                        invitation_id,
                                    ),
                                )
                            except sqlite3.IntegrityError:
                                continue
                            if updated.rowcount == 1:
                                break
                        else:
                            error = PocketError(503, "暂时无法创建任务对齐确认会话")
                        if error is None:
                            frozen = alignment_document or {
                                "title": task["title"],
                                "purpose": task["purpose"],
                                "objective": task["objective"],
                                "strategy": task["strategy"],
                                "key_points": json_loads(task["key_points_json"], []),
                                "acceptance_criteria": json_loads(
                                    task["acceptance_criteria_json"], []
                                ),
                                "due_at": task["due_at"],
                            }
                            response = {
                                "invitation_id": invitation_id,
                                "confirmation_token": confirmation_token,
                                "confirmation_expires_at": confirmation_expires_at,
                                "alignment": {
                                    "task_id": task["id"],
                                    "task_version": task["version"],
                                    "assignee_member_id": invitation[
                                        "assignee_member_id"
                                    ],
                                    "assignee_label": member["display_name"],
                                    "title": frozen["title"],
                                    "purpose": frozen["purpose"],
                                    "objective": frozen["objective"],
                                    "strategy": frozen["strategy"],
                                    "key_points": frozen["key_points"],
                                    "acceptance_criteria": frozen[
                                        "acceptance_criteria"
                                    ],
                                    "due_at": frozen["due_at"],
                                },
                            }

        if error is not None:
            raise error
        if response is None:
            raise PocketError(503, "暂时无法预览任务对齐内容")
        return response

    def confirm_task_alignment(
        self, invitation_id: str, confirmation_token: str
    ) -> dict[str, Any]:
        presented_hash = _secret_hash(confirmation_token)
        now_dt = datetime.now(UTC)
        now = _iso_datetime(now_dt)
        error: PocketError | None = None
        result: dict[str, Any] | None = None

        with self.database.transaction() as connection:
            invitation = connection.execute(
                """
                SELECT * FROM secretary_task_alignment_invitations WHERE id = ?
                """,
                (invitation_id,),
            ).fetchone()
            token_matches = bool(
                invitation is not None
                and invitation["confirmation_token_hash"] is not None
                and secrets.compare_digest(
                    invitation["confirmation_token_hash"], presented_hash
                )
            )
            if not token_matches:
                error = PocketError(401, "任务对齐确认凭据无效或已失效")
            elif (
                invitation["consumed_at"] is not None
                or invitation["confirmation_consumed_at"] is not None
            ):
                error = PocketError(409, "任务对齐确认凭据已使用")
            elif (
                invitation["revoked_at"] is not None
                or invitation["confirmation_expires_at"] is None
                or parse_utc(invitation["confirmation_expires_at"]) <= now_dt
                or parse_utc(invitation["expires_at"]) <= now_dt
            ):
                connection.execute(
                    """
                    UPDATE secretary_task_alignment_invitations
                    SET revoked_at = COALESCE(revoked_at, ?)
                    WHERE id = ?
                    """,
                    (now, invitation_id),
                )
                error = PocketError(401, "任务对齐确认凭据无效或已失效")
            else:
                task = connection.execute(
                    """
                    SELECT * FROM secretary_business_tasks
                    WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                    """,
                    (invitation["task_id"], invitation["workspace_id"]),
                ).fetchone()
                member = connection.execute(
                    """
                    SELECT display_name, active FROM secretary_workspace_members
                    WHERE id = ? AND workspace_id = ?
                    """,
                    (
                        invitation["assignee_member_id"],
                        invitation["workspace_id"],
                    ),
                ).fetchone()
                alignment_case: sqlite3.Row | None = None
                alignment_revision: sqlite3.Row | None = None
                alignment_document: dict[str, Any] | None = None
                binding_changed = (
                    task is None
                    or member is None
                    or not bool(member["active"])
                    or (
                        invitation["alignment_case_id"] is None
                        and task["version"] != invitation["task_version"]
                    )
                    or task["stage"] != "issued"
                    or task["assignee_member_id"] != invitation["assignee_member_id"]
                )
                if not binding_changed and invitation["alignment_case_id"] is not None:
                    alignment_case = connection.execute(
                        """
                        SELECT * FROM secretary_task_alignment_cases WHERE id = ?
                        """,
                        (invitation["alignment_case_id"],),
                    ).fetchone()
                    alignment_revision = connection.execute(
                        """
                        SELECT * FROM secretary_task_alignment_revisions WHERE id = ?
                        """,
                        (invitation["alignment_revision_id"],),
                    ).fetchone()
                    linked_current = bool(
                        alignment_case is not None
                        and alignment_revision is not None
                        and alignment_revision["case_id"] == alignment_case["id"]
                        and alignment_revision["digest"]
                        == invitation["alignment_revision_digest"]
                        and alignment_revision["revision_no"]
                        == alignment_case["current_revision_no"]
                        and alignment_revision["required_responder_role"] == "assignee"
                        and alignment_revision["required_responder_member_id"]
                        == invitation["assignee_member_id"]
                        and alignment_revision["proposed_by_member_id"]
                        != invitation["assignee_member_id"]
                        and self._alignment_case_is_current(
                            connection,
                            alignment_case,
                            alignment_revision,
                            task,
                        )
                    )
                    if linked_current:
                        alignment_document = self._agreement_revision_document(
                            alignment_revision
                        )
                    else:
                        if (
                            alignment_case is not None
                            and alignment_case["status"] == "pending"
                            and alignment_revision is not None
                            and alignment_revision["revision_no"]
                            == alignment_case["current_revision_no"]
                        ):
                            self._mark_alignment_case_stale(
                                connection,
                                alignment_case,
                                now=now,
                                device_id=f"alignment:{invitation_id}",
                            )
                        binding_changed = True
                if binding_changed:
                    connection.execute(
                        """
                        UPDATE secretary_task_alignment_invitations
                        SET revoked_at = COALESCE(revoked_at, ?)
                        WHERE id = ?
                        """,
                        (now, invitation_id),
                    )
                    error = PocketError(
                        409,
                        "任务内容或承办人已变化，请让下达人重新创建对齐邀请",
                    )
                else:
                    if alignment_document is None:
                        updated = connection.execute(
                            """
                            UPDATE secretary_business_tasks
                            SET stage = 'aligned', version = version + 1,
                                updated_by = ?, updated_at = ?
                            WHERE id = ? AND workspace_id = ? AND version = ?
                              AND stage = 'issued' AND assignee_member_id = ?
                            """,
                            (
                                invitation["assignee_member_id"],
                                now,
                                task["id"],
                                invitation["workspace_id"],
                                invitation["task_version"],
                                invitation["assignee_member_id"],
                            ),
                        )
                    else:
                        updated = connection.execute(
                            """
                            UPDATE secretary_business_tasks
                            SET title = ?, purpose = ?, objective = ?, strategy = ?,
                                key_points_json = ?, acceptance_criteria_json = ?,
                                due_at = ?, domain = ?, tier = ?, priority = ?,
                                issuer_member_id = ?, assignee_member_id = ?,
                                acceptance_owner_id = ?, stage = 'aligned',
                                version = version + 1, updated_by = ?, updated_at = ?
                            WHERE id = ? AND workspace_id = ? AND version = ?
                              AND stage = 'issued' AND assignee_member_id = ?
                            """,
                            (
                                alignment_document["title"],
                                alignment_document["purpose"],
                                alignment_document["objective"],
                                alignment_document["strategy"],
                                _json(alignment_document["key_points"]),
                                _json(alignment_document["acceptance_criteria"]),
                                alignment_document["due_at"],
                                alignment_document["domain"],
                                alignment_document["tier"],
                                alignment_document["priority"],
                                alignment_document["issuer_member_id"],
                                alignment_document["assignee_member_id"],
                                alignment_document["acceptance_owner_id"],
                                invitation["assignee_member_id"],
                                now,
                                task["id"],
                                invitation["workspace_id"],
                                task["version"],
                                invitation["assignee_member_id"],
                            ),
                        )
                    consumed = connection.execute(
                        """
                        UPDATE secretary_task_alignment_invitations
                        SET consumed_at = ?, confirmation_consumed_at = ?,
                            confirmed_by_member_id = ?, confirmed_at = ?
                        WHERE id = ? AND consumed_at IS NULL
                          AND confirmation_consumed_at IS NULL
                        """,
                        (
                            now,
                            now,
                            invitation["assignee_member_id"],
                            now,
                            invitation_id,
                        ),
                    )
                    if updated.rowcount != 1 or consumed.rowcount != 1:
                        raise PocketError(409, "任务对齐状态已变化，请重新同步")
                    if alignment_case is not None and alignment_revision is not None:
                        decision_id = new_id("agreement_decision")
                        connection.execute(
                            """
                            INSERT INTO secretary_task_alignment_decisions(
                                id, case_id, revision_id, revision_digest,
                                action, actor_role, actor_member_id,
                                actor_session_id, assurance_method, reason,
                                counter_revision_id, client_mutation_id,
                                version, created_at
                            ) VALUES (?, ?, ?, ?, 'accept', 'assignee', ?, NULL,
                                      'dual_channel_capability', NULL, NULL, ?, 1, ?)
                            """,
                            (
                                decision_id,
                                alignment_case["id"],
                                alignment_revision["id"],
                                alignment_revision["digest"],
                                invitation["assignee_member_id"],
                                f"legacy-confirm:{invitation_id}",
                                now,
                            ),
                        )
                        case_updated = connection.execute(
                            """
                            UPDATE secretary_task_alignment_cases
                            SET status = 'accepted', accepted_revision_no = ?,
                                version = version + 1, updated_at = ?, closed_at = ?
                            WHERE id = ? AND status = 'pending' AND version = ?
                              AND current_revision_no = ?
                            """,
                            (
                                alignment_revision["revision_no"],
                                now,
                                now,
                                alignment_case["id"],
                                alignment_case["version"],
                                alignment_revision["revision_no"],
                            ),
                        )
                        if case_updated.rowcount != 1:
                            raise PocketError(409, "任务协议状态已变化，请重新同步")
                        connection.execute(
                            """
                            UPDATE secretary_task_alignment_invitations
                            SET revoked_at = COALESCE(revoked_at, ?)
                            WHERE alignment_case_id = ? AND revoked_at IS NULL
                            """,
                            (now, alignment_case["id"]),
                        )
                        connection.execute(
                            """
                            UPDATE secretary_task_assignee_sessions
                            SET revoked_at = COALESCE(revoked_at, ?),
                                revoke_reason = 'agreement_accepted'
                            WHERE agreement_id = ? AND revoked_at IS NULL
                            """,
                            (now, alignment_case["id"]),
                        )
                    task = connection.execute(
                        "SELECT * FROM secretary_business_tasks WHERE id = ?",
                        (task["id"],),
                    ).fetchone()
                    task_payload = self._task_dict(connection, task)
                    self._append_event(
                        connection,
                        workspace_id=invitation["workspace_id"],
                        aggregate_type="task",
                        aggregate_id=task["id"],
                        aggregate_version=task["version"],
                        event_type="task.aligned_by_assignee",
                        operation="upsert",
                        actor_id=invitation["assignee_member_id"],
                        actor_type="member",
                        device_id=f"alignment:{invitation_id}",
                        payload=task_payload,
                    )
                    if alignment_case is not None:
                        alignment_case = connection.execute(
                            """
                            SELECT * FROM secretary_task_alignment_cases WHERE id = ?
                            """,
                            (alignment_case["id"],),
                        ).fetchone()
                        decision = connection.execute(
                            """
                            SELECT * FROM secretary_task_alignment_decisions
                            WHERE id = ?
                            """,
                            (decision_id,),
                        ).fetchone()
                        assert alignment_case is not None and decision is not None
                        self._append_event(
                            connection,
                            workspace_id=invitation["workspace_id"],
                            aggregate_type="task_agreement",
                            aggregate_id=alignment_case["id"],
                            aggregate_version=alignment_case["version"],
                            event_type="task.agreement_accept",
                            operation="upsert",
                            actor_id=invitation["assignee_member_id"],
                            actor_type="member",
                            device_id=f"alignment:{invitation_id}",
                            payload={
                                "agreement": self._agreement_dict(
                                    connection, alignment_case
                                ),
                                "decision": self._agreement_decision_dict(decision),
                                "task": {
                                    "id": task["id"],
                                    "stage": task["stage"],
                                    "version": task["version"],
                                    "updated_at": task["updated_at"],
                                },
                            },
                            occurred_at=now,
                        )
                    result = {
                        "invitation_id": invitation_id,
                        "task_id": task["id"],
                        "stage": task["stage"],
                        "version": task["version"],
                        "assignee_member_id": invitation["assignee_member_id"],
                        "assignee_label": member["display_name"],
                        "confirmed_at": now,
                    }

        if error is not None:
            raise error
        if result is None:
            raise PocketError(503, "暂时无法完成任务对齐")
        return result

    def transition_task(
        self,
        workspace_id: str,
        task_id: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"task.transition:{task_id}"
        request_payload = {"workspace_id": workspace_id, "task_id": task_id, **payload}
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            row = connection.execute(
                """
                SELECT * FROM secretary_business_tasks
                WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                """,
                (task_id, workspace_id),
            ).fetchone()
            if row is None:
                raise PocketError(404, "任务不存在")
            expected_version = int(payload["expected_version"])
            self._require_version(row, expected_version)
            target = payload["target_stage"]
            require_task_transition(row["stage"], target)
            if (
                row["assignee_member_id"] != DEFAULT_OWNER_ID
                and (
                    (row["stage"] == "aligned" and target == "in_progress")
                    or (
                        row["stage"] == "in_progress"
                        and target == "submitted"
                    )
                )
            ):
                raise PocketError(
                    409,
                    "外部承办任务必须由承办人使用任务执行凭据启动或提交",
                )
            if (
                row["assignee_member_id"] != DEFAULT_OWNER_ID
                and target == "abnormal_closed"
            ):
                raise PocketError(409, "外部承办任务非正常关闭必须走正式变更协议")
            if target == "aligned" and row["assignee_member_id"] != DEFAULT_OWNER_ID:
                raise PocketError(409, "需要承办人使用独立确认凭据完成任务对齐")
            returning_for_rework = (
                row["stage"] == "submitted" and target == "in_progress"
            )
            if returning_for_rework and not str(payload.get("note") or "").strip():
                raise PocketError(422, "退回返工必须填写说明")
            final_target = target
            event_type = (
                "task.returned_for_rework"
                if returning_for_rework
                else f"task.{target}"
            )
            if (
                row["stage"] == "draft"
                and target == "issued"
                and not bool(row["requires_alignment"])
            ):
                final_target = "aligned"
                event_type = "task.aligned_automatically"
            if final_target == "accepted" and not json_loads(
                row["acceptance_criteria_json"], []
            ):
                raise PocketError(409, "没有验收标准的任务不能验收")
            now = utc_now()
            assignments = [
                "stage = ?",
                "version = version + 1",
                "updated_by = ?",
                "updated_at = ?",
            ]
            values: list[Any] = [final_target, DEFAULT_OWNER_ID, now]
            timestamp_field = transition_timestamp_field(final_target)
            if timestamp_field is not None and not returning_for_rework:
                assignments.append(f"{timestamp_field} = ?")
                values.append(now)
            if returning_for_rework:
                assignments.append("submitted_at = NULL")
            if final_target == "submitted":
                assignments.append("progress = 100")
            values.extend([task_id, workspace_id, expected_version])
            updated = connection.execute(
                f"UPDATE secretary_business_tasks SET {', '.join(assignments)} "
                "WHERE id = ? AND workspace_id = ? AND version = ?",
                values,
            )
            if updated.rowcount != 1:
                raise PocketError(412, "任务版本已变化，请重新同步")
            row = connection.execute(
                "SELECT * FROM secretary_business_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            response = self._task_dict(connection, row)
            if payload.get("note"):
                response["transition_note"] = payload["note"]
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="task",
                aggregate_id=task_id,
                aggregate_version=response["version"],
                event_type=event_type,
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=response,
            )
            response.pop("transition_note", None)
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
            )
            return response

    @staticmethod
    def _require_self_managed_task(task: sqlite3.Row) -> None:
        if any(
            task[column] != DEFAULT_OWNER_ID
            for column in (
                "issuer_member_id",
                "assignee_member_id",
                "acceptance_owner_id",
            )
        ):
            raise PocketError(409, "当前版本仅支持主人自办任务的步骤编排")
        if task["stage"] in TERMINAL_TASK_STAGES:
            raise PocketError(409, "已关闭任务不能修改步骤")

    @staticmethod
    def _require_self_managed_plan(
        connection: sqlite3.Connection, task_id: str
    ) -> None:
        external_step = connection.execute(
            """
            SELECT 1 FROM secretary_task_steps
            WHERE task_id = ? AND deleted_at IS NULL
              AND (assignee_member_id IS NULL OR assignee_member_id <> ?)
            LIMIT 1
            """,
            (task_id, DEFAULT_OWNER_ID),
        ).fetchone()
        if external_step is not None:
            raise PocketError(409, "当前版本仅支持主人自办步骤的编排")

    @staticmethod
    def _task_step_row(
        connection: sqlite3.Connection, task_id: str, step_id: str
    ) -> sqlite3.Row:
        step = connection.execute(
            """
            SELECT * FROM secretary_task_steps
            WHERE id = ? AND task_id = ? AND deleted_at IS NULL
            """,
            (step_id, task_id),
        ).fetchone()
        if step is None:
            raise PocketError(404, "任务步骤不存在")
        return step

    @staticmethod
    def _validate_task_step_graph(connection: sqlite3.Connection, task_id: str) -> None:
        rows = connection.execute(
            """
            SELECT id, parent_step_id FROM secretary_task_steps
            WHERE task_id = ? AND deleted_at IS NULL
            ORDER BY id
            """,
            (task_id,),
        ).fetchall()
        step_ids = {row["id"] for row in rows}
        parents: dict[str, str | None] = {}
        for row in rows:
            parent_id = row["parent_step_id"]
            if parent_id is not None and parent_id not in step_ids:
                raise PocketError(422, "父步骤必须属于同一任务")
            if parent_id == row["id"]:
                raise PocketError(422, "步骤不能以自身作为父步骤")
            parents[row["id"]] = parent_id

        for step_id in sorted(step_ids):
            seen: set[str] = set()
            cursor: str | None = step_id
            while cursor is not None:
                if cursor in seen:
                    raise PocketError(422, "父子步骤不能形成环")
                seen.add(cursor)
                cursor = parents.get(cursor)

        dependency_rows = connection.execute(
            """
            SELECT dependency.step_id, dependency.depends_on_step_id
            FROM secretary_task_step_dependencies dependency
            JOIN secretary_task_steps step ON step.id = dependency.step_id
            WHERE step.task_id = ? AND step.deleted_at IS NULL
            ORDER BY dependency.step_id, dependency.depends_on_step_id
            """,
            (task_id,),
        ).fetchall()
        dependencies: dict[str, set[str]] = {step_id: set() for step_id in step_ids}
        for dependency in dependency_rows:
            step_id = dependency["step_id"]
            depends_on = dependency["depends_on_step_id"]
            if depends_on not in step_ids:
                raise PocketError(422, "步骤依赖必须属于同一任务")
            if step_id == depends_on:
                raise PocketError(422, "步骤不能依赖自身")
            if parents.get(step_id) == depends_on:
                raise PocketError(422, "父步骤不能同时作为直接依赖")
            dependencies[step_id].add(depends_on)

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise PocketError(422, "步骤依赖不能形成环")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency_id in sorted(dependencies[step_id]):
                visit(dependency_id)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in sorted(step_ids):
            visit(step_id)

    @staticmethod
    def _replace_step_dependencies(
        connection: sqlite3.Connection,
        *,
        step_id: str,
        dependency_ids: list[str],
    ) -> None:
        connection.execute(
            "DELETE FROM secretary_task_step_dependencies WHERE step_id = ?",
            (step_id,),
        )
        for dependency_id in dependency_ids:
            connection.execute(
                """
                INSERT INTO secretary_task_step_dependencies(step_id, depends_on_step_id)
                VALUES (?, ?)
                """,
                (step_id, dependency_id),
            )

    @staticmethod
    def _task_progress(connection: sqlite3.Connection, task_id: str) -> int:
        counts = connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done
            FROM secretary_task_steps
            WHERE task_id = ? AND deleted_at IS NULL AND status <> 'canceled'
            """,
            (task_id,),
        ).fetchone()
        total = int(counts["total"] or 0)
        return round(100 * int(counts["done"] or 0) / total) if total else 0

    @staticmethod
    def _bump_task_after_step_write(
        connection: sqlite3.Connection,
        task: sqlite3.Row,
        *,
        now: str,
        progress: int | None = None,
    ) -> sqlite3.Row:
        assignments = [
            "version = version + 1",
            "updated_by = ?",
            "updated_at = ?",
        ]
        values: list[Any] = [DEFAULT_OWNER_ID, now]
        if progress is not None:
            assignments.insert(0, "progress = ?")
            values.insert(0, progress)
        values.extend([task["id"], task["workspace_id"], task["version"]])
        updated = connection.execute(
            f"UPDATE secretary_business_tasks SET {', '.join(assignments)} "
            "WHERE id = ? AND workspace_id = ? AND version = ?",
            values,
        )
        if updated.rowcount != 1:
            raise PocketError(412, "任务版本已变化，请重新同步")
        row = connection.execute(
            "SELECT * FROM secretary_business_tasks WHERE id = ?", (task["id"],)
        ).fetchone()
        assert row is not None
        return row

    def append_task_step(
        self,
        workspace_id: str,
        task_id: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"task.step.create:{task_id}"
        request_payload = {"workspace_id": workspace_id, "task_id": task_id, **payload}
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            task = connection.execute(
                """
                SELECT * FROM secretary_business_tasks
                WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                """,
                (task_id, workspace_id),
            ).fetchone()
            if task is None:
                raise PocketError(404, "任务不存在")
            self._require_version(task, int(payload["expected_version"]))
            self._require_self_managed_task(task)
            self._require_self_managed_plan(connection, task_id)
            self._validate_task_step_graph(connection, task_id)
            if payload.get("step_type", "action") == "key_result" and not payload.get(
                "success_metric"
            ):
                raise PocketError(422, "关键结果步骤必须提供非空 success_metric")
            position = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(position), -1) + 1
                    FROM secretary_task_steps
                    WHERE task_id = ? AND deleted_at IS NULL
                    """,
                    (task_id,),
                ).fetchone()[0]
            )
            now = utc_now()
            step_id = new_id("step")
            connection.execute(
                """
                INSERT INTO secretary_task_steps(
                    id, workspace_id, task_id, parent_step_id, step_type,
                    title, description, assignee_member_id, assignee_label,
                    status, position, due_at, success_metric_json, version,
                    created_at, updated_at, client_mutation_id
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, 1, ?, ?, ?
                )
                """,
                (
                    step_id,
                    workspace_id,
                    task_id,
                    payload.get("parent_step_id"),
                    payload.get("step_type", "action"),
                    payload["title"],
                    payload.get("description") or "",
                    DEFAULT_OWNER_ID,
                    self._member_label(connection, workspace_id, DEFAULT_OWNER_ID),
                    position,
                    _iso_datetime(payload.get("due_at")),
                    _json(payload.get("success_metric") or {}),
                    now,
                    now,
                    payload.get("client_mutation_id"),
                ),
            )
            self._replace_step_dependencies(
                connection,
                step_id=step_id,
                dependency_ids=payload.get("depends_on_step_ids", []),
            )
            self._validate_task_step_graph(connection, task_id)
            task = self._bump_task_after_step_write(
                connection,
                task,
                now=now,
                progress=self._task_progress(connection, task_id),
            )
            response = self._task_dict(connection, task)
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="task",
                aggregate_id=task_id,
                aggregate_version=response["version"],
                event_type="task.step_created",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=response,
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
                status_code=201,
            )
            return response

    def patch_task_step(
        self,
        workspace_id: str,
        task_id: str,
        step_id: str,
        expected_version: int,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"task.step.update:{task_id}:{step_id}"
        request_payload = {
            "workspace_id": workspace_id,
            "task_id": task_id,
            "step_id": step_id,
            "expected_version": expected_version,
            **payload,
        }
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            task = connection.execute(
                """
                SELECT * FROM secretary_business_tasks
                WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                """,
                (task_id, workspace_id),
            ).fetchone()
            if task is None:
                raise PocketError(404, "任务不存在")
            self._require_version(task, expected_version)
            self._require_self_managed_task(task)
            self._require_self_managed_plan(connection, task_id)
            self._validate_task_step_graph(connection, task_id)
            step = self._task_step_row(connection, task_id, step_id)
            effective_type = payload.get("step_type", step["step_type"])
            effective_metric = (
                payload["success_metric"]
                if "success_metric" in payload
                else json_loads(step["success_metric_json"], {})
            )
            if effective_type == "key_result" and not effective_metric:
                raise PocketError(422, "关键结果步骤必须提供非空 success_metric")
            mapping: dict[str, tuple[str, Callable[[Any], Any]]] = {
                "parent_step_id": ("parent_step_id", lambda value: value),
                "step_type": ("step_type", str),
                "title": ("title", str),
                "description": ("description", str),
                "due_at": ("due_at", _iso_datetime),
                "success_metric": ("success_metric_json", _json),
            }
            assignments: list[str] = []
            values: list[Any] = []
            for key, (column, transform) in mapping.items():
                if key in payload:
                    assignments.append(f"{column} = ?")
                    values.append(transform(payload[key]))
            now = utc_now()
            if assignments:
                assignments.extend(["version = version + 1", "updated_at = ?"])
                values.extend([now, step_id])
                connection.execute(
                    f"UPDATE secretary_task_steps SET {', '.join(assignments)} "
                    "WHERE id = ?",
                    values,
                )
            if "depends_on_step_ids" in payload:
                self._replace_step_dependencies(
                    connection,
                    step_id=step_id,
                    dependency_ids=payload["depends_on_step_ids"],
                )
                if not assignments:
                    connection.execute(
                        """
                        UPDATE secretary_task_steps
                        SET version = version + 1, updated_at = ? WHERE id = ?
                        """,
                        (now, step_id),
                    )
            self._validate_task_step_graph(connection, task_id)
            task = self._bump_task_after_step_write(connection, task, now=now)
            response = self._task_dict(connection, task)
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="task",
                aggregate_id=task_id,
                aggregate_version=response["version"],
                event_type="task.step_metadata_updated",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=response,
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
            )
            return response

    def reorder_task_steps(
        self,
        workspace_id: str,
        task_id: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"task.steps.reorder:{task_id}"
        request_payload = {"workspace_id": workspace_id, "task_id": task_id, **payload}
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            task = connection.execute(
                """
                SELECT * FROM secretary_business_tasks
                WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                """,
                (task_id, workspace_id),
            ).fetchone()
            if task is None:
                raise PocketError(404, "任务不存在")
            self._require_version(task, int(payload["expected_version"]))
            self._require_self_managed_task(task)
            self._require_self_managed_plan(connection, task_id)
            self._validate_task_step_graph(connection, task_id)
            steps = connection.execute(
                """
                SELECT id, position FROM secretary_task_steps
                WHERE task_id = ? AND deleted_at IS NULL
                ORDER BY position, created_at, id
                """,
                (task_id,),
            ).fetchall()
            requested_ids = payload["step_ids"]
            existing_ids = [step["id"] for step in steps]
            if len(requested_ids) != len(existing_ids) or set(requested_ids) != set(
                existing_ids
            ):
                raise PocketError(422, "step_ids 必须完整且仅包含当前任务的步骤")
            original_positions = {step["id"]: step["position"] for step in steps}
            temporary_base = (
                max(original_positions.values(), default=-1) + len(steps) + 1
            )
            for offset, step_id_value in enumerate(requested_ids):
                connection.execute(
                    "UPDATE secretary_task_steps SET position = ? WHERE id = ?",
                    (temporary_base + offset, step_id_value),
                )
            now = utc_now()
            for position, step_id_value in enumerate(requested_ids):
                if original_positions[step_id_value] == position:
                    connection.execute(
                        "UPDATE secretary_task_steps SET position = ? WHERE id = ?",
                        (position, step_id_value),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE secretary_task_steps
                        SET position = ?, version = version + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (position, now, step_id_value),
                    )
            task = self._bump_task_after_step_write(connection, task, now=now)
            response = self._task_dict(connection, task)
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="task",
                aggregate_id=task_id,
                aggregate_version=response["version"],
                event_type="task.steps_reordered",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=response,
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
            )
            return response

    def upsert_task_step_schedule(
        self,
        workspace_id: str,
        task_id: str,
        step_id: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"task.step.schedule:{task_id}:{step_id}"
        request_payload = {
            "workspace_id": workspace_id,
            "task_id": task_id,
            "step_id": step_id,
            **payload,
        }
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            task = connection.execute(
                """
                SELECT * FROM secretary_business_tasks
                WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                """,
                (task_id, workspace_id),
            ).fetchone()
            if task is None:
                raise PocketError(404, "任务不存在")
            self._require_version(task, int(payload["expected_version"]))
            self._require_self_managed_task(task)
            self._require_self_managed_plan(connection, task_id)
            self._validate_task_step_graph(connection, task_id)
            step = self._task_step_row(connection, task_id, step_id)
            if step["step_type"] != "action":
                raise PocketError(409, "只有 action 步骤可以排入日程")
            if step["status"] in {"done", "canceled"}:
                raise PocketError(409, "已完成或已取消的步骤不能排入日程")
            if payload.get("kind", "focus") not in {"focus", "reminder"}:
                raise PocketError(422, "步骤日程 kind 仅支持 focus 或 reminder")
            child = connection.execute(
                """
                SELECT 1 FROM secretary_task_steps
                WHERE parent_step_id = ? AND task_id = ? AND deleted_at IS NULL
                LIMIT 1
                """,
                (step_id, task_id),
            ).fetchone()
            if child is not None:
                raise PocketError(409, "只有叶子 action 步骤可以排入日程")
            start_at = _iso_datetime(payload["start_at"])
            end_at = _iso_datetime(payload["end_at"])
            if end_at <= start_at:
                raise PocketError(422, "日程结束时间必须晚于开始时间")
            now = utc_now()
            calendar = connection.execute(
                """
                SELECT * FROM secretary_calendar_entries
                WHERE workspace_id = ? AND step_id = ? AND status = 'scheduled'
                  AND deleted_at IS NULL
                ORDER BY updated_at DESC, id DESC LIMIT 1
                """,
                (workspace_id, step_id),
            ).fetchone()
            if calendar is None:
                calendar_id = new_id("calendar")
                connection.execute(
                    """
                    INSERT INTO secretary_calendar_entries(
                        id, workspace_id, memo_id, task_id, step_id, title,
                        description, start_at_utc, end_at_utc, timezone,
                        all_day, kind, domain, status, attendees_json,
                        external_provider, external_id, version, created_by,
                        updated_by, client_mutation_id, created_at, updated_at
                    ) VALUES (
                        ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'scheduled', '[]', NULL, NULL, 1, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        calendar_id,
                        workspace_id,
                        step_id,
                        payload["title"],
                        payload.get("description") or "",
                        start_at,
                        end_at,
                        payload["timezone"],
                        int(payload.get("all_day", False)),
                        payload.get("kind", "focus"),
                        task["domain"],
                        DEFAULT_OWNER_ID,
                        DEFAULT_OWNER_ID,
                        payload.get("client_mutation_id"),
                        now,
                        now,
                    ),
                )
                calendar_event = "calendar.created"
            else:
                calendar_id = calendar["id"]
                updated_calendar = connection.execute(
                    """
                    UPDATE secretary_calendar_entries
                    SET title = ?, description = ?, start_at_utc = ?,
                        end_at_utc = ?, timezone = ?, all_day = ?, kind = ?,
                        domain = ?, client_mutation_id = ?,
                        version = version + 1, updated_by = ?, updated_at = ?
                    WHERE id = ? AND version = ?
                    """,
                    (
                        payload["title"],
                        payload.get("description") or "",
                        start_at,
                        end_at,
                        payload["timezone"],
                        int(payload.get("all_day", False)),
                        payload.get("kind", "focus"),
                        task["domain"],
                        payload.get("client_mutation_id"),
                        DEFAULT_OWNER_ID,
                        now,
                        calendar_id,
                        calendar["version"],
                    ),
                )
                if updated_calendar.rowcount != 1:
                    raise PocketError(412, "日程版本已变化，请重新同步")
                calendar_event = "calendar.updated"
            connection.execute(
                """
                UPDATE secretary_task_steps
                SET version = version + 1, updated_at = ? WHERE id = ?
                """,
                (now, step_id),
            )
            task = self._bump_task_after_step_write(connection, task, now=now)
            calendar = connection.execute(
                "SELECT * FROM secretary_calendar_entries WHERE id = ?",
                (calendar_id,),
            ).fetchone()
            assert calendar is not None
            calendar_response = self._calendar_dict(calendar)
            task_response = self._task_dict(connection, task)
            response = {
                "task": task_response,
                "calendar_entry": calendar_response,
            }
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="calendar_entry",
                aggregate_id=calendar_id,
                aggregate_version=calendar_response["version"],
                event_type=calendar_event,
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=calendar_response,
            )
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="task",
                aggregate_id=task_id,
                aggregate_version=task_response["version"],
                event_type="task.step_scheduled",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=task_response,
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
            )
            return response

    def set_task_step_schedule_status(
        self,
        workspace_id: str,
        task_id: str,
        step_id: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"task.step.schedule.status:{task_id}:{step_id}"
        request_payload = {
            "workspace_id": workspace_id,
            "task_id": task_id,
            "step_id": step_id,
            **payload,
        }
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            task = connection.execute(
                """
                SELECT * FROM secretary_business_tasks
                WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                """,
                (task_id, workspace_id),
            ).fetchone()
            if task is None:
                raise PocketError(404, "任务不存在")
            self._require_version(task, int(payload["expected_version"]))
            self._require_self_managed_task(task)
            self._require_self_managed_plan(connection, task_id)
            self._validate_task_step_graph(connection, task_id)
            self._task_step_row(connection, task_id, step_id)
            calendar = connection.execute(
                """
                SELECT * FROM secretary_calendar_entries
                WHERE workspace_id = ? AND step_id = ? AND status = 'scheduled'
                  AND deleted_at IS NULL
                ORDER BY updated_at DESC, id DESC LIMIT 1
                """,
                (workspace_id, step_id),
            ).fetchone()
            if calendar is None:
                raise PocketError(404, "步骤当前没有活动日程")
            now = utc_now()
            target_status = payload["target_status"]
            updated_calendar = connection.execute(
                """
                UPDATE secretary_calendar_entries
                SET status = ?, version = version + 1,
                    updated_by = ?, updated_at = ?
                WHERE id = ? AND version = ? AND status = 'scheduled'
                """,
                (
                    target_status,
                    DEFAULT_OWNER_ID,
                    now,
                    calendar["id"],
                    calendar["version"],
                ),
            )
            if updated_calendar.rowcount != 1:
                raise PocketError(412, "日程版本已变化，请重新同步")
            connection.execute(
                """
                UPDATE secretary_task_steps
                SET version = version + 1, updated_at = ? WHERE id = ?
                """,
                (now, step_id),
            )
            task = self._bump_task_after_step_write(connection, task, now=now)
            calendar = connection.execute(
                "SELECT * FROM secretary_calendar_entries WHERE id = ?",
                (calendar["id"],),
            ).fetchone()
            assert calendar is not None
            calendar_response = self._calendar_dict(calendar)
            task_response = self._task_dict(connection, task)
            response = {
                "task": task_response,
                "calendar_entry": calendar_response,
            }
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="calendar_entry",
                aggregate_id=calendar["id"],
                aggregate_version=calendar_response["version"],
                event_type=f"calendar.{target_status}",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=calendar_response,
            )
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="task",
                aggregate_id=task_id,
                aggregate_version=task_response["version"],
                event_type="task.step_schedule_status_changed",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=task_response,
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
            )
            return response

    def set_task_step(
        self,
        workspace_id: str,
        task_id: str,
        step_id: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"task.step.set:{task_id}:{step_id}"
        request_payload = {
            "workspace_id": workspace_id,
            "task_id": task_id,
            "step_id": step_id,
            **payload,
        }
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            task = connection.execute(
                """
                SELECT * FROM secretary_business_tasks
                WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                """,
                (task_id, workspace_id),
            ).fetchone()
            if task is None:
                raise PocketError(404, "任务不存在")
            expected_version = int(payload["expected_version"])
            self._require_version(task, expected_version)
            self._require_self_managed_task(task)
            self._require_self_managed_plan(connection, task_id)
            self._validate_task_step_graph(connection, task_id)
            self._task_step_row(connection, task_id, step_id)
            now = utc_now()
            status = payload["status"]
            if status == "done":
                unfinished_dependency = connection.execute(
                    """
                    SELECT 1
                    FROM secretary_task_step_dependencies dependency
                    JOIN secretary_task_steps prerequisite
                      ON prerequisite.id = dependency.depends_on_step_id
                    WHERE dependency.step_id = ?
                      AND prerequisite.deleted_at IS NULL
                      AND prerequisite.status NOT IN ('done', 'canceled')
                    LIMIT 1
                    """,
                    (step_id,),
                ).fetchone()
                if unfinished_dependency is not None:
                    raise PocketError(409, "步骤依赖尚未完成，不能标记为完成")
            completed_at = now if status == "done" else None
            updated_step = connection.execute(
                """
                UPDATE secretary_task_steps
                SET status = ?, completed_at = ?, version = version + 1,
                    updated_at = ? WHERE id = ?
                """,
                (status, completed_at, now, step_id),
            )
            if updated_step.rowcount != 1:
                raise PocketError(412, "任务步骤已变化，请重新同步")
            task = self._bump_task_after_step_write(
                connection,
                task,
                now=now,
                progress=self._task_progress(connection, task_id),
            )
            response = self._task_dict(connection, task)
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="task",
                aggregate_id=task_id,
                aggregate_version=response["version"],
                event_type="task.step_status_updated",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=response,
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
            )
            return response

    def create_task_change(
        self,
        workspace_id: str,
        task_id: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"task.change.create:{task_id}"
        request_payload = {"workspace_id": workspace_id, "task_id": task_id, **payload}
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            task = connection.execute(
                """
                SELECT * FROM secretary_business_tasks
                WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                """,
                (task_id, workspace_id),
            ).fetchone()
            if task is None:
                raise PocketError(404, "任务不存在")
            base_version = int(payload["base_version"])
            self._require_version(task, base_version)
            if task["stage"] in {"accepted", "abnormal_closed"}:
                raise PocketError(409, "已关闭任务不能发起变更")
            if task["issuer_member_id"] != DEFAULT_OWNER_ID:
                raise PocketError(403, "只有当前任务下达人才能发起变更")
            change_type = payload["change_type"]
            locked_agreement = connection.execute(
                """
                SELECT id, status FROM secretary_task_alignment_cases
                WHERE task_id = ? AND status IN ('pending', 'accepted')
                ORDER BY updated_at DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if (
                locked_agreement is not None
                and locked_agreement["status"] == "pending"
            ):
                raise PocketError(409, "任务协议待回应期间不能发起任务变更")
            pending = connection.execute(
                """
                SELECT id FROM secretary_task_changes
                WHERE task_id = ? AND status = 'proposed'
                """,
                (task_id,),
            ).fetchone()
            if pending is not None:
                raise PocketError(409, "该任务已有待确认的变更")
            change_id = new_id("change")
            responder_member_id = task["assignee_member_id"]
            if responder_member_id is None:
                raise PocketError(409, "任务尚未指定承办人，不能发起双方变更")
            patch = dict(payload["patch"])
            if change_type == "assignee":
                self._member_label(
                    connection,
                    workspace_id,
                    str(patch["assignee_member_id"]),
                )
            document = self._task_change_document(
                task,
                change_id=change_id,
                change_type=change_type,
                patch=patch,
                reason=payload["reason"],
                proposer_member_id=DEFAULT_OWNER_ID,
                responder_member_id=responder_member_id,
            )
            if document["before"] == next(iter(document["patch"].values())):
                raise PocketError(422, "任务变更值与当前值相同")
            canonical_json, proposal_digest = _task_change_digest(document)
            now = utc_now()
            connection.execute(
                """
                INSERT INTO secretary_task_changes(
                    id, workspace_id, task_id, change_type, base_version,
                    before_json, patch_json, reason, status, proposed_by,
                    proposed_at, version, client_mutation_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?, 1, ?, ?)
                """,
                (
                    change_id,
                    workspace_id,
                    task_id,
                    change_type,
                    base_version,
                    _json(document["before"]),
                    _json(document["patch"]),
                    document["reason"],
                    DEFAULT_OWNER_ID,
                    now,
                    payload.get("client_mutation_id"),
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO secretary_task_change_proposals(
                    change_id, workspace_id, task_id, proposer_member_id,
                    responder_member_id, base_task_version, digest,
                    canonical_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    change_id,
                    workspace_id,
                    task_id,
                    DEFAULT_OWNER_ID,
                    responder_member_id,
                    base_version,
                    proposal_digest,
                    canonical_json,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM secretary_task_changes WHERE id = ?", (change_id,)
            ).fetchone()
            response = {
                **self._change_dict(row),
                "proposal_digest": proposal_digest,
                "responder_member_id": responder_member_id,
                "protocol_version": 1,
            }
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="task_change",
                aggregate_id=change_id,
                aggregate_version=1,
                event_type="task.change_proposed",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=response,
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
                status_code=201,
            )
            return response

    def task_execution_view(
        self,
        task_id: str,
        principal: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        with self.database.connect() as connection:
            task = self._task_execution_principal_task(
                connection,
                task_id,
                principal,
                device_id=principal["device_id"],
                require_active=True,
            )
            return self._task_execution_projection(
                connection,
                task,
                member_id=principal["member_id"],
            )

    def start_task_execution(
        self,
        task_id: str,
        payload: dict[str, Any],
        principal: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
        if_match: str | None,
    ) -> tuple[dict[str, Any], str]:
        operation = f"task_execution.start:{task_id}"
        with self.database.transaction() as connection:
            (
                task,
                cached,
                cached_etag,
                request_hash,
                mutation_request_hash,
                _current_etag,
            ) = (
                self._task_execution_write_context(
                    connection,
                    task_id=task_id,
                    payload=payload,
                    principal=principal,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    device_id=device_id,
                    if_match=if_match,
                )
            )
            if cached is not None:
                assert cached_etag is not None
                return cached, cached_etag
            if task["stage"] != "aligned":
                raise PocketError(409, "只有已对齐任务可以由承办人启动执行")
            if self._task_has_pending_change(connection, task_id):
                raise PocketError(409, "任务存在待确认变更，暂不能启动执行")
            now = utc_now()
            updated = connection.execute(
                """
                UPDATE secretary_business_tasks
                SET stage = 'in_progress', started_at = ?,
                    version = version + 1, updated_by = ?, updated_at = ?
                WHERE id = ? AND workspace_id = ? AND version = ?
                  AND assignment_epoch = ? AND assignee_member_id = ?
                  AND stage = 'aligned'
                """,
                (
                    now,
                    principal["member_id"],
                    now,
                    task_id,
                    task["workspace_id"],
                    task["version"],
                    principal["assignment_epoch"],
                    principal["member_id"],
                ),
            )
            if updated.rowcount != 1:
                raise PocketError(412, "任务版本或承办人绑定已变化")
            task = connection.execute(
                "SELECT * FROM secretary_business_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            assert task is not None
            projection, etag = self._task_execution_projection(
                connection,
                task,
                member_id=principal["member_id"],
            )
            result = {"task": projection}
            event_payload = dict(result)
            event_payload.update(self._task_execution_actor_metadata(principal))
            event_payload["client_mutation_id"] = payload["client_mutation_id"]
            if payload.get("note"):
                event_payload["note"] = payload["note"]
            self._append_event(
                connection,
                workspace_id=task["workspace_id"],
                aggregate_type="task",
                aggregate_id=task_id,
                aggregate_version=task["version"],
                event_type="task.execution_started",
                operation="upsert",
                actor_id=None,
                actor_type="system",
                device_id=device_id,
                payload=event_payload,
            )
            self._store_task_execution_command_response(
                connection,
                workspace_id=task["workspace_id"],
                actor_id=principal["idempotency_actor_id"],
                mutation_actor_id=self._task_execution_mutation_actor_id(
                    task_id, principal
                ),
                task_id=task_id,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                client_mutation_id=payload["client_mutation_id"],
                mutation_request_hash=mutation_request_hash,
                response=result,
                etag=etag,
            )
            return result, etag

    def create_task_execution_checkin(
        self,
        task_id: str,
        payload: dict[str, Any],
        principal: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
        if_match: str | None,
    ) -> tuple[dict[str, Any], str]:
        operation = f"task_execution.checkin.create:{task_id}"
        with self.database.transaction() as connection:
            (
                task,
                cached,
                cached_etag,
                request_hash,
                mutation_request_hash,
                _current_etag,
            ) = (
                self._task_execution_write_context(
                    connection,
                    task_id=task_id,
                    payload=payload,
                    principal=principal,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    device_id=device_id,
                    if_match=if_match,
                    status_code=201,
                )
            )
            if cached is not None:
                assert cached_etag is not None
                return cached, cached_etag
            if task["stage"] != "in_progress":
                raise PocketError(409, "只有执行中的任务可以追加执行回报")
            duplicate = connection.execute(
                """
                SELECT id FROM secretary_task_checkins
                WHERE task_id = ? AND created_by = ? AND client_mutation_id = ?
                LIMIT 1
                """,
                (task_id, principal["member_id"], payload["client_mutation_id"]),
            ).fetchone()
            if duplicate is not None:
                raise PocketError(409, "client_mutation_id 已用于其他执行回报")
            workspace = self._require_workspace(connection, task["workspace_id"])
            now_dt = datetime.now(UTC)
            now = _iso_datetime(now_dt)
            assert now is not None
            report_date = (
                now_dt.astimezone(ZoneInfo(str(workspace["timezone"])))
                .date()
                .isoformat()
            )
            checkin_id = new_id("checkin")
            connection.execute(
                """
                INSERT INTO secretary_task_checkins(
                    id, workspace_id, task_id, task_version, report_date,
                    summary, reported_progress, risks_json, blockers_json,
                    next_actions_json, forecast_at, created_by, device_id,
                    client_mutation_id, version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    checkin_id,
                    task["workspace_id"],
                    task_id,
                    task["version"],
                    report_date,
                    payload["summary"],
                    payload["reported_progress"],
                    _json(payload.get("risks", [])),
                    _json(payload.get("blockers", [])),
                    _json(payload.get("next_actions", [])),
                    _iso_datetime(payload.get("forecast_at")),
                    principal["member_id"],
                    device_id,
                    payload["client_mutation_id"],
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM secretary_task_checkins WHERE id = ?",
                (checkin_id,),
            ).fetchone()
            assert row is not None
            checkin = {
                "id": row["id"],
                "task_id": row["task_id"],
                "task_version": row["task_version"],
                "report_date": row["report_date"],
                "summary": row["summary"],
                "reported_progress": row["reported_progress"],
                "risks": json_loads(row["risks_json"], []),
                "blockers": json_loads(row["blockers_json"], []),
                "next_actions": json_loads(row["next_actions_json"], []),
                "forecast_at": row["forecast_at"],
                "version": row["version"],
                "created_at": row["created_at"],
            }
            projection, etag = self._task_execution_projection(
                connection,
                task,
                member_id=principal["member_id"],
            )
            result = {"task": projection, "check_in": checkin}
            self._append_event(
                connection,
                workspace_id=task["workspace_id"],
                aggregate_type="task_checkin",
                aggregate_id=checkin_id,
                aggregate_version=1,
                event_type="task.execution_checkin_recorded",
                operation="upsert",
                actor_id=None,
                actor_type="system",
                device_id=device_id,
                payload={
                    "check_in": checkin,
                    "client_mutation_id": payload["client_mutation_id"],
                    **self._task_execution_actor_metadata(principal),
                },
                occurred_at=now,
            )
            self._store_task_execution_command_response(
                connection,
                workspace_id=task["workspace_id"],
                actor_id=principal["idempotency_actor_id"],
                mutation_actor_id=self._task_execution_mutation_actor_id(
                    task_id, principal
                ),
                task_id=task_id,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                client_mutation_id=payload["client_mutation_id"],
                mutation_request_hash=mutation_request_hash,
                response=result,
                etag=etag,
                status_code=201,
            )
            return result, etag

    def set_task_execution_step_status(
        self,
        task_id: str,
        step_id: str,
        payload: dict[str, Any],
        principal: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
        if_match: str | None,
    ) -> tuple[dict[str, Any], str]:
        operation = f"task_execution.step.set:{task_id}:{step_id}"
        request_payload = {"step_id": step_id, **payload}
        with self.database.transaction() as connection:
            (
                task,
                cached,
                cached_etag,
                request_hash,
                mutation_request_hash,
                _current_etag,
            ) = (
                self._task_execution_write_context(
                    connection,
                    task_id=task_id,
                    payload=request_payload,
                    principal=principal,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    device_id=device_id,
                    if_match=if_match,
                )
            )
            if cached is not None:
                assert cached_etag is not None
                return cached, cached_etag
            if task["stage"] != "in_progress":
                raise PocketError(409, "只有执行中的任务可以更新步骤状态")
            if self._task_has_pending_change(connection, task_id):
                raise PocketError(409, "任务存在待确认变更，暂不能更新步骤")
            step = connection.execute(
                """
                SELECT * FROM secretary_task_steps
                WHERE id = ? AND task_id = ? AND workspace_id = ?
                  AND deleted_at IS NULL
                """,
                (step_id, task_id, task["workspace_id"]),
            ).fetchone()
            if step is None:
                raise PocketError(404, "任务步骤不存在")
            if step["assignee_member_id"] != principal["member_id"]:
                raise PocketError(403, "只能更新当前承办人本人负责的步骤")
            if step["version"] != payload["expected_step_version"]:
                raise PocketError(
                    412,
                    f"步骤版本冲突：预期 {payload['expected_step_version']}，"
                    f"当前为 {step['version']}",
                )
            target = payload["status"]
            if target not in {"pending", "in_progress", "blocked", "done"}:
                raise PocketError(422, "外部承办人不能取消任务步骤")
            transitions = {
                "pending": {"in_progress", "blocked", "done"},
                "in_progress": {"pending", "blocked", "done"},
                "blocked": {"pending", "in_progress", "done"},
                "done": set(),
                "canceled": set(),
            }
            if target not in transitions.get(str(step["status"]), set()):
                raise PocketError(
                    409,
                    f"步骤不能从 {step['status']} 转换为 {target}",
                )
            if target == "done":
                unfinished_dependency = connection.execute(
                    """
                    SELECT 1
                    FROM secretary_task_step_dependencies dependency
                    JOIN secretary_task_steps prerequisite
                      ON prerequisite.id = dependency.depends_on_step_id
                    WHERE dependency.step_id = ?
                      AND prerequisite.deleted_at IS NULL
                      AND prerequisite.status NOT IN ('done', 'canceled')
                    LIMIT 1
                    """,
                    (step_id,),
                ).fetchone()
                if unfinished_dependency is not None:
                    raise PocketError(409, "步骤依赖尚未完成，不能标记为完成")
            now = utc_now()
            updated_step = connection.execute(
                """
                UPDATE secretary_task_steps
                SET status = ?, completed_at = ?, version = version + 1,
                    updated_at = ?
                WHERE id = ? AND task_id = ? AND version = ?
                  AND assignee_member_id = ? AND deleted_at IS NULL
                """,
                (
                    target,
                    now if target == "done" else None,
                    now,
                    step_id,
                    task_id,
                    payload["expected_step_version"],
                    principal["member_id"],
                ),
            )
            if updated_step.rowcount != 1:
                raise PocketError(412, "步骤版本或承办人绑定已变化")
            progress = self._task_progress(connection, task_id)
            updated_task = connection.execute(
                """
                UPDATE secretary_business_tasks
                SET progress = ?, version = version + 1,
                    updated_by = ?, updated_at = ?
                WHERE id = ? AND workspace_id = ? AND version = ?
                  AND assignment_epoch = ? AND assignee_member_id = ?
                  AND stage = 'in_progress'
                """,
                (
                    progress,
                    principal["member_id"],
                    now,
                    task_id,
                    task["workspace_id"],
                    task["version"],
                    principal["assignment_epoch"],
                    principal["member_id"],
                ),
            )
            if updated_task.rowcount != 1:
                raise PocketError(412, "任务版本或承办人绑定已变化")
            task = connection.execute(
                "SELECT * FROM secretary_business_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            assert task is not None
            projection, etag = self._task_execution_projection(
                connection,
                task,
                member_id=principal["member_id"],
            )
            projected_step = next(
                item for item in projection["steps"] if item["id"] == step_id
            )
            result = {"task": projection, "step": projected_step}
            event_payload = {
                **result,
                **self._task_execution_actor_metadata(principal),
                "client_mutation_id": payload["client_mutation_id"],
            }
            if payload.get("note"):
                event_payload["note"] = payload["note"]
            self._append_event(
                connection,
                workspace_id=task["workspace_id"],
                aggregate_type="task",
                aggregate_id=task_id,
                aggregate_version=task["version"],
                event_type="task.execution_step_updated",
                operation="upsert",
                actor_id=None,
                actor_type="system",
                device_id=device_id,
                payload=event_payload,
            )
            self._store_task_execution_command_response(
                connection,
                workspace_id=task["workspace_id"],
                actor_id=principal["idempotency_actor_id"],
                mutation_actor_id=self._task_execution_mutation_actor_id(
                    task_id, principal
                ),
                task_id=task_id,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                client_mutation_id=payload["client_mutation_id"],
                mutation_request_hash=mutation_request_hash,
                response=result,
                etag=etag,
            )
            return result, etag

    def submit_task_execution(
        self,
        task_id: str,
        payload: dict[str, Any],
        principal: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
        if_match: str | None,
    ) -> tuple[dict[str, Any], str]:
        operation = f"task_execution.submit:{task_id}"
        with self.database.transaction() as connection:
            (
                task,
                cached,
                cached_etag,
                request_hash,
                mutation_request_hash,
                _current_etag,
            ) = (
                self._task_execution_write_context(
                    connection,
                    task_id=task_id,
                    payload=payload,
                    principal=principal,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    device_id=device_id,
                    if_match=if_match,
                )
            )
            if cached is not None:
                assert cached_etag is not None
                return cached, cached_etag
            if task["stage"] != "in_progress":
                raise PocketError(409, "只有执行中的任务可以提交验收")
            if self._task_has_pending_change(connection, task_id):
                raise PocketError(409, "任务存在待确认变更，暂不能提交验收")
            unfinished_leaf_action = connection.execute(
                """
                SELECT step.id
                FROM secretary_task_steps step
                WHERE step.task_id = ? AND step.deleted_at IS NULL
                  AND step.assignee_member_id = ?
                  AND step.step_type = 'action'
                  AND step.status NOT IN ('done', 'canceled')
                  AND NOT EXISTS (
                      SELECT 1 FROM secretary_task_steps child
                      WHERE child.parent_step_id = step.id
                        AND child.deleted_at IS NULL
                  )
                ORDER BY step.position, step.id LIMIT 1
                """,
                (task_id, principal["member_id"]),
            ).fetchone()
            if unfinished_leaf_action is not None:
                raise PocketError(409, "本人负责的叶子行动步骤尚未完成，不能提交验收")
            now = utc_now()
            updated = connection.execute(
                """
                UPDATE secretary_business_tasks
                SET stage = 'submitted', progress = 100, submitted_at = ?,
                    version = version + 1, updated_by = ?, updated_at = ?
                WHERE id = ? AND workspace_id = ? AND version = ?
                  AND assignment_epoch = ? AND assignee_member_id = ?
                  AND stage = 'in_progress'
                """,
                (
                    now,
                    principal["member_id"],
                    now,
                    task_id,
                    task["workspace_id"],
                    task["version"],
                    principal["assignment_epoch"],
                    principal["member_id"],
                ),
            )
            if updated.rowcount != 1:
                raise PocketError(412, "任务版本或承办人绑定已变化")
            task = connection.execute(
                "SELECT * FROM secretary_business_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            assert task is not None
            projection, etag = self._task_execution_projection(
                connection,
                task,
                member_id=principal["member_id"],
            )
            result = {"task": projection}
            event_payload = {
                **result,
                **self._task_execution_actor_metadata(principal),
                "client_mutation_id": payload["client_mutation_id"],
            }
            if payload.get("note"):
                event_payload["note"] = payload["note"]
            self._append_event(
                connection,
                workspace_id=task["workspace_id"],
                aggregate_type="task",
                aggregate_id=task_id,
                aggregate_version=task["version"],
                event_type="task.execution_submitted",
                operation="upsert",
                actor_id=None,
                actor_type="system",
                device_id=device_id,
                payload=event_payload,
            )
            self._store_task_execution_command_response(
                connection,
                workspace_id=task["workspace_id"],
                actor_id=principal["idempotency_actor_id"],
                mutation_actor_id=self._task_execution_mutation_actor_id(
                    task_id, principal
                ),
                task_id=task_id,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                client_mutation_id=payload["client_mutation_id"],
                mutation_request_hash=mutation_request_hash,
                response=result,
                etag=etag,
            )
            return result, etag

    def create_task_execution_invitation(
        self,
        workspace_id: str,
        task_id: str,
        expected_task_version: int,
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        now_dt = datetime.now(UTC)
        now = _iso_datetime(now_dt)
        assert now is not None
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            replay = connection.execute(
                """
                SELECT id FROM secretary_task_execution_invitations
                WHERE workspace_id = ? AND created_by = ? AND task_id = ?
                  AND creation_idempotency_key = ?
                """,
                (workspace_id, DEFAULT_OWNER_ID, task_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                raise PocketError(
                    409,
                    "此请求已创建过执行邀请；确认码只返回一次，请使用新的请求键",
                )
            task = connection.execute(
                """
                SELECT * FROM secretary_business_tasks
                WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                """,
                (task_id, workspace_id),
            ).fetchone()
            if task is None:
                raise PocketError(404, "任务不存在")
            self._require_version(task, expected_task_version)
            if task["issuer_member_id"] != DEFAULT_OWNER_ID:
                raise PocketError(403, "只有当前任务下达人才能签发执行邀请")
            if task["stage"] not in {"aligned", "in_progress"}:
                raise PocketError(409, "只有已对齐或执行中的任务可以签发执行邀请")
            if task["assignee_member_id"] == DEFAULT_OWNER_ID:
                raise PocketError(409, "主人自办任务不需要外部执行邀请")
            assignee = connection.execute(
                """
                SELECT * FROM secretary_workspace_members
                WHERE id = ? AND workspace_id = ? AND active = 1
                """,
                (task["assignee_member_id"], workspace_id),
            ).fetchone()
            if assignee is None or assignee["kind"] != "external":
                raise PocketError(409, "任务当前承办人不是有效的外部成员")
            agreement = connection.execute(
                """
                SELECT * FROM secretary_task_alignment_cases
                WHERE workspace_id = ? AND task_id = ?
                  AND issuer_member_id = ? AND assignee_member_id = ?
                  AND status = 'accepted' AND accepted_revision_no IS NOT NULL
                ORDER BY closed_at DESC, updated_at DESC, id DESC LIMIT 1
                """,
                (
                    workspace_id,
                    task_id,
                    DEFAULT_OWNER_ID,
                    task["assignee_member_id"],
                ),
            ).fetchone()
            if agreement is None:
                raise PocketError(409, "任务尚未完成当前双方的任务协议对齐")

            capability_expires_dt = now_dt + timedelta(
                seconds=TASK_EXECUTION_REFRESH_ABSOLUTE_TTL_SECONDS
            )
            if task["due_at"] is not None:
                due_cap = parse_utc(task["due_at"]) + timedelta(
                    seconds=TASK_EXECUTION_DUE_GRACE_SECONDS
                )
                if due_cap <= now_dt:
                    raise PocketError(
                        409,
                        "任务期限加七天宽限期已经过去，请先通过正式任务变更调整期限",
                    )
                capability_expires_dt = min(capability_expires_dt, due_cap)
            expires_dt = min(
                capability_expires_dt,
                now_dt + timedelta(seconds=ALIGNMENT_INVITATION_TTL_SECONDS),
            )
            expires_at = _iso_datetime(expires_dt)
            capability_expires_at = _iso_datetime(capability_expires_dt)
            assert expires_at is not None and capability_expires_at is not None

            # A fresh handle invalidates only older unused handles.  A live
            # execution family is rotated only after this handle exchanges.
            connection.execute(
                """
                UPDATE secretary_task_execution_invitations
                SET revoked_at = COALESCE(revoked_at, ?),
                    revoke_reason = COALESCE(revoke_reason, 'replaced')
                WHERE task_id = ? AND used_at IS NULL AND revoked_at IS NULL
                """,
                (now, task_id),
            )
            for _attempt in range(5):
                invitation_id = new_id("execution_invite")
                code = _new_alignment_code()
                try:
                    connection.execute(
                        """
                        INSERT INTO secretary_task_execution_invitations(
                            id, workspace_id, task_id, agreement_id,
                            assignee_member_id, task_version_at_issue,
                            assignment_epoch_at_issue, code_hash,
                            failed_attempts, max_attempts, created_by,
                            created_device_id, creation_idempotency_key,
                            created_at, expires_at, capability_expires_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            invitation_id,
                            workspace_id,
                            task_id,
                            agreement["id"],
                            task["assignee_member_id"],
                            task["version"],
                            task["assignment_epoch"],
                            _secret_hash(code),
                            ALIGNMENT_MAX_FAILED_ATTEMPTS,
                            DEFAULT_OWNER_ID,
                            device_id,
                            idempotency_key,
                            now,
                            expires_at,
                            capability_expires_at,
                        ),
                    )
                except sqlite3.IntegrityError:
                    continue
                break
            else:
                raise PocketError(503, "暂时无法创建任务执行邀请")
            event_payload = {
                "invitation_id": invitation_id,
                "task_id": task_id,
                "task_version": task["version"],
                "assignment_epoch": task["assignment_epoch"],
                "assignee_member_id": task["assignee_member_id"],
                "expires_at": expires_at,
                "capability_expires_at": capability_expires_at,
            }
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="task",
                aggregate_id=task_id,
                aggregate_version=task["version"],
                event_type="task.execution_invitation_created",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=event_payload,
            )
            return {
                **event_payload,
                "code": code,
                "assignee_label": task["assignee_label"],
                "confirmation_path": (
                    f"/api/v1/task-execution-invitations/{invitation_id}"
                ),
            }

    def task_execution_invitation_shell(
        self, invitation_id: str
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            invitation = connection.execute(
                """
                SELECT * FROM secretary_task_execution_invitations WHERE id = ?
                """,
                (invitation_id,),
            ).fetchone()
            if invitation is None:
                raise PocketError(404, "任务执行邀请不可用")
            if (
                invitation["revoked_at"] is not None
                or parse_utc(invitation["expires_at"]) <= datetime.now(UTC)
            ):
                raise PocketError(410, "任务执行邀请已失效")
            if invitation["used_at"] is not None:
                raise PocketError(410, "任务执行邀请已经使用")
            return {
                "invitation_id": invitation["id"],
                "expires_at": invitation["expires_at"],
            }

    def exchange_task_execution(
        self,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        invitation_id = payload["invitation_id"]
        normalized_code = _normalized_alignment_code(payload["code"])
        presented_hash = (
            _secret_hash(normalized_code) if normalized_code is not None else "0" * 64
        )
        exchange_idempotency_hash = _secret_hash(idempotency_key)
        exchange_request_hash = _hash_request(
            {
                "invitation_id": invitation_id,
                "code_hash": presented_hash,
                "client_device_id": payload["client_device_id"],
            }
        )
        now_dt = datetime.now(UTC)
        now = _iso_datetime(now_dt)
        assert now is not None
        error: PocketError | None = None
        result: dict[str, Any] | None = None
        with self.database.transaction() as connection:
            invitation = connection.execute(
                """
                SELECT * FROM secretary_task_execution_invitations WHERE id = ?
                """,
                (invitation_id,),
            ).fetchone()
            family = connection.execute(
                """
                SELECT * FROM secretary_task_execution_refresh_families
                WHERE invitation_id = ?
                """,
                (invitation_id,),
            ).fetchone()
            if invitation is None:
                error = PocketError(401, "任务执行邀请凭据无效或已失效")
            elif not secrets.compare_digest(invitation["code_hash"], presented_hash):
                if (
                    invitation["revoked_at"] is None
                    and invitation["used_at"] is None
                    and parse_utc(invitation["expires_at"]) > now_dt
                ):
                    failed_attempts = min(
                        invitation["failed_attempts"] + 1,
                        invitation["max_attempts"],
                    )
                    revoked_at = (
                        now
                        if failed_attempts >= invitation["max_attempts"]
                        else None
                    )
                    connection.execute(
                        """
                        UPDATE secretary_task_execution_invitations
                        SET failed_attempts = ?,
                            revoked_at = COALESCE(revoked_at, ?),
                            revoke_reason = CASE WHEN ? IS NULL THEN revoke_reason
                                ELSE COALESCE(revoke_reason, 'attempts_exhausted') END
                        WHERE id = ?
                        """,
                        (failed_attempts, revoked_at, revoked_at, invitation_id),
                    )
                error = PocketError(401, "任务执行邀请凭据无效或已失效")
            elif family is not None:
                first_session = connection.execute(
                    """
                    SELECT * FROM secretary_task_execution_sessions
                    WHERE refresh_family_id = ? AND access_generation = 1
                    """,
                    (family["id"],),
                ).fetchone()
                first_refresh = connection.execute(
                    """
                    SELECT * FROM secretary_task_execution_refresh_tokens
                    WHERE family_id = ? AND generation = 1
                    """,
                    (family["id"],),
                ).fetchone()
                exact_replay = bool(
                    first_session is not None
                    and first_refresh is not None
                    and secrets.compare_digest(
                        first_session["exchange_idempotency_hash"],
                        exchange_idempotency_hash,
                    )
                    and secrets.compare_digest(
                        first_session["exchange_request_hash"],
                        exchange_request_hash,
                    )
                )
                if (
                    exact_replay
                    and family["revoked_at"] is None
                    and first_session["revoked_at"] is None
                    and parse_utc(first_session["expires_at"]) > now_dt
                    and first_refresh["revoked_at"] is None
                    and first_refresh["used_at"] is None
                ):
                    access_token = self._task_execution_session_access_token(
                        session_id=first_session["id"],
                        exchange_idempotency_hash=first_session[
                            "exchange_idempotency_hash"
                        ],
                        exchange_request_hash=first_session[
                            "exchange_request_hash"
                        ],
                    )
                    refresh_token = self._task_execution_refresh_token(
                        token_id=first_refresh["id"],
                        family_id=family["id"],
                        generation=1,
                    )
                    if not (
                        secrets.compare_digest(
                            first_session["token_hash"], _secret_hash(access_token)
                        )
                        and secrets.compare_digest(
                            first_refresh["token_hash"], _secret_hash(refresh_token)
                        )
                    ):
                        self._revoke_task_execution_family(
                            connection,
                            family["id"],
                            now=now,
                            reason="token_integrity_failure",
                        )
                        error = PocketError(409, "任务执行会话完整性校验失败")
                    else:
                        result = {
                            "token_type": "Bearer",
                            "access_token": access_token,
                            "access_expires_at": first_session["expires_at"],
                            "refresh_token": refresh_token,
                            "refresh_expires_at": family["absolute_expires_at"],
                            "session": self._task_execution_session_dict(
                                first_session,
                                refresh_expires_at=family["absolute_expires_at"],
                            ),
                        }
                else:
                    if family["revoked_at"] is None:
                        self._revoke_task_execution_family(
                            connection,
                            family["id"],
                            now=now,
                            reason="unsafe_exchange_replay",
                        )
                    error = PocketError(409, "令牌交换请求冲突或会话已经轮换")
            elif (
                invitation["revoked_at"] is not None
                or invitation["used_at"] is not None
                or invitation["failed_attempts"] >= invitation["max_attempts"]
                or parse_utc(invitation["expires_at"]) <= now_dt
                or parse_utc(invitation["capability_expires_at"]) <= now_dt
            ):
                error = PocketError(401, "任务执行邀请凭据无效或已失效")
            else:
                task = connection.execute(
                    """
                    SELECT * FROM secretary_business_tasks
                    WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                    """,
                    (invitation["task_id"], invitation["workspace_id"]),
                ).fetchone()
                assignee = connection.execute(
                    """
                    SELECT * FROM secretary_workspace_members
                    WHERE id = ? AND workspace_id = ? AND active = 1
                    """,
                    (
                        invitation["assignee_member_id"],
                        invitation["workspace_id"],
                    ),
                ).fetchone()
                agreement = connection.execute(
                    """
                    SELECT * FROM secretary_task_alignment_cases WHERE id = ?
                    """,
                    (invitation["agreement_id"],),
                ).fetchone()
                binding_current = bool(
                    task is not None
                    and assignee is not None
                    and assignee["kind"] == "external"
                    and agreement is not None
                    and agreement["status"] == "accepted"
                    and agreement["task_id"] == invitation["task_id"]
                    and agreement["assignee_member_id"]
                    == invitation["assignee_member_id"]
                    and task["stage"] in {"aligned", "in_progress"}
                    and task["issuer_member_id"] == DEFAULT_OWNER_ID
                    and task["assignee_member_id"]
                    == invitation["assignee_member_id"]
                    and task["assignment_epoch"]
                    == invitation["assignment_epoch_at_issue"]
                )
                if not binding_current:
                    connection.execute(
                        """
                        UPDATE secretary_task_execution_invitations
                        SET revoked_at = COALESCE(revoked_at, ?),
                            revoke_reason = COALESCE(
                                revoke_reason, 'binding_not_current'
                            )
                        WHERE id = ?
                        """,
                        (now, invitation_id),
                    )
                    error = PocketError(409, "任务或承办人绑定已变化，邀请已失效")
                else:
                    assert task is not None
                    old_families = connection.execute(
                        """
                        SELECT id FROM secretary_task_execution_refresh_families
                        WHERE task_id = ? AND revoked_at IS NULL ORDER BY id
                        """,
                        (task["id"],),
                    ).fetchall()
                    for old_family in old_families:
                        self._revoke_task_execution_family(
                            connection,
                            old_family["id"],
                            now=now,
                            reason="rotated_by_new_invitation",
                        )
                    family_id = new_id("execution_family")
                    session_id = new_id("execution_session")
                    refresh_id = new_id("execution_refresh")
                    absolute_expires_at = invitation["capability_expires_at"]
                    access_expires_at = _iso_datetime(
                        min(
                            parse_utc(absolute_expires_at),
                            now_dt
                            + timedelta(seconds=TASK_EXECUTION_ACCESS_TTL_SECONDS),
                        )
                    )
                    idle_expires_at = _iso_datetime(
                        min(
                            parse_utc(absolute_expires_at),
                            now_dt
                            + timedelta(
                                seconds=TASK_EXECUTION_REFRESH_IDLE_TTL_SECONDS
                            ),
                        )
                    )
                    assert access_expires_at is not None and idle_expires_at is not None
                    access_token = self._task_execution_session_access_token(
                        session_id=session_id,
                        exchange_idempotency_hash=exchange_idempotency_hash,
                        exchange_request_hash=exchange_request_hash,
                    )
                    refresh_token = self._task_execution_refresh_token(
                        token_id=refresh_id,
                        family_id=family_id,
                        generation=1,
                    )
                    connection.execute(
                        """
                        INSERT INTO secretary_task_execution_refresh_families(
                            id, workspace_id, task_id, invitation_id,
                            assignee_member_id, assignment_epoch,
                            client_device_id, created_at, absolute_expires_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            family_id,
                            invitation["workspace_id"],
                            task["id"],
                            invitation_id,
                            invitation["assignee_member_id"],
                            task["assignment_epoch"],
                            payload["client_device_id"],
                            now,
                            absolute_expires_at,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO secretary_task_execution_sessions(
                            id, workspace_id, task_id, invitation_id,
                            refresh_family_id, access_generation,
                            assignee_member_id, assignment_epoch, token_hash,
                            client_device_id, exchange_idempotency_hash,
                            exchange_request_hash, assurance_method,
                            created_at, expires_at
                        ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?,
                                  'dual_channel_task_execution', ?, ?)
                        """,
                        (
                            session_id,
                            invitation["workspace_id"],
                            task["id"],
                            invitation_id,
                            family_id,
                            invitation["assignee_member_id"],
                            task["assignment_epoch"],
                            _secret_hash(access_token),
                            payload["client_device_id"],
                            exchange_idempotency_hash,
                            exchange_request_hash,
                            now,
                            access_expires_at,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO secretary_task_execution_refresh_tokens(
                            id, family_id, generation, token_hash,
                            created_at, idle_expires_at
                        ) VALUES (?, ?, 1, ?, ?, ?)
                        """,
                        (
                            refresh_id,
                            family_id,
                            _secret_hash(refresh_token),
                            now,
                            idle_expires_at,
                        ),
                    )
                    consumed = connection.execute(
                        """
                        UPDATE secretary_task_execution_invitations
                        SET used_at = ?
                        WHERE id = ? AND used_at IS NULL AND revoked_at IS NULL
                        """,
                        (now, invitation_id),
                    )
                    if consumed.rowcount != 1:
                        raise PocketError(409, "任务执行邀请状态已变化")
                    session = connection.execute(
                        """
                        SELECT * FROM secretary_task_execution_sessions WHERE id = ?
                        """,
                        (session_id,),
                    ).fetchone()
                    assert session is not None
                    self._append_event(
                        connection,
                        workspace_id=invitation["workspace_id"],
                        aggregate_type="task",
                        aggregate_id=task["id"],
                        aggregate_version=task["version"],
                        event_type="task.execution_session_issued",
                        operation="upsert",
                        actor_id=None,
                        actor_type="system",
                        device_id=payload["client_device_id"],
                        payload={
                            "actor_session_id": session_id,
                            "refresh_family_id": family_id,
                            "actor_subject_type": "task_execution_capability",
                            "actor_subject_id": session_id,
                            "on_behalf_of_member_id": invitation[
                                "assignee_member_id"
                            ],
                            "assurance_method": "dual_channel_task_execution",
                            "assignment_epoch": task["assignment_epoch"],
                            "task_id": task["id"],
                            "access_expires_at": access_expires_at,
                            "refresh_expires_at": absolute_expires_at,
                        },
                    )
                    result = {
                        "token_type": "Bearer",
                        "access_token": access_token,
                        "access_expires_at": access_expires_at,
                        "refresh_token": refresh_token,
                        "refresh_expires_at": absolute_expires_at,
                        "session": self._task_execution_session_dict(
                            session,
                            refresh_expires_at=absolute_expires_at,
                        ),
                    }
        if error is not None:
            raise error
        if result is None:
            raise PocketError(503, "暂时无法交换任务执行会话")
        return result

    def refresh_task_execution(
        self,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], str]:
        presented_token = payload["refresh_token"]
        if not presented_token.startswith("cp_task_er_"):
            raise PocketError(401, "任务执行 refresh 凭据无效或已失效")
        presented_hash = _secret_hash(presented_token)
        rotation_idempotency_hash = _secret_hash(idempotency_key)
        rotation_request_hash = _hash_request(
            {
                "refresh_token_hash": presented_hash,
                "client_device_id": payload["client_device_id"],
            }
        )
        now_dt = datetime.now(UTC)
        now = _iso_datetime(now_dt)
        assert now is not None
        error: PocketError | None = None
        result: tuple[dict[str, Any], str] | None = None
        with self.database.transaction() as connection:
            token = connection.execute(
                """
                SELECT * FROM secretary_task_execution_refresh_tokens
                WHERE token_hash = ?
                """,
                (presented_hash,),
            ).fetchone()
            family = (
                connection.execute(
                    """
                    SELECT * FROM secretary_task_execution_refresh_families
                    WHERE id = ?
                    """,
                    (token["family_id"],),
                ).fetchone()
                if token is not None
                else None
            )
            bound_task = (
                connection.execute(
                    """
                    SELECT * FROM secretary_business_tasks
                    WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                    """,
                    (family["task_id"], family["workspace_id"]),
                ).fetchone()
                if family is not None
                else None
            )
            bound_member = (
                connection.execute(
                    """
                    SELECT * FROM secretary_workspace_members
                    WHERE id = ? AND workspace_id = ? AND active = 1
                    """,
                    (family["assignee_member_id"], family["workspace_id"]),
                ).fetchone()
                if family is not None
                else None
            )
            if (
                token is None
                or family is None
                or not secrets.compare_digest(token["token_hash"], presented_hash)
                or (
                    token["used_at"] is None
                    and (
                        token["revoked_at"] is not None
                        or family["revoked_at"] is not None
                    )
                )
            ):
                error = PocketError(401, "任务执行 refresh 凭据无效或已失效")
            elif (
                token["used_at"] is None
                and (
                    parse_utc(token["idle_expires_at"]) <= now_dt
                    or parse_utc(family["absolute_expires_at"]) <= now_dt
                )
            ):
                newly_revoked = self._revoke_task_execution_family(
                    connection,
                    family["id"],
                    now=now,
                    reason="refresh_expired",
                )
                if newly_revoked:
                    self._append_task_execution_security_revoked(
                        connection,
                        family=family,
                        task=bound_task,
                        token=token,
                        reason="refresh_expired",
                    )
                error = PocketError(401, "任务执行 refresh 凭据无效或已失效")
            elif token["used_at"] is None and not secrets.compare_digest(
                family["client_device_id"].encode("utf-8"),
                payload["client_device_id"].encode("utf-8"),
            ):
                newly_revoked = self._revoke_task_execution_family(
                    connection,
                    family["id"],
                    now=now,
                    reason="refresh_device_mismatch",
                )
                if newly_revoked:
                    self._append_task_execution_security_revoked(
                        connection,
                        family=family,
                        task=bound_task,
                        token=token,
                        reason="refresh_device_mismatch",
                    )
                error = PocketError(403, "任务执行 refresh 与设备标识不匹配")
            elif token["used_at"] is not None:
                same_request = bool(
                    token["rotation_idempotency_hash"] is not None
                    and token["rotation_request_hash"] is not None
                    and secrets.compare_digest(
                        token["rotation_idempotency_hash"],
                        rotation_idempotency_hash,
                    )
                    and secrets.compare_digest(
                        token["rotation_request_hash"], rotation_request_hash
                    )
                )
                replacement_token = connection.execute(
                    """
                    SELECT * FROM secretary_task_execution_refresh_tokens
                    WHERE id = ?
                    """,
                    (token["replacement_token_id"],),
                ).fetchone()
                replacement_session = connection.execute(
                    """
                    SELECT * FROM secretary_task_execution_sessions WHERE id = ?
                    """,
                    (token["replacement_session_id"],),
                ).fetchone()
                safe_replay = bool(
                    same_request
                    and family["revoked_at"] is None
                    and bound_task is not None
                    and bound_member is not None
                    and bound_member["kind"] == "external"
                    and bound_task["stage"]
                    in {"aligned", "in_progress", "submitted"}
                    and bound_task["assignee_member_id"]
                    == family["assignee_member_id"]
                    and bound_task["assignment_epoch"]
                    == family["assignment_epoch"]
                    and self._task_execution_effective_expiry(
                        bound_task, family
                    ) > now_dt
                    and replacement_token is not None
                    and replacement_token["used_at"] is None
                    and replacement_token["revoked_at"] is None
                    and parse_utc(replacement_token["idle_expires_at"]) > now_dt
                    and replacement_session is not None
                    and replacement_session["revoked_at"] is None
                    and parse_utc(replacement_session["expires_at"]) > now_dt
                    and parse_utc(token["used_at"]) + timedelta(seconds=30)
                    > now_dt
                    and isinstance(token["rotation_response_etag"], str)
                    and isinstance(token["rotation_response_json"], str)
                )
                if not safe_replay:
                    newly_revoked = self._revoke_task_execution_family(
                        connection,
                        family["id"],
                        now=now,
                        reason="refresh_reuse_detected",
                    )
                    if newly_revoked:
                        self._append_task_execution_security_revoked(
                            connection,
                            family=family,
                            task=bound_task,
                            token=token,
                            reason="refresh_reuse_detected",
                            session_id=(
                                str(replacement_session["id"])
                                if replacement_session is not None
                                else None
                            ),
                        )
                    error = PocketError(
                        401, "检测到旧 refresh 凭据重用，执行会话已全部撤销"
                    )
                else:
                    assert replacement_token is not None
                    assert replacement_session is not None
                    access_token = self._task_execution_session_access_token(
                        session_id=replacement_session["id"],
                        exchange_idempotency_hash=replacement_session[
                            "exchange_idempotency_hash"
                        ],
                        exchange_request_hash=replacement_session[
                            "exchange_request_hash"
                        ],
                    )
                    refresh_token = self._task_execution_refresh_token(
                        token_id=replacement_token["id"],
                        family_id=family["id"],
                        generation=replacement_token["generation"],
                    )
                    if not (
                        secrets.compare_digest(
                            replacement_session["token_hash"],
                            _secret_hash(access_token),
                        )
                        and secrets.compare_digest(
                            replacement_token["token_hash"],
                            _secret_hash(refresh_token),
                        )
                    ):
                        newly_revoked = self._revoke_task_execution_family(
                            connection,
                            family["id"],
                            now=now,
                            reason="token_integrity_failure",
                        )
                        if newly_revoked:
                            self._append_task_execution_security_revoked(
                                connection,
                                family=family,
                                task=bound_task,
                                token=token,
                                reason="token_integrity_failure",
                                session_id=str(replacement_session["id"]),
                            )
                        error = PocketError(409, "任务执行 refresh 完整性校验失败")
                    else:
                        etag = str(token["rotation_response_etag"])
                        replay_task = json_loads(
                            token["rotation_response_json"], None
                        )
                        if not isinstance(replay_task, dict):
                            newly_revoked = self._revoke_task_execution_family(
                                connection,
                                family["id"],
                                now=now,
                                reason="rotation_replay_integrity_failure",
                            )
                            if newly_revoked:
                                self._append_task_execution_security_revoked(
                                    connection,
                                    family=family,
                                    task=bound_task,
                                    token=token,
                                    reason=(
                                        "rotation_replay_integrity_failure"
                                    ),
                                    session_id=str(replacement_session["id"]),
                                )
                            error = PocketError(
                                409, "任务执行 refresh 重放记录无效"
                            )
                        else:
                            result = (
                                {
                                    "token_type": "Bearer",
                                    "access_token": access_token,
                                    "access_expires_at": replacement_session[
                                        "expires_at"
                                    ],
                                    "refresh_token": refresh_token,
                                    "refresh_expires_at": family[
                                        "absolute_expires_at"
                                    ],
                                    "session": self._task_execution_session_dict(
                                        replacement_session,
                                        refresh_expires_at=family[
                                            "absolute_expires_at"
                                        ],
                                    ),
                                    "task": replay_task,
                                },
                                etag,
                            )
            else:
                task = bound_task
                member = bound_member
                binding_current = bool(
                    task is not None
                    and member is not None
                    and member["kind"] == "external"
                    and task["stage"] in {"aligned", "in_progress", "submitted"}
                    and task["assignee_member_id"] == family["assignee_member_id"]
                    and task["assignment_epoch"] == family["assignment_epoch"]
                    and self._task_execution_effective_expiry(task, family) > now_dt
                )
                if not binding_current:
                    newly_revoked = self._revoke_task_execution_family(
                        connection,
                        family["id"],
                        now=now,
                        reason="binding_not_current",
                    )
                    if newly_revoked:
                        self._append_task_execution_security_revoked(
                            connection,
                            family=family,
                            task=task,
                            token=token,
                            reason="binding_not_current",
                        )
                    error = PocketError(401, "任务执行 refresh 凭据无效或已失效")
                else:
                    assert task is not None
                    projection, etag = self._task_execution_projection(
                        connection,
                        task,
                        member_id=family["assignee_member_id"],
                    )
                    generation = int(token["generation"]) + 1
                    session_id = new_id("execution_session")
                    refresh_id = new_id("execution_refresh")
                    access_expires_at = _iso_datetime(
                        min(
                            parse_utc(family["absolute_expires_at"]),
                            now_dt
                            + timedelta(seconds=TASK_EXECUTION_ACCESS_TTL_SECONDS),
                        )
                    )
                    idle_expires_at = _iso_datetime(
                        min(
                            parse_utc(family["absolute_expires_at"]),
                            now_dt
                            + timedelta(
                                seconds=TASK_EXECUTION_REFRESH_IDLE_TTL_SECONDS
                            ),
                        )
                    )
                    assert access_expires_at is not None and idle_expires_at is not None
                    access_token = self._task_execution_session_access_token(
                        session_id=session_id,
                        exchange_idempotency_hash=rotation_idempotency_hash,
                        exchange_request_hash=rotation_request_hash,
                    )
                    refresh_token = self._task_execution_refresh_token(
                        token_id=refresh_id,
                        family_id=family["id"],
                        generation=generation,
                    )
                    connection.execute(
                        """
                        UPDATE secretary_task_execution_sessions
                        SET revoked_at = COALESCE(revoked_at, ?),
                            revoke_reason = COALESCE(
                                revoke_reason, 'access_rotated'
                            )
                        WHERE refresh_family_id = ? AND revoked_at IS NULL
                        """,
                        (now, family["id"]),
                    )
                    connection.execute(
                        """
                        INSERT INTO secretary_task_execution_sessions(
                            id, workspace_id, task_id, invitation_id,
                            refresh_family_id, access_generation,
                            assignee_member_id, assignment_epoch, token_hash,
                            client_device_id, exchange_idempotency_hash,
                            exchange_request_hash, assurance_method,
                            created_at, expires_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  'dual_channel_task_execution', ?, ?)
                        """,
                        (
                            session_id,
                            family["workspace_id"],
                            family["task_id"],
                            family["invitation_id"],
                            family["id"],
                            generation,
                            family["assignee_member_id"],
                            family["assignment_epoch"],
                            _secret_hash(access_token),
                            family["client_device_id"],
                            rotation_idempotency_hash,
                            rotation_request_hash,
                            now,
                            access_expires_at,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO secretary_task_execution_refresh_tokens(
                            id, family_id, generation, token_hash,
                            created_at, idle_expires_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            refresh_id,
                            family["id"],
                            generation,
                            _secret_hash(refresh_token),
                            now,
                            idle_expires_at,
                        ),
                    )
                    rotated = connection.execute(
                        """
                        UPDATE secretary_task_execution_refresh_tokens
                        SET used_at = ?, rotation_idempotency_hash = ?,
                            rotation_request_hash = ?, rotation_response_etag = ?,
                            rotation_response_json = ?,
                            replacement_token_id = ?, replacement_session_id = ?
                        WHERE id = ? AND used_at IS NULL AND revoked_at IS NULL
                        """,
                        (
                            now,
                            rotation_idempotency_hash,
                            rotation_request_hash,
                            etag,
                            _json(projection),
                            refresh_id,
                            session_id,
                            token["id"],
                        ),
                    )
                    if rotated.rowcount != 1:
                        raise PocketError(409, "任务执行 refresh 状态已变化")
                    session = connection.execute(
                        """
                        SELECT * FROM secretary_task_execution_sessions WHERE id = ?
                        """,
                        (session_id,),
                    ).fetchone()
                    assert session is not None
                    self._append_event(
                        connection,
                        workspace_id=family["workspace_id"],
                        aggregate_type="task",
                        aggregate_id=family["task_id"],
                        aggregate_version=task["version"],
                        event_type="task.execution_refresh_rotated",
                        operation="upsert",
                        actor_id=None,
                        actor_type="system",
                        device_id=family["client_device_id"],
                        payload={
                            "session_id": session_id,
                            "actor_session_id": session_id,
                            "refresh_family_id": family["id"],
                            "actor_subject_type": "task_execution_capability",
                            "actor_subject_id": session_id,
                            "on_behalf_of_member_id": family[
                                "assignee_member_id"
                            ],
                            "assurance_method": "task_execution_refresh",
                            "assignment_epoch": family["assignment_epoch"],
                            "generation": generation,
                            "access_generation": generation,
                            "access_expires_at": access_expires_at,
                            "refresh_expires_at": family["absolute_expires_at"],
                        },
                    )
                    result = (
                        {
                            "token_type": "Bearer",
                            "access_token": access_token,
                            "access_expires_at": access_expires_at,
                            "refresh_token": refresh_token,
                            "refresh_expires_at": family["absolute_expires_at"],
                            "session": self._task_execution_session_dict(
                                session,
                                refresh_expires_at=family["absolute_expires_at"],
                            ),
                            "task": projection,
                        },
                        etag,
                    )
        if error is not None:
            raise error
        if result is None:
            raise PocketError(503, "暂时无法刷新任务执行会话")
        return result

    def create_task_change_invitation(
        self,
        workspace_id: str,
        change_id: str,
        expected_change_version: int,
        expected_task_version: int,
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        now_dt = datetime.now(UTC)
        now = _iso_datetime(now_dt)
        expires_at = _iso_datetime(
            now_dt + timedelta(seconds=ALIGNMENT_INVITATION_TTL_SECONDS)
        )
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            replay = connection.execute(
                """
                SELECT id FROM secretary_task_change_invitations
                WHERE workspace_id = ? AND created_by = ?
                  AND change_id = ? AND creation_idempotency_key = ?
                """,
                (workspace_id, DEFAULT_OWNER_ID, change_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                raise PocketError(
                    409,
                    "此请求已创建过邀请；确认码只返回一次，请用新的请求键重新创建",
                )
            change = connection.execute(
                """
                SELECT * FROM secretary_task_changes
                WHERE id = ? AND workspace_id = ?
                """,
                (change_id, workspace_id),
            ).fetchone()
            proposal = connection.execute(
                """
                SELECT * FROM secretary_task_change_proposals
                WHERE change_id = ? AND workspace_id = ?
                """,
                (change_id, workspace_id),
            ).fetchone()
            if change is None or proposal is None:
                raise PocketError(404, "任务变更不存在")
            self._require_version(change, expected_change_version)
            task = connection.execute(
                """
                SELECT * FROM secretary_business_tasks
                WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                """,
                (change["task_id"], workspace_id),
            ).fetchone()
            if task is None:
                raise PocketError(404, "任务不存在")
            self._require_version(task, expected_task_version)
            if expected_task_version != proposal["base_task_version"]:
                raise PocketError(412, "任务版本与变更提案基线不一致")
            pending_agreement = connection.execute(
                """
                SELECT id FROM secretary_task_alignment_cases
                WHERE task_id = ? AND status = 'pending' LIMIT 1
                """,
                (task["id"],),
            ).fetchone()
            if pending_agreement is not None:
                raise PocketError(409, "任务协议待回应期间不能创建变更邀请")
            if proposal["responder_member_id"] == proposal["proposer_member_id"]:
                raise PocketError(409, "主人自办任务请直接显式回应，无需生成外部邀请")
            if not self._task_change_proposal_is_current(proposal, change, task):
                raise PocketError(409, "任务或承办人绑定已变化，该变更提案已失效")
            responder_label = self._member_label(
                connection, workspace_id, proposal["responder_member_id"]
            )
            connection.execute(
                """
                UPDATE secretary_task_change_sessions
                SET revoked_at = COALESCE(revoked_at, ?),
                    revoke_reason = COALESCE(
                        revoke_reason, 'superseded_by_new_invitation'
                    )
                WHERE change_id = ? AND revoked_at IS NULL
                """,
                (now, change_id),
            )
            connection.execute(
                """
                UPDATE secretary_task_change_invitations
                SET revoked_at = COALESCE(revoked_at, ?)
                WHERE change_id = ? AND revoked_at IS NULL
                """,
                (now, change_id),
            )
            for _attempt in range(5):
                invitation_id = new_id("change_invite")
                code = _new_alignment_code()
                try:
                    connection.execute(
                        """
                        INSERT INTO secretary_task_change_invitations(
                            id, workspace_id, change_id, task_id,
                            change_version, task_version, responder_member_id,
                            code_hash, failed_attempts, max_attempts, created_by,
                            created_device_id, creation_idempotency_key,
                            created_at, expires_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            invitation_id,
                            workspace_id,
                            change_id,
                            change["task_id"],
                            expected_change_version,
                            expected_task_version,
                            proposal["responder_member_id"],
                            _secret_hash(code),
                            ALIGNMENT_MAX_FAILED_ATTEMPTS,
                            DEFAULT_OWNER_ID,
                            device_id,
                            idempotency_key,
                            now,
                            expires_at,
                        ),
                    )
                except sqlite3.IntegrityError:
                    continue
                break
            else:
                raise PocketError(503, "暂时无法创建任务变更邀请")
            event_payload = {
                "invitation_id": invitation_id,
                "change_id": change_id,
                "task_id": change["task_id"],
                "change_version": expected_change_version,
                "task_version": expected_task_version,
                "responder_member_id": proposal["responder_member_id"],
                "expires_at": expires_at,
            }
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="task_change",
                aggregate_id=change_id,
                aggregate_version=expected_change_version,
                event_type="task.change_invitation_created",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=event_payload,
            )
            return {
                **event_payload,
                "code": code,
                "responder_label": responder_label,
                "confirmation_path": (
                    f"/api/v1/task-change-invitations/{invitation_id}"
                ),
            }

    def task_change_invitation_shell(self, invitation_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            invitation = connection.execute(
                """
                SELECT * FROM secretary_task_change_invitations WHERE id = ?
                """,
                (invitation_id,),
            ).fetchone()
            if invitation is None:
                raise PocketError(404, "任务变更邀请不可用")
            if (
                invitation["revoked_at"] is not None
                or parse_utc(invitation["expires_at"]) <= datetime.now(UTC)
            ):
                raise PocketError(410, "任务变更邀请已失效")
            if invitation["used_at"] is not None:
                raise PocketError(410, "确认码已使用，请返回刚才打开的确认页")
            return {
                "invitation_id": invitation["id"],
                "expires_at": invitation["expires_at"],
            }

    def exchange_task_change(
        self,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        invitation_id = payload["invitation_id"]
        normalized_code = _normalized_alignment_code(payload["code"])
        presented_hash = (
            _secret_hash(normalized_code) if normalized_code is not None else "0" * 64
        )
        exchange_idempotency_hash = _secret_hash(idempotency_key)
        exchange_request_hash = _hash_request(
            {
                "invitation_id": invitation_id,
                "code_hash": presented_hash,
                "client_device_id": payload["client_device_id"],
            }
        )
        now_dt = datetime.now(UTC)
        now = _iso_datetime(now_dt)
        error: PocketError | None = None
        result: dict[str, Any] | None = None
        with self.database.transaction() as connection:
            invitation = connection.execute(
                """
                SELECT * FROM secretary_task_change_invitations WHERE id = ?
                """,
                (invitation_id,),
            ).fetchone()
            existing_session = connection.execute(
                """
                SELECT * FROM secretary_task_change_sessions
                WHERE invitation_id = ?
                """,
                (invitation_id,),
            ).fetchone()
            if invitation is None:
                error = PocketError(401, "任务变更邀请凭据无效或已失效")
            else:
                code_matches = secrets.compare_digest(
                    invitation["code_hash"], presented_hash
                )
                if not code_matches:
                    if (
                        invitation["revoked_at"] is None
                        and invitation["used_at"] is None
                        and parse_utc(invitation["expires_at"]) > now_dt
                    ):
                        failed_attempts = min(
                            invitation["failed_attempts"] + 1,
                            invitation["max_attempts"],
                        )
                        revoked_at = (
                            now
                            if failed_attempts >= invitation["max_attempts"]
                            else None
                        )
                        connection.execute(
                            """
                            UPDATE secretary_task_change_invitations
                            SET failed_attempts = ?,
                                revoked_at = COALESCE(revoked_at, ?)
                            WHERE id = ?
                            """,
                            (failed_attempts, revoked_at, invitation_id),
                        )
                    error = PocketError(
                        401, "任务变更邀请凭据无效或已失效"
                    )
                elif existing_session is not None:
                    same_idempotency = secrets.compare_digest(
                        existing_session["exchange_idempotency_hash"],
                        exchange_idempotency_hash,
                    )
                    same_request = secrets.compare_digest(
                        existing_session["exchange_request_hash"],
                        exchange_request_hash,
                    )
                    if existing_session["revoked_at"] is not None:
                        error = PocketError(409, "任务变更会话已经关闭")
                    elif parse_utc(existing_session["expires_at"]) <= now_dt:
                        connection.execute(
                            """
                            UPDATE secretary_task_change_sessions
                            SET revoked_at = COALESCE(revoked_at, ?),
                                revoke_reason = COALESCE(revoke_reason, 'expired')
                            WHERE id = ? AND revoked_at IS NULL
                            """,
                            (now, existing_session["id"]),
                        )
                        error = PocketError(409, "任务变更会话已经过期")
                    elif same_idempotency and same_request:
                        change = connection.execute(
                            "SELECT * FROM secretary_task_changes WHERE id = ?",
                            (invitation["change_id"],),
                        ).fetchone()
                        proposal = connection.execute(
                            """
                            SELECT * FROM secretary_task_change_proposals
                            WHERE change_id = ?
                            """,
                            (invitation["change_id"],),
                        ).fetchone()
                        task = connection.execute(
                            """
                            SELECT * FROM secretary_business_tasks
                            WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                            """,
                            (invitation["task_id"], invitation["workspace_id"]),
                        ).fetchone()
                        binding_current = bool(
                            change is not None
                            and proposal is not None
                            and existing_session["change_id"] == change["id"]
                            and existing_session["task_id"] == task["id"]
                            if task is not None
                            else False
                        )
                        binding_current = bool(
                            binding_current
                            and proposal["responder_member_id"]
                            == existing_session["responder_member_id"]
                            and invitation["change_version"] == change["version"]
                            and invitation["task_version"] == task["version"]
                            and self._task_change_proposal_is_current(
                                proposal, change, task
                            )
                        )
                        if not binding_current:
                            connection.execute(
                                """
                                UPDATE secretary_task_change_sessions
                                SET revoked_at = COALESCE(revoked_at, ?),
                                    revoke_reason = COALESCE(
                                        revoke_reason, 'proposal_not_current'
                                    )
                                WHERE id = ?
                                """,
                                (now, existing_session["id"]),
                            )
                            error = PocketError(
                                409, "任务或变更提案已变化，无法重放会话交换"
                            )
                        else:
                            access_token = self._task_change_session_access_token(
                                session_id=existing_session["id"],
                                exchange_idempotency_hash=existing_session[
                                    "exchange_idempotency_hash"
                                ],
                                exchange_request_hash=existing_session[
                                    "exchange_request_hash"
                                ],
                            )
                            if not secrets.compare_digest(
                                existing_session["token_hash"],
                                _secret_hash(access_token),
                            ):
                                connection.execute(
                                    """
                                    UPDATE secretary_task_change_sessions
                                    SET revoked_at = COALESCE(revoked_at, ?),
                                        revoke_reason = COALESCE(
                                            revoke_reason,
                                            'session_token_integrity_failure'
                                        )
                                    WHERE id = ?
                                    """,
                                    (now, existing_session["id"]),
                                )
                                connection.execute(
                                    """
                                    UPDATE secretary_task_change_invitations
                                    SET revoked_at = COALESCE(revoked_at, ?)
                                    WHERE id = ?
                                    """,
                                    (now, invitation_id),
                                )
                                error = PocketError(
                                    409, "任务变更会话完整性校验失败"
                                )
                            else:
                                assert change is not None
                                result = {
                                    "token_type": "Bearer",
                                    "access_token": access_token,
                                    "expires_at": existing_session["expires_at"],
                                    "session": self._task_change_session_dict(
                                        existing_session
                                    ),
                                    "change": self._task_change_protocol_dict(
                                        connection, change
                                    ),
                                }
                    else:
                        connection.execute(
                            """
                            UPDATE secretary_task_change_sessions
                            SET revoked_at = COALESCE(revoked_at, ?),
                                revoke_reason = COALESCE(
                                    revoke_reason, 'unsafe_exchange_replay'
                                )
                            WHERE id = ? AND revoked_at IS NULL
                            """,
                            (now, existing_session["id"]),
                        )
                        connection.execute(
                            """
                            UPDATE secretary_task_change_invitations
                            SET revoked_at = COALESCE(revoked_at, ?)
                            WHERE id = ?
                            """,
                            (now, invitation_id),
                        )
                        error = PocketError(
                            409, "令牌交换请求冲突；旧会话已撤销"
                        )
                elif (
                    invitation["revoked_at"] is not None
                    or invitation["used_at"] is not None
                    or invitation["failed_attempts"] >= invitation["max_attempts"]
                    or parse_utc(invitation["expires_at"]) <= now_dt
                ):
                    error = PocketError(
                        401, "任务变更邀请凭据无效或已失效"
                    )
                else:
                    change = connection.execute(
                        "SELECT * FROM secretary_task_changes WHERE id = ?",
                        (invitation["change_id"],),
                    ).fetchone()
                    proposal = connection.execute(
                        """
                        SELECT * FROM secretary_task_change_proposals
                        WHERE change_id = ?
                        """,
                        (invitation["change_id"],),
                    ).fetchone()
                    task = connection.execute(
                        """
                        SELECT * FROM secretary_business_tasks
                        WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                        """,
                        (invitation["task_id"], invitation["workspace_id"]),
                    ).fetchone()
                    member = connection.execute(
                        """
                        SELECT id FROM secretary_workspace_members
                        WHERE id = ? AND workspace_id = ? AND active = 1
                        """,
                        (
                            invitation["responder_member_id"],
                            invitation["workspace_id"],
                        ),
                    ).fetchone()
                    binding_current = bool(
                        change is not None
                        and proposal is not None
                        and task is not None
                        and member is not None
                        and change["workspace_id"] == invitation["workspace_id"]
                        and change["task_id"] == invitation["task_id"]
                        and change["version"] == invitation["change_version"]
                        and task["version"] == invitation["task_version"]
                        and proposal["responder_member_id"]
                        == invitation["responder_member_id"]
                        and self._task_change_proposal_is_current(
                            proposal, change, task
                        )
                    )
                    if not binding_current:
                        connection.execute(
                            """
                            UPDATE secretary_task_change_invitations
                            SET revoked_at = COALESCE(revoked_at, ?)
                            WHERE id = ?
                            """,
                            (now, invitation_id),
                        )
                        error = PocketError(
                            409, "任务或承办人绑定已变化，邀请已失效"
                        )
                    else:
                        assert change is not None and proposal is not None
                        session_id = new_id("change_session")
                        access_token = self._task_change_session_access_token(
                            session_id=session_id,
                            exchange_idempotency_hash=exchange_idempotency_hash,
                            exchange_request_hash=exchange_request_hash,
                        )
                        expires_at = _iso_datetime(
                            min(
                                parse_utc(invitation["expires_at"]),
                                now_dt
                                + timedelta(seconds=TASK_ACCESS_SESSION_TTL_SECONDS),
                            )
                        )
                        connection.execute(
                            """
                            INSERT INTO secretary_task_change_sessions(
                                id, workspace_id, change_id, task_id,
                                invitation_id, responder_member_id, token_hash,
                                client_device_id, exchange_idempotency_hash,
                                exchange_request_hash, assurance_method,
                                created_at, expires_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                      'dual_channel_capability', ?, ?)
                            """,
                            (
                                session_id,
                                invitation["workspace_id"],
                                invitation["change_id"],
                                invitation["task_id"],
                                invitation_id,
                                invitation["responder_member_id"],
                                _secret_hash(access_token),
                                payload["client_device_id"],
                                exchange_idempotency_hash,
                                exchange_request_hash,
                                now,
                                expires_at,
                            ),
                        )
                        consumed = connection.execute(
                            """
                            UPDATE secretary_task_change_invitations
                            SET used_at = ?
                            WHERE id = ? AND used_at IS NULL AND revoked_at IS NULL
                            """,
                            (now, invitation_id),
                        )
                        if consumed.rowcount != 1:
                            raise PocketError(409, "任务变更邀请状态已变化")
                        session = connection.execute(
                            """
                            SELECT * FROM secretary_task_change_sessions WHERE id = ?
                            """,
                            (session_id,),
                        ).fetchone()
                        assert session is not None
                        self._append_event(
                            connection,
                            workspace_id=invitation["workspace_id"],
                            aggregate_type="task_change",
                            aggregate_id=change["id"],
                            aggregate_version=change["version"],
                            event_type="task.change_session_issued",
                            operation="upsert",
                            actor_id=invitation["responder_member_id"],
                            actor_type="member",
                            device_id=payload["client_device_id"],
                            payload={
                                "session": self._task_change_session_dict(session),
                                "proposal_digest": proposal["digest"],
                            },
                        )
                        result = {
                            "token_type": "Bearer",
                            "access_token": access_token,
                            "expires_at": expires_at,
                            "session": self._task_change_session_dict(session),
                            "change": self._task_change_protocol_dict(
                                connection, change
                            ),
                        }
        if error is not None:
            raise error
        if result is None:
            raise PocketError(503, "暂时无法交换任务变更会话")
        return result

    def respond_task_change(
        self,
        change_id: str,
        payload: dict[str, Any],
        principal: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"task_change.respond:{change_id}"
        request_payload = {"change_id": change_id, **payload}
        now = _iso_datetime(datetime.now(UTC))
        with self.database.transaction() as connection:
            change = connection.execute(
                "SELECT * FROM secretary_task_changes WHERE id = ?",
                (change_id,),
            ).fetchone()
            proposal = connection.execute(
                "SELECT * FROM secretary_task_change_proposals WHERE change_id = ?",
                (change_id,),
            ).fetchone()
            if (
                change is None
                or proposal is None
                or not self._principal_can_access_task_change(proposal, principal)
            ):
                raise PocketError(404, "任务变更不存在")
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=change["workspace_id"],
                actor_id=principal["idempotency_actor_id"],
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            if principal.get("auth_kind") == "task_change_session":
                session = connection.execute(
                    """
                    SELECT * FROM secretary_task_change_sessions WHERE id = ?
                    """,
                    (principal.get("session_id"),),
                ).fetchone()
                session_current = bool(
                    session is not None
                    and session["change_id"] == change_id
                    and session["task_id"] == change["task_id"]
                    and session["responder_member_id"]
                    == principal.get("member_id")
                    and secrets.compare_digest(
                        session["token_hash"],
                        principal.get("presented_token_hash", ""),
                    )
                    and secrets.compare_digest(
                        session["client_device_id"].encode("utf-8"),
                        device_id.encode("utf-8"),
                    )
                    and session["revoked_at"] is None
                    and parse_utc(session["expires_at"]) > datetime.now(UTC)
                )
                if not session_current:
                    raise PocketError(401, "任务变更会话凭据无效或已失效")
            if principal.get("replay_only"):
                raise PocketError(401, "任务变更会话已经撤销")
            if change["status"] != "proposed":
                raise PocketError(409, "任务变更已经关闭")
            self._require_version(change, payload["expected_change_version"])
            if not secrets.compare_digest(
                proposal["digest"], payload["proposal_digest"]
            ):
                raise PocketError(409, "任务变更提案摘要已变化")
            if principal.get("member_id") != proposal["responder_member_id"]:
                raise PocketError(403, "只有当前承办人才能回应任务变更")
            if (
                proposal["responder_member_id"] != proposal["proposer_member_id"]
                and principal.get("auth_kind")
                in {"owner_token", "owner_device_session"}
            ):
                raise PocketError(403, "下达人不能代替外部承办人回应")
            task = connection.execute(
                """
                SELECT * FROM secretary_business_tasks
                WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                """,
                (change["task_id"], change["workspace_id"]),
            ).fetchone()
            if task is None:
                raise PocketError(409, "任务已不存在")
            self._require_version(task, payload["expected_task_version"])
            pending_agreement = connection.execute(
                """
                SELECT id FROM secretary_task_alignment_cases
                WHERE task_id = ? AND status = 'pending' LIMIT 1
                """,
                (task["id"],),
            ).fetchone()
            if pending_agreement is not None:
                raise PocketError(409, "任务协议待回应期间不能处理任务变更")
            responder = connection.execute(
                """
                SELECT id FROM secretary_workspace_members
                WHERE id = ? AND workspace_id = ? AND active = 1
                """,
                (proposal["responder_member_id"], change["workspace_id"]),
            ).fetchone()
            if responder is None or not self._task_change_proposal_is_current(
                proposal, change, task
            ):
                raise PocketError(409, "任务或承办人绑定已变化，该变更提案已失效")
            duplicate_mutation = connection.execute(
                """
                SELECT id FROM secretary_task_change_decisions
                WHERE change_id = ? AND client_mutation_id = ?
                """,
                (change_id, payload["client_mutation_id"]),
            ).fetchone()
            if duplicate_mutation is not None:
                raise PocketError(409, "client_mutation_id 已用于其他请求")
            action = payload["decision"]
            final_status = "accepted" if action == "accept" else "rejected"
            decision_id = new_id("change_decision")
            connection.execute(
                """
                INSERT INTO secretary_task_change_decisions(
                    id, change_id, proposal_digest, action, actor_member_id,
                    actor_session_id, assurance_method, reason,
                    client_mutation_id, version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    decision_id,
                    change_id,
                    proposal["digest"],
                    action,
                    principal["member_id"],
                    principal.get("session_id"),
                    principal["assurance_method"],
                    payload.get("reason"),
                    payload["client_mutation_id"],
                    now,
                ),
            )
            task_changed = False
            affected_step_ids: list[str] = []
            if action == "accept":
                patch = json_loads(change["patch_json"], {})
                change_type = change["change_type"]
                assignments: list[str]
                values: list[Any]
                if change_type == "assignee":
                    member_id = str(patch["assignee_member_id"])
                    member_label = self._member_label(
                        connection, change["workspace_id"], member_id
                    )
                    affected_step_ids = [
                        str(row["id"])
                        for row in connection.execute(
                            """
                            SELECT id FROM secretary_task_steps
                            WHERE task_id = ? AND assignee_member_id = ?
                              AND deleted_at IS NULL ORDER BY position, id
                            """,
                            (task["id"], task["assignee_member_id"]),
                        ).fetchall()
                    ]
                    connection.execute(
                        """
                        UPDATE secretary_task_steps
                        SET assignee_member_id = ?, assignee_label = ?,
                            version = version + 1, updated_at = ?
                        WHERE task_id = ? AND assignee_member_id = ?
                          AND deleted_at IS NULL
                        """,
                        (
                            member_id,
                            member_label,
                            now,
                            task["id"],
                            task["assignee_member_id"],
                        ),
                    )
                    assignments = [
                        "assignee_member_id = ?",
                        "assignee_label = ?",
                        "assignment_epoch = assignment_epoch + 1",
                        "requires_alignment = 1",
                        "stage = 'issued'",
                        "started_at = NULL",
                        "submitted_at = NULL",
                        "accepted_at = NULL",
                        "abnormal_close_reason = NULL",
                    ]
                    values = [member_id, member_label]
                elif change_type == "due_at":
                    assignments = ["due_at = ?"]
                    values = [_iso_datetime(patch["due_at"])]
                elif change_type == "acceptance_criteria":
                    assignments = ["acceptance_criteria_json = ?"]
                    values = [_json(patch["acceptance_criteria"])]
                elif change_type == "abnormal_close":
                    assignments = [
                        "stage = 'abnormal_closed'",
                        "abnormal_close_reason = ?",
                    ]
                    values = [patch["abnormal_close_reason"]]
                else:
                    raise PocketError(409, "任务变更类型无效")
                assignments.extend(
                    ["version = version + 1", "updated_by = ?", "updated_at = ?"]
                )
                values.extend(
                    [
                        principal["member_id"],
                        now,
                        task["id"],
                        change["workspace_id"],
                        payload["expected_task_version"],
                    ]
                )
                updated_task = connection.execute(
                    f"UPDATE secretary_business_tasks SET {', '.join(assignments)} "
                    "WHERE id = ? AND workspace_id = ? AND version = ?",
                    values,
                )
                if updated_task.rowcount != 1:
                    raise PocketError(412, "任务版本已变化，请重新同步")
                task_changed = True
            updated_change = connection.execute(
                """
                UPDATE secretary_task_changes
                SET status = ?, decided_by = ?, decided_at = ?,
                    version = version + 1, updated_at = ?
                WHERE id = ? AND status = 'proposed' AND version = ?
                """,
                (
                    final_status,
                    principal["member_id"],
                    now,
                    now,
                    change_id,
                    payload["expected_change_version"],
                ),
            )
            if updated_change.rowcount != 1:
                raise PocketError(412, "任务变更版本已变化")
            connection.execute(
                """
                UPDATE secretary_task_change_invitations
                SET revoked_at = COALESCE(revoked_at, ?)
                WHERE change_id = ? AND revoked_at IS NULL
                """,
                (now, change_id),
            )
            connection.execute(
                """
                UPDATE secretary_task_change_sessions
                SET revoked_at = COALESCE(revoked_at, ?), revoke_reason = ?
                WHERE change_id = ? AND revoked_at IS NULL
                """,
                (now, f"change_{final_status}", change_id),
            )
            change = connection.execute(
                "SELECT * FROM secretary_task_changes WHERE id = ?",
                (change_id,),
            ).fetchone()
            task = connection.execute(
                "SELECT * FROM secretary_business_tasks WHERE id = ?",
                (proposal["task_id"],),
            ).fetchone()
            decision = connection.execute(
                """
                SELECT * FROM secretary_task_change_decisions WHERE id = ?
                """,
                (decision_id,),
            ).fetchone()
            assert change is not None and task is not None and decision is not None
            task_projection = {
                "id": task["id"],
                "stage": task["stage"],
                "version": task["version"],
                "assignee_member_id": task["assignee_member_id"],
                "assignee_label": task["assignee_label"],
                "assignment_epoch": task["assignment_epoch"],
                "due_at": task["due_at"],
                "acceptance_criteria": json_loads(
                    task["acceptance_criteria_json"], []
                ),
                "abnormal_close_reason": task["abnormal_close_reason"],
                "updated_at": task["updated_at"],
            }
            result = {
                "change": self._task_change_protocol_dict(connection, change),
                "decision": self._task_change_decision_dict(decision),
                "task": task_projection,
            }
            actor_type = (
                "owner"
                if principal.get("auth_kind")
                in {"owner_token", "owner_device_session"}
                else "member"
            )
            change_event_payload = dict(result)
            if affected_step_ids:
                change_event_payload["step_reassignment"] = {
                    "affected_step_ids": affected_step_ids,
                    "affected_step_count": len(affected_step_ids),
                }
            self._append_event(
                connection,
                workspace_id=change["workspace_id"],
                aggregate_type="task_change",
                aggregate_id=change_id,
                aggregate_version=change["version"],
                event_type=f"task.change_{final_status}",
                operation="upsert",
                actor_id=principal["member_id"],
                actor_type=actor_type,
                device_id=device_id,
                payload=change_event_payload,
                occurred_at=now,
            )
            if task_changed:
                self._append_event(
                    connection,
                    workspace_id=change["workspace_id"],
                    aggregate_type="task",
                    aggregate_id=task["id"],
                    aggregate_version=task["version"],
                    event_type="task.change_applied",
                    operation="upsert",
                    actor_id=principal["member_id"],
                    actor_type=actor_type,
                    device_id=device_id,
                    payload=self._task_dict(connection, task),
                )
            self._store_idempotent_response(
                connection,
                workspace_id=change["workspace_id"],
                actor_id=principal["idempotency_actor_id"],
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=result,
            )
            return result

    def decide_task_change(
        self,
        workspace_id: str,
        change_id: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
        assurance_method: str = "owner_token",
    ) -> dict[str, Any]:
        operation = f"task.change.decide:{change_id}"
        request_payload = {
            "workspace_id": workspace_id,
            "change_id": change_id,
            **payload,
        }
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            change = connection.execute(
                """
                SELECT * FROM secretary_task_changes
                WHERE id = ? AND workspace_id = ?
                """,
                (change_id, workspace_id),
            ).fetchone()
            if change is None:
                raise PocketError(404, "任务变更不存在")
            if change["status"] != "proposed":
                raise PocketError(409, "该任务变更已经处理")
            proposal = connection.execute(
                """
                SELECT * FROM secretary_task_change_proposals WHERE change_id = ?
                """,
                (change_id,),
            ).fetchone()
            decision = payload["decision"]
            if proposal is not None and decision != "cancel":
                raise PocketError(
                    409,
                    "该变更已启用双方确认协议；接受或拒绝必须由当前回应方提交",
                )
            task = connection.execute(
                "SELECT * FROM secretary_business_tasks WHERE id = ?",
                (change["task_id"],),
            ).fetchone()
            expected_version = int(payload["expected_version"])
            self._require_version(task, expected_version)
            if decision != "cancel" and task["version"] != change["base_version"]:
                raise PocketError(412, "任务已变化，请基于最新版本重新发起变更")
            if decision == "accept":
                locked_agreement = connection.execute(
                    """
                    SELECT id, status FROM secretary_task_alignment_cases
                    WHERE task_id = ? AND status IN ('pending', 'accepted')
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (task["id"],),
                ).fetchone()
                if locked_agreement is not None:
                    raise PocketError(
                        409,
                        (
                            "任务协议待回应期间不能接受任务变更"
                            if locked_agreement["status"] == "pending"
                            else "已接受协议的字段暂不支持任务变更"
                        ),
                    )
            final_status = {
                "accept": "accepted",
                "reject": "rejected",
                "cancel": "canceled",
            }[decision]
            now = utc_now()
            protocol_decision_id: str | None = None
            task_changed = False
            affected_step_ids: list[str] = []
            if proposal is not None:
                if proposal["proposer_member_id"] != DEFAULT_OWNER_ID:
                    raise PocketError(403, "只有变更提议方才能取消提案")
                self._task_change_proposal_document(proposal)
                protocol_decision_id = new_id("change_decision")
                connection.execute(
                    """
                    INSERT INTO secretary_task_change_decisions(
                        id, change_id, proposal_digest, action,
                        actor_member_id, actor_session_id, assurance_method,
                        reason, client_mutation_id, version, created_at
                    ) VALUES (?, ?, ?, 'cancel', ?, NULL, ?, ?, ?, 1, ?)
                    """,
                    (
                        protocol_decision_id,
                        change_id,
                        proposal["digest"],
                        DEFAULT_OWNER_ID,
                        assurance_method,
                        payload.get("reason"),
                        f"legacy-cancel:{_secret_hash(idempotency_key)}",
                        now,
                    ),
                )
            if decision == "accept":
                patch = json_loads(change["patch_json"], {})
                change_type = change["change_type"]
                task_update_completed = False
                if change_type == "assignee":
                    member_id = patch["assignee_member_id"]
                    member_label = self._member_label(
                        connection, workspace_id, member_id
                    )
                    affected_step_ids = [
                        str(row["id"])
                        for row in connection.execute(
                            """
                            SELECT id FROM secretary_task_steps
                            WHERE task_id = ? AND assignee_member_id = ?
                              AND deleted_at IS NULL ORDER BY position, id
                            """,
                            (task["id"], task["assignee_member_id"]),
                        ).fetchall()
                    ]
                    connection.execute(
                        """
                        UPDATE secretary_task_steps
                        SET assignee_member_id = ?, assignee_label = ?,
                            version = version + 1, updated_at = ?
                        WHERE task_id = ? AND assignee_member_id = ?
                          AND deleted_at IS NULL
                        """,
                        (
                            member_id,
                            member_label,
                            now,
                            task["id"],
                            task["assignee_member_id"],
                        ),
                    )
                    updated_task = connection.execute(
                        """
                        UPDATE secretary_business_tasks
                        SET assignee_member_id = ?, assignee_label = ?,
                            requires_alignment = 1,
                            assignment_epoch = assignment_epoch + 1,
                            version = version + 1, updated_by = ?, updated_at = ?
                        WHERE id = ? AND workspace_id = ? AND version = ?
                        """,
                        (
                            member_id,
                            member_label,
                            DEFAULT_OWNER_ID,
                            now,
                            task["id"],
                            workspace_id,
                            expected_version,
                        ),
                    )
                    if updated_task.rowcount != 1:
                        raise PocketError(412, "任务版本已变化，请重新同步")
                    task_update_completed = True
                elif change_type == "due_at":
                    connection.execute(
                        "UPDATE secretary_business_tasks SET due_at = ? WHERE id = ?",
                        (_iso_datetime(patch["due_at"]), task["id"]),
                    )
                elif change_type == "acceptance_criteria":
                    connection.execute(
                        """
                        UPDATE secretary_business_tasks
                        SET acceptance_criteria_json = ? WHERE id = ?
                        """,
                        (_json(patch["acceptance_criteria"]), task["id"]),
                    )
                elif change_type == "abnormal_close":
                    connection.execute(
                        """
                        UPDATE secretary_business_tasks
                        SET stage = 'abnormal_closed', abnormal_close_reason = ?
                        WHERE id = ?
                        """,
                        (patch["abnormal_close_reason"], task["id"]),
                    )
                if not task_update_completed:
                    updated_task = connection.execute(
                        """
                        UPDATE secretary_business_tasks
                        SET version = version + 1, updated_by = ?, updated_at = ?
                        WHERE id = ? AND workspace_id = ? AND version = ?
                        """,
                        (
                            DEFAULT_OWNER_ID,
                            now,
                            task["id"],
                            workspace_id,
                            expected_version,
                        ),
                    )
                    if updated_task.rowcount != 1:
                        raise PocketError(412, "任务版本已变化，请重新同步")
                task_changed = True
            connection.execute(
                """
                UPDATE secretary_task_changes
                SET status = ?, decided_by = ?, decided_at = ?,
                    version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (final_status, DEFAULT_OWNER_ID, now, now, change_id),
            )
            if proposal is not None:
                connection.execute(
                    """
                    UPDATE secretary_task_change_invitations
                    SET revoked_at = COALESCE(revoked_at, ?)
                    WHERE change_id = ? AND revoked_at IS NULL
                    """,
                    (now, change_id),
                )
                connection.execute(
                    """
                    UPDATE secretary_task_change_sessions
                    SET revoked_at = COALESCE(revoked_at, ?),
                        revoke_reason = COALESCE(revoke_reason, 'change_canceled')
                    WHERE change_id = ? AND revoked_at IS NULL
                    """,
                    (now, change_id),
                )
            change = connection.execute(
                "SELECT * FROM secretary_task_changes WHERE id = ?", (change_id,)
            ).fetchone()
            change_response = self._change_dict(change)
            task = connection.execute(
                "SELECT * FROM secretary_business_tasks WHERE id = ?",
                (change["task_id"],),
            ).fetchone()
            assert task is not None
            task_response = self._task_dict(connection, task)
            if protocol_decision_id is not None:
                protocol_decision = connection.execute(
                    """
                    SELECT * FROM secretary_task_change_decisions WHERE id = ?
                    """,
                    (protocol_decision_id,),
                ).fetchone()
                assert protocol_decision is not None
                change_event_payload = {
                    "change": self._task_change_protocol_dict(connection, change),
                    "decision": self._task_change_decision_dict(protocol_decision),
                    "task": task_response,
                }
            else:
                change_event_payload = dict(change_response)
            if affected_step_ids:
                change_event_payload["step_reassignment"] = {
                    "affected_step_ids": affected_step_ids,
                    "affected_step_count": len(affected_step_ids),
                }
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="task_change",
                aggregate_id=change_id,
                aggregate_version=change_response["version"],
                event_type=f"task.change_{final_status}",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=change_event_payload,
                occurred_at=now,
            )
            if task_changed:
                self._append_event(
                    connection,
                    workspace_id=workspace_id,
                    aggregate_type="task",
                    aggregate_id=task["id"],
                    aggregate_version=task_response["version"],
                    event_type="task.change_applied",
                    operation="upsert",
                    actor_id=DEFAULT_OWNER_ID,
                    device_id=device_id,
                    payload={
                        **task_response,
                        **(
                            {
                                "step_reassignment": {
                                    "affected_step_ids": affected_step_ids,
                                    "affected_step_count": len(affected_step_ids),
                                }
                            }
                            if affected_step_ids
                            else {}
                        ),
                    },
                )
            response = {"change": change_response, "task": task_response}
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
            )
            return response

    @staticmethod
    def _require_link(
        connection: sqlite3.Connection,
        workspace_id: str,
        table: str,
        resource_id: str | None,
        label: str,
    ) -> None:
        if resource_id is None:
            return
        allowed = {
            "secretary_memos",
            "secretary_business_tasks",
            "secretary_task_steps",
            "secretary_calendar_entries",
        }
        if table not in allowed:
            raise ValueError("unsupported linked resource")
        row = connection.execute(
            f"SELECT 1 FROM {table} WHERE id = ? AND workspace_id = ? "
            "AND deleted_at IS NULL",
            (resource_id, workspace_id),
        ).fetchone()
        if row is None:
            raise PocketError(422, f"关联的{label}不存在")

    def list_calendar(self, workspace_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            self._require_workspace(connection, workspace_id)
            items = [
                self._calendar_dict(row)
                for row in self._active_rows(
                    connection, "secretary_calendar_entries", workspace_id
                )
            ]
            return {"items": items, "total": len(items)}

    def create_calendar_entry(
        self,
        workspace_id: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = "calendar.create"
        request_payload = {"workspace_id": workspace_id, **payload}
        now = utc_now()
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            if payload.get("memo_id") is not None:
                raise PocketError(409, "备忘转日程必须使用专用物化接口")
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            if payload.get("step_id") is not None:
                raise PocketError(422, "步骤日程必须通过任务步骤排期接口创建")
            for key, table, label in (
                ("memo_id", "secretary_memos", "备忘"),
                ("task_id", "secretary_business_tasks", "任务"),
            ):
                self._require_link(
                    connection, workspace_id, table, payload.get(key), label
                )
            memo_id = payload.get("memo_id")
            entry_id = new_id("calendar")
            start_at = _iso_datetime(payload["start_at"])
            end_at = _iso_datetime(payload["end_at"])
            if end_at <= start_at:
                raise PocketError(422, "日程结束时间必须晚于开始时间")
            values = {
                "id": entry_id,
                "workspace_id": workspace_id,
                "memo_id": memo_id,
                "task_id": payload.get("task_id"),
                "step_id": payload.get("step_id"),
                "title": payload["title"],
                "description": payload.get("description") or "",
                "start_at_utc": start_at,
                "end_at_utc": end_at,
                "timezone": payload["timezone"],
                "all_day": int(payload.get("all_day", False)),
                "kind": payload.get("kind", "focus"),
                "domain": payload["domain"],
                "status": payload.get("status", "scheduled"),
                "attendees_json": _json(payload.get("attendees", [])),
                "external_provider": payload.get("external_provider"),
                "external_id": payload.get("external_id"),
                "version": 1,
                "created_by": DEFAULT_OWNER_ID,
                "updated_by": DEFAULT_OWNER_ID,
                "client_mutation_id": payload.get("client_mutation_id"),
                "created_at": now,
                "updated_at": now,
            }
            columns = list(values)
            connection.execute(
                f"INSERT INTO secretary_calendar_entries({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
            row = connection.execute(
                "SELECT * FROM secretary_calendar_entries WHERE id = ?", (entry_id,)
            ).fetchone()
            response = self._calendar_dict(row)
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="calendar_entry",
                aggregate_id=entry_id,
                aggregate_version=1,
                event_type="calendar.created",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=response,
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
                status_code=201,
            )
            return response

    def update_calendar_entry(
        self,
        workspace_id: str,
        entry_id: str,
        expected_version: int,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
        event_type: str = "calendar.updated",
    ) -> dict[str, Any]:
        operation = f"calendar.update:{entry_id}:{event_type}"
        request_payload = {
            "workspace_id": workspace_id,
            "entry_id": entry_id,
            "expected_version": expected_version,
            **payload,
        }
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            row = connection.execute(
                """
                SELECT * FROM secretary_calendar_entries
                WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                """,
                (entry_id, workspace_id),
            ).fetchone()
            if row is None:
                raise PocketError(404, "日程不存在")
            if row["step_id"] is not None:
                raise PocketError(409, "步骤日程必须通过任务步骤排期接口修改")
            self._require_version(row, expected_version)
            start_at = _iso_datetime(payload.get("start_at", row["start_at_utc"]))
            end_at = _iso_datetime(payload.get("end_at", row["end_at_utc"]))
            if end_at <= start_at:
                raise PocketError(422, "日程结束时间必须晚于开始时间")
            mapping: dict[str, tuple[str, Callable[[Any], Any]]] = {
                "domain": ("domain", str),
                "title": ("title", str),
                "description": ("description", lambda value: value or ""),
                "start_at": ("start_at_utc", _iso_datetime),
                "end_at": ("end_at_utc", _iso_datetime),
                "timezone": ("timezone", str),
                "all_day": ("all_day", int),
                "status": ("status", str),
                "external_provider": ("external_provider", lambda value: value),
                "external_id": ("external_id", lambda value: value),
            }
            assignments: list[str] = []
            values: list[Any] = []
            for key, (column, transform) in mapping.items():
                if key in payload:
                    assignments.append(f"{column} = ?")
                    values.append(transform(payload[key]))
            if not assignments:
                raise PocketError(422, "没有可更新的日程字段")
            now = utc_now()
            assignments.extend(
                ["version = version + 1", "updated_by = ?", "updated_at = ?"]
            )
            values.extend(
                [DEFAULT_OWNER_ID, now, entry_id, workspace_id, expected_version]
            )
            connection.execute(
                f"UPDATE secretary_calendar_entries SET {', '.join(assignments)} "
                "WHERE id = ? AND workspace_id = ? AND version = ?",
                values,
            )
            row = connection.execute(
                "SELECT * FROM secretary_calendar_entries WHERE id = ?", (entry_id,)
            ).fetchone()
            response = self._calendar_dict(row)
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="calendar_entry",
                aggregate_id=entry_id,
                aggregate_version=response["version"],
                event_type=event_type,
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=response,
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
            )
            return response

    def list_meetings(self, workspace_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            self._require_workspace(connection, workspace_id)
            items = [
                self._meeting_dict(connection, row)
                for row in self._active_rows(
                    connection, "secretary_meetings", workspace_id
                )
            ]
            return {"items": items, "total": len(items)}

    def create_meeting(
        self,
        workspace_id: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = "meeting.create"
        request_payload = {"workspace_id": workspace_id, **payload}
        now = utc_now()
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            organizer_id = payload["organizer_member_id"]
            self._member_label(connection, workspace_id, organizer_id)
            calendar_id = payload.get("calendar_entry_id")
            self._require_link(
                connection,
                workspace_id,
                "secretary_calendar_entries",
                calendar_id,
                "日程",
            )
            self._require_link(
                connection,
                workspace_id,
                "secretary_business_tasks",
                payload.get("related_task_id"),
                "任务",
            )
            start_at = _iso_datetime(payload["start_at"])
            end_at = _iso_datetime(payload["end_at"])
            if end_at <= start_at:
                raise PocketError(422, "会议结束时间必须晚于开始时间")
            meeting_id = new_id("meeting")
            values = {
                "id": meeting_id,
                "workspace_id": workspace_id,
                "calendar_entry_id": calendar_id,
                "related_task_id": payload.get("related_task_id"),
                "domain": payload["domain"],
                "title": payload["title"],
                "purpose": payload["purpose"],
                "agenda_json": _json(payload.get("agenda", [])),
                "starts_at_utc": start_at,
                "ends_at_utc": end_at,
                "timezone": payload["timezone"],
                "organizer_member_id": organizer_id,
                "location": payload.get("location"),
                "provider": payload.get("provider"),
                "external_id": payload.get("external_id"),
                "status": payload.get("status", "planned"),
                "version": 1,
                "created_by": DEFAULT_OWNER_ID,
                "updated_by": DEFAULT_OWNER_ID,
                "client_mutation_id": payload.get("client_mutation_id"),
                "created_at": now,
                "updated_at": now,
            }
            columns = list(values)
            connection.execute(
                f"INSERT INTO secretary_meetings({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
            participants = list(payload.get("participants", []))
            if not any(item["member_id"] == organizer_id for item in participants):
                participants.insert(
                    0,
                    {
                        "member_id": organizer_id,
                        "role": "organizer",
                        "rsvp": "accepted",
                        "minutes_confirmation_required": True,
                    },
                )
            for participant in participants:
                label = self._member_label(
                    connection, workspace_id, participant["member_id"]
                )
                connection.execute(
                    """
                    INSERT INTO secretary_meeting_participants(
                        meeting_id, member_id, display_name, role, rsvp,
                        minutes_confirmation_required
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        meeting_id,
                        participant["member_id"],
                        label,
                        participant.get("role", "required"),
                        participant.get("rsvp", "pending"),
                        int(participant.get("minutes_confirmation_required", True)),
                    ),
                )
            row = connection.execute(
                "SELECT * FROM secretary_meetings WHERE id = ?", (meeting_id,)
            ).fetchone()
            response = self._meeting_dict(connection, row)
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="meeting",
                aggregate_id=meeting_id,
                aggregate_version=1,
                event_type="meeting.created",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=response,
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
                status_code=201,
            )
            return response

    def update_meeting(
        self,
        workspace_id: str,
        meeting_id: str,
        expected_version: int,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"meeting.update:{meeting_id}"
        request_payload = {
            "workspace_id": workspace_id,
            "meeting_id": meeting_id,
            "expected_version": expected_version,
            **payload,
        }
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            row = connection.execute(
                """
                SELECT * FROM secretary_meetings
                WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                """,
                (meeting_id, workspace_id),
            ).fetchone()
            if row is None:
                raise PocketError(404, "会议不存在")
            self._require_version(row, expected_version)
            start_at = _iso_datetime(payload.get("start_at", row["starts_at_utc"]))
            end_at = _iso_datetime(payload.get("end_at", row["ends_at_utc"]))
            if end_at <= start_at:
                raise PocketError(422, "会议结束时间必须晚于开始时间")
            mapping: dict[str, tuple[str, Callable[[Any], Any]]] = {
                "domain": ("domain", str),
                "calendar_entry_id": ("calendar_entry_id", lambda value: value),
                "title": ("title", str),
                "purpose": ("purpose", str),
                "agenda": ("agenda_json", _json),
                "organizer_member_id": ("organizer_member_id", str),
                "start_at": ("starts_at_utc", _iso_datetime),
                "end_at": ("ends_at_utc", _iso_datetime),
                "timezone": ("timezone", str),
                "status": ("status", str),
                "location": ("location", lambda value: value),
                "provider": ("provider", lambda value: value),
                "external_id": ("external_id", lambda value: value),
            }
            if "organizer_member_id" in payload:
                self._member_label(
                    connection, workspace_id, payload["organizer_member_id"]
                )
            if "calendar_entry_id" in payload:
                self._require_link(
                    connection,
                    workspace_id,
                    "secretary_calendar_entries",
                    payload["calendar_entry_id"],
                    "日程",
                )
            assignments: list[str] = []
            values: list[Any] = []
            for key, (column, transform) in mapping.items():
                if key in payload:
                    assignments.append(f"{column} = ?")
                    values.append(transform(payload[key]))
            if not assignments:
                raise PocketError(422, "没有可更新的会议字段")
            now = utc_now()
            assignments.extend(
                ["version = version + 1", "updated_by = ?", "updated_at = ?"]
            )
            values.extend(
                [DEFAULT_OWNER_ID, now, meeting_id, workspace_id, expected_version]
            )
            connection.execute(
                f"UPDATE secretary_meetings SET {', '.join(assignments)} "
                "WHERE id = ? AND workspace_id = ? AND version = ?",
                values,
            )
            row = connection.execute(
                "SELECT * FROM secretary_meetings WHERE id = ?", (meeting_id,)
            ).fetchone()
            response = self._meeting_dict(connection, row)
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="meeting",
                aggregate_id=meeting_id,
                aggregate_version=response["version"],
                event_type="meeting.updated",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=response,
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
            )
            return response

    def create_meeting_minutes(
        self,
        workspace_id: str,
        meeting_id: str,
        expected_version: int,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"meeting.minutes.create:{meeting_id}"
        request_payload = {
            "workspace_id": workspace_id,
            "meeting_id": meeting_id,
            "expected_version": expected_version,
            **payload,
        }
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            meeting = connection.execute(
                """
                SELECT * FROM secretary_meetings
                WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
                """,
                (meeting_id, workspace_id),
            ).fetchone()
            if meeting is None:
                raise PocketError(404, "会议不存在")
            self._require_version(meeting, expected_version)
            previous_revision = connection.execute(
                """
                SELECT COALESCE(MAX(revision), 0) FROM secretary_meeting_minutes
                WHERE meeting_id = ?
                """,
                (meeting_id,),
            ).fetchone()[0]
            now = utc_now()
            connection.execute(
                """
                UPDATE secretary_meeting_minutes SET status = 'superseded',
                    version = version + 1, updated_at = ?
                WHERE meeting_id = ? AND status <> 'superseded'
                """,
                (now, meeting_id),
            )
            minutes_id = new_id("minutes")
            confirmer_ids = payload.get("required_confirmer_member_ids", [])
            status = "confirming" if confirmer_ids else payload.get("status", "draft")
            connection.execute(
                """
                INSERT INTO secretary_meeting_minutes(
                    id, workspace_id, meeting_id, revision, content, status,
                    version, created_by, client_mutation_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    minutes_id,
                    workspace_id,
                    meeting_id,
                    int(previous_revision) + 1,
                    payload["content"],
                    status,
                    DEFAULT_OWNER_ID,
                    payload.get("client_mutation_id"),
                    now,
                    now,
                ),
            )
            for member_id in confirmer_ids:
                label = self._member_label(connection, workspace_id, member_id)
                connection.execute(
                    """
                    INSERT INTO secretary_meeting_minute_confirmations(
                        minutes_id, member_id, display_name, status
                    ) VALUES (?, ?, ?, 'pending')
                    """,
                    (minutes_id, member_id, label),
                )
            connection.execute(
                """
                UPDATE secretary_meetings
                SET status = 'minutes_pending', version = version + 1,
                    updated_by = ?, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (DEFAULT_OWNER_ID, now, meeting_id, expected_version),
            )
            minutes = connection.execute(
                "SELECT * FROM secretary_meeting_minutes WHERE id = ?", (minutes_id,)
            ).fetchone()
            response = self._minutes_dict(connection, minutes)
            meeting = connection.execute(
                "SELECT * FROM secretary_meetings WHERE id = ?", (meeting_id,)
            ).fetchone()
            meeting_response = self._meeting_dict(connection, meeting)
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="meeting",
                aggregate_id=meeting_id,
                aggregate_version=meeting_response["version"],
                event_type="meeting.minutes_created",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=meeting_response,
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
                status_code=201,
            )
            return response

    def decide_meeting_minutes(
        self,
        workspace_id: str,
        minutes_id: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"meeting.minutes.decide:{minutes_id}"
        request_payload = {
            "workspace_id": workspace_id,
            "minutes_id": minutes_id,
            **payload,
        }
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            minutes = connection.execute(
                """
                SELECT * FROM secretary_meeting_minutes
                WHERE id = ? AND workspace_id = ?
                """,
                (minutes_id, workspace_id),
            ).fetchone()
            if minutes is None:
                raise PocketError(404, "会议纪要不存在")
            self._require_version(minutes, int(payload["expected_version"]))
            if minutes["status"] not in {"draft", "confirming"}:
                raise PocketError(409, "该会议纪要已结束确认")
            confirmation = connection.execute(
                """
                SELECT * FROM secretary_meeting_minute_confirmations
                WHERE minutes_id = ? AND member_id = ?
                """,
                (minutes_id, DEFAULT_OWNER_ID),
            ).fetchone()
            if confirmation is None:
                raise PocketError(403, "当前成员不在该纪要的确认名单中")
            if confirmation["status"] != "pending":
                raise PocketError(409, "当前成员已经确认或质疑该纪要")
            decision = payload["decision"]
            now = utc_now()
            confirmation_status = "confirmed" if decision == "confirm" else "disputed"
            connection.execute(
                """
                UPDATE secretary_meeting_minute_confirmations
                SET status = ?, comment = ?, decided_at = ?
                WHERE minutes_id = ? AND member_id = ?
                """,
                (
                    confirmation_status,
                    payload.get("comment"),
                    now,
                    minutes_id,
                    DEFAULT_OWNER_ID,
                ),
            )
            if decision == "dispute":
                minutes_status = "disputed"
            else:
                outstanding = connection.execute(
                    """
                    SELECT COUNT(*) FROM secretary_meeting_minute_confirmations
                    WHERE minutes_id = ? AND status <> 'confirmed'
                    """,
                    (minutes_id,),
                ).fetchone()[0]
                minutes_status = "confirmed" if outstanding == 0 else "confirming"
            connection.execute(
                """
                UPDATE secretary_meeting_minutes
                SET status = ?, version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (minutes_status, now, minutes_id),
            )
            if minutes_status == "confirmed":
                connection.execute(
                    """
                    UPDATE secretary_meetings
                    SET status = 'minutes_confirmed', version = version + 1,
                        updated_by = ?, updated_at = ? WHERE id = ?
                    """,
                    (DEFAULT_OWNER_ID, now, minutes["meeting_id"]),
                )
            minutes = connection.execute(
                "SELECT * FROM secretary_meeting_minutes WHERE id = ?", (minutes_id,)
            ).fetchone()
            response = self._minutes_dict(connection, minutes)
            meeting = connection.execute(
                "SELECT * FROM secretary_meetings WHERE id = ?",
                (minutes["meeting_id"],),
            ).fetchone()
            meeting_response = self._meeting_dict(connection, meeting)
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="meeting",
                aggregate_id=meeting["id"],
                aggregate_version=meeting_response["version"],
                event_type=f"meeting.minutes_{minutes_status}",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=meeting_response,
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
            )
            return response

    @staticmethod
    def _document_row(
        connection: sqlite3.Connection, workspace_id: str, document_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM secretary_documents
            WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL
            """,
            (document_id, workspace_id),
        ).fetchone()
        if row is None:
            raise PocketError(404, "文档不存在")
        return row

    def _validate_document_audience(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        access_scope: str,
        viewer_member_ids: list[str],
    ) -> list[str]:
        normalized = list(dict.fromkeys(viewer_member_ids))
        if access_scope == "restricted" and not normalized:
            raise PocketError(422, "restricted 文档必须指定至少一个可看成员")
        if access_scope != "restricted" and normalized:
            raise PocketError(422, "只有 restricted 文档可以指定可看成员")
        for member_id in normalized:
            self._member_label(connection, workspace_id, member_id)
        return normalized

    @staticmethod
    def _require_source_item(
        connection: sqlite3.Connection,
        source_item_id: str | None,
        access_scope: str,
    ) -> None:
        if source_item_id is None:
            return
        row = connection.execute(
            "SELECT state FROM items WHERE id = ?", (source_item_id,)
        ).fetchone()
        if row is None:
            raise PocketError(422, "关联的文档库文件不存在")
        # Items are governed by the global knowledge-library ACL and can later
        # transition to ``ready``.  Allowing an inbox item to be attached to a
        # private document would therefore create a time-of-check/time-of-use
        # bypass when that item is subsequently made Agent-searchable.
        if access_scope != "workspace":
            raise PocketError(
                409,
                "文档库文件使用 workspace/Agent 全库权限，不能关联到非 workspace 文档",
            )

    @staticmethod
    def _effective_document_viewers(
        connection: sqlite3.Connection, row: sqlite3.Row
    ) -> set[str]:
        if row["access_scope"] == "owner_only":
            return {DEFAULT_OWNER_ID}
        if row["access_scope"] == "workspace":
            members = connection.execute(
                """
                SELECT id FROM secretary_workspace_members
                WHERE workspace_id = ? AND active = 1
                """,
                (row["workspace_id"],),
            ).fetchall()
            return {str(member["id"]) for member in members}
        return {
            DEFAULT_OWNER_ID,
            *json_loads(row["viewer_member_ids_json"], []),
        }

    @staticmethod
    def _render_template(content: str, variables: dict[str, str]) -> str:
        placeholders = set(TEMPLATE_PLACEHOLDER.findall(content))
        provided = set(variables)
        missing = sorted(placeholders - provided)
        extra = sorted(provided - placeholders)
        if missing:
            raise PocketError(422, f"模板变量缺失：{', '.join(missing)}")
        if extra:
            raise PocketError(422, f"模板未声明变量：{', '.join(extra)}")
        rendered = TEMPLATE_PLACEHOLDER.sub(
            lambda match: variables[match.group(1)], content
        )
        # Reject malformed template syntax and placeholder-shaped variable
        # values.  The generated document must never silently retain a token
        # that a later renderer or automation could interpret as unresolved.
        if "{{" in rendered or "}}" in rendered:
            raise PocketError(422, "模板包含无法识别或未解析的变量标记")
        return rendered

    @staticmethod
    def _utf16_excerpt(content: str, start: int, end: int) -> str:
        """Slice using JavaScript/React Native UTF-16 code-unit offsets."""

        encoded = content.encode("utf-16-le")
        if end * 2 > len(encoded):
            raise PocketError(422, "片段范围超过文档内容长度")
        try:
            return encoded[start * 2 : end * 2].decode("utf-16-le")
        except UnicodeDecodeError as error:
            raise PocketError(422, "片段偏移不能切分 Unicode 字符") from error

    def _insert_document(
        self,
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        payload: dict[str, Any],
        kind: str,
        content: str,
        origin_template_id: str | None = None,
        origin_template_version: int | None = None,
        template_variables: dict[str, str] | None = None,
    ) -> sqlite3.Row:
        access_scope = payload.get("access_scope", "owner_only")
        viewer_member_ids = self._validate_document_audience(
            connection,
            workspace_id,
            access_scope,
            payload.get("viewer_member_ids", []),
        )
        self._require_source_item(
            connection, payload.get("source_item_id"), access_scope
        )
        now = utc_now()
        document_id = new_id("document")
        status = "review_pending" if kind in {"contract", "work_report"} else "draft"
        values = {
            "id": document_id,
            "workspace_id": workspace_id,
            "source_item_id": payload.get("source_item_id"),
            "origin_template_id": origin_template_id,
            "origin_template_version": origin_template_version,
            "domain": payload["domain"],
            "kind": kind,
            "title": payload["title"],
            "content": content,
            "mime_type": payload.get("mime_type", "text/markdown"),
            "storage_ref": payload.get("storage_ref"),
            "source_json": _json(payload.get("source") or {}),
            "access_scope": access_scope,
            "viewer_member_ids_json": _json(viewer_member_ids),
            "status": status,
            "tags_json": _json(payload.get("tags", [])),
            "template_variables_json": _json(template_variables or {}),
            "version": 1,
            "created_by": DEFAULT_OWNER_ID,
            "updated_by": DEFAULT_OWNER_ID,
            "client_mutation_id": payload.get("client_mutation_id"),
            "created_at": now,
            "updated_at": now,
        }
        columns = list(values)
        connection.execute(
            f"INSERT INTO secretary_documents({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            tuple(values[column] for column in columns),
        )
        return connection.execute(
            "SELECT * FROM secretary_documents WHERE id = ?", (document_id,)
        ).fetchone()

    def list_documents(self, workspace_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            self._require_workspace(connection, workspace_id)
            items = [
                self._document_summary_dict(row)
                for row in self._active_rows(
                    connection, "secretary_documents", workspace_id
                )
            ]
            return {"items": items, "total": len(items)}

    def get_document(self, workspace_id: str, document_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            self._require_workspace(connection, workspace_id)
            row = self._document_row(connection, workspace_id, document_id)
            return self._document_dict(connection, row)

    def create_document(
        self,
        workspace_id: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = "document.create"
        request_payload = {"workspace_id": workspace_id, **payload}
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            row = self._insert_document(
                connection,
                workspace_id=workspace_id,
                payload=payload,
                kind=payload.get("kind", "general"),
                content=payload["content"],
            )
            response = self._document_dict(connection, row)
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="document",
                aggregate_id=row["id"],
                aggregate_version=1,
                event_type="document.created",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=self._document_summary_dict(row),
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
                status_code=201,
            )
            return response

    def update_document(
        self,
        workspace_id: str,
        document_id: str,
        expected_version: int,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"document.update:{document_id}"
        request_payload = {
            "workspace_id": workspace_id,
            "document_id": document_id,
            "expected_version": expected_version,
            **payload,
        }
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            row = self._document_row(connection, workspace_id, document_id)
            self._require_version(row, expected_version)
            if row["status"] == "archived":
                raise PocketError(409, "已归档文档不能修改")
            access_scope = payload.get("access_scope", row["access_scope"])
            viewer_member_ids = payload.get(
                "viewer_member_ids",
                json_loads(row["viewer_member_ids_json"], []),
            )
            viewer_member_ids = self._validate_document_audience(
                connection, workspace_id, access_scope, viewer_member_ids
            )
            source_item_id = payload.get("source_item_id", row["source_item_id"])
            self._require_source_item(connection, source_item_id, access_scope)
            mapping: dict[str, tuple[str, Callable[[Any], Any]]] = {
                "domain": ("domain", str),
                "title": ("title", str),
                "content": ("content", str),
                "mime_type": ("mime_type", str),
                "storage_ref": ("storage_ref", lambda value: value),
                "source_item_id": ("source_item_id", lambda value: value),
                "source": ("source_json", _json),
                "tags": ("tags_json", _json),
                "access_scope": ("access_scope", str),
                "viewer_member_ids": ("viewer_member_ids_json", _json),
            }
            assignments: list[str] = []
            values: list[Any] = []
            for key, (column, transform) in mapping.items():
                if key in payload:
                    assignments.append(f"{column} = ?")
                    values.append(transform(payload[key]))
            if "viewer_member_ids" not in payload and "access_scope" in payload:
                assignments.append("viewer_member_ids_json = ?")
                values.append(_json(viewer_member_ids))
            if not assignments:
                raise PocketError(422, "没有可更新的文档字段")
            content_changed = "content" in payload
            audience_changed = bool(
                {"access_scope", "viewer_member_ids"}.intersection(payload)
            )
            if content_changed:
                status = (
                    "review_pending"
                    if row["kind"] in {"contract", "work_report"}
                    else "draft"
                )
                assignments.append("status = ?")
                values.append(status)
            now = utc_now()
            assignments.extend(
                ["version = version + 1", "updated_by = ?", "updated_at = ?"]
            )
            values.extend(
                [DEFAULT_OWNER_ID, now, document_id, workspace_id, expected_version]
            )
            updated = connection.execute(
                f"UPDATE secretary_documents SET {', '.join(assignments)} "
                "WHERE id = ? AND workspace_id = ? AND version = ?",
                values,
            )
            if updated.rowcount != 1:
                raise PocketError(412, "文档版本已变化，请重新同步")
            if content_changed or audience_changed:
                connection.execute(
                    """
                    UPDATE secretary_document_excerpts SET revoked_at = ?
                    WHERE document_id = ? AND revoked_at IS NULL
                    """,
                    (now, document_id),
                )
            row = self._document_row(connection, workspace_id, document_id)
            response = self._document_dict(connection, row)
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="document",
                aggregate_id=document_id,
                aggregate_version=response["version"],
                event_type="document.updated",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=self._document_summary_dict(row),
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
            )
            return response

    def review_document(
        self,
        workspace_id: str,
        document_id: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"document.review:{document_id}"
        request_payload = {
            "workspace_id": workspace_id,
            "document_id": document_id,
            **payload,
        }
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            row = self._document_row(connection, workspace_id, document_id)
            self._require_version(row, int(payload["expected_version"]))
            if row["status"] == "archived":
                raise PocketError(409, "已归档文档不能审阅")
            if row["kind"] != payload["review_type"]:
                raise PocketError(422, "review_type 必须与文档 kind 一致")
            now = utc_now()
            review_id = new_id("document_review")
            connection.execute(
                """
                INSERT INTO secretary_document_reviews(
                    id, workspace_id, document_id, document_version,
                    review_type, summary, conclusion, findings_json,
                    reviewer_member_id, version, client_mutation_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    review_id,
                    workspace_id,
                    document_id,
                    row["version"],
                    payload["review_type"],
                    payload["summary"],
                    payload["conclusion"],
                    _json(payload.get("findings", [])),
                    DEFAULT_OWNER_ID,
                    payload.get("client_mutation_id"),
                    now,
                ),
            )
            updated = connection.execute(
                """
                UPDATE secretary_documents
                SET status = 'reviewed', version = version + 1,
                    updated_by = ?, updated_at = ?
                WHERE id = ? AND workspace_id = ? AND version = ?
                """,
                (
                    DEFAULT_OWNER_ID,
                    now,
                    document_id,
                    workspace_id,
                    row["version"],
                ),
            )
            if updated.rowcount != 1:
                raise PocketError(412, "文档版本已变化，请重新同步")
            row = self._document_row(connection, workspace_id, document_id)
            response = self._document_dict(connection, row)
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="document",
                aggregate_id=document_id,
                aggregate_version=response["version"],
                event_type="document.reviewed",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=self._document_summary_dict(row),
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
                status_code=201,
            )
            return response

    def generate_document(
        self,
        workspace_id: str,
        template_id: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"document.generate:{template_id}"
        request_payload = {
            "workspace_id": workspace_id,
            "template_id": template_id,
            **payload,
        }
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            template = self._document_row(connection, workspace_id, template_id)
            self._require_version(template, int(payload["expected_version"]))
            if template["kind"] != "template":
                raise PocketError(422, "只有 template 文档可以用于生成")
            if template["status"] == "archived":
                raise PocketError(409, "已归档模板不能生成文档")
            variables = payload.get("variables", {})
            content = self._render_template(template["content"], variables)
            generated_payload = {
                **payload,
                "domain": payload.get("domain") or template["domain"],
                "mime_type": template["mime_type"],
                "source_item_id": None,
                "source": {
                    "source_kind": "document",
                    "source_ref": template_id,
                    "authority": "authoritative",
                },
            }
            row = self._insert_document(
                connection,
                workspace_id=workspace_id,
                payload=generated_payload,
                kind=payload.get("kind", "general"),
                content=content,
                origin_template_id=template_id,
                origin_template_version=template["version"],
                template_variables=variables,
            )
            response = self._document_dict(connection, row)
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="document",
                aggregate_id=row["id"],
                aggregate_version=1,
                event_type="document.generated",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=self._document_summary_dict(row),
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
                status_code=201,
            )
            return response

    def archive_document(
        self,
        workspace_id: str,
        document_id: str,
        expected_version: int,
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"document.archive:{document_id}"
        request_payload = {
            "workspace_id": workspace_id,
            "document_id": document_id,
            "expected_version": expected_version,
        }
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            row = self._document_row(connection, workspace_id, document_id)
            self._require_version(row, expected_version)
            if row["status"] == "archived":
                raise PocketError(409, "文档已经归档")
            now = utc_now()
            updated = connection.execute(
                """
                UPDATE secretary_documents
                SET status = 'archived', version = version + 1,
                    updated_by = ?, updated_at = ?
                WHERE id = ? AND workspace_id = ? AND version = ?
                """,
                (
                    DEFAULT_OWNER_ID,
                    now,
                    document_id,
                    workspace_id,
                    expected_version,
                ),
            )
            if updated.rowcount != 1:
                raise PocketError(412, "文档版本已变化，请重新同步")
            connection.execute(
                """
                UPDATE secretary_document_excerpts SET revoked_at = ?
                WHERE document_id = ? AND revoked_at IS NULL
                """,
                (now, document_id),
            )
            row = self._document_row(connection, workspace_id, document_id)
            response = self._document_dict(connection, row)
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="document",
                aggregate_id=document_id,
                aggregate_version=response["version"],
                event_type="document.archived",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=self._document_summary_dict(row),
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
            )
            return response

    def create_document_excerpt(
        self,
        workspace_id: str,
        document_id: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        device_id: str,
    ) -> dict[str, Any]:
        operation = f"document.excerpt.create:{document_id}"
        request_payload = {
            "workspace_id": workspace_id,
            "document_id": document_id,
            **payload,
        }
        with self.database.transaction() as connection:
            self._require_workspace(connection, workspace_id)
            cached, request_hash = self._idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
            if cached is not None:
                return cached
            row = self._document_row(connection, workspace_id, document_id)
            self._require_version(row, int(payload["expected_version"]))
            if row["status"] == "archived":
                raise PocketError(409, "已归档文档不能创建浏览片段")
            start = int(payload["start_offset"])
            end = int(payload["end_offset"])
            excerpt_content = self._utf16_excerpt(row["content"], start, end)
            viewer_member_ids = list(dict.fromkeys(payload["viewer_member_ids"]))
            for member_id in viewer_member_ids:
                self._member_label(connection, workspace_id, member_id)
            allowed = self._effective_document_viewers(connection, row)
            unauthorized = sorted(set(viewer_member_ids) - allowed)
            if unauthorized:
                raise PocketError(
                    403,
                    f"片段受众超出文档可看范围：{', '.join(unauthorized)}",
                )
            now = utc_now()
            excerpt_id = new_id("document_excerpt")
            connection.execute(
                """
                INSERT INTO secretary_document_excerpts(
                    id, workspace_id, document_id, source_document_version,
                    title, content, start_offset, end_offset,
                    viewer_member_ids_json, version, created_by,
                    client_mutation_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    excerpt_id,
                    workspace_id,
                    document_id,
                    row["version"],
                    payload["title"],
                    excerpt_content,
                    start,
                    end,
                    _json(viewer_member_ids),
                    DEFAULT_OWNER_ID,
                    payload.get("client_mutation_id"),
                    now,
                ),
            )
            updated = connection.execute(
                """
                UPDATE secretary_documents
                SET version = version + 1, updated_by = ?, updated_at = ?
                WHERE id = ? AND workspace_id = ? AND version = ?
                """,
                (
                    DEFAULT_OWNER_ID,
                    now,
                    document_id,
                    workspace_id,
                    row["version"],
                ),
            )
            if updated.rowcount != 1:
                raise PocketError(412, "文档版本已变化，请重新同步")
            row = self._document_row(connection, workspace_id, document_id)
            response = self._document_dict(connection, row)
            self._append_event(
                connection,
                workspace_id=workspace_id,
                aggregate_type="document",
                aggregate_id=document_id,
                aggregate_version=response["version"],
                event_type="document.excerpt_created",
                operation="upsert",
                actor_id=DEFAULT_OWNER_ID,
                device_id=device_id,
                payload=self._document_summary_dict(row),
            )
            self._store_idempotent_response(
                connection,
                workspace_id=workspace_id,
                actor_id=DEFAULT_OWNER_ID,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response=response,
                status_code=201,
            )
            return response

    def list_document_excerpts(
        self,
        workspace_id: str,
        document_id: str,
        viewer_member_id: str,
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            self._require_workspace(connection, workspace_id)
            row = self._document_row(connection, workspace_id, document_id)
            self._member_label(connection, workspace_id, viewer_member_id)
            if viewer_member_id not in self._effective_document_viewers(
                connection, row
            ):
                raise PocketError(403, "该成员无权浏览此文档")
            excerpts = connection.execute(
                """
                SELECT * FROM secretary_document_excerpts
                WHERE document_id = ? AND revoked_at IS NULL
                ORDER BY created_at DESC, id DESC
                """,
                (document_id,),
            ).fetchall()
            items = [
                self._document_excerpt_dict(excerpt)
                for excerpt in excerpts
                if viewer_member_id == DEFAULT_OWNER_ID
                or viewer_member_id in json_loads(excerpt["viewer_member_ids_json"], [])
            ]
            return {
                "document_id": document_id,
                "viewer_member_id": viewer_member_id,
                "items": items,
                "total": len(items),
            }
