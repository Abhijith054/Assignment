#!/usr/bin/env python3
"""Transaction cleaner: local web app + command line, standard library only.

Web app :  python server.py
CLI     :  python server.py input.csv [--cleaned outputs/cleaned.csv] [--report outputs/report.json]
"""

from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
import argparse
import csv
import io
import json
import re
import sys
import unicodedata

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "outputs"

EXPECTED_COLUMNS = [
    "transaction_id", "customer_name", "amount",
    "transaction_date", "status", "order_id"
]
STATUS_MAP = {
    "paid": "paid", "complete": "paid", "completed": "paid", "success": "paid",
    "successful": "paid", "settled": "paid",
    "pending": "pending", "processing": "pending", "awaiting": "pending",
    "in progress": "pending", "open": "pending",
    "cancelled": "cancelled", "canceled": "cancelled", "void": "cancelled",
    "voided": "cancelled", "failed": "cancelled",
    "refund": "refund", "refunded": "refund", "returned": "refund",
    "chargeback": "refund",
}
NULL_AMOUNTS = {"", "n/a", "na", "null", "none", "nan", "-", "--", "nil", "?"}
MAX_AMOUNT = Decimal("10000000")   # amounts above this are treated as data-entry errors
YEAR_RANGE = (2000, 2100)
TITLES = {"mr", "mrs", "ms", "miss", "dr", "prof", "shri", "smt"}
CENT = Decimal("0.01")
SEP = " | "

OUTPUT_FIELDS = [
    "source_row", "transaction_id", "customer_name_original", "customer_name_clean",
    "customer_key", "amount_original", "amount_clean", "date_original", "date_clean",
    "status_original", "status_clean", "order_id", "is_valid", "issues", "notes",
    "is_duplicate_id", "is_likely_duplicate", "duplicate_of",
]


# ----------------------------------------------------------------- names
def _title_token(tok):
    if tok != tok.upper() and tok != tok.lower():     # keep McDonald, iPhone...
        return tok
    return "-".join("'".join(p.capitalize() for p in a.split("'")) for a in tok.split("-"))


def clean_name(value):
    """Return (original, display_name, match_key)."""
    original = value if value is not None else ""
    s = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", original)).strip()
    if s.count(",") == 1:                             # "Rao, Anita" -> "Anita Rao"
        last, first = [p.strip() for p in s.split(",")]
        if last and first:
            s = f"{first} {last}"
    tokens = [t for t in s.replace(".", " ").replace(",", " ").split() if t]
    tokens = [t for t in tokens if t.lower() not in TITLES]
    display = " ".join(_title_token(t) for t in tokens)
    folded = unicodedata.normalize("NFKD", display)
    key = "".join(c for c in folded if not unicodedata.combining(c)).casefold()
    return original, display, key


# ----------------------------------------------------------------- dates
def _valid_date(y, m, d):
    if not (YEAR_RANGE[0] <= y <= YEAR_RANGE[1]):
        return None
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _bad_date(raw, year):
    if not (YEAR_RANGE[0] <= year <= YEAR_RANGE[1]):
        return f"invalid_date: year {year} in '{raw}' is outside {YEAR_RANGE[0]}-{YEAR_RANGE[1]}"
    return f"invalid_date: '{raw}' is not a real calendar date"


def parse_date(value):
    """Return (iso | None, note | None, error | None)."""
    raw = (value or "").strip()
    if not raw:
        return None, None, "missing_date: date is empty"

    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", raw)
    if m:
        y, a, b = map(int, m.groups())
        d = _valid_date(y, a, b)                              # YYYY-MM-DD
        if d:
            return d.isoformat(), None, None
        d = _valid_date(y, b, a)                              # YYYY-DD-MM (month > 12)
        if d:
            return d.isoformat(), f"date '{raw}' read as YYYY-DD-MM (month {a} is impossible)", None
        return None, None, _bad_date(raw, y)

    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", raw)
    if m:
        a, b, y = map(int, m.groups())
        dmy, mdy = _valid_date(y, b, a), _valid_date(y, a, b)
        if dmy and mdy and dmy != mdy:                        # both readings possible
            return dmy.isoformat(), f"date '{raw}' is ambiguous; assumed DD/MM/YYYY", None
        if dmy:
            return dmy.isoformat(), None, None
        if mdy:                                               # day > 12, so only MM/DD fits
            return mdy.isoformat(), f"date '{raw}' read as MM/DD/YYYY (day > 12)", None
        return None, None, _bad_date(raw, y)

    return None, None, f"invalid_date: unrecognised date format '{raw}'"


