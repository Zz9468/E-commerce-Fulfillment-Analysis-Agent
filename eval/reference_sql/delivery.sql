-- Independent baseline. Run for any set of whole purchase months.
WITH orders AS (
    SELECT date_trunc('month', purchased_at)::date AS month, *
    FROM order_fact
    WHERE dataset_id = %(dataset_id)s
), results AS (
    SELECT month, state_code, city_name,
           GROUPING(state_code, city_name) AS region_level,
           sum(CASE WHEN order_status = 'delivered' THEN 1 ELSE 0 END) AS delivered,
           count(CASE WHEN order_status = 'delivered'
                           AND delivered_at::date > estimated_delivery_date THEN 1 END) AS late,
           count(CASE WHEN order_status = 'delivered'
                           AND estimated_delivery_date IS NOT NULL THEN delivered_at END)
               AS late_samples,
           avg(CASE WHEN order_status = 'delivered' AND delivered_at >= purchased_at
                    THEN extract(epoch FROM delivered_at - purchased_at) END) / 86400 AS avg_days,
           count(CASE WHEN order_status = 'delivered' AND delivered_at >= purchased_at
                      THEN 1 END) AS avg_samples
    FROM orders
    WHERE month = ANY(%(months)s::date[])
    GROUP BY GROUPING SETS ((month), (month, state_code), (month, state_code, city_name))
)
SELECT *, late::numeric / nullif(late_samples, 0) AS late_rate
FROM results
ORDER BY month, region_level DESC, state_code, city_name;
