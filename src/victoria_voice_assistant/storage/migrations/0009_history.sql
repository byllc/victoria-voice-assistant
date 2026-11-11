-- Every request to the assistant, all kept (no archive, nothing deleted; Bill, 2026-11-11).
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,              -- Unix time the request began
    device TEXT NOT NULL,
    room TEXT,
    request TEXT NOT NULL,         -- what was said or typed
    tools TEXT NOT NULL DEFAULT '[]',  -- JSON: [{"name": ..., "arguments": {...}}]
    reply TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS history_at ON history (at);
