"""
app.py
=======
The Streamlit dashboard: the "front end" an analyst would actually use.
Run it with:

    streamlit run src/dashboard/app.py

It has four views, picked from the sidebar:
  - Alert Queue        : browse/filter open cases, like a real analyst inbox
  - Case Investigation  : one case's full evidence, and a status/notes workflow
  - Network Graph       : the counterparty chain behind a case, drawn with networkx
  - MI Reporting        : portfolio-level volumes, typology mix, trends

Streamlit re-runs this entire script top-to-bottom on every click, so:
  - anything expensive (loading CSVs) is wrapped in @st.cache_data so it
    only actually re-runs when the underlying file changes
  - anything that needs to persist ACROSS reruns (which case is currently
    selected) is stored in st.session_state, not a plain variable
"""

import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

# app.py lives at src/dashboard/app.py - walk up to the project root so
# `import config` and the flat per-module imports below all resolve,
# exactly the same convention run_pipeline.py uses.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
for sub in ["case_management", "network"]:
    sys.path.insert(0, str(PROJECT_ROOT / "src" / sub))

import config                                              # noqa: E402
import alert_store                                          # noqa: E402
from graph_utils import build_case_graph, draw_case_graph, graph_stats  # noqa: E402

st.set_page_config(page_title="TM Surveillance Platform", layout="wide")


# ---------------------------------------------------------------------------
# Cached data loading
# ---------------------------------------------------------------------------
@st.cache_data
def load_customers():
    return pd.read_csv(config.CUSTOMERS_FILE)


@st.cache_data
def load_transactions():
    df = pd.read_csv(config.TRANSACTIONS_FILE)
    # format="mixed" parses each row's timestamp independently, which is
    # more robust than parse_dates= for a CSV that could contain a mix of
    # precisions (see run_pipeline.py's note on rounding injected
    # timestamps to the second - this is a defensive backstop, not the
    # primary fix).
    df["date"] = pd.to_datetime(df["date"], format="mixed")
    return df


def data_available() -> bool:
    return config.CUSTOMERS_FILE.exists() and config.TRANSACTIONS_FILE.exists() and config.CASE_DB_FILE.exists()


# ---------------------------------------------------------------------------
# Page: Alert Queue
# ---------------------------------------------------------------------------
def page_alert_queue():
    st.title("Alert Queue")
    st.caption("Every case produced by the risk-scoring engine, ranked by priority score.")

    col1, col2, col3 = st.columns(3)
    status = col1.selectbox("Status", ["All"] + config.ALERT_STATUSES)
    priority = col2.selectbox("Priority", ["All", "Critical", "High", "Medium", "Low"])
    typology = col3.selectbox(
        "Typology", ["All"] + list(config.TYPOLOGY_WEIGHTS.keys())
    )

    queue = alert_store.get_alert_queue(status=status, priority=priority, typology=typology)
    st.write(f"**{len(queue)} cases** match the current filters.")

    display_cols = ["case_id", "customer_name", "business_type", "risk_rating",
                     "typologies_triggered", "score", "priority", "status"]
    st.dataframe(queue[display_cols], use_container_width=True, hide_index=True, height=420)

    st.divider()
    st.subheader("Open a case")
    if queue.empty:
        st.info("No cases match the current filters.")
        return

    options = queue.apply(
        lambda r: f"{r['case_id']} — {r['customer_name']} — {r['priority']} ({r['score']:.1f})", axis=1
    )
    choice = st.selectbox("Select a case to investigate", options)
    selected_case_id = choice.split(" — ")[0]

    if st.button("Open Case Investigation ->", type="primary"):
        st.session_state["selected_case"] = selected_case_id
        st.session_state["nav"] = "Case Investigation"
        st.rerun()


