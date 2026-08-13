"""
velocity.py
============
Detects VELOCITY / RAPID FUND MOVEMENT: a customer suddenly transacting far
more often, or moving far more money, than their own history says is normal
for them.

Unlike structuring or high-risk jurisdiction (which compare against a fixed
rule), velocity compares each customer against THEMSELVES using a z-score:
  z = (recent activity level - that customer's own historical average)
      / that customer's own historical standard deviation

A z-score of 3, for example, means "three standard deviations above what's
normal for this specific customer" - the same $50,000/week might be
completely unremarkable for a busy import/export business (z close to 0)
and wildly abnormal for someone who usually makes two small transactions a
month (z very high). This is exactly the kind of profile-relative reasoning
real transaction monitoring systems use instead of one-size-fits-all limits.

We compute this on a rolling basis across each customer's own history (not
just "the last N days of the whole dataset") so a spike anywhere in their
timeline gets caught, not only spikes right at the end of the data.
"""

import numpy as np
import pandas as pd

import config
from common import customer_activity_view, make_flag, flags_to_dataframe


def _daily_baseline_stats(baseline_dates_ord, baseline_amounts, start_ord, end_ord):
    """
    Bucket a customer's baseline-period transactions into daily counts and
    daily $ volumes, INCLUDING days with zero activity, then return the
    mean/std of each. Including zero-activity days is what makes a mostly-
    quiet customer's baseline std small - which is exactly what makes a
    sudden burst look statistically extreme for them.
    """
    n_days = max(1, end_ord - start_ord)
    daily_counts = np.zeros(n_days)
    daily_volume = np.zeros(n_days)
    offsets = np.clip(baseline_dates_ord - start_ord, 0, n_days - 1)
    for off, amt in zip(offsets, baseline_amounts):
        daily_counts[off] += 1
        daily_volume[off] += amt
    return daily_counts.mean(), daily_counts.std(), daily_volume.mean(), daily_volume.std()


