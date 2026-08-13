# Sample Case Write-Ups

Three investigation summaries below, written in the style a real AML
analyst would use, based on cases this pipeline actually produced from a
5,000-customer / 6-month simulation run. Case IDs, scores, and figures are
real output from `output/cases.db` - you can pull up the same cases
yourself in the dashboard (Alert Queue → search the case ID → Case
Investigation).

These three were chosen deliberately to show three different outcomes: an
escalation, a case where the automated score under-states the real risk,
and a case that's plausibly a false positive. A portfolio that only shows
"and then we filed a SAR" isn't showing the whole job - most alerts, in
real AML programs, get closed with no action after review, and being able
to explain *why* is as important as knowing when to escalate.

---

## Case 1: CASE000459 — Thompson-Mayer (Import/Export Trading)

**Priority: High (score 80.7/100) — Recommended action: Escalate**

### Customer profile
- **Type:** Business — Import/Export Trading
- **Declared KYC risk rating:** High
- **Home country:** Pakistan (PK)
- **Review window:** 2026-02-18 to 2026-08-07 (full history)

### What was flagged
Three independent typologies fired for this customer in the same review
period:

1. **Structuring** (3 separate clusters, confidence 0.73–0.81): repeated
   groups of 3-4 transactions, each individually landing between $9,200
   and $9,750 - just under the $10,000 reporting threshold - clustered
   within 5-day windows. One cluster: `TXN00227592` and three others
   totaling $36,957.63, average transaction size $9,239.41. The pattern
   recurs three separate times across the review period, not once.
2. **Layering** (confidence 0.57): a 3-hop wire chain -
   `CUST02973 → CUST03184 → ... `, starting at $12,989.81 and ending at
   $12,548.13 (3.4% shrinkage, consistent with fees), completed in 40.5
   hours.
3. **High-risk jurisdiction exposure** (5 separate flags, confidence
   0.25–0.50): $117,533.64 cumulative, in transactions to/from Cuba,
   Libya, Myanmar, Syria, and Venezuela over the review period, including
   one single $27,073.18 wire to Venezuela and a $20,560.09 wire to Syria.

### Assessment
Individually, none of these three signals would be conclusive. An
import/export trading business legitimately has counterparties in
higher-risk markets - which is exactly why the high-risk-jurisdiction
detector scored each of those flags at reduced confidence (0.25-0.50, not
0.8+) and tagged them "consistent with declared business/geography." Taken
**together**, though, the picture changes: this customer isn't just
trading internationally, they're also repeatedly structuring cash/ACH
deposits just under the reporting threshold (three separate times, not a
one-off), and moving money through a same-day multi-hop wire chain. The
combination - structuring to avoid a reporting trigger, plus a rapid
pass-through chain, plus concentrated exposure to a specific handful of
higher-risk countries (not a broad, diversified trade book) - is
consistent with trade-based money laundering (TBML) layered on top of
deliberate structuring, not simply "an import/export business doing
import/export business."

### Recommended next step
- **Escalate to senior AML investigator.**
- Request enhanced due diligence documentation for the flagged high-risk-
  country transactions: invoices, bills of lading, or contracts
  substantiating that these are genuine trade payments and not
  pass-through movements.
- Cross-reference `CUST03184` (the layering chain's intermediate hop) - if
  that account also has its own KYC file, check whether it has a
  plausible, independent business relationship with this customer.
- If documentation doesn't substantiate the trade activity, file a SAR
  covering both the structuring pattern and the high-risk jurisdiction
  concentration.

---

## Case 2: CASE000492 — Kimberly Gallagher (Individual, Student)

**Priority: Low by automated score (25.6/100) — Recommended action: Manual override, escalate for review**

### Customer profile
- **Type:** Individual — occupation: Student
- **Declared KYC risk rating:** Medium
- **Home country:** Italy (IT)
- **Review window:** 2026-06-29 05:30 to 2026-06-30 10:13 (under 30 hours)

### What was flagged
One typology, but a striking instance of it:

- **Round-tripping** (confidence 0.976 - the highest-confidence single
  flag among all three cases in this document): $55,869.58 sent out on
  2026-06-29, and $56,207.27 returned the very next day (0.6% difference,
  1-day turnaround). Direct cycle - money went to one counterparty and
  came straight back, no intermediary.

### Assessment
This is exactly the case this document exists to highlight: **the
automated score says "Low priority" and that's the wrong read.** The
scoring engine's multi-typology bonus rewards several *independent*
signals firing together - but a single signal can be this strong on its
own. A confidence of 0.976 out of 1.0 on round-tripping means the pattern
match is nearly textbook: an exact-in/exact-out pair, same or related
counterparty, returned within a day.

More importantly, look at the profile: this is a declared **student**,
sending and receiving **$56,000** in under 30 hours. There is no
plausible legitimate transaction size for a student's declared profile
anywhere near this figure, whether or not the round-trip pattern existed
at all - the profile-inconsistency here is arguably a stronger signal than
the round-trip pattern itself, and it isn't something the current scoring
formula weighs directly (it only feeds into confidence indirectly, via
each detector's own logic).

This is a genuine limitation worth stating plainly: a purely additive,
weighted-score model will systematically under-rank strong single
signals relative to weaker-but-multiple signals. A production system
would need either a "confidence floor" override (any flag above some
confidence threshold gets a minimum priority regardless of typology
count) or a human-in-the-loop rule ("always review Top-N by single-flag
confidence, not just by aggregate score") to catch cases like this one.

### Recommended next step
- **Manually escalate for review despite the Low automated priority.**
- Request source-of-funds documentation for both legs of the transfer.
- Verify the counterparty relationship (`CUST03172`) - is there a
  plausible reason a student would be moving this amount with them (joint
  account, family member, shared business)?
- Flag this case in the retro/tuning backlog as a candidate for adding a
  confidence-floor override to the scoring engine (see
  [README Limitations](../README.md#limitations)).

---

## Case 3: CASE000634 — Gallagher, Howard and Sweeney (Professional Services)

**Priority: Low (score 15.0/100) — Recommended action: Request explanation, likely close**

### Customer profile
- **Type:** Business — Professional Services (Legal/Accounting)
- **Declared KYC risk rating:** Low
- **Home country:** Sweden (SE)
- **Review window:** 2026-05-18 to 2026-05-25 (7 days)

### What was flagged
- **Velocity** (confidence 1.0, z-score 21.7): this firm's own 90-day
  baseline is extremely quiet - about 0.3 transactions/day and
  $139.71/day in volume. In a single 7-day window, that jumped to 9
  transactions totaling $74,837.02.

### Assessment
A z-score of 21.7 sounds alarming, but the underlying story is
plausible and mundane: this is a small professional-services firm (legal
or accounting) whose normal activity is genuinely tiny - a handful of
small transactions a month. A single large client event - a settlement
disbursement, a real estate closing held in escrow, a retainer collection
tied to a big matter - would produce exactly this signature: a short burst
of unusually large activity against an otherwise flat baseline. Firms like
this don't have "smooth" cash flow the way a retailer does; their activity
is lumpy by nature, concentrated around case/matter milestones.

This is why the velocity detector alone, with no corroborating typology
(no structuring, no high-risk country exposure, no round-tripping), is
treated as Low priority: the pattern is real and worth a look, but a
single statistical outlier with an obvious mundane explanation is exactly
the kind of alert that should be resolved quickly, not escalated reflexively.

### Recommended next step
- **Request a brief explanation from the relationship manager or the
  customer** for the source of the 9 transactions (client name/matter
  reference is sufficient - full underlying documents not required at
  this stage).
- If the explanation is consistent with normal professional-services
  activity (e.g. a documented client settlement) and the counterparties
  aren't otherwise flagged, **close the case with a disposition note**
  ("Reviewed - legitimate client-matter related activity, no further
  action").
- If no reasonable explanation is provided, or the counterparties turn
  out to be unrelated to any client matter on file, escalate.

---

## What these three cases illustrate together

- **Case 1** shows the scoring engine working as intended: independent
  signals compounding into a well-justified High-priority escalation.
- **Case 2** shows where it falls short: a single extremely strong signal,
  paired with an obvious profile mismatch, under-scored by a formula that
  rewards breadth over depth - a real limitation to know about and design
  around, not paper over.
- **Case 3** shows the other half of an analyst's job that a demo focused
  only on "catching bad guys" tends to skip: most alerts have an innocent
  explanation, and writing a clear, defensible closure rationale is just
  as much a core skill as writing an escalation.