# ---------------------------------------------------------------------------
# Page: Case Investigation
# ---------------------------------------------------------------------------
def page_case_investigation():
    st.title("Case Investigation")

    case_id_input = st.text_input(
        "Case ID", value=st.session_state.get("selected_case", ""),
        help="Pick a case from the Alert Queue page, or type a case_id directly.",
    )
    if not case_id_input:
        st.info("Select a case from the Alert Queue page first, or enter a case_id above.")
        return

    case = alert_store.get_case(case_id_input.strip())
    if case is None:
        st.error(f"No case found with id '{case_id_input}'.")
        return
    st.session_state["selected_case"] = case["case_id"]

    # --- Header: who, what, how bad -----------------------------------------------
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Priority Score", f"{case['score']:.1f} / 100")
    c2.metric("Priority Band", case["priority"])
    c3.metric("Typologies Triggered", case["num_typologies"])
    c4.metric("Current Status", case["status"])

    st.subheader(f"{case['customer_name']}  ({case['customer_id']})")
    p1, p2, p3, p4 = st.columns(4)
    p1.write(f"**Customer type:** {case['customer_type']}")
    p2.write(f"**Business / occupation:** {case['business_type']}")
    p3.write(f"**Declared risk rating:** {case['risk_rating']}")
    p4.write(f"**Home country:** {case['home_country']}")
    st.write(f"**Typologies:** {case['typologies_triggered'].replace(';', ', ')}")
    st.write(f"**Evidence window:** {case['window_start']} -> {case['window_end']}  "
             f"({case['num_transactions_involved']} transactions involved)")

    st.divider()

    # --- Evidence: the reasoning behind each flag -----------------------------------------------
    st.subheader("Evidence")
    st.caption("Every rule that fired for this customer, with the reasoning behind it.")
    for flag in case["flags_detail"]:
        with st.expander(
            f"**{flag['typology'].replace('_', ' ').title()}** "
            f"— confidence {flag['confidence']:.0%} — "
            f"{flag['window_start']} to {flag['window_end']}"
        ):
            ev = flag["evidence"]
            simple_fields = {k: v for k, v in ev.items() if not isinstance(v, (list, dict))}
            st.table(pd.DataFrame(simple_fields.items(), columns=["Metric", "Value"]))

            for key, value in ev.items():
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    st.write(f"**{key.replace('_', ' ').title()}:**")
                    st.dataframe(pd.DataFrame(value), use_container_width=True, hide_index=True)
                elif isinstance(value, list):
                    st.write(f"**{key.replace('_', ' ').title()}:** {value}")

    st.divider()

    # --- Workflow: change status -----------------------------------------------
    st.subheader("Update Case Status")
    w1, w2 = st.columns([1, 2])
    new_status = w1.selectbox("New status", config.ALERT_STATUSES,
                               index=config.ALERT_STATUSES.index(case["status"]))
    analyst = w1.text_input("Analyst", value="analyst1")
    note = w2.text_area("Note / rationale", placeholder="Why are you moving this case?")

    if st.button("Update Status", type="primary"):
        alert_store.update_status(case["case_id"], new_status, analyst=analyst, note=note)
        st.success(f"Case {case['case_id']} moved to '{new_status}'.")
        st.rerun()

    history = alert_store.get_case_history(case["case_id"])
    if not history.empty:
        st.write("**Status history:**")
        st.dataframe(history[["timestamp", "old_status", "new_status", "analyst", "note"]],
                     use_container_width=True, hide_index=True)

    st.divider()
    if st.button("View Network Graph for this case ->"):
        st.session_state["nav"] = "Network Graph"
        st.rerun()


# ---------------------------------------------------------------------------
# Page: Network Graph
# ---------------------------------------------------------------------------
def page_network_graph():
    st.title("Network Graph")
    st.caption("Counterparty chain behind a case's evidence - who the money moved through.")

    case_id_input = st.text_input("Case ID", value=st.session_state.get("selected_case", ""))
    if not case_id_input:
        st.info("Select a case from the Alert Queue page first, or enter a case_id above.")
        return

    case = alert_store.get_case(case_id_input.strip())
    if case is None:
        st.error(f"No case found with id '{case_id_input}'.")
        return

    typologies_in_case = sorted({f["typology"] for f in case["flags_detail"]})
    selected_typologies = st.multiselect(
        "Show evidence from typology (leave empty for all)",
        typologies_in_case, default=typologies_in_case,
        help="Compound cases can have a lot of evidence at once (e.g. a velocity "
             "burst alone can involve dozens of transactions) - narrow this down "
             "to see one pattern's chain more clearly.",
    )

    transactions_df = load_transactions()
    graph = build_case_graph(case, transactions_df, typologies=selected_typologies or None)
    stats = graph_stats(graph)

    s1, s2, s3 = st.columns(3)
    s1.metric("Accounts involved", stats["accounts_involved"])
    s2.metric("Money movements", stats["money_movements"])
    s3.metric("Total flow", f"${stats['total_flow_usd']:,.0f}")

    fig = draw_case_graph(graph, title=f"{case['case_id']} — {case['customer_name']}")
    st.pyplot(fig, use_container_width=True)
    st.caption(
        "🔴 Red = this case's customer   🔵 Blue = another of our customers   "
        "⚪ Grey = external counterparty (outside the bank)"
    )


