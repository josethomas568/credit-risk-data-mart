-- examples.sql --------------------------------------------------------------
-- Analyst queries to run in the Athena console (workgroup credit-risk-mart, database credit_risk_mart).
-- Each scans a few MB at most; Athena charges $5 per TB, so each costs well under a cent.
-- tests/test_athena_sql.py runs all of them against the local Parquet files.

-- 1. Roll-rate matrix, Aug -> Sep 2005: rows are last month's bucket, columns this month's ----
SELECT from_bucket,
       ROUND(100 * SUM(CASE WHEN to_bucket = 'Current' THEN roll_rate ELSE 0 END), 1) AS to_current_pct,
       ROUND(100 * SUM(CASE WHEN to_bucket = '30'      THEN roll_rate ELSE 0 END), 1) AS to_30_pct,
       ROUND(100 * SUM(CASE WHEN to_bucket = '60'      THEN roll_rate ELSE 0 END), 1) AS to_60_pct,
       ROUND(100 * SUM(CASE WHEN to_bucket = '90+'     THEN roll_rate ELSE 0 END), 1) AS to_90plus_pct,
       MAX(from_bucket_accounts) AS accounts
FROM credit_risk_mart.v_roll_rates
WHERE to_month = '2005-09'
GROUP BY from_bucket, from_order
ORDER BY from_order;

-- 2. 30+ delinquency by credit-limit band and month (reads only the partitions it needs) -----
SELECT a.limit_band,
       f.month_key,
       ROUND(100 * CAST(SUM(CASE WHEN f.bucket_order >= 1 THEN 1 ELSE 0 END) AS DOUBLE) / COUNT(*), 1) AS pct_30plus
FROM credit_risk_mart.fact_account_month f
JOIN credit_risk_mart.dim_account a ON a.account_id = f.account_id
WHERE f.month_key IN ('2005-07', '2005-08', '2005-09')
GROUP BY a.limit_band, a.limit_band_order, f.month_key
ORDER BY a.limit_band_order, f.month_key;

-- 3. The September coding break: how often each status code is used, by month ----------------
SELECT status_code,
       status_desc,
       SUM(CASE WHEN month_key = '2005-07' THEN 1 ELSE 0 END) AS jul_2005,
       SUM(CASE WHEN month_key = '2005-08' THEN 1 ELSE 0 END) AS aug_2005,
       SUM(CASE WHEN month_key = '2005-09' THEN 1 ELSE 0 END) AS sep_2005
FROM credit_risk_mart.fact_account_month
WHERE month_key IN ('2005-07', '2005-08', '2005-09')
GROUP BY status_code, status_desc
ORDER BY status_code;

-- 4. Default rate by credit-limit band and Sep 2005 bucket ----------------------------------
SELECT limit_band,
       sep_bucket,
       accounts,
       ROUND(100 * default_rate, 1) AS default_rate_pct,
       CAST(exposure_at_default AS BIGINT) AS exposure_at_default_ntd
FROM credit_risk_mart.v_default_by_limit_and_bucket
WHERE accounts >= 50
ORDER BY limit_band_order, sep_bucket_order;

-- 5. Raw file vs mart, straight from S3: Sep 2005 balance total two ways ---------------------
SELECT (SELECT SUM(bill_amt1) FROM credit_risk_mart.raw_uci_credit_card)                 AS raw_csv_sep_balance,
       (SELECT CAST(SUM(statement_balance) AS DOUBLE) FROM credit_risk_mart.fact_account_month
         WHERE month_key = '2005-09')                                                      AS mart_sep_balance;
