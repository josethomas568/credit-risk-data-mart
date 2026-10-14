#!/usr/bin/env python3
"""
Deploy the credit risk data mart to AWS: S3 data lake -> Athena -> QuickSight.

    python aws/deploy.py                       # everything, region us-east-1, default AWS profile
    python aws/deploy.py --profile me --region us-east-2
    python aws/deploy.py --skip-quicksight     # S3 + Athena only
    python aws/deploy.py --dry-run             # rehearse offline: no AWS calls, no credentials needed

Steps
  1. Build the mart and export it to Parquet (aws/export_lake.py)
  2. CloudFormation stack: S3 bucket, Glue database, Athena workgroup (1 GB scan cap), QuickSight role
  3. Upload raw/, source/, mart/, quality/ to S3 (unchanged files are skipped)
  4. Athena: create 14 tables and 3 views
  5. Athena: run 7 validation checks; exit 1 if any fails
  6. QuickSight: Athena data source, 4 SPICE datasets, risk-manager analysis and dashboard

Re-running is safe: every step updates what exists. aws/teardown.py removes everything.
Cost: Athena about $0.0001 per full run; QuickSight needs a subscription (see aws/README.md).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from botocore.exceptions import ClientError, NoCredentialsError, ProfileNotFound

sys.path.insert(0, str(Path(__file__).resolve().parent))
import awsutil as au  # noqa: E402
import export_lake  # noqa: E402
import quicksight_view as qv  # noqa: E402


def athena_step(sess, out: dict, database: str) -> tuple[bool, au.Athena]:
    ath = au.Athena(sess.client("athena"), out["WorkGroupName"], database)
    ddl = au.sql_statements(au.ATHENA_SQL / "01_tables.sql", database, out["BucketName"])
    for s in ddl:
        ath.run(s)
    log_n = sum(1 for s in ddl if s.upper().startswith("CREATE"))
    au.log(f"  created {log_n} tables")
    views = au.sql_statements(au.ATHENA_SQL / "02_views.sql", database, out["BucketName"])
    for s in views:
        ath.run(s)
    au.log(f"  created {len(views)} views")
    au.log("\n5. Validation checks in Athena")
    ok = True
    for s in au.sql_statements(au.ATHENA_SQL / "03_checks.sql", database, out["BucketName"]):
        r = ath.run(s)[0]
        passed = str(r["passed"]).lower() == "true"
        ok &= passed
        au.log(f"  {'PASS' if passed else 'FAIL'}  {r['check_name']:<56} {r['detail']}")
    return ok, ath


def wait_qs(fn, status_path, ok=("CREATION_SUCCESSFUL", "UPDATE_SUCCESSFUL"), what=""):
    while True:
        obj = fn()
        cur = obj
        for k in status_path:
            cur = cur[k]
        if cur in ok:
            return obj
        if cur.endswith("FAILED"):
            raise RuntimeError(f"QuickSight {what} {cur}: {json.dumps(obj, default=str)[:800]}")
        time.sleep(au.POLL)


def qs_owner(qs_id, account: str, user: str | None) -> str:
    if user:
        return qs_id.describe_user(AwsAccountId=account, Namespace="default", UserName=user)["User"]["Arn"]
    users = qs_id.list_users(AwsAccountId=account, Namespace="default")["UserList"]
    admins = [u for u in users if u.get("Role", "").startswith("ADMIN")]
    if not admins:
        raise RuntimeError("no QuickSight admin user found; pass --quicksight-user")
    return admins[0]["Arn"]


def quicksight_step(sess, args, account: str, out: dict, database: str) -> str | None:
    qs = sess.client("quicksight")
    try:
        info = qs.describe_account_subscription(AwsAccountId=account)["AccountInfo"]
        status = info.get("AccountSubscriptionStatus", "")
    except ClientError as e:
        if au.code(e) not in ("ResourceNotFoundException", "AccessDeniedException"):
            raise
        status = "NOT_SUBSCRIBED"
    if "UNSUBSCRIBED" in status or status == "NOT_SUBSCRIBED":
        au.log("  QuickSight is not set up in this account yet, so this step is skipped.\n"
               "  Sign up once in the console (see aws/README.md, 'QuickSight'), then run:\n"
               "    python aws/deploy.py --skip-export")
        return None
    qs_id = sess.client("quicksight", region_name=args.quicksight_identity_region or args.region)
    owner = qs_owner(qs_id, account, args.quicksight_user)
    au.log(f"  owner {owner.split('/')[-1]}")
    manifest = json.loads((au.LAKE / "manifest.json").read_text())

    # data source
    req = qv.data_source_request(account, out["WorkGroupName"], out["QuickSightRoleArn"], owner)
    try:
        qs.describe_data_source(AwsAccountId=account, DataSourceId=qv.DATA_SOURCE_ID)
        qs.update_data_source(**{k: v for k, v in req.items() if k not in ("Type", "Permissions")})
    except ClientError as e:
        if au.code(e) != "ResourceNotFoundException":
            raise
        qs.create_data_source(**req)
    ds = wait_qs(lambda: qs.describe_data_source(AwsAccountId=account, DataSourceId=qv.DATA_SOURCE_ID),
                 ["DataSource", "Status"], what="data source")["DataSource"]
    au.log(f"  data source {qv.DATA_SOURCE_ID}")

    # datasets (SPICE)
    arns = {}
    for key in qv.DATASETS:
        req = qv.dataset_request(account, key, ds["Arn"], database, manifest, owner)
        try:
            qs.describe_data_set(AwsAccountId=account, DataSetId=req["DataSetId"])
            qs.update_data_set(**{k: v for k, v in req.items() if k != "Permissions"})
            qs.create_ingestion(AwsAccountId=account, DataSetId=req["DataSetId"],
                                IngestionId=f"deploy-{int(time.time())}", IngestionType="FULL_REFRESH")
        except ClientError as e:
            if au.code(e) != "ResourceNotFoundException":
                raise
            qs.create_data_set(**req)
        while True:
            ing = qs.list_ingestions(AwsAccountId=account, DataSetId=req["DataSetId"])["Ingestions"]
            latest = ing[0] if ing else {"IngestionStatus": "QUEUED"}
            if latest["IngestionStatus"] == "COMPLETED":
                break
            if latest["IngestionStatus"] in ("FAILED", "CANCELLED"):
                raise RuntimeError(f"SPICE import of {key} failed: {latest.get('ErrorInfo')}")
            time.sleep(au.POLL)
        arns[key] = f"arn:aws:quicksight:{args.region}:{account}:dataset/{req['DataSetId']}"
        au.log(f"  dataset {req['DataSetId']} imported to SPICE "
               f"({latest.get('RowInfo', {}).get('RowsIngested', '?')} rows)")

    # analysis and dashboard
    for kind, req, desc, upd, create, idkey in [
        ("analysis", qv.analysis_request(account, arns, owner), qs.describe_analysis, qs.update_analysis,
         qs.create_analysis, "AnalysisId"),
        ("dashboard", qv.dashboard_request(account, arns, owner), qs.describe_dashboard, qs.update_dashboard,
         qs.create_dashboard, "DashboardId")]:
        rid = req[idkey]
        try:
            desc(AwsAccountId=account, **{idkey: rid})
            upd(**{k: v for k, v in req.items() if k != "Permissions"})
        except ClientError as e:
            if au.code(e) != "ResourceNotFoundException":
                raise
            create(**req)
        path = ["Analysis", "Status"] if kind == "analysis" else ["Dashboard", "Version", "Status"]
        obj = wait_qs(lambda: desc(AwsAccountId=account, **{idkey: rid}), path, what=kind)
        errors = obj["Analysis"].get("Errors") if kind == "analysis" else obj["Dashboard"]["Version"].get("Errors")
        if errors:
            au.log(f"  {kind} warnings: {errors}")
        if kind == "dashboard":
            ver = obj["Dashboard"]["Version"]["VersionNumber"]
            if ver > 1:
                qs.update_dashboard_published_version(AwsAccountId=account, DashboardId=rid, VersionNumber=ver)
        au.log(f"  {kind} {rid}")
    return f"https://{args.region}.quicksight.aws.amazon.com/sn/dashboards/{qv.DASHBOARD_ID}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--profile")
    ap.add_argument("--stack", default=au.STACK)
    ap.add_argument("--skip-export", action="store_true", help="upload the existing output/lake as is")
    ap.add_argument("--skip-quicksight", action="store_true")
    ap.add_argument("--quicksight-user", help="QuickSight user name to own the assets (default: first admin)")
    ap.add_argument("--quicksight-identity-region", help="region where QuickSight users live, if different")
    ap.add_argument("--dry-run", action="store_true", help="rehearse offline against the AWS API models")
    args = ap.parse_args()

    t0 = time.time()
    au.log(f"Deploying credit risk mart to AWS ({args.region}){' - DRY RUN' if args.dry_run else ''}")
    au.log("\n1. Export mart to Parquet")
    if args.skip_export and (au.LAKE / "manifest.json").exists():
        au.log("  using existing output/lake")
    elif export_lake.main() != 0:
        return 1
    database = json.loads((au.LAKE / "manifest.json").read_text())["database"]

    try:
        sess = au.session(args.profile, args.region, args.dry_run)
        account = sess.client("sts").get_caller_identity()["Account"]
    except (NoCredentialsError, ProfileNotFound) as e:
        au.log(f"No AWS credentials: {e}. Run 'aws configure' (or pass --profile) and try again.")
        return 2
    au.log(f"  AWS account {account}")

    au.log("\n2. Infrastructure (CloudFormation)")
    out = au.deploy_stack(sess.client("cloudformation"), (au.ROOT / "aws" / "infra.yaml").read_text(),
                          {"DatabaseName": database}, args.stack)
    au.log(f"  bucket {out['BucketName']}, workgroup {out['WorkGroupName']}")

    au.log("\n3. Upload to S3")
    up, skip, stale = au.sync_lake(sess.client("s3"), out["BucketName"])
    au.log(f"  {up} uploaded, {skip} unchanged, {stale} stale removed -> s3://{out['BucketName']}/")

    au.log("\n4. Athena tables and views")
    ok, ath = athena_step(sess, out, database)
    au.log(f"  {ath.queries} queries, {ath.bytes_scanned / 1e6:.1f} MB scanned, about ${ath.cost_usd:.4f}")
    if not ok:
        au.log("\nValidation failed; QuickSight step not run.")
        return 1

    url = None
    if not args.skip_quicksight:
        au.log("\n6. QuickSight")
        url = quicksight_step(sess, args, account, out, database)

    au.log(f"\nDone in {time.time() - t0:.0f}s.")
    au.log(f"  Athena console: https://{args.region}.console.aws.amazon.com/athena/home?region={args.region}"
           f"#/query-editor (workgroup {out['WorkGroupName']}, database {database})")
    if url:
        au.log(f"  QuickSight dashboard: {url}")
    if args.dry_run:
        n = len(sess.calls)
        au.log(f"  dry run: {n} AWS API calls validated against the service models; none were sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
