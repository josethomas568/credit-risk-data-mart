-- 02_dimensions.sql ---------------------------------------------------------
-- Conformed dimensions: month, delinquency bucket, status code, account (with segments).

CREATE OR REPLACE TABLE dim_month AS
SELECT * FROM (VALUES
    (1, DATE '2005-04-01', '2005-04', 'Apr 2005', 'PAY_6', 'BILL_AMT6', 'PAY_AMT6'),
    (2, DATE '2005-05-01', '2005-05', 'May 2005', 'PAY_5', 'BILL_AMT5', 'PAY_AMT5'),
    (3, DATE '2005-06-01', '2005-06', 'Jun 2005', 'PAY_4', 'BILL_AMT4', 'PAY_AMT4'),
    (4, DATE '2005-07-01', '2005-07', 'Jul 2005', 'PAY_3', 'BILL_AMT3', 'PAY_AMT3'),
    (5, DATE '2005-08-01', '2005-08', 'Aug 2005', 'PAY_2', 'BILL_AMT2', 'PAY_AMT2'),
    (6, DATE '2005-09-01', '2005-09', 'Sep 2005', 'PAY_0', 'BILL_AMT1', 'PAY_AMT1')
) t(month_index, month_start, month_key, month_label, src_status_col, src_balance_col, src_payment_col);

-- Delinquency buckets. Codes per the UCI documentation, plus the two undocumented
-- codes that appear in the data (-2 and 0), whose meaning comes from the dataset's
-- author: -2 no consumption, 0 revolving credit (minimum paid).
CREATE OR REPLACE TABLE dim_bucket AS
SELECT * FROM (VALUES
    ('Current', 0, 'Not past due (no balance, paid in full, or minimum paid)'),
    ('30',      1, '1 month past due'),
    ('60',      2, '2 months past due'),
    ('90+',     3, '3 or more months past due')
) t(bucket, bucket_order, bucket_desc);

CREATE OR REPLACE TABLE dim_status_code AS
SELECT code AS status_code,
       CASE WHEN code = -2 THEN 'No consumption'
            WHEN code = -1 THEN 'Paid in full'
            WHEN code =  0 THEN 'Revolving (minimum paid)'
            WHEN code =  9 THEN '9+ months late'
            ELSE code || ' month' || CASE WHEN code > 1 THEN 's' ELSE '' END || ' late' END AS status_desc,
       GREATEST(code, 0) AS months_past_due,
       CASE WHEN code <= 0 THEN 'Current' WHEN code = 1 THEN '30' WHEN code = 2 THEN '60' ELSE '90+' END AS bucket,
       code IN (-1, 1, 2, 3, 4, 5, 6, 7, 8, 9) AS documented_in_source
FROM range(-2, 10) r(code);

CREATE OR REPLACE TABLE dim_account AS
SELECT
    account_id,
    credit_limit,
    sex_code,
    CASE sex_code WHEN 1 THEN 'Male' WHEN 2 THEN 'Female' ELSE 'Unknown' END                       AS sex,
    education_code,
    CASE education_code WHEN 1 THEN 'Graduate school' WHEN 2 THEN 'University'
                        WHEN 3 THEN 'High school' ELSE 'Other / unknown' END                       AS education,
    marriage_code,
    CASE marriage_code WHEN 1 THEN 'Married' WHEN 2 THEN 'Single' ELSE 'Other / unknown' END       AS marital_status,
    age,
    CASE WHEN age < 30 THEN '21-29' WHEN age < 40 THEN '30-39' WHEN age < 50 THEN '40-49'
         WHEN age < 60 THEN '50-59' ELSE '60+' END                                                 AS age_band,
    CASE WHEN credit_limit <= 50000 THEN '<= 50k' WHEN credit_limit <= 100000 THEN '50k-100k'
         WHEN credit_limit <= 200000 THEN '100k-200k' WHEN credit_limit <= 300000 THEN '200k-300k'
         WHEN credit_limit <= 500000 THEN '300k-500k' ELSE '> 500k' END                            AS limit_band,
    CASE WHEN credit_limit <= 50000 THEN 1 WHEN credit_limit <= 100000 THEN 2 WHEN credit_limit <= 200000 THEN 3
         WHEN credit_limit <= 300000 THEN 4 WHEN credit_limit <= 500000 THEN 5 ELSE 6 END          AS limit_band_order,
    default_next_month = 1                                                                         AS defaulted_oct_2005
FROM stg_credit_raw;
