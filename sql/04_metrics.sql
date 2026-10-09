-- 04_metrics.sql ------------------------------------------------------------
-- Derived metrics: bucket distribution, roll rates, default rates by bucket and segment,
-- and a monthly portfolio summary. All read fact_account_month / dim_account only.

-- Accounts and balances by delinquency bucket, every month --------------------------
CREATE OR REPLACE TABLE mart_bucket_month AS
SELECT m.month_index, m.month_key, b.bucket, b.bucket_order,
       COUNT(f.account_id)                            AS accounts,
       COALESCE(SUM(f.statement_balance), 0)          AS statement_balance,
       COALESCE(SUM(f.exposure), 0)                   AS exposure,
       COUNT(f.account_id)::DOUBLE / SUM(COUNT(f.account_id)) OVER (PARTITION BY m.month_index) AS pct_accounts,
       COALESCE(SUM(f.exposure), 0) / SUM(SUM(f.exposure)) OVER (PARTITION BY m.month_index)    AS pct_exposure
FROM dim_month m CROSS JOIN dim_bucket b
LEFT JOIN fact_account_month f ON f.month_index = m.month_index AND f.bucket = b.bucket
GROUP BY m.month_index, m.month_key, b.bucket, b.bucket_order;

-- Roll rates: share of each bucket that moves to each bucket next month ----------------
CREATE OR REPLACE TABLE mart_roll_rates AS
WITH grid AS (
    SELECT m.month_index AS to_month_index, m.month_key AS to_month, fb.bucket AS from_bucket, fb.bucket_order AS from_order,
           tb.bucket AS to_bucket, tb.bucket_order AS to_order
    FROM dim_month m, dim_bucket fb, dim_bucket tb WHERE m.month_index > 1
), t AS (
    SELECT month_index AS to_month_index, prior_bucket AS from_bucket, bucket AS to_bucket,
           COUNT(*) AS accounts, SUM(prior_balance) AS from_balance, SUM(exposure) AS to_exposure
    FROM fact_account_month WHERE month_index > 1 GROUP BY ALL
)
SELECT g.to_month_index, pm.month_key AS from_month, g.to_month, g.from_bucket, g.from_order, g.to_bucket, g.to_order,
       COALESCE(t.accounts, 0)       AS accounts,
       COALESCE(t.from_balance, 0)   AS from_balance,
       COALESCE(t.to_exposure, 0)    AS to_exposure,
       SUM(COALESCE(t.accounts, 0)) OVER (PARTITION BY g.to_month_index, g.from_bucket) AS from_bucket_accounts,
       COALESCE(t.accounts, 0)::DOUBLE
         / NULLIF(SUM(COALESCE(t.accounts, 0)) OVER (PARTITION BY g.to_month_index, g.from_bucket), 0) AS roll_rate,
       CASE WHEN g.to_order > g.from_order THEN 'Roll forward'
            WHEN g.to_order < g.from_order AND g.to_order = 0 THEN 'Cure'
            WHEN g.to_order < g.from_order THEN 'Roll back'
            ELSE 'Stay' END AS movement
FROM grid g
JOIN dim_month pm ON pm.month_index = g.to_month_index - 1
LEFT JOIN t USING (to_month_index, from_bucket, to_bucket);

-- Headline roll rates per month (the numbers collections teams quote) ------------------
CREATE OR REPLACE TABLE mart_roll_summary AS
SELECT to_month_index, from_month, to_month,
       SUM(accounts) FILTER (WHERE from_bucket = 'Current' AND to_bucket <> 'Current')::DOUBLE
         / NULLIF(SUM(accounts) FILTER (WHERE from_bucket = 'Current'), 0)   AS current_to_delinquent,
       SUM(accounts) FILTER (WHERE from_bucket = '30' AND to_order >= 2)::DOUBLE
         / NULLIF(SUM(accounts) FILTER (WHERE from_bucket = '30'), 0)        AS b30_to_60plus,
       SUM(accounts) FILTER (WHERE from_bucket = '60' AND to_order >= 3)::DOUBLE
         / NULLIF(SUM(accounts) FILTER (WHERE from_bucket = '60'), 0)        AS b60_to_90plus,
       SUM(accounts) FILTER (WHERE from_bucket = '90+' AND to_bucket = '90+')::DOUBLE
         / NULLIF(SUM(accounts) FILTER (WHERE from_bucket = '90+'), 0)       AS b90_stay,
       SUM(accounts) FILTER (WHERE from_order > 0 AND to_bucket = 'Current')::DOUBLE
         / NULLIF(SUM(accounts) FILTER (WHERE from_order > 0), 0)            AS cure_rate
FROM mart_roll_rates GROUP BY ALL;

