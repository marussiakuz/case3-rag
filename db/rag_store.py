"""
Хранилище RAG-эмбеддингов в PostgreSQL (таблица rag_embeddings).

Поиск: косинусное сходство через numpy — аналог FAISS IndexFlatIP,
но без бинарных файлов: всё хранится в БД gd_app.
"""
from __future__ import annotations

import json
from typing import Any

import numpy as np
import psycopg2.extras

from db.connection import get_conn, put_conn


# ── Запись ────────────────────────────────────────────────────────────────────

def upsert_index(index_name: str, texts: list[str],
                 metadata: list[dict], embeddings: np.ndarray) -> None:
    """
    Пересохраняет все векторы для index_name.
    Сначала удаляет старые записи с этим именем, потом вставляет новые.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM rag_embeddings WHERE index_name = %s", (index_name,))
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO rag_embeddings (index_name, text, metadata, embedding)
                VALUES %s
                """,
                [
                    (
                        index_name,
                        text,
                        json.dumps(meta, ensure_ascii=False),
                        embeddings[i].tolist(),
                    )
                    for i, (text, meta) in enumerate(zip(texts, metadata))
                ],
                template="(%s, %s, %s::jsonb, %s::float4[])",
            )
        conn.commit()
        print(f"  ✓ rag_embeddings[{index_name}]  {len(texts)} векторов")
    finally:
        put_conn(conn)


# ── Чтение и поиск ────────────────────────────────────────────────────────────

def search(index_name: str, query_vec: np.ndarray, top_k: int = 6) -> list[dict[str, Any]]:
    """
    Косинусный поиск по index_name.
    Загружает все векторы индекса (обычно < 1000), считает сходство через numpy.
    Возвращает top_k записей, отсортированных по убыванию сходства.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT text, metadata, embedding FROM rag_embeddings WHERE index_name = %s",
                (index_name,),
            )
            rows = cur.fetchall()
    finally:
        put_conn(conn)

    if not rows:
        return []

    texts = [r[0] for r in rows]
    metas = [r[1] for r in rows]
    matrix = np.array([r[2] for r in rows], dtype="float32")

    # Косинусное сходство (векторы нормализованы при записи)
    q = query_vec.astype("float32")
    q /= np.linalg.norm(q) + 1e-9
    scores = matrix @ q

    top_idx = np.argsort(scores)[::-1][:top_k]
    results = []
    for i in top_idx:
        entry = dict(metas[i])
        entry["text"] = texts[i]
        entry["score"] = float(scores[i])
        results.append(entry)
    return results


def insert_embedding(index_name: str, text: str, metadata: dict, embedding: np.ndarray) -> None:
    """Добавляет одну запись в индекс без удаления существующих (для динамического пополнения)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO rag_embeddings (index_name, text, metadata, embedding)
                VALUES (%s, %s, %s::jsonb, %s::float4[])
                """,
                (
                    index_name,
                    text,
                    json.dumps(metadata, ensure_ascii=False),
                    embedding.tolist(),
                ),
            )
        conn.commit()
    finally:
        put_conn(conn)


def load_all_metadata(index_name: str) -> list[dict[str, Any]]:
    """Возвращает все metadata-записи индекса (без эмбеддингов)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT metadata FROM rag_embeddings WHERE index_name = %s",
                (index_name,),
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        put_conn(conn)