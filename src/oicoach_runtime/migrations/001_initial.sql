CREATE TABLE runtime_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO runtime_metadata VALUES ('api_version', '1');

CREATE TABLE session_bindings (
    binding_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    session_dir TEXT NOT NULL UNIQUE,
    session_id TEXT,
    session_file TEXT,
    workspace TEXT NOT NULL,
    workflow_revision TEXT NOT NULL,
    last_successful_run_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('new','ready','blocked','uncertain')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (workflow_id, task_id)
);

CREATE TABLE events (
    event_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('wake','manual','maintenance')),
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 4096),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json) AND length(CAST(payload_json AS BLOB)) <= 65536),
    schedule_mode TEXT NOT NULL CHECK (schedule_mode IN ('at','after_turn')),
    not_before_utc TEXT,
    relative_seconds REAL,
    anchor_run_id TEXT REFERENCES runs(run_id),
    status TEXT NOT NULL CHECK (status IN (
        'waiting_anchor','queued','leased','running','succeeded','failed',
        'cancelled','blocked','uncertain','dead_letter'
    )),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1),
    lease_owner TEXT,
    lease_expires_utc TEXT,
    run_id TEXT REFERENCES runs(run_id),
    dedupe_key TEXT NOT NULL CHECK (length(dedupe_key) BETWEEN 1 AND 256),
    request_fingerprint TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    last_error_code TEXT,
    last_error_message TEXT,
    UNIQUE (workflow_id, task_id, dedupe_key),
    CHECK ((schedule_mode = 'at' AND not_before_utc IS NOT NULL AND relative_seconds IS NULL AND anchor_run_id IS NULL)
        OR (schedule_mode = 'after_turn' AND relative_seconds >= 0 AND relative_seconds IS NOT NULL AND anchor_run_id IS NOT NULL)),
    CHECK (status != 'waiting_anchor' OR (schedule_mode = 'after_turn' AND not_before_utc IS NULL)),
    CHECK (status NOT IN ('queued','leased','running','succeeded') OR not_before_utc IS NOT NULL)
);
CREATE INDEX events_due ON events(not_before_utc, created_at, event_id) WHERE status = 'queued';

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(event_id),
    binding_id TEXT REFERENCES session_bindings(binding_id),
    request_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('leased','running','succeeded','failed','blocked','uncertain')),
    dispatch_intent_at TEXT,
    acknowledged_at TEXT,
    prompt_result_status TEXT,
    settled_at TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    possible_side_effects INTEGER NOT NULL DEFAULT 0 CHECK (possible_side_effects IN (0,1)),
    agent_pid INTEGER,
    agent_start_identity TEXT,
    agent_exit_code INTEGER,
    error_code TEXT,
    error_message TEXT,
    CHECK (status != 'succeeded' OR (prompt_result_status IS NOT NULL AND prompt_result_status = 'completed' AND settled_at IS NOT NULL))
);
-- A database backstop for the single foreground run invariant.
CREATE UNIQUE INDEX one_active_run ON runs((1)) WHERE status IN ('leased','running');

CREATE TABLE audit_log (
    sequence INTEGER PRIMARY KEY,
    timestamp_utc TEXT NOT NULL,
    component TEXT NOT NULL,
    event_id TEXT,
    run_id TEXT,
    transition TEXT NOT NULL,
    error_code TEXT
);
