"""
typologies.py
==============
Injects deliberately suspicious transaction patterns into the otherwise
"clean" dataset produced by transactions.py, and records a ground-truth
label for every injected pattern.

This is the most important file for testing the rest of the platform: it
gives us a known answer key ("these exact transactions, on this customer,
are a structuring scenario") that we can later use to check whether our
rule-based detectors actually catch what they're supposed to catch. Real
AML teams do something similar when tuning rules - they use known past
SAR (Suspicious Activity Report) cases as a regression test set.

Every injected row gets two extra columns that ordinary transactions don't
have: `gt_scenario_id` and `gt_typology` ("gt" = ground truth). These are
saved to a *separate* ground_truth_scenarios.csv file that the detectors
and dashboard never read - they exist purely so we (the developer) can
grade detector performance. This mirrors a real-world train/test split:
the detection logic must work "blind", using only the transaction data a
bank would actually have.

Five typologies are implemented here, one function each:
  - inject_structuring
  - inject_layering
  - inject_velocity
  - inject_high_risk_jurisdiction
  - inject_round_tripping
"""

import string

import numpy as np
import pandas as pd
from faker import Faker

import config

TXN_COLUMNS = [
    "transaction_id", "customer_id", "date", "amount", "currency", "amount_usd",
    "direction", "transaction_type", "sender_id", "sender_name", "sender_country",
    "receiver_id", "receiver_name", "receiver_country",
    "counterparty_id", "counterparty_name", "counterparty_country",
    "gt_scenario_id", "gt_typology",
]


def _external_id(rng, prefix="EXT"):
    suffix = "".join(rng.choice(list(string.ascii_uppercase + string.digits), size=8))
    return f"{prefix}-{suffix}"


def _owner_id(sender_id, receiver_id):
    """Whichever side is a real customer_id becomes the row's 'owner' account."""
    if str(sender_id).startswith("CUST"):
        return sender_id
    if str(receiver_id).startswith("CUST"):
        return receiver_id
    return sender_id  # fallback, shouldn't normally happen


def _base_row(rng, txn_id, date, amount, currency, direction, txn_type,
              sender_id, sender_name, sender_country,
              receiver_id, receiver_name, receiver_country,
              scenario_id, typology):
    # All injected scenarios use USD directly (amount == amount_usd), which
    # keeps the injection logic simple - the FX conversion in transactions.py
    # only applies to organically generated, multi-currency background activity.
    return {
        "transaction_id": txn_id,
        "customer_id": _owner_id(sender_id, receiver_id),
        "date": date,
        "amount": round(amount, 2),
        "currency": currency,
        "amount_usd": round(amount, 2),
        "direction": direction,
        "transaction_type": txn_type,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "sender_country": sender_country,
        "receiver_id": receiver_id,
        "receiver_name": receiver_name,
        "receiver_country": receiver_country,
        "counterparty_id": receiver_id if direction == "debit" else sender_id,
        "counterparty_name": receiver_name if direction == "debit" else sender_name,
        "counterparty_country": receiver_country if direction == "debit" else sender_country,
        "gt_scenario_id": scenario_id,
        "gt_typology": typology,
    }


# ---------------------------------------------------------------------------
# 1. Structuring: several transactions kept just under the reporting
#    threshold, clustered in a short window.
# ---------------------------------------------------------------------------
def inject_structuring(customer, rng, fake, scenario_id, window_start, window_end, id_gen):
    rows = []
    n_txns = int(rng.integers(config.STRUCTURING_MIN_TXN_COUNT, config.STRUCTURING_MIN_TXN_COUNT + 4))
    window_days = min(config.STRUCTURING_WINDOW_DAYS, max(1, (window_end - window_start).days))
    burst_start = window_start + pd.Timedelta(days=int(rng.integers(0, max(1, window_days))))

    use_cash = rng.random() < 0.65
    for _ in range(n_txns):
        amount = rng.uniform(config.CTR_THRESHOLD * config.STRUCTURING_LOWER_RATIO,
                              config.CTR_THRESHOLD * 0.99)
        txn_date = burst_start + pd.Timedelta(
            days=float(rng.uniform(0, config.STRUCTURING_WINDOW_DAYS)),
            hours=float(rng.uniform(0, 24)),
        )
        if use_cash:
            sender_id, sender_name = f"CASH-{customer['customer_id']}", "Cash Deposit"
            txn_type = "Cash Deposit"
        else:
            sender_id, sender_name = _external_id(rng), fake.name()
            txn_type = "Wire Transfer"

        rows.append(_base_row(
            rng, id_gen(), txn_date, amount, "USD", "credit", txn_type,
            sender_id, sender_name, customer["home_country"],
            customer["customer_id"], customer["customer_name"], customer["home_country"],
            scenario_id, "structuring",
        ))
    return rows


