"""
high_risk_jurisdiction.py
===========================
Detects HIGH-RISK JURISDICTION EXPOSURE: transactions to/from countries on
our illustrative high-risk list (config.HIGH_RISK_COUNTRIES) that don't fit
what we'd expect from the customer's declared profile.

Logic:
  1. Pull every transaction where the *counterparty's* country is high-risk.
  2. Cluster them per customer using the same short-window clustering
     approach as structuring (several such transactions close together
     matters more than one, in isolation, spread over a year).
  3. Flag clusters that clear either a transaction-count or a cumulative-
     dollar threshold.
  4. Adjust confidence based on whether this exposure is actually expected
     for the customer: a Money Service Business or an Import/Export trading
     company legitimately deals with counterparties all over the world, so
     the same pattern is less surprising (and gets a lower confidence
     score) than seeing it from, say, a local retail business with a
     purely domestic customer base.
"""

import pandas as pd

import config
from common import customer_activity_view, make_flag, flags_to_dataframe, cluster_by_window

# Business types where cross-border activity (including to higher-risk
# markets) is a normal part of how the business operates.
NATURALLY_GLOBAL_BUSINESS_TYPES = {
    "Import/Export Trading", "Money Service Business", "Cryptocurrency Exchange",
}


def detect_high_risk_jurisdiction(transactions_df: pd.DataFrame, customers_df: pd.DataFrame,
                                   cfg=config) -> pd.DataFrame:
    customers_indexed = customers_df.set_index("customer_id")

    activity = customer_activity_view(transactions_df)
    activity = activity[activity["party_customer_id"].isin(customers_indexed.index)].copy()
    activity["own_home_country"] = activity["party_customer_id"].map(customers_indexed["home_country"])

    # Two guards keep this typology meaningful instead of noisy:
    #  1. Country must differ from the customer's own home country - a
    #     customer whose own home country sits on our illustrative
    #     high-risk list shouldn't have their ordinary domestic activity
    #     misread as cross-border exposure.
    #  2. Counterparty must be EXTERNAL (outside our bank). A transfer to
    #     another of our own customers is already covered by that other
    #     customer's own KYC risk rating - "jurisdiction exposure" is about
    #     money moving somewhere the bank has no visibility into, not
    #     between two accounts we already monitor directly.
    is_external = ~activity["counterparty_id_for_party"].astype(str).str.startswith("CUST")
    risky = activity[
        activity["counterparty_country_for_party"].isin(cfg.HIGH_RISK_COUNTRIES)
        & (activity["counterparty_country_for_party"] != activity["own_home_country"])
        & is_external
    ].copy()

    flags = []

    for customer_id, group in risky.groupby("party_customer_id"):
        if customer_id not in customers_indexed.index:
            continue
        group = group.sort_values("date").reset_index(drop=True)
        dates = list(group["date"])

        for idx_cluster in cluster_by_window(dates, cfg.HIGH_RISK_WINDOW_DAYS):
            cluster = group.iloc[idx_cluster]
            count = len(cluster)
            total = cluster["amount_usd"].sum()
            if count < cfg.HIGH_RISK_MIN_TXN_COUNT and total < cfg.HIGH_RISK_MIN_CUMULATIVE_AMOUNT:
                continue

            profile = customers_indexed.loc[customer_id]
            is_naturally_global = (
                profile["home_country"] in cfg.HIGH_RISK_COUNTRIES
                or profile["business_type"] in NATURALLY_GLOBAL_BUSINESS_TYPES
            )
            consistency_penalty = 0.5 if is_naturally_global else 1.0

            count_score = min(1.0, count / (cfg.HIGH_RISK_MIN_TXN_COUNT * 2))
            amount_score = min(1.0, total / (cfg.HIGH_RISK_MIN_CUMULATIVE_AMOUNT * 2))
            confidence = min(1.0, max(count_score, amount_score) * consistency_penalty)

            evidence = {
                "num_transactions": int(count),
                "total_amount_usd": round(float(total), 2),
                "countries_involved": sorted(cluster["counterparty_country_for_party"].unique().tolist()),
                "customer_declared_risk_rating": profile["risk_rating"],
                "customer_home_country": profile["home_country"],
                "customer_business_type": profile["business_type"] or profile["occupation"],
                "profile_assessment": (
                    "Consistent with declared business/geography - lower priority"
                    if is_naturally_global else
                    "Inconsistent with customer's declared profile - unexpected exposure"
                ),
                "window_days": int((cluster["date"].max() - cluster["date"].min()).days),
                "transactions": [
                    {
                        "transaction_id": r["transaction_id"],
                        "date": str(r["date"]),
                        "amount_usd": r["amount_usd"],
                        "counterparty_country": r["counterparty_country_for_party"],
                        "counterparty_name": r["counterparty_id_for_party"],
                    }
                    for _, r in cluster.iterrows()
                ],
            }

            flags.append(make_flag(
                customer_id=customer_id,
                typology="high_risk_jurisdiction",
                confidence=confidence,
                metric_value=total,
                window_start=cluster["date"].min(),
                window_end=cluster["date"].max(),
                transaction_ids=list(cluster["transaction_id"]),
                evidence=evidence,
            ))

    return flags_to_dataframe(flags)


if __name__ == "__main__":
    print("Run via run_pipeline.py - this module is not meant to be run standalone.")
