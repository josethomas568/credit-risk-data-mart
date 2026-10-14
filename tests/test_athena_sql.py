"""Offline tests for the AWS port.

The Athena SQL can't run here, so it is tested two ways before deploy:
  1. Syntax: every statement parses as Trino (Athena engine v3) SQL, and the DDL as Hive/Athena DDL (sqlglot).
  2. Logic: the views and checks run in DuckDB against the exact Parquet/CSV files that get uploaded,
     with each table registered under the same database name Athena uses. Every check must pass.
"""
import json
import re
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[1]
LAKE = ROOT / "output" / "lake"
ATHENA = ROOT / "aws" / "athena"
DB = "credit_risk_mart"


def statements(path: Path, db: str = DB, bucket: str = "example-bucket") -> list[str]:
    text = path.read_text().replace("${DATABASE}", db).replace("${BUCKET}", bucket)
    text = re.sub(r"(?m)^\s*--.*$", "", text)
    return [s.strip() for s in re.split(r";\s*(?:\n|$)", text) if s.strip()]


@pytest.fixture(scope="module")
def lake():
    import sys
    sys.path.insert(0, str(ROOT / "aws"))
    import export_lake
    if not (LAKE / "manifest.json").exists():
        assert export_lake.main() == 0
    return json.loads((LAKE / "manifest.json").read_text())


@pytest.fixture(scope="module")
def con(lake):
    c = duckdb.connect()
    c.execute(f"CREATE SCHEMA {DB}")
    for t in lake["tables"]:
        path = (LAKE / t["prefix"]).as_posix() + "/"
        if t["format"] == "csv":
            c.execute(f"""CREATE VIEW {DB}.{t['name']} AS SELECT
                "ID" AS id, "LIMIT_BAL"::DOUBLE AS limit_bal, "SEX" AS sex, "EDUCATION" AS education,
                "MARRIAGE" AS marriage, "AGE" AS age, "PAY_0" AS pay_0, "PAY_2" AS pay_2, "PAY_3" AS pay_3,
                "PAY_4" AS pay_4, "PAY_5" AS pay_5, "PAY_6" AS pay_6,
                {', '.join(f'"BILL_AMT{i}"::DOUBLE AS bill_amt{i}' for i in range(1, 7))},
                {', '.join(f'"PAY_AMT{i}"::DOUBLE AS pay_amt{i}' for i in range(1, 7))},
                "default.payment.next.month" AS default_payment_next_month
                FROM read_csv('{path}*.csv', header = true)""")
        else:
            hp = "true" if t["partition"] else "false"
            c.execute(f"CREATE VIEW {DB}.{t['name']} AS SELECT * FROM read_parquet('{path}**/*.parquet', hive_partitioning = {hp})")
    for s in statements(ATHENA / "02_views.sql"):
        c.execute(s)
    yield c
    c.close()


def test_ddl_matches_files(lake):
    ddl = (ATHENA / "01_tables.sql").read_text()
    for t in lake["tables"]:
        assert f"`{t['name']}`" in ddl
        for col in t.get("columns", []):
            assert f"`{col['name']}` {col['type']}" in ddl, (t["name"], col)


def test_sql_parses_as_athena():
    sqlglot = pytest.importorskip("sqlglot")
    for f in ["02_views.sql", "03_checks.sql"]:
        for s in statements(ATHENA / f):
            sqlglot.parse_one(s, read="trino")          # raises on syntax Athena's engine would reject
    for s in statements(ATHENA / "01_tables.sql"):
        sqlglot.parse_one(s, read="athena")


@pytest.mark.parametrize("stmt", statements(ATHENA / "03_checks.sql"), ids=lambda s: s.split("'")[1][:40])
def test_check_passes(con, stmt):
    name, passed, detail = con.execute(stmt).fetchone()
    assert passed, f"{name}: {detail}"


def test_views_match_known_values(con):
    r = con.execute(f"SELECT accounts_30plus, ROUND(rate_30plus, 4) FROM {DB}.v_delinquency_by_month "
                    "WHERE month_key = '2005-09'").fetchone()
    assert r == (6818, 0.2273)
    n = con.execute(f"SELECT SUM(accounts) FROM {DB}.v_default_by_limit_and_bucket").fetchone()[0]
    assert n == 30000


@pytest.mark.parametrize("n", range(5))
def test_example_queries_run(con, n):
    sqlglot = pytest.importorskip("sqlglot")
    stmt = statements(ATHENA / "examples.sql")[n]
    sqlglot.parse_one(stmt, read="trino")
    rows = con.execute(stmt).fetchall()
    assert rows
    if n == 4:
        assert rows[0][0] == rows[0][1]          # raw CSV and mart agree
