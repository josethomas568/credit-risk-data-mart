"""QuickSight definitions for the risk manager view: data source, four SPICE datasets, and one sheet.

The sheet rebuilds the dashboard's "Risk manager" view:
    month control -> 30+ rate, 90+ rate, current-to-delinquent roll rate, default rate (KPIs)
                     delinquency trend (line), roll-rate matrix (pivot), default rate by segment (bar)
Every request built here is checked against the QuickSight API model in tests/test_aws_deploy.py.
"""
from __future__ import annotations

PREFIX = "credit-risk-mart"
DATA_SOURCE_ID = f"{PREFIX}-athena"
ANALYSIS_ID = f"{PREFIX}-risk-view"
DASHBOARD_ID = f"{PREFIX}-risk-view"
SHEET = "risk-manager"
MONTHS = ["2005-04", "2005-05", "2005-06", "2005-07", "2005-08", "2005-09"]
SEGMENTS = ["Limit band", "Age band", "Education", "Marital status", "Sex", "Sep 2005 bucket", "Worst status, 6 months"]

# dataset key -> (Athena table, columns used)
DATASETS = {
    "portfolio": ("mart_portfolio_month", ["month_index", "month_key", "month_label", "accounts", "accounts_30plus",
                                           "accounts_90plus", "rate_30plus", "rate_60plus", "rate_90plus"]),
    "rollsummary": ("mart_roll_summary", ["to_month_index", "to_month", "current_to_delinquent", "cure_rate"]),
    "rollrates": ("mart_roll_rates", ["to_month", "from_bucket", "from_order", "to_bucket", "to_order",
                                      "accounts", "from_bucket_accounts", "roll_rate", "movement"]),
    "segments": ("mart_default_by_segment", ["segment_type", "segment_value", "sort_order", "accounts", "defaults",
                                             "default_rate"]),
}

QS_TYPE = {"string": "STRING", "int": "INTEGER", "bigint": "INTEGER", "double": "DECIMAL", "boolean": "BOOLEAN",
           "date": "DATETIME"}

OWNER_ACTIONS = {
    "datasource": ["quicksight:DescribeDataSource", "quicksight:DescribeDataSourcePermissions",
                   "quicksight:PassDataSource", "quicksight:UpdateDataSource", "quicksight:DeleteDataSource",
                   "quicksight:UpdateDataSourcePermissions"],
    "dataset": ["quicksight:DescribeDataSet", "quicksight:DescribeDataSetPermissions", "quicksight:PassDataSet",
                "quicksight:DescribeIngestion", "quicksight:ListIngestions", "quicksight:UpdateDataSet",
                "quicksight:DeleteDataSet", "quicksight:CreateIngestion", "quicksight:CancelIngestion",
                "quicksight:UpdateDataSetPermissions"],
    "analysis": ["quicksight:RestoreAnalysis", "quicksight:UpdateAnalysisPermissions", "quicksight:DeleteAnalysis",
                 "quicksight:DescribeAnalysisPermissions", "quicksight:QueryAnalysis", "quicksight:DescribeAnalysis",
                 "quicksight:UpdateAnalysis"],
    "dashboard": ["quicksight:DescribeDashboard", "quicksight:ListDashboardVersions",
                  "quicksight:UpdateDashboardPermissions", "quicksight:QueryDashboard", "quicksight:UpdateDashboard",
                  "quicksight:DeleteDashboard", "quicksight:DescribeDashboardPermissions",
                  "quicksight:UpdateDashboardPublishedVersion"],
}


def dataset_id(key: str) -> str:
    return f"{PREFIX}-{key}"


def perms(owner_arn: str, kind: str) -> list[dict]:
    return [{"Principal": owner_arn, "Actions": OWNER_ACTIONS[kind]}]


# ---------------------------------------------------------------------------------- requests
def data_source_request(account: str, workgroup: str, role_arn: str, owner: str) -> dict:
    return dict(AwsAccountId=account, DataSourceId=DATA_SOURCE_ID, Name="Credit risk mart (Athena)", Type="ATHENA",
                DataSourceParameters={"AthenaParameters": {"WorkGroup": workgroup, "RoleArn": role_arn}},
                Permissions=perms(owner, "datasource"))


