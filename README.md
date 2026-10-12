# Messy Transactions Cleaner (Web UI)

A small local web app that lets a user drag and drop a CSV, clean and validate transaction rows, and download `cleaned.csv` and `report.json`. It uses only the Python standard library.

## Run

Requires Python 3.9+.

**Local Web App:**
```bash
python3 server.py
```
Open http://127.0.0.1:8000 in your browser. Drop a CSV file or choose one, then click **Clean CSV**.

**Command Line (CLI):**
```bash
python3 server.py <path_to_input.csv>
```

Outputs are saved to `outputs/cleaned.csv` and `outputs/report.json`.

## Required CSV columns

`transaction_id, customer_name, amount, transaction_date, status, order_id`

## Assumptions and policies

- Original names, amounts, dates, and statuses are retained in the output.
- Customer names are normalized by collapsing whitespace and title-casing; this does not attempt fuzzy identity matching.
- Output dates use `YYYY-MM-DD`.
- Accepted date formats are `YYYY-MM-DD`, `YYYY/MM/DD`, `DD-MM-YYYY`, and `DD/MM/YYYY`. Slash dates are interpreted as day/month/year (`DD/MM/YYYY`).
- A value like `2026-15-08` is accepted as `YYYY-DD-MM` only when the middle component is greater than 12 (so it cannot be a month) and the final component is a valid month. It becomes `2026-08-15`. Other impossible dates are rejected.
- Amounts support common currency symbols and US-style thousands separators, e.g. `₹1,470.00`. European decimal-comma input such as `1.470,50` is not supported by this version.
- Missing or unparsable amounts are invalid. Zero amounts are flagged. Negative amounts are flagged unless status is `refund`.
- Only `paid`, `pending`, `cancelled`, and `refund` are known statuses. Unknown or missing status is flagged.
- For repeated transaction IDs, the first occurrence is retained as the canonical row; later occurrences are flagged with `duplicate_transaction_id` and invalidated. This avoids invalidating an earlier record solely because a later row reused its ID.
- Likely duplicates are identified using normalized customer name, normalized date, amount, order ID, and normalized status. Including status prevents a `refund` record from being grouped with a `paid` record.
- Likely duplicate candidates are flagged for review but not automatically invalidated. Revenue includes the first record in a likely-duplicate group once and excludes later records in that group.
- Revenue sums valid `paid` transactions only, excluding later repeated-ID rows and later likely-duplicate rows. The exact policy is included in `report.json`.
- Invalid rows are retained in `cleaned.csv`; `input_rows = valid_rows + invalid_rows`.

## Edge cases tested

1. An empty amount or `N/A` yields a blank `amount_clean` and a missing-amount issue.
2. An impossible date such as `2026-15-40` is rejected.
3. `2026-15-08` is interpreted as `YYYY-DD-MM` and normalized to `2026-08-15`.
4. A repeated transaction ID keeps its first occurrence and flags later occurrences.
5. Rows with the same customer/date/amount/order but different statuses (such as refund vs paid) are not grouped as likely duplicates.
6. Later records in a same-status likely-duplicate group are excluded from revenue to prevent double counting.

## Files

- `server.py`: local web server, CLI runner, cleaning pipeline, duplicate detection, and report generation.
- `outputs/`: directory containing `cleaned.csv` and `report.json`.