# ---------------------------------------------------------------------------
# 2. Layering: money hops rapidly through a chain of accounts.
# ---------------------------------------------------------------------------
def inject_layering(chain_customers, rng, fake, scenario_id, window_start, window_end, id_gen):
    rows = []
    amount = float(rng.uniform(8_000, 60_000))
    hop_time = window_start + pd.Timedelta(
        days=float(rng.uniform(0, max(1.0, (window_end - window_start).days - 2)))
    )

    for i in range(len(chain_customers) - 1):
        sender = chain_customers[i]
        receiver = chain_customers[i + 1]
        txn_type = "Wire Transfer" if rng.random() < 0.8 else "ACH Transfer"

        rows.append(_base_row(
            rng, id_gen(), hop_time, amount, "USD", "debit", txn_type,
            sender["customer_id"], sender["customer_name"], sender["home_country"],
            receiver["customer_id"], receiver["customer_name"], receiver["home_country"],
            scenario_id, "layering",
        ))

        # Money shrinks a little each hop (fees / partial cash-out) and
        # moves on again soon after - the defining "rapid, sequential" trait.
        amount *= (1 - rng.uniform(0, config.LAYERING_AMOUNT_TOLERANCE))
        hop_time = hop_time + pd.Timedelta(hours=float(rng.uniform(1, config.LAYERING_MAX_HOP_HOURS)))

    return rows


# ---------------------------------------------------------------------------
# 3. Velocity: a sudden burst of activity far above the customer's own norm.
# ---------------------------------------------------------------------------
def inject_velocity(customer, rng, fake, scenario_id, window_start, window_end, id_gen):
    rows = []
    normal_daily_rate = max(0.2, customer["expected_monthly_txn_count"] / 30)
    multiplier = rng.uniform(8, 15)
    n_txns = max(6, int(normal_daily_rate * config.VELOCITY_RECENT_WINDOW_DAYS * multiplier))

    avg_amount = customer["expected_monthly_volume"] / max(1, customer["expected_monthly_txn_count"])
    burst_start = window_start

    for _ in range(n_txns):
        txn_date = burst_start + pd.Timedelta(
            days=float(rng.uniform(0, config.VELOCITY_RECENT_WINDOW_DAYS)),
            hours=float(rng.uniform(0, 24)),
        )
        amount = float(rng.lognormal(mean=np.log(max(avg_amount, 50)), sigma=0.5))
        direction = "credit" if rng.random() < 0.5 else "debit"
        txn_type = rng.choice(["Wire Transfer", "ACH Transfer"])
        cp_id, cp_name = _external_id(rng), fake.company()

        if direction == "credit":
            sender_id, sender_name, sender_country = cp_id, cp_name, customer["home_country"]
            receiver_id, receiver_name, receiver_country = (
                customer["customer_id"], customer["customer_name"], customer["home_country"]
            )
        else:
            sender_id, sender_name, sender_country = (
                customer["customer_id"], customer["customer_name"], customer["home_country"]
            )
            receiver_id, receiver_name, receiver_country = cp_id, cp_name, customer["home_country"]

        rows.append(_base_row(
            rng, id_gen(), txn_date, amount, "USD", direction, txn_type,
            sender_id, sender_name, sender_country,
            receiver_id, receiver_name, receiver_country,
            scenario_id, "velocity",
        ))
    return rows


