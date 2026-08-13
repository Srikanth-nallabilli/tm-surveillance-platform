"""
test_detectors.py
===================
Lightweight sanity tests for the detection and scoring logic.

These are NOT exhaustive unit tests for every edge case - they exist to
catch the two things most likely to silently break this project as it
evolves: (1) a detector crashing outright, and (2) a detector losing the
ability to catch the exact pattern it's designed for. Each test builds a
tiny, hand-crafted transaction set with an obvious instance of one
typology and checks that the matching detector fires on it.

Run with:  pytest tests/test_detectors.py -v
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
for sub in ["data_generation", "detection", "scoring"]:
    sys.path.insert(0, str(PROJECT_ROOT / "src" / sub))

import config                                    # noqa: E402
from structuring import detect_structuring       # noqa: E402
from layering import detect_layering             # noqa: E402
from velocity import detect_velocity             # noqa: E402
from high_risk_jurisdiction import detect_high_risk_jurisdiction  # noqa: E402
from round_tripping import detect_round_tripping  # noqa: E402
from risk_scoring import score_alerts            # noqa: E402


def make_customer(customer_id="CUST00001", risk_rating="Low", home_country="US",
                   business_type=None, expected_monthly_txn_count=10,
                   expected_monthly_volume=5000.0):
    return {
        "customer_id": customer_id, "customer_name": f"Test Customer {customer_id}",
        "customer_type": "Business" if business_type else "Individual",
        "business_type": business_type, "occupation": None if business_type else "Self-Employed",
        "home_country": home_country, "is_pep": False, "risk_rating": risk_rating,
        "expected_activity_level": "Medium", "expected_monthly_volume": expected_monthly_volume,
        "expected_monthly_txn_count": expected_monthly_txn_count, "onboarding_date": "2024-01-01",
    }


def make_txn(txn_id, date, amount, sender_id, receiver_id,
             transaction_type="Wire Transfer", sender_country="US", receiver_country="US"):
    return {
        "transaction_id": txn_id, "customer_id": sender_id if sender_id.startswith("CUST") else receiver_id,
        "date": pd.Timestamp(date), "amount": amount, "currency": "USD", "amount_usd": amount,
        "direction": "debit", "transaction_type": transaction_type,
        "sender_id": sender_id, "sender_name": sender_id, "sender_country": sender_country,
        "receiver_id": receiver_id, "receiver_name": receiver_id, "receiver_country": receiver_country,
        "counterparty_id": receiver_id, "counterparty_name": receiver_id,
        "counterparty_country": receiver_country,
    }


def test_structuring_detects_obvious_pattern():
    customers = pd.DataFrame([make_customer()])
    txns = pd.DataFrame([
        make_txn(f"T{i}", f"2024-06-{10+i:02d}", 9500 + i * 20, "EXT-CASH", "CUST00001", "Cash Deposit")
        for i in range(4)
    ])
    flags = detect_structuring(txns, customers)
    assert len(flags) >= 1
    assert flags.iloc[0]["customer_id"] == "CUST00001"
    assert flags.iloc[0]["confidence"] > 0


def test_structuring_ignores_normal_activity():
    customers = pd.DataFrame([make_customer()])
    # Amounts nowhere near the reporting threshold - should never fire.
    txns = pd.DataFrame([
        make_txn(f"T{i}", f"2024-06-{10+i:02d}", 150 + i * 10, "EXT-A", "CUST00001", "Card Payment")
        for i in range(5)
    ])
    flags = detect_structuring(txns, customers)
    assert len(flags) == 0


def test_layering_detects_chain():
    customers = pd.DataFrame([make_customer(f"CUST{i:05d}") for i in range(1, 6)])
    txns = pd.DataFrame([
        make_txn("T1", "2024-06-01 09:00", 20000, "CUST00001", "CUST00002"),
        make_txn("T2", "2024-06-01 15:00", 19000, "CUST00002", "CUST00003"),
        make_txn("T3", "2024-06-02 08:00", 18200, "CUST00003", "CUST00004"),
        make_txn("T4", "2024-06-02 20:00", 17500, "CUST00004", "CUST00005"),
    ])
    flags = detect_layering(txns, customers)
    assert len(flags) >= 1
    assert flags.iloc[0]["customer_id"] == "CUST00001"


def test_round_tripping_detects_direct_cycle():
    customers = pd.DataFrame([make_customer("CUST00001"), make_customer("CUST00002")])
    txns = pd.DataFrame([
        make_txn("T1", "2024-06-01", 30000, "CUST00001", "CUST00002"),
        make_txn("T2", "2024-06-05", 29500, "CUST00002", "CUST00001"),
    ])
    flags = detect_round_tripping(txns, customers)
    assert len(flags) >= 1
    assert flags.iloc[0]["evidence_json"]  # evidence should be populated


def test_high_risk_jurisdiction_flags_unexpected_exposure():
    customers = pd.DataFrame([make_customer("CUST00001", business_type="Consulting Services")])
    risky_country = config.HIGH_RISK_COUNTRIES[0]
    txns = pd.DataFrame([
        make_txn(f"T{i}", f"2024-06-{10+i:02d}", 8000, "CUST00001", f"EXT-{i}",
                  receiver_country=risky_country)
        for i in range(3)
    ])
    flags = detect_high_risk_jurisdiction(txns, customers)
    assert len(flags) >= 1


def test_high_risk_jurisdiction_ignores_own_home_country():
    # Customer's own home country happens to be on the high-risk list -
    # their domestic activity should NOT be flagged as "foreign exposure".
    risky_country = config.HIGH_RISK_COUNTRIES[0]
    customers = pd.DataFrame([make_customer("CUST00001", home_country=risky_country)])
    txns = pd.DataFrame([
        make_txn(f"T{i}", f"2024-06-{10+i:02d}", 8000, "CUST00001", f"EXT-{i}",
                  sender_country=risky_country, receiver_country=risky_country)
        for i in range(5)
    ])
    flags = detect_high_risk_jurisdiction(txns, customers)
    assert len(flags) == 0


def test_scoring_ranks_multi_typology_case_higher():
    customers = pd.DataFrame([make_customer("CUST00001"), make_customer("CUST00002")])
    flags = pd.DataFrame([
        {"customer_id": "CUST00001", "typology": "structuring", "confidence": 0.8,
         "metric_value": 1, "window_start": "2024-06-01", "window_end": "2024-06-05",
         "transaction_ids": "T1;T2;T3", "related_customer_ids": "", "evidence_json": "{}"},
        {"customer_id": "CUST00002", "typology": "structuring", "confidence": 0.8,
         "metric_value": 1, "window_start": "2024-06-01", "window_end": "2024-06-05",
         "transaction_ids": "T4;T5;T6", "related_customer_ids": "", "evidence_json": "{}"},
        {"customer_id": "CUST00002", "typology": "velocity", "confidence": 0.8,
         "metric_value": 5, "window_start": "2024-06-01", "window_end": "2024-06-07",
         "transaction_ids": "T7;T8", "related_customer_ids": "", "evidence_json": "{}"},
    ])
    cases = score_alerts(flags, customers)
    score_1 = cases.loc[cases["customer_id"] == "CUST00001", "score"].iloc[0]
    score_2 = cases.loc[cases["customer_id"] == "CUST00002", "score"].iloc[0]
    assert score_2 > score_1  # two independent typologies should outscore one


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
