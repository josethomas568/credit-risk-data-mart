-- 03_snapshots.sql ----------------------------------------------------------
-- Monthly account snapshot: one row per account per month (30,000 x 6 = 180,000).
-- The wide source is unpivoted through dim_month so every month uses the same logic.

CREATE OR REPLACE TABLE fact_account_month AS
WITH long AS (
    SELECT r.account_id, m.month_index,
           CASE m.month_index WHEN 1 THEN r.pay_status_m1 WHEN 2 THEN r.pay_status_m2 WHEN 3 THEN r.pay_status_m3
                              WHEN 4 THEN r.pay_status_m4 WHEN 5 THEN r.pay_status_m5 ELSE r.pay_status_m6 END AS status_code,
           CASE m.month_index WHEN 1 THEN r.bill_m1 WHEN 2 THEN r.bill_m2 WHEN 3 THEN r.bill_m3
                              WHEN 4 THEN r.bill_m4 WHEN 5 THEN r.bill_m5 ELSE r.bill_m6 END                   AS statement_balance,
           CASE m.month_index WHEN 1 THEN r.paid_m1 WHEN 2 THEN r.paid_m2 WHEN 3 THEN r.paid_m3
                              WHEN 4 THEN r.paid_m4 WHEN 5 THEN r.paid_m5 ELSE r.paid_m6 END                   AS payment_amount
    FROM stg_credit_raw r CROSS JOIN dim_month m
)
SELECT
    l.account_id,
    l.month_index,
    m.month_key,
    l.status_code,
    s.status_desc,
    s.months_past_due,
    s.bucket,
    b.bucket_order,
    l.statement_balance,
    GREATEST(l.statement_balance, 0)                                          AS exposure,          -- credit balances excluded
    l.payment_amount,
    a.credit_limit,
    l.statement_balance / NULLIF(a.credit_limit, 0)                            AS utilization,
    LAG(l.statement_balance) OVER w                                            AS prior_balance,
    LAG(s.bucket)            OVER w                                            AS prior_bucket,
    LAG(b.bucket_order)      OVER w                                            AS prior_bucket_order,
    -- statement roll-forward: balance = prior balance - payment + new activity (purchases, fees, interest, credits)
    l.statement_balance - (LAG(l.statement_balance) OVER w - l.payment_amount)  AS implied_new_activity,
    l.payment_amount / NULLIF(GREATEST(LAG(l.statement_balance) OVER w, 0), 0)  AS payment_rate,
    a.defaulted_oct_2005
FROM long l
JOIN dim_month m        USING (month_index)
JOIN dim_account a      USING (account_id)
LEFT JOIN dim_status_code s ON s.status_code = l.status_code
LEFT JOIN dim_bucket b  ON b.bucket = s.bucket
WINDOW w AS (PARTITION BY l.account_id ORDER BY l.month_index);