# --------------------------------------------------------------- amounts
def parse_amount(value):
    """Return (Decimal | None, note | None, error | None). Sign is preserved."""
    raw = (value or "").strip()
    if raw.lower() in NULL_AMOUNTS:
        return None, None, "missing_amount: amount is empty or a null marker"

    s, neg = raw, False
    if s.startswith("(") and s.endswith(")"):             # accounting negative
        neg, s = True, s[1:-1]
    s = re.sub(r"(?i)\b(inr|usd|eur|gbp|rs)\b\.?|[₹$€£]", "", s)
    s = re.sub(r"[\s\u00a0']", "", s)
    if s.endswith("-"):
        neg, s = not neg, s[:-1]
    if s[:1] in ("+", "-") and s:
        neg ^= s[0] == "-"
        s = s[1:]
    if not s or not re.fullmatch(r"[\d.,]+", s) or not re.search(r"\d", s):
        return None, None, f"invalid_amount: '{raw}' is not numeric"

    note = None
    has_c, has_d = "," in s, "." in s
    bad = (None, None, f"invalid_amount: malformed number '{raw}'")
    if has_c and has_d:                                   # last separator is the decimal
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        thou = "." if dec == "," else ","
        head, _, tail = s.rpartition(dec)
        if dec in head or not re.fullmatch(r"\d+", tail):
            return bad
        s = head.replace(thou, "") + "." + tail
    elif has_c:
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2):                    # 12,5
            s = parts[0] + "." + parts[1]
        elif (1 <= len(parts[0]) <= 3 and len(parts[-1]) == 3
              and all(len(p) in (2, 3) for p in parts[1:-1])):             # 1,470 / 1,47,000
            s = "".join(parts)
            if len(parts) == 2:
                note = f"amount '{raw}' read with comma as thousands separator"
        else:
            return bad
    elif has_d:
        parts = s.split(".")
        if len(parts) > 2:                                                 # 1.470.000
            if 1 <= len(parts[0]) <= 3 and all(len(p) == 3 for p in parts[1:]):
                s = "".join(parts)
            else:
                return bad
        elif len(parts[1]) == 3:
            note = f"amount '{raw}' read with '.' as decimal point"
    try:
        val = Decimal(s).quantize(CENT, rounding=ROUND_HALF_UP)
    except Exception:
        return None, None, f"invalid_amount: '{raw}' is not numeric"
    return (-val if neg else val), note, None


# -------------------------------------------------------------- statuses
def normalize_status(value):
    raw = re.sub(r"\s+", " ", (value or "").strip().lower())
    if not raw:
        return "", "missing_status: status is empty"
    if raw in STATUS_MAP:
        return STATUS_MAP[raw], None
    return "unknown", f"unknown_status: '{(value or '').strip()}' is not a recognised status"


# ------------------------------------------------------------ processing
def build_record(index, input_row):
    row = {str(k).strip().lower(): (v if v is not None else "")
           for k, v in input_row.items() if k is not None}
    issues, notes = [], []
    original_name, cleaned_name, key = clean_name(row.get("customer_name", ""))

    transaction_id = row.get("transaction_id", "").strip()
    if not transaction_id:
        issues.append("missing_transaction_id: transaction_id is empty")
    if not cleaned_name:
        issues.append("missing_customer_name: customer name is empty")

    cleaned_date, note, err = parse_date(row.get("transaction_date", ""))
    if note:
        notes.append(note)
    if err:
        issues.append(err)

    status, err = normalize_status(row.get("status", ""))
    if err:
        issues.append(err)

    amount, note, err = parse_amount(row.get("amount", ""))
    if note:
        notes.append(note)
    if err:
        issues.append(err)
    if amount is not None:
        if amount == 0:
            issues.append("zero_amount: amount is 0")
        elif amount < 0 and status != "refund":
            issues.append(f"negative_amount_not_refund: {amount} on a non-refund row")
        elif amount < 0:
            amount = -amount
            notes.append("negative refund amount stored as positive")
        if abs(amount) > MAX_AMOUNT:
            issues.append(f"amount_out_of_range: {amount} exceeds {MAX_AMOUNT}")

    order_id = row.get("order_id", "").strip()
    if not order_id:
        notes.append("order_id is empty")

    return {
        "source_row": index,
        "transaction_id": transaction_id,
        "customer_name_original": original_name,
        "customer_name_clean": cleaned_name,
        "customer_key": key,
        "amount_original": row.get("amount", ""),
        "amount": amount,
        "amount_clean": f"{amount:.2f}" if amount is not None else "",
        "date_original": row.get("transaction_date", ""),
        "date_clean": cleaned_date or "",
        "status_original": row.get("status", ""),
        "status_clean": status,
        "order_id": order_id,
        "issues_list": issues,
        "notes_list": notes,
        "is_duplicate_id": False,
        "is_likely_duplicate": False,
        "duplicate_of": "",
    }


