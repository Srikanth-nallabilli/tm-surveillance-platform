"""
run_pipeline.py
=================
The single entry point that builds the entire dataset and alert queue from
scratch. Run this once (it takes a few minutes for the full-size dataset)
and then explore the results with the Streamlit dashboard:

    python run_pipeline.py
    streamlit run src/dashboard/app.py

What this script does, in order:
  1. Generate synthetic customers (customers.py)
  2. Generate normal transaction history for them (transactions.py)
  3. Inject deliberately suspicious scenarios with a hidden ground-truth
     label (typologies.py)
  4. Run all five rule-based detectors against the transaction data alone
     - the detectors never see the ground-truth labels (run_detectors.py)
  5. Combine detector flags into one weighted priority score per customer
     (risk_scoring.py)
  6. Load the scored cases into the SQLite case management database, ready
     for the dashboard's alert queue (alert_store.py)
  7. Print a plain-language summary, including how well the detectors
     recovered the known-injected scenarios - a sanity check that the
     rules are actually working, not just running.

Every number used below (customer count, history length, thresholds) comes
from config.py - change it there, not here.
"""

import sys
import time
from pathlib import Path

import pandas as pd

import config

# Make every module in src/<subpackage> importable with simple flat
# imports (e.g. `import customers`) instead of Python package plumbing -
# keeps each module readable on its own for anyone new to the codebase.
for sub in ["data_generation", "detection", "scoring", "case_management", "network"]:
    sys.path.insert(0, str(config.BASE_DIR / "src" / sub))

from customers import generate_customers                       # noqa: E402
from transactions import generate_transactions                 # noqa: E402
from typologies import inject_all_typologies                   # noqa: E402
from run_detectors import run_all_detectors                    # noqa: E402
from risk_scoring import score_alerts                          # noqa: E402
import alert_store                                              # noqa: E402


def step(msg):
    print(f"\n{'=' * 70}\n{msg}\n{'=' * 70}")


