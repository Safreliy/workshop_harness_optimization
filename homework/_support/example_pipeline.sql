WITH latest_orders AS (
  SELECT
    SAFE_ID(sale_key) AS order_id,
    TRIM(person_key) AS customer_id,
    MONEY_CENTS(value_in_cents, 'int_cents') AS order_amount_cents,
    UPPER(TRIM(currency_iso)) AS currency,
    LOWER(TRIM(record_state)) AS status,
    ROW_NUMBER() OVER (PARTITION BY SAFE_ID(sale_key) ORDER BY CAST(snapshot_no AS INTEGER) DESC) AS rn
  FROM orders
),
orders_final AS (
  SELECT order_id, customer_id, order_amount_cents, currency, status
  FROM latest_orders WHERE rn = 1
),
pay_dedup AS (
  SELECT
    SAFE_ID(sale_key) AS event_id,
    SAFE_ID(sale_key_order) AS order_id,
    MONEY_CENTS(value_in_cents, 'int_cents') AS amount,
    UPPER(TRIM(currency_iso)) AS currency,
    LOWER(TRIM(record_state)) AS status,
    ROW_NUMBER() OVER (PARTITION BY SAFE_ID(sale_key) ORDER BY CAST(snapshot_no AS INTEGER) DESC) AS rn
  FROM payments
),
pay_final AS (
  SELECT order_id, amount
  FROM pay_dedup
  WHERE rn = 1 AND status = 'settled' AND amount IS NOT NULL
),
ref_dedup AS (
  SELECT
    SAFE_ID(sale_key) AS event_id,
    SAFE_ID(sale_key_order) AS order_id,
    MONEY_CENTS(value_in_cents, 'int_cents') AS amount,
    UPPER(TRIM(currency_iso)) AS currency,
    LOWER(TRIM(record_state)) AS status,
    ROW_NUMBER() OVER (PARTITION BY SAFE_ID(sale_key) ORDER BY CAST(snapshot_no AS INTEGER) DESC) AS rn
  FROM refunds
),
ref_final AS (
  SELECT order_id, amount
  FROM ref_dedup
  WHERE rn = 1 AND status = 'settled' AND amount IS NOT NULL
),
pay_agg AS (
  SELECT order_id, SUM(amount) AS paid_cents
  FROM pay_final
  WHERE order_id IN (SELECT order_id FROM orders_final)
  GROUP BY order_id
),
ref_agg AS (
  SELECT order_id, SUM(amount) AS refunded_cents
  FROM ref_final
  WHERE order_id IN (SELECT order_id FROM orders_final)
  GROUP BY order_id
)
SELECT
  o.order_id,
  o.customer_id,
  o.currency,
  o.order_amount_cents,
  COALESCE(p.paid_cents, 0) AS paid_cents,
  COALESCE(r.refunded_cents, 0) AS refunded_cents,
  COALESCE(p.paid_cents, 0) - COALESCE(r.refunded_cents, 0) AS net_cents,
  o.status
FROM orders_final o
LEFT JOIN pay_agg p ON p.order_id = o.order_id
LEFT JOIN ref_agg r ON r.order_id = o.order_id
