"""
config.py
=========
Central configuration for the whole platform.

Why this file exists: in a real transaction-monitoring system, every rule has
"tuning parameters" (thresholds, lookback windows, weights) that compliance
and analytics teams review periodically. Keeping them all in one place -
instead of scattered inside detector code - means:
  1. You can explain your assumptions in one glance (useful in interviews).
  2. Tuning a rule means changing a number here, not hunting through logic.
  3. It mirrors how real TM systems (e.g. Actimize, SAS AML) separate
     "rule parameters" from "rule engine code".

Every threshold below is a reasonable, illustrative choice loosely inspired
by real-world AML practice (e.g. the $10,000 USD Currency Transaction Report
threshold used in the US Bank Secrecy Act). They are NOT regulatory advice
and the country risk list is NOT an authoritative sanctions/FATF list -
see README "Limitations" section.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Reproducibility & scale
# ---------------------------------------------------------------------------
RANDOM_SEED = 42          # fixed seed so the "random" data is reproducible
NUM_CUSTOMERS = 5000      # "several thousand" customers as requested
HISTORY_MONTHS = 6        # months of transaction history to simulate
SIMULATION_END_DATE = None  # None = use today's date as the end of history

# ---------------------------------------------------------------------------
# File locations
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"

CUSTOMERS_FILE = DATA_DIR / "customers.csv"
TRANSACTIONS_FILE = DATA_DIR / "transactions.csv"
GROUND_TRUTH_FILE = DATA_DIR / "ground_truth_scenarios.csv"
ALERTS_FILE = OUTPUT_DIR / "alerts.csv"
CASE_DB_FILE = OUTPUT_DIR / "cases.db"

# ---------------------------------------------------------------------------
# Illustrative country risk list
# ---------------------------------------------------------------------------
# In a real institution this would come from a licensed data feed (Dow Jones,
# Refinitiv World-Check, the FATF public statements, OFAC lists, etc.) and
# would be refreshed regularly. Here it's a simplified, hard-coded list used
# purely so the simulation has *something* to react to.
HIGH_RISK_COUNTRIES = [
    "IR", "KP", "SY", "MM", "AF", "YE", "SS", "VE", "CU", "LY",
]
MEDIUM_RISK_COUNTRIES = [
    "PK", "NG", "PH", "TR", "UA", "RU", "PA", "AE", "LB", "KH",
]
HOME_COUNTRY = "US"  # the bank's "home" jurisdiction for this simulation

ALL_COUNTRIES = [HOME_COUNTRY] + [
    "GB", "CA", "DE", "FR", "AU", "JP", "SG", "CH", "NL", "MX",
    "BR", "IN", "ZA", "IT", "ES", "SE", "KR", "HK",
] + MEDIUM_RISK_COUNTRIES + HIGH_RISK_COUNTRIES

# ---------------------------------------------------------------------------
# Structuring detector
# ---------------------------------------------------------------------------
# Classic pattern: multiple deposits/transfers each kept just under a
# reporting threshold (in the US, cash transactions over $10,000 trigger a
# Currency Transaction Report). We flag customers who cluster several
# transactions just below that line within a short window.
CTR_THRESHOLD = 10_000.0
STRUCTURING_LOWER_RATIO = 0.90   # "just under" = between 90% and 100% of threshold
STRUCTURING_WINDOW_DAYS = 5
STRUCTURING_MIN_TXN_COUNT = 3    # need at least this many "near-threshold" txns

# ---------------------------------------------------------------------------
# Layering detector
# ---------------------------------------------------------------------------
# Classic pattern: money hops quickly through a chain of accounts to obscure
# its origin. We look for chains of outgoing -> incoming transfers where each
# hop happens soon after the last and the amount is roughly preserved.
LAYERING_MAX_HOP_HOURS = 72       # max time between one hop and the next
LAYERING_MIN_CHAIN_LENGTH = 3     # minimum number of hops to be a "chain"
LAYERING_AMOUNT_TOLERANCE = 0.15  # amount can shrink up to 15% per hop (fees)

# ---------------------------------------------------------------------------
# Velocity / rapid fund movement detector
# ---------------------------------------------------------------------------
# Compares a customer's recent activity to their own historical baseline
# using a z-score (how many standard deviations above their normal pattern).
VELOCITY_BASELINE_DAYS = 90         # history used to establish "normal"
VELOCITY_RECENT_WINDOW_DAYS = 7     # recent window checked for a spike
VELOCITY_ZSCORE_THRESHOLD = 4.0     # flag if recent activity > mean + 4*std
VELOCITY_MIN_TXN_FOR_BASELINE = 6   # need enough history to trust the baseline
VELOCITY_MIN_RECENT_TXN_COUNT = 4   # absolute floor - ignore spikes built from 1-2 transactions
VELOCITY_MIN_BASELINE_DAILY_USD = 25.0  # below this, a customer's $ baseline is too thin to trust
VELOCITY_MIN_MULTIPLIER = 4.0       # recent count must also be >= this many times their own normal rate

# ---------------------------------------------------------------------------
# High-risk jurisdiction detector
# ---------------------------------------------------------------------------
HIGH_RISK_WINDOW_DAYS = 30
HIGH_RISK_MIN_TXN_COUNT = 2         # >=2 txns to/from high-risk countries...
HIGH_RISK_MIN_CUMULATIVE_AMOUNT = 5_000.0  # ...or cumulative amount over this

# ---------------------------------------------------------------------------
# Round-tripping detector
# ---------------------------------------------------------------------------
ROUND_TRIP_WINDOW_DAYS = 14
ROUND_TRIP_AMOUNT_TOLERANCE = 0.10   # returned amount within +/-10% of the sent amount

# ---------------------------------------------------------------------------
# Risk scoring engine
# ---------------------------------------------------------------------------
# Base points contributed by each typology if triggered. These reflect a
# rough view of "how serious is this pattern on its own" - layering and
# structuring are classic hard "red flags", velocity spikes are noisier
# (more false positives), so they carry less weight alone.
TYPOLOGY_WEIGHTS = {
    "structuring": 30,
    "layering": 35,
    "velocity": 15,
    "high_risk_jurisdiction": 20,
    "round_tripping": 25,
}

# Extra points added per *additional* distinct typology triggered on the same
# customer in the same review period - multiple independent red flags firing
# together is more suspicious than the sum of its parts.
MULTI_TYPOLOGY_BONUS_PER_EXTRA = 8
MULTI_TYPOLOGY_BONUS_CAP = 24

# If a customer's declared KYC risk rating is High, amplify the final score
# slightly - a High-risk customer tripping a rule is more concerning than a
# Low-risk one doing the exact same thing... but low-risk customers tripping
# rules unexpectedly also matters, which the detectors already account for
# by comparing against *that customer's own* declared/expected profile.
RISK_RATING_MULTIPLIER = {
    "Low": 1.0,
    "Medium": 1.05,
    "High": 1.15,
}

# Final score (0-100) is bucketed into a priority band for the alert queue.
PRIORITY_BANDS = [
    (85, "Critical"),
    (65, "High"),
    (40, "Medium"),
    (0, "Low"),
]

# ---------------------------------------------------------------------------
# Case management
# ---------------------------------------------------------------------------
ALERT_STATUSES = ["New", "Under Review", "Escalated", "Closed"]
