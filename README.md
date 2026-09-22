# b2b-reorder-prediction

**Practice/demo project for learning** — modeled on the type of work in a real $500 fixed-price Upwork posting: *"Build a reorder-prediction automation for a B2B distributor client (n8n/Python, ERP data)"*.

This is a learning exercise built from mock data. It is **not** client work and does not represent paid experience. It was built to practice exactly the skills that posting asks for: ingesting messy ERP CSV exports, computing reorder intervals, flagging drifted accounts, and producing a scheduled digest report.

## What it does

A B2B distributor's rep doesn't want a dashboard to stare at — they want a daily email that says *"these accounts are overdue to reorder"*. This pipeline does that:

1. **Ingest** an order-history CSV export (no live DB). It auto-detects the ERP column layout — an Epicor-style format (`CustomerID`, `OrderDate`, `ProductClass`, …) or a Sage-style format (`AccountCode`, `TranDate`, `Category`, …) — and normalizes both into one shape.
2. **Analyze**: for each account × product category, compute the **median days between consecutive orders** (the account's "typical reorder interval"). Flag accounts where `days_since_last_order > median_interval × 1.25` — i.e. 25% past their own historical pattern. Accounts with only one order on record are reported as *insufficient history*, not guessed at.
3. **Report**: a scheduled digest in plain text (email body) plus an `alerts.csv` attachment with the drifted accounts and their drift ratios, sorted worst-first.
4. **Quarantine bad data**: malformed rows never touch the math. They're collected into `errors.csv` with a human-readable reason per row (bad date, missing quantity, missing account, non-positive quantity, unrecognized format).

## How to run

```bash
# demo: analyze the bundled sample exports, write output/ to ./output
python3 reorder.py

# your own export (either supported format)
python3 reorder.py path/to/orders.csv

# run the tests
python3 -m pytest tests/ -q
```

Environment variables (see `.env.example`):
- `REORDER_INPUT` — default input CSV
- `REORDER_OUTPUT` — output directory for `digest.txt`, `alerts.csv`, `errors.csv`

Dependencies: **stdlib only** (`csv`, `statistics`, `datetime`, …). `pytest` is needed only to run the tests.

## Sample output

```
REORDER WATCH — daily digest for 2026-09-23
============================================================
Accounts tracked : 4
Drifted accounts : 2
Thin history      : 0
Bad rows quarantined: 5

OVERDUE TO REORDER (interval drifted past 125%):
  - Harbor Wholesale (ACC-104) — Fasteners: typical interval 14 days, last order 82 days ago (x5.86 of typical)
  - Northgate Hardware (ACC-101) — Fasteners: typical interval 14 days, last order 35 days ago (x2.5 of typical)
...
```

## How this answers each of the posting's asks

| Client ask | How it's covered |
|---|---|
| Ingest an order-history CSV export (possibly varying ERP formats, no live DB) | `load_orders()` auto-detects 2 column variants via `FORMAT_VARIANTS`; new ERPs = one new mapping dict. Sample files in both formats included. |
| Compute each account's typical reorder interval per product/category, flag drifted accounts | `compute_intervals()` → median gap per (account, category); `flag_drift()` flags `days_since > interval × 1.25`. |
| Actionable output: a scheduled email digest report (text + CSV attachment) | `render_digest()` (email body) + `alerts.csv` (attachment); `main()` writes both plus `errors.csv`. Scheduling is one cron/n8n trigger calling this script. |
| Clear docs | This README, plus per-function docstrings explaining *why* each decision was made. |
| Written explanation of error/bad-data handling | See below. |

## Error / bad-data handling

- **Format detection failure**: if the header matches no known variant, zero rows are loaded and row 0 is quarantined with "unrecognized format" — the pipeline never misreads columns.
- **Bad rows quarantined, never dropped silently**: each rejected row lands in `errors.csv` with its 1-based row number and a specific reason.
- **Bad dates**: strings matching no known format or impossible calendar dates (`2026-13-01`, `31/02/2026`) are rejected. UTF-8 BOM is handled (`utf-8-sig`).
- **Quantities**: must be present, numeric, and positive; `-5` and empty values are rejected.
- **Missing identity fields**: rows without an account id or category are rejected (interval math needs both).
- **Thin history**: an account with a single order gets `median_interval = None` and is reported as `insufficient_history` rather than flagged as drifted — no guesses from no data.
- **Boundary behavior**: the drift check is strictly `>` (an account exactly at 125% is *not* flagged), which is covered by a test.

## Learning roadmap

- [x] Dual-format CSV ingestion with auto-detection
- [x] Median reorder-interval computation per account × category
- [x] Drift flagging with a tunable `DRIFT_FACTOR`
- [x] Quarantine + audit trail for bad rows
- [x] Text digest + CSV alerts output, demo end-to-end
- [ ] Add a third ERP format variant (e.g. SAP-style columns)
- [ ] Replace the demo "today" with the real clock + wire to a cron / n8n Schedule Trigger
- [ ] Send the digest via SMTP instead of writing files
- [ ] Category-level tuning of `DRIFT_FACTOR` (fasteners vs. adhesives behave differently)