def flag_duplicate_ids(rows):
    groups = defaultdict(list)
    for r in rows:
        if r["transaction_id"]:
            groups[r["transaction_id"].upper()].append(r)
    for members in groups.values():
        if len(members) < 2:
            continue
        keeper = next((r for r in members if not r["issues_list"]), members[0])
        others = ", ".join(str(r["source_row"]) for r in members if r is not keeper)
        for r in members:
            r["is_duplicate_id"] = True
            if r is keeper:
                r["notes_list"].append(f"transaction_id reused in source_row(s) {others}; this first clean row kept")
                continue
            same = all(r[f] == keeper[f] for f in ("customer_key", "amount_clean", "date_clean", "status_clean"))
            r["duplicate_of"] = keeper["transaction_id"]
            r["issues_list"].append(
                f"duplicate_transaction_id: ID already used by source_row {keeper['source_row']} "
                f"({'exact repeat' if same else 'different details'})")


def flag_likely_duplicates(rows):
    """Same customer + amount + date + order under different IDs. A refund is never a duplicate of a payment."""
    groups = defaultdict(list)
    for r in rows:
        if any(i.startswith("duplicate_transaction_id") for i in r["issues_list"]):
            continue
        if r["customer_key"] and r["amount"] is not None and r["date_clean"]:
            k = (r["customer_key"], r["amount_clean"], r["date_clean"],
                 r["order_id"].upper(), r["status_clean"] == "refund")
            groups[k].append(r)
    for members in groups.values():
        if len(members) < 2:
            continue
        clean = [r for r in members if not r["issues_list"]] or members
        keeper = sorted(clean, key=lambda r: (r["status_clean"] != "paid", r["source_row"]))[0]
        for r in members:
            r["is_likely_duplicate"] = True
            if r is keeper:
                r["notes_list"].append("likely duplicates in other rows; this row kept")
            else:
                r["duplicate_of"] = r["duplicate_of"] or keeper["transaction_id"]
                r["issues_list"].append(
                    "likely_duplicate_transaction: same customer, amount, date and order as "
                    f"{keeper['transaction_id']} (source_row {keeper['source_row']})")


def build_report(cleaned_csv, expected_rows):
    """Computed by re-reading the cleaned CSV, so report and file always agree."""
    rows = list(csv.DictReader(io.StringIO(cleaned_csv)))
    valid = [r for r in rows if r["is_valid"] == "True"]
    invalid = [r for r in rows if r["is_valid"] == "False"]
    paid_rows = [r for r in valid if r["status_clean"] == "paid"]
    paid = sum((Decimal(r["amount_clean"]) for r in paid_rows), Decimal("0.00"))
    refunds = sum((Decimal(r["amount_clean"]) for r in valid if r["status_clean"] == "refund"), Decimal("0.00"))

    def has(r, code):
        return any(i.startswith(code) for i in r["issues"].split(SEP) if i)

    id_rows = [r for r in rows if r["is_duplicate_id"] == "True"]
    ld_rows = [r for r in rows if r["is_likely_duplicate"] == "True"]
    ld_groups = {(r["customer_key"], r["amount_clean"], r["date_clean"], r["order_id"].upper(),
                  r["status_clean"] == "refund") for r in ld_rows}
    by_issue = Counter(i.split(":")[0] for r in invalid for i in r["issues"].split(SEP) if i)

    report = {
        "input_rows": expected_rows,
        "valid_rows": len(valid),
        "invalid_rows": len(invalid),
        "duplicate_id_groups": len({r["transaction_id"].upper() for r in id_rows}),
        "duplicate_id_rows_involved": len(id_rows),
        "duplicate_id_rows_excluded": sum(1 for r in rows if has(r, "duplicate_transaction_id")),
        "likely_duplicate_groups": len(ld_groups),
        "likely_duplicate_rows_involved": len(ld_rows),
        "likely_duplicate_rows_excluded": sum(1 for r in rows if has(r, "likely_duplicate_transaction")),
        "revenue_policy": (
            "total_revenue = sum of amount_clean over VALID rows with status 'paid'. Duplicates are "
            "invalid: the first clean row of a repeated transaction ID is kept and later repeats are "
            "invalid; among likely duplicates (same customer, amount, date, order; refunds kept "
            "separate) one row is kept, preferring 'paid'. Refunds are valid but reported separately "
            "and subtracted in net_revenue."),
        "revenue_rows": len(paid_rows),
        "total_revenue": format(paid, ".2f"),
        "refunds_total": format(refunds, ".2f"),
        "net_revenue": format(paid - refunds, ".2f"),
        "valid_rows_by_status": dict(Counter(r["status_clean"] for r in valid)),
        "invalid_rows_by_issue": dict(by_issue),
        "reconciliation": {
            "valid_plus_invalid_equals_input": len(valid) + len(invalid) == expected_rows,
            "cleaned_csv_rows_equal_input": len(rows) == expected_rows,
            "revenue_rows_equal_valid_paid_rows": len(paid_rows) == sum(
                1 for r in valid if r["status_clean"] == "paid"),
        },
    }
    assert all(report["reconciliation"].values()), report["reconciliation"]
    return report


