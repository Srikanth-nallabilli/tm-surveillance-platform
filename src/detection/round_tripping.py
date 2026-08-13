"""
round_tripping.py
===================
Detects ROUND-TRIPPING: money that leaves a customer's account and comes
back to them (directly, or via one intermediary "pass-through" account)
within a short window, at roughly the same dollar amount.

We model this as CYCLE DETECTION on the money-flow graph: starting from a
customer, follow outgoing wire transfers forward in time and see whether
the money eventually finds its way back to the same customer within
config.ROUND_TRIP_WINDOW_DAYS. A cycle of length 2 (customer -> counterparty
-> customer) is the classic direct round trip; a cycle of length 3
(customer -> counterparty -> intermediary -> customer) is a slightly more
disguised version routed through one extra account.

Performance note: naively exploring every possible path in a large,
busy transaction graph can blow up combinatorially. We prune hard on two
things that any real round-trip must satisfy: (1) every hop must move
strictly forward in time and stay inside the overall time window, and
(2) the amount at every hop must stay in a broad band around the
original amount (money doesn't roughly double or vanish to a tenth of its
size and still count as "the same funds coming back").
"""

import networkx as nx
import pandas as pd

import config
from common import make_flag, flags_to_dataframe

MAX_CYCLE_DEPTH = 3          # customer -> ... -> customer, at most 3 hops
MAX_EXPLORED_PER_START = 300  # safety valve against pathological branching


def _build_wire_graph(transactions_df: pd.DataFrame) -> nx.MultiDiGraph:
    wires = transactions_df[transactions_df["transaction_type"] == "Wire Transfer"]
    graph = nx.MultiDiGraph()
    for _, row in wires.iterrows():
        graph.add_edge(
            row["sender_id"], row["receiver_id"],
            date=row["date"], amount=row["amount_usd"], transaction_id=row["transaction_id"],
        )
    return graph


def _find_cycles_from(graph, start_customer, cfg):
    cycles = []
    if start_customer not in graph:
        return cycles

    for _, first_v, first_data in graph.out_edges(start_customer, data=True):
        amount0 = first_data["amount"]
        band_lo, band_hi = amount0 * 0.5, amount0 * 1.5
        stack = [([(start_customer, first_v, first_data)], {first_data["transaction_id"]})]
        explored = 0

        while stack and explored < MAX_EXPLORED_PER_START:
            explored += 1
            path, visited_txns = stack.pop()
            last_u, last_v, last_data = path[-1]

            if last_v == start_customer and len(path) >= 2:
                cycles.append(path)
                continue
            if len(path) >= MAX_CYCLE_DEPTH:
                continue

            for _, next_v, next_data in graph.out_edges(last_v, data=True):
                if next_data["transaction_id"] in visited_txns:
                    continue
                if next_data["date"] <= last_data["date"]:
                    continue
                if (next_data["date"] - first_data["date"]).days > cfg.ROUND_TRIP_WINDOW_DAYS:
                    continue
                if not (band_lo <= next_data["amount"] <= band_hi):
                    continue
                stack.append((path + [(last_v, next_v, next_data)], visited_txns | {next_data["transaction_id"]}))

    return cycles


def _dedupe_cycles(cycles):
    seen = set()
    kept = []
    for cycle in cycles:
        key = frozenset(edge[2]["transaction_id"] for edge in cycle)
        if key in seen:
            continue
        seen.add(key)
        kept.append(cycle)
    return kept


def detect_round_tripping(transactions_df: pd.DataFrame, customers_df: pd.DataFrame,
                           cfg=config) -> pd.DataFrame:
    graph = _build_wire_graph(transactions_df)
    real_customers = customers_df["customer_id"].tolist()

    all_cycles = []
    for cust in real_customers:
        all_cycles.extend(_find_cycles_from(graph, cust, cfg))
    all_cycles = _dedupe_cycles(all_cycles)

    flags = []
    for cycle in all_cycles:
        amount0 = cycle[0][2]["amount"]
        final_amount = cycle[-1][2]["amount"]
        dates = [edge[2]["date"] for edge in cycle]
        txn_ids = [edge[2]["transaction_id"] for edge in cycle]
        path_nodes = [cycle[0][0]] + [edge[1] for edge in cycle]

        amount_match_score = max(0.0, 1 - abs(final_amount - amount0) / amount0)
        span_days = (max(dates) - min(dates)).days
        speed_score = max(0.0, 1 - span_days / cfg.ROUND_TRIP_WINDOW_DAYS)
        directness_score = 1.0 if len(cycle) == 2 else 0.75
        confidence = min(1.0, 0.4 * amount_match_score + 0.3 * speed_score + 0.3 * directness_score)

        involved_customers = [n for n in path_nodes if str(n).startswith("CUST") and n != path_nodes[0]]

        evidence = {
            "cycle_type": "direct" if len(cycle) == 2 else "via intermediary",
            "path": path_nodes,
            "amount_sent_usd": round(amount0, 2),
            "amount_returned_usd": round(final_amount, 2),
            "amount_difference_pct": round(abs(final_amount - amount0) / amount0 * 100, 1),
            "days_to_return": span_days,
            "hops": [
                {"from": edge[0], "to": edge[1], "date": str(edge[2]["date"]),
                 "amount_usd": edge[2]["amount"], "transaction_id": edge[2]["transaction_id"]}
                for edge in cycle
            ],
        }

        flags.append(make_flag(
            customer_id=path_nodes[0],
            typology="round_tripping",
            confidence=confidence,
            metric_value=amount0,
            window_start=min(dates),
            window_end=max(dates),
            transaction_ids=txn_ids,
            related_customer_ids=involved_customers,
            evidence=evidence,
        ))

    return flags_to_dataframe(flags)


if __name__ == "__main__":
    print("Run via run_pipeline.py - this module is not meant to be run standalone.")
