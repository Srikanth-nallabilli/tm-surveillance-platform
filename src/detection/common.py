"""
common.py
=========
Shared helpers used by every typology detector.

Design note on how detectors read the transaction table:
----------------------------------------------------------
Each row in transactions.csv is ONE transaction event with a `sender_id`
and a `receiver_id`. Only one of those two columns is guaranteed to be a
"real" customer of our bank (ids starting with "CUST"); the other might be
an external party. Occasionally BOTH sides are our own customers (an
internal transfer) - and in that case the event belongs on *both*
customers' activity history, even though it's stored as a single row.

`customer_activity_view()` below turns the raw transaction table into a
"long" table with one row per (transaction, account-that-owns-it) pair, so
that "give me everything that happened on customer X's account" is a simple
filter, regardless of whether X was the sender or the receiver, and
regardless of whether the counterparty was internal or external.

We deliberately do NOT use this long view for volume/MI reporting (that
would double count internal transfers) - it exists purely to support
per-account pattern detection.
"""

import json

import pandas as pd

import config


def customer_activity_view(transactions_df: pd.DataFrame) -> pd.DataFrame:
    """Reshape transactions into one row per (transaction, owning account)."""
    is_cust_sender = transactions_df["sender_id"].astype(str).str.startswith("CUST")
    is_cust_receiver = transactions_df["receiver_id"].astype(str).str.startswith("CUST")

    sender_view = transactions_df[is_cust_sender].copy()
    sender_view["party_customer_id"] = sender_view["sender_id"]
    sender_view["party_role"] = "sender"
    sender_view["counterparty_id_for_party"] = sender_view["receiver_id"]
    sender_view["counterparty_country_for_party"] = sender_view["receiver_country"]

    receiver_view = transactions_df[is_cust_receiver].copy()
    receiver_view["party_customer_id"] = receiver_view["receiver_id"]
    receiver_view["party_role"] = "receiver"
    receiver_view["counterparty_id_for_party"] = receiver_view["sender_id"]
    receiver_view["counterparty_country_for_party"] = receiver_view["sender_country"]

    combined = pd.concat([sender_view, receiver_view], ignore_index=True)
    return combined


def cluster_by_window(dates, window_days):
    """
    Greedily group a sorted list of dates into clusters where every date in
    a cluster is within `window_days` of the cluster's first date.
    Used by any detector that looks for "several things happening close
    together in time" (structuring, high-risk jurisdiction bursts).
    Returns a list of index-lists (positions into the original `dates` list).
    """
    clusters = []
    i = 0
    n = len(dates)
    while i < n:
        cluster = [i]
        j = i + 1
        while j < n and (dates[j] - dates[i]).days <= window_days:
            cluster.append(j)
            j += 1
        clusters.append(cluster)
        i = j if len(cluster) > 1 else i + 1
    return clusters


def make_flag(customer_id, typology, confidence, metric_value,
              window_start, window_end, transaction_ids,
              related_customer_ids=None, evidence=None):
    """
    Build one standardized detector output row. Every detector (structuring,
    layering, velocity, high_risk_jurisdiction, round_tripping) returns a
    DataFrame made of rows shaped exactly like this, so the scoring engine
    downstream can treat all five typologies identically.

    confidence     : 0.0-1.0, how strong *this specific instance* of the
                     pattern is (not just "did it trigger or not" - a
                     structuring case with 8 near-threshold transactions is
                     more convincing than one with exactly 3).
    metric_value    : the key number behind the confidence score (useful to
                     show an analyst directly, e.g. "z-score = 4.2").
    transaction_ids : list of transaction_id strings that make up the evidence.
    evidence        : dict of extra human-readable detail for the investigation view.
    """
    return {
        "customer_id": customer_id,
        "typology": typology,
        "confidence": round(float(confidence), 3),
        "metric_value": round(float(metric_value), 2),
        "window_start": window_start,
        "window_end": window_end,
        "transaction_ids": ";".join(transaction_ids),
        "related_customer_ids": ";".join(related_customer_ids or []),
        "evidence_json": json.dumps(evidence or {}, default=str),
    }


def flags_to_dataframe(flag_rows):
    cols = ["customer_id", "typology", "confidence", "metric_value",
            "window_start", "window_end", "transaction_ids",
            "related_customer_ids", "evidence_json"]
    if not flag_rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(flag_rows, columns=cols)
