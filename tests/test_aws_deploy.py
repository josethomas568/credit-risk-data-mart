"""Offline rehearsal of aws/deploy.py and aws/teardown.py.

Every AWS request is validated against botocore's service model (a malformed request fails the
test), Athena SQL runs in DuckDB on the exact files that get uploaded, and Hive DDL is parsed.
Deploy runs twice against the same fake account so the update paths are exercised too.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "aws"))
import awsutil as au  # noqa: E402
import deploy  # noqa: E402
import quicksight_view as qv  # noqa: E402
import teardown  # noqa: E402


@pytest.fixture(scope="module")
def shared():
    sess = au.DryRunSession("us-east-1")
    return sess


def run(monkeypatch, sess, module, argv):
    monkeypatch.setattr(au, "session", lambda *a, **k: sess)
    monkeypatch.setattr(sys, "argv", [module.__name__] + argv)
    return module.main()


def test_first_deploy_creates_everything(monkeypatch, shared):
    assert run(monkeypatch, shared, deploy, ["--dry-run"]) == 0
    ops = {op for svc, op in shared.calls}
    for op in ["create_stack", "put_object", "start_query_execution", "create_data_source", "create_data_set",
               "create_analysis", "create_dashboard"]:
        assert op in ops, op
    assert len([k for k in shared.objects if k.endswith(".parquet")]) == 18


def test_second_deploy_updates_in_place(monkeypatch, shared):
    shared.calls.clear()
    assert run(monkeypatch, shared, deploy, ["--dry-run", "--skip-export"]) == 0
    ops = [op for svc, op in shared.calls]
    assert "put_object" not in ops                       # unchanged files are not re-uploaded
    for op in ["update_data_source", "update_data_set", "create_ingestion", "update_analysis", "update_dashboard"]:
        assert op in ops, op
    assert not any(op.startswith("create_data") for op in ops)


def test_teardown_removes_everything(monkeypatch, shared):
    shared.calls.clear()
    assert run(monkeypatch, shared, teardown, ["--dry-run"]) == 0
    assert shared.objects == {}
    assert not shared.stack_created
    assert not shared.qs_state


def test_definition_uses_only_columns_that_exist():
    import json
    m = json.loads((au.LAKE / "manifest.json").read_text())
    cols = {t["name"]: {c["name"] for c in t.get("columns", [])} for t in m["tables"]}
    d = qv.definition({k: f"arn:{k}" for k in qv.DATASETS})
    calc = {c["Name"] for c in d["CalculatedFields"]}
    used = []

    def walk(o):
        if isinstance(o, dict):
            if "DataSetIdentifier" in o and "ColumnName" in o:
                used.append((o["DataSetIdentifier"], o["ColumnName"]))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(d)
    for ds, c in used:
        table, wanted = qv.DATASETS[ds]
        assert c in calc or (c in cols[table] and c in wanted), (ds, c)
    ids = [list(v.values())[0]["VisualId"] for s in d["Sheets"] for v in s["Visuals"]]
    scoped = {vid for fg in d["FilterGroups"] for sc in fg["ScopeConfiguration"]["SelectedSheets"]
              ["SheetVisualScopingConfigurations"] for vid in sc["VisualIds"]}
    assert scoped <= set(ids)
    laid_out = {e["ElementId"] for e in d["Sheets"][0]["Layouts"][0]["Configuration"]["GridLayout"]["Elements"]}
    assert set(ids) <= laid_out
