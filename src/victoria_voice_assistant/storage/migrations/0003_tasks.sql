CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    model TEXT,
    device TEXT NOT NULL,
    request TEXT NOT NULL,
    mode TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'waiting', 'finished', 'failed', 'cancelled', 'timed_out')),
    result TEXT,
    error TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_created_at ON tasks (created_at);
