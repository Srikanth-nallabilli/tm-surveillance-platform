# Detection Methodology

This document explains exactly how each of the five typology detectors
works: the logic, the thresholds, and why they're set where they are. All
thresholds referenced below live in [`config.py`](../config.py) - nothing
here is hard-coded inside the detection code itself.

Every detector follows the same contract: it reads `transactions.csv` and
`customers.csv` (nothing else - no ground truth, no answer key) and returns
zero or more **flags**, each with a `confidence` score from 0 to 1. A
flag is not "suspicious = true/false" - it's "here's how strongly this
specific instance matches the pattern."

---

## 1. Structuring

**The idea:** launderers keep individual transactions just under a
regulatory reporting threshold (in the US, cash transactions over $10,000
trigger a Currency Transaction Report) to avoid detection, so they break
one large transaction into several smaller ones.

**The logic** (`src/detection/structuring.py`):
1. Pull every transaction between 90% and 100% of `CTR_THRESHOLD`
   ($10,000) in USD-equivalent terms - the "just under the line" band.
2. For each customer, walk their near-threshold transactions in date
   order and greedily cluster any that fall within `STRUCTURING_WINDOW_DAYS`
   (5 days) of the first transaction in the cluster.
3. Flag any cluster with at least `STRUCTURING_MIN_TXN_COUNT` (3)
   transactions.

**Confidence** blends two things: how many transactions are in the
cluster (more = more deliberate) and how close, on average, each amount
sits to the threshold (closer to $10,000 = more deliberate than sitting at
$9,000).

**Why a 90-100% band, not 80-100%?** An earlier, wider band (80-100%)
produced far too many false positives - ordinary businesses naturally have
some transactions that happen to land in a wide dollar range purely by
chance. Narrowing the band to the tightest, most textbook-recognizable
range cut noise sharply without losing any of the injected test scenarios
(which are generated using that same config value, so tightening the
threshold tightens both sides consistently).

---

## 2. Layering

**The idea:** money moves rapidly through a chain of accounts to obscure
its origin - the "layering" stage of the classic
placement→layering→integration laundering model.

**The logic** (`src/detection/layering.py`):
1. Build a directed graph (using `networkx`) of every Wire Transfer / ACH
   Transfer, with edges carrying the transaction's date, amount, and ID.
2. From every edge, greedily walk forward in time: from the current hop's
   receiver, find the next outgoing transfer that happens soon after
   (within `LAYERING_MAX_HOP_HOURS`, 72 hours) and moves a similar amount
   (allowed to shrink by up to `LAYERING_AMOUNT_TOLERANCE`, 15%, per hop -
   modeling fees or partial cash-outs). If several transactions qualify,
   take the earliest.
3. If the resulting chain reaches `LAYERING_MIN_CHAIN_LENGTH` (3) hops,
   flag it.

Standard graph algorithms (`shortest_path`, `all_simple_paths`, etc.)
don't understand "edges must be visited in time order with a roughly
preserved amount" - that's domain-specific, so the walk is hand-written on
top of the `networkx` graph rather than using a built-in path function.

**Confidence** blends chain length (longer = more deliberate) and speed
(a chain completed quickly relative to its allowed window is more
suspicious than one that just barely squeaks in under the time limit).

**Performance note:** naively exploring every path in a busy graph can
blow up combinatorially. The time-window and amount-tolerance checks are
applied *during* the walk (not after), which prunes the search space hard
- most transactions aren't a plausible next hop for a given chain, so the
branching factor stays small in practice.

---

## 3. Velocity / rapid fund movement

**The idea:** a customer suddenly transacts far more often, or moves far
more money, than *their own* history says is normal for them - the
comparison is always customer-relative, not a fixed limit, because
"unusual" only means something in the context of a customer's own pattern.

**The logic** (`src/detection/velocity.py`):
1. For each customer, and for each of their transaction dates `d`, define
   a "baseline" window (the `VELOCITY_BASELINE_DAYS` = 90 days before `d`)
   and a "recent" window (`VELOCITY_RECENT_WINDOW_DAYS` = 7 days starting
   at `d`).
2. Bucket the baseline period into **daily** counts and dollar volumes,
   including days with zero activity - this is what makes a normally
   quiet customer's baseline standard deviation small, so a real burst
   shows up as a large z-score for them specifically.
3. Compute a z-score for both count and volume: `(recent daily rate -
   baseline mean) / baseline standard deviation`. Take whichever is
   larger (a burst of many small transactions is just as much a red flag
   as a few huge ones - the typology is "frequency OR volume").
4. Flag if the z-score clears `VELOCITY_ZSCORE_THRESHOLD` (4.0) **and**
   the recent transaction count clears two separate gates: an absolute
   floor (`VELOCITY_MIN_RECENT_TXN_COUNT`) and a floor relative to the
   customer's own baseline rate (`VELOCITY_MIN_MULTIPLIER`, 4x) - a fixed
   count like "4 transactions" is a huge jump for someone who transacts
   once a month, but noise for someone who transacts daily.

**Three real bugs found (and fixed) while tuning this detector** - kept
here because they're a good illustration of why you validate detection
logic against known cases instead of trusting it on sight:

- **Cold-start bug:** for transaction dates near the very start of the
  dataset, the 90-day baseline window reached back before the simulation's
  data even began. Those "missing" days were zero-filled as if the
  customer was simply inactive, which artificially deflated the baseline
  and manufactured fake spikes right at the start of the data. Fixed by
  skipping any candidate window whose baseline period isn't fully covered
  by real data.
- **Thin-baseline instability:** a customer with a near-zero historical
  dollar baseline (e.g. $2/day) makes the volume z-score wildly unstable -
  one ordinary $500 transaction divided by a near-zero baseline produces
  an enormous but meaningless ratio. Fixed by only trusting the volume
  signal once the baseline clears `VELOCITY_MIN_BASELINE_DAILY_USD`;
  below that, the detector falls back to the count-based z-score.
- **Poisson noise at low volume:** low-activity individuals naturally
  have occasional busy weeks purely by chance (a customer averaging one
  transaction every five days will, some weeks, have four just from
  ordinary random clustering). A fixed "4 transactions" absolute floor
  wasn't a strong enough filter; adding the *relative* multiplier gate
  above fixed it.

---

## 4. High-risk jurisdiction exposure

**The idea:** transactions to/from a high-risk country that don't fit
what the customer's declared KYC profile would predict.

**The logic** (`src/detection/high_risk_jurisdiction.py`):
1. Pull every transaction where the counterparty's country is on the
   illustrative `HIGH_RISK_COUNTRIES` list (see [README
   Limitations](../README.md#limitations) - this list is for simulation
   purposes only).
2. Two guards keep this meaningful instead of noisy:
   - the counterparty's country must differ from the customer's **own**
     home country (a customer domiciled in a high-risk country transacting
     domestically isn't "cross-border exposure" - it's just their normal
     life, and the risk was already captured in their KYC rating at
     onboarding);
   - the counterparty must be **external** to the bank (a transfer to
     another of our own customers is already covered by that customer's
     own risk rating - this typology is about money moving somewhere the
     bank has no visibility into).
3. Cluster the remaining transactions per customer (same short-window
   clustering as structuring, `HIGH_RISK_WINDOW_DAYS` = 30 days) and flag
   clusters that clear a transaction-count or cumulative-dollar threshold.
4. Adjust confidence down (not to zero - still worth visibility, just
   lower priority) if the exposure is actually expected given the
   customer's profile: businesses like Import/Export Trading, Money
   Service Businesses, or Cryptocurrency Exchanges legitimately deal with
   counterparties worldwide.

**Bug found while tuning:** the first version didn't have guard #1, so any
customer whose own home country happened to sit on the high-risk list had
*all* their ordinary domestic activity misread as "high-risk exposure" -
producing an enormous false-positive rate (>2 flags per customer on
average across the whole customer base). The fix was conceptual, not just
a threshold tweak: domestic activity in your own country of residence
isn't "jurisdiction exposure," regardless of how that country happens to
be rated.

---

## 5. Round-tripping

**The idea:** funds leave a customer's account and come back to them,
directly or via one intermediary "pass-through" account, within a short
window, at roughly the same dollar amount - a common way to fabricate the
appearance of legitimate business activity.

**The logic** (`src/detection/round_tripping.py`): modeled as **cycle
detection** on the money-flow graph. Starting from a customer, follow
outgoing wire transfers forward in time (via `networkx`) and check whether
the money finds its way back to the same customer within
`ROUND_TRIP_WINDOW_DAYS` (14 days), at most `MAX_CYCLE_DEPTH` (3) hops
away. A 2-hop cycle (customer → counterparty → customer) is the classic
direct round trip; a 3-hop cycle (customer → counterparty → intermediary →
customer) is a lightly disguised version.

To keep the search tractable on a busy graph, every candidate hop must
stay within a broad amount band (50%-150% of the original amount) - a
real round trip doesn't send $80,000 and receive back $8,000, so this
prunes unrelated transactions aggressively without excluding genuine
cases.

**Confidence** blends how closely the returned amount matches the
original (tighter = more deliberate), how quickly the funds returned, and
whether it was a direct or intermediary-routed cycle (direct is scored
slightly higher - it's the more blatant version of the pattern).

---

## A note on "recall" vs. "precision"

`run_pipeline.py` prints a detector performance table comparing what each
rule caught against the scenarios this project deliberately injected. That
number is **recall**: of the customers we know are suspicious (because we
put the suspicious activity there ourselves), what fraction did each
detector catch? It is not **precision** (what fraction of *all* flags are
real) - measuring precision would require a labeled sample of the organic,
non-injected data, which by construction has no ground truth. Real TM
programs live with this same asymmetry: it's much easier to check "did we
catch the known bad guys" than "how many of our alerts are false alarms,"
which is exactly why alert review capacity is one of the biggest cost
centers in AML compliance.
