"""Cross-check logic for CAF requisition and issue exports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO

import pandas as pd


REQUISITION_REQUIRED_COLUMNS = ["Part", "Quantity"]
ISSUE_REQUIRED_COLUMNS = ["Part", "Issue Qty.", "From Bin"]
QUANTITY_TOLERANCE = 0.000001
EXPIRY_LOT_COLUMNS = [
    "Expiration Date",
    "Expiry Date",
    "Expiry",
    "Lot",
    "Lot Number",
    "Manufacturer Lot",
    "Manufacturer Lot Number",
]


@dataclass(frozen=True)
class ChecklistItem:
    title: str
    status: str
    message: str


@dataclass(frozen=True)
class CheckResult:
    checks: list[ChecklistItem]
    flags: list[str]
    next_steps: list[str]
    summary: dict[str, object]
    sku_summary: pd.DataFrame
    sku_mismatches: pd.DataFrame
    duplicate_issue_rows: pd.DataFrame
    missing_locations: pd.DataFrame

    @property
    def is_valid(self) -> bool:
        return all(check.status == "Validated" for check in self.checks)


def run_cross_check(
    requisition_file: BinaryIO,
    requisition_filename: str,
    issue_file: BinaryIO,
    issue_filename: str,
) -> CheckResult:
    """Read the two uploaded exports and run all business-rule checks."""

    checks: list[ChecklistItem] = []
    next_steps: list[str] = []

    req_df, req_error = read_export(requisition_file, requisition_filename)
    issue_df, issue_error = read_export(issue_file, issue_filename)

    if req_error or issue_error:
        if req_error:
            add_check(checks, "Read requisition file", False, req_error)
        if issue_error:
            add_check(checks, "Read issue file", False, issue_error)
        return empty_result(checks, [], next_steps)

    assert req_df is not None
    assert issue_df is not None

    req_missing = missing_columns(req_df, REQUISITION_REQUIRED_COLUMNS)
    issue_missing = missing_columns(issue_df, ISSUE_REQUIRED_COLUMNS)
    required_valid = not req_missing and not issue_missing
    required_message = required_columns_message(req_missing, issue_missing)
    add_check(checks, "Required columns", required_valid, required_message)
    if not required_valid:
        next_steps.append(
            "Re-export the documents from GMAO or correct the workbook so the required columns are present."
        )
        return empty_result(
            checks,
            [],
            next_steps,
            requisition_rows=len(req_df),
            issue_rows=len(issue_df),
        )

    req_prepared = prepare_export(req_df, "Quantity")
    issue_prepared = prepare_export(issue_df, "Issue Qty.")

    invalid_qty = invalid_quantity_summary(req_prepared, issue_prepared)
    qty_format_valid = invalid_qty.empty
    add_check(
        checks,
        "Quantity values",
        qty_format_valid,
        "All required quantities are numeric."
        if qty_format_valid
        else "One or more quantity values are missing or non-numeric.",
    )
    if not qty_format_valid:
        next_steps.append(
            "Investigate rows with missing or non-numeric quantities before relying on the reconciliation result."
        )

    effective_issue_rows = effective_issue_row_count(issue_prepared)
    row_count_valid = len(req_prepared) == effective_issue_rows
    add_check(
        checks,
        "Row count",
        row_count_valid,
        (
            f"Requisition has {len(req_prepared)} row(s); issue has {len(issue_prepared)} raw row(s), "
            f"or {effective_issue_rows} effective row(s) after valid expiry/lot splits."
        ),
    )
    if not row_count_valid:
        next_steps.append(
            "Compare the export filters and data rows, then regenerate or amend the issue so the row count matches the requisition after valid expiry/lot splits."
        )

    sku_summary = build_sku_summary(req_prepared, issue_prepared)
    sku_mismatches = sku_summary[sku_summary["Status"] == "Flagged"].reset_index(drop=True)
    sku_valid = sku_mismatches.empty
    add_check(
        checks,
        "SKU quantity totals",
        sku_valid,
        "Every SKU total matches between requisition Quantity and issue Issue Qty."
        if sku_valid
        else f"{len(sku_mismatches)} SKU(s) have quantity differences.",
    )
    if not sku_valid:
        next_steps.append(
            "Use the requisition as the client request and investigate each flagged SKU quantity before picking."
        )

    duplicate_issue_rows = find_duplicate_issue_rows(issue_prepared)
    duplicate_valid = duplicate_issue_rows.empty
    add_check(
        checks,
        "Duplicate issue SKU quantities",
        duplicate_valid,
        "No issue rows have the same SKU and same issue quantity without unique expiry/lot details."
        if duplicate_valid
        else f"{len(duplicate_issue_rows)} issue row(s) have the same SKU and same issue quantity without unique expiry/lot details.",
    )
    if not duplicate_valid:
        next_steps.append(
            "Investigate duplicate issue rows with the same SKU and quantity unless they should be separated by unique expiry or lot details."
        )

    missing_locations = find_missing_issue_locations(issue_prepared)
    location_valid = missing_locations.empty
    add_check(
        checks,
        "Issue location completeness",
        location_valid,
        "All issue rows have a recorded location."
        if location_valid
        else f"{len(missing_locations)} issue row(s) have missing locations.",
    )
    if not location_valid:
        next_steps.append(
            "Verify issue rows with missing locations and remove or correct them before picking if necessary."
        )

    flags = (
        sku_flags(sku_mismatches)
        + duplicate_issue_flags(duplicate_issue_rows)
        + missing_location_flags(missing_locations)
    )

    return CheckResult(
        checks=checks,
        flags=flags,
        next_steps=deduplicate(next_steps),
        summary={
            "requisition_rows": len(req_prepared),
            "issue_rows": len(issue_prepared),
            "effective_issue_rows": effective_issue_rows,
            "requisition_filename": requisition_filename,
            "issue_filename": issue_filename,
        },
        sku_summary=sku_summary,
        sku_mismatches=sku_mismatches,
        duplicate_issue_rows=duplicate_issue_rows,
        missing_locations=missing_locations,
    )


def read_export(file_obj: BinaryIO, filename: str) -> tuple[pd.DataFrame | None, str | None]:
    try:
        df = pd.read_excel(file_obj)
    except Exception as exc:
        return None, f"{filename}: could not read Excel file ({exc})"

    df.columns = [str(column).strip() for column in df.columns]
    df = df.dropna(how="all").copy()
    return df, None


def prepare_export(df: pd.DataFrame, quantity_column: str) -> pd.DataFrame:
    output = df.copy()
    output["_Excel Row"] = output.index + 2
    output["_SKU Key"] = output["Part"].map(normalize_sku)
    output["_Qty"] = pd.to_numeric(output[quantity_column], errors="coerce")
    output["_Qty Invalid"] = output["_Qty"].isna()
    if "From Bin" in output.columns:
        output["_Location Key"] = output["From Bin"].map(normalize_location)
    return output


def build_sku_summary(req_df: pd.DataFrame, issue_df: pd.DataFrame) -> pd.DataFrame:
    req_totals = aggregate_by_sku(req_df, "Requisition Quantity")
    issue_totals = aggregate_by_sku(issue_df, "Issue Quantity")
    summary = req_totals.merge(issue_totals, on="SKU", how="outer")
    summary["Requisition Quantity"] = summary["Requisition Quantity"].fillna(0)
    summary["Issue Quantity"] = summary["Issue Quantity"].fillna(0)
    summary["Difference"] = summary["Issue Quantity"] - summary["Requisition Quantity"]
    summary["Status"] = summary["Difference"].abs().le(QUANTITY_TOLERANCE).map(
        {True: "Validated", False: "Flagged"}
    )
    return summary.sort_values(["Status", "SKU"], ascending=[True, True]).reset_index(drop=True)


def aggregate_by_sku(df: pd.DataFrame, quantity_name: str) -> pd.DataFrame:
    return (
        df.groupby("_SKU Key", dropna=False)["_Qty"]
        .sum(min_count=1)
        .reset_index()
        .rename(columns={"_SKU Key": "SKU", "_Qty": quantity_name})
    )


def effective_issue_row_count(issue_df: pd.DataFrame) -> int:
    effective_rows = 0
    for _, group in issue_df.groupby("_SKU Key", dropna=False):
        if is_valid_expiry_lot_split(group):
            effective_rows += 1
        else:
            effective_rows += len(group)
    return effective_rows


def is_valid_expiry_lot_split(group: pd.DataFrame) -> bool:
    if len(group) < 2:
        return False

    signatures = group.apply(expiry_lot_signature, axis=1)
    return signatures.ne("").all() and signatures.is_unique


def invalid_quantity_summary(req_df: pd.DataFrame, issue_df: pd.DataFrame) -> pd.DataFrame:
    req_invalid = invalid_quantity_rows(req_df, "Requisition")
    issue_invalid = invalid_quantity_rows(issue_df, "Issue")
    if not req_invalid and not issue_invalid:
        return pd.DataFrame()
    return pd.DataFrame(req_invalid + issue_invalid)


def invalid_quantity_rows(df: pd.DataFrame, document_type: str) -> list[dict[str, object]]:
    invalid = df[df["_Qty Invalid"]]
    return [
        {
            "Document": document_type,
            "SKU": row.get("Part", ""),
        }
        for _, row in invalid.iterrows()
    ]


def find_duplicate_issue_rows(issue_df: pd.DataFrame) -> pd.DataFrame:
    valid = issue_df[
        issue_df["_SKU Key"].ne("")
        & issue_df["_Qty"].notna()
    ].copy()
    duplicate_rows = flagged_duplicate_issue_rows(valid)
    if duplicate_rows.empty:
        return pd.DataFrame(
            columns=[
                "Issue Row",
                "Issue Line",
                "SKU",
                "Issue Quantity",
                "Duplicate Count",
                "Expiry/Lot Details",
            ]
        )

    duplicate_rows["Duplicate Count"] = duplicate_rows.groupby(["_SKU Key", "_Qty"])["_SKU Key"].transform("size")
    duplicate_rows["Issue Quantity"] = duplicate_rows["_Qty"]
    duplicate_rows["SKU"] = duplicate_rows["_SKU Key"]
    duplicate_rows["Issue Row"] = duplicate_rows["_Excel Row"]
    duplicate_rows["Issue Line"] = duplicate_rows["Line"] if "Line" in duplicate_rows.columns else ""

    return duplicate_rows.loc[
        :,
        ["Issue Row", "Issue Line", "SKU", "Issue Quantity", "Duplicate Count", "Expiry/Lot Details"],
    ].sort_values(["SKU", "Issue Quantity", "Issue Row"]).reset_index(drop=True)


def flagged_duplicate_issue_rows(issue_df: pd.DataFrame) -> pd.DataFrame:
    flagged_groups = []
    for _, group in issue_df.groupby(["_SKU Key", "_Qty"], dropna=False):
        if len(group) < 2:
            continue

        group = group.copy()
        group["Expiry/Lot Details"] = group.apply(expiry_lot_signature, axis=1)
        has_missing_expiry_lot = group["Expiry/Lot Details"].eq("")
        repeated_expiry_lot = group["Expiry/Lot Details"].duplicated(keep=False)
        flagged = group[has_missing_expiry_lot | repeated_expiry_lot]

        if not flagged.empty:
            flagged_groups.append(flagged)

    if not flagged_groups:
        return pd.DataFrame()
    return pd.concat(flagged_groups, ignore_index=True)


def expiry_lot_signature(row: pd.Series) -> str:
    values = []
    for column in EXPIRY_LOT_COLUMNS:
        if column not in row.index:
            continue
        normalized = normalize_expiry_lot_value(row[column])
        if normalized:
            values.append(f"{column}: {normalized}")
    return " | ".join(values)


def normalize_expiry_lot_value(value: object) -> str:
    if pd.isna(value):
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")

    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat"} or text == "*":
        return ""
    return text.upper()


def find_missing_issue_locations(issue_df: pd.DataFrame) -> pd.DataFrame:
    flagged = issue_df[issue_df["_Location Key"].eq("")].copy()
    if flagged.empty:
        return pd.DataFrame(columns=["Issue Row", "Issue Line", "SKU", "Issue Quantity", "From Bin"])

    flagged["Issue Row"] = flagged["_Excel Row"]
    flagged["Issue Line"] = flagged["Line"] if "Line" in flagged.columns else ""
    flagged["SKU"] = flagged["_SKU Key"]
    flagged["Issue Quantity"] = flagged["_Qty"]

    return flagged.loc[
        :,
        ["Issue Row", "Issue Line", "SKU", "Issue Quantity", "From Bin"],
    ].sort_values(["Issue Row"]).reset_index(drop=True)


def sku_flags(sku_mismatches: pd.DataFrame) -> list[str]:
    flags = []
    for _, row in sku_mismatches.iterrows():
        flags.append(
            f"{row['SKU']}: requisition quantity {format_quantity(row['Requisition Quantity'])}, "
            f"issue quantity {format_quantity(row['Issue Quantity'])}."
        )
    return flags


def duplicate_issue_flags(duplicate_issue_rows: pd.DataFrame) -> list[str]:
    flags = []
    for _, row in duplicate_issue_rows.iterrows():
        line_text = f", issue line {row['Issue Line']}" if row.get("Issue Line", "") != "" else ""
        expiry_lot_text = (
            f" Expiry/lot details: {row['Expiry/Lot Details']}."
            if row.get("Expiry/Lot Details", "") != ""
            else " No unique expiry/lot details were found."
        )
        flags.append(
            f"Issue row {row['Issue Row']}{line_text}: duplicate SKU {row['SKU']} "
            f"with issue quantity {format_quantity(row['Issue Quantity'])}.{expiry_lot_text}"
        )
    return flags


def missing_location_flags(missing_locations: pd.DataFrame) -> list[str]:
    flags = []
    for _, row in missing_locations.iterrows():
        line_text = f", issue line {row['Issue Line']}" if row.get("Issue Line", "") != "" else ""
        flags.append(
            f"Issue row {row['Issue Row']}{line_text}: SKU {row['SKU']} is missing a location."
        )
    return flags


def missing_columns(df: pd.DataFrame, required_columns: list[str]) -> list[str]:
    return [column for column in required_columns if column not in df.columns]


def required_columns_message(req_missing: list[str], issue_missing: list[str]) -> str:
    if not req_missing and not issue_missing:
        return (
            "Required requisition columns are present: "
            f"{', '.join(REQUISITION_REQUIRED_COLUMNS)}. Required issue columns are present: "
            f"{', '.join(ISSUE_REQUIRED_COLUMNS)}."
        )

    messages = []
    if req_missing:
        messages.append(f"Requisition missing: {', '.join(req_missing)}")
    if issue_missing:
        messages.append(f"Issue missing: {', '.join(issue_missing)}")
    return "; ".join(messages)


def add_check(
    checks: list[ChecklistItem],
    title: str,
    is_valid: bool,
    message: str,
) -> None:
    status = "Validated" if is_valid else "Flagged"
    checks.append(ChecklistItem(title=title, status=status, message=message))


def empty_result(
    checks: list[ChecklistItem],
    flags: list[str],
    next_steps: list[str],
    requisition_rows: int = 0,
    issue_rows: int = 0,
) -> CheckResult:
    columns = ["Status"]
    return CheckResult(
        checks=checks,
        flags=flags,
        next_steps=deduplicate(next_steps),
        summary={
            "requisition_rows": requisition_rows,
            "issue_rows": issue_rows,
            "effective_issue_rows": issue_rows,
        },
        sku_summary=pd.DataFrame(columns=columns),
        sku_mismatches=pd.DataFrame(columns=columns),
        duplicate_issue_rows=pd.DataFrame(columns=columns),
        missing_locations=pd.DataFrame(columns=columns),
    )


def normalize_sku(value: object) -> str:
    if pd.isna(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip().upper()


def normalize_location(value: object) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip().upper()


def format_quantity(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
