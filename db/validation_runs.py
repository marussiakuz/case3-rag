"""CRUD для таблицы validation_runs в gd_app."""
from __future__ import annotations

from typing import Any

from db.connection import get_conn, put_conn

_DDL = """
CREATE TABLE IF NOT EXISTS validation_runs (
    id                        SERIAL PRIMARY KEY,
    run_at                    TIMESTAMPTZ DEFAULT NOW(),
    total_queries             INT,
    completed_queries         INT,
    execution_accuracy        FLOAT,
    strict_execution_accuracy FLOAT,
    avg_time_seconds          FLOAT,
    n_errors                  INT,
    simple_ea                 FLOAT,
    medium_ea                 FLOAT,
    complex_ea                FLOAT,
    duration_seconds          FLOAT
)
"""


def ensure_table() -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(_DDL)
        conn.commit()
    finally:
        put_conn(conn)


def save_run(stats: dict[str, Any]) -> None:
    ensure_table()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO validation_runs
                    (total_queries, completed_queries, execution_accuracy,
                     strict_execution_accuracy, avg_time_seconds, n_errors,
                     simple_ea, medium_ea, complex_ea, duration_seconds)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    stats.get("total_queries"),
                    stats.get("completed_queries"),
                    stats.get("execution_accuracy"),
                    stats.get("strict_execution_accuracy"),
                    stats.get("avg_time_seconds"),
                    stats.get("n_errors"),
                    stats.get("simple_ea"),
                    stats.get("medium_ea"),
                    stats.get("complex_ea"),
                    stats.get("duration_seconds"),
                ),
            )
        conn.commit()
    finally:
        put_conn(conn)


def load_runs(limit: int = 20) -> list[dict[str, Any]]:
    ensure_table()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id,
                       to_char(run_at, 'DD.MM.YYYY HH24:MI') AS run_at,
                       total_queries, completed_queries,
                       execution_accuracy, strict_execution_accuracy,
                       avg_time_seconds, n_errors,
                       simple_ea, medium_ea, complex_ea, duration_seconds
                FROM validation_runs
                ORDER BY run_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        put_conn(conn)
