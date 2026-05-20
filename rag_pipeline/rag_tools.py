"""
RAG-инструменты для SQL-генератора, аудитора безопасности и оптимизатора.

Векторы хранятся в PostgreSQL (gd_app.rag_embeddings).
Поиск: косинусное сходство через numpy в db/rag_store.py.

Публичный API:
    from rag_pipeline.rag_tools import (
        get_generation_context,
        get_security_context,
        get_performance_context,
    )
"""

from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import os

import torch
from sentence_transformers import SentenceTransformer

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from db.rag_store import load_all_metadata, search as pg_search

MODEL_NAME = "intfloat/multilingual-e5-small"
QUERY_PREFIX = "query: "

DEFAULT_TOP_K_GENERATION = 6
DEFAULT_TOP_K_SECURITY = 4
DEFAULT_TOP_K_PERFORMANCE = 3


# ── Модель (singleton) ────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _get_model() -> SentenceTransformer:
    # используем все доступные CPU-ядра для инференса
    n_threads = os.cpu_count() or 4
    torch.set_num_threads(n_threads)
    return SentenceTransformer(MODEL_NAME)


@lru_cache(maxsize=8)
def _get_all_metadata(index_name: str) -> list[dict]:
    """Метаданные индекса кешируются — они не меняются в рантайме."""
    return load_all_metadata(index_name)


# ── Поиск ─────────────────────────────────────────────────────────────────────

def _search(query: str, index_name: str, top_k: int) -> list[dict[str, Any]]:
    model = _get_model()
    q_vec = model.encode(
        [QUERY_PREFIX + query],
        normalize_embeddings=True,
    ).astype("float32")[0]
    return pg_search(index_name, q_vec, top_k)


# ── Форматирование результатов ─────────────────────────────────────────────────

def _format_generation_context(results: list[dict], index_name: str = "generation") -> str:
    """
    Собирает контекст для SQL-генератора.
    task_anchor результаты → подтягивают полную schema для этой таблицы.
    """
    all_metadata = _get_all_metadata(index_name)
    schema_by_table: dict[str, dict] = {
        m["table_name"]: m
        for m in all_metadata
        if m.get("source") == "schema" and m.get("table_name")
    }

    seen_tables: set[str] = set()
    tables: list[dict] = []
    patterns: list[dict] = []

    for r in results:
        src = r.get("source", "")
        if src == "schema":
            tname = r.get("table_name", "")
            if tname not in seen_tables:
                seen_tables.add(tname)
                tables.append(r)
        elif src == "task_anchor":
            tname = r.get("table_name", "")
            if tname and tname not in seen_tables and tname in schema_by_table:
                seen_tables.add(tname)
                tables.append(schema_by_table[tname])
        elif src == "pg_pattern":
            patterns.append(r)

    parts: list[str] = []

    if tables:
        parts.append("=== РЕЛЕВАНТНЫЕ ТАБЛИЦЫ БАЗЫ ДАННЫХ ===")
        for r in tables:
            parts.append(r["text"])
            parts.append("")

    if patterns:
        parts.append("=== РЕКОМЕНДУЕМЫЕ ПАТТЕРНЫ SQL ===")
        for r in patterns:
            parts.append(f"[{r['pattern_type']}] {r['description']}")
            parts.append(r["text"])
            parts.append("")

    return "\n".join(parts).strip()


def _format_security_context(results: list[dict]) -> str:
    seen_classes: set[str] = set()
    parts: list[str] = ["=== РЕЛЕВАНТНЫЕ КЛАССЫ УЯЗВИМОСТЕЙ ==="]

    for r in results:
        vuln_class = r.get("vuln_class", "")
        if vuln_class not in seen_classes:
            parts.append(
                f"[{vuln_class}] риск {r.get('risk_score', '?')}/10\n"
                f"{r['text']}"
            )
            parts.append("")
            seen_classes.add(vuln_class)

    return "\n".join(parts).strip()


def _format_performance_context(results: list[dict]) -> str:
    seen_ids: set[str] = set()
    parts: list[str] = ["=== РЕКОМЕНДАЦИИ ПО ОПТИМИЗАЦИИ ==="]

    for r in results:
        opt_id = r.get("optimization_id", "")
        if opt_id not in seen_ids:
            parts.append(
                f"[{r.get('category', '').upper()}] {r.get('name', '')}\n"
                f"Когда применять: {r.get('applies_when', '')}\n"
                f"{r['text']}"
            )
            parts.append("")
            seen_ids.add(opt_id)

    return "\n".join(parts).strip()


# ── Публичный API ─────────────────────────────────────────────────────────────

def get_generation_context(task_description: str, top_k: int = DEFAULT_TOP_K_GENERATION) -> str:
    results = _search(task_description, "generation", top_k)
    return _format_generation_context(results)


def get_security_context(sql_query: str, top_k: int = DEFAULT_TOP_K_SECURITY) -> str:
    results = _search(sql_query, "security", top_k * 2)
    return _format_security_context(results)


def get_performance_context(sql_query: str, top_k: int = DEFAULT_TOP_K_PERFORMANCE) -> str:
    results = _search(sql_query, "performance", top_k * 2)
    return _format_performance_context(results)


def get_table_context(table_names: list[str]) -> str:
    schema_path = ROOT / "schema.json"
    if not schema_path.exists():
        return ""
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    parts = ["=== ОПИСАНИЯ ТАБЛИЦ ==="]
    for tname in table_names:
        if tname in schema["tables"]:
            parts.append(schema["tables"][tname]["text_description"])
            parts.append("")
    return "\n".join(parts).strip()


def _format_solutions_context(results: list[dict]) -> str:
    seen: set[str] = set()
    parts: list[str] = ["=== УРОКИ ИЗ ПОХОЖИХ ЗАДАЧ (анализ Opus) ==="]

    for r in results:
        lesson = r.get("lesson_for_generator", "")
        if not lesson or lesson in seen:
            continue
        seen.add(lesson)
        task_type = r.get("task_type", "")
        approach = r.get("correct_sql_approach", "")
        errors = r.get("generator_errors", [])

        parts.append(f"[Тип: {task_type}]")
        if errors:
            parts.append(f"Типичные ошибки: {'; '.join(errors[:3])}")
        if approach:
            parts.append(f"Правильный подход: {approach}")
        parts.append(f"Урок: {lesson}")
        parts.append("")

    if len(parts) == 1:
        return ""
    return "\n".join(parts).strip()


def get_solutions_context(task_description: str, top_k: int = 3) -> str:
    """
    Извлекает уроки из RAG-индекса 'solutions' (разборы Opus по похожим задачам).

    Возвращает пустую строку если индекс пуст (до первых мета-аудитов).
    """
    try:
        results = _search(task_description, "solutions", top_k)
        return _format_solutions_context(results)
    except Exception:
        return ""


def get_sensitive_fields() -> dict[str, list[str]]:
    schema_path = ROOT / "schema.json"
    if not schema_path.exists():
        return {}
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return schema.get("sensitive_fields_summary", {})


# ── Быстрая проверка ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Тест get_generation_context ===")
    ctx = get_generation_context("показать активных сотрудников по подразделениям")
    print(ctx[:800], "...")

    print("\n=== Тест get_security_context ===")
    bad_sql = "SELECT * FROM sys_employee WHERE name = '" + "' OR '1'='1" + "'"
    sec = get_security_context(bad_sql)
    print(sec[:800], "...")