-- 01_staging.sql ------------------------------------------------------------
-- Raw landing table: the UCI "Default of Credit Card Clients" file as published
-- (Kaggle copy UCI_Credit_Card.csv). One row per account, wide format:
--   PAY_0, PAY_2..PAY_6      repayment status, Sep 2005 back to Apr 2005
--   BILL_AMT1..BILL_AMT6     statement balance,  Sep 2005 back to Apr 2005 (NT$)
--   PAY_AMT1..PAY_AMT6       amount paid in the month, Sep 2005 back to Apr 2005 (NT$)
--   default.payment.next.month  1 = defaulted on the Oct 2005 payment
-- {RAW} is replaced by the build script.

CREATE OR REPLACE TABLE stg_credit_raw AS
SELECT
    "ID"::INTEGER                       AS account_id,
    "LIMIT_BAL"::DECIMAL(12,2)          AS credit_limit,
    "SEX"::INTEGER                      AS sex_code,
    "EDUCATION"::INTEGER                AS education_code,
    "MARRIAGE"::INTEGER                 AS marriage_code,
    "AGE"::INTEGER                      AS age,
    "PAY_0"::INTEGER AS pay_status_m6, "PAY_2"::INTEGER AS pay_status_m5, "PAY_3"::INTEGER AS pay_status_m4,
    "PAY_4"::INTEGER AS pay_status_m3, "PAY_5"::INTEGER AS pay_status_m2, "PAY_6"::INTEGER AS pay_status_m1,
    "BILL_AMT1"::DECIMAL(12,2) AS bill_m6, "BILL_AMT2"::DECIMAL(12,2) AS bill_m5, "BILL_AMT3"::DECIMAL(12,2) AS bill_m4,
    "BILL_AMT4"::DECIMAL(12,2) AS bill_m3, "BILL_AMT5"::DECIMAL(12,2) AS bill_m2, "BILL_AMT6"::DECIMAL(12,2) AS bill_m1,
    "PAY_AMT1"::DECIMAL(12,2)  AS paid_m6, "PAY_AMT2"::DECIMAL(12,2)  AS paid_m5, "PAY_AMT3"::DECIMAL(12,2)  AS paid_m4,
    "PAY_AMT4"::DECIMAL(12,2)  AS paid_m3, "PAY_AMT5"::DECIMAL(12,2)  AS paid_m2, "PAY_AMT6"::DECIMAL(12,2)  AS paid_m1,
    "default.payment.next.month"::INTEGER AS default_next_month
FROM read_csv('{RAW}', header = true, all_varchar = true);

-- Source control totals, captured before any transformation (used by the reconciliation tests)
CREATE OR REPLACE TABLE ctl_source_totals AS
SELECT m.month_index,
       COUNT(*)                                                     AS accounts,
       SUM(CASE m.month_index WHEN 1 THEN bill_m1 WHEN 2 THEN bill_m2 WHEN 3 THEN bill_m3
                              WHEN 4 THEN bill_m4 WHEN 5 THEN bill_m5 ELSE bill_m6 END) AS statement_balance,
       SUM(CASE m.month_index WHEN 1 THEN paid_m1 WHEN 2 THEN paid_m2 WHEN 3 THEN paid_m3
                              WHEN 4 THEN paid_m4 WHEN 5 THEN paid_m5 ELSE paid_m6 END) AS payments,
       SUM(credit_limit)                                            AS credit_limit,
       SUM(default_next_month)                                      AS defaults
FROM stg_credit_raw CROSS JOIN range(1, 7) m(month_index)
GROUP BY m.month_index;
