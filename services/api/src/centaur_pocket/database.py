from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);

INSERT INTO schema_meta(version)
SELECT 1
WHERE NOT EXISTS (SELECT 1 FROM schema_meta);

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('folder')),
    name TEXT NOT NULL,
    config_json TEXT NOT NULL,
    schedule TEXT NOT NULL DEFAULT 'manual',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_sync_at TEXT
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    scanned_count INTEGER NOT NULL DEFAULT 0,
    imported_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    unchanged_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    task_count INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_runs_source_started
ON sync_runs(source_id, started_at DESC);

CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL UNIQUE,
    first_source_id TEXT REFERENCES sources(id) ON DELETE SET NULL,
    origin_uri TEXT NOT NULL,
    file_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    title TEXT NOT NULL,
    text_content TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    source_modified_at TEXT,
    state TEXT NOT NULL
        CHECK (state IN ('inbox', 'needs_review', 'ready', 'archived')),
    category TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_state_updated
ON items(state, updated_at DESC);

CREATE TABLE IF NOT EXISTS item_sources (
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    origin_uri TEXT NOT NULL,
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    source_modified_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (source_id, origin_uri)
);

CREATE INDEX IF NOT EXISTS idx_item_sources_item
ON item_sources(item_id);

CREATE TABLE IF NOT EXISTS governance_tasks (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'applied', 'skipped')),
    proposal_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_governance_tasks_status_created
ON governance_tasks(status, created_at DESC);

CREATE TABLE IF NOT EXISTS governance_actions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES governance_tasks(id) ON DELETE CASCADE,
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    action TEXT NOT NULL CHECK (action IN ('apply', 'skip', 'undo')),
    before_json TEXT,
    after_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_governance_actions_task_created
