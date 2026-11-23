-- State concentration excludes unknown regions and non-positive increments.
WITH late_orders AS (
    SELECT date_trunc('month', purchased_at)::date AS month, state_code
    FROM order_fact
    WHERE dataset_id = %(dataset_id)s
      AND order_status = 'delivered'
      AND delivered_at::date > estimated_delivery_date
), increments AS (
    SELECT state_code,
           sum(CASE WHEN month = %(month)s THEN 1 ELSE -1 END) AS increment
    FROM late_orders
    WHERE state_code IS NOT NULL AND month IN (%(baseline_month)s, %(month)s)
    GROUP BY state_code
    HAVING sum(CASE WHEN month = %(month)s THEN 1 ELSE -1 END) > 0
)
SELECT max(increment) AS max_positive, sum(increment)::bigint AS total_positive,
       max(increment) / nullif(sum(increment), 0) AS concentration
FROM increments;