def process_csv(csv_text):
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        raise ValueError("The uploaded CSV is empty or has no header row.")
    headers = [(h or "").strip().lower() for h in reader.fieldnames]
    missing = [c for c in EXPECTED_COLUMNS if c not in headers]
    if missing:
        raise ValueError("Missing required column(s): " + ", ".join(missing))

    rows = [build_record(i, r) for i, r in enumerate(reader, start=2)]
    flag_duplicate_ids(rows)
    flag_likely_duplicates(rows)

    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=OUTPUT_FIELDS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for r in rows:
        out = dict(r)
        out["is_valid"] = "True" if not r["issues_list"] else "False"
        out["issues"] = SEP.join(r["issues_list"])
        out["notes"] = SEP.join(r["notes_list"])
        out["is_duplicate_id"] = str(r["is_duplicate_id"])
        out["is_likely_duplicate"] = str(r["is_likely_duplicate"])
        writer.writerow(out)
    cleaned_csv = buf.getvalue()
    return cleaned_csv, build_report(cleaned_csv, len(rows))


def save_outputs(cleaned_csv, report, cleaned_path, report_path):
    cleaned_file = Path(cleaned_path)
    report_file = Path(report_path)
    cleaned_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.parent.mkdir(parents=True, exist_ok=True)
    cleaned_file.write_text(cleaned_csv, encoding="utf-8", newline="")
    report_file.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


