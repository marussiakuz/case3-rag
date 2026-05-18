"""
Строит три RAG-индекса из knowledge_base и сохраняет их в PostgreSQL (gd_app).

  generation — описания таблиц из schema.json + pg_patterns + pg_docs + task_anchors
  security   — классы уязвимостей из knowledge_base/security/vuln_classes.json
  performance — советы по оптимизации из knowledge_base/performance/pg_optimization.json

Вместо FAISS-файлов данные хранятся в таблице rag_embeddings (gd_app):
  - embedding: FLOAT4[] — нормализованный вектор (384 размерности)
  - поиск: косинусное сходство через numpy в db/rag_store.py

Модель: intfloat/multilingual-e5-small (поддерживает русский + SQL, ~90 МБ)

Запуск:
    python rag_pipeline/build_indices.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from db.rag_store import upsert_index

RAG_DIR = ROOT / "rag_pipeline"
KB_DIR = RAG_DIR / "knowledge_base"

MODEL_NAME = "intfloat/multilingual-e5-small"
PASSAGE_PREFIX = "passage: "


def _load_model() -> SentenceTransformer:
    print(f"Загружаем модель {MODEL_NAME} ...")
    return SentenceTransformer(MODEL_NAME)


def _embed(model: SentenceTransformer, texts: list[str]) -> np.ndarray:
    """Эмбеддинг с нужным префиксом и L2-нормализацией (→ cosine similarity)."""
    prefixed = [PASSAGE_PREFIX + t for t in texts]
    vecs = model.encode(prefixed, normalize_embeddings=True, show_progress_bar=True)
    return vecs.astype("float32")


def _save_index(vecs: np.ndarray, metadata: list[dict], name: str) -> None:
    """Сохраняет индекс в PostgreSQL (gd_app.rag_embeddings)."""
    texts = [m.get("text", "") for m in metadata]
    upsert_index(name, texts, metadata, vecs)


# ── Generation index ──────────────────────────────────────────────────────────

def build_generation_index(model: SentenceTransformer) -> None:
    print("\n[1/3] Строим generation index ...")
    documents: list[dict] = []

    # Источник A: описания таблиц из schema.json
    schema_path = ROOT / "schema.json"
    if not schema_path.exists():
        raise FileNotFoundError(f"schema.json не найден: {schema_path}\nЗапусти сначала: python rag_pipeline/schema_parser.py")

    # Загружаем примеры задач из датасета заранее — для обогащения schema-чанков
    from collections import defaultdict
    table_tasks: dict[str, list[str]] = defaultdict(list)
    dataset_path = ROOT / "validation" / "dataset.json"
    if dataset_path.exists():
        for item in json.loads(dataset_path.read_text(encoding="utf-8")):
            table_tasks[item["table"]].append(item["task"])
        print(f"  → Примеры задач из dataset.json: {sum(len(v) for v in table_tasks.values())} задач")

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    for table_name, table_data in schema["tables"].items():
        text = table_data.get("text_description", "")
        if not text:
            continue
        # Обогащаем чанк примерами задач из датасета — улучшает семантический поиск:
        # FAISS будет находить нужную таблицу даже когда запрос содержит слова
        # типа «иерархия», «CTE», «НДС», не упомянутые в описании таблицы.
        if table_tasks.get(table_name):
            tasks_text = "\n".join(f"- {t}" for t in table_tasks[table_name])
            text += f"\nПримеры задач для этой таблицы:\n{tasks_text}"
        documents.append({
            "source": "schema",
            "table_name": table_name,
            "text": text,
            "has_sensitive": bool(
                [c for c, v in table_data["columns"].items() if v.get("is_sensitive")]
            ),
        })

    print(f"  → Таблиц из schema.json: {len(documents)}")

    # Источник B: кастомные PostgreSQL паттерны (pg_patterns.json)
    pg_path = KB_DIR / "generation" / "pg_patterns.json"
    pg_patterns = json.loads(pg_path.read_text(encoding="utf-8"))
    for pattern in pg_patterns:
        text = pattern["text"]
        if pattern.get("example"):
            text += "\nПример:\n" + pattern["example"]
        documents.append({
            "source": "pg_pattern",
            "pattern_id": pattern["pattern_id"],
            "pattern_type": pattern["pattern_type"],
            "description": pattern["description"],
            "text": text,
        })

    print(f"  → Кастомных паттернов: {len(pg_patterns)}")

    # Источник C: чанки из PostgreSQL 16 docs (если скачаны)
    docs_path = KB_DIR / "generation" / "pg_docs.json"
    if docs_path.exists():
        pg_docs = json.loads(docs_path.read_text(encoding="utf-8"))
        for chunk in pg_docs:
            documents.append({
                "source": "pg_docs",
                "section_key": chunk.get("section_key", ""),
                "topic": chunk.get("topic", ""),
                "heading": chunk.get("heading", ""),
                "text": chunk["text"],
            })
        print(f"  → Чанков из PG docs: {len(pg_docs)}")
    else:
        print("  → PG docs не найдены (запусти fetch_pg_docs.py)")

    # Источник D: task_anchor — один вектор на каждую задачу из датасета.
    # Когда FAISS находит task_anchor, _format_generation_context подтягивает
    # полную schema-запись для этой таблицы. Это решает проблему «размытия»
    # эмбеддинга когда все 10 задач таблицы слиты в один вектор.
    if dataset_path.exists():
        for item in json.loads(dataset_path.read_text(encoding="utf-8")):
            documents.append({
                "source": "task_anchor",
                "table_name": item["table"],
                "text": item["task"],
            })
        n_anchors = sum(1 for d in documents if d["source"] == "task_anchor")
        print(f"  → Task anchors из dataset.json: {n_anchors}")

    print(f"  → Итого документов: {len(documents)}")

    texts = [d["text"] for d in documents]
    vecs = _embed(model, texts)
    _save_index(vecs, documents, "generation")


# ── Security index ────────────────────────────────────────────────────────────

def build_security_index(model: SentenceTransformer) -> None:
    print("\n[2/3] Строим security index ...")
    documents: list[dict] = []

    vuln_path = KB_DIR / "security" / "vuln_classes.json"
    vuln_classes = json.loads(vuln_path.read_text(encoding="utf-8"))

    for vuln in vuln_classes:
        # Основной документ — полное описание уязвимости
        documents.append({
            "source": "vuln_class",
            "vuln_class": vuln["vuln_class"],
            "name": vuln["name"],
            "risk_score": vuln["risk_score"],
            "recommendation": vuln.get("recommendation", ""),
            "text": vuln["text"],
        })

        # Дополнительный документ — bad/good примеры (лучше находятся по SQL-коду)
        if vuln.get("example_bad"):
            example_text = (
                f"Уязвимость {vuln['name']} (риск {vuln['risk_score']}/10).\n"
                f"Опасный пример:\n{vuln['example_bad']}\n"
                f"Безопасный вариант:\n{vuln.get('example_good', '')}"
            )
            documents.append({
                "source": "vuln_example",
                "vuln_class": vuln["vuln_class"],
                "name": vuln["name"],
                "risk_score": vuln["risk_score"],
                "recommendation": vuln.get("recommendation", ""),
                "text": example_text,
            })

    print(f"  → Документов по уязвимостям: {len(documents)}")

    texts = [d["text"] for d in documents]
    vecs = _embed(model, texts)
    _save_index(vecs, documents, "security")


# ── Performance index ─────────────────────────────────────────────────────────

def build_performance_index(model: SentenceTransformer) -> None:
    print("\n[3/3] Строим performance index ...")
    documents: list[dict] = []

    opt_path = KB_DIR / "performance" / "pg_optimization.json"
    if not opt_path.exists():
        print(f"  → Файл не найден: {opt_path}")
        return

    optimizations = json.loads(opt_path.read_text(encoding="utf-8"))
    for opt in optimizations:
        # Основной документ — полное описание техники
        full_text = (
            f"{opt['name']}. {opt['description']}\n"
            f"Категория: {opt['category']}\n"
            f"Когда применять: {opt['applies_when']}\n"
            f"{opt['text']}"
        )
        documents.append({
            "source": "pg_optimization",
            "optimization_id": opt["optimization_id"],
            "category": opt["category"],
            "name": opt["name"],
            "applies_when": opt["applies_when"],
            "text": full_text,
        })

        # Дополнительный документ — пример (лучше находится по SQL-коду)
        if opt.get("example_good"):
            example_text = (
                f"Пример оптимизации: {opt['name']}\n"
                f"{opt['example_good']}"
            )
            documents.append({
                "source": "pg_optimization_example",
                "optimization_id": opt["optimization_id"],
                "category": opt["category"],
                "name": opt["name"],
                "applies_when": opt["applies_when"],
                "text": example_text,
            })

    print(f"  → Документов по оптимизации: {len(documents)}")

    texts = [d["text"] for d in documents]
    vecs = _embed(model, texts)
    _save_index(vecs, documents, "performance")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    model = _load_model()
    build_generation_index(model)
    build_security_index(model)
    build_performance_index(model)
    print("\n✓ Все индексы построены и сохранены в PostgreSQL (gd_app.rag_embeddings).")


if __name__ == "__main__":
    main()
