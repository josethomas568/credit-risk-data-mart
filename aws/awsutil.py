"""Shared helpers for deploy.py and teardown.py: sessions, CloudFormation, S3 sync, Athena, dry-run."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import time
import uuid
from pathlib import Path

import boto3
import botocore.session
import duckdb
from botocore.exceptions import ClientError
from botocore.validate import ParamValidator

ROOT = Path(__file__).resolve().parents[1]
LAKE = ROOT / "output" / "lake"
ATHENA_SQL = ROOT / "aws" / "athena"
STACK = "credit-risk-mart"
ZONES = ["raw/", "source/", "mart/", "quality/"]
POLL = 2.0


def log(msg: str = ""):
    print(msg, flush=True)


def code(e: ClientError) -> str:
    return e.response.get("Error", {}).get("Code", "")


def sql_statements(path: Path, database: str, bucket: str) -> list[str]:
    text = path.read_text().replace("${DATABASE}", database).replace("${BUCKET}", bucket)
    text = re.sub(r"(?m)^\s*--.*$", "", text)
    return [s.strip() for s in re.split(r";\s*(?:\n|$)", text) if s.strip()]


# ---------------------------------------------------------------------------- CloudFormation
def deploy_stack(cfn, template: str, params: dict, stack: str = STACK) -> dict:
    kw = dict(StackName=stack, TemplateBody=template, Capabilities=["CAPABILITY_IAM"],
              Parameters=[{"ParameterKey": k, "ParameterValue": str(v)} for k, v in params.items()])
    try:
        cfn.describe_stacks(StackName=stack)
        exists = True
    except ClientError as e:
        if "does not exist" not in str(e):
            raise
        exists = False
    try:
        if exists:
            cfn.update_stack(**kw)
            log(f"  updating stack {stack}")
        else:
            cfn.create_stack(**kw, Tags=[{"Key": "project", "Value": stack}], OnFailure="DELETE")
            log(f"  creating stack {stack}")
    except ClientError as e:
        if "No updates are to be performed" not in str(e):
            raise
        log(f"  stack {stack} already up to date")
    return wait_stack(cfn, stack)


def wait_stack(cfn, stack: str) -> dict:
    while True:
        s = cfn.describe_stacks(StackName=stack)["Stacks"][0]
        st = s["StackStatus"]
        if st.endswith("_IN_PROGRESS"):
            time.sleep(POLL * 2)
            continue
        if st in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
            return {o["OutputKey"]: o["OutputValue"] for o in s.get("Outputs", [])}
        reasons = [f"{ev['LogicalResourceId']}: {ev.get('ResourceStatusReason')}"
                   for ev in cfn.describe_stack_events(StackName=stack)["StackEvents"]
                   if "FAILED" in ev["ResourceStatus"]][:5]
        raise RuntimeError(f"stack {stack} ended in {st}: " + "; ".join(reasons))


# ---------------------------------------------------------------------------- S3
def sync_lake(s3, bucket: str, lake: Path = LAKE) -> tuple[int, int, int]:
    """Upload the local lake; skip unchanged files (ETag = MD5 for single-part puts); delete stale keys."""
    local = {p.relative_to(lake).as_posix(): p for p in sorted(lake.rglob("*")) if p.is_file()}
    remote = {}
    for z in ZONES + ["manifest.json"]:
        token = None
        while True:
            kw = dict(Bucket=bucket, Prefix=z)
            if token:
                kw["ContinuationToken"] = token
            r = s3.list_objects_v2(**kw)
            for o in r.get("Contents", []):
                remote[o["Key"]] = o["ETag"].strip('"')
            token = r.get("NextContinuationToken")
            if not token:
                break
    up = skip = 0
    for key, p in local.items():
        body = p.read_bytes()
        if remote.get(key) == hashlib.md5(body).hexdigest():
            skip += 1
            continue
        ctype = "application/vnd.apache.parquet" if key.endswith(".parquet") else (
            mimetypes.guess_type(key)[0] or "application/octet-stream")
        s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=ctype)
        up += 1
    stale = [k for k in remote if k not in local]
    for i in range(0, len(stale), 1000):
        s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in stale[i:i + 1000]], "Quiet": True})
    return up, skip, len(stale)


def empty_bucket(s3, bucket: str) -> int:
    n = 0
    while True:
        r = s3.list_objects_v2(Bucket=bucket)
        keys = [{"Key": o["Key"]} for o in r.get("Contents", [])]
        if not keys:
            return n
        s3.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
        n += len(keys)


# ---------------------------------------------------------------------------- Athena
class Athena:
    def __init__(self, client, workgroup: str, database: str):
        self.c, self.wg, self.db = client, workgroup, database
        self.bytes_scanned = 0
        self.queries = 0

    def run(self, sql: str) -> list[dict]:
        qid = self.c.start_query_execution(QueryString=sql, WorkGroup=self.wg,
                                           QueryExecutionContext={"Database": self.db})["QueryExecutionId"]
        while True:
            q = self.c.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
            state = q["Status"]["State"]
            if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            time.sleep(POLL / 2)
        self.queries += 1
        self.bytes_scanned += q.get("Statistics", {}).get("DataScannedInBytes", 0)
        if state != "SUCCEEDED":
            raise RuntimeError(f"Athena query {state}: {q['Status'].get('StateChangeReason')}\n{sql[:300]}")
        rows, token, header = [], None, None
        while True:
            kw = dict(QueryExecutionId=qid)
            if token:
                kw["NextToken"] = token
            r = self.c.get_query_results(**kw)
            data = [[d.get("VarCharValue") for d in row["Data"]] for row in r["ResultSet"]["Rows"]]
            if header is None:
                if not data:
                    return []
                header, data = data[0], data[1:]
            rows += [dict(zip(header, d)) for d in data]
            token = r.get("NextToken")
            if not token:
                return rows

    @property
    def cost_usd(self) -> float:
        # $5 per TB scanned; DDL and failed queries are not charged
        return self.bytes_scanned / 1e12 * 5


# ---------------------------------------------------------------------------- local DuckDB stand-in for Athena
def connect_lake(lake: Path = LAKE, database: str = "credit_risk_mart") -> duckdb.DuckDBPyConnection:
    """Register every lake table in DuckDB under the Athena database name (used by tests and --dry-run)."""
    manifest = json.loads((lake / "manifest.json").read_text())
    c = duckdb.connect()
    c.execute(f"CREATE SCHEMA {database}")
    for t in manifest["tables"]:
        path = (lake / t["prefix"]).as_posix() + "/"
        if t["format"] == "csv":
            bills = ", ".join(f'"BILL_AMT{i}"::DOUBLE AS bill_amt{i}' for i in range(1, 7))
            pays = ", ".join(f'"PAY_AMT{i}"::DOUBLE AS pay_amt{i}' for i in range(1, 7))
            c.execute(f"""CREATE VIEW {database}.{t['name']} AS SELECT
                "ID" AS id, "LIMIT_BAL"::DOUBLE AS limit_bal, "SEX" AS sex, "EDUCATION" AS education,
                "MARRIAGE" AS marriage, "AGE" AS age, "PAY_0" AS pay_0, "PAY_2" AS pay_2, "PAY_3" AS pay_3,
                "PAY_4" AS pay_4, "PAY_5" AS pay_5, "PAY_6" AS pay_6, {bills}, {pays},
                "default.payment.next.month" AS default_payment_next_month
                FROM read_csv('{path}*.csv', header = true)""")
        else:
            hp = "true" if t["partition"] else "false"
            c.execute(f"CREATE VIEW {database}.{t['name']} AS "
                      f"SELECT * FROM read_parquet('{path}**/*.parquet', hive_partitioning = {hp})")
    return c


# ---------------------------------------------------------------------------- dry run
class DryRunClient:
    """Stands in for a boto3 client. Validates every request against the real AWS API model, then
    answers from a handler. Unhandled operations return {}."""

    def __init__(self, service: str, handlers: dict, calls: list):
        self.service = service
        self.model = botocore.session.get_session().get_service_model(service)
        self.ops = {self._snake(o): o for o in self.model.operation_names}
        self.handlers, self.calls = handlers, calls
        self.validator = ParamValidator()

    @staticmethod
    def _snake(name: str) -> str:
        return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower().replace("_s3", "s3")

    def __getattr__(self, op: str):
        if op not in self.ops:
            raise AttributeError(f"{self.service} has no operation {op}")

        def call(**params):
            om = self.model.operation_model(self.ops[op])
            report = self.validator.validate(params, om.input_shape) if om.input_shape else None
            if report and report.has_errors():
                raise ValueError(f"{self.service}.{op}: invalid request\n{report.generate_report()}")
            self.calls.append((self.service, op))
            h = self.handlers.get(op)
            return h(**params) if h else {}
        return call


def not_found(op: str, msg: str = "not found", code_: str = "ResourceNotFoundException"):
    raise ClientError({"Error": {"Code": code_, "Message": msg}}, op)


class DryRunSession:
    """Rehearses a deploy offline: AWS calls are validated and recorded; Athena SQL runs in DuckDB
    against the local lake; Hive DDL is parsed (sqlglot) instead of executed."""
    ACCOUNT = "123456789012"

    def __init__(self, region: str = "us-east-1"):
        self.region_name = region
        self.calls: list = []
        self.duck = connect_lake()
        self.qs_state: dict = {}
        self.stack_created = False
        self.queries: dict = {}
        self.objects: dict = {}

    # -- service handlers
    def _cfn(self):
        acct, reg = self.ACCOUNT, self.region_name
        outputs = [{"OutputKey": "BucketName", "OutputValue": f"credit-risk-mart-{acct}-{reg}"},
                   {"OutputKey": "DatabaseName", "OutputValue": "credit_risk_mart"},
                   {"OutputKey": "WorkGroupName", "OutputValue": "credit-risk-mart"},
                   {"OutputKey": "QuickSightRoleArn", "OutputValue": f"arn:aws:iam::{acct}:role/credit-risk-mart-qs"}]

        def describe_stacks(StackName):
            if not self.stack_created:
                raise ClientError({"Error": {"Code": "ValidationError",
                                             "Message": f"Stack with id {StackName} does not exist"}}, "DescribeStacks")
            return {"Stacks": [{"StackName": StackName, "StackStatus": "CREATE_COMPLETE", "Outputs": outputs}]}

        def create_stack(**kw):
            self.stack_created = True
            return {"StackId": "arn:aws:cloudformation:stack/" + kw["StackName"]}

        def delete_stack(**kw):
            self.stack_created = False
            return {}
        return {"describe_stacks": describe_stacks, "create_stack": create_stack, "delete_stack": delete_stack,
                "describe_stack_events": lambda **kw: {"StackEvents": []}}

    def _s3(self):
        def list_objects_v2(Bucket, Prefix="", ContinuationToken=None):
            return {"Contents": [{"Key": k, "ETag": f'"{v}"'} for k, v in self.objects.items() if k.startswith(Prefix)]}

        def put_object(Bucket, Key, Body, **kw):
            self.objects[Key] = hashlib.md5(Body).hexdigest()
            return {"ETag": self.objects[Key]}

        def delete_objects(Bucket, Delete):
            for o in Delete["Objects"]:
                self.objects.pop(o["Key"], None)
            return {}
        return {"list_objects_v2": list_objects_v2, "put_object": put_object, "delete_objects": delete_objects}

    def _athena(self):
        import sqlglot

        def start(QueryString, **kw):
            qid = str(uuid.uuid4())
            s = QueryString.strip()
            if re.match(r"(?i)^(DROP TABLE|CREATE EXTERNAL TABLE)", s):
                sqlglot.parse_one(s, read="athena")            # Hive DDL: parse only
                self.queries[qid] = (None, None)
            else:
                sqlglot.parse_one(s, read="trino")
                cur = self.duck.execute(s)
                cols = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchall() if cols else []
                self.queries[qid] = (cols, rows)
            return {"QueryExecutionId": qid}

        def get_exec(QueryExecutionId):
            return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}, "Statistics": {"DataScannedInBytes": 0}}}

        def fmt(v):
            return None if v is None else ("true" if v is True else "false" if v is False else str(v))

        def results(QueryExecutionId, NextToken=None):
            cols, rows = self.queries[QueryExecutionId]
            if not cols:
                return {"ResultSet": {"Rows": []}}
            out = [{"Data": [{"VarCharValue": c} for c in cols]}]
            out += [{"Data": [{"VarCharValue": fmt(v)} if v is not None else {} for v in r]} for r in rows]
            return {"ResultSet": {"Rows": out}}
        return {"start_query_execution": start, "get_query_execution": get_exec, "get_query_results": results}

    def _quicksight(self):
        acct, reg, st = self.ACCOUNT, self.region_name, self.qs_state

        def arn(kind, rid):
            return f"arn:aws:quicksight:{reg}:{acct}:{kind}/{rid}"

        def creator(kind, idkey):
            def f(**kw):
                st[(kind, kw[idkey])] = kw
                return {"Arn": arn(kind, kw[idkey]), "Status": 201, "CreationStatus": "CREATION_IN_PROGRESS"}
            return f

        def describer(kind, idkey, outkey, status_key="Status"):
            def f(**kw):
                if (kind, kw[idkey]) not in st:
                    not_found("Describe" + outkey)
                body = {"Arn": arn(kind, kw[idkey]), status_key: "CREATION_SUCCESSFUL", "Errors": []}
                if kind == "dashboard":
                    body = {"Arn": arn(kind, kw[idkey]), "Version": {"VersionNumber": 1, "Status": "CREATION_SUCCESSFUL",
                                                                     "Errors": []}}
                return {outkey: body}
            return f

        def deleter(kind, idkey):
            def f(**kw):
                if st.pop((kind, kw[idkey]), None) is None:
                    not_found("Delete")
                return {}
            return f

        return {
            "describe_account_subscription": lambda **kw: {"AccountInfo": {"AccountSubscriptionStatus": "ACCOUNT_CREATED",
                                                                           "Edition": "ENTERPRISE"}},
            "list_users": lambda **kw: {"UserList": [{"UserName": "jose", "Role": "ADMIN",
                                                      "Arn": f"arn:aws:quicksight:us-east-1:{acct}:user/default/jose"}]},
            "create_data_source": creator("datasource", "DataSourceId"),
            "describe_data_source": describer("datasource", "DataSourceId", "DataSource"),
            "delete_data_source": deleter("datasource", "DataSourceId"),
            "create_data_set": creator("dataset", "DataSetId"),
            "describe_data_set": describer("dataset", "DataSetId", "DataSet"),
            "delete_data_set": deleter("dataset", "DataSetId"),
            "list_ingestions": lambda **kw: {"Ingestions": [{"IngestionId": "i1", "IngestionStatus": "COMPLETED",
                                                             "RowInfo": {"RowsIngested": 1}}]},
            "create_analysis": creator("analysis", "AnalysisId"),
            "describe_analysis": describer("analysis", "AnalysisId", "Analysis"),
            "delete_analysis": deleter("analysis", "AnalysisId"),
            "create_dashboard": creator("dashboard", "DashboardId"),
            "describe_dashboard": describer("dashboard", "DashboardId", "Dashboard"),
            "delete_dashboard": deleter("dashboard", "DashboardId"),
        }

    def client(self, service: str, region_name: str | None = None):
        handlers = {"sts": {"get_caller_identity": lambda **kw: {"Account": self.ACCOUNT,
                                                                  "Arn": f"arn:aws:iam::{self.ACCOUNT}:user/jose"}},
                    "cloudformation": self._cfn(), "s3": self._s3(), "athena": self._athena(),
                    "quicksight": self._quicksight()}[service]
        return DryRunClient(service, handlers, self.calls)


def session(profile: str | None, region: str, dry_run: bool):
    if dry_run:
        return DryRunSession(region)
    return boto3.Session(profile_name=profile, region_name=region)
