"""
RAG-инструменты для SQL-генератора и аудитора безопасности.

Публичный API (вызывается из оркестратора):

    from rag_pipeline.rag_tools import get_generation_context, get_security_context

    # Контекст для генератора SQL
    context = get_generation_context("выгрузить активных сотрудников по подразделениям")

    # Контекст для судьи
    security_ctx = get_security_context("SELECT * FROM sys_employee WHERE id = '\" + x + \"'")

Перед использованием необходимо построить индексы:
    python rag_pipeline/build_indices.py
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer

# ── Конфигурация ──────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent
INDICES_DIR = Path(__file__).parent / "indices"

MODEL_NAME = "intfloat/multilingual-e5-small"
QUERY_PREFIX = "query: "      # префикс для запросов (не документов!)

DEFAULT_TOP_K_GENERATION = 6
DEFAULT_TOP_K_SECURITY = 4


# ── Загрузка (ленивая, singleton) ─────────────────────────────────────────────

@lru_cache(maxsize=1)
def _get_model() -> SentenceTransformer:
    """Модель загружается один раз при первом вызове."""
    return SentenceTransformer(MODEL_NAME)


@lru_cache(maxsize=1)
def _load_generation_index() -> tuple[faiss.Index, list[dict]]:
    return _load_index("generation")


@lru_cache(maxsize=1)
def _load_security_index() -> tuple[faiss.Index, list[dict]]:
    return _load_index("security")


def _load_index(name: str) -> tuple[faiss.Index, list[dict]]:
    faiss_path = INDICES_DIR / f"{name}.faiss"
    meta_path = INDICES_DIR / f"{name}_meta.json"

    if not faiss_path.exists():
        raise FileNotFoundError(
            f"Индекс '{name}' не найден: {faiss_path}\n"
            "Запусти: python rag_pipeline/build_indices.py"
        )

    index = faiss.read_index(str(faiss_path))
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    return index, metadata


# ── Поиск ─────────────────────────────────────────────────────────────────────

def _search(
    query: str,
    index: faiss.Index,
    metadata: list[dict],
    top_k: int,
) -> list[dict[str, Any]]:
    model = _get_model()
    q_vec = model.encode(
        [QUERY_PREFIX + query],
        normalize_embeddings=True,
    ).astype("float32")

    scores, indices = index.search(q_vec, top_k)
    results = []
    for score, idx in zip(scores[0], indices[0]):
        if idx >= 0:
            results.append({"score": float(score), **metadata[idx]})
    return results


# ── Форматирование результатов ─────────────────────────────────────────────────

def _format_generation_context(results: list[dict]) -> str:
    """
    Собирает контекст для SQL-генератора.
    Разделяет таблицы и паттерны PostgreSQL.
    """
    tables = [r for r in results if r.get("source") == "schema"]
    patterns = [r for r in results if r.get("source") == "pg_pattern"]

    parts: list[str] = []

    if tables:
        parts.append("=== РЕЛЕВАНТНЫЕ ТАБЛИЦЫ БАЗЫ ДАННЫХ ===")
        for r in tables:
            parts.append(r["text"])
            parts.append("")  # пустая строка-разделитель

    if patterns:
        parts.append("=== РЕКОМЕНДУЕМЫЕ ПАТТЕРНЫ SQL ===")
        for r in patterns:
            parts.append(f"[{r['pattern_type']}] {r['description']}")
            parts.append(r["text"])
            parts.append("")

    return "\n".join(parts).strip()


def _format_security_context(results: list[dict]) -> str:
    """
    Собирает контекст для аудитора безопасности.
    Включает описания уязвимостей и примеры.
    """
    seen_classes: set[str] = set()
    parts: list[str] = ["=== РЕЛЕВАНТНЫЕ КЛАССЫ УЯЗВИМОСТЕЙ ==="]

    for r in results:
        vuln_class = r.get("vuln_class", "")
        # Показываем каждый класс только один раз (из двух документов — основной + пример)
        if vuln_class not in seen_classes:
            parts.append(
                f"[{vuln_class}] риск {r.get('risk_score', '?')}/10\n"
                f"{r['text']}"
            )
            parts.append("")
            seen_classes.add(vuln_class)

    if r.get("recommendation"):
        parts.append(f"Рекомендация: {r['recommendation']}")

    return "\n".join(parts).strip()


# ── Публичный API ─────────────────────────────────────────────────────────────

def get_generation_context(
    task_description: str,
    top_k: int = DEFAULT_TOP_K_GENERATION,
) -> str:
    """
    Возвращает отформатированный контекст для SQL-генератора.

    Содержит:
    - описания релевантных таблиц (схема, колонки, FK)
    - рекомендуемые паттерны PostgreSQL

    Args:
        task_description: Задача на естественном языке (на русском)
        top_k: Количество документов для извлечения

    Returns:
        Строка с контекстом для инжекции в system prompt генератора
    """
    index, metadata = _load_generation_index()
    results = _search(task_description, index, metadata, top_k)
    return _format_generation_context(results)


def get_security_context(
    sql_query: str,
    top_k: int = DEFAULT_TOP_K_SECURITY,
) -> str:
    """
    Возвращает отформатированный контекст для аудитора безопасности.

    Содержит классы уязвимостей, наиболее релевантные для данного SQL-запроса.

    Args:
        sql_query: SQL-запрос для аудита
        top_k: Количество классов уязвимостей для проверки

    Returns:
        Строка с описанием уязвимостей для инжекции в промт аудитора
    """
    index, metadata = _load_security_index()
    results = _search(sql_query, index, metadata, top_k * 2)  # берём с запасом, потом дедупликация
    return _format_security_context(results)


def get_table_context(table_names: list[str]) -> str:
    """
    Возвращает описания конкретных таблиц по имени (без семантического поиска).
    Полезно когда генератор/аудитор знает точные имена таблиц.

    Args:
        table_names: Список имён таблиц, например ['sys_employee', 'sys_company']
    """
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


def get_sensitive_fields() -> dict[str, list[str]]:
    """
    Возвращает словарь sensitive-полей из schema.json.
    Используется SecurityAuditor для проверки DIRECT_SENSITIVE.

    Returns:
        {'sys_employee': ['email', 'phone', ...], 'sys_company': ['inn', ...], ...}
    """
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

    print("\n=== Тест get_sensitive_fields ===")
    sens = get_sensitive_fields()
    for tname, cols in sens.items():
        print(f"  {tname}: {cols}")
