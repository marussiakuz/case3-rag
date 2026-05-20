"""
CRUD для таблицы query_history в gd_app.
"""
from __future__ import annotations

import json
from typing import Any

from db.connection import get_conn, put_conn


def _ensure_iterations_column() -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE query_history ADD COLUMN IF NOT EXISTS iterations_used INT DEFAULT 1"
            )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        put_conn(conn)


def save_query(record: dict[str, Any]) -> None:
    """Сохраняет один запрос из UI в query_history."""
    _ensure_iterations_column()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO query_history
                    (query, sql, gen_time, tokens_total, risk_score,
                     approved, summary, vulnerabilities, created_at, iterations_used)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                        to_timestamp(%s, 'DD.MM.YYYY HH24:MI:SS'), %s)
                """,
                (
                    record.get("query", ""),
                    record.get("sql", ""),
                    record.get("gen_time"),
                    record.get("tokens_total"),
                    record.get("risk_score"),
                    record.get("approved"),
                    record.get("summary", ""),
                    json.dumps(record.get("vulnerabilities", []), ensure_ascii=False),
                    record.get("timestamp", ""),
                    record.get("iterations_used", 1),
                ),
            )
        conn.commit()
    finally:
        put_conn(conn)


def load_history(limit: int = 200) -> list[dict[str, Any]]:
    """Загружает историю запросов (новые сначала)."""
    _ensure_iterations_column()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT query, sql, gen_time, tokens_total, risk_score,
                       approved, summary, vulnerabilities,
                       to_char(created_at, 'DD.MM.YYYY HH24:MI:SS') AS timestamp,
                       COALESCE(iterations_used, 1) AS iterations_used
                FROM query_history
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        return [dict(zip(cols, row)) for row in rows]
    finally:
        put_conn(conn)


def clear_history() -> None:
    """Удаляет всю историю."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM query_history")
        conn.commit()
    finally:
        put_conn(conn)


def get_tokens_used_today() -> int:
    """Сумма токенов за сегодня (по времени сервера PostgreSQL)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(tokens_total), 0)
                FROM query_history
                WHERE created_at >= CURRENT_DATE
                """
            )
            return int(cur.fetchone()[0])
    finally:
        put_conn(conn)