# Sample Case Write-Ups

Three investigation summaries below, written in the style a real AML
analyst would use, based on cases this pipeline actually produced from a
5,000-customer / 6-month simulation run. Case IDs, scores, and figures are
real output from `output/cases.db` - you can pull up the same cases
yourself in the dashboard (Alert Queue → search the case ID → Case
Investigation), or regenerate the identical dataset with `python
run_pipeline.py` (generation is seeded and reproducible - see `config.py`).

These three were chosen deliberately to show three different outcomes: a
clear escalation, a case where the automated score under-states the real
risk, and a case that's genuinely ambiguous and needs a human judgment
call. A portfolio that only shows "and then we filed a SAR" isn't showing
the whole job - most alerts, in real AML programs, get closed with no
action after review, and being able to explain *why* (or explain what's
still unresolved) is as important as knowing when to escalate.

---

## Case 1: CASE000460 — Thompson-Mayer (Import/Export Trading)

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
   0.25–0.50): $117,522.64 cumulative, in transactions to/from Cuba,
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

## Case 2: CASE000605 — Davis, Acosta and Diaz (Healthcare Services)

**Priority: Low by automated score (25.2/100) — Recommended action: Manual override, escalate for review**

### Customer profile
- **Type:** Business — Healthcare Services
- **Declared KYC risk rating:** Medium
- **Home country:** Libya (LY) - on this platform's illustrative high-risk
  country list (see [README Limitations](../README.md#limitations))
- **Review window:** 2026-05-26 01:21 to 2026-05-27 20:51 (under 44 hours)

### What was flagged
One typology, but a striking instance of it:

- **Round-tripping** (confidence 0.961): $49,180.05 sent to a counterparty
  (`CUST04692`) on 2026-05-26, and $47,079.31 returned the very next day
  (4.3% difference, 1-day turnaround). Direct cycle - money went to one
  counterparty and came straight back, no intermediary.

### Assessment
This is exactly the case this document exists to highlight: **the
automated score says "Low priority" and that's the wrong read.** A
confidence of 0.961 out of 1.0 on round-tripping means the pattern match is
nearly textbook - a large sum leaving and returning within a day, at
roughly the same value.

There's a second issue layered on top that the automated pipeline
can't see on its own: this customer's **home country is Libya**, which
sits on this platform's high-risk country list - yet their declared KYC
risk rating is only **Medium**. By design, the round-tripping detector
doesn't factor country risk into its confidence score at all (that's
deliberately the high-risk-jurisdiction detector's job, and it only
reacts to *foreign* counterparties, not a customer's own domicile - see
[docs/methodology.md](methodology.md#4-high-risk-jurisdiction-exposure)).
That separation of concerns keeps each detector's logic simple and
explainable, but it also means a case like this one doesn't get any
extra weight from an already-elevated jurisdiction profile unless a human
analyst connects the two facts themselves.

This is a genuine limitation worth stating plainly: a purely additive,
per-typology scoring model will under-rank a strong single signal, and
won't automatically cross-reference a customer's baseline risk profile
into a typology score that wasn't designed to consider it. A production
system would need either a "confidence floor" override (any flag above
some confidence threshold gets a minimum priority regardless of typology
count) or an explicit rule blending base KYC risk into every typology's
score, not just the ones that already reason about geography.

### Recommended next step
- **Manually escalate for review despite the Low automated priority.**
- Request source-of-funds and business-purpose documentation for both legs
  of the transfer from the customer.
- Verify the counterparty relationship (`CUST04692`) - is there a
  documented, ongoing business relationship that would explain a same-day
  round trip of this size?
- Separately, refer this customer's file to KYC/onboarding for a risk
  rating review - a Medium rating paired with a high-risk home
  jurisdiction is worth re-examining on its own, independent of this
  alert.

---

## Case 3: CASE000514 — Alexandra Harper (Self-Employed Individual)

**Priority: Low (score 15.8/100) — Recommended action: Request detailed explanation; escalate if unexplained**

### Customer profile
- **Type:** Individual — occupation: Self-Employed
- **Declared KYC risk rating:** Medium
- **Home country:** United Arab Emirates (AE)
- **Review window:** 2026-05-20 to 2026-05-29 (about 9 days)

### What was flagged
- **Velocity** (confidence 1.0, z-score 12.3): this customer's own 90-day
  baseline is modest but not tiny - about 1.3 transactions/day and
  $2,537/day in volume (roughly $1,968 per transaction on average). In a
  single 7-day window, that jumped to **111 transactions** totaling
  $94,163.86.

### Assessment
This one is genuinely ambiguous, which is exactly why it belongs in this
document - not every alert resolves cleanly in either direction.

Two numbers matter here, not just the headline z-score. Against her own
baseline, transaction **count** jumped roughly 12x (about 9 transactions
expected in a 7-day window, 111 actually occurred) while dollar **volume**
only jumped about 5x. Doing the arithmetic: her average transaction size
during the burst (~$848) is well under half her normal average (~$1,968).
That combination - a much larger jump in *how often* than in *how much* -
is a materially different signature from "one big legitimate payment
event." A single large client settlement would show up mostly as a volume
spike with a normal or even higher average transaction size; a burst of
many smaller transactions is the more classic signature of a pass-through
or aggregation pattern (funds moving through an account in many small
pieces), which is worth genuine scrutiny for a declared self-employed
individual with no business type on file to explain high transaction
throughput.

That said, there are entirely legitimate explanations too - she could have
started using the account for a side gig-economy business (ride-share,
marketplace selling, freelance platform payouts routinely arrive as many
small deposits), which a "Self-Employed" declared occupation doesn't rule
out. The data alone doesn't resolve this; it needs a human to ask.

### Recommended next step
- **Request a specific explanation from the customer**, not just a general
  one: what is the source of the 111 transactions, and is there a platform,
  employer, or marketplace name that would explain many small, similar-
  sized deposits?
- Pull the underlying transaction list (available in the Case Investigation
  evidence view) and check whether the counterparties are diverse and
  plausible (many different payers, consistent with gig income) or
  suspiciously repetitive/structured (which would raise the concern level
  further, not lower it).
- If the explanation and counterparty pattern are consistent with
  legitimate gig/marketplace income, **close with a disposition note**
  documenting the explanation. If not, or if no explanation is provided,
  escalate - do not default to closing just because there's a plausible
  innocent story available.

---

## What these three cases illustrate together

- **Case 1** shows the scoring engine working as intended: independent
  signals compounding into a well-justified High-priority escalation.
- **Case 2** shows where it falls short in a specific, explainable way: a
  single extremely strong signal, plus a risk factor (home-country
  jurisdiction) that the triggered typology's own logic was never designed
  to weigh - a real architectural limitation to know about and design
  around, not paper over.
- **Case 3** shows the other half of an analyst's job that a demo focused
  only on "catching bad guys" tends to skip: some alerts don't resolve
  cleanly either way from the data alone, and knowing exactly what
  question to ask next - not just "escalate" or "close" - is a core skill
  in its own right.
