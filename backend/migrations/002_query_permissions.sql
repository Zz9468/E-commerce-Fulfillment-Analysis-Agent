DO $$
BEGIN
    CREATE ROLE fulfillment_read_hq NOLOGIN;
EXCEPTION WHEN duplicate_object OR unique_violation THEN NULL;
END;
$$;

DO $$
BEGIN
    CREATE ROLE fulfillment_read_sp NOLOGIN;
EXCEPTION WHEN duplicate_object OR unique_violation THEN NULL;
END;
$$;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO fulfillment_read_hq, fulfillment_read_sp;

GRANT SELECT (dataset_id, source, imported_at, has_carrier_handoff)
    ON public.dataset_version TO fulfillment_read_hq, fulfillment_read_sp;
GRANT SELECT (covered_months, order_count)
    ON public.dataset_version TO fulfillment_read_hq;
GRANT SELECT (dataset_id, order_status, purchased_at, estimated_delivery_date,
              delivered_at, handed_to_carrier_at, state_code, city_name)
    ON public.order_fact TO fulfillment_read_hq, fulfillment_read_sp;

ALTER TABLE public.order_fact ENABLE ROW LEVEL SECURITY;
CREATE POLICY order_fact_hq ON public.order_fact FOR SELECT
    TO fulfillment_read_hq USING (true);
CREATE POLICY order_fact_sp ON public.order_fact FOR SELECT
    TO fulfillment_read_sp USING (state_code = 'SP');
