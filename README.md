# CAF Requisition and Issue Cross-Check Tool

Streamlit application for validating that one CAF requisition export and one matching issue export are aligned before the issue document is released for picking.

The app is designed for GitHub deployment to Streamlit Cloud and follows the same lightweight structure as the CAF Pick List Consolidation Tool:

- `app.py` is the Streamlit entry point.
- `requisition_issue_checker.py` contains the Excel parsing and business-rule checks.
- `assets/` stores the CAF and Kaizen Institute logos.
- `.streamlit/secrets.toml.example` shows optional local password configuration.
- Uploaded files are processed in memory and are not stored permanently.

## Workflow

1. Upload one requisition Excel export.
2. Upload the matching issue Excel export.
3. Run the cross-check.
4. Review the colour-coded checklist and any flagged SKU quantity differences, duplicate issue rows, or non-numeric issue locations.

## Business Rules

The tool checks one requisition document against one issue document.

Required requisition columns:

- `Part`
- `Quantity`

Required issue columns:

- `Part`
- `Issue Qty.`
- `From Bin`

Validation rules:

- The number of data rows must match across both sheets.
- SKU quantities are compared by total quantity across the full document.
- Issue rows with the same SKU and same issue quantity are flagged as potential customer input errors.
- Issue rows with missing or non-numeric `From Bin` locations, such as `UNDER QUERY` or `OH GOODS OUT`, are flagged for verification/removal.
- Requisition is treated as the client request and source of truth for investigation.
- Requisition `Status` is ignored.

## Output

The app displays:

- A green/red validation checklist.
- Summary metrics for requisition row count, issue row count, and total flags.
- A SKU quantity comparison table.
- A duplicate issue SKU/quantity row table.
- An issue location exception table.
- Plain-English next steps for any flagged checks.

If every check passes, the app confirms that no system adjustments are required before moving forward.

## Run Locally

```powershell
pip install -r requirements.txt
streamlit run app.py
```

## Streamlit Cloud

Deploy the repository to Streamlit Cloud with `app.py` as the entry point.

Optional password protection can be enabled by adding this secret in Streamlit Cloud:

```toml
app_password = "replace-with-client-password"
```

If `app_password` is not configured, the app runs without a password gate.

For local testing, copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and update the password value. Do not commit the real secrets file.

## Repository Hygiene

The repository should not include generated or sensitive working files. The `.gitignore` excludes:

- Python caches
- virtual environments
- real Streamlit secrets
- Excel files
- PDF files
- CSV exports
- logs
