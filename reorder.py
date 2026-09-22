#!/usr/bin/env python3
"""Reorder-prediction pipeline for a B2B distributor (practice/demo project).

What it does:
  1. Loads an order-history CSV export from a distributor's ERP system.
  2. Handles at least two column-format variants (e.g. Epicor-style and
     Sage-style exports) by auto-detecting the format from the header row.
  3. Computes each account's typical reorder interval (median days between
     consecutive orders) per product category.
  4. Flags accounts whose time-since-last-order has drifted past
     (interval * DRIFT_FACTOR) — these are likely "due or overdue to reorder".
  5. Quarantines malformed rows into an errors report with a human-readable
     reason for each rejected row, so bad ERP exports never silently corrupt
     the numbers.
  6. Produces an actionable output: a scheduled email-digest style report
     (plain text) plus an alerts CSV (who is drifted, by how much).

Dependencies: standard library only.

Usage:
    python3 reorder.py                      # runs the demo end to end
    python3 reorder.py <orders.csv>          # analyzes your own CSV export

Environment variables (see .env.example):
    REORDER_INPUT    default input CSV (demo uses the sample files)
    REORDER_OUTPUT   output directory for digest.txt, alerts.csv, errors.csv
"""

import csv
import os
from collections import defaultdict
from datetime import date, datetime
from statistics import median

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# How far past a customer's typical interval we flag them as "drifted".
# > 1.25 means "25% overdue vs. their own history" — a common starting rule
# for this kind of heuristic. Real-world builds would tune this per category.
DRIFT_FACTOR = 1.25

# Date formats we accept when parsing the order date column.
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y")

# Demo "today" so the sample output is deterministic. When run in scheduled
# mode (n8n cron / cron), today should come from the system clock instead.
DEMO_TODAY = date(2026, 9, 23)

# ---------------------------------------------------------------------------
# Format handling
# ---------------------------------------------------------------------------
#
# ERP exports rarely share column names. Instead of hard-coding one layout,
# we describe each known variant as a mapping from a canonical field name
# to the column name used by that ERP. Adding a new ERP = adding one dict.
#
#   account_id  -> the customer / account code
#   account_name-> the customer display name
#   order_date  -> the date the order was placed
#   category    -> product class / category (we track intervals per category)
#   qty         -> order quantity (must be a positive number)

FORMAT_VARIANTS = {
    "format_a_epicor_like": {
        "account_id": "CustomerID",
        "account_name": "CustomerName",
        "order_date": "OrderDate",
        "category": "ProductClass",
        "qty": "Qty",
    },
    "format_b_sage_like": {
        "account_id": "AccountCode",
        "account_name": "AccountName",
        "order_date": "TranDate",
        "category": "Category",
        "qty": "Quantity",
    },
}


def detect_format(header):
    """Return the variant key whose required columns are all present.

    Returns None when the header matches no known variant, so the caller can
    report the file as unsupported instead of misreading its columns.
    """
    columns = set(header or [])
    for variant, mapping in FORMAT_VARIANTS.items():
        if set(mapping.values()).issubset(columns):
            return variant
    return None


def parse_date(raw):
    """Parse a date string with any of the accepted formats.

    Raises ValueError when the string matches no known format or is not a
    real calendar date (e.g. 2026-13-01 or 31/02/2026).
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty order date")
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unparseable order date '{raw}'")


def parse_qty(raw):
    """Parse a quantity; it must be a positive number."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("missing quantity")
    try:
        qty = float(text)
    except ValueError:
        raise ValueError(f"non-numeric quantity '{raw}'")
    if qty <= 0:
        raise ValueError(f"non-positive quantity '{raw}'")
    return qty