def dataset_request(account: str, key: str, data_source_arn: str, database: str, manifest: dict, owner: str) -> dict:
    table, cols = DATASETS[key]
    schema = {c["name"]: c["type"] for t in manifest["tables"] if t["name"] == table for c in t["columns"]}
    missing = [c for c in cols if c not in schema]
    if missing:
        raise ValueError(f"{table} has no column(s) {missing}")

    def qtype(t: str) -> str:
        return "DECIMAL" if t.startswith("decimal") else QS_TYPE[t]

    return dict(
        AwsAccountId=account, DataSetId=dataset_id(key), Name=f"Credit risk mart - {table}", ImportMode="SPICE",
        PhysicalTableMap={key: {"RelationalTable": {
            "DataSourceArn": data_source_arn, "Catalog": "AwsDataCatalog", "Schema": database, "Name": table,
            "InputColumns": [{"Name": c, "Type": qtype(schema[c])} for c in cols]}}},
        LogicalTableMap={key: {"Alias": table, "Source": {"PhysicalTableId": key}}},
        Permissions=perms(owner, "dataset"))


# ---------------------------------------------------------------------------------- definition helpers
def col(ds: str, name: str) -> dict:
    return {"DataSetIdentifier": ds, "ColumnName": name}


def pct(decimals: int = 1) -> dict:
    return {"FormatConfiguration": {"PercentageDisplayFormatConfiguration": {
        "DecimalPlacesConfiguration": {"DecimalPlaces": decimals}}}}


def measure(fid: str, ds: str, name: str, agg: str = "MAX", fmt: dict | None = None) -> dict:
    m = {"FieldId": fid, "Column": col(ds, name), "AggregationFunction": {"SimpleNumericalAggregation": agg}}
    if fmt:
        m["FormatConfiguration"] = fmt
    return {"NumericalMeasureField": m}


def dim(fid: str, ds: str, name: str) -> dict:
    return {"CategoricalDimensionField": {"FieldId": fid, "Column": col(ds, name)}}


def title(text: str) -> dict:
    return {"Visibility": "VISIBLE", "FormatText": {"PlainText": text}}


def subtitle(text: str) -> dict:
    return {"Visibility": "VISIBLE", "FormatText": {"PlainText": text}}


def kpi(vid: str, text: str, sub: str, value: dict) -> dict:
    return {"KPIVisual": {
        "VisualId": vid, "Title": title(text), "Subtitle": subtitle(sub),
        "ChartConfiguration": {"FieldWells": {"Values": [value]},
                               "KPIOptions": {"PrimaryValueDisplayType": "ACTUAL"}}}}


def sort_by(ds: str, column: str, direction: str = "ASC") -> dict:
    return {"SortBy": col(ds, column), "Direction": direction,
            "AggregationFunction": {"NumericalAggregationFunction": {"SimpleNumericalAggregation": "MIN"}}}


def param_filter(fgid: str, ds: str, column: str, param: str, visuals: list[str]) -> dict:
    return {"FilterGroupId": fgid, "CrossDataset": "SINGLE_DATASET", "Status": "ENABLED",
            "Filters": [{"CategoryFilter": {"FilterId": f"{fgid}-f", "Column": col(ds, column), "Configuration": {
                "CustomFilterConfiguration": {"MatchOperator": "EQUALS", "ParameterName": param,
                                              "NullOption": "NON_NULLS_ONLY"}}}}],
            "ScopeConfiguration": {"SelectedSheets": {"SheetVisualScopingConfigurations": [
                {"SheetId": SHEET, "Scope": "SELECTED_VISUALS", "VisualIds": visuals}]}}}


def static_filter(fgid: str, ds: str, column: str, value: str, visuals: list[str]) -> dict:
    return {"FilterGroupId": fgid, "CrossDataset": "SINGLE_DATASET", "Status": "ENABLED",
            "Filters": [{"CategoryFilter": {"FilterId": f"{fgid}-f", "Column": col(ds, column), "Configuration": {
                "CustomFilterConfiguration": {"MatchOperator": "EQUALS", "CategoryValue": value,
                                              "NullOption": "NON_NULLS_ONLY"}}}}],
            "ScopeConfiguration": {"SelectedSheets": {"SheetVisualScopingConfigurations": [
                {"SheetId": SHEET, "Scope": "SELECTED_VISUALS", "VisualIds": visuals}]}}}


