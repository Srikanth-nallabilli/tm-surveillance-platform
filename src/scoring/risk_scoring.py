"""
risk_scoring.py
=================
Turns the raw detector output (one row per rule that fired) into one
CASE per customer with a single 0-100 priority score - the thing an
analyst actually triages, instead of a pile of disconnected rule hits.

Why not just count triggered rules?
------------------------------------
A binary "flagged / not flagged" per rule throws away useful information:
a structuring cluster of 3 barely-qualifying transactions is not as
convincing as one of 9, and a Low-risk customer suddenly wiring money to a
sanctioned-adjacent country is a different story than a Money Service
Business doing the same thing as part of its normal book of business. Our
detectors already express this as a per-flag `confidence` (0-1) - the
scoring engine's job is to combine those confidences, across possibly
several different typologies, into one number.

The formula, in plain English:
  1. For each typology a customer triggered, take their STRONGEST instance
     of it (max confidence) - repeatedly re-triggering the same rule in
     different weeks shouldn't multiply the score.
  2. Multiply each typology's confidence by that typology's base weight
     (config.TYPOLOGY_WEIGHTS - how serious is this pattern, on its own).
  3. Add a "multiple independent red flags" bonus if more than one
     DISTINCT typology fired - a customer triggering structuring AND
     high-risk-juriscition AND velocity in the same period is a materially
     stronger case than the sum of three isolated rule hits would suggest,
     which is exactly how real investigators reason about compounding
     evidence. The bonus is capped so it can't dominate the score.
  4. Scale the result by the customer's own declared KYC risk rating - the
     same behavior from a High-risk customer is marginally more concerning
     than from a Low-risk one (each detector already does its own
     profile-relative reasoning internally, so this is a modest final
     adjustment, not the primary driver).
  5. Clip to 100 and bucket into a priority band for the alert queue.
"""

import json

import pandas as pd

import config


def _bucket_priority(score, cfg):
    for min_score, label in cfg.PRIORITY_BANDS:
        if score >= min_score:
            return label
    return cfg.PRIORITY_BANDS[-1][1]


def score_alerts(flags_df: pd.DataFrame, customers_df: pd.DataFrame,
                  cfg=config) -> pd.DataFrame:
    """
    Aggregate detector flags into one scored case per customer.

    Note on scope: this treats the entire historical dataset as a single
    "review period" and produces at most one case per customer. A
    production system would re-run this on a rolling cadence (e.g. daily)
    and open a new case per period; we simplify that away here since it
    doesn't change the scoring logic itself, only how often it runs.
    """
    columns = [
        "case_id", "customer_id", "customer_name", "customer_type", "business_type",
        "risk_rating", "home_country", "typologies_triggered", "num_typologies",
        "num_flags", "num_transactions_involved", "score", "priority",
        "window_start", "window_end", "flags_detail_json",
    ]
    if flags_df.empty:
        return pd.DataFrame(columns=columns)

    customers_indexed = customers_df.set_index("customer_id")
    cases = []
    case_counter = 0

    for customer_id, group in flags_df.groupby("customer_id"):
        if customer_id not in customers_indexed.index:
            continue
        profile = customers_indexed.loc[customer_id]

        typ_confidence = group.groupby("typology")["confidence"].max().to_dict()
        weighted_sum = sum(cfg.TYPOLOGY_WEIGHTS.get(t, 0) * c for t, c in typ_confidence.items())

        num_typologies = len(typ_confidence)
        multi_bonus = 0
        if num_typologies > 1:
            multi_bonus = min(cfg.MULTI_TYPOLOGY_BONUS_CAP,
                               cfg.MULTI_TYPOLOGY_BONUS_PER_EXTRA * (num_typologies - 1))

        risk_multiplier = cfg.RISK_RATING_MULTIPLIER.get(profile["risk_rating"], 1.0)
        raw_score = (weighted_sum + multi_bonus) * risk_multiplier
        final_score = round(min(100.0, raw_score), 1)
        priority = _bucket_priority(final_score, cfg)

        flags_detail = []
        all_txn_ids = set()
        for _, row in group.iterrows():
            txn_ids = row["transaction_ids"].split(";") if row["transaction_ids"] else []
            related_ids = row["related_customer_ids"].split(";") if row["related_customer_ids"] else []
            flags_detail.append({
                "typology": row["typology"],
                "confidence": row["confidence"],
                "metric_value": row["metric_value"],
                "window_start": str(row["window_start"]),
                "window_end": str(row["window_end"]),
                "transaction_ids": txn_ids,
                "related_customer_ids": related_ids,
                "evidence": json.loads(row["evidence_json"]),
            })
            all_txn_ids.update(txn_ids)

        case_counter += 1
        cases.append({
            "case_id": f"CASE{case_counter:06d}",
            "customer_id": customer_id,
            "customer_name": profile["customer_name"],
            "customer_type": profile["customer_type"],
            "business_type": profile["business_type"] if pd.notna(profile["business_type"]) else profile["occupation"],
            "risk_rating": profile["risk_rating"],
            "home_country": profile["home_country"],
            "typologies_triggered": ";".join(sorted(typ_confidence.keys())),
            "num_typologies": num_typologies,
            "num_flags": len(group),
            "num_transactions_involved": len(all_txn_ids),
            "score": final_score,
            "priority": priority,
            "window_start": group["window_start"].min(),
            "window_end": group["window_end"].max(),
            "flags_detail_json": json.dumps(flags_detail, default=str),
        })

    result = pd.DataFrame(cases, columns=columns)
    return result.sort_values("score", ascending=False).reset_index(drop=True)


if __name__ == "__main__":
    print("Run via run_pipeline.py - this module is not meant to be run standalone.")
