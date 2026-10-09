# Source data

`raw/UCI_Credit_Card.csv` is the UCI **Default of Credit Card Clients** dataset (Yeh & Lien, 2009), in the
CSV layout Kaggle publishes (`uciml/default-of-credit-card-clients-dataset`). It is the same file used by
[Credit-risk-and-Default-Prediction](https://github.com/josethomas568/Credit-risk-and-Default-Prediction).

- 30,000 rows, 25 columns, 2.86 MB
- SHA-256 `a0f0ab49d6326671d6cd83be5c88dcf18007025fe9a53ecd699119c871176ca1` (checked by `build_mart.py`)
- Licence: CC BY 4.0 (UCI Machine Learning Repository)

Column map: `PAY_0, PAY_2..PAY_6` = repayment status Sep back to Apr 2005; `BILL_AMT1..6` = statement balance
Sep back to Apr; `PAY_AMT1..6` = amount paid Sep back to Apr; `default.payment.next.month` = default on the
Oct 2005 payment. Amounts are NT$.
