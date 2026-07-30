from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
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


class Database:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

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
