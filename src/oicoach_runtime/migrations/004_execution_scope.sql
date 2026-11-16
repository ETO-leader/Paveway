ALTER TABLE runs ADD COLUMN execution_scope TEXT;
UPDATE runs SET execution_scope = (
    SELECT e.workflow_id || char(31) || coalesce(
        (SELECT json_extract(m.value, '$.binding_task_id') FROM runtime_metadata m
         WHERE m.key = 'native-wakeup-config:v1:' || e.event_id), e.task_id)
    FROM events e WHERE e.event_id = runs.event_id
);
DROP INDEX IF EXISTS one_active_run;
CREATE INDEX IF NOT EXISTS runs_active ON runs(status) WHERE status IN ('leased','running');
CREATE UNIQUE INDEX IF NOT EXISTS runs_active_scope ON runs(execution_scope)
    WHERE status IN ('leased','running') AND execution_scope IS NOT NULL;
