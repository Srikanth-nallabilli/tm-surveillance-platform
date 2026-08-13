"""
alert_store.py
================
The case management layer: turns the scored cases from risk_scoring.py
into a persistent alert queue an analyst can actually work - open a case,
change its status, leave a note, come back tomorrow and see what happened.

We use plain SQLite (Python's built-in sqlite3 module - no extra database
server needed) as a simple, portable, file-based store. This matters
specifically because the dashboard is a Streamlit app: Streamlit re-runs
the whole script on every click, so anything held only in a Python
variable would be wiped out the moment an analyst changes a filter. Writing
status changes straight to a file on disk is what makes them "stick".

Two tables:
  - cases          : one row per case (the current state - status, score,
                      assigned analyst, etc.)
  - case_history    : one row per status change (an audit trail - who did
                      what, when, and why - which every real case
                      management system needs for compliance purposes)
"""

import json
import sqlite3
from datetime import datetime, timezone

import pandas as pd

import config

CASES_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    customer_id TEXT,
    customer_name TEXT,
    customer_type TEXT,
    business_type TEXT,
    risk_rating TEXT,
    home_country TEXT,
    typologies_triggered TEXT,
    num_typologies INTEGER,
    num_flags INTEGER,
    num_transactions_involved INTEGER,
    score REAL,
    priority TEXT,
    window_start TEXT,
    window_end TEXT,
    flags_detail_json TEXT,
    status TEXT DEFAULT 'New',
    assigned_analyst TEXT,
    disposition TEXT,
    created_at TEXT,
    updated_at TEXT
);
"""

HISTORY_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS case_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT,
    timestamp TEXT,
    old_status TEXT,
    new_status TEXT,
    analyst TEXT,
    note TEXT
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db(db_path=config.CASE_DB_FILE):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(CASES_TABLE_SCHEMA)
    conn.execute(HISTORY_TABLE_SCHEMA)
    conn.commit()
    conn.close()


def load_cases_into_db(cases_df: pd.DataFrame, db_path=config.CASE_DB_FILE):
    """
    Insert newly-scored cases into the alert queue. Cases that already
    exist (same case_id, from a previous pipeline run) are left alone -
    we never want re-running the detectors to silently wipe out an
    analyst's status changes or notes on a case they're actively working.
    """
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    existing_ids = {row[0] for row in conn.execute("SELECT case_id FROM cases").fetchall()}

    now = _now()
    inserted = 0
    for _, row in cases_df.iterrows():
        if row["case_id"] in existing_ids:
            continue
        conn.execute(
            """INSERT INTO cases (
                case_id, customer_id, customer_name, customer_type, business_type,
                risk_rating, home_country, typologies_triggered, num_typologies,
                num_flags, num_transactions_involved, score, priority,
                window_start, window_end, flags_detail_json, status,
                assigned_analyst, disposition, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["case_id"], row["customer_id"], row["customer_name"], row["customer_type"],
                row["business_type"], row["risk_rating"], row["home_country"],
                row["typologies_triggered"], int(row["num_typologies"]), int(row["num_flags"]),
                int(row["num_transactions_involved"]), float(row["score"]), row["priority"],
                str(row["window_start"]), str(row["window_end"]), row["flags_detail_json"],
                "New", None, None, now, now,
            ),
        )
        inserted += 1

    conn.commit()
    conn.close()
    return inserted


def get_alert_queue(db_path=config.CASE_DB_FILE, status=None, priority=None, typology=None) -> pd.DataFrame:
    """Return the alert queue as a DataFrame, optionally filtered."""
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    query = "SELECT * FROM cases WHERE 1=1"
    params = []
    if status and status != "All":
        query += " AND status = ?"
        params.append(status)
    if priority and priority != "All":
        query += " AND priority = ?"
        params.append(priority)
    if typology and typology != "All":
        query += " AND typologies_triggered LIKE ?"
        params.append(f"%{typology}%")
    query += " ORDER BY score DESC"

    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    return df


def get_case(case_id: str, db_path=config.CASE_DB_FILE):
    """Return one case as a dict, with flags_detail_json parsed back into a list."""
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query("SELECT * FROM cases WHERE case_id = ?", conn, params=[case_id])
    conn.close()
    if df.empty:
        return None
    case = df.iloc[0].to_dict()
    case["flags_detail"] = json.loads(case["flags_detail_json"])
    return case


def get_case_history(case_id: str, db_path=config.CASE_DB_FILE) -> pd.DataFrame:
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query(
        "SELECT * FROM case_history WHERE case_id = ? ORDER BY timestamp ASC", conn, params=[case_id]
    )
    conn.close()
    return df


def update_status(case_id: str, new_status: str, analyst: str = "analyst",
                   note: str = "", disposition: str = None, db_path=config.CASE_DB_FILE):
    """
    Move a case to a new status and record the change in the audit trail.
    This is the core "workflow" action - every real case management tool
    is built around exactly this: change status, say why, keep the history.
    """
    if new_status not in config.ALERT_STATUSES:
        raise ValueError(f"Unknown status '{new_status}'. Must be one of {config.ALERT_STATUSES}")

    init_db(db_path)
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT status FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if row is None:
        conn.close()
        raise ValueError(f"Case '{case_id}' not found")
    old_status = row[0]

    now = _now()
    conn.execute(
        "UPDATE cases SET status = ?, assigned_analyst = ?, disposition = ?, updated_at = ? WHERE case_id = ?",
        (new_status, analyst, disposition, now, case_id),
    )
    conn.execute(
        "INSERT INTO case_history (case_id, timestamp, old_status, new_status, analyst, note) "
        "VALUES (?,?,?,?,?,?)",
        (case_id, now, old_status, new_status, analyst, note),
    )
    conn.commit()
    conn.close()


def queue_summary(db_path=config.CASE_DB_FILE) -> dict:
    """Quick counts used by the dashboard's summary/MI view."""
    df = get_alert_queue(db_path)
    if df.empty:
        return {"total": 0, "by_status": {}, "by_priority": {}, "by_typology": {}}

    typ_counts = {}
    for typs in df["typologies_triggered"].dropna():
        for t in typs.split(";"):
            typ_counts[t] = typ_counts.get(t, 0) + 1

    return {
        "total": len(df),
        "by_status": df["status"].value_counts().to_dict(),
        "by_priority": df["priority"].value_counts().to_dict(),
        "by_typology": typ_counts,
    }


if __name__ == "__main__":
    print("Run via run_pipeline.py - this module is not meant to be run standalone.")
