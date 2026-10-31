CREATE TABLE IF NOT EXISTS spend (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    month TEXT NOT NULL,
    pool TEXT,
    agent TEXT NOT NULL,
    model TEXT NOT NULL,
    task_id TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    reason TEXT,
    decider_choice TEXT,
    confidence REAL
);
CREATE INDEX IF NOT EXISTS spend_pool_month ON spend (pool, month);
