"""
layering.py
============
Detects LAYERING: money hopping rapidly through a chain of accounts to
obscure its origin (classic "placement -> layering -> integration" middle
stage of money laundering).

How the search works:
  1. Build a directed graph of money movement using networkx: one edge per
     transfer-type transaction (Wire/ACH - the channels layering actually
     uses), from sender to receiver, carrying the date/amount/transaction_id.
  2. Starting from each edge, greedily walk FORWARD in time: from the
     current hop's receiver, find the next outgoing transfer that (a)
     happens soon after (within config.LAYERING_MAX_HOP_HOURS), and
     (b) moves a similar amount (allowing for a fee-like shrinkage of up to
     config.LAYERING_AMOUNT_TOLERANCE per hop). If several qualify, take the
     earliest one - a real "trail" a launderer left has one continuation,
     not several.
  3. If the resulting chain has at least config.LAYERING_MIN_CHAIN_LENGTH
     hops, flag it.

Standard networkx path-finding functions (shortest_path, all_simple_paths,
etc.) assume a static graph and don't understand "must move forward in
time" or "amount must roughly carry through" - so we use networkx only to
hold the graph, and write our own constrained walk on top of it.
"""

import networkx as nx
import pandas as pd

import config
from common import make_flag, flags_to_dataframe

LAYERING_TRANSACTION_TYPES = {"Wire Transfer", "ACH Transfer"}
MAX_CHAIN_DEPTH = 10  # hard safety cap so a dense graph can't blow up runtime


def _build_transfer_graph(transactions_df: pd.DataFrame) -> nx.MultiDiGraph:
    transfers = transactions_df[transactions_df["transaction_type"].isin(LAYERING_TRANSACTION_TYPES)]
    graph = nx.MultiDiGraph()
    for _, row in transfers.iterrows():
        graph.add_edge(
            row["sender_id"], row["receiver_id"],
            date=row["date"], amount=row["amount_usd"], transaction_id=row["transaction_id"],
        )
    return graph


def _find_chains(graph: nx.MultiDiGraph, cfg):
    """Greedily walk every edge forward in time and yield qualifying chains."""
    all_edges = [
        (u, v, data) for u, v, data in graph.edges(data=True)
    ]
    all_edges.sort(key=lambda e: e[2]["date"])

    chains = []
    for start_u, start_v, start_data in all_edges:
        chain = [(start_u, start_v, start_data)]
        visited_txn_ids = {start_data["transaction_id"]}
        current_node, current_date, current_amount = start_v, start_data["date"], start_data["amount"]

        while len(chain) < MAX_CHAIN_DEPTH:
            candidates = []
            for _, next_v, next_data in graph.out_edges(current_node, data=True):
                if next_data["transaction_id"] in visited_txn_ids:
                    continue
                hop_hours = (next_data["date"] - current_date).total_seconds() / 3600
                if not (0 < hop_hours <= cfg.LAYERING_MAX_HOP_HOURS):
                    continue
                min_amount = current_amount * (1 - cfg.LAYERING_AMOUNT_TOLERANCE)
                max_amount = current_amount * 1.05
                if not (min_amount <= next_data["amount"] <= max_amount):
                    continue
                candidates.append((current_node, next_v, next_data))

            if not candidates:
                break
            next_hop = min(candidates, key=lambda e: e[2]["date"])
            chain.append(next_hop)
            visited_txn_ids.add(next_hop[2]["transaction_id"])
            current_node, current_date, current_amount = next_hop[1], next_hop[2]["date"], next_hop[2]["amount"]

        if len(chain) >= cfg.LAYERING_MIN_CHAIN_LENGTH:
            chains.append(chain)
    return chains


def _dedupe_chains(chains):
    """Keep the longest chain when one chain is a prefix of another (same start)."""
    chains_sorted = sorted(chains, key=len, reverse=True)
    kept = []
    seen_txn_sets = []
    for chain in chains_sorted:
        txn_ids = frozenset(edge[2]["transaction_id"] for edge in chain)
        if any(txn_ids <= existing for existing in seen_txn_sets):
            continue
        kept.append(chain)
        seen_txn_sets.append(txn_ids)
    return kept


def detect_layering(transactions_df: pd.DataFrame, customers_df: pd.DataFrame,
                     cfg=config) -> pd.DataFrame:
    graph = _build_transfer_graph(transactions_df)
    chains = _find_chains(graph, cfg)
    chains = _dedupe_chains(chains)

    flags = []
    for chain in chains:
        nodes_in_order = [chain[0][0]] + [edge[1] for edge in chain]
        internal_nodes = [n for n in nodes_in_order if str(n).startswith("CUST")]
        if not internal_nodes:
            continue  # a chain entirely between external parties isn't ours to flag

        primary_customer = internal_nodes[0]
        amounts = [edge[2]["amount"] for edge in chain]
        dates = [edge[2]["date"] for edge in chain]
        txn_ids = [edge[2]["transaction_id"] for edge in chain]

        total_shrinkage = 1 - (amounts[-1] / amounts[0]) if amounts[0] else 0
        span_hours = (max(dates) - min(dates)).total_seconds() / 3600

        length_score = min(1.0, 0.4 + 0.15 * (len(chain) - cfg.LAYERING_MIN_CHAIN_LENGTH))
        speed_score = max(0.0, 1 - span_hours / (cfg.LAYERING_MAX_HOP_HOURS * len(chain)))
        confidence = min(1.0, 0.6 * length_score + 0.4 * speed_score)

        evidence = {
            "chain_length_hops": len(chain),
            "accounts_in_chain": nodes_in_order,
            "starting_amount_usd": round(amounts[0], 2),
            "ending_amount_usd": round(amounts[-1], 2),
            "total_shrinkage_pct": round(total_shrinkage * 100, 1),
            "total_span_hours": round(span_hours, 1),
            "hops": [
                {"from": edge[0], "to": edge[1], "date": str(edge[2]["date"]),
                 "amount_usd": edge[2]["amount"], "transaction_id": edge[2]["transaction_id"]}
                for edge in chain
            ],
        }

        flags.append(make_flag(
            customer_id=primary_customer,
            typology="layering",
            confidence=confidence,
            metric_value=len(chain),
            window_start=min(dates),
            window_end=max(dates),
            transaction_ids=txn_ids,
            related_customer_ids=[n for n in internal_nodes if n != primary_customer],
            evidence=evidence,
        ))

    return flags_to_dataframe(flags)


if __name__ == "__main__":
    print("Run via run_pipeline.py - this module is not meant to be run standalone.")
