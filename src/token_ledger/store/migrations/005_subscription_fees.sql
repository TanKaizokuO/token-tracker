-- Subscription payments are historical account-level facts, not request valuations.
CREATE TABLE subscription_fees (
    fee_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    account_alias TEXT NOT NULL,
    access_route TEXT NOT NULL,
    paid_date TEXT NOT NULL,
    amount REAL NOT NULL CHECK (amount >= 0),
    currency TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_subscription_fees_date ON subscription_fees (paid_date);