def detect_velocity(transactions_df: pd.DataFrame, customers_df: pd.DataFrame,
                     cfg=config) -> pd.DataFrame:
    activity = customer_activity_view(transactions_df)
    flags = []

    # The dataset itself has a hard start date - no transaction, for any
    # customer, can exist before it. If a candidate window's baseline
    # period reaches earlier than that, the "missing" days would be
    # zero-filled not because the customer was quiet but because the
    # simulation simply hadn't started yet, which artificially deflates
    # the baseline and manufactures fake spikes right at the start of the
    # data. We only trust windows with a genuine, fully-populated baseline.
    dataset_min_date = transactions_df["date"].min().to_datetime64()

    for customer_id, group in activity.groupby("party_customer_id"):
        group = group.sort_values("date").reset_index(drop=True)
        n = len(group)
        if n < cfg.VELOCITY_MIN_TXN_FOR_BASELINE + 1:
            continue  # not enough history to trust a baseline at all

        dates = group["date"].values.astype("datetime64[ns]")
        amounts = group["amount_usd"].values
        txn_ids = group["transaction_id"].values
        day_ord = dates.astype("datetime64[D]").astype(int)

        candidates = []
        for i in range(n):
            d = dates[i]
            baseline_start = d - np.timedelta64(cfg.VELOCITY_BASELINE_DAYS, "D")
            if baseline_start < dataset_min_date:
                continue
            recent_end = d + np.timedelta64(cfg.VELOCITY_RECENT_WINDOW_DAYS, "D")

            b_lo = np.searchsorted(dates, baseline_start, side="left")
            b_hi = np.searchsorted(dates, d, side="left")
            r_lo = b_hi
            r_hi = np.searchsorted(dates, recent_end, side="left")

            baseline_n = b_hi - b_lo
            if baseline_n < cfg.VELOCITY_MIN_TXN_FOR_BASELINE:
                continue

            start_ord = int((baseline_start.astype("datetime64[D]")).astype(int))
            end_ord = int((d.astype("datetime64[D]")).astype(int))
            mean_c, std_c, mean_v, std_v = _daily_baseline_stats(
                day_ord[b_lo:b_hi], amounts[b_lo:b_hi], start_ord, end_ord
            )
            std_c = max(std_c, 0.5)
            std_v = max(std_v, mean_v * 0.5 + 1)

            recent_n = r_hi - r_lo
            # Two gates, both must pass: an absolute floor (a couple of
            # transactions is never "velocity", no matter how quiet the
            # customer usually is) AND a relative floor scaled to the
            # customer's OWN normal rate (a fixed count like "4" is a huge
            # jump for someone who transacts once a month, but noise for
            # someone who transacts daily - Poisson randomness alone
            # produces occasional busy weeks for low-activity customers,
            # so we require a genuine multiple of their own baseline, not
            # just a small absolute count).
            required_recent_n = max(
                cfg.VELOCITY_MIN_RECENT_TXN_COUNT,
                mean_c * cfg.VELOCITY_RECENT_WINDOW_DAYS * cfg.VELOCITY_MIN_MULTIPLIER,
            )
            if recent_n < required_recent_n:
                continue
            recent_amt = amounts[r_lo:r_hi].sum()
            recent_daily_c = recent_n / cfg.VELOCITY_RECENT_WINDOW_DAYS
            recent_daily_v = recent_amt / cfg.VELOCITY_RECENT_WINDOW_DAYS

            z_count = (recent_daily_c - mean_c) / std_c
            # A customer whose baseline $ volume is nearly zero (very sparse
            # history) makes the volume z-score wildly unstable - one
            # ordinary transaction divided by a near-zero baseline produces
            # a huge but meaningless ratio. Only trust the volume signal
            # once there's a real dollar baseline to compare against.
            if mean_v >= cfg.VELOCITY_MIN_BASELINE_DAILY_USD:
                z_volume = (recent_daily_v - mean_v) / std_v
            else:
                z_volume = z_count
            # "Unusually high frequency OR volume" (per the typology
            # definition) is an OR, not an AND - a burst of many small
            # transactions is just as much a velocity red flag as a few
            # huge ones, so we take whichever signal is stronger. The
            # earlier cold-start and thin-baseline fixes are what make it
            # safe to use max() here without reintroducing false positives.
            z = max(z_count, z_volume)

            if z >= cfg.VELOCITY_ZSCORE_THRESHOLD:
                candidates.append({
                    "window_start": d, "window_end": recent_end, "z": z,
                    "recent_n": int(recent_n), "recent_amt": float(recent_amt),
                    "baseline_mean_c": float(mean_c), "baseline_mean_v": float(mean_v),
                    "lo": r_lo, "hi": r_hi,
                })

        if not candidates:
            continue

        # A burst usually trips several consecutive transaction-anchored
        # windows in a row; merge overlapping candidates into one flag so we
        # don't spam the alert queue with near-duplicate windows.
        candidates.sort(key=lambda w: w["window_start"])
        merged = [candidates[0]]
        merged_lo_hi = [(candidates[0]["lo"], candidates[0]["hi"])]
        for c in candidates[1:]:
            if c["window_start"] <= merged[-1]["window_end"]:
                lo, hi = merged_lo_hi[-1]
                merged_lo_hi[-1] = (lo, max(hi, c["hi"]))
                if c["z"] > merged[-1]["z"]:
                    merged[-1] = c
                merged[-1]["window_end"] = max(merged[-1]["window_end"], c["window_end"])
            else:
                merged.append(c)
                merged_lo_hi.append((c["lo"], c["hi"]))

        for w, (lo, hi) in zip(merged, merged_lo_hi):
            confidence = min(1.0, w["z"] / (cfg.VELOCITY_ZSCORE_THRESHOLD * 2))
            evidence = {
                "zscore": round(float(w["z"]), 2),
                "recent_txn_count": w["recent_n"],
                "recent_total_amount_usd": round(w["recent_amt"], 2),
                "baseline_avg_daily_txn_count": round(w["baseline_mean_c"], 3),
                "baseline_avg_daily_volume_usd": round(w["baseline_mean_v"], 2),
                "recent_window_days": cfg.VELOCITY_RECENT_WINDOW_DAYS,
                "baseline_window_days": cfg.VELOCITY_BASELINE_DAYS,
            }
            flags.append(make_flag(
                customer_id=customer_id,
                typology="velocity",
                confidence=confidence,
                metric_value=w["z"],
                window_start=w["window_start"],
                window_end=w["window_end"],
                transaction_ids=list(txn_ids[lo:hi]),
                evidence=evidence,
            ))

    return flags_to_dataframe(flags)


if __name__ == "__main__":
    print("Run via run_pipeline.py - this module is not meant to be run standalone.")
