CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint TEXT PRIMARY KEY,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    client_id TEXT NOT NULL,
    device TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_sent_at REAL
);
