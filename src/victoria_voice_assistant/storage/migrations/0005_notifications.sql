CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('result', 'question', 'error', 'notice')),
    headline TEXT NOT NULL,
    summary TEXT NOT NULL,
    full TEXT NOT NULL,
    priority TEXT NOT NULL CHECK (priority IN ('urgent', 'normal', 'low')),
    target TEXT NOT NULL,
    style TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'delivered', 'acknowledged', 'expired')),
    task_id TEXT,
    mentioned INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS notifications_target_state ON notifications (target, state);
