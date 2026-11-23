CREATE TABLE dataset_version (
    dataset_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source text NOT NULL CHECK (btrim(source) <> ''),
    orders_sha256 text NOT NULL CHECK (orders_sha256 ~ '^[0-9a-f]{64}$'),
    customers_sha256 text NOT NULL CHECK (customers_sha256 ~ '^[0-9a-f]{64}$'),
    imported_at timestamptz NOT NULL DEFAULT now(),
    covered_months date[] NOT NULL DEFAULT '{}',
    has_carrier_handoff boolean NOT NULL,
    order_count bigint NOT NULL DEFAULT 0 CHECK (order_count >= 0),
    quality_counts jsonb NOT NULL DEFAULT '{}',
    UNIQUE (source, orders_sha256, customers_sha256)
);

CREATE TABLE order_fact (
    dataset_id uuid NOT NULL REFERENCES dataset_version (dataset_id),
    order_id text NOT NULL,
    order_status text NOT NULL,
    purchased_at timestamp NOT NULL,
    estimated_delivery_date date,
    delivered_at timestamp,
    handed_to_carrier_at timestamp,
    state_code text CHECK (state_code ~ '^[A-Z]{2}$'),
    city_name text CHECK (state_code IS NOT NULL OR city_name IS NULL),
    PRIMARY KEY (dataset_id, order_id)
);

CREATE INDEX order_fact_dataset_purchase_idx
    ON order_fact (dataset_id, purchased_at);
CREATE INDEX order_fact_dataset_state_purchase_idx
    ON order_fact (dataset_id, state_code, purchased_at);
