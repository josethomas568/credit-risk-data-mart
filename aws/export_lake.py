#!/usr/bin/env python3
"""
Export the DuckDB mart to an S3-ready data lake layout, and generate the Athena DDL from it.

    python aws/export_lake.py          # writes output/lake/ and aws/athena/01_tables.sql

Layout (mirrors the S3 bucket one-to-one):
    raw/uci_credit_card/UCI_Credit_Card.csv          source file, byte-for-byte
    source/stg_credit_raw/data.parquet               typed source table
    mart/<table>/data.parquet                        dimensions, facts, metric tables
    mart/fact_account_month/month_key=YYYY-MM/...    snapshot fact, partitioned by month
    quality/dq_results/data.parquet                  data-quality test results
    manifest.json                                    tables, columns, row counts, checksums

The DDL is generated from the Parquet files actually written, so Athena's column types always
match the files. Nothing here talks to AWS; aws/deploy.py uploads and registers it.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import build_mart  # noqa: E402

LAKE = ROOT / "output" / "lake"
DDL_OUT = ROOT / "aws" / "athena" / "01_tables.sql"
DATABASE = "credit_risk_mart"

# zone, table, partition column (or None), description
TABLES = [
    ("source", "stg_credit_raw", None, "UCI source file, typed; one row per account, wide months"),
    ("mart", "dim_account", None, "Account dimension with segment bands and Oct 2005 default flag"),
    ("mart", "dim_month", None, "Month dimension, Apr-Sep 2005"),
    ("mart", "dim_bucket", None, "Delinquency buckets: Current, 30, 60, 90+"),
    ("mart", "dim_status_code", None, "Repayment status codes and their bucket"),
    ("mart", "fact_account_month", "month_key", "Monthly account snapshot, partitioned by month"),
    ("mart", "mart_bucket_month", None, "Accounts and exposure by bucket and month"),
    ("mart", "mart_roll_rates", None, "Bucket-to-bucket transitions by month"),
    ("mart", "mart_roll_summary", None, "Headline roll rates by month"),
    ("mart", "mart_default_by_bucket", None, "Oct 2005 default rate by Sep 2005 bucket"),
    ("mart", "mart_default_by_segment", None, "Default rate by segment value"),
    ("mart", "mart_portfolio_month", None, "Portfolio delinquency, exposure and payment summary by month"),
    ("quality", "dq_results", None, "Data-quality test results from the build"),
]

HIVE = {"VARCHAR": "string", "INTEGER": "int", "BIGINT": "bigint", "DOUBLE": "double", "BOOLEAN": "boolean",
        "DATE": "date", "SMALLINT": "smallint", "TINYINT": "tinyint", "FLOAT": "float"}


def hive_type(duck: str) -> str:
    if duck.startswith("DECIMAL"):
        return duck.lower()
    if duck in HIVE:
        return HIVE[duck]
    raise ValueError(f"no Athena type for DuckDB type {duck}")


def select_sql(con, table: str) -> str:
    """SELECT with Athena-safe types: HUGEINT (DuckDB sums of ints) becomes BIGINT."""
    cols = con.execute(f"DESCRIBE {table}").fetchall()
    parts = [f'CAST("{c}" AS BIGINT) AS "{c}"' if t in ("HUGEINT", "UBIGINT") else f'"{c}"' for c, t, *_ in cols]
    return f"SELECT {', '.join(parts)} FROM {table}"


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def export(con: duckdb.DuckDBPyConnection, lake: Path = LAKE) -> dict:
    shutil.rmtree(lake, ignore_errors=True)
    lake.mkdir(parents=True)
    raw_src = ROOT / "data" / "raw" / "UCI_Credit_Card.csv"
    raw_dst = lake / "raw" / "uci_credit_card" / "UCI_Credit_Card.csv"
    raw_dst.parent.mkdir(parents=True)
    shutil.copyfile(raw_src, raw_dst)
    manifest = {"database": DATABASE, "tables": []}
    manifest["tables"].append(dict(
        zone="raw", name="raw_uci_credit_card", prefix="raw/uci_credit_card/", format="csv",
        description="UCI_Credit_Card.csv as published (all 25 columns)", partition=None,
        rows=con.execute("SELECT COUNT(*) FROM stg_credit_raw").fetchone()[0],
        files={"UCI_Credit_Card.csv": sha256(raw_dst)}))

    for zone, table, part, desc in TABLES:
        prefix = f"{zone}/{table}/"
        out = lake / prefix
        sql = select_sql(con, table)
        if part:
            out.mkdir(parents=True)
            con.execute(f"COPY ({sql}) TO '{out.as_posix()}' "
                        f"(FORMAT PARQUET, COMPRESSION SNAPPY, PARTITION_BY ({part}), FILENAME_PATTERN 'data_{{i}}')")
        else:
            out.mkdir(parents=True)
            con.execute(f"COPY ({sql}) TO '{(out / 'data.parquet').as_posix()}' (FORMAT PARQUET, COMPRESSION SNAPPY)")
        # schema and row count read back from the files written, not from DuckDB's table
        glob = (out / "**" / "*.parquet").as_posix()
        schema = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{glob}', hive_partitioning = false)").fetchall()
        rows = con.execute(f"SELECT COUNT(*) FROM read_parquet('{glob}')").fetchone()[0]
        cols = [dict(name=c, type=hive_type(t)) for c, t, *_ in schema]
        files = {str(p.relative_to(out)): sha256(p) for p in sorted(out.rglob("*.parquet"))}
        parts = sorted({p.parent.name.split("=", 1)[1] for p in out.rglob("*.parquet")}) if part else None
        manifest["tables"].append(dict(zone=zone, name=table, prefix=prefix, format="parquet", description=desc,
                                       partition=part, partition_values=parts, columns=cols, rows=rows, files=files))
    (lake / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def ddl(manifest: dict) -> str:
    """Athena DDL for every table. ${BUCKET} and ${DATABASE} are filled in by deploy.py."""
    out = ["-- Generated by aws/export_lake.py from the Parquet files it wrote. Do not edit by hand.",
           "-- deploy.py replaces ${BUCKET} and ${DATABASE} and runs each statement in Athena.", ""]
    for t in manifest["tables"]:
        name = t["name"]
        out.append(f"DROP TABLE IF EXISTS `${{DATABASE}}`.`{name}`;")
        if t["format"] == "csv":
            out.append(f"""CREATE EXTERNAL TABLE `${{DATABASE}}`.`{name}` (
  `id` int, `limit_bal` double, `sex` int, `education` int, `marriage` int, `age` int,
  `pay_0` int, `pay_2` int, `pay_3` int, `pay_4` int, `pay_5` int, `pay_6` int,
  `bill_amt1` double, `bill_amt2` double, `bill_amt3` double, `bill_amt4` double, `bill_amt5` double, `bill_amt6` double,
  `pay_amt1` double, `pay_amt2` double, `pay_amt3` double, `pay_amt4` double, `pay_amt5` double, `pay_amt6` double,
  `default_payment_next_month` int
)
COMMENT '{t["description"]}'
ROW FORMAT SERDE 'org.apache.hadoop.hive.serde2.OpenCSVSerde'
WITH SERDEPROPERTIES ('separatorChar' = ',', 'quoteChar' = '"', 'escapeChar' = '\\\\')
STORED AS TEXTFILE
LOCATION 's3://${{BUCKET}}/{t["prefix"]}'
TBLPROPERTIES ('skip.header.line.count' = '1');
""")
            continue
        cols = ",\n".join(f"  `{c['name']}` {c['type']}" for c in t["columns"])
        part = ""
        props = ["'parquet.compression' = 'SNAPPY'"]
        if t["partition"]:
            part = f"\nPARTITIONED BY (`{t['partition']}` string)"
            props += ["'projection.enabled' = 'true'",
                      f"'projection.{t['partition']}.type' = 'enum'",
                      f"'projection.{t['partition']}.values' = '{','.join(t['partition_values'])}'",
                      f"'storage.location.template' = 's3://${{BUCKET}}/{t['prefix']}{t['partition']}=${{{t['partition']}}}/'"]
        out.append(f"""CREATE EXTERNAL TABLE `${{DATABASE}}`.`{name}` (
{cols}
)
COMMENT '{t["description"]}'{part}
STORED AS PARQUET
LOCATION 's3://${{BUCKET}}/{t["prefix"]}'
TBLPROPERTIES ({', '.join(props)});
""")
    return "\n".join(out)


def main():
    raw = ROOT / "data" / "raw" / "UCI_Credit_Card.csv"
    db = ROOT / "output" / "credit_risk_mart.duckdb"
    con = build_mart.build(raw, db, quiet=True)
    failed = con.execute("SELECT COUNT(*) FROM dq_results WHERE status = 'FAIL'").fetchone()[0]
    if failed:
        print(f"Not exporting: {failed} blocking data-quality test(s) failed. Run build_mart.py for details.")
        return 1
    m = export(con)
    DDL_OUT.parent.mkdir(parents=True, exist_ok=True)
    DDL_OUT.write_text(ddl(m))
    size = sum(p.stat().st_size for p in LAKE.rglob("*") if p.is_file())
    print(f"Exported {len(m['tables'])} tables to {LAKE} ({size / 1e6:.1f} MB)")
    for t in m["tables"]:
        print(f"  {t['zone']:<8}{t['name']:<26}{t['rows']:>8,} rows  {len(t['files'])} file(s)")
    print(f"Wrote {DDL_OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
