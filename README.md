# TM Surveillance Platform

A synthetic **Transaction Monitoring (TM)** simulation platform for AML/KYC
compliance: it generates a realistic bank-scale customer and transaction
dataset (with known suspicious activity deliberately hidden inside it),
runs five rule-based typology detectors against that data, combines their
output into a single weighted priority score per customer, and presents the
results through a case-management workflow and an interactive dashboard.

This is a **portfolio / learning project**, built to demonstrate
understanding of how transaction monitoring actually works end to end -
data, detection logic, scoring, investigation workflow, and reporting -
not a production compliance tool. See [Limitations](#limitations) below.

## Why this project exists

Real transaction monitoring systems generate an alert, and an analyst has
to answer: *why did this fire, how confident should I be, and what do I do
next?* Most tutorials only show the first half of that (a rule that flags
something). This project builds the whole loop:

**generate data → detect patterns → score & prioritize → investigate → close the case → report on it**

## Architecture

```
tm-surveillance-platform/
├── config.py                    # every threshold, weight and file path - single source of truth
├── run_pipeline.py               # one script that runs the entire pipeline end to end
├── src/
│   ├── data_generation/
│   │   ├── customers.py          # synthetic customer/KYC profiles
│   │   ├── transactions.py       # normal ("clean") transaction history
│   │   └── typologies.py         # injects 5 suspicious patterns + hidden ground truth
│   ├── detection/
│   │   ├── common.py             # shared helpers (account-centric view, flag format)
│   │   ├── structuring.py
│   │   ├── layering.py
│   │   ├── velocity.py
│   │   ├── high_risk_jurisdiction.py
│   │   ├── round_tripping.py
│   │   └── run_detectors.py      # runs all 5 and combines their output
│   ├── scoring/
│   │   └── risk_scoring.py       # combines flags into one weighted case score
│   ├── case_management/
│   │   └── alert_store.py        # SQLite-backed alert queue + audit trail
│   ├── network/
│   │   └── graph_utils.py        # networkx graph construction + matplotlib rendering
│   └── dashboard/
│       └── app.py                # Streamlit app (4 views)
├── data/                          # generated customers.csv / transactions.csv (gitignored)
├── output/                        # alerts.csv, cases.db (gitignored)
└── docs/
    ├── methodology.md             # detection logic for each typology, in depth
    └── case_writeups.md           # 3 sample investigation write-ups
```

Every module does one job and reads its parameters from `config.py` - no
thresholds are hard-coded inside detection logic. That's a deliberate
mirror of how real institutions separate "rule parameters" (which
compliance/analytics teams tune) from "rule engine code" (which engineers
maintain).

## Quickstart

```bash
python -m venv venv
venv\Scripts\activate        # on macOS/Linux: source venv/bin/activate
pip install -r requirements.txt

python run_pipeline.py       # generates data, runs detectors, scores, builds the case DB
streamlit run src/dashboard/app.py
```

`run_pipeline.py` prints a detector performance summary at the end -
comparing what each rule caught against the scenarios that were
deliberately injected. That comparison only exists for this project's own
testing purposes (see [Methodology](#methodology--how-detection-works)) -
production detectors don't get an answer key.

## Methodology - how detection works

Full detail for each typology (the exact logic, the thresholds, and why
they were chosen) lives in **[docs/methodology.md](docs/methodology.md)**.
The short version:

| Typology | Core idea | Key thresholds |
|---|---|---|
| **Structuring** | Several transactions clustered just under a reporting threshold in a short window | 90-100% of $10,000, ≥3 txns in 5 days |
| **Layering** | Money hops rapidly through a chain of accounts, shrinking slightly each hop | ≥3 hops, ≤72h between hops |
| **Velocity** | A customer's own recent activity is a statistical outlier vs. their own history | z-score ≥ 4 vs. their 90-day baseline |
| **High-risk jurisdiction** | Cross-border exposure to a high-risk country that doesn't fit the customer's declared profile | ≥2 txns or ≥$5,000 to/from a high-risk country in 30 days |
| **Round-tripping** | Funds leave and return to the same customer, directly or via one intermediary | ≤14 days, return amount within 10% of the amount sent |

Detection is entirely **rule-based** - no machine learning. Each rule
outputs a graded confidence (0-1), not just a yes/no flag, based on things
like cluster size, timing tightness, and how well the pattern matches the
customer's own declared profile.

## Risk scoring

The scoring engine (`src/scoring/risk_scoring.py`) combines a customer's
flags into one 0-100 score:

1. Take each triggered typology's **strongest** instance (its max
   confidence), multiplied by that typology's base weight.
2. Add a capped bonus if **more than one distinct typology** fired -
   independent red flags compounding is more convincing than any one
   alone.
3. Scale slightly by the customer's declared KYC risk rating.
4. Bucket into a priority band: Critical / High / Medium / Low.

A single weak signal never produces a Critical alert; a customer tripping
three unrelated rules at once reliably does.

## Case management

Cases are stored in a small SQLite database (`output/cases.db`) with a
proper workflow: `New → Under Review → Escalated → Closed`, each status
change logged with a timestamp, analyst, and note - a minimal audit trail,
the same shape every real case management tool uses.

## Dashboard

Four views (`streamlit run src/dashboard/app.py`):

- **Alert Queue** - filterable list of every case, sorted by priority
- **Case Investigation** - one customer's full evidence (why each rule
  fired, with the underlying transactions), plus the status-change workflow
- **Network Graph** - the counterparty chain behind a case's evidence,
  drawn with networkx (built directly from the evidence transaction IDs -
  narrow-able to a single typology for compound cases)
- **MI Reporting** - portfolio-level volumes, typology mix, and status
  trends

## Sample case write-ups

Three analyst-style investigation summaries, based on cases this pipeline
actually generated, are in
**[docs/case_writeups.md](docs/case_writeups.md)**.

## Limitations

This is a simulation, and being explicit about what it does *not* do is
part of demonstrating real understanding of the domain:

- **Rule-based, not ML.** Every detector here is a hand-tuned threshold
  rule. Real institutions increasingly layer statistical/ML models on top
  of (not instead of) rules - for anomaly scoring, entity resolution, and
  reducing false positives. This project intentionally stays rule-based
  because the *reasoning* behind each rule needs to be explainable to a
  regulator, which is still the industry norm for the primary detection
  layer.
- **Synthetic data, not real behavior.** The data generator uses
  simplified statistical distributions (lognormal amounts, uniform random
  timing). Real transaction data has richer structure - seasonality,
  payroll cycles, merchant category patterns - that this simulation
  doesn't attempt to reproduce.
- **The high-risk country list is illustrative**, not a real sanctions or
  FATF list, and is not maintained or updated. A production system would
  use a licensed, continuously updated data feed.
- **False positives are real and expected.** Even after tuning (see
  `config.py` and the commit history for the false-positive fixes made
  while building this), the detectors still over-flag relative to the
  known ground truth - which is realistic: real-world TM systems commonly
  see false-positive rates upward of 90%. This project's "detector
  performance" report (run via `run_pipeline.py`) measures **recall**
  (did we catch what we know is suspicious) - it does not measure
  precision, which would require a labeled sample of the organic
  (non-injected) data too.
- **One case per customer, ever.** The scoring engine treats the entire
  6-month dataset as a single review period. A production system reruns
  detection on a rolling cadence (e.g. daily) and opens a new case per
  period.
- **No entity resolution.** Every customer/account is treated as a single
  clean identity. Real AML programs spend significant effort on
  identifying when two "different" customers are actually connected
  (shared address, shared beneficial owner, etc.) - this project doesn't
  model that.

## License

MIT - built as a personal portfolio project.
