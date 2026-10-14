-- 02_views.sql --------------------------------------------------------------
-- Analytics computed in Athena from the snapshot fact, not copied from DuckDB.
-- Written in the SQL subset Athena (Trino) and DuckDB share, so tests/test_athena_sql.py
-- runs these exact statements against the same Parquet files before anything is deployed.
-- Integer division is avoided on purpose: Trino truncates int/int, DuckDB does not.

-- Delinquency by month: accounts and exposure 30/60/90+ days past due --------------------
CREATE OR REPLACE VIEW ${DATABASE}.v_delinquency_by_month AS
SELECT f.month_key,
       m.month_label,
       COUNT(*)                                                         AS accounts,
       SUM(CASE WHEN f.bucket_order >= 1 THEN 1 ELSE 0 END)             AS accounts_30plus,
       SUM(CASE WHEN f.bucket_order >= 2 THEN 1 ELSE 0 END)             AS accounts_60plus,
       SUM(CASE WHEN f.bucket_order >= 3 THEN 1 ELSE 0 END)             AS accounts_90plus,
       CAST(SUM(CASE WHEN f.bucket_order >= 1 THEN 1 ELSE 0 END) AS DOUBLE) / COUNT(*) AS rate_30plus,
       CAST(SUM(CASE WHEN f.bucket_order >= 2 THEN 1 ELSE 0 END) AS DOUBLE) / COUNT(*) AS rate_60plus,
       CAST(SUM(CASE WHEN f.bucket_order >= 3 THEN 1 ELSE 0 END) AS DOUBLE) / COUNT(*) AS rate_90plus,
       SUM(f.exposure)                                                  AS exposure,
       SUM(CASE WHEN f.bucket_order >= 1 THEN f.exposure ELSE 0 END)    AS exposure_30plus
FROM ${DATABASE}.fact_account_month f
JOIN ${DATABASE}.dim_month m ON m.month_key = f.month_key
GROUP BY f.month_key, m.month_label;

-- Roll rates recomputed from the snapshot: where each bucket's accounts went next month -----
CREATE OR REPLACE VIEW ${DATABASE}.v_roll_rates AS
WITH moves AS (
    SELECT f.month_key AS to_month, f.prior_bucket AS from_bucket, f.bucket AS to_bucket, COUNT(*) AS accounts
    FROM ${DATABASE}.fact_account_month f
    WHERE f.prior_bucket IS NOT NULL
    GROUP BY f.month_key, f.prior_bucket, f.bucket
)
SELECT mv.to_month,
       mv.from_bucket,
       fb.bucket_order AS from_order,
       mv.to_bucket,
       tb.bucket_order AS to_order,
       mv.accounts,
       SUM(mv.accounts) OVER (PARTITION BY mv.to_month, mv.from_bucket) AS from_bucket_accounts,
       CAST(mv.accounts AS DOUBLE) / SUM(mv.accounts) OVER (PARTITION BY mv.to_month, mv.from_bucket) AS roll_rate,
       CASE WHEN tb.bucket_order > fb.bucket_order THEN 'Roll forward'
            WHEN tb.bucket_order < fb.bucket_order AND tb.bucket_order = 0 THEN 'Cure'
            WHEN tb.bucket_order < fb.bucket_order THEN 'Roll back'
            ELSE 'Stay' END AS movement
FROM moves mv
JOIN ${DATABASE}.dim_bucket fb ON fb.bucket = mv.from_bucket
JOIN ${DATABASE}.dim_bucket tb ON tb.bucket = mv.to_bucket;

-- Default rate by credit-limit band and Sep 2005 bucket (a two-way cut the mart doesn't store) --
CREATE OR REPLACE VIEW ${DATABASE}.v_default_by_limit_and_bucket AS
SELECT a.limit_band,
       a.limit_band_order,
       f.bucket          AS sep_bucket,
       f.bucket_order    AS sep_bucket_order,
       COUNT(*)          AS accounts,
       SUM(CASE WHEN a.defaulted_oct_2005 THEN 1 ELSE 0 END)                       AS defaults,
       CAST(SUM(CASE WHEN a.defaulted_oct_2005 THEN 1 ELSE 0 END) AS DOUBLE) / COUNT(*) AS default_rate,
       SUM(f.exposure)   AS exposure,
       SUM(CASE WHEN a.defaulted_oct_2005 THEN f.exposure ELSE 0 END)             AS exposure_at_default
FROM ${DATABASE}.fact_account_month f
JOIN ${DATABASE}.dim_account a ON a.account_id = f.account_id
WHERE f.month_key = '2005-09'
GROUP BY a.limit_band, a.limit_band_order, f.bucket, f.bucket_order;