# ------------------------------------------------------------------- web
HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Transaction Cleaner</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0b1020;
      --panel: #121a2d;
      --line: #293651;
      --text: #edf2ff;
      --muted: #9eabc5;
      --accent: #8b9cff;
      --good: #6ee7b7;
    }
    * {
      box-sizing: border-box;
    }
    body {
      margin: 0;
      background: radial-gradient(ellipse at top, #182443 0, #0b1020 55%);
      color: var(--text);
      font: 16px/1.5 system-ui, -apple-system, Segoe UI, sans-serif;
      min-height: 100vh;
    }
    main {
      max-width: 960px;
      margin: 0 auto;
      padding: 52px 20px 72px;
    }
    h1 {
      font-size: clamp(2.2rem, 6vw, 4rem);
      line-height: 1.05;
      letter-spacing: -0.05em;
      margin: 12px 0 16px;
    }
    .intro {
      color: var(--muted);
      max-width: 650px;
      font-size: 1.05rem;
    }
    .panel {
      margin-top: 32px;
      background: rgba(18, 26, 45, 0.9);
      border: 1px solid var(--line);
      border-radius: 24px;
      padding: 24px;
      box-shadow: 0 20px 60px #0003;
    }
    .drop {
      display: flex;
      min-height: 230px;
      border: 1.5px dashed #52658d;
      border-radius: 18px;
      align-items: center;
      justify-content: center;
      text-align: center;
      padding: 24px;
      cursor: pointer;
      transition: 0.2s;
      background: #0e1629;
    }
    .drop.drag {
      border-color: var(--accent);
      background: #172342;
    }
    .drop strong {
      display: block;
      font-size: 1.15rem;
    }
    .drop p {
      color: var(--muted);
      margin: 8px 0 16px;
    }
    .fileicon {
      font-size: 2.2rem;
      margin-bottom: 8px;
    }
    button, .download {
      border: 0;
      background: var(--accent);
      color: #0b1020;
      font-weight: 750;
      border-radius: 11px;
      padding: 12px 18px;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 0.95rem;
    }
    button:disabled {
      opacity: 0.45;
      cursor: not-allowed;
    }
    input[type=file] {
      display: none;
    }
    .filename {
      color: var(--good);
      margin-top: 14px;
      overflow-wrap: anywhere;
    }
    .note {
      color: var(--muted);
      font-size: 0.86rem;
      margin-top: 16px;
    }
    .error {
      color: #ff9eaa;
      margin-top: 14px;
    }
    .hidden {
      display: none !important;
    }
    .stats {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-top: 24px;
    }
    .stat {
      background: #0d1527;
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 16px;
    }
    .stat span {
      display: block;
      color: var(--muted);
      font-size: 0.82rem;
    }
    .stat strong {
      display: block;
      font-size: 1.6rem;
      margin-top: 5px;
      overflow-wrap: anywhere;
    }
    .results {
      margin-top: 24px;
    }
    .results h2 {
      font-size: 1.25rem;
    }
    .downloads {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }
    .download.secondary {
      background: #202c45;
      color: var(--text);
      border: 1px solid var(--line);
    }
    .summary {
      color: var(--muted);
      font-size: 0.9rem;
    }
    footer {
      margin-top: 24px;
      color: var(--muted);
      font-size: 0.82rem;
    }
    @media(max-width: 650px) {
      main {
        padding: 32px 14px;
      }
      .panel {
        padding: 16px;
        border-radius: 18px;
      }
      .stats {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
      .stat strong {
        font-size: 1.35rem;
      }
    }
  </style>
</head>
<body>
<main>
  <h1>Clean messy transactions.</h1>
  <p class="intro">Drop a CSV file to normalize customer names, dates, amounts and statuses, flag invalid rows and duplicate transactions, and generate a reconciliation report.</p>
  <section class="panel">
    <div id="drop" class="drop" role="button" tabindex="0" aria-label="Choose or drop a CSV file">
      <div>
        <div class="fileicon">&#8679;</div>
        <strong>Drop your CSV file here</strong>
        <p>or browse files on your device</p>
        <button id="browse" type="button">Choose CSV file</button>
        <div id="filename" class="filename"></div>
      </div>
    </div>
    <input id="file" type="file" accept=".csv,text/csv">
    <p class="note">Expected columns: transaction_id, customer_name, amount, transaction_date, status, order_id. Files are processed by this local app.</p>
    <div style="margin-top: 18px">
      <button id="process" type="button" disabled>Clean CSV</button>
    </div>
    <div id="error" class="error" role="alert"></div>
    <div id="results" class="results hidden">
      <h2>Cleaning complete</h2>
      <p id="summary" class="summary"></p>
      <div id="stats" class="stats"></div>
      <div class="downloads" style="margin-top: 20px">
        <a id="csvDownload" class="download" download="cleaned.csv">Download cleaned.csv</a>
        <a id="jsonDownload" class="download secondary" download="report.json">Download report.json</a>
      </div>
    </div>
  </section>
  <footer>Assumptions: dates such as 03/09/2026 are read as DD/MM/YYYY (noted per row); impossible dates are rejected; unknown statuses, zero, negative non-refund and implausibly large amounts are invalid; repeated transaction IDs and likely duplicates are invalid so revenue counts each sale once.</footer>
</main>
<script>
  const fileInput = document.getElementById('file');
  const drop = document.getElementById('drop');
  const browse = document.getElementById('browse');
  const processBtn = document.getElementById('process');
  const filename = document.getElementById('filename');
  const error = document.getElementById('error');
  const results = document.getElementById('results');
  let selectedFile = null;

  function choose(file) {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith('.csv')) {
      error.textContent = 'Please choose a .csv file.';
      return;
    }
    selectedFile = file;
    filename.textContent = file.name;
    processBtn.disabled = false;
    error.textContent = '';
    results.classList.add('hidden');
  }

  browse.addEventListener('click', (e) => {
    e.stopPropagation();
    fileInput.click();
  });
  drop.addEventListener('click', (e) => {
    if (e.target !== browse) fileInput.click();
  });
  drop.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      fileInput.click();
    }
  });
  fileInput.addEventListener('change', (e) => choose(e.target.files[0]));

  ['dragenter', 'dragover'].forEach(event => {
    drop.addEventListener(event, (e) => {
      e.preventDefault();
      drop.classList.add('drag');
    });
  });
  ['dragleave', 'drop'].forEach(event => {
    drop.addEventListener(event, (e) => {
      e.preventDefault();
      drop.classList.remove('drag');
    });
  });
  drop.addEventListener('drop', (e) => choose(e.dataTransfer.files[0]));

  function blobUrl(text, type) {
    return URL.createObjectURL(new Blob([text], { type }));
  }

  processBtn.addEventListener('click', async () => {
    if (!selectedFile) return;
    processBtn.disabled = true;
    processBtn.textContent = 'Cleaning…';
    error.textContent = '';

    try {
      const raw = await selectedFile.text();
      const response = await fetch('/clean', {
        method: 'POST',
        headers: { 'Content-Type': 'text/csv; charset=utf-8' },
        body: raw,
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.error || 'Could not process this CSV.');
      }

      document.getElementById('csvDownload').href = blobUrl(data.cleaned_csv, 'text/csv;charset=utf-8');
      document.getElementById('jsonDownload').href = blobUrl(JSON.stringify(data.report, null, 2), 'application/json;charset=utf-8');

      const r = data.report;
      document.getElementById('summary').textContent = 'Your output files are ready. Invalid rows remain in the cleaned file with explanations.';
      const cards = [
        ['Input rows', r.input_rows],
        ['Valid rows', r.valid_rows],
        ['Invalid rows', r.invalid_rows],
        ['Total revenue', r.total_revenue],
      ];
      document.getElementById('stats').innerHTML = cards.map(
        ([label, value]) => `<div class="stat"><span>${label}</span><strong>${value}</strong></div>`
      ).join('');
      results.classList.remove('hidden');
    } catch (e) {
      error.textContent = e.message || 'Something went wrong.';
    } finally {
      processBtn.disabled = !selectedFile;
      processBtn.textContent = 'Clean CSV';
    }
  });
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def send_bytes(self, status, content_type, data):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if urlparse(self.path).path in ("/", "/index.html"):
            self.send_bytes(200, "text/html; charset=utf-8", HTML.encode("utf-8"))
        else:
            self.send_bytes(404, "text/plain; charset=utf-8", b"Not found")

    def do_POST(self):
        if urlparse(self.path).path != "/clean":
            self.send_bytes(404, "application/json; charset=utf-8", b'{"error":"Not found"}')
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 15 * 1024 * 1024:
            payload = json.dumps({"error": "Upload a non-empty CSV smaller than 15 MB."}).encode()
            self.send_bytes(400, "application/json; charset=utf-8", payload)
            return
        raw = self.rfile.read(length)
        try:
            cleaned_csv, report = process_csv(raw.decode("utf-8-sig"))
            save_outputs(cleaned_csv, report, OUTPUT_DIR / "cleaned.csv", OUTPUT_DIR / "report.json")
            payload = json.dumps({"cleaned_csv": cleaned_csv, "report": report}).encode("utf-8")
            self.send_bytes(200, "application/json; charset=utf-8", payload)
        except (UnicodeDecodeError, csv.Error, ValueError) as exc:
            payload = json.dumps({"error": str(exc)}).encode("utf-8")
            self.send_bytes(400, "application/json; charset=utf-8", payload)

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))


def main():
    ap = argparse.ArgumentParser(
        description="Clean a transactions CSV (CLI) or run the local web app (no arguments)."
    )
    ap.add_argument("input", nargs="?", help="input CSV; if given, run in CLI mode")
    ap.add_argument("--cleaned", default=str(OUTPUT_DIR / "cleaned.csv"), help="output cleaned CSV path")
    ap.add_argument("--report", default=str(OUTPUT_DIR / "report.json"), help="output report JSON path")
    args = ap.parse_args()

    if args.input:
        path = Path(args.input)
        if not path.is_file():
            sys.exit(f"Input file not found: {path}")
        try:
            cleaned_csv, report = process_csv(path.read_text(encoding="utf-8-sig"))
        except ValueError as exc:
            sys.exit(str(exc))
        save_outputs(cleaned_csv, report, args.cleaned, args.report)
        print(f"{report['input_rows']} rows -> {report['valid_rows']} valid, "
              f"{report['invalid_rows']} invalid; total revenue {report['total_revenue']}")
        return

    server = ThreadingHTTPServer(("127.0.0.1", 8000), Handler)
    print("Transaction Cleaner is running at http://127.0.0.1:8000")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()