"""
customers.py
============
Generates the synthetic customer base: the "who" of the simulation.

Each row is one customer with the kind of profile a bank would capture at
account opening (KYC - Know Your Customer): what type of customer they are,
what they do for a living / what business they run, where they're based,
how the bank rated their risk, and how much activity is "expected" for them.

That "expected activity" field matters a lot later on: several of our
detectors (velocity, high-risk jurisdiction) work by comparing what a
customer *actually* does against what their own profile says is normal for
them - which is exactly how real transaction monitoring reasons about
"unusual" activity. A $50,000 wire is nothing unusual for an import/export
trading company; it would be very unusual for a retired individual.
"""

import numpy as np
import pandas as pd
from faker import Faker

import config

# ---------------------------------------------------------------------------
# Reference lists used to build realistic, varied profiles
# ---------------------------------------------------------------------------

# Business types split into "higher inherent risk" and "lower inherent risk"
# buckets. This isn't a judgement that every business of this type is
# suspicious - it reflects the fact that some sectors (cash-intensive,
# cross-border trade, money transmission) are simply harder to monitor and
# get extra KYC attention in real banks.
HIGH_RISK_BUSINESS_TYPES = [
    "Money Service Business",
    "Casino / Gaming",
    "Precious Metals Dealer",
    "Cryptocurrency Exchange",
    "Import/Export Trading",
    "Cash-Intensive Retail (Convenience Store)",
    "Cash-Intensive Retail (Restaurant)",
    "Cash-Intensive Retail (Car Wash)",
    "Charity / NPO",
]
LOW_RISK_BUSINESS_TYPES = [
    "Consulting Services",
    "Technology / SaaS",
    "Professional Services (Legal/Accounting)",
    "Manufacturing",
    "Healthcare Services",
    "Education Services",
    "Real Estate Agency",
    "Retail - E-commerce",
    "Construction",
]
ALL_BUSINESS_TYPES = HIGH_RISK_BUSINESS_TYPES + LOW_RISK_BUSINESS_TYPES

INDIVIDUAL_OCCUPATIONS = [
    "Salaried Employee", "Self-Employed", "Retired", "Student",
    "Healthcare Worker", "Educator", "Freelancer / Gig Worker",
    "Government Employee", "Business Owner",
]

ACTIVITY_LEVELS = ["Low", "Medium", "High"]


def _assign_risk_rating(rng, business_type, home_country, is_pep):
    """
    Compute a declared KYC risk rating (Low/Medium/High) for a customer.

    Real risk ratings combine several inputs (business type, geography,
    PEP status, product usage) into a score, then add a band. We do the
    same at a simplified level. We also add some randomness on top of the
    "objective" score, because in practice onboarding risk ratings involve
    analyst judgement and aren't perfectly deterministic.
    """
    points = 0
    if business_type in HIGH_RISK_BUSINESS_TYPES:
        points += 2
    if home_country in config.HIGH_RISK_COUNTRIES:
        points += 3
    elif home_country in config.MEDIUM_RISK_COUNTRIES:
        points += 1
    if is_pep:
        points += 2

    # Map the point total to a probability distribution over ratings, then
    # sample - so most customers land where you'd expect, but not all of them.
    if points == 0:
        probs = {"Low": 0.85, "Medium": 0.14, "High": 0.01}
    elif points <= 2:
        probs = {"Low": 0.35, "Medium": 0.55, "High": 0.10}
    else:
        probs = {"Low": 0.05, "Medium": 0.35, "High": 0.60}

    ratings, weights = zip(*probs.items())
    return rng.choice(ratings, p=weights)