def load_orders(path):
    """Load and normalize an order-history CSV.

    Returns (orders, errors):
      orders: list of dicts {account_id, account_name, order_date, category, qty}
      errors: list of dicts {row, reason} for quarantined rows.

    A row is quarantined (not dropped silently) when: a required field is
    missing/empty, the date cannot be parsed, the quantity is not a positive
    number, or the file's format cannot be recognized at all.
    """
    orders, errors = [], []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        variant = detect_format(reader.fieldnames)
        if variant is None:
            errors.append({
                "row": 0,
                "reason": (
                    "unrecognized format: header %s does not match any known "
                    "ERP variant" % (reader.fieldnames,)
                ),
            })
            return orders, errors
        mapping = FORMAT_VARIANTS[variant]
        for row_num, row in enumerate(reader, start=2):  # 1-based, header = 1
            try:
                account_id = (row.get(mapping["account_id"]) or "").strip()
                account_name = (row.get(mapping["account_name"]) or "").strip()
                if not account_id:
                    raise ValueError("missing account id")
                order_date = parse_date(row.get(mapping["order_date"]))
                category = (row.get(mapping["category"]) or "").strip()
                if not category:
                    raise ValueError("missing product category")
                qty = parse_qty(row.get(mapping["qty"]))
                orders.append({
                    "account_id": account_id,
                    "account_name": account_name,
                    "order_date": order_date,
                    "category": category,
                    "qty": qty,
                })
            except ValueError as exc:
                errors.append({"row": row_num, "reason": str(exc)})
    return orders, errors


# ---------------------------------------------------------------------------
# Reorder-interval analysis
# ---------------------------------------------------------------------------

def compute_intervals(orders):
    """Compute per (account, category) reorder intervals.

    Returns a dict keyed by (account_id, account_name, category) with:
      order_count        number of clean orders seen
      median_interval    median days between consecutive orders (None when
                         fewer than 2 orders — not enough history to judge)
      last_date          date of the most recent order
    """
    dates_by_key = defaultdict(list)
    for o in orders:
        key = (o["account_id"], o["account_name"], o["category"])
        dates_by_key[key].append(o["order_date"])

    stats = {}
    for key, dates in dates_by_key.items():
        dates = sorted(dates)
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        stats[key] = {
            "order_count": len(dates),
            "median_interval": median(gaps) if gaps else None,
            "last_date": dates[-1],
        }
    return stats


def flag_drift(stats, today, factor=DRIFT_FACTOR):
    """Flag accounts whose time-since-last-order has drifted.

    An account is drifted when:
        days_since_last_order > median_interval * factor

    Accounts with fewer than 2 orders are skipped: with no interval history
    there is no "typical" pattern to drift from, so we report them as
    "insufficient history" rather than guessing.

    Returns alerts sorted by drift ratio (worst first).
    """
    alerts = []
    for (account_id, account_name, category), s in stats.items():
        if s["median_interval"] is None:
            alerts.append({
                "account_id": account_id,
                "account_name": account_name,
                "category": category,
                "status": "insufficient_history",
                "order_count": s["order_count"],
                "median_interval_days": "",
                "days_since_last_order": (today - s["last_date"]).days,
                "drift_ratio": "",
            })
            continue
        days_since = (today - s["last_date"]).days
        threshold = s["median_interval"] * factor
        if days_since > threshold:
            alerts.append({
                "account_id": account_id,
                "account_name": account_name,
                "category": category,
                "status": "drifted",
                "order_count": s["order_count"],
                "median_interval_days": s["median_interval"],
                "days_since_last_order": days_since,
                "drift_ratio": round(days_since / s["median_interval"], 2),
            })
    # "drifted" rows first (worst ratio first), then "insufficient_history".
    def sort_key(a):
        ratio = a["drift_ratio"]
        return (0 if a["status"] == "drifted" else 1,
                -(ratio if isinstance(ratio, (int, float)) else 0))
    return sorted(alerts, key=sort_key)


# ---------------------------------------------------------------------------
# Output: digest report + alerts CSV
# ---------------------------------------------------------------------------

