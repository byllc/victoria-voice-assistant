-- Projects and chat contexts (feature 20261112-023414): a context is one saved chat transcript,
-- a project groups contexts. Archived, never deleted (Bill, 2026-11-12).
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at REAL NOT NULL,
    archived_at REAL
);
CREATE TABLE IF NOT EXISTS contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects (id),
    name TEXT,                     -- from the first request until renamed
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    archived_at REAL
);
CREATE INDEX IF NOT EXISTS contexts_project ON contexts (project_id, updated_at);
CREATE TABLE IF NOT EXISTS context_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    context_id INTEGER NOT NULL REFERENCES contexts (id),
    at REAL NOT NULL,
    role TEXT NOT NULL,            -- user | assistant
    text TEXT NOT NULL,
    tools TEXT NOT NULL DEFAULT '[]'   -- on a user message: the tools its request called (names)
);
CREATE INDEX IF NOT EXISTS context_messages_context ON context_messages (context_id, id);
-- Which context each browser had open, so a refresh reopens it.
CREATE TABLE IF NOT EXISTS client_contexts (
    client_id TEXT PRIMARY KEY,
    context_id INTEGER NOT NULL REFERENCES contexts (id)
);
ALTER TABLE history ADD COLUMN context_id INTEGER;
