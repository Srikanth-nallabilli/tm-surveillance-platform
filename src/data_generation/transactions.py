"""
transactions.py
================
Generates "normal" (non-suspicious) transaction history for every customer.

Design idea: each row is a single transaction *event* with a sender and a
receiver. Exactly one side of every transaction is the customer whose
account we're generating activity for (the `customer_id` column) - that's
the account being monitored. The other side (the "counterparty") is either:
  - another customer in our synthetic bank (an "internal" transfer), or
  - someone entirely outside the bank (an "external" counterparty), which
    is the far more common case in reality.

Having some internal counterparties matters a lot later: it's what lets the
layering and round-tripping detectors trace money hopping from one of our
customers to another, exactly like a real investigator following a chain of
accounts.

This module intentionally produces *only* normal-looking activity. Suspicious
patterns are injected separately in typologies.py, so we can keep a clean
mental (and code) separation between "the noisy but innocent background"
and "the needles we hid in the haystack".
"""

import string

import numpy as np
import pandas as pd
from faker import Faker

import config

TRANSACTION_TYPES_INDIVIDUAL = {
    "Card Payment": 0.35,
    "ACH Transfer": 0.25,
    "Cash Deposit": 0.15,
    "Cash Withdrawal": 0.15,
    "Wire Transfer": 0.05,
    "Check": 0.05,
}
TRANSACTION_TYPES_BUSINESS = {
    "Wire Transfer": 0.30,
    "ACH Transfer": 0.30,
    "Check": 0.15,
    "Cash Deposit": 0.15,
    "Cash Withdrawal": 0.05,
    "Card Payment": 0.05,
}

# Cash-based transaction types tend to involve smaller amounts in practice
# (there's a physical/practical ceiling on cash handling) - we model that
# with a size multiplier applied to the customer's "typical amount".
TYPE_SIZE_MULTIPLIER = {
    "Cash Deposit": 0.4,
    "Cash Withdrawal": 0.3,
    "Card Payment": 0.15,
    "Check": 0.6,
    "ACH Transfer": 1.0,
    "Wire Transfer": 1.6,
}

CURRENCIES_BY_COUNTRY = {
    "US": "USD", "GB": "GBP", "DE": "EUR", "FR": "EUR", "NL": "EUR",
    "IT": "EUR", "ES": "EUR", "CA": "CAD", "AU": "AUD", "JP": "JPY",
    "SG": "SGD", "CH": "CHF", "SE": "SEK", "KR": "KRW", "HK": "HKD",
    "IN": "INR", "BR": "BRL", "MX": "MXN", "ZA": "ZAR",
}
DEFAULT_CURRENCY = "USD"

# Simplified, static FX rates to USD purely so we can compute an
# `amount_usd` column for apples-to-apples threshold comparisons across
# currencies (real systems pull live rates - not needed for a simulation).
FX_TO_USD = {
    "USD": 1.0, "GBP": 1.27, "EUR": 1.08, "CAD": 0.73, "AUD": 0.66,
    "JPY": 0.0067, "SGD": 0.74, "CHF": 1.12, "SEK": 0.094, "KRW": 0.00075,
    "HKD": 0.128, "INR": 0.012, "BRL": 0.20, "MXN": 0.059, "ZAR": 0.055,
}


def _random_external_id(rng):
    suffix = "".join(rng.choice(list(string.ascii_uppercase + string.digits), size=8))
    return f"EXT-{suffix}"


def _pick_transaction_type(rng, customer_type):
    table = TRANSACTION_TYPES_BUSINESS if customer_type == "Business" else TRANSACTION_TYPES_INDIVIDUAL
    types, weights = zip(*table.items())
    return rng.choice(types, p=weights)


def _pick_counterparty(rng, fake, customer_row, customers_df):
    """
    Decide who the other side of the transaction is.

    ~25% of the time we pick another real customer (internal transfer),
    which builds the connected network structure our layering/round-tripping
    detectors rely on. Otherwise we invent an external party. Most external
    counterparties share the customer's home country; a small slice are
    foreign, and a very small slice happen to be in a medium-risk country
    even for ordinary customers (real life is never perfectly clean - a few
    organic false-positive candidates make the detectors' job realistically
    imperfect, which is worth discussing in an interview).

    Genuinely HIGH-risk-country counterparties are kept rare and mostly
    reserved for customers whose business naturally involves cross-border
    flows (import/export, money service businesses, crypto exchanges) - so
    the high-risk-jurisdiction detector's "is this consistent with the
    customer's declared profile" logic actually has organic cases to react
    to, rather than every customer accumulating high-risk exposure at
    random. The deliberate, meaningful high-risk scenarios come from
    typologies.py, not from this background generator.
    """
    is_internal = rng.random() < 0.25
    if is_internal and len(customers_df) > 1:
        other = customers_df.sample(n=1, random_state=int(rng.integers(0, 1_000_000))).iloc[0]
        if other["customer_id"] == customer_row["customer_id"]:
            is_internal = False
        else:
            return other["customer_id"], other["customer_name"], other["home_country"]

    # External counterparty
    naturally_global = customer_row.get("business_type") in {
        "Import/Export Trading", "Money Service Business", "Cryptocurrency Exchange",
    }
    roll = rng.random()
    if roll < 0.80:
        country = customer_row["home_country"]
    elif roll < 0.95:
        country = rng.choice([c for c in config.ALL_COUNTRIES
                               if c not in config.HIGH_RISK_COUNTRIES])
    elif naturally_global:
        country = rng.choice(config.MEDIUM_RISK_COUNTRIES + config.HIGH_RISK_COUNTRIES)
    else:
        country = rng.choice(config.MEDIUM_RISK_COUNTRIES)

    name = fake.company() if rng.random() < 0.5 else fake.name()
    return _random_external_id(rng), name, country