# ---------------------------------------------------------------------------
# 4. High-risk jurisdiction exposure: cross-border activity that doesn't
#    match the customer's declared profile.
# ---------------------------------------------------------------------------
def inject_high_risk_jurisdiction(customer, rng, fake, scenario_id, window_start, window_end, id_gen):
    rows = []
    n_txns = int(rng.integers(config.HIGH_RISK_MIN_TXN_COUNT + 1, config.HIGH_RISK_MIN_TXN_COUNT + 4))
    window_days = max(1, min(config.HIGH_RISK_WINDOW_DAYS, (window_end - window_start).days))
    burst_start = window_start + pd.Timedelta(days=int(rng.integers(0, window_days)))

    for _ in range(n_txns):
        risky_country = rng.choice(config.HIGH_RISK_COUNTRIES)
        amount = float(rng.uniform(3_000, 40_000))
        txn_date = burst_start + pd.Timedelta(
            days=float(rng.uniform(0, config.HIGH_RISK_WINDOW_DAYS)),
            hours=float(rng.uniform(0, 24)),
        )
        direction = "credit" if rng.random() < 0.5 else "debit"
        cp_id, cp_name = _external_id(rng), fake.company()

        if direction == "credit":
            sender_id, sender_name, sender_country = cp_id, cp_name, risky_country
            receiver_id, receiver_name, receiver_country = (
                customer["customer_id"], customer["customer_name"], customer["home_country"]
            )
        else:
            sender_id, sender_name, sender_country = (
                customer["customer_id"], customer["customer_name"], customer["home_country"]
            )
            receiver_id, receiver_name, receiver_country = cp_id, cp_name, risky_country

        rows.append(_base_row(
            rng, id_gen(), txn_date, amount, "USD", direction, "Wire Transfer",
            sender_id, sender_name, sender_country,
            receiver_id, receiver_name, receiver_country,
            scenario_id, "high_risk_jurisdiction",
        ))
    return rows


# ---------------------------------------------------------------------------
# 5. Round-tripping: funds leave and come back to related accounts shortly
#    after, often through a related or intermediary counterparty.
# ---------------------------------------------------------------------------
def inject_round_tripping(customer, counterparty, rng, fake, scenario_id, window_start, window_end, id_gen,
                           intermediary=None):
    rows = []
    amount = float(rng.uniform(5_000, 80_000))
    out_date = window_start + pd.Timedelta(
        days=float(rng.uniform(0, max(1.0, (window_end - window_start).days - config.ROUND_TRIP_WINDOW_DAYS)))
    )
    return_gap = rng.uniform(1, config.ROUND_TRIP_WINDOW_DAYS)

    cp_id = counterparty["customer_id"] if counterparty is not None else _external_id(rng)
    cp_name = counterparty["customer_name"] if counterparty is not None else fake.company()
    cp_country = counterparty["home_country"] if counterparty is not None else customer["home_country"]

    if intermediary is None:
        # Direct round trip: customer -> counterparty -> back to customer
        rows.append(_base_row(
            rng, id_gen(), out_date, amount, "USD", "debit", "Wire Transfer",
            customer["customer_id"], customer["customer_name"], customer["home_country"],
            cp_id, cp_name, cp_country,
            scenario_id, "round_tripping",
        ))
        return_amount = amount * (1 + rng.uniform(-config.ROUND_TRIP_AMOUNT_TOLERANCE,
                                                    config.ROUND_TRIP_AMOUNT_TOLERANCE))
        rows.append(_base_row(
            rng, id_gen(), out_date + pd.Timedelta(days=return_gap), return_amount, "USD", "credit", "Wire Transfer",
            cp_id, cp_name, cp_country,
            customer["customer_id"], customer["customer_name"], customer["home_country"],
            scenario_id, "round_tripping",
        ))
    else:
        # Layered round trip: customer -> counterparty -> intermediary -> back to customer
        mid_date = out_date + pd.Timedelta(hours=float(rng.uniform(2, 48)))
        rows.append(_base_row(
            rng, id_gen(), out_date, amount, "USD", "debit", "Wire Transfer",
            customer["customer_id"], customer["customer_name"], customer["home_country"],
            cp_id, cp_name, cp_country,
            scenario_id, "round_tripping",
        ))
        amount2 = amount * (1 - rng.uniform(0, 0.08))
        rows.append(_base_row(
            rng, id_gen(), mid_date, amount2, "USD", "debit", "Wire Transfer",
            cp_id, cp_name, cp_country,
            intermediary["customer_id"], intermediary["customer_name"], intermediary["home_country"],
            scenario_id, "round_tripping",
        ))
        return_amount = amount2 * (1 + rng.uniform(-config.ROUND_TRIP_AMOUNT_TOLERANCE,
                                                     config.ROUND_TRIP_AMOUNT_TOLERANCE))
        rows.append(_base_row(
            rng, id_gen(), out_date + pd.Timedelta(days=return_gap), return_amount, "USD", "credit", "Wire Transfer",
            intermediary["customer_id"], intermediary["customer_name"], intermediary["home_country"],
            customer["customer_id"], customer["customer_name"], customer["home_country"],
            scenario_id, "round_tripping",
        ))
    return rows


