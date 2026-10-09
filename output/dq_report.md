# Data-quality report

Source: `UCI_Credit_Card.csv` (30,000 accounts, Apr-Sep 2005). Mode: normal.

**11 passed, 0 failed, 6 warnings.**

| Test | Name | Severity | Status | Failing rows | Example |
|---|---|---|---|---:|---|
| T01 | No duplicate accounts | ERROR | PASS | 0 |  |
| T02 | One snapshot per account per month | ERROR | PASS | 0 |  |
| T03 | Every account has six snapshots | ERROR | PASS | 0 |  |
| T04 | Valid status codes | ERROR | PASS | 0 |  |
| T05 | Every snapshot is bucketed | ERROR | PASS | 0 |  |
| T06 | Valid account attributes | ERROR | PASS | 0 |  |
| T07 | Payments not negative | ERROR | PASS | 0 |  |
| T08 | Default flag is 0/1 | ERROR | PASS | 0 |  |
| T09 | Balances reconcile to source | ERROR | PASS | 0 |  |
| T10 | Statement roll-forward closes | ERROR | PASS | 0 |  |
| T11 | Derived tables reconcile to snapshot | ERROR | PASS | 0 |  |
| T12 | Status skips a month (source quirk) | WARN | WARN | 6,209 | 1 / 2005-08: 0 -> 2 months past due / 1000 / 2005-08: 0 -> 2 months past due / 10008 / 2005-07: 0 -> 2 months past due / 10018 / 2005-05: 0 -> 2 months past due |
| T13 | Undocumented source codes | WARN | WARN | 6 | education 0: 14 accounts use undocumented education code 0 / education 5: 280 accounts use undocumented education code 5 / education 6: 51 accounts use undocume |
| T14 | Balance over credit limit | WARN | WARN | 6 | 2005-04: 798 accounts over limit; 89 over 120% / 2005-05: 820 accounts over limit; 105 over 120% / 2005-06: 1018 accounts over limit; 144 over 120% / 2005-07: 1 |
| T15 | Credit balances | WARN | WARN | 6 | 2005-04: 688 accounts with a credit balance, total -2,115,251 / 2005-05: 655 accounts with a credit balance, total -1,070,734 / 2005-06: 675 accounts with a cre |
| T16 | "No consumption" with a balance | WARN | WARN | 6 | 2005-04: 1428 accounts coded -2 with a positive statement balance / 2005-05: 1446 accounts coded -2 with a positive statement balance / 2005-06: 1446 accounts c |
| T17 | Status-code mix shift | WARN | WARN | 2 | 2005-09 code -2: 9.2% of accounts vs 14.4% in the other months / 2005-09 code 1: 12.3% of accounts vs 0.0% in the other months |

## Portfolio by month

| Month | Accounts | 30+ rate | 90+ rate | Exposure (NT$) | 30+ exposure share | Payment rate |
|---|---:|---:|---:|---:|---:|---:|
| Apr 2005 | 30,000 | 10.3% | 1.0% | 1,168,268,063 | 13.1% |  |
| May 2005 | 30,000 | 9.9% | 1.1% | 1,210,412,763 | 12.2% | 12.3% |
| Jun 2005 | 30,000 | 11.7% | 1.2% | 1,298,989,558 | 12.8% | 12.0% |
| Jul 2005 | 30,000 | 14.0% | 1.3% | 1,411,355,065 | 14.1% | 12.1% |
| Aug 2005 | 30,000 | 14.8% | 1.6% | 1,476,195,541 | 15.3% | 12.6% |
| Sep 2005 | 30,000 | 22.7% | 1.5% | 1,537,381,257 | 19.4% | 11.5% |
