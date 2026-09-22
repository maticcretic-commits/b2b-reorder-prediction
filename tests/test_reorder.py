"""Tests for the b2b-reorder-prediction pipeline."""

import csv
import os
import tempfile
from datetime import date

import pytest

import reorder

DATA = os.path.join(os.path.dirname(reorder.__file__), "data")
A_CSV = os.path.join(DATA, "sample_orders_format_a.csv")
B_CSV = os.path.join(DATA, "sample_orders_format_b.csv")
TODAY = date(2026, 9, 23)

# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def test_detect_format_a():
    with open(A_CSV, newline="", encoding="utf-8-sig") as fh:
        header = next(csv.reader(fh))
    assert reorder.detect_format(header) == "format_a_epicor_like"


def test_detect_format_b():
    with open(B_CSV, newline="", encoding="utf-8-sig") as fh:
        header = next(csv.reader(fh))
    assert reorder.detect_format(header) == "format_b_sage_like"


def test_detect_format_unknown_returns_none():
    assert reorder.detect_format(["foo", "bar", "baz"]) is None
    assert reorder.detect_format([]) is None


# ---------------------------------------------------------------------------
# Loading + bad-row quarantine
# ---------------------------------------------------------------------------

def test_format_a_loads_good_rows_and_quarantines_bad_ones():
    orders, errors = reorder.load_orders(A_CSV)
    # 11 good rows (7 Northgate/Fasteners + 4 Peak/Electrical)
    assert len(orders) == 11
    # 3 bad rows: bad date, missing qty, missing account id
    assert len(errors) == 3
    reasons = " | ".join(e["reason"] for e in errors)
    assert "unparseable order date" in reasons
    assert "missing quantity" in reasons
    assert "missing account id" in reasons


def test_format_b_loads_good_rows_and_quarantines_bad_ones():
    orders, errors = reorder.load_orders(B_CSV)
    # 7 good rows (4 Lakeside/Adhesives + 3 Harbor/Fasteners)
    assert len(orders) == 7
    # 2 bad rows: impossible date 31/02/2026, negative quantity
    assert len(errors) == 2
    reasons = " | ".join(e["reason"] for e in errors)
    assert "unparseable order date" in reasons
    assert "non-positive quantity" in reasons


def test_date_parsing_accepts_both_styles_and_rejects_impossible():
    assert reorder.parse_date("2026-06-11") == date(2026, 6, 11)
    assert reorder.parse_date("29/07/2026") == date(2026, 7, 29)
    with pytest.raises(ValueError):
        reorder.parse_date("2026-13-01")   # impossible month
    with pytest.raises(ValueError):
        reorder.parse_date("31/02/2026")   # impossible day
    with pytest.raises(ValueError):
        reorder.parse_date("")             # empty


# ---------------------------------------------------------------------------
# Interval computation
# ---------------------------------------------------------------------------

def test_median_interval_computation():
    orders, _ = reorder.load_orders(A_CSV)
    stats = reorder.compute_intervals(orders)
    northgate = stats[("ACC-101", "Northgate Hardware", "Fasteners")]
    assert northgate["order_count"] == 7
    assert northgate["median_interval"] == 14
    peak = stats[("ACC-103", "Peak Electric", "Electrical")]
    assert peak["median_interval"] == 32


def test_single_order_has_no_interval():
    stats = reorder.compute_intervals([{
        "account_id": "ACC-999", "account_name": "Solo Co",
        "order_date": date(2026, 9, 1), "category": "Widgets", "qty": 1,
    }])
    assert stats[("ACC-999", "Solo Co", "Widgets")]["median_interval"] is None


# ---------------------------------------------------------------------------
# Drift flagging
# ---------------------------------------------------------------------------

def test_drift_flagging_flags_drifted_and_skips_healthy():
    orders_a, _ = reorder.load_orders(A_CSV)
    orders_b, _ = reorder.load_orders(B_CSV)
    stats = reorder.compute_intervals(orders_a + orders_b)
    alerts = reorder.flag_drift(stats, TODAY)
    drifted = {(a["account_id"], a["category"]) for a in alerts
               if a["status"] == "drifted"}
    # Northgate/Fasteners: 35 days since last, 14-day interval -> drifted
    assert ("ACC-101", "Fasteners") in drifted
    # Harbor/Fasteners: 82 days since last, 14-day interval -> drifted
    assert ("ACC-104", "Fasteners") in drifted
    # Peak/Electrical: 18 days since last, 32-day interval -> healthy, not drifted
    assert ("ACC-103", "Electrical") not in drifted
    # Lakeside/Adhesives: 26 days since last, 30-day interval -> healthy
    assert ("ACC-102", "Adhesives") not in drifted


def test_drift_threshold_is_strictly_greater_than():
    # Exactly at the threshold (1.25x) must NOT flag; only past it flags.
    stats = {("A", "A Co", "Cat"): {
        "order_count": 2, "median_interval": 20,
        "last_date": date(2026, 8, 29),  # 25 days before TODAY = 1.25 x 20
    }}
    assert reorder.flag_drift(stats, TODAY) == []
    stats[("A", "A Co", "Cat")]["last_date"] = date(2026, 8, 28)  # 26 days
    alerts = reorder.flag_drift(stats, TODAY)
    assert len(alerts) == 1 and alerts[0]["status"] == "drifted"


def test_insufficient_history_is_reported_not_flagged_as_drifted():
    stats = {("B", "B Co", "Cat"): {
        "order_count": 1, "median_interval": None,
        "last_date": date(2026, 9, 1),
    }}
    alerts = reorder.flag_drift(stats, TODAY)
    assert len(alerts) == 1
    assert alerts[0]["status"] == "insufficient_history"


# ---------------------------------------------------------------------------
# Digest output
# ---------------------------------------------------------------------------

def test_digest_generation_mentions_alerts_and_errors():
    orders_a, errors_a = reorder.load_orders(A_CSV)
    orders_b, errors_b = reorder.load_orders(B_CSV)
    stats = reorder.compute_intervals(orders_a + orders_b)
    alerts = reorder.flag_drift(stats, TODAY)
    digest = reorder.render_digest(alerts, errors_a + errors_b, TODAY, stats)
    assert "Northgate Hardware" in digest
    assert "Harbor Wholesale" in digest
    assert "Peak Electric" not in digest or "OVERDUE" not in digest.split("Peak")[0]
    assert "quarantined" in digest.lower()
    fields, rows = reorder.alerts_to_rows(alerts)
    assert fields[0] == "account_id"
    assert all(len(r) == len(fields) for r in rows)


def test_run_end_to_end_writes_files(tmp_path):
    digest, alerts, errors, digest_path = reorder.run(
        [A_CSV, B_CSV], TODAY, str(tmp_path))
    assert os.path.exists(digest_path)
    assert os.path.exists(os.path.join(str(tmp_path), "alerts.csv"))
    assert os.path.exists(os.path.join(str(tmp_path), "errors.csv"))
    assert len(alerts) == 2          # Northgate + Harbor drifted
    assert len(errors) == 5          # 3 bad rows (A) + 2 bad rows (B)
    assert "OVERDUE TO REORDER" in digest
