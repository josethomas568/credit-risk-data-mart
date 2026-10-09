"""pytest suite: build the mart (clean and with planted defects) and check the results."""
from pathlib import Path

import duckdb
import pytest

import build_mart as bm

RAW = bm.ROOT / "data" / "raw" / "UCI_Credit_Card.csv"


@pytest.fixture(scope="module")
def clean(tmp_path_factory):
    db = tmp_path_factory.mktemp("clean") / "mart.duckdb"
    con = bm.build(RAW, db, quiet=True)
    yield con
    con.close()


@pytest.fixture(scope="module")
def injected(tmp_path_factory):
    db = tmp_path_factory.mktemp("inj") / "mart.duckdb"
    con = bm.build(RAW, db, inject=True, quiet=True)
    yield con
    con.close()


def one(con, sql):
    return con.execute(sql).fetchone()[0]


def test_source_file_is_the_published_dataset():
    assert bm.sha256(RAW) == bm.SOURCE_SHA256


def test_grain(clean):
    assert one(clean, "SELECT COUNT(*) FROM dim_account") == 30_000
    assert one(clean, "SELECT COUNT(*) FROM fact_account_month") == 180_000


def test_all_blocking_tests_pass(clean):
    assert one(clean, "SELECT COUNT(*) FROM dq_results WHERE status = 'FAIL'") == 0


def test_default_rate_matches_the_model_notebook(clean):
    assert round(one(clean, "SELECT AVG(defaulted_oct_2005::INT) FROM dim_account"), 4) == 0.2212


def test_balances_reconcile_to_source(clean):
    diff = one(clean, """SELECT MAX(ABS(c.statement_balance - f.b)) FROM ctl_source_totals c
                         JOIN (SELECT month_index, SUM(statement_balance) b FROM fact_account_month GROUP BY 1) f USING (month_index)""")
    assert diff == 0


def test_roll_rates_sum_to_one(clean):
    worst = one(clean, """SELECT MAX(ABS(s - 1)) FROM (SELECT to_month_index, from_bucket, SUM(roll_rate) s
                          FROM mart_roll_rates GROUP BY ALL HAVING SUM(accounts) > 0)""")
    assert worst < 1e-9


def test_known_values(clean):
    # Sep 2005 60-day bucket: 2,667 accounts, 1,844 defaulted in Oct
    row = clean.execute("SELECT accounts, defaults FROM mart_default_by_bucket WHERE bucket = '60'").fetchone()
    assert row == (2667, 1844)


def test_sep_coding_break_is_flagged(clean):
    items = [r[0] for r in clean.execute("SELECT item FROM dq_failures WHERE test_id = 'T17'").fetchall()]
    assert "2005-09 code 1" in items


@pytest.mark.parametrize("label,_sql,tests", [d for ds in bm.INJECTIONS.values() for d in ds])
def test_planted_defect_is_caught(injected, label, _sql, tests):
    for t in tests:
        assert one(injected, f"SELECT status FROM dq_results WHERE test_id = '{t}'") == "FAIL", f"{label}: {t}"
