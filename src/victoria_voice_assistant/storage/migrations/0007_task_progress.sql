CREATE TABLE IF NOT EXISTS task_progress (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    at REAL NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS task_progress_task ON task_progress (task_id, at);