-- Default (Oct 2005) by delinquency bucket in Sep 2005: the last roll, into default ------
CREATE OR REPLACE TABLE mart_default_by_bucket AS
SELECT b.bucket, b.bucket_order,
       COUNT(f.account_id)                                      AS accounts,
       COUNT(f.account_id) FILTER (WHERE f.defaulted_oct_2005)  AS defaults,
       COUNT(f.account_id) FILTER (WHERE f.defaulted_oct_2005)::DOUBLE / NULLIF(COUNT(f.account_id), 0) AS default_rate,
       COALESCE(SUM(f.exposure), 0)                             AS exposure,
       COALESCE(SUM(f.exposure) FILTER (WHERE f.defaulted_oct_2005), 0) AS exposure_at_default
FROM dim_bucket b LEFT JOIN fact_account_month f ON f.bucket = b.bucket AND f.month_index = 6
GROUP BY ALL;

-- Default rates by segment (one row per segment value, for every segment type) ----------
CREATE OR REPLACE TABLE mart_default_by_segment AS
WITH sep AS (
    SELECT a.*, f.exposure, f.bucket AS sep_bucket,
           MAX(f2.months_past_due) AS worst_months_past_due_6m
    FROM dim_account a
    JOIN fact_account_month f  ON f.account_id = a.account_id AND f.month_index = 6
    JOIN fact_account_month f2 ON f2.account_id = a.account_id
    GROUP BY ALL
), seg AS (
    SELECT 'Limit band' AS segment_type, limit_band AS segment_value, limit_band_order AS sort_order, * FROM sep
    UNION ALL SELECT 'Age band', age_band, CAST(LEFT(age_band, 2) AS INTEGER), * FROM sep
    UNION ALL SELECT 'Education', education, education_code, * FROM sep
    UNION ALL SELECT 'Marital status', marital_status, marriage_code, * FROM sep
    UNION ALL SELECT 'Sex', sex, sex_code, * FROM sep
    UNION ALL SELECT 'Sep 2005 bucket', sep_bucket, CASE sep_bucket WHEN 'Current' THEN 0 WHEN '30' THEN 1 WHEN '60' THEN 2 ELSE 3 END, * FROM sep
    UNION ALL SELECT 'Worst status, 6 months',
                     CASE WHEN worst_months_past_due_6m = 0 THEN 'Never late' WHEN worst_months_past_due_6m = 1 THEN 'Worst 30'
                          WHEN worst_months_past_due_6m = 2 THEN 'Worst 60' ELSE 'Worst 90+' END,
                     LEAST(worst_months_past_due_6m, 3), * FROM sep
)
SELECT segment_type, segment_value, MIN(sort_order) AS sort_order,
       COUNT(*)                                         AS accounts,
       COUNT(*) FILTER (WHERE defaulted_oct_2005)       AS defaults,
       AVG(defaulted_oct_2005::INTEGER)                 AS default_rate,
       SUM(exposure)                                    AS exposure,
       SUM(exposure) FILTER (WHERE defaulted_oct_2005)  AS exposure_at_default,
       COALESCE(SUM(exposure) FILTER (WHERE defaulted_oct_2005), 0) / NULLIF(SUM(exposure), 0) AS exposure_default_rate,
       AVG(defaulted_oct_2005::INTEGER) / (SELECT AVG(defaulted_oct_2005::INTEGER) FROM dim_account) AS lift_vs_portfolio
FROM seg GROUP BY segment_type, segment_value;

-- Monthly portfolio summary (both views' headline numbers) ----------------------------
CREATE OR REPLACE TABLE mart_portfolio_month AS
SELECT f.month_index, m.month_key, m.month_label,
       COUNT(*)                                                        AS accounts,
       COUNT(*) FILTER (WHERE bucket_order >= 1)                       AS accounts_30plus,
       COUNT(*) FILTER (WHERE bucket_order >= 2)                       AS accounts_60plus,
       COUNT(*) FILTER (WHERE bucket_order >= 3)                       AS accounts_90plus,
       AVG((bucket_order >= 1)::INTEGER)                               AS rate_30plus,
       AVG((bucket_order >= 2)::INTEGER)                               AS rate_60plus,
       AVG((bucket_order >= 3)::INTEGER)                               AS rate_90plus,
       SUM(statement_balance)                                          AS statement_balance,
       SUM(exposure)                                                   AS exposure,
       SUM(exposure) FILTER (WHERE bucket_order >= 1)                  AS exposure_30plus,
       SUM(exposure) FILTER (WHERE bucket_order >= 1) / SUM(exposure)  AS exposure_rate_30plus,
       SUM(payment_amount)                                             AS payments,
       SUM(payment_amount) / NULLIF(SUM(GREATEST(prior_balance, 0)), 0) AS payment_rate,
       SUM(statement_balance) / SUM(credit_limit)                      AS utilization,
       SUM(credit_limit)                                               AS credit_limit
FROM fact_account_month f JOIN dim_month m USING (month_index)
GROUP BY ALL;
