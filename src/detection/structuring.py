"""
structuring.py
===============
Detects STRUCTURING: multiple transactions deliberately kept just under a
reporting threshold (config.CTR_THRESHOLD, modeled on the $10,000 US
Currency Transaction Report line) and clustered close together in time.

Logic in plain English:
  1. Pull every transaction between 80% and 100% of the threshold
     (config.STRUCTURING_LOWER_RATIO controls the lower bound) - the "just
     under the line" band.
  2. For each customer, walk through their near-threshold transactions in
     date order and group any that fall within a short rolling window
     (config.STRUCTURING_WINDOW_DAYS) of each other.
  3. If a group has at least config.STRUCTURING_MIN_TXN_COUNT transactions,
     flag it - a single near-threshold transaction is unremarkable, several
     in a few days is the classic structuring red flag.

We use amount_usd (not the raw `amount`) so the threshold comparison is
apples-to-apples regardless of transaction currency.
"""

import pandas as pd

import config
from common import customer_activity_view, make_flag, flags_to_dataframe, cluster_by_window


def detect_structuring(transactions_df: pd.DataFrame, customers_df: pd.DataFrame,
                        cfg=config) -> pd.DataFrame:
    activity = customer_activity_view(transactions_df)

    lower_bound = cfg.CTR_THRESHOLD * cfg.STRUCTURING_LOWER_RATIO
    near_threshold = activity[
        (activity["amount_usd"] >= lower_bound) & (activity["amount_usd"] < cfg.CTR_THRESHOLD)
    ].copy()

    flags = []
    for customer_id, group in near_threshold.groupby("party_customer_id"):
        group = group.sort_values("date").reset_index(drop=True)
        dates = list(group["date"])

        for idx_cluster in cluster_by_window(dates, cfg.STRUCTURING_WINDOW_DAYS):
            if len(idx_cluster) < cfg.STRUCTURING_MIN_TXN_COUNT:
                continue
            cluster = group.iloc[idx_cluster]

            total_amount = cluster["amount_usd"].sum()
            avg_proximity = (cluster["amount_usd"] / cfg.CTR_THRESHOLD).mean()  # closer to 1.0 = more deliberate
            size_score = min(1.0, 0.5 + (len(cluster) - cfg.STRUCTURING_MIN_TXN_COUNT) / 5)
            confidence = min(1.0, 0.5 * size_score + 0.5 * avg_proximity)

            evidence = {
                "num_transactions": int(len(cluster)),
                "total_amount_usd": round(float(total_amount), 2),
                "avg_amount_usd": round(float(cluster["amount_usd"].mean()), 2),
                "reporting_threshold_usd": cfg.CTR_THRESHOLD,
                "window_days": int((cluster["date"].max() - cluster["date"].min()).days),
                "transactions": [
                    {
                        "transaction_id": r["transaction_id"],
                        "date": str(r["date"]),
                        "amount_usd": r["amount_usd"],
                        "transaction_type": r["transaction_type"],
                        "counterparty_name": r["counterparty_id_for_party"],
                    }
                    for _, r in cluster.iterrows()
                ],
            }

            flags.append(make_flag(
                customer_id=customer_id,
                typology="structuring",
                confidence=confidence,
                metric_value=total_amount,
                window_start=cluster["date"].min(),
                window_end=cluster["date"].max(),
                transaction_ids=list(cluster["transaction_id"]),
                evidence=evidence,
            ))

    return flags_to_dataframe(flags)


if __name__ == "__main__":
    print("Run via run_pipeline.py - this module is not meant to be run standalone.")
