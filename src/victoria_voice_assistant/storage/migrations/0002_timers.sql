CREATE TABLE IF NOT EXISTS timers (
    id TEXT PRIMARY KEY,
    device TEXT NOT NULL,
    name TEXT NOT NULL,
    duration_s REAL NOT NULL,
    ends_at REAL NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('running', 'ringing')),
    ring_started_at REAL,
    created_at REAL NOT NULL
);