ON governance_actions(task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS activity_events (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    resource_type TEXT,
    resource_id TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_activity_events_created
ON activity_events(created_at DESC);

CREATE TABLE IF NOT EXISTS idempotency_records (
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (operation, idempotency_key)
);

CREATE VIRTUAL TABLE IF NOT EXISTS item_fts USING fts5(
    item_id UNINDEXED,
    title,
    body,
    tags,
    category,
    tokenize = 'unicode61'
);
"""


MIGRATION_V2 = """
BEGIN IMMEDIATE;

CREATE TABLE sources_v2 (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('folder', 'wechat_visible_web')),
    provider TEXT,
    name TEXT NOT NULL,
    config_json TEXT NOT NULL,
    schedule TEXT NOT NULL DEFAULT 'manual',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_sync_at TEXT,
    CHECK (
        (kind = 'folder' AND provider IS NULL)
        OR
        (kind = 'wechat_visible_web' AND provider = 'wechat_visible_web')
    )
);

INSERT INTO sources_v2(
    id, kind, provider, name, config_json, schedule, enabled,
    created_at, updated_at, last_sync_at
)
SELECT
    id, kind, NULL, name, config_json, schedule, enabled,
    created_at, updated_at, last_sync_at
FROM sources;

DROP TABLE sources;
ALTER TABLE sources_v2 RENAME TO sources;

CREATE TABLE collector_pairings (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    revoked_at TEXT
);

CREATE INDEX idx_collector_pairings_source_created
ON collector_pairings(source_id, created_at DESC);

CREATE TABLE collector_tokens (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    client_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);

CREATE INDEX idx_collector_tokens_source_created
ON collector_tokens(source_id, created_at DESC);

CREATE TABLE collector_rate_limits (
    token_id TEXT PRIMARY KEY REFERENCES collector_tokens(id) ON DELETE CASCADE,
    window_started_at TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE collector_batches (
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_id, batch_id)
);

CREATE TABLE source_coverage_sessions (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    browser_session_id TEXT NOT NULL,
    state TEXT NOT NULL,
    browser_version TEXT,
    extension_version TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    current_conversation_id TEXT,
    current_conversation_name TEXT,
    unread_conversation_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    last_heartbeat_at TEXT NOT NULL,
    ended_at TEXT,
    UNIQUE (source_id, browser_session_id)
);

CREATE INDEX idx_coverage_sessions_source_heartbeat
ON source_coverage_sessions(source_id, last_heartbeat_at DESC);

CREATE TABLE source_gaps (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    coverage_session_id TEXT REFERENCES source_coverage_sessions(id)
        ON DELETE SET NULL,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_source_gaps_source_started
ON source_gaps(source_id, started_at DESC);

CREATE TABLE ingest_events (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    collector_token_id TEXT REFERENCES collector_tokens(id) ON DELETE SET NULL,
    provider_event_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE (source_id, provider_event_key)
);

CREATE INDEX idx_ingest_events_source_received
ON ingest_events(source_id, received_at DESC);

CREATE TABLE im_conversations (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    provider_conversation_id TEXT NOT NULL,
    display_name TEXT,
    conversation_type TEXT NOT NULL
        CHECK (conversation_type IN ('direct', 'group', 'unknown')),
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_id, provider_conversation_id)
);

CREATE INDEX idx_im_conversations_source_updated
ON im_conversations(source_id, updated_at DESC);

CREATE TABLE conversation_policies (
    conversation_id TEXT PRIMARY KEY
        REFERENCES im_conversations(id) ON DELETE CASCADE,
    agent_enabled INTEGER NOT NULL DEFAULT 0 CHECK (agent_enabled IN (0, 1)),
    retention_days INTEGER NOT NULL DEFAULT 365
        CHECK (retention_days BETWEEN 1 AND 3650),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE im_messages (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL REFERENCES im_conversations(id)
        ON DELETE CASCADE,
    ingest_event_id TEXT NOT NULL REFERENCES ingest_events(id)
        ON DELETE CASCADE,
    provider_msgid TEXT NOT NULL,
    sender_provider_id TEXT,
    sender_display_name TEXT,
    direction TEXT NOT NULL
        CHECK (direction IN ('incoming', 'outgoing', 'system', 'unknown')),
    message_type TEXT NOT NULL
        CHECK (message_type IN (
            'text', 'image', 'voice', 'file', 'video', 'system', 'other'
        )),
    text_content TEXT,
    content_hash TEXT,
    displayed_time_text TEXT,
    sent_at TEXT,
    observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (source_id, provider_msgid)
);

CREATE INDEX idx_im_messages_conversation_observed
ON im_messages(conversation_id, observed_at DESC, id DESC);

CREATE INDEX idx_im_messages_source_content_hash
ON im_messages(source_id, content_hash);

UPDATE schema_meta SET version = 2;
COMMIT;
"""

MIGRATION_V3 = """
BEGIN IMMEDIATE;

ALTER TABLE im_messages
ADD COLUMN authority TEXT NOT NULL DEFAULT 'observed'
    CHECK (authority IN ('authoritative', 'observed', 'user_provided'));

ALTER TABLE im_messages
ADD COLUMN acquisition TEXT NOT NULL DEFAULT 'rendered_dom';

CREATE TABLE im_accounts (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    provider_account_id TEXT NOT NULL,
    display_name TEXT,
    is_self INTEGER NOT NULL DEFAULT 0 CHECK (is_self IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_id, provider_account_id)
);

CREATE TABLE im_identities (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    provider_identity_id TEXT NOT NULL,
    display_name TEXT,
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_id, provider_identity_id)
);

CREATE TABLE im_conversation_members (
    conversation_id TEXT NOT NULL REFERENCES im_conversations(id) ON DELETE CASCADE,
    identity_id TEXT NOT NULL REFERENCES im_identities(id) ON DELETE CASCADE,
    display_name TEXT,
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    PRIMARY KEY (conversation_id, identity_id)
);

CREATE TABLE im_message_versions (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES im_messages(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK (event_type IN ('created', 'edited', 'recalled')),
    text_content TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_im_message_versions_message_observed
ON im_message_versions(message_id, observed_at DESC);

CREATE TABLE im_message_references (
    message_id TEXT NOT NULL REFERENCES im_messages(id) ON DELETE CASCADE,
    related_provider_msgid TEXT NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN ('reply', 'quote', 'forward', 'thread')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (message_id, related_provider_msgid, relation)
);

CREATE TABLE im_attachments (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES im_messages(id) ON DELETE CASCADE,
    provider_media_id TEXT,
    media_type TEXT NOT NULL,
    file_name TEXT,
    mime_type TEXT,
    size_bytes INTEGER,
    content_hash TEXT,
    encrypted_blob_uri TEXT,
    parse_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (parse_status IN ('pending', 'processing', 'ready', 'failed', 'unavailable')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_im_attachments_message
ON im_attachments(message_id);

CREATE VIRTUAL TABLE im_message_fts USING fts5(
    message_id UNINDEXED,
    body,
    sender,
    conversation,
    tokenize = 'unicode61'
);

INSERT INTO im_message_fts(message_id, body, sender, conversation)
SELECT message.id, COALESCE(message.text_content, ''),
       COALESCE(message.sender_display_name, ''),
       COALESCE(conversation.display_name, '')
FROM im_messages message
JOIN im_conversations conversation ON conversation.id = message.conversation_id
WHERE message.text_content IS NOT NULL;

CREATE TRIGGER im_message_fts_after_insert
AFTER INSERT ON im_messages
WHEN NEW.text_content IS NOT NULL
BEGIN
    INSERT INTO im_message_fts(message_id, body, sender, conversation)
    SELECT NEW.id, NEW.text_content, COALESCE(NEW.sender_display_name, ''),
           COALESCE(conversation.display_name, '')
    FROM im_conversations conversation WHERE conversation.id = NEW.conversation_id;
END;

CREATE TRIGGER im_message_fts_after_delete
AFTER DELETE ON im_messages
BEGIN
    DELETE FROM im_message_fts WHERE message_id = OLD.id;
END;

CREATE TRIGGER im_message_fts_after_update
AFTER UPDATE OF text_content, sender_display_name, conversation_id ON im_messages
BEGIN
    DELETE FROM im_message_fts WHERE message_id = OLD.id;
    INSERT INTO im_message_fts(message_id, body, sender, conversation)
    SELECT NEW.id, NEW.text_content, COALESCE(NEW.sender_display_name, ''),
           COALESCE(conversation.display_name, '')
    FROM im_conversations conversation
    WHERE conversation.id = NEW.conversation_id AND NEW.text_content IS NOT NULL;
END;

CREATE TABLE knowledge_candidates (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL REFERENCES im_conversations(id) ON DELETE CASCADE,
    claim_type TEXT NOT NULL CHECK (claim_type IN ('decision', 'commitment', 'task')),
    text_content TEXT NOT NULL,
    speaker TEXT,
    explicitness TEXT NOT NULL DEFAULT 'explicit'
        CHECK (explicitness IN ('explicit', 'inferred')),
    authority TEXT NOT NULL DEFAULT 'observed'
        CHECK (authority IN ('authoritative', 'observed', 'user_provided', 'inferred')),
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    status TEXT NOT NULL DEFAULT 'provisional'
        CHECK (status IN ('provisional', 'confirmed', 'dismissed', 'superseded')),
    valid_from TEXT,
    valid_to TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX idx_knowledge_candidates_status_updated
ON knowledge_candidates(status, updated_at DESC);

CREATE TABLE knowledge_evidence (
    candidate_id TEXT NOT NULL REFERENCES knowledge_candidates(id) ON DELETE CASCADE,
    message_id TEXT NOT NULL REFERENCES im_messages(id) ON DELETE CASCADE,
    evidence_role TEXT NOT NULL DEFAULT 'primary',
    excerpt TEXT,
    PRIMARY KEY (candidate_id, message_id)
);

CREATE VIRTUAL TABLE knowledge_fts USING fts5(
    candidate_id UNINDEXED,
    body,
    speaker,
    claim_type,
    tokenize = 'unicode61'
);

CREATE TRIGGER knowledge_fts_after_insert
AFTER INSERT ON knowledge_candidates
BEGIN
    INSERT INTO knowledge_fts(candidate_id, body, speaker, claim_type)
    VALUES (NEW.id, NEW.text_content, COALESCE(NEW.speaker, ''), NEW.claim_type);
END;

CREATE TRIGGER knowledge_fts_after_delete
AFTER DELETE ON knowledge_candidates
BEGIN
    DELETE FROM knowledge_fts WHERE candidate_id = OLD.id;
END;

CREATE TRIGGER knowledge_fts_after_update
AFTER UPDATE OF text_content, speaker, claim_type ON knowledge_candidates
BEGIN
    DELETE FROM knowledge_fts WHERE candidate_id = OLD.id;
    INSERT INTO knowledge_fts(candidate_id, body, speaker, claim_type)
    VALUES (NEW.id, NEW.text_content, COALESCE(NEW.speaker, ''), NEW.claim_type);
END;

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    source_id TEXT REFERENCES sources(id) ON DELETE CASCADE,
    idempotency_key TEXT UNIQUE,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'retry_wait', 'completed', 'failed', 'canceled')),
    attempt INTEGER NOT NULL DEFAULT 0,
    run_after TEXT NOT NULL,
    lease_until TEXT,
    heartbeat_at TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX idx_jobs_runnable
ON jobs(status, run_after, created_at);

CREATE TABLE ragflow_projections (
    candidate_id TEXT PRIMARY KEY REFERENCES knowledge_candidates(id) ON DELETE CASCADE,
    dataset_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    projected_at TEXT NOT NULL,
    UNIQUE (dataset_id, chunk_id)
);

UPDATE schema_meta SET version = 3;
COMMIT;
"""

MIGRATION_V4 = """
BEGIN IMMEDIATE;

CREATE TABLE mobile_devices (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    platform TEXT NOT NULL CHECK (platform IN ('android', 'ios')),
    app_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE INDEX idx_mobile_devices_created
ON mobile_devices(created_at DESC);

CREATE TABLE mobile_pairings (
    id TEXT PRIMARY KEY,
    code_hash TEXT NOT NULL UNIQUE CHECK (length(code_hash) = 64),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    claimed_at TEXT,
    claimed_device_id TEXT REFERENCES mobile_devices(id) ON DELETE SET NULL
);

CREATE INDEX idx_mobile_pairings_expires
ON mobile_pairings(expires_at);

CREATE TABLE mobile_sessions (
    id TEXT PRIMARY KEY,
    mobile_device_id TEXT NOT NULL REFERENCES mobile_devices(id) ON DELETE CASCADE,
    access_token_hash TEXT NOT NULL UNIQUE
        CHECK (length(access_token_hash) = 64),
    access_expires_at TEXT NOT NULL,
    refresh_token_hash TEXT NOT NULL UNIQUE
        CHECK (length(refresh_token_hash) = 64),
    refresh_expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);

CREATE UNIQUE INDEX idx_mobile_one_active_session
ON mobile_sessions(mobile_device_id)
WHERE revoked_at IS NULL;

CREATE INDEX idx_mobile_sessions_access_expiry
ON mobile_sessions(access_expires_at);

CREATE INDEX idx_mobile_sessions_refresh_expiry
ON mobile_sessions(refresh_expires_at);

UPDATE schema_meta SET version = 4;
COMMIT;
"""

MIGRATION_V5 = """
BEGIN IMMEDIATE;

CREATE TABLE sources_v5 (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('folder', 'wechat_visible_web', 'rss')),
    provider TEXT,
    name TEXT NOT NULL,
    config_json TEXT NOT NULL,
    schedule TEXT NOT NULL DEFAULT 'manual',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_sync_at TEXT,
    CHECK (
        (kind = 'folder' AND provider IS NULL)
        OR
        (kind = 'wechat_visible_web' AND provider = 'wechat_visible_web')
        OR
        (kind = 'rss' AND provider = 'rss')
    )
);

INSERT INTO sources_v5(
    id, kind, provider, name, config_json, schedule, enabled,
    created_at, updated_at, last_sync_at
)
SELECT
    id, kind, provider, name, config_json, schedule, enabled,
    created_at, updated_at, last_sync_at
FROM sources;

DROP TABLE sources;
ALTER TABLE sources_v5 RENAME TO sources;

CREATE TABLE reliable_source_candidates (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    organization_origin TEXT NOT NULL,
    feed_url TEXT NOT NULL,
    trust_reason TEXT NOT NULL,
    scope TEXT NOT NULL,
    review_due_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'confirmed', 'dismissed')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_by_device_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    confirmed_at TEXT,
    dismissed_at TEXT,
    dismiss_reason TEXT,
    reliable_source_id TEXT
        REFERENCES reliable_sources(id) ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,
    CHECK (
        (status = 'pending' AND confirmed_at IS NULL AND dismissed_at IS NULL)
        OR (status = 'confirmed' AND confirmed_at IS NOT NULL
            AND dismissed_at IS NULL AND reliable_source_id IS NOT NULL)
        OR (status = 'dismissed' AND dismissed_at IS NOT NULL
            AND confirmed_at IS NULL AND reliable_source_id IS NULL)
    )
);

CREATE INDEX idx_reliable_candidates_status_created
ON reliable_source_candidates(status, created_at DESC);

CREATE TABLE reliable_sources (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL UNIQUE REFERENCES sources(id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL UNIQUE
        REFERENCES reliable_source_candidates(id) ON DELETE RESTRICT,
    display_name TEXT NOT NULL,
    organization_origin TEXT NOT NULL,
    feed_url TEXT NOT NULL UNIQUE,
    trust_reason TEXT NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_collected_at TEXT
);

CREATE TABLE reliable_collection_plans (
    id TEXT PRIMARY KEY,
    reliable_source_id TEXT NOT NULL UNIQUE
        REFERENCES reliable_sources(id) ON DELETE CASCADE,
    schedule TEXT NOT NULL CHECK (schedule IN ('manual', 'daily')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    review_due_at TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    last_collected_at TEXT,
    next_run_at TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    last_failure_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_reliable_plans_due
ON reliable_collection_plans(enabled, schedule, next_run_at);

CREATE TABLE reliable_feed_snapshots (
    id TEXT PRIMARY KEY,
    reliable_source_id TEXT NOT NULL
        REFERENCES reliable_sources(id) ON DELETE CASCADE,
    request_url TEXT NOT NULL,
    resolved_ip TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed', 'not_modified', 'failed')),
    content_hash TEXT,
    etag TEXT,
    last_modified TEXT,
    byte_count INTEGER NOT NULL DEFAULT 0,
    entry_count INTEGER NOT NULL DEFAULT 0,
    new_entry_count INTEGER NOT NULL DEFAULT 0,
    changed_entry_count INTEGER NOT NULL DEFAULT 0,
    duplicate_entry_count INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    collected_at TEXT NOT NULL
);

CREATE INDEX idx_reliable_snapshots_source_collected
ON reliable_feed_snapshots(reliable_source_id, collected_at DESC);

CREATE TABLE reliable_entries (
    id TEXT PRIMARY KEY,
    reliable_source_id TEXT NOT NULL
        REFERENCES reliable_sources(id) ON DELETE CASCADE,
    identity_key TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    url_trust TEXT NOT NULL CHECK (url_trust IN (
        'feed_claimed_unverified',
        'feed_url_fallback_missing',
        'feed_url_fallback_invalid'
    )),
    publisher TEXT NOT NULL,
    published_at TEXT,
    current_version INTEGER NOT NULL DEFAULT 1 CHECK (current_version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (reliable_source_id, identity_key)
);

CREATE INDEX idx_reliable_entries_source_updated
ON reliable_entries(reliable_source_id, updated_at DESC);

CREATE TABLE reliable_entry_versions (
    id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES reliable_entries(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version >= 1),
    content_hash TEXT NOT NULL,
    snapshot_id TEXT NOT NULL
        REFERENCES reliable_feed_snapshots(id) ON DELETE RESTRICT,
    item_id TEXT NOT NULL UNIQUE REFERENCES items(id) ON DELETE RESTRICT,
    governance_task_id TEXT NOT NULL UNIQUE
        REFERENCES governance_tasks(id) ON DELETE RESTRICT,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    UNIQUE (entry_id, version),
    UNIQUE (entry_id, content_hash)
);

CREATE INDEX idx_reliable_entry_versions_entry
ON reliable_entry_versions(entry_id, version DESC);

CREATE TABLE reliable_domain_idempotency (
    actor_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(actor_id, operation, idempotency_key)
);

UPDATE schema_meta SET version = 5;
COMMIT;
"""

MIGRATION_V6 = """
BEGIN IMMEDIATE;

CREATE TABLE sources_v6 (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (
        kind IN ('folder', 'wechat_visible_web', 'rss', 'outlook_mail')
    ),
    provider TEXT,
    name TEXT NOT NULL,
    config_json TEXT NOT NULL,
    schedule TEXT NOT NULL DEFAULT 'manual',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_sync_at TEXT,
    CHECK (
        (kind = 'folder' AND provider IS NULL)
        OR
        (kind = 'wechat_visible_web' AND provider = 'wechat_visible_web')
        OR
        (kind = 'rss' AND provider = 'rss')
        OR
        (kind = 'outlook_mail' AND provider = 'microsoft_graph')
    )
);

INSERT INTO sources_v6(
    id, kind, provider, name, config_json, schedule, enabled,
    created_at, updated_at, last_sync_at
)
SELECT
    id, kind, provider, name, config_json, schedule, enabled,
    created_at, updated_at, last_sync_at
FROM sources;

DROP TABLE sources;
ALTER TABLE sources_v6 RENAME TO sources;

CREATE TABLE outlook_accounts (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL UNIQUE REFERENCES sources(id) ON DELETE RESTRICT,
    account_label TEXT NOT NULL,
    client_id TEXT NOT NULL,
    tenant TEXT NOT NULL,
    scopes_json TEXT NOT NULL CHECK(json_valid(scopes_json)),
    mailbox_fingerprint TEXT NOT NULL CHECK(length(mailbox_fingerprint) = 40),
    status TEXT NOT NULL CHECK (
        status IN ('connected', 'action_required', 'disconnected')
    ),
    sync_enabled INTEGER NOT NULL DEFAULT 1 CHECK(sync_enabled IN (0, 1)),
    sync_interval_minutes INTEGER NOT NULL DEFAULT 15
        CHECK(sync_interval_minutes BETWEEN 5 AND 1440),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    connected_at TEXT NOT NULL,
    last_sync_at TEXT,
    next_sync_at TEXT,
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_outlook_one_live_account
ON outlook_accounts((1))
WHERE status IN ('connected', 'action_required');

CREATE INDEX idx_outlook_accounts_due
ON outlook_accounts(status, sync_enabled, next_sync_at);

CREATE TABLE outlook_device_authorizations (
    id TEXT PRIMARY KEY,
    account_label TEXT NOT NULL,
    client_id TEXT NOT NULL,
    tenant TEXT NOT NULL,
    scopes_json TEXT NOT NULL CHECK(json_valid(scopes_json)),
    device_flow_ciphertext TEXT,
    verification_uri TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'connected', 'denied', 'expired', 'failed', 'canceled')
    ),
    interval_seconds INTEGER NOT NULL CHECK(interval_seconds BETWEEN 1 AND 60),
    next_poll_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    created_by_device_id TEXT NOT NULL,
    account_id TEXT REFERENCES outlook_accounts(id) ON DELETE SET NULL,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE UNIQUE INDEX idx_outlook_one_pending_authorization
ON outlook_device_authorizations((1))
WHERE status = 'pending';

CREATE INDEX idx_outlook_authorizations_created
ON outlook_device_authorizations(created_at DESC);

CREATE TABLE outlook_credentials (
    account_id TEXT PRIMARY KEY
        REFERENCES outlook_accounts(id) ON DELETE CASCADE,
    token_ciphertext TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE outlook_sync_cursors (
    account_id TEXT NOT NULL
        REFERENCES outlook_accounts(id) ON DELETE CASCADE,
    folder_key TEXT NOT NULL CHECK(folder_key IN ('inbox')),
    cursor_kind TEXT NOT NULL CHECK(cursor_kind IN ('next', 'delta')),
    cursor_ciphertext TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id, folder_key)
);

CREATE TABLE outlook_sync_runs (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL
        REFERENCES outlook_accounts(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (
        status IN ('running', 'completed', 'partial', 'failed')
    ),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    page_count INTEGER NOT NULL DEFAULT 0 CHECK(page_count >= 0),
    changed_count INTEGER NOT NULL DEFAULT 0 CHECK(changed_count >= 0),
    deleted_count INTEGER NOT NULL DEFAULT 0 CHECK(deleted_count >= 0),
    candidate_count INTEGER NOT NULL DEFAULT 0 CHECK(candidate_count >= 0),
    error_code TEXT
);

CREATE INDEX idx_outlook_sync_runs_account_started
ON outlook_sync_runs(account_id, started_at DESC);

CREATE UNIQUE INDEX idx_outlook_one_running_sync
ON outlook_sync_runs(account_id)
WHERE status = 'running';

CREATE TABLE outlook_messages (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL
        REFERENCES outlook_accounts(id) ON DELETE CASCADE,
    graph_message_id TEXT NOT NULL,
    conversation_id TEXT,
    internet_message_id TEXT,
    subject TEXT NOT NULL,
    sender_json TEXT NOT NULL CHECK(json_valid(sender_json)),
    to_recipients_json TEXT NOT NULL CHECK(json_valid(to_recipients_json)),
    cc_recipients_json TEXT NOT NULL CHECK(json_valid(cc_recipients_json)),
    body_preview TEXT NOT NULL,
    importance TEXT NOT NULL CHECK(importance IN ('low', 'normal', 'high')),
    is_read INTEGER NOT NULL CHECK(is_read IN (0, 1)),
    has_attachments INTEGER NOT NULL CHECK(has_attachments IN (0, 1)),
    received_at TEXT,
    sent_at TEXT,
    change_key TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'deleted')),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    UNIQUE(account_id, graph_message_id)
);

CREATE INDEX idx_outlook_messages_account_received
ON outlook_messages(account_id, status, received_at DESC, id DESC);

CREATE TABLE outlook_task_candidates (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL UNIQUE
        REFERENCES outlook_messages(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    purpose TEXT NOT NULL,
    objective TEXT NOT NULL,
    strategy TEXT NOT NULL,
    acceptance_criteria_json TEXT NOT NULL CHECK(json_valid(acceptance_criteria_json)),
    priority TEXT NOT NULL CHECK(priority IN ('normal', 'high', 'critical')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'confirmed', 'dismissed')),
    task_id TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX idx_outlook_task_candidates_status_created
ON outlook_task_candidates(status, created_at DESC);

CREATE TABLE outlook_local_drafts (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL
        REFERENCES outlook_accounts(id) ON DELETE CASCADE,
    reply_to_message_id TEXT REFERENCES outlook_messages(id) ON DELETE SET NULL,
    to_recipients_json TEXT NOT NULL CHECK(json_valid(to_recipients_json)),
    cc_recipients_json TEXT NOT NULL CHECK(json_valid(cc_recipients_json)),
    subject TEXT NOT NULL,
    body_text TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK(length(content_hash) = 64),
    status TEXT NOT NULL DEFAULT 'editing' CHECK (
        status IN (
            'editing', 'preparing', 'prepared', 'sending',
            'sent', 'uncertain', 'canceled'
        )
    ),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE INDEX idx_outlook_drafts_account_updated
ON outlook_local_drafts(account_id, status, updated_at DESC);

CREATE UNIQUE INDEX idx_outlook_one_active_reply_draft
ON outlook_local_drafts(account_id, reply_to_message_id)
WHERE reply_to_message_id IS NOT NULL
  AND status IN ('editing', 'preparing', 'prepared', 'sending', 'uncertain');

CREATE TABLE outlook_send_intents (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES outlook_local_drafts(id) ON DELETE CASCADE,
    draft_version INTEGER NOT NULL CHECK(draft_version >= 1),
    content_hash TEXT NOT NULL CHECK(length(content_hash) = 64),
    marker_value TEXT NOT NULL UNIQUE,
    remote_graph_id TEXT,
    remote_snapshot_hash TEXT CHECK(
        remote_snapshot_hash IS NULL OR length(remote_snapshot_hash) = 64
    ),
    remote_change_key TEXT,
    sender_address TEXT CHECK(
        sender_address IS NULL OR length(sender_address) BETWEEN 3 AND 320
    ),
    status TEXT NOT NULL CHECK (
        status IN (
            'preparing', 'ready', 'prepare_uncertain', 'sending',
            'verifying', 'sent', 'send_uncertain', 'failed',
            'canceled', 'expired'
        )
    ),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    expires_at TEXT NOT NULL,
    send_started_at TEXT,
    verified_at TEXT,
    sent_at TEXT,
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(
        status NOT IN (
            'ready', 'sending', 'verifying', 'send_uncertain', 'sent'
        ) OR sender_address IS NOT NULL
    )
);

CREATE UNIQUE INDEX idx_outlook_one_active_send_intent
ON outlook_send_intents(draft_id);

CREATE TABLE outlook_archived_attachments (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES outlook_messages(id) ON DELETE RESTRICT,
    attachment_ref TEXT NOT NULL,
    file_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    content_hash TEXT NOT NULL CHECK(length(content_hash) = 64),
    archive_relpath TEXT NOT NULL,
    item_id TEXT REFERENCES items(id) ON DELETE RESTRICT,
    document_id TEXT,
    archived_at TEXT NOT NULL,
    UNIQUE(message_id, attachment_ref)
);

CREATE INDEX idx_outlook_archived_attachments_message
ON outlook_archived_attachments(message_id, archived_at DESC);

CREATE TABLE outlook_domain_idempotency (
    actor_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL CHECK(length(payload_hash) = 64),
    response_json TEXT NOT NULL CHECK(json_valid(response_json)),
    created_at TEXT NOT NULL,
    PRIMARY KEY(actor_id, operation, idempotency_key)
);

UPDATE schema_meta SET version = 6;
COMMIT;
"""

LATEST_SCHEMA_VERSION = 6


class Database:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA_V1)
            row = connection.execute(
                "SELECT MAX(version) AS version FROM schema_meta"
            ).fetchone()
            version = int(row["version"] if row is not None else 0)
            if version > LATEST_SCHEMA_VERSION:
                raise RuntimeError(
                    "数据库版本高于当前服务支持的版本："
                    f"{version} > {LATEST_SCHEMA_VERSION}"
                )
            if version < 2:
                self._migrate_to_v2(connection)
                version = 2
            if version < 3:
                connection.executescript(MIGRATION_V3)
                version = 3
            if version < 4:
                connection.executescript(MIGRATION_V4)
                version = 4
            if version < 5:
                self._migrate_to_v5(connection)
                version = 5
            if version < 6:
                self._migrate_to_v6(connection)

            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError("数据库升级后外键检查失败")

    @staticmethod
    def _migrate_to_v2(connection: sqlite3.Connection) -> None:
        # Rebuilding sources is required because SQLite cannot alter its CHECK
        # constraint in-place. Keeping foreign keys off only for this atomic
        # migration preserves every existing folder/source relationship.
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.executescript(MIGRATION_V2)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("数据库升级后外键检查失败")

    @staticmethod
    def _migrate_to_v5(connection: sqlite3.Connection) -> None:
        # As in v2, the source-kind CHECK must be rebuilt. Foreign keys are
        # disabled only around the atomic table replacement, then checked
        # before application traffic is accepted.
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.executescript(MIGRATION_V5)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("可靠信源迁移后外键检查失败")

    @staticmethod
    def _migrate_to_v6(connection: sqlite3.Connection) -> None:
        # Outlook adds another governed source kind. Keep the source-table
        # replacement atomic, then verify every pre-existing relationship.
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.executescript(MIGRATION_V6)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("Outlook 邮箱迁移后外键检查失败")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=10,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()
