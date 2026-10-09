-- 05_dq_tests.sql -----------------------------------------------------------
-- Automated data-quality tests. Each test writes the rows that break it to
-- dq_failures; dq_results has one row per test with PASS / FAIL / WARN.
--   ERROR severity -> FAIL blocks publishing the mart (build exits 1)
--   WARN  severity -> known source issue, counted and reported, does not block
-- Tolerance on money reconciliations: 0.005 (half a cent).

CREATE OR REPLACE TABLE dq_failures (test_id VARCHAR, item VARCHAR, detail VARCHAR);

-- T01 No duplicate accounts --------------------------------------------------------
INSERT INTO dq_failures
SELECT 'T01', account_id::VARCHAR, 'account_id appears ' || COUNT(*) || ' times in dim_account'
FROM dim_account GROUP BY account_id HAVING COUNT(*) > 1;

-- T02 Snapshot grain: one row per account per month -----------------------------------
INSERT INTO dq_failures
SELECT 'T02', account_id || ' / ' || month_key, COUNT(*) || ' snapshot rows for this account-month'
FROM fact_account_month GROUP BY account_id, month_key HAVING COUNT(*) > 1;

-- T03 Completeness: every account has all six monthly snapshots ------------------------
INSERT INTO dq_failures
SELECT 'T03', a.account_id::VARCHAR, 'has ' || COUNT(f.month_index) || ' of 6 monthly snapshots'
FROM dim_account a LEFT JOIN fact_account_month f USING (account_id)
GROUP BY a.account_id HAVING COUNT(DISTINCT f.month_index) <> 6;

-- T04 Valid repayment status codes (-2..9) --------------------------------------------
INSERT INTO dq_failures
SELECT 'T04', account_id || ' / ' || month_key, 'status code ' || COALESCE(status_code::VARCHAR, 'NULL') || ' is not a valid code (-2..9)'
FROM fact_account_month WHERE status_code IS NULL OR status_code NOT BETWEEN -2 AND 9;

-- T05 Every snapshot has a delinquency bucket ------------------------------------------
INSERT INTO dq_failures
SELECT 'T05', account_id || ' / ' || month_key, 'no bucket assigned'
FROM fact_account_month WHERE bucket IS NULL;

-- T06 Valid account attributes ---------------------------------------------------------
INSERT INTO dq_failures
SELECT 'T06', account_id::VARCHAR,
       concat_ws('; ',
           CASE WHEN credit_limit IS NULL OR credit_limit <= 0 THEN 'credit limit ' || COALESCE(credit_limit::VARCHAR, 'NULL') END,
           CASE WHEN sex_code NOT IN (1, 2) THEN 'sex code ' || sex_code END,
           CASE WHEN age IS NULL OR age NOT BETWEEN 18 AND 100 THEN 'age ' || COALESCE(age::VARCHAR, 'NULL') END,
           CASE WHEN education_code NOT BETWEEN 0 AND 6 THEN 'education code ' || education_code END,
           CASE WHEN marriage_code NOT BETWEEN 0 AND 3 THEN 'marriage code ' || marriage_code END)
FROM dim_account
WHERE credit_limit IS NULL OR credit_limit <= 0 OR sex_code NOT IN (1, 2) OR age IS NULL OR age NOT BETWEEN 18 AND 100
   OR education_code NOT BETWEEN 0 AND 6 OR marriage_code NOT BETWEEN 0 AND 3;

-- T07 Payments are not negative --------------------------------------------------------
INSERT INTO dq_failures
SELECT 'T07', account_id || ' / ' || month_key, 'payment ' || payment_amount
FROM fact_account_month WHERE payment_amount < 0 OR payment_amount IS NULL;

-- T08 Default flag is 0 or 1 -----------------------------------------------------------
INSERT INTO dq_failures
SELECT 'T08', account_id::VARCHAR, 'default flag ' || COALESCE(default_next_month::VARCHAR, 'NULL')
FROM stg_credit_raw WHERE default_next_month IS NULL OR default_next_month NOT IN (0, 1);

-- T09 Balances reconcile: snapshot totals = source control totals, every month ----------
INSERT INTO dq_failures
SELECT 'T09', m.month_key,
       format('source: {} accounts, balance {:,.2f}, payments {:,.2f}; mart: {} accounts, balance {:,.2f}, payments {:,.2f}',
              c.accounts, c.statement_balance, c.payments, COALESCE(f.accounts, 0), COALESCE(f.bal, 0), COALESCE(f.pay, 0))
FROM ctl_source_totals c
JOIN dim_month m USING (month_index)
LEFT JOIN (SELECT month_index, COUNT(*) AS accounts, SUM(statement_balance) AS bal, SUM(payment_amount) AS pay
           FROM fact_account_month GROUP BY 1) f USING (month_index)
WHERE c.accounts <> COALESCE(f.accounts, 0)
   OR ABS(c.statement_balance - COALESCE(f.bal, 0)) > 0.005
   OR ABS(c.payments - COALESCE(f.pay, 0)) > 0.005;

