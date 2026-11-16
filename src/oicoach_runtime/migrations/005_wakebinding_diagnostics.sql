ALTER TABLE wake_bindings ADD COLUMN last_failure_at TEXT;
ALTER TABLE wake_bindings ADD COLUMN last_failure_code TEXT;
ALTER TABLE wake_bindings ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE wake_bindings ADD COLUMN next_attempt_at TEXT;
CREATE INDEX IF NOT EXISTS wake_bindings_retry ON wake_bindings(next_attempt_at);
