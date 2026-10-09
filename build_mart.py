#!/usr/bin/env python3
"""
Build the credit card risk data mart and run its data-quality tests.

    python build_mart.py                 # build from data/raw/UCI_Credit_Card.csv
    python build_mart.py --inject        # same, with four planted defects: tests must fail
    python build_mart.py --raw path.csv  # build from another extract with the same layout

Steps: verify the source file -> stage -> dimensions -> monthly snapshots -> metrics ->
data-quality tests -> exports (CSV per table, dashboard data, test report).

Exit code 0 when every ERROR-level test passes, 1 otherwise. WARN tests (known source
issues) are reported but do not block.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent
SQL = ["01_staging.sql", "02_dimensions.sql", "03_snapshots.sql", "04_metrics.sql", "05_dq_tests.sql"]
SOURCE_SHA256 = "a0f0ab49d6326671d6cd83be5c88dcf18007025fe9a53ecd699119c871176ca1"
EXPORT_TABLES = ["dim_account", "dim_month", "dim_bucket", "dim_status_code", "fact_account_month",
                 "mart_bucket_month", "mart_roll_rates", "mart_roll_summary", "mart_default_by_bucket",
                 "mart_default_by_segment", "mart_portfolio_month", "dq_results", "dq_failures"]

# Planted defects for --inject. Each simulates a real failure mode and names the tests that must catch it.
INJECTIONS = {
    "after:01_staging.sql": [
        ("Duplicate account in the extract (account 1042 loaded twice)",
         "INSERT INTO stg_credit_raw SELECT * FROM stg_credit_raw WHERE account_id = 1042", ["T01", "T02", "T09"]),
        ("Invalid status code (account 2001, Sep 2005 coded 11)",
         "UPDATE stg_credit_raw SET pay_status_m6 = 11 WHERE account_id = 2001", ["T04", "T05"]),
    ],
    "after:03_snapshots.sql": [
        ("Balance edited in the mart after load (account 3003, Aug 2005 +5,000)",
         "UPDATE fact_account_month SET statement_balance = statement_balance + 5000 "
         "WHERE account_id = 3003 AND month_index = 5", ["T09", "T10"]),
        ("Snapshot row lost (account 4004, Jun 2005)",
         "DELETE FROM fact_account_month WHERE account_id = 4004 AND month_index = 3", ["T03", "T09"]),
    ],
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build(raw: Path, db: Path, inject: bool = False, quiet: bool = False) -> duckdb.DuckDBPyConnection:
    db.parent.mkdir(parents=True, exist_ok=True)
    db.unlink(missing_ok=True)
    con = duckdb.connect(str(db))
    for f in SQL:
        con.execute((ROOT / "sql" / f).read_text().replace("{RAW}", raw.as_posix()))
        if not quiet:
            print(f"  ran sql/{f}")
        if inject:
            for label, stmt, _ in INJECTIONS.get(f"after:{f}", []):
                con.execute(stmt)
                if not quiet:
                    print(f"    planted: {label}")
    return con


def export(con: duckdb.DuckDBPyConnection, out: Path):
    (out / "tables").mkdir(parents=True, exist_ok=True)
    for t in EXPORT_TABLES:
        con.execute(f"COPY {t} TO '{(out / 'tables' / (t + '.csv')).as_posix()}' (HEADER)")


def dashboard_data(con: duckdb.DuckDBPyConnection) -> dict:
    """Columnar per-account data for the self-service dashboard (read from the mart, not the raw file)."""
    q = con.execute("""
        SELECT a.account_id, a.limit_band_order, a.age_band, a.education, a.marital_status, a.sex,
               a.defaulted_oct_2005::INTEGER AS d,
               list(f.bucket_order ORDER BY f.month_index)                         AS b,
               list(f.status_code ORDER BY f.month_index)                          AS s,
               list(ROUND(f.statement_balance)::BIGINT ORDER BY f.month_index)    AS bal,
               list(ROUND(f.payment_amount)::BIGINT ORDER BY f.month_index)       AS pay,
               ROUND(a.credit_limit)::BIGINT AS lim
        FROM dim_account a JOIN fact_account_month f USING (account_id)
        GROUP BY ALL ORDER BY a.account_id""").fetchall()
    cols = ["id", "lb", "ab", "ed", "ms", "sx", "d", "b", "s", "bal", "pay", "lim"]
    data = {c: [r[i] for r in q] for i, c in enumerate(cols)}
    dicts = {}
    for c in ["ab", "ed", "ms", "sx"]:          # dictionary-encode the text segments
        vals = sorted(set(data[c]))
        dicts[c] = vals
        idx = {v: i for i, v in enumerate(vals)}
        data[c] = [idx[v] for v in data[c]]
    for c in ["b", "s", "bal", "pay"]:          # flatten 6-month lists
        data[c] = [x for row in data[c] for x in row]
    months = [r[0] for r in con.execute("SELECT month_label FROM dim_month ORDER BY month_index").fetchall()]
    month_keys = [r[0] for r in con.execute("SELECT month_key FROM dim_month ORDER BY month_index").fetchall()]
    limit_bands = [r[0] for r in con.execute(
        "SELECT DISTINCT limit_band, limit_band_order FROM dim_account ORDER BY 2").fetchall()]
    dq = con.execute("SELECT test_id, test_name, category, severity, status, failing_rows, rule, sample "
                     "FROM dq_results ORDER BY test_id").df().to_dict("records")
    # reference figures the dashboard's own maths is checked against (tests/test_dashboard_parity.js)
    ref = dict(
        portfolio=con.execute("SELECT month_key, accounts_30plus, exposure, exposure_30plus, payments "
                              "FROM mart_portfolio_month ORDER BY month_index").df().to_dict("records"),
        roll=con.execute("SELECT to_month, from_bucket, to_bucket, accounts FROM mart_roll_rates "
                         "ORDER BY to_month_index, from_order, to_order").df().to_dict("records"),
        default_by_bucket=con.execute("SELECT bucket, accounts, defaults, exposure_at_default "
                                      "FROM mart_default_by_bucket ORDER BY bucket_order").df().to_dict("records"),
    )
    return dict(months=months, month_keys=month_keys, limit_bands=limit_bands, dicts=dicts, n=len(q), data=data, dq=dq, ref=ref)


def write_report(con: duckdb.DuckDBPyConnection, path: Path, inject: bool):
    res = con.execute("SELECT * FROM dq_results ORDER BY test_id").df()
    pm = con.execute("SELECT * FROM mart_portfolio_month ORDER BY month_index").df()
    L = ["# Data-quality report", "",
         f"Source: `UCI_Credit_Card.csv` (30,000 accounts, Apr-Sep 2005). Mode: {'planted defects' if inject else 'normal'}.", "",
         f"**{(res.status == 'PASS').sum()} passed, {(res.status == 'FAIL').sum()} failed, {(res.status == 'WARN').sum()} warnings.**", "",
         "| Test | Name | Severity | Status | Failing rows | Example |", "|---|---|---|---|---:|---|"]
    for r in res.itertuples():
        ex = (r.sample or "").replace("|", "/")[:160]
        L.append(f"| {r.test_id} | {r.test_name} | {r.severity} | {r.status} | {r.failing_rows:,} | {ex} |")
    L += ["", "## Portfolio by month", "", "| Month | Accounts | 30+ rate | 90+ rate | Exposure (NT$) | 30+ exposure share | Payment rate |",
          "|---|---:|---:|---:|---:|---:|---:|"]
    for r in pm.itertuples():
        pr = "" if r.payment_rate != r.payment_rate else f"{r.payment_rate:.1%}"
        L.append(f"| {r.month_label} | {r.accounts:,} | {r.rate_30plus:.1%} | {r.rate_90plus:.1%} | {r.exposure:,.0f} | "
                 f"{r.exposure_rate_30plus:.1%} | {pr} |")
    path.write_text("\n".join(L) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", default=str(ROOT / "data" / "raw" / "UCI_Credit_Card.csv"))
    ap.add_argument("--out", default=str(ROOT / "output"))
    ap.add_argument("--inject", action="store_true", help="plant four defects; the ERROR tests must catch them")
    ap.add_argument("--no-dashboard", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    raw, out = Path(args.raw), Path(args.out)
    if not raw.exists():
        print(f"Source file not found: {raw}\nSee data/README.md for where to get it.")
        return 2
    digest = sha256(raw)
    print(f"Building credit risk mart from {raw.name}")
    print(f"  source sha256 {digest[:16]}... " + ("(matches the published UCI file)" if digest == SOURCE_SHA256
                                                  else "(DIFFERENT from the published UCI file)"))
    db = out / ("credit_risk_mart_injected.duckdb" if args.inject else "credit_risk_mart.duckdb")
    con = build(raw, db, inject=args.inject)
    export(con, out / ("injected" if args.inject else ""))
    report = out / ("dq_report_injected.md" if args.inject else "dq_report.md")
    write_report(con, report, args.inject)
    if not args.no_dashboard and not args.inject:
        tpl = (ROOT / "dashboard" / "template.html").read_text()
        payload = json.dumps(dashboard_data(con), separators=(",", ":"), default=float).replace("</", "<\\/")
        (out / "credit_risk_dashboard.html").write_text(tpl.replace("__DATA__", payload))
        print(f"  wrote dashboard {out / 'credit_risk_dashboard.html'}")

    res = con.execute("SELECT test_id, test_name, severity, status, failing_rows, sample FROM dq_results ORDER BY test_id").df()
    print("\nData-quality tests")
    for r in res.itertuples():
        line = f"  {r.status:<4}  {r.test_id} {r.test_name:<40} {r.failing_rows:>7,} row(s)"
        if r.status == "FAIL":
            line += f"  e.g. {(r.sample or '')[:90]}"
        print(line)
    failed = int((res.status == "FAIL").sum())
    if args.inject:
        caught = []
        for defects in INJECTIONS.values():
            for label, _, tests in defects:
                hit = all(res.loc[res.test_id == t, "status"].iloc[0] == "FAIL" for t in tests)
                caught.append(hit)
                print(f"  {'caught' if hit else 'MISSED'}: {label} -> {', '.join(tests)}")
        print(f"\n{sum(caught)}/{len(caught)} planted defects caught ({time.time() - t0:.1f}s)")
        con.close()
        return 0 if all(caught) else 1
    print(f"\n{'MART NOT PUBLISHED - ' + str(failed) + ' test(s) failed' if failed else 'Mart ready'}"
          f"   ({time.time() - t0:.1f}s; report {report})")
    con.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
