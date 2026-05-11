"""
Строит два FAISS-индекса из knowledge_base:

  indices/generation.faiss + indices/generation_meta.json
      — описания таблиц из schema.json
      — PostgreSQL паттерны из knowledge_base/generation/pg_patterns.json

  indices/security.faiss + indices/security_meta.json
      — классы уязвимостей из knowledge_base/security/vuln_classes.json

Модель: intfloat/multilingual-e5-small (поддерживает русский + SQL, ~90 МБ)

Запуск:
    python rag_pipeline/build_indices.py
"""

from __future__ import annotations

import json
from pathlib import Path

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer

ROOT = Path(__file__).parent.parent
RAG_DIR = ROOT / "rag_pipeline"
INDICES_DIR = RAG_DIR / "indices"
KB_DIR = RAG_DIR / "knowledge_base"

MODEL_NAME = "intfloat/multilingual-e5-small"
PASSAGE_PREFIX = "passage: "  # обязательный префикс для этой модели


def _load_model() -> SentenceTransformer:
    print(f"Загружаем модель {MODEL_NAME} ...")
    return SentenceTransformer(MODEL_NAME)


def _embed(model: SentenceTransformer, texts: list[str]) -> np.ndarray:
    """Эмбеддинг с нужным префиксом и L2-нормализацией (→ cosine similarity)."""
    prefixed = [PASSAGE_PREFIX + t for t in texts]
    vecs = model.encode(prefixed, normalize_embeddings=True, show_progress_bar=True)
    return vecs.astype("float32")


def _save_index(vecs: np.ndarray, metadata: list[dict], name: str) -> None:
    """Сохраняет FAISS IndexFlatIP (inner product = cosine для нормализованных) + JSON-мета."""
    INDICES_DIR.mkdir(parents=True, exist_ok=True)
    dim = vecs.shape[1]
    index = faiss.IndexFlatIP(dim)
    index.add(vecs)

    faiss_path = INDICES_DIR / f"{name}.faiss"
    meta_path = INDICES_DIR / f"{name}_meta.json"

    faiss.write_index(index, str(faiss_path))
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"  ✓ {faiss_path.name}  ({index.ntotal} векторов, dim={dim})")
    print(f"  ✓ {meta_path.name}")


# ── Generation index ──────────────────────────────────────────────────────────

def build_generation_index(model: SentenceTransformer) -> None:
    print("\n[1/2] Строим generation index ...")
    documents: list[dict] = []

    # Источник A: описания таблиц из schema.json
    schema_path = ROOT / "schema.json"
    if not schema_path.exists():
        raise FileNotFoundError(f"schema.json не найден: {schema_path}\nЗапусти сначала: python rag_pipeline/schema_parser.py")

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    for table_name, table_data in schema["tables"].items():
        text = table_data.get("text_description", "")
        if not text:
            continue
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

    print(f"  → Итого документов: {len(documents)}")

    texts = [d["text"] for d in documents]
    vecs = _embed(model, texts)
    _save_index(vecs, documents, "generation")


# ── Security index ────────────────────────────────────────────────────────────

def build_security_index(model: SentenceTransformer) -> None:
    print("\n[2/2] Строим security index ...")
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


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    model = _load_model()
    build_generation_index(model)
    build_security_index(model)
    print("\n✓ Все индексы построены.")
    print(f"  Папка: {INDICES_DIR}")


if __name__ == "__main__":
    main()
