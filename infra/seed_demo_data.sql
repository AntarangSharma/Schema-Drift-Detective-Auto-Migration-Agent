-- Seed demo schema for the Day-3 thin slice.
-- Mirrors the dbt project's sources.yml exactly so lineage walks work.

CREATE TABLE IF NOT EXISTS source_raw.customers (
    customer_id  BIGSERIAL    PRIMARY KEY,
    name         TEXT         NOT NULL,
    email        TEXT         NOT NULL UNIQUE,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS source_raw.orders (
    order_id     BIGSERIAL    PRIMARY KEY,
    customer_id  BIGINT       NOT NULL REFERENCES source_raw.customers(customer_id),
    amount       NUMERIC(10,2) NOT NULL,
    status       TEXT         NOT NULL CHECK (status IN ('pending', 'paid', 'cancelled', 'refunded')),
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_orders_customer ON source_raw.orders (customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_status   ON source_raw.orders (status);

-- A handful of rows so downstream models have something to chew on.
INSERT INTO source_raw.customers (name, email) VALUES
    ('Ada Lovelace',  'ada@example.com'),
    ('Alan Turing',   'alan@example.com'),
    ('Grace Hopper',  'grace@example.com')
ON CONFLICT (email) DO NOTHING;

INSERT INTO source_raw.orders (customer_id, amount, status) VALUES
    (1, 49.99,  'paid'),
    (1, 19.50,  'paid'),
    (2, 199.00, 'pending'),
    (3, 12.00,  'paid'),
    (3, 75.50,  'cancelled')
ON CONFLICT DO NOTHING;
