"""
run_detectors.py
==================
Thin orchestrator that runs all five typology detectors and concatenates
their output into one standardized "flags" table. This is the single
function the rest of the pipeline (scoring, dashboard) calls - it doesn't
need to know anything about the five detectors individually.
"""

import pandas as pd

from structuring import detect_structuring
from layering import detect_layering
from velocity import detect_velocity
from high_risk_jurisdiction import detect_high_risk_jurisdiction
from round_tripping import detect_round_tripping

DETECTORS = {
    "structuring": detect_structuring,
    "layering": detect_layering,
    "velocity": detect_velocity,
    "high_risk_jurisdiction": detect_high_risk_jurisdiction,
    "round_tripping": detect_round_tripping,
}


def run_all_detectors(transactions_df: pd.DataFrame, customers_df: pd.DataFrame,
                       verbose: bool = True) -> pd.DataFrame:
    """
    Run every detector against the same transaction/customer data and
    return one combined DataFrame of flags (one row per detector hit).

    Every detector receives ONLY transactions_df and customers_df - neither
    contains the gt_scenario_id/gt_typology ground-truth columns used
    during data generation, so detection genuinely runs "blind", the same
    way it would against real bank data.
    """
    all_flags = []
    for name, detector_fn in DETECTORS.items():
        result = detector_fn(transactions_df, customers_df)
        if verbose:
            print(f"  {name}: {len(result)} flags")
        all_flags.append(result)

    combined = pd.concat(all_flags, ignore_index=True) if all_flags else pd.DataFrame()
    return combined
