from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pandas as pd
import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError

from requisition_issue_checker import (
    CheckResult,
    run_cross_check,
)


APP_TITLE = "CAF Requisition and Issue Cross-Check Tool"
ASSET_DIR = Path(__file__).resolve().parent / "assets"


st.set_page_config(page_title=APP_TITLE, layout="wide")


def check_password() -> bool:
    """Optional Streamlit Cloud password gate using st.secrets['app_password'].""" 

    expected_password = get_configured_password()
    if not expected_password:
        return True

    if st.session_state.get("authenticated"):
        return True

    st.title(APP_TITLE)
    st.caption("Enter the client access password to continue.")
    password = st.text_input("Password", type="password")
    if st.button("Unlock", type="primary"):
        if password == expected_password:
            st.session_state["authenticated"] = True
            st.rerun()
        st.error("Incorrect password.")
    return False


def get_configured_password() -> str | None:
    """Return the configured password, or None when secrets are not configured."""

    try:
        return st.secrets.get("app_password")
    except (FileNotFoundError, StreamlitSecretNotFoundError):
        return None


def main() -> None:
    if not check_password():
        return

    initialise_session_state()
    render_brand_header()

    st.title(APP_TITLE)
    st.write(
        "Upload one requisition export and its matching issue export, then validate "
        "that the issue is safe to move forward for picking."
    )

    with st.expander("Validation rules", expanded=False):
        st.markdown(
            """
- One requisition file is checked against one issue file.
- Row counts must match across the two sheets.
- SKU quantities are compared by total quantity across the whole document.
- Issue rows with the same SKU and same issue quantity are flagged as potential customer input errors.
- Requisition is treated as the client request and source of truth for investigation.
            """
        )

    st.subheader("1. Upload Data")
    col1, col2 = st.columns(2)
    with col1:
        render_document_upload(
            document_type="requisition",
            title="REQUISITION EXPORT",
            accent_colour="#0B5CAB",
        )

    with col2:
        render_document_upload(
            document_type="issue",
            title="ISSUE EXPORT",
            accent_colour="#B54708",
        )

    st.subheader("2. Validate Inputs")
    ready = validate_upload_form()

    if not ready:
        st.info("Upload both Excel files to run the checks.")
        return

    if st.button("Run cross-check", type="primary", width="stretch"):
        st.session_state["run_result"] = process_files()

    if "run_result" not in st.session_state:
        return

    result = st.session_state["run_result"]
    render_result(result)


def initialise_session_state() -> None:
    st.session_state.setdefault("document_upload_nonce", {"requisition": 0, "issue": 0})


def render_document_upload(document_type: str, title: str, accent_colour: str) -> None:
    staged_key = f"staged_{document_type}"
    staged = st.session_state.get(staged_key)
    display_name = title.replace(" EXPORT", "").title()

    st.markdown(
        f"""
        <div style="font-weight: 800; color: {accent_colour}; font-size: 1.05rem; letter-spacing: 0.04em;">
            {title}
        </div>
        """,
        unsafe_allow_html=True,
    )

    if staged:
        st.markdown(f":green[**{title} UPLOADED**]")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Source File": staged["filename"],
                    }
                ]
            ),
            width="stretch",
            hide_index=True,
        )
        if st.button(f"Replace {display_name.lower()} file", key=f"replace_{document_type}", width="stretch"):
            st.session_state.pop(staged_key, None)
            st.session_state.pop("run_result", None)
            st.session_state["document_upload_nonce"][document_type] += 1
            st.rerun()
        return

    uploaded_file = st.file_uploader(
        f"Upload {display_name.lower()} file",
        type=["xlsx", "xls"],
        accept_multiple_files=False,
        key=f"{document_type}_upload_{st.session_state['document_upload_nonce'][document_type]}",
    )

    if uploaded_file is None:
        return

    st.session_state[staged_key] = {
        "filename": uploaded_file.name,
        "bytes": uploaded_file.getvalue(),
    }
    st.session_state.pop("run_result", None)
    st.session_state["document_upload_nonce"][document_type] += 1
    st.rerun()


def validate_upload_form() -> bool:
    requisition = st.session_state.get("staged_requisition")
    issue = st.session_state.get("staged_issue")
    checks = [
        ("Requisition file uploaded", requisition is not None),
        ("Issue file uploaded", issue is not None),
    ]

    checklist = pd.DataFrame(
        [
            {"Check": label, "Status": "Ready" if passed else "Required"}
            for label, passed in checks
        ]
    )
    for _, row in checklist.iterrows():
        if row["Status"] == "Ready":
            st.markdown(f"- :green[**{row['Check']}**: Ready]")
        else:
            st.markdown(f"- :red[**{row['Check']}**: Required]")
    return all(passed for _, passed in checks)