def _assign_expected_activity(rng, customer_type, business_type):
    """
    Decide how much monthly $ volume and how many transactions/month are
    "expected" (normal) for this customer, based on who they are.

    We use a lognormal distribution because transaction volumes in the real
    world are heavily right-skewed - most customers are modest, a few are
    very large - which a lognormal captures much better than a normal
    (bell-curve) distribution would.
    """
    if customer_type == "Individual":
        activity_level = rng.choice(ACTIVITY_LEVELS, p=[0.55, 0.35, 0.10])
        base_volume = {"Low": 1_500, "Medium": 6_000, "High": 20_000}[activity_level]
        base_txn_count = {"Low": 6, "Medium": 14, "High": 30}[activity_level]
    else:
        # Businesses run bigger numbers, and high-risk business types tend
        # to run higher cash/transfer volumes (e.g. an MSB moves a lot of
        # money by definition).
        activity_level = rng.choice(ACTIVITY_LEVELS, p=[0.30, 0.45, 0.25])
        multiplier = 3.0 if business_type in HIGH_RISK_BUSINESS_TYPES else 1.0
        base_volume = {"Low": 15_000, "Medium": 60_000, "High": 250_000}[activity_level] * multiplier
        base_txn_count = {"Low": 10, "Medium": 25, "High": 60}[activity_level]

    # Lognormal noise around the base value (sigma=0.35 keeps most values
    # within roughly +/-40% of the base, with an occasional larger outlier).
    expected_volume = float(rng.lognormal(mean=np.log(base_volume), sigma=0.35))
    expected_txn_count = max(1, int(rng.lognormal(mean=np.log(base_txn_count), sigma=0.30)))

    return activity_level, round(expected_volume, 2), expected_txn_count


def generate_customers(n_customers: int = config.NUM_CUSTOMERS,
                        seed: int = config.RANDOM_SEED) -> pd.DataFrame:
    """
    Build the full synthetic customer base as a pandas DataFrame.

    Parameters
    ----------
    n_customers : how many customers to generate
    seed        : random seed, so the same call always produces the same data

    Returns
    -------
    pd.DataFrame with one row per customer.
    """
    Faker.seed(seed)
    fake = Faker()
    rng = np.random.default_rng(seed)

    # Country weighting: mostly home country, a modest spread of other
    # "normal" countries, and a small tail of medium/high-risk countries -
    # a bank's customer base is not evenly spread across every geography.
    country_pool = (
        [config.HOME_COUNTRY] * 60
        + [c for c in config.ALL_COUNTRIES if c not in config.HIGH_RISK_COUNTRIES
           and c not in config.MEDIUM_RISK_COUNTRIES and c != config.HOME_COUNTRY] * 3
        + config.MEDIUM_RISK_COUNTRIES * 2
        + config.HIGH_RISK_COUNTRIES * 1
    )

    rows = []
    onboarding_start = pd.Timestamp.today().normalize() - pd.Timedelta(days=5 * 365)
    onboarding_end = pd.Timestamp.today().normalize() - pd.Timedelta(days=config.HISTORY_MONTHS * 30)

    for i in range(n_customers):
        customer_id = f"CUST{i+1:05d}"
        customer_type = rng.choice(["Individual", "Business"], p=[0.55, 0.45])
        home_country = rng.choice(country_pool)

        # PEP = Politically Exposed Person. Small, realistic base rate.
        is_pep = bool(rng.random() < 0.015)

        if customer_type == "Individual":
            name = fake.name()
            business_type = None
            occupation = rng.choice(INDIVIDUAL_OCCUPATIONS)
        else:
            name = fake.company()
            business_type = rng.choice(ALL_BUSINESS_TYPES)
            occupation = None

        risk_rating = _assign_risk_rating(rng, business_type, home_country, is_pep)
        activity_level, expected_volume, expected_txn_count = _assign_expected_activity(
            rng, customer_type, business_type
        )

        onboarding_date = fake.date_between_dates(
            date_start=onboarding_start.date(), date_end=onboarding_end.date()
        )

        rows.append({
            "customer_id": customer_id,
            "customer_name": name,
            "customer_type": customer_type,
            "business_type": business_type,
            "occupation": occupation,
            "home_country": home_country,
            "is_pep": is_pep,
            "risk_rating": risk_rating,
            "expected_activity_level": activity_level,
            "expected_monthly_volume": expected_volume,
            "expected_monthly_txn_count": expected_txn_count,
            "onboarding_date": onboarding_date,
        })

    df = pd.DataFrame(rows)
    return df


if __name__ == "__main__":
    # Quick manual check: generate a small sample and print a preview.
    sample = generate_customers(n_customers=10)
    print(sample.head(10).to_string())
