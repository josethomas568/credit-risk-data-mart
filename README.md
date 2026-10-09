# Credit card risk data mart

A DuckDB data mart over the UCI Default of Credit Card Clients dataset: 30,000 accounts, six monthly
snapshots (Apr–Sep 2005), delinquency buckets, roll rates and default rates by segment. It also has
17 automated data-quality tests and a self-service dashboard with separate views for a risk manager and
a finance analyst.

It uses the same file as my [default-prediction model](https://github.com/josethomas568/Credit-risk-and-Default-Prediction).
The model asks *who* will default; this mart tracks *how* the portfolio gets there month by month.

```bash
pip install -r requirements.txt
python build_mart.py            # build, test, export; exits 1 if a blocking test fails (~2 s)
python build_mart.py --inject   # plant 4 defects; exits 0 only if the tests catch all 4
python -m pytest -q             # 12 checks on the built mart
npm install && npm test         # dashboard figures = mart figures (108 compared)
```

Open `output/credit_risk_dashboard.html` in a browser. It is one self-contained file and needs no server.

## What it found

| | |
|---|---|
| Default rate (Oct 2005) | **22.1%** (6,636 of 30,000), the same as the model notebook |
| 30+ days past due | 10.3% in Apr → 14.8% in Aug → **22.7% in Sep** (Sep partly a coding change; see below) |
| Current → delinquent roll rate | 3.2% into May, rising to 6.1% into Jul and **11.1% into Sep** |
| Default rate by Sep bucket | Current 13.8% · 30 days 33.9% · 60 days 69.1% · 90+ 71.9% |
| Default rate by credit limit | ≤ NT$50k **31.8%** vs > NT$500k 11.2% |
| Exposure at default | **NT$322.0m**, 20.9% of Sep exposure. 45% of it sat in accounts that were still current in Sep |

**The September coding break.** Status code 1 (one month late) covers 12.3% of accounts in Sep 2005
and almost none in Apr–Aug. Over the same months, 6,209 snapshots jump from current straight to 60 days.
So the source system almost certainly changed how it codes delinquency in September. Test T17 flags the
change on every build, and the dashboard warns about it on the September roll rates. The mart keeps the
published codes rather than guessing a remapping.

## Model

```
UCI_Credit_Card.csv ─► stg_credit_raw ─► dim_account ─────────────┐
   (sha256 checked)    ctl_source_totals  dim_month, dim_bucket,   ├─► fact_account_month (180,000 rows)
                                          dim_status_code ─────────┘        │
                                                                            ├─► mart_bucket_month
                                                                            ├─► mart_roll_rates, mart_roll_summary
                                                                            ├─► mart_default_by_bucket
                                                                            ├─► mart_default_by_segment
                                                                            ├─► mart_portfolio_month
                                                                            └─► dq_results, dq_failures
```

| Table | Grain | Notes |
|---|---|---|
| `dim_account` | account | Limit, demographics, limit and age bands, Oct 2005 default flag |
| `fact_account_month` | account × month | Status code, bucket, balance, exposure, payment, utilization, prior bucket, payment rate |
| `mart_bucket_month` | month × bucket | Accounts and exposure in each bucket |
| `mart_roll_rates` | month × from × to bucket | Full transition matrix, labelled roll forward / stay / roll back / cure |
| `mart_roll_summary` | month | Current→delinquent, 30→60+, 60→90+, 90+ stay, cure rate |
| `mart_default_by_bucket` | Sep bucket | Accounts, defaults, default rate, exposure at default |
| `mart_default_by_segment` | segment value | 7 segment types: limit, age, education, marital status, sex, Sep bucket, worst 6-month bucket |
| `mart_portfolio_month` | month | 30+/60+/90+ rates, exposure, payment rate, utilization |

**Buckets** come from the repayment status code: ≤ 0 Current (−2 no consumption, −1 paid in full, 0 minimum
paid), 1 = 30, 2 = 60, ≥ 3 = 90+. **Roll rate** = accounts moving from bucket A to B ÷ accounts in A the
month before. **Exposure** = positive statement balance.

## Data-quality tests (`sql/05_dq_tests.sql`)

ERROR tests stop the build. WARN tests measure known problems in the source and are reported every build.

| Test | Rule | Result on the real file |
|---|---|---|
| T01 | No duplicate accounts | pass |
| T02 | One snapshot per account per month | pass |
| T03 | Every account has all six months | pass |
| T04 | Status code in −2..9 | pass |
| T05 | Every snapshot has a bucket | pass |
| T06 | Valid limit, sex, age, education, marriage codes | pass |
| T07 | Payments not negative | pass |
| T08 | Default flag is 0/1 | pass |
| T09 | Monthly counts, balances, payments = source control totals | pass |
| T10 | Statement roll-forward closes per account-month | pass |
| T11 | Bucket, roll-rate and segment tables total back to the snapshot | pass |
| T12 | Status skips a month | warn: 6,209 snapshots |
| T13 | Undocumented codes (status −2/0, education 0/5/6, marriage 0) | warn |
| T14 | Balance over credit limit | warn: 798–2,115 accounts a month |
| T15 | Credit (negative) balances | warn: 590–688 accounts a month |
| T16 | "No consumption" (−2) with a balance | warn |
| T17 | A status code's monthly share moves > 5 points | warn: Sep 2005 code 1 and −2 |

`--inject` plants four realistic defects, and each one is caught by the tests named:

| Planted defect | Caught by |
|---|---|
| Account 1042 loaded twice | T01, T02, T09 |
| Account 2001 Sep status coded 11 | T04, T05 |
| Account 3003 Aug balance edited +5,000 in the mart | T09, T10 |
| Account 4004 Jun snapshot deleted | T03, T09 |

## Dashboard

There are three views over the same per-account data, with filters for month, credit limit, age,
education, marital status and sex:

- **Risk manager:** 30+/90+ rates and month-on-month change, delinquency trend, roll-rate matrix for
  any month, default rate by any segment against the filtered portfolio, default rate by Sep bucket and by
  worst six-month bucket.
- **Finance analyst:** exposure and its 30+ share, exposure by bucket over time, exposure at default by
  bucket, payment rate, utilization, and exposure and defaulted exposure by segment.
- **Data quality:** every test's status, failing-row count and examples, plus a live check that the
  dashboard's own totals equal the mart's.

The requirements (objective, users, success criteria, sources, risks) are in the one-page requirements
doc.

## Layout

```
build_mart.py               one command: verify source, build, test, export, dashboard
sql/01_staging.sql          typed load + source control totals
sql/02_dimensions.sql       month, bucket, status-code and account dimensions
sql/03_snapshots.sql        fact_account_month (unpivot + roll-forward)
sql/04_metrics.sql          buckets, roll rates, default rates, portfolio summary
sql/05_dq_tests.sql         17 data-quality tests
dashboard/template.html     dashboard; build_mart.py injects the data
tests/                      pytest suite + dashboard parity test
output/                     dashboard, dq_report.md, tables/*.csv
```