# ---------------------------------------------------------------------------
# Page: MI Reporting
# ---------------------------------------------------------------------------
def page_mi_reporting():
    st.title("MI Reporting")
    st.caption("Portfolio-level view: alert volumes, typology mix, and trends.")

    summary = alert_store.queue_summary()
    customers_df = load_customers()
    transactions_df = load_transactions()

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Total cases", summary["total"])
    k2.metric("Critical + High priority", summary["by_priority"].get("Critical", 0)
              + summary["by_priority"].get("High", 0))
    k3.metric("Customers monitored", f"{len(customers_df):,}")
    k4.metric("Transactions analyzed", f"{len(transactions_df):,}")

    st.divider()
    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Cases by typology")
        if summary["by_typology"]:
            typ_df = pd.DataFrame(summary["by_typology"].items(), columns=["Typology", "Cases"])
            fig = px.bar(typ_df.sort_values("Cases"), x="Cases", y="Typology", orientation="h")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No cases yet.")

    with col2:
        st.subheader("Cases by priority")
        if summary["by_priority"]:
            pri_df = pd.DataFrame(summary["by_priority"].items(), columns=["Priority", "Cases"])
            order = ["Critical", "High", "Medium", "Low"]
            pri_df["Priority"] = pd.Categorical(pri_df["Priority"], categories=order, ordered=True)
            pri_df = pri_df.sort_values("Priority")
            fig = px.pie(pri_df, names="Priority", values="Cases", hole=0.4)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No cases yet.")

    st.subheader("Case workflow status")
    if summary["by_status"]:
        status_df = pd.DataFrame(summary["by_status"].items(), columns=["Status", "Cases"])
        status_df["Status"] = pd.Categorical(status_df["Status"], categories=config.ALERT_STATUSES, ordered=True)
        status_df = status_df.sort_values("Status")
        fig = px.bar(status_df, x="Status", y="Cases", text="Cases")
        st.plotly_chart(fig, use_container_width=True)

    st.divider()
    st.subheader("Transaction volume over time")
    weekly = transactions_df.set_index("date")["amount_usd"].resample("W").sum().reset_index()
    fig = px.line(weekly, x="date", y="amount_usd", labels={"amount_usd": "Weekly volume (USD)"})
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Top 10 highest-priority cases")
    queue = alert_store.get_alert_queue()
    if not queue.empty:
        top10 = queue.sort_values("score", ascending=False).head(10)
        st.dataframe(
            top10[["case_id", "customer_name", "typologies_triggered", "score", "priority", "status"]],
            use_container_width=True, hide_index=True,
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
# Short slugs for the URL (?page=mi) instead of spaces/punctuation - lets
# any view be deep-linked or bookmarked, e.g. sharing a link straight to
# one case's investigation view with a colleague.
PAGE_SLUGS = {"Alert Queue": "queue", "Case Investigation": "case",
              "Network Graph": "network", "MI Reporting": "mi"}
SLUG_TO_PAGE = {v: k for k, v in PAGE_SLUGS.items()}


def main():
    st.sidebar.title("TM Surveillance Platform")
    st.sidebar.caption("Synthetic AML transaction monitoring simulation")

    if not data_available():
        st.warning(
            "No data found yet. From the project root, run:\n\n"
            "```\npython run_pipeline.py\n```\n\n"
            "then reload this page."
        )
        return

    # A URL like ?page=case&case_id=CASE000459 pre-selects both the page
    # and the case - only applied on first load of a session so it doesn't
    # fight with the user's own in-app navigation afterwards.
    if "nav" not in st.session_state:
        query_page = st.query_params.get("page")
        if query_page in SLUG_TO_PAGE:
            st.session_state["nav"] = SLUG_TO_PAGE[query_page]
    if "selected_case" not in st.session_state:
        query_case = st.query_params.get("case_id")
        if query_case:
            st.session_state["selected_case"] = query_case

    pages = {
        "Alert Queue": page_alert_queue,
        "Case Investigation": page_case_investigation,
        "Network Graph": page_network_graph,
        "MI Reporting": page_mi_reporting,
    }
    default_page = st.session_state.get("nav", "Alert Queue")
    nav = st.sidebar.radio("Go to", list(pages.keys()), index=list(pages.keys()).index(default_page))
    st.session_state["nav"] = nav
    st.query_params["page"] = PAGE_SLUGS[nav]

    pages[nav]()


if __name__ == "__main__":
    main()
