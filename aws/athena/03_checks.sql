-- 03_checks.sql -------------------------------------------------------------
-- Post-deploy validation, run in Athena by deploy.py. Each statement returns ONE row:
--   check_name | passed (boolean) | detail
-- Together they prove the lake in S3 matches the source file and the DuckDB build.
-- Portable SQL (Athena/Trino and DuckDB); tests/test_athena_sql.py runs them locally first.

-- A1 The raw CSV in S3 has 30,000 unique accounts ---------------------------------------
SELECT 'A1 raw file: 30,000 unique accounts' AS check_name,
       COUNT(*) = 30000 AND COUNT(DISTINCT id) = 30000 AS passed,
       CAST(COUNT(*) AS VARCHAR) || ' rows, ' || CAST(COUNT(DISTINCT id) AS VARCHAR) || ' distinct ids' AS detail
FROM ${DATABASE}.raw_uci_credit_card;

-- A2 Every snapshot partition holds all 30,000 accounts ---------------------------------
SELECT 'A2 snapshot: 6 months x 30,000 accounts' AS check_name,
       COUNT(*) = 6 AND MIN(n) = 30000 AND MAX(n) = 30000 AS passed,
       CAST(COUNT(*) AS VARCHAR) || ' partitions, ' || CAST(MIN(n) AS VARCHAR) || '-' || CAST(MAX(n) AS VARCHAR) || ' rows each' AS detail
FROM (SELECT month_key, COUNT(*) AS n FROM ${DATABASE}.fact_account_month GROUP BY month_key) p;

-- A3 Monthly balances and payments in the snapshot reconcile to the raw CSV ---------------
SELECT 'A3 balances reconcile: raw CSV = snapshot, every month' AS check_name,
       SUM(CASE WHEN ABS(r.bal - s.bal) > 0.005 OR ABS(r.paid - s.paid) > 0.005 THEN 1 ELSE 0 END) = 0 AS passed,
       CAST(SUM(CASE WHEN ABS(r.bal - s.bal) > 0.005 OR ABS(r.paid - s.paid) > 0.005 THEN 1 ELSE 0 END) AS VARCHAR)
         || ' of ' || CAST(COUNT(*) AS VARCHAR) || ' months differ' AS detail
FROM (
    SELECT '2005-04' AS month_key, SUM(bill_amt6) AS bal, SUM(pay_amt6) AS paid FROM ${DATABASE}.raw_uci_credit_card
    UNION ALL SELECT '2005-05', SUM(bill_amt5), SUM(pay_amt5) FROM ${DATABASE}.raw_uci_credit_card
    UNION ALL SELECT '2005-06', SUM(bill_amt4), SUM(pay_amt4) FROM ${DATABASE}.raw_uci_credit_card
    UNION ALL SELECT '2005-07', SUM(bill_amt3), SUM(pay_amt3) FROM ${DATABASE}.raw_uci_credit_card
    UNION ALL SELECT '2005-08', SUM(bill_amt2), SUM(pay_amt2) FROM ${DATABASE}.raw_uci_credit_card
    UNION ALL SELECT '2005-09', SUM(bill_amt1), SUM(pay_amt1) FROM ${DATABASE}.raw_uci_credit_card
) r
JOIN (SELECT month_key, CAST(SUM(statement_balance) AS DOUBLE) AS bal, CAST(SUM(payment_amount) AS DOUBLE) AS paid
      FROM ${DATABASE}.fact_account_month GROUP BY month_key) s
  ON s.month_key = r.month_key;

-- A4 Roll rates recomputed in Athena match the mart table from DuckDB ---------------------
SELECT 'A4 roll rates: Athena recompute = mart table' AS check_name,
       SUM(CASE WHEN COALESCE(v.accounts, 0) <> m.accounts THEN 1 ELSE 0 END) = 0 AS passed,
       CAST(SUM(CASE WHEN COALESCE(v.accounts, 0) <> m.accounts THEN 1 ELSE 0 END) AS VARCHAR)
         || ' of ' || CAST(COUNT(*) AS VARCHAR) || ' transitions differ' AS detail
FROM ${DATABASE}.mart_roll_rates m
LEFT JOIN ${DATABASE}.v_roll_rates v
  ON v.to_month = m.to_month AND v.from_bucket = m.from_bucket AND v.to_bucket = m.to_bucket;

-- A5 Delinquency recomputed in Athena matches the mart's portfolio table -------------------
SELECT 'A5 delinquency: Athena recompute = mart table' AS check_name,
       SUM(CASE WHEN v.accounts_30plus <> m.accounts_30plus OR v.accounts_90plus <> m.accounts_90plus
                  OR v.exposure <> m.exposure THEN 1 ELSE 0 END) = 0 AS passed,
       CAST(SUM(CASE WHEN v.accounts_30plus <> m.accounts_30plus OR v.accounts_90plus <> m.accounts_90plus
                       OR v.exposure <> m.exposure THEN 1 ELSE 0 END) AS VARCHAR) || ' of 6 months differ' AS detail
FROM ${DATABASE}.mart_portfolio_month m
JOIN ${DATABASE}.v_delinquency_by_month v ON v.month_key = m.month_key;

-- A6 Portfolio default rate is 22.12%, as in the source and the prediction model ----------
-- Counted, not averaged: Trino's AVG over a DECIMAL keeps the input's scale (0.2212 would become 0.2).
SELECT 'A6 default rate = 22.12% (6,636 of 30,000)' AS check_name,
       SUM(CASE WHEN defaulted_oct_2005 THEN 1 ELSE 0 END) = 6636 AND COUNT(*) = 30000 AS passed,
       CAST(SUM(CASE WHEN defaulted_oct_2005 THEN 1 ELSE 0 END) AS VARCHAR) || ' of '
         || CAST(COUNT(*) AS VARCHAR) || ' accounts defaulted' AS detail
FROM ${DATABASE}.dim_account;

-- A7 The build that produced the lake passed every blocking data-quality test --------------
SELECT 'A7 data quality: no blocking test failed in the build' AS check_name,
       SUM(CASE WHEN status = 'FAIL' THEN 1 ELSE 0 END) = 0 AS passed,
       CAST(SUM(CASE WHEN status = 'PASS' THEN 1 ELSE 0 END) AS VARCHAR) || ' pass, '
         || CAST(SUM(CASE WHEN status = 'FAIL' THEN 1 ELSE 0 END) AS VARCHAR) || ' fail, '
         || CAST(SUM(CASE WHEN status = 'WARN' THEN 1 ELSE 0 END) AS VARCHAR) || ' warn' AS detail
FROM ${DATABASE}.dq_results;