def generate_transactions(customers_df: pd.DataFrame,
                           months: int = config.HISTORY_MONTHS,
                           seed: int = config.RANDOM_SEED) -> pd.DataFrame:
    """
    Generate normal transaction history for every customer in customers_df.

    Returns a DataFrame with one row per transaction event: who sent it, who
    received it, when, how much, in what currency, and what type it was.
    """
    Faker.seed(seed)
    fake = Faker()
    rng = np.random.default_rng(seed)

    end_date = pd.Timestamp.today().normalize()
    start_date = end_date - pd.Timedelta(days=months * 30)

    rows = []
    txn_counter = 0

    for _, cust in customers_df.iterrows():
        # How many transactions does this customer generate over the whole
        # history window? Centered on their declared expected monthly count.
        total_expected = max(1, int(cust["expected_monthly_txn_count"] * months))
        n_txns = max(1, int(rng.poisson(lam=total_expected)))

        avg_amount = cust["expected_monthly_volume"] / max(1, cust["expected_monthly_txn_count"])

        # Random transaction dates spread across the window. Business
        # customers skew towards weekdays (banks / trade partners operate
        # on business days); individuals are spread more evenly.
        raw_days = rng.integers(0, (end_date - start_date).days, size=n_txns)
        dates = [start_date + pd.Timedelta(days=int(d),
                                            hours=int(rng.integers(0, 24)),
                                            minutes=int(rng.integers(0, 60)))
                 for d in raw_days]

        for txn_date in dates:
            txn_counter += 1
            transaction_type = _pick_transaction_type(rng, cust["customer_type"])
            direction = "credit" if rng.random() < 0.5 else "debit"

            size_mult = TYPE_SIZE_MULTIPLIER.get(transaction_type, 1.0)
            amount = float(rng.lognormal(mean=np.log(max(avg_amount * size_mult, 5)), sigma=0.6))
            amount = round(min(amount, avg_amount * 8), 2)  # cap extreme outliers

            cp_id, cp_name, cp_country = _pick_counterparty(rng, fake, cust, customers_df)

            currency = CURRENCIES_BY_COUNTRY.get(cp_country, DEFAULT_CURRENCY) \
                if cp_country != cust["home_country"] and rng.random() < 0.5 \
                else CURRENCIES_BY_COUNTRY.get(cust["home_country"], DEFAULT_CURRENCY)
            amount_usd = round(amount * FX_TO_USD.get(currency, 1.0), 2)

            if direction == "credit":
                sender_id, sender_name, sender_country = cp_id, cp_name, cp_country
                receiver_id, receiver_name, receiver_country = (
                    cust["customer_id"], cust["customer_name"], cust["home_country"]
                )
            else:
                sender_id, sender_name, sender_country = (
                    cust["customer_id"], cust["customer_name"], cust["home_country"]
                )
                receiver_id, receiver_name, receiver_country = cp_id, cp_name, cp_country

            rows.append({
                "transaction_id": f"TXN{txn_counter:08d}",
                "customer_id": cust["customer_id"],
                "date": txn_date,
                "amount": amount,
                "currency": currency,
                "amount_usd": amount_usd,
                "direction": direction,
                "transaction_type": transaction_type,
                "sender_id": sender_id,
                "sender_name": sender_name,
                "sender_country": sender_country,
                "receiver_id": receiver_id,
                "receiver_name": receiver_name,
                "receiver_country": receiver_country,
                "counterparty_id": cp_id,
                "counterparty_name": cp_name,
                "counterparty_country": cp_country,
            })

    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    # Re-number transaction_id in chronological order for readability.
    df["transaction_id"] = [f"TXN{i+1:08d}" for i in range(len(df))]
    return df


if __name__ == "__main__":
    from customers import generate_customers
    custs = generate_customers(n_customers=20)
    txns = generate_transactions(custs, months=3)
    print(txns.head(10).to_string())
    print(f"\nGenerated {len(txns)} transactions for {len(custs)} customers")