def render_digest(alerts, errors, today, stats):
    """Render the scheduled email-digest body as plain text."""
    drifted = [a for a in alerts if a["status"] == "drifted"]
    thin = [a for a in alerts if a["status"] == "insufficient_history"]
    lines = [
        f"REORDER WATCH — daily digest for {today.isoformat()}",
        "=" * 60,
        f"Accounts tracked : {len(stats)}",
        f"Drifted accounts : {len(drifted)}",
        f"Thin history      : {len(thin)}",
        f"Bad rows quarantined: {len(errors)}",
        "",
    ]
    if drifted:
        lines.append("OVERDUE TO REORDER (interval drifted past %.0f%%):" % (DRIFT_FACTOR * 100))
        for a in drifted:
            lines.append(
                f"  - {a['account_name']} ({a['account_id']}) — {a['category']}: "
                f"typical interval {a['median_interval_days']} days, "
                f"last order {a['days_since_last_order']} days ago "
                f"(x{a['drift_ratio']} of typical)"
            )
        lines.append("")
    else:
        lines.append("No accounts are overdue to reorder. All good.")
        lines.append("")
    if thin:
        lines.append("ACCOUNTS WITH TOO LITTLE HISTORY TO JUDGE:")
        for a in thin:
            lines.append(f"  - {a['account_name']} ({a['account_id']}) — {a['category']}: "
                         f"only {a['order_count']} order(s) on record")
        lines.append("")
    if errors:
        lines.append("DATA QUALITY — rows quarantined from this run (see errors.csv):")
        for e in errors[:10]:
            lines.append(f"  - row {e['row']}: {e['reason']}")
        if len(errors) > 10:
            lines.append(f"  ... and {len(errors) - 10} more")
        lines.append("")
    lines.append("Full detail: alerts.csv  •  Quarantined rows: errors.csv")
    return "\n".join(lines)


def alerts_to_rows(alerts):
    """Convert alerts to CSV-ready rows."""
    fields = ["account_id", "account_name", "category", "status",
              "order_count", "median_interval_days",
              "days_since_last_order", "drift_ratio"]
    return fields, [[a[f] for f in fields] for a in alerts]


def errors_to_rows(errors):
    """Convert quarantined rows to CSV-ready rows."""
    return ["row", "reason"], [[e["row"], e["reason"]] for e in errors]


def write_csv(path, fields, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(fields)
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Demo / entry point
# ---------------------------------------------------------------------------

def run(input_paths, today, output_dir):
    """Run the full pipeline on the given CSV exports."""
    all_orders, all_errors = [], []
    for path in input_paths:
        orders, errors = load_orders(path)
        all_orders.extend(orders)
        # Tag errors with their source file so combined runs stay auditable.
        for e in errors:
            all_errors.append({"row": e["row"], "reason": f"{os.path.basename(path)}: {e['reason']}"})
    stats = compute_intervals(all_orders)
    alerts = flag_drift(stats, today)
    digest = render_digest(alerts, all_errors, today, stats)

    os.makedirs(output_dir, exist_ok=True)
    digest_path = os.path.join(output_dir, "digest.txt")
    alerts_path = os.path.join(output_dir, "alerts.csv")
    errors_path = os.path.join(output_dir, "errors.csv")
    with open(digest_path, "w", encoding="utf-8") as fh:
        fh.write(digest + "\n")
    fields, rows = alerts_to_rows(alerts)
    write_csv(alerts_path, fields, rows)
    fields, rows = errors_to_rows(all_errors)
    write_csv(errors_path, fields, rows)
    return digest, alerts, all_errors, digest_path


def main():
    """End-to-end demo: analyze the bundled sample exports."""
    here = os.path.dirname(os.path.abspath(__file__))
    inputs = [
        os.path.join(here, "data", "sample_orders_format_a.csv"),
        os.path.join(here, "data", "sample_orders_format_b.csv"),
    ]
    output_dir = os.environ.get("REORDER_OUTPUT", os.path.join(here, "output"))
    digest, alerts, errors, digest_path = run(inputs, DEMO_TODAY, output_dir)
    print(digest)
    print()
    print(f"Wrote: {digest_path}")
    print(f"       {os.path.join(output_dir, 'alerts.csv')}  ({len(alerts)} alerts)")
    print(f"       {os.path.join(output_dir, 'errors.csv')}  ({len(errors)} quarantined rows)")


if __name__ == "__main__":
    main()