def grid(eid: str, kind: str, col_i: int, row_i: int, cspan: int, rspan: int) -> dict:
    return {"ElementId": eid, "ElementType": kind, "ColumnIndex": col_i, "RowIndex": row_i,
            "ColumnSpan": cspan, "RowSpan": rspan}


# ---------------------------------------------------------------------------------- the sheet
def definition(dataset_arns: dict[str, str]) -> dict:
    decl = [{"Identifier": k, "DataSetArn": arn} for k, arn in dataset_arns.items()]
    params = [
        {"StringParameterDeclaration": {"ParameterValueType": "SINGLE_VALUED", "Name": "pMonth",
                                        "DefaultValues": {"StaticValues": ["2005-09"]}}},
        {"StringParameterDeclaration": {"ParameterValueType": "SINGLE_VALUED", "Name": "pSegment",
                                        "DefaultValues": {"StaticValues": ["Limit band"]}}},
    ]
    controls = [
        {"Dropdown": {"ParameterControlId": "ctl-month", "Title": "Month", "SourceParameterName": "pMonth",
                      "Type": "SINGLE_SELECT", "SelectableValues": {"Values": MONTHS}}},
        {"Dropdown": {"ParameterControlId": "ctl-segment", "Title": "Segment by", "SourceParameterName": "pSegment",
                      "Type": "SINGLE_SELECT", "SelectableValues": {"Values": SEGMENTS}}},
    ]
    visuals = [
        kpi("kpi-30plus", "30+ days past due", "Share of accounts, selected month",
            measure("k30", "portfolio", "rate_30plus", fmt=pct(1))),
        kpi("kpi-90plus", "90+ days past due", "Share of accounts, selected month",
            measure("k90", "portfolio", "rate_90plus", fmt=pct(2))),
        kpi("kpi-roll", "Current to delinquent", "Roll rate into the selected month (none for Apr)",
            measure("kroll", "rollsummary", "current_to_delinquent", fmt=pct(1))),
        kpi("kpi-default", "Default rate, Oct 2005", "Accounts that defaulted on the next payment",
            {"NumericalMeasureField": {"FieldId": "kdef", "Column": col("segments", "Portfolio default rate"),
                                       "FormatConfiguration": pct(1)}}),
        {"LineChartVisual": {
            "VisualId": "line-delinquency", "Title": title("Delinquency rate by month"),
            "Subtitle": subtitle("30+ jumps in Sep 2005 partly because the source started using code 1 (test T17)"),
            "ChartConfiguration": {
                "FieldWells": {"LineChartAggregatedFieldWells": {
                    "Category": [dim("lm", "portfolio", "month_label")],
                    "Values": [measure("l30", "portfolio", "rate_30plus", fmt=pct(1)),
                               measure("l60", "portfolio", "rate_60plus", fmt=pct(1)),
                               measure("l90", "portfolio", "rate_90plus", fmt=pct(1))]}},
                "SortConfiguration": {"CategorySort": [{"ColumnSort": sort_by("portfolio", "month_index")}]},
                "Legend": {"Visibility": "VISIBLE", "Position": "BOTTOM"},
                "DataLabels": {"Visibility": "HIDDEN"}}}},
        {"PivotTableVisual": {
            "VisualId": "pivot-roll", "Title": title("Roll rates into the selected month"),
            "Subtitle": subtitle("Rows: bucket last month. Columns: bucket this month. Each row sums to 100%"),
            "ChartConfiguration": {
                "FieldWells": {"PivotTableAggregatedFieldWells": {
                    "Rows": [dim("pr", "rollrates", "from_bucket")],
                    "Columns": [dim("pc", "rollrates", "to_bucket")],
                    "Values": [measure("pv", "rollrates", "roll_rate", agg="SUM", fmt=pct(1))]}},
                "SortConfiguration": {"FieldSortOptions": [
                    {"FieldId": "pr", "SortBy": {"Column": sort_by("rollrates", "from_order")}},
                    {"FieldId": "pc", "SortBy": {"Column": sort_by("rollrates", "to_order")}}]},
                "TotalOptions": {"RowTotalOptions": {"TotalsVisibility": "HIDDEN"},
                                 "ColumnTotalOptions": {"TotalsVisibility": "HIDDEN"}}}}},
        {"BarChartVisual": {
            "VisualId": "bar-segment", "Title": title("Default rate by segment"),
            "Subtitle": subtitle("Pick the segment with the 'Segment by' control. Portfolio rate: 22.1%"),
            "ChartConfiguration": {
                "Orientation": "HORIZONTAL",
                "FieldWells": {"BarChartAggregatedFieldWells": {
                    "Category": [dim("bs", "segments", "segment_value")],
                    "Values": [measure("bv", "segments", "default_rate", fmt=pct(1))]}},
                "SortConfiguration": {"CategorySort": [{"ColumnSort": sort_by("segments", "sort_order")}]},
                "DataLabels": {"Visibility": "VISIBLE"},
                "Legend": {"Visibility": "HIDDEN"}}}},
    ]
    filters = [
        param_filter("fg-month-portfolio", "portfolio", "month_key", "pMonth", ["kpi-30plus", "kpi-90plus"]),
        param_filter("fg-month-rollsummary", "rollsummary", "to_month", "pMonth", ["kpi-roll"]),
        param_filter("fg-month-rollrates", "rollrates", "to_month", "pMonth", ["pivot-roll"]),
        param_filter("fg-segment", "segments", "segment_type", "pSegment", ["bar-segment"]),
        static_filter("fg-default-total", "segments", "segment_type", "Sep 2005 bucket", ["kpi-default"]),
    ]
    note = ("<text-box>Source data note: September 2005 is coded differently from April to August. "
            "Status code 1 (one month late) covers 12.3% of accounts in September and almost none before, "
            "so September's 30+ rate and its moves into and out of the 30 bucket partly reflect the "
            "coding change. Data-quality test T17 flags it on every build.</text-box>")
    layout = [grid("kpi-30plus", "VISUAL", 0, 0, 9, 5), grid("kpi-90plus", "VISUAL", 9, 0, 9, 5),
              grid("kpi-roll", "VISUAL", 18, 0, 9, 5), grid("kpi-default", "VISUAL", 27, 0, 9, 5),
              grid("line-delinquency", "VISUAL", 0, 5, 18, 12), grid("pivot-roll", "VISUAL", 18, 5, 18, 12),
              grid("bar-segment", "VISUAL", 0, 17, 24, 13), grid("note-sep", "TEXT_BOX", 24, 17, 12, 7)]
    sheet = {"SheetId": SHEET, "Name": "Risk manager", "ContentType": "INTERACTIVE",
             "ParameterControls": controls, "Visuals": visuals,
             "TextBoxes": [{"SheetTextBoxId": "note-sep", "Content": note}],
             "Layouts": [{"Configuration": {"GridLayout": {"Elements": layout}}}]}
    # aggregated calculated field: defaults / accounts over whatever rows the filters leave
    calc = [{"DataSetIdentifier": "segments", "Name": "Portfolio default rate",
             "Expression": "sum({defaults}) / sum({accounts})"}]
    return {"DataSetIdentifierDeclarations": decl, "CalculatedFields": calc, "ParameterDeclarations": params,
            "FilterGroups": filters, "Sheets": [sheet]}


def analysis_request(account: str, dataset_arns: dict, owner: str) -> dict:
    return dict(AwsAccountId=account, AnalysisId=ANALYSIS_ID, Name="Credit risk mart - risk manager view",
                Definition=definition(dataset_arns), Permissions=perms(owner, "analysis"),
                ValidationStrategy={"Mode": "STRICT"})


def dashboard_request(account: str, dataset_arns: dict, owner: str) -> dict:
    return dict(AwsAccountId=account, DashboardId=DASHBOARD_ID, Name="Credit risk mart - risk manager view",
                Definition=definition(dataset_arns), Permissions=perms(owner, "dashboard"),
                ValidationStrategy={"Mode": "STRICT"},
                VersionDescription="Risk manager view rebuilt from the DuckDB dashboard",
                DashboardPublishOptions={"AdHocFilteringOption": {"AvailabilityStatus": "ENABLED"},
                                         "ExportToCSVOption": {"AvailabilityStatus": "ENABLED"},
                                         "SheetControlsOption": {"VisibilityState": "EXPANDED"}})