-- T10 Balances reconcile: each account's statement roll-forward closes ------------------
--     prior balance - payment + new activity = balance. New activity is derived when the
--     snapshot is built, so this catches a balance or payment edited in the mart afterwards
--     (a manual fix, a bad update script); T09 catches drift from the source file.
INSERT INTO dq_failures
SELECT 'T10', account_id || ' / ' || month_key,
       format('prior {:,.2f} - paid {:,.2f} + activity {:,.2f} <> balance {:,.2f}', prior_balance, payment_amount,
              implied_new_activity, statement_balance)
FROM fact_account_month
WHERE month_index > 1
  AND ABS(prior_balance - payment_amount + implied_new_activity - statement_balance) > 0.005;

-- T11 Derived tables reconcile to the snapshot ----------------------------------------
INSERT INTO dq_failures
SELECT 'T11', 'bucket table ' || month_key, format('bucket accounts {} / exposure {:,.2f} vs snapshot {} / {:,.2f}', b.n, b.e, f.n, f.e)
FROM (SELECT month_index, month_key, SUM(accounts) n, SUM(exposure) e FROM mart_bucket_month GROUP BY ALL) b
JOIN (SELECT month_index, COUNT(*) n, SUM(exposure) e FROM fact_account_month GROUP BY 1) f USING (month_index)
WHERE b.n <> f.n OR ABS(b.e - f.e) > 0.005
UNION ALL
SELECT 'T11', 'roll rates ' || r.from_month || ' ' || r.from_bucket,
       format('roll-rate rows sum to {} accounts; {} accounts were in this bucket', r.n, COALESCE(b.accounts, 0))
FROM (SELECT to_month_index, from_month, from_bucket, SUM(accounts) n FROM mart_roll_rates GROUP BY ALL) r
LEFT JOIN mart_bucket_month b ON b.month_index = r.to_month_index - 1 AND b.bucket = r.from_bucket
WHERE r.n <> COALESCE(b.accounts, 0)
UNION ALL
SELECT 'T11', 'default by segment: ' || segment_type,
       format('{} accounts / {} defaults vs portfolio {} / {}', SUM(accounts), SUM(defaults),
              (SELECT COUNT(*) FROM dim_account), (SELECT COUNT(*) FILTER (WHERE defaulted_oct_2005) FROM dim_account))
FROM mart_default_by_segment GROUP BY segment_type
HAVING SUM(accounts) <> (SELECT COUNT(*) FROM dim_account)
    OR SUM(defaults) <> (SELECT COUNT(*) FILTER (WHERE defaulted_oct_2005) FROM dim_account);

-- ---- WARN: known issues in the source data, measured every build --------------------

-- T12 Status skips a month: more than one month later than the prior month -------------
--     (source quirk: code 1 is almost absent before Sep 2005, so 0 -> 2 is common)
INSERT INTO dq_failures
SELECT 'T12', account_id || ' / ' || month_key,
       format('{} -> {} months past due', prior_bucket_order_mpd, months_past_due)
FROM (SELECT *, LAG(months_past_due) OVER (PARTITION BY account_id ORDER BY month_index) AS prior_bucket_order_mpd
      FROM fact_account_month)
WHERE months_past_due > prior_bucket_order_mpd + 1;

-- T13 Undocumented codes in the source (-2, 0 status; education 0/5/6; marriage 0) -------
INSERT INTO dq_failures
SELECT 'T13', 'status ' || status_code, COUNT(*) || ' snapshots use undocumented status code ' || status_code
FROM fact_account_month f JOIN dim_status_code s USING (status_code)
WHERE NOT s.documented_in_source GROUP BY status_code
UNION ALL
SELECT 'T13', 'education ' || education_code, COUNT(*) || ' accounts use undocumented education code ' || education_code
FROM dim_account WHERE education_code IN (0, 5, 6) GROUP BY education_code
UNION ALL
SELECT 'T13', 'marriage ' || marriage_code, COUNT(*) || ' accounts use undocumented marriage code ' || marriage_code
FROM dim_account WHERE marriage_code = 0 GROUP BY marriage_code;

-- T14 Balance over the credit limit (allowed, but large overlimits are worth review) ------
INSERT INTO dq_failures
SELECT 'T14', month_key, COUNT(*) || ' accounts over limit; ' || COUNT(*) FILTER (WHERE utilization > 1.2) || ' over 120%'
FROM fact_account_month WHERE statement_balance > credit_limit GROUP BY month_key;

-- T15 Credit (negative) balances ------------------------------------------------------
INSERT INTO dq_failures
SELECT 'T15', month_key, COUNT(*) || format(' accounts with a credit balance, total {:,.0f}', SUM(statement_balance))
FROM fact_account_month WHERE statement_balance < 0 GROUP BY month_key;

