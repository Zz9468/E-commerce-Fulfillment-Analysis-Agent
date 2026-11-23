-- Independently establish the late-rate sample, then calculate interval quantiles.
WITH eligible AS (
    SELECT date_trunc('month', purchased_at)::date AS month,
           purchased_at, handed_to_carrier_at, delivered_at
    FROM order_fact
    WHERE dataset_id = %(dataset_id)s
      AND (%(state)s::text IS NULL OR state_code = %(state)s)
      AND order_status = 'delivered'
      AND delivered_at IS NOT NULL
      AND estimated_delivery_date IS NOT NULL
), intervals AS (
    SELECT month,
           CASE WHEN handed_to_carrier_at BETWEEN purchased_at AND delivered_at
                THEN handed_to_carrier_at - purchased_at END AS before_handoff,
           CASE WHEN handed_to_carrier_at BETWEEN purchased_at AND delivered_at
                THEN delivered_at - handed_to_carrier_at END AS after_handoff
    FROM eligible
    WHERE month = ANY(%(months)s::date[])
)
SELECT month,
       count(before_handoff) AS sample_count,
       count(*) - count(before_handoff) AS excluded_count,
       extract(epoch FROM percentile_cont(0.5) WITHIN GROUP (ORDER BY before_handoff))
           / 86400 AS before_p50,
       extract(epoch FROM percentile_cont(0.9) WITHIN GROUP (ORDER BY before_handoff))
           / 86400 AS before_p90,
       extract(epoch FROM percentile_cont(0.5) WITHIN GROUP (ORDER BY after_handoff))
           / 86400 AS after_p50,
       extract(epoch FROM percentile_cont(0.9) WITHIN GROUP (ORDER BY after_handoff))
           / 86400 AS after_p90
FROM intervals
GROUP BY month
ORDER BY month;