def process_files() -> CheckResult:
    requisition = st.session_state["staged_requisition"]
    issue = st.session_state["staged_issue"]

    requisition_buffer = BytesIO(requisition["bytes"])
    requisition_buffer.name = requisition["filename"]
    issue_buffer = BytesIO(issue["bytes"])
    issue_buffer.name = issue["filename"]

    return run_cross_check(
        requisition_file=requisition_buffer,
        requisition_filename=requisition["filename"],
        issue_file=issue_buffer,
        issue_filename=issue["filename"],
    )


def render_result(result: CheckResult) -> None:
    st.subheader("3. Process According to Business Rules")

    if result.is_valid:
        st.success("Validated: the requisition and issue documents are aligned.")
    else:
        st.error("Flagged: adjustments or investigation are required before moving forward.")

    col1, col2, col3 = st.columns(3)
    col1.metric("Requisition rows", result.summary["requisition_rows"])
    col2.metric("Issue rows", result.summary["issue_rows"])
    col3.metric("Flags", len(result.flags))

    st.subheader("4. Preview and Summary")
    render_checklist(result)

    with st.expander("SKU quantity comparison", expanded=not result.sku_mismatches.empty):
        st.dataframe(
            style_sku_summary(result.sku_summary),
            width="stretch",
            hide_index=True,
        )

    with st.expander("Duplicate issue SKU/quantity rows", expanded=not result.duplicate_issue_rows.empty):
        st.dataframe(
            style_duplicate_issue_rows(result.duplicate_issue_rows),
            width="stretch",
            hide_index=True,
        )

    with st.expander("Issue rows with non-numeric locations", expanded=not result.non_numeric_locations.empty):
        st.dataframe(
            style_location_rows(result.non_numeric_locations),
            width="stretch",
            hide_index=True,
        )

    if result.flags:
        st.subheader("Flag Details")
        for flag in result.flags:
            st.markdown(f"- :red[{flag}]")

    if result.next_steps:
        st.subheader("Next Steps")
        for step in result.next_steps:
            st.markdown(f"- :red[{step}]")
    elif result.is_valid:
        st.markdown(":green[No system adjustments are required before moving forward.]")


def render_checklist(result: CheckResult) -> None:
    for item in result.checks:
        if item.status == "Validated":
            st.markdown(f"- :green[**{item.title}:** {item.message}]")
        else:
            st.markdown(f"- :red[**{item.title}:** {item.message}]")


def style_sku_summary(df: pd.DataFrame):
    return df.style.apply(highlight_flagged_sku_rows, axis=1)


def highlight_flagged_sku_rows(row: pd.Series) -> list[str]:
    if row.get("Status") == "Flagged":
        return [
            "background-color: #FDE7E9; color: #8A0013; font-weight: 700;"
            for _ in row
        ]
    return ["" for _ in row]


def style_duplicate_issue_rows(df: pd.DataFrame):
    return df.style.apply(highlight_all_rows, axis=1)


def highlight_all_rows(row: pd.Series) -> list[str]:
    return [
        "background-color: #FFF4E5; color: #8A4B00; font-weight: 700;"
        for _ in row
    ]


def style_location_rows(df: pd.DataFrame):
    return df.style.apply(highlight_location_rows, axis=1)


def highlight_location_rows(row: pd.Series) -> list[str]:
    return [
        "background-color: #FDE7E9; color: #8A0013; font-weight: 700;"
        for _ in row
    ]


def render_brand_header() -> None:
    caf_logo = ASSET_DIR / "01_CAF Logo.png"
    kaizen_logo = ASSET_DIR / "02_Kaizen Institute Logo.png"
    if not caf_logo.exists() and not kaizen_logo.exists():
        return

    st.markdown(
        """
        <style>
        div[data-testid="stHorizontalBlock"]:has(.brand-spacer) {
            align-items: center;
            margin-bottom: 18px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    caf_col, kaizen_col, spacer = st.columns([0.12, 0.22, 0.66], gap="small")
    if caf_logo.exists():
        caf_col.image(str(caf_logo), width=100)
    if kaizen_logo.exists():
        kaizen_col.image(str(kaizen_logo), width=177)
    spacer.markdown('<span class="brand-spacer"></span>', unsafe_allow_html=True)


if __name__ == "__main__":
    main()
