\set ON_ERROR_STOP on

CREATE TABLE tenants (
    id integer PRIMARY KEY,
    name text NOT NULL,
    plan text NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE TABLE customers (
    id bigint PRIMARY KEY,
    tenant_id integer NOT NULL REFERENCES tenants(id),
    email text NOT NULL,
    segment text NOT NULL,
    country_code char(2) NOT NULL,
    is_test boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL
);

CREATE TABLE products (
    id integer PRIMARY KEY,
    sku text NOT NULL UNIQUE,
    category text NOT NULL,
    active boolean NOT NULL
);

CREATE TABLE orders (
    id bigint PRIMARY KEY,
    tenant_id integer NOT NULL REFERENCES tenants(id),
    customer_id bigint NOT NULL REFERENCES customers(id),
    status text NOT NULL,
    sales_channel text NOT NULL,
    total numeric(12,2) NOT NULL,
    discount numeric(12,2) NOT NULL,
    is_test boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL,
    refunded_at timestamptz,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE order_items (
    id bigint PRIMARY KEY,
    -- Constraints are intentionally omitted on this synthetic fact table to keep
    -- workshop bootstrap fast on laptops; generated keys remain valid.
    order_id bigint NOT NULL,
    product_id integer NOT NULL,
    quantity smallint NOT NULL,
    unit_price numeric(10,2) NOT NULL
);

CREATE TABLE events (
    id bigint PRIMARY KEY,
    project_id integer NOT NULL,
    actor_id bigint NOT NULL,
    event_type text NOT NULL,
    occurred_at timestamptz NOT NULL,
    properties jsonb NOT NULL
);

CREATE TABLE support_tickets (
    id bigint PRIMARY KEY,
    tenant_id integer NOT NULL REFERENCES tenants(id),
    queue text NOT NULL,
    status text NOT NULL,
    priority integer NOT NULL,
    assignee_id integer,
    created_at timestamptz NOT NULL,
    resolved_at timestamptz
);

INSERT INTO tenants
SELECT g,
       'Tenant ' || g,
       (ARRAY['starter', 'growth', 'enterprise'])[(g % 3) + 1],
       timestamptz '2023-01-01 00:00:00+00' + (g || ' days')::interval
FROM generate_series(1, 200) AS g;

INSERT INTO customers
SELECT g,
       ((g * 17) % 200) + 1,
       'customer' || g || '@example.com',
       (ARRAY['smb', 'mid_market', 'enterprise'])[(g % 3) + 1],
       (ARRAY['RU', 'KZ', 'AM', 'GE'])[(g % 4) + 1],
       g % 211 = 0,
       timestamptz '2024-01-01 00:00:00+00'
           + ((g % 500) || ' days')::interval
FROM generate_series(1, 60000) AS g;

INSERT INTO products
SELECT g,
       'SKU-' || lpad(g::text, 6, '0'),
       (ARRAY['hardware', 'software', 'service', 'accessory'])[(g % 4) + 1],
       g % 29 <> 0
FROM generate_series(1, 5000) AS g;

INSERT INTO orders
SELECT g,
       ((g * 13) % 200) + 1,
       ((g * 17) % 60000) + 1,
       CASE
           WHEN g % 17 = 0 THEN 'refunded'
           WHEN g % 11 = 0 THEN 'cancelled'
           WHEN g % 5 = 0 THEN 'pending'
           ELSE 'paid'
       END,
       (ARRAY['web', 'mobile', 'partner'])[(g % 3) + 1],
       round((20 + (g % 20000) / 13.0)::numeric, 2),
       round((g % 700 / 17.0)::numeric, 2),
       g % 97 = 0,
       timestamptz '2025-01-01 00:00:00+00'
           + ((g % 620) || ' days')::interval
           + ((g % 1440) || ' minutes')::interval,
       CASE WHEN g % 17 = 0
            THEN timestamptz '2025-01-02 00:00:00+00'
                 + ((g % 620) || ' days')::interval
            ELSE NULL END,
       jsonb_build_object('source', (ARRAY['organic', 'ads', 'referral'])[(g % 3) + 1])
FROM generate_series(1, 220000) AS g;

INSERT INTO order_items
SELECT g,
       ((g * 19) % 220000) + 1,
       ((g * 37) % 5000) + 1,
       ((g % 4) + 1)::smallint,
       round((5 + (g % 10000) / 19.0)::numeric, 2)
FROM generate_series(1, 360000) AS g;

INSERT INTO events
SELECT g,
       ((g * 7) % 80) + 1,
       ((g * 23) % 60000) + 1,
       (ARRAY['page_view', 'search', 'checkout', 'purchase', 'login'])[(g % 5) + 1],
       timestamptz '2025-01-01 00:00:00+00'
           + ((g % 620) || ' days')::interval
           + ((g % 86400) || ' seconds')::interval,
       jsonb_build_object(
           'country', (ARRAY['RU', 'KZ', 'AM', 'GE'])[(g % 4) + 1],
           'device', (ARRAY['ios', 'android', 'web'])[(g % 3) + 1],
           'campaign', 'campaign_' || (g % 1000)
       )
FROM generate_series(1, 280000) AS g;

INSERT INTO support_tickets
SELECT g,
       ((g * 31) % 200) + 1,
       (ARRAY['billing', 'technical', 'onboarding', 'fraud'])[(g % 4) + 1],
       CASE WHEN g % 9 = 0 THEN 'open'
            WHEN g % 7 = 0 THEN 'pending'
            ELSE 'resolved' END,
       (g % 5) + 1,
       CASE WHEN g % 13 = 0 THEN NULL ELSE (g % 150) + 1 END,
       timestamptz '2025-01-01 00:00:00+00'
           + ((g % 620) || ' days')::interval,
       CASE WHEN g % 9 <> 0
            THEN timestamptz '2025-01-02 00:00:00+00'
                 + ((g % 620) || ' days')::interval
            ELSE NULL END
FROM generate_series(1, 100000) AS g;

-- Deliberately incomplete index set: the benchmark asks the agent to find gaps.
CREATE INDEX idx_customers_tenant ON customers (tenant_id);
CREATE INDEX idx_orders_customer ON orders (customer_id);
CREATE INDEX idx_orders_created_at ON orders (created_at);
CREATE INDEX idx_order_items_order ON order_items (order_id);
CREATE INDEX idx_events_occurred_at ON events (occurred_at);
CREATE INDEX idx_tickets_tenant ON support_tickets (tenant_id);

ANALYZE;

CREATE TABLE benchmark_metadata (
    key text PRIMARY KEY,
    value text NOT NULL
);
INSERT INTO benchmark_metadata VALUES ('dataset_version', '1');
