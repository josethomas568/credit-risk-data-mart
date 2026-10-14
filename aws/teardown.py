#!/usr/bin/env python3
"""
Remove everything deploy.py created: QuickSight dashboard, analysis, datasets and data source;
every object in the S3 bucket; then the CloudFormation stack (bucket, Glue database and its
tables and views, Athena workgroup, IAM role). Your QuickSight subscription is not touched;
cancel it in the QuickSight console if you no longer need it.

    python aws/teardown.py            # asks for confirmation
    python aws/teardown.py --yes
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import awsutil as au  # noqa: E402
import quicksight_view as qv  # noqa: E402


def gone(fn, **kw) -> bool:
    try:
        fn(**kw)
        return True
    except ClientError as e:
        if au.code(e) in ("ResourceNotFoundException", "AccessDeniedException", "UnsupportedUserEditionException"):
            return False
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--profile")
    ap.add_argument("--stack", default=au.STACK)
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sess = au.session(args.profile, args.region, args.dry_run)
    if args.dry_run:                                  # pretend a full deploy exists
        sess.stack_created = True
        for k, i in [("dashboard", qv.DASHBOARD_ID), ("analysis", qv.ANALYSIS_ID), ("datasource", qv.DATA_SOURCE_ID)]:
            sess.qs_state[(k, i)] = {}
        for key in qv.DATASETS:
            sess.qs_state[("dataset", qv.dataset_id(key))] = {}
    account = sess.client("sts").get_caller_identity()["Account"]
    if not (args.yes or args.dry_run):
        if input(f"Delete the credit risk mart from account {account} ({args.region})? Type 'delete': ") != "delete":
            print("Nothing deleted.")
            return 1

    qs = sess.client("quicksight")
    print("QuickSight")
    for label, fn, kw in [
        ("dashboard", qs.delete_dashboard, dict(DashboardId=qv.DASHBOARD_ID)),
        ("analysis", qs.delete_analysis, dict(AnalysisId=qv.ANALYSIS_ID, ForceDeleteWithoutRecovery=True)),
        *[(f"dataset {k}", qs.delete_data_set, dict(DataSetId=qv.dataset_id(k))) for k in qv.DATASETS],
        ("data source", qs.delete_data_source, dict(DataSourceId=qv.DATA_SOURCE_ID)),
    ]:
        print(f"  {label}: {'deleted' if gone(fn, AwsAccountId=account, **kw) else 'not found'}")

    cfn = sess.client("cloudformation")
    try:
        stack = cfn.describe_stacks(StackName=args.stack)["Stacks"][0]
    except ClientError as e:
        if "does not exist" in str(e):
            print(f"Stack {args.stack} not found; nothing else to delete.")
            return 0
        raise
    bucket = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}.get("BucketName")
    if bucket:
        print(f"S3: deleted {au.empty_bucket(sess.client('s3'), bucket)} objects from {bucket}")
    cfn.delete_stack(StackName=args.stack)
    print(f"CloudFormation: deleting stack {args.stack}")
    while True:
        try:
            st = cfn.describe_stacks(StackName=args.stack)["Stacks"][0]["StackStatus"]
        except ClientError as e:
            if "does not exist" in str(e):
                break
            raise
        if st == "DELETE_COMPLETE":
            break
        if st == "DELETE_FAILED":
            print("  stack deletion failed; check the CloudFormation console")
            return 1
        time.sleep(au.POLL * 2)
    print("  deleted. Nothing from this project is left in the account.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