def main():
    t0 = time.time()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # --- 1. Customers -----------------------------------------------
    step(f"STEP 1/6 - Generating {config.NUM_CUSTOMERS:,} synthetic customers")
    customers_df = generate_customers(n_customers=config.NUM_CUSTOMERS, seed=config.RANDOM_SEED)
    customers_df.to_csv(config.CUSTOMERS_FILE, index=False)
    print(f"  Saved {len(customers_df):,} customers -> {config.CUSTOMERS_FILE}")

    # --- 2. Normal transaction history -----------------------------------------------
    step(f"STEP 2/6 - Generating {config.HISTORY_MONTHS} months of normal transaction history")
    txns_df = generate_transactions(customers_df, months=config.HISTORY_MONTHS, seed=config.RANDOM_SEED)
    print(f"  Generated {len(txns_df):,} organic transactions")

    # --- 3. Inject suspicious scenarios -----------------------------------------------
    step("STEP 3/6 - Injecting suspicious typology scenarios (with hidden ground truth)")
    start_date, end_date = txns_df["date"].min(), txns_df["date"].max()
    scenarios_per_typology = max(10, config.NUM_CUSTOMERS // 140)
    n_compound = max(4, config.NUM_CUSTOMERS // 400)
    injected_rows, ground_truth_df = inject_all_typologies(
        customers_df, start_date, end_date,
        scenarios_per_typology=scenarios_per_typology,
        n_compound_customers=n_compound,
        seed=config.RANDOM_SEED,
    )
    injected_df = pd.DataFrame(injected_rows)
    print(f"  Injected {len(injected_df):,} transactions across {len(ground_truth_df):,} scenarios:")
    print(f"  {ground_truth_df['typology'].value_counts().to_string()}")

    full_txns = pd.concat([txns_df, injected_df], ignore_index=True, sort=False)
    # Injected timestamps come from float-based pd.Timedelta arithmetic
    # (e.g. "wait a random number of hours"), which produces nanosecond-
    # precision values. Real core banking systems don't record that kind
    # of precision, and worse, mixing "clean minute" organic timestamps
    # with nanosecond-precision injected ones in the same CSV column makes
    # pandas' automatic date parsing unreliable when the file is re-read
    # (it silently falls back to treating the whole column as text).
    # Rounding to the nearest second keeps the data realistic and keeps
    # every downstream CSV read simple and correct.
    full_txns["date"] = full_txns["date"].dt.round("s")
    full_txns = full_txns.sort_values("date").reset_index(drop=True)
    full_txns["transaction_id"] = [f"TXN{i+1:08d}" for i in range(len(full_txns))]

    # The ground truth (gt_scenario_id / gt_typology) columns exist ONLY so
    # we can grade detector performance below. They are saved separately
    # and stripped out of the "clean" transactions file that the detectors
    # and dashboard actually read - exactly like a bank's analysts would
    # never have an answer key, just raw transaction data.
    ground_truth_scenarios_path = config.GROUND_TRUTH_FILE
    ground_truth_df.to_csv(ground_truth_scenarios_path, index=False)

    gt_labels = full_txns[["transaction_id", "gt_scenario_id", "gt_typology"]].copy()
    clean_txns = full_txns.drop(columns=["gt_scenario_id", "gt_typology"])
    clean_txns.to_csv(config.TRANSACTIONS_FILE, index=False)
    print(f"  Saved {len(clean_txns):,} total transactions -> {config.TRANSACTIONS_FILE}")

    # --- 4. Run detectors (blind to ground truth) -----------------------------------------------
    step("STEP 4/6 - Running the 5 rule-based typology detectors")
    flags_df = run_all_detectors(clean_txns, customers_df, verbose=True)
    print(f"\n  Total flags across all detectors: {len(flags_df):,}")

    # --- 5. Score into cases -----------------------------------------------
    step("STEP 5/6 - Scoring flags into prioritized cases")
    cases_df = score_alerts(flags_df, customers_df)
    print(f"  Produced {len(cases_df):,} cases")
    if not cases_df.empty:
        print(f"  Priority breakdown:\n{cases_df['priority'].value_counts().to_string()}")
    cases_df.to_csv(config.ALERTS_FILE, index=False)

    # --- 6. Load into the case management database -----------------------------------------------
    step("STEP 6/6 - Loading cases into the case management database")
    inserted = alert_store.load_cases_into_db(cases_df, db_path=config.CASE_DB_FILE)
    print(f"  Inserted {inserted:,} new cases into {config.CASE_DB_FILE}")
    print(f"  (existing cases from a previous run, if any, were left untouched)")

    # --- Detector performance vs ground truth -----------------------------------------------
    step("DETECTOR PERFORMANCE vs. INJECTED GROUND TRUTH")
    _print_detector_performance(ground_truth_df, flags_df, gt_labels)

    elapsed = time.time() - t0
    print(f"\nPipeline complete in {elapsed:.1f}s.")
    print("Next: streamlit run src/dashboard/app.py")


def _print_detector_performance(ground_truth_df, flags_df, gt_labels):
    """
    For each typology, how many of the customers we DELIBERATELY targeted
    with that scenario actually got flagged by that same detector? This is
    a customer-level recall check - a quick, honest sanity test that each
    rule is doing its job, using the answer key we built during data
    generation. It is NOT part of the detection pipeline itself.
    """
    if ground_truth_df.empty:
        print("  No ground truth scenarios to evaluate.")
        return

    gt_by_typology = ground_truth_df.groupby("typology")["primary_customer_id"].apply(set)
    flags_by_typology = (
        flags_df.groupby("typology")["customer_id"].apply(set) if not flags_df.empty else {}
    )

    rows = []
    for typology, truth_customers in gt_by_typology.items():
        caught = flags_by_typology.get(typology, set())
        hits = truth_customers & caught
        recall = len(hits) / len(truth_customers) if truth_customers else 0
        rows.append((typology, len(truth_customers), len(hits), f"{recall:.0%}"))

    summary = pd.DataFrame(rows, columns=["typology", "injected_scenarios", "caught_by_detector", "recall"])
    print(summary.to_string(index=False))
    print(
        "\n  Note: this measures RECALL only (did we catch the customers we know are\n"
        "  suspicious). It says nothing about PRECISION (what fraction of all flags\n"
        "  are real) - see README 'Limitations' for a discussion of that tradeoff."
    )


if __name__ == "__main__":
    main()