-- T16 Status says "no consumption" (-2) but a positive balance is billed -----------------
INSERT INTO dq_failures
SELECT 'T16', month_key, COUNT(*) || ' accounts coded -2 with a positive statement balance'
FROM fact_account_month WHERE status_code = -2 AND statement_balance > 0 GROUP BY month_key;

-- T17 Status-code mix shifts: a code's share of accounts in one month differs from its
--     average share in the other five months by more than 5 percentage points. Catches a
--     change in how the source system codes delinquency (it flags Sep 2005: code 1).
INSERT INTO dq_failures
WITH share AS (
    SELECT g.month_index, g.status_code,
           COUNT(f.account_id)::DOUBLE / SUM(COUNT(f.account_id)) OVER (PARTITION BY g.month_index) AS pct
    FROM (SELECT DISTINCT m.month_index, s.status_code FROM dim_month m, dim_status_code s) g
    LEFT JOIN fact_account_month f ON f.month_index = g.month_index AND f.status_code = g.status_code
    GROUP BY g.month_index, g.status_code
), cmp AS (
    SELECT a.month_index, a.status_code, a.pct, AVG(b.pct) AS other_pct
    FROM share a JOIN share b ON b.status_code = a.status_code AND b.month_index <> a.month_index
    GROUP BY a.month_index, a.status_code, a.pct
)
SELECT 'T17', m.month_key || ' code ' || c.status_code,
       format('{:.1f}% of accounts vs {:.1f}% in the other months', 100 * c.pct, 100 * c.other_pct)
FROM cmp c JOIN dim_month m USING (month_index)
WHERE ABS(c.pct - c.other_pct) > 0.05;

-- Results -------------------------------------------------------------------------------
CREATE OR REPLACE TABLE dq_catalog AS
SELECT * FROM (VALUES
  ('T01', 'No duplicate accounts',                      'Uniqueness',     'ERROR', 'account_id is unique in dim_account'),
  ('T02', 'One snapshot per account per month',         'Uniqueness',     'ERROR', '(account_id, month) is unique in fact_account_month'),
  ('T03', 'Every account has six snapshots',            'Completeness',   'ERROR', 'Apr-Sep 2005 present for each account'),
  ('T04', 'Valid status codes',                         'Validity',       'ERROR', 'Repayment status in -2..9'),
  ('T05', 'Every snapshot is bucketed',                 'Validity',       'ERROR', 'Bucket is one of Current / 30 / 60 / 90+'),
  ('T06', 'Valid account attributes',                   'Validity',       'ERROR', 'Limit > 0, sex 1-2, age 18-100, education 0-6, marriage 0-3'),
  ('T07', 'Payments not negative',                      'Validity',       'ERROR', 'payment_amount >= 0'),
  ('T08', 'Default flag is 0/1',                        'Validity',       'ERROR', 'default.payment.next.month in (0, 1)'),
  ('T09', 'Balances reconcile to source',               'Reconciliation', 'ERROR', 'Monthly account count, balance and payment totals equal the source file'),
  ('T10', 'Statement roll-forward closes',              'Reconciliation', 'ERROR', 'Prior balance - payment + new activity = balance, per account-month'),
  ('T11', 'Derived tables reconcile to snapshot',       'Reconciliation', 'ERROR', 'Bucket, roll-rate and segment tables total back to the snapshot'),
  ('T12', 'Status skips a month (source quirk)',        'Source issue',   'WARN',  'Months past due rises by more than 1 in a month'),
  ('T13', 'Undocumented source codes',                  'Source issue',   'WARN',  'Status -2/0, education 0/5/6, marriage 0 are not in the UCI codebook'),
  ('T14', 'Balance over credit limit',                  'Source issue',   'WARN',  'statement balance > credit limit'),
  ('T15', 'Credit balances',                            'Source issue',   'WARN',  'statement balance < 0 (excluded from exposure)'),
  ('T16', '"No consumption" with a balance',            'Source issue',   'WARN',  'status -2 with a positive statement balance'),
  ('T17', 'Status-code mix shift',                      'Source issue',   'WARN',  'A code''s monthly share moves > 5 points vs the other months (coding change)')
) t(test_id, test_name, category, severity, rule);

CREATE OR REPLACE TABLE dq_results AS
SELECT c.test_id, c.test_name, c.category, c.severity, c.rule,
       COUNT(f.test_id) AS failing_rows,
       CASE WHEN COUNT(f.test_id) = 0 THEN 'PASS' WHEN c.severity = 'ERROR' THEN 'FAIL' ELSE 'WARN' END AS status,
       COALESCE(array_to_string(list(f.item || ': ' || f.detail ORDER BY f.item)
                       FILTER (WHERE f.test_id IS NOT NULL)[1:5], ' | '), '') AS sample
FROM dq_catalog c LEFT JOIN dq_failures f ON f.test_id = c.test_id
GROUP BY c.test_id, c.test_name, c.category, c.severity, c.rule ORDER BY c.test_id;
