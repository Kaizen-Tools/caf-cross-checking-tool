"""Cross-check logic for CAF requisition and issue exports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO
import re

import pandas as pd


REQUISITION_REQUIRED_COLUMNS = ["Part", "Quantity"]
ISSUE_REQUIRED_COLUMNS = ["Part", "Issue Qty."]
QUANTITY_TOLERANCE = 0.000001


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

    @property
    def is_valid(self) -> bool:
        return all(check.status == "Validated" for check in self.checks)


def run_cross_check(
    requisition_file: BinaryIO,
    requisition_filename: str,
    requisition_number: str,
    issue_file: BinaryIO,
    issue_filename: str,
    issue_number: str,
) -> CheckResult:
    """Read the two uploaded exports and run all business-rule checks."""

    checks: list[ChecklistItem] = []
    flags: list[str] = []
    next_steps: list[str] = []

    req_number = requisition_number.strip()
    iss_number = issue_number.strip()
    document_number = req_number if req_number == iss_number else f"{req_number} / {iss_number}"

    reference_valid, reference_message = validate_document_references(
        requisition_filename=requisition_filename,
        requisition_number=req_number,
        issue_filename=issue_filename,
        issue_number=iss_number,
    )
    add_check(checks, "Document numbers", reference_valid, reference_message)
    if not reference_valid:
        next_steps.append(
            "Confirm the requisition and issue numbers, then upload the matching Req and issue files."
        )

    req_df, req_error = read_export(requisition_file, requisition_filename)
    issue_df, issue_error = read_export(issue_file, issue_filename)

    if req_error or issue_error:
        if req_error:
            add_check(checks, "Read requisition file", False, req_error)
        if issue_error:
            add_check(checks, "Read issue file", False, issue_error)
        return empty_result(checks, flags, next_steps, document_number)

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
            flags,
            next_steps,
            document_number,
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

    row_count_valid = len(req_prepared) == len(issue_prepared)
    add_check(
        checks,
        "Row count",
        row_count_valid,
        f"Requisition has {len(req_prepared)} row(s); issue has {len(issue_prepared)} row(s).",
    )
    if not row_count_valid:
        next_steps.append(
            "Compare the export filters and data rows, then regenerate or amend the issue so the row count matches the requisition."
        )

    sku_summary = build_sku_summary(req_prepared, issue_prepared)
    sku_mismatches = sku_summary[sku_summary["Status"] == "Flagged"].reset_index(drop=True)
    sku_valid = sku_mismatches.empty
    flags = sku_flags(sku_mismatches)
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

    return CheckResult(
        checks=checks,
        flags=flags,
        next_steps=deduplicate(next_steps),
        summary={
            "document_number": document_number,
            "requisition_rows": len(req_prepared),
            "issue_rows": len(issue_prepared),
            "requisition_filename": requisition_filename,
            "issue_filename": issue_filename,
        },
        sku_summary=sku_summary,
        sku_mismatches=sku_mismatches,
    )


def read_export(file_obj: BinaryIO, filename: str) -> tuple[pd.DataFrame | None, str | None]:
    try:
        df = pd.read_excel(file_obj)
    except Exception as exc:
        return None, f"{filename}: could not read Excel file ({exc})"

    df.columns = [str(column).strip() for column in df.columns]
    df = df.dropna(how="all").copy()
    return df, None


def validate_document_references(
    requisition_filename: str,
    requisition_number: str,
    issue_filename: str,
    issue_number: str,
) -> tuple[bool, str]:
    req_norm = normalize_reference(requisition_number)
    issue_norm = normalize_reference(issue_number)

    if not req_norm or not issue_norm:
        return False, "Both document numbers must be entered."
    if req_norm != issue_norm:
        return False, "The entered requisition and issue numbers do not match."
    return True, "The entered requisition and issue numbers match."


def prepare_export(df: pd.DataFrame, quantity_column: str) -> pd.DataFrame:
    output = df.copy()
    output["_SKU Key"] = output["Part"].map(normalize_sku)
    output["_Qty"] = pd.to_numeric(output[quantity_column], errors="coerce")
    output["_Qty Invalid"] = output["_Qty"].isna()
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


def sku_flags(sku_mismatches: pd.DataFrame) -> list[str]:
    flags = []
    for _, row in sku_mismatches.iterrows():
        flags.append(
            f"{row['SKU']}: requisition quantity {format_quantity(row['Requisition Quantity'])}, "
            f"issue quantity {format_quantity(row['Issue Quantity'])}."
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
    document_number: str,
    requisition_rows: int = 0,
    issue_rows: int = 0,
) -> CheckResult:
    columns = ["Status"]
    return CheckResult(
        checks=checks,
        flags=flags,
        next_steps=deduplicate(next_steps),
        summary={
            "document_number": document_number,
            "requisition_rows": requisition_rows,
            "issue_rows": issue_rows,
        },
        sku_summary=pd.DataFrame(columns=columns),
        sku_mismatches=pd.DataFrame(columns=columns),
    )


def normalize_reference(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).strip().lower())


def normalize_sku(value: object) -> str:
    if pd.isna(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip().upper()


def format_quantity(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
