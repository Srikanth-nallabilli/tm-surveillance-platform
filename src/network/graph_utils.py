"""
graph_utils.py
================
Builds and draws the counterparty network for a single case, using
networkx for the graph structure and matplotlib for rendering (kept
simple and dependency-light so it "just works" inside Streamlit).

The idea: every case's evidence already lists exactly which
transaction_ids triggered it (across all its flags, whatever typology).
We look those specific transactions back up in the full transaction table,
and draw a directed graph of "who sent money to whom" using only that
evidence - which is exactly what an investigator does when they draw a
box-and-arrow diagram on a whiteboase while working a case. For a
layering or round-tripping case this naturally comes out as a chain or a
cycle; for a structuring or velocity case it comes out as a simple star
(the customer and the handful of counterparties involved) - both are
useful to see.
"""

import matplotlib
matplotlib.use("Agg")  # headless backend - required for Streamlit/server use
import matplotlib.pyplot as plt
import networkx as nx
import pandas as pd


def build_case_graph(case: dict, transactions_df: pd.DataFrame, typologies=None) -> nx.DiGraph:
    """
    Build a directed graph of the money movement behind one case.

    `case` is the dict returned by alert_store.get_case() - it has
    `flags_detail`, a list of the individual rule hits, each carrying its
    own `transaction_ids`. A compound case (several typologies triggered)
    can involve a lot of evidence at once - e.g. a velocity flag alone
    might reference dozens of transactions - so `typologies` lets the
    caller (the dashboard) restrict the graph to just the flag(s) an
    analyst actually wants to look at, such as "only the layering chain",
    instead of always drawing every scrap of evidence in one tangled graph.
    """
    txn_ids = set()
    for flag in case["flags_detail"]:
        if typologies and flag["typology"] not in typologies:
            continue
        txn_ids.update(flag.get("transaction_ids", []))

    relevant = transactions_df[transactions_df["transaction_id"].isin(txn_ids)]

    graph = nx.DiGraph()
    primary_customer = case["customer_id"]

    for _, row in relevant.iterrows():
        for node_id, node_country in [(row["sender_id"], row["sender_country"]),
                                       (row["receiver_id"], row["receiver_country"])]:
            if node_id not in graph:
                node_type = "primary" if node_id == primary_customer else (
                    "customer" if str(node_id).startswith("CUST") else "external"
                )
                graph.add_node(node_id, node_type=node_type, country=node_country)

        # Multiple transactions between the same two parties accumulate
        # onto one edge (summed amount, kept count) rather than drawing a
        # tangle of parallel arrows.
        if graph.has_edge(row["sender_id"], row["receiver_id"]):
            graph[row["sender_id"]][row["receiver_id"]]["amount_usd"] += row["amount_usd"]
            graph[row["sender_id"]][row["receiver_id"]]["txn_count"] += 1
        else:
            graph.add_edge(row["sender_id"], row["receiver_id"],
                            amount_usd=row["amount_usd"], txn_count=1)

    return graph


NODE_COLORS = {"primary": "#d62728", "customer": "#1f77b4", "external": "#7f7f7f"}


def draw_case_graph(graph: nx.DiGraph, title: str = "Counterparty Network"):
    """Render the case graph as a matplotlib Figure, ready for st.pyplot()."""
    fig, ax = plt.subplots(figsize=(8, 6))

    if graph.number_of_nodes() == 0:
        ax.text(0.5, 0.5, "No transaction evidence to visualize", ha="center", va="center")
        ax.axis("off")
        return fig

    layout = nx.spring_layout(graph, seed=42, k=1.2 / max(1, graph.number_of_nodes() ** 0.5))

    node_colors = [NODE_COLORS.get(graph.nodes[n]["node_type"], "#7f7f7f") for n in graph.nodes]
    node_sizes = [1400 if graph.nodes[n]["node_type"] == "primary" else 900 for n in graph.nodes]

    nx.draw_networkx_nodes(graph, layout, ax=ax, node_color=node_colors, node_size=node_sizes, alpha=0.9)
    nx.draw_networkx_edges(graph, layout, ax=ax, arrowstyle="-|>", arrowsize=18,
                            edge_color="#555555", connectionstyle="arc3,rad=0.08")

    labels = {n: (n if len(str(n)) <= 12 else str(n)[:10] + "…") for n in graph.nodes}
    nx.draw_networkx_labels(graph, layout, labels=labels, ax=ax, font_size=8)

    edge_labels = {
        (u, v): f"${d['amount_usd']:,.0f}" + (f" ({d['txn_count']}x)" if d["txn_count"] > 1 else "")
        for u, v, d in graph.edges(data=True)
    }
    nx.draw_networkx_edge_labels(graph, layout, edge_labels=edge_labels, ax=ax, font_size=7)

    ax.set_title(title)
    ax.axis("off")
    fig.tight_layout()
    return fig


def graph_stats(graph: nx.DiGraph) -> dict:
    if graph.number_of_nodes() == 0:
        return {"accounts_involved": 0, "money_movements": 0, "total_flow_usd": 0.0}
    return {
        "accounts_involved": graph.number_of_nodes(),
        "money_movements": graph.number_of_edges(),
        "total_flow_usd": round(sum(d["amount_usd"] for _, _, d in graph.edges(data=True)), 2),
    }
