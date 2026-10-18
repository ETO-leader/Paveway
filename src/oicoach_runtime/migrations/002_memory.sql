CREATE TABLE memory_notes (
    workflow_id TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    key TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('fact','preference','summary','hypothesis')),
    pinned INTEGER NOT NULL CHECK (pinned IN (0,1)),
    source_event_id TEXT REFERENCES events(event_id),
    expires_at TEXT,
    version INTEGER NOT NULL CHECK (version >= 1),
    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workflow_id, task_id, key)
);
CREATE INDEX memory_scope ON memory_notes(workflow_id, task_id, deleted, updated_at);