# ---------------------------------------------------------------------------
# Orchestrator: injects all five typologies across the customer base.
# ---------------------------------------------------------------------------
def inject_all_typologies(customers_df: pd.DataFrame,
                           start_date: pd.Timestamp,
                           end_date: pd.Timestamp,
                           scenarios_per_typology: int = 35,
                           n_compound_customers: int = 12,
                           seed: int = config.RANDOM_SEED):
    """
    Injects suspicious scenarios into the timeline and returns:
      - a list of injected transaction row dicts (to be appended to the
        normal transactions DataFrame)
      - a ground_truth DataFrame summarizing every scenario, one row per
        scenario_id, for later detector-performance evaluation.

    `n_compound_customers` reserves a handful of customers who get TWO
    typologies injected on purpose (e.g. layering + high-risk jurisdiction).
    Real investigations often involve exactly this: one bad actor tripping
    multiple independent red flags. It also gives the scoring engine
    something meaningful to demonstrate (a compound case should score
    higher than a single-typology case).
    """
    Faker.seed(seed + 1)
    fake = Faker()
    rng = np.random.default_rng(seed + 1)

    scenario_counter = 0
    txn_counter = 0

    def next_scenario_id():
        nonlocal scenario_counter
        scenario_counter += 1
        return f"SCEN{scenario_counter:05d}"

    def next_txn_id():
        nonlocal txn_counter
        txn_counter += 1
        return f"INJ{txn_counter:06d}"

    # Reserve a "safe margin" so injected windows have baseline history
    # before them (velocity needs a real baseline to compare against) and
    # don't run past the end of the simulated period.
    safe_start = start_date + pd.Timedelta(days=config.VELOCITY_BASELINE_DAYS + 5)
    safe_end = end_date - pd.Timedelta(days=5)
    if safe_start >= safe_end:
        safe_start = start_date + pd.Timedelta(days=2)

    all_ids = customers_df["customer_id"].tolist()
    used_customers = set()

    def draw_customers(n, exclude=None):
        exclude = exclude or set()
        pool = [c for c in all_ids if c not in exclude]
        chosen = rng.choice(pool, size=min(n, len(pool)), replace=False)
        return list(chosen)

    all_rows = []
    gt_summary = []

    def record_scenario(scenario_id, typology, primary_id, involved_ids, rows):
        all_rows.extend(rows)
        dates = [r["date"] for r in rows]
        gt_summary.append({
            "scenario_id": scenario_id,
            "typology": typology,
            "primary_customer_id": primary_id,
            "involved_customer_ids": ";".join(sorted(set(involved_ids))),
            "num_transactions": len(rows),
            "total_amount_usd": round(sum(r["amount_usd"] for r in rows), 2),
            "start_date": min(dates),
            "end_date": max(dates),
        })

    # --- 1. Structuring -----------------------------------------------
    for cust_id in draw_customers(scenarios_per_typology, used_customers):
        used_customers.add(cust_id)
        customer = customers_df.loc[customers_df["customer_id"] == cust_id].iloc[0]
        sid = next_scenario_id()
        rows = inject_structuring(customer, rng, fake, sid, safe_start, safe_end, next_txn_id)
        record_scenario(sid, "structuring", cust_id, [cust_id], rows)

    # --- 2. Layering -----------------------------------------------
    # config.LAYERING_MIN_CHAIN_LENGTH is a number of HOPS (edges), so we
    # need one more customer (node) than that to guarantee the detector's
    # minimum-hop threshold can actually be met.
    for _ in range(scenarios_per_typology):
        n_hops = int(rng.integers(config.LAYERING_MIN_CHAIN_LENGTH, config.LAYERING_MIN_CHAIN_LENGTH + 3))
        chain_ids = draw_customers(n_hops + 1, used_customers)
        if len(chain_ids) < 2:
            continue
        used_customers.update(chain_ids)
        chain_customers = [customers_df.loc[customers_df["customer_id"] == cid].iloc[0] for cid in chain_ids]
        sid = next_scenario_id()
        rows = inject_layering(chain_customers, rng, fake, sid, safe_start, safe_end, next_txn_id)
        record_scenario(sid, "layering", chain_ids[0], chain_ids, rows)

    # --- 3. Velocity -----------------------------------------------
    low_medium = customers_df[customers_df["expected_activity_level"].isin(["Low", "Medium"])]
    candidates = [c for c in low_medium["customer_id"].tolist() if c not in used_customers]
    rng.shuffle(candidates)
    for cust_id in candidates[:scenarios_per_typology]:
        used_customers.add(cust_id)
        customer = customers_df.loc[customers_df["customer_id"] == cust_id].iloc[0]
        sid = next_scenario_id()
        rows = inject_velocity(customer, rng, fake, sid, safe_start, safe_end, next_txn_id)
        record_scenario(sid, "velocity", cust_id, [cust_id], rows)

    # --- 4. High-risk jurisdiction -----------------------------------------------
    eligible = customers_df[
        (~customers_df["home_country"].isin(config.HIGH_RISK_COUNTRIES))
        & (~customers_df["business_type"].isin(["Import/Export Trading", "Money Service Business",
                                                  "Cryptocurrency Exchange"]))
    ]
    candidates = [c for c in eligible["customer_id"].tolist() if c not in used_customers]
    rng.shuffle(candidates)
    for cust_id in candidates[:scenarios_per_typology]:
        used_customers.add(cust_id)
        customer = customers_df.loc[customers_df["customer_id"] == cust_id].iloc[0]
        sid = next_scenario_id()
        rows = inject_high_risk_jurisdiction(customer, rng, fake, sid, safe_start, safe_end, next_txn_id)
        record_scenario(sid, "high_risk_jurisdiction", cust_id, [cust_id], rows)

    # --- 5. Round-tripping -----------------------------------------------
    for _ in range(scenarios_per_typology):
        picked = draw_customers(2, used_customers)
        if len(picked) < 2:
            continue
        used_customers.update(picked)
        customer = customers_df.loc[customers_df["customer_id"] == picked[0]].iloc[0]
        counterparty = customers_df.loc[customers_df["customer_id"] == picked[1]].iloc[0]
        intermediary = None
        involved = [picked[0], picked[1]]
        if rng.random() < 0.3:
            extra = draw_customers(1, used_customers)
            if extra:
                used_customers.add(extra[0])
                intermediary = customers_df.loc[customers_df["customer_id"] == extra[0]].iloc[0]
                involved.append(extra[0])
        sid = next_scenario_id()
        rows = inject_round_tripping(customer, counterparty, rng, fake, sid, safe_start, safe_end,
                                      next_txn_id, intermediary=intermediary)
        record_scenario(sid, "round_tripping", picked[0], involved, rows)

    # --- Compound cases: a handful of customers get a SECOND typology -----
    # sorted(), not list(): Python randomizes string hashing per-process
    # (PYTHONHASHSEED), so a plain set's iteration order is NOT
    # reproducible across runs even with every RNG seeded identically.
    # Sorting first means rng.shuffle() below always starts from the same
    # order, which is what actually makes RANDOM_SEED reproducible here.
    compound_candidates = sorted(used_customers)
    rng.shuffle(compound_candidates)
    typology_injectors = {
        "structuring": lambda c, sid: inject_structuring(c, rng, fake, sid, safe_start, safe_end, next_txn_id),
        "velocity": lambda c, sid: inject_velocity(c, rng, fake, sid, safe_start, safe_end, next_txn_id),
        "high_risk_jurisdiction": lambda c, sid: inject_high_risk_jurisdiction(
            c, rng, fake, sid, safe_start, safe_end, next_txn_id),
    }
    for cust_id in compound_candidates[:n_compound_customers]:
        customer = customers_df.loc[customers_df["customer_id"] == cust_id].iloc[0]
        typ = rng.choice(list(typology_injectors.keys()))
        sid = next_scenario_id()
        rows = typology_injectors[typ](customer, sid)
        record_scenario(sid, typ, cust_id, [cust_id], rows)

    ground_truth_df = pd.DataFrame(gt_summary)
    return all_rows, ground_truth_df
