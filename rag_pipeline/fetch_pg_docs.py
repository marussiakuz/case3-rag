"""
Скачивает ключевые разделы PostgreSQL 16 docs и нарезает их на чанки
для RAG generation index.

Результат: knowledge_base/generation/pg_docs.json

Запуск:
    python rag_pipeline/fetch_pg_docs.py

Зависимостей нет — только stdlib (urllib, html.parser, re, json).
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

OUT_PATH = Path(__file__).parent / "knowledge_base" / "generation" / "pg_docs.json"

# Разделы PG 16 docs, которые реально помогают генератору SQL
# (базовый SELECT/INSERT/UPDATE LLM уже знает, берём сложные/нишевые)
SECTIONS = [
    {
        "key": "queries-with",
        "url": "https://www.postgresql.org/docs/16/queries-with.html",
        "topic": "CTE (WITH Queries)",
    },
    {
        "key": "queries-table-expressions",
        "url": "https://www.postgresql.org/docs/16/queries-table-expressions.html",
        "topic": "JOIN и табличные выражения",
    },
    {
        "key": "functions-window",
        "url": "https://www.postgresql.org/docs/16/functions-window.html",
        "topic": "Оконные функции (Window Functions)",
    },
    {
        "key": "functions-aggregate",
        "url": "https://www.postgresql.org/docs/16/functions-aggregate.html",
        "topic": "Агрегатные функции",
    },
    {
        "key": "functions-datetime",
        "url": "https://www.postgresql.org/docs/16/functions-datetime.html",
        "topic": "Функции даты и времени",
    },
    {
        "key": "plpgsql-statements",
        "url": "https://www.postgresql.org/docs/16/plpgsql-statements.html",
        "topic": "PL/pgSQL: операторы (хранимые процедуры)",
    },
]

CHUNK_TARGET_CHARS = 2000   # ~500 токенов
CHUNK_MIN_CHARS = 200       # отбрасываем слишком короткие


# ── HTML → plain text ─────────────────────────────────────────────────────────

class _PGDocsParser(HTMLParser):
    """Извлекает структурированный текст из HTML страницы PG docs."""

    BLOCK_TAGS = {"p", "li", "dt", "dd", "pre", "code", "blockquote", "td", "th"}
    HEADING_TAGS = {"h1", "h2", "h3", "h4"}
    SKIP_TAGS = {"script", "style", "nav", "footer", "head", "toc"}

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._current_tag = ""
        self.chunks: list[dict] = []   # {"heading": str, "text": str}
        self._current_heading = ""
        self._current_lines: list[str] = []
        self._in_heading = False
        self._heading_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        self._current_tag = tag
        if tag in self.HEADING_TAGS:
            self._in_heading = True
            self._heading_buf = []
        if tag in self.BLOCK_TAGS and not self._in_heading:
            self._current_lines.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        if tag in self.HEADING_TAGS and self._in_heading:
            self._in_heading = False
            heading_text = "".join(self._heading_buf).strip()
            # При новом заголовке h2/h3 — сохраняем накопленный чанк
            if tag in {"h2", "h3"} and self._current_lines:
                body = _clean(" ".join(self._current_lines))
                if len(body) >= CHUNK_MIN_CHARS:
                    self.chunks.append({
                        "heading": self._current_heading,
                        "text": body,
                    })
                self._current_lines = []
            self._current_heading = heading_text

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_heading:
            self._heading_buf.append(data)
        else:
            self._current_lines.append(data)

    def finalize(self) -> None:
        """Сохраняем последний накопленный чанк."""
        if self._current_lines:
            body = _clean(" ".join(self._current_lines))
            if len(body) >= CHUNK_MIN_CHARS:
                self.chunks.append({
                    "heading": self._current_heading,
                    "text": body,
                })


def _clean(text: str) -> str:
    text = text.replace("\xa0", " ")          # неразрывный пробел
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r" \n ", "\n", text)
    return text.strip()


_NAV_PATTERNS = re.compile(
    r"Supported Versions|Development Versions|Unsupported versions"
    r"|postgresql\.org|Prev Up Chapter|Home Next",
    re.IGNORECASE,
)


# ── Нарезка на чанки ──────────────────────────────────────────────────────────

def _split_chunk(heading: str, text: str, topic: str) -> list[dict]:
    """
    Если чанк слишком большой — нарезаем по абзацам.
    Если маленький — оставляем как есть.
    """
    if len(text) <= CHUNK_TARGET_CHARS:
        return [{"heading": heading, "text": text, "topic": topic}]

    results = []
    paragraphs = re.split(r"\n{2,}", text)
    current = ""
    for para in paragraphs:
        if len(current) + len(para) < CHUNK_TARGET_CHARS:
            current += "\n\n" + para if current else para
        else:
            if len(current) >= CHUNK_MIN_CHARS:
                results.append({"heading": heading, "text": current.strip(), "topic": topic})
            current = para
    if len(current) >= CHUNK_MIN_CHARS:
        results.append({"heading": heading, "text": current.strip(), "topic": topic})

    return results or [{"heading": heading, "text": text[:CHUNK_TARGET_CHARS], "topic": topic}]


# ── Загрузка ──────────────────────────────────────────────────────────────────

def _fetch(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; GreenData-RAG/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_section(section: dict) -> list[dict]:
    print(f"  Загружаем: {section['key']} ...", end=" ", flush=True)
    html = _fetch(section["url"])

    parser = _PGDocsParser()
    parser.feed(html)
    parser.finalize()

    raw_chunks = parser.chunks
    final_chunks = []
    for chunk in raw_chunks:
        # Пропускаем навигационные блоки (пустой заголовок или nav-текст)
        if not chunk["heading"] or _NAV_PATTERNS.search(chunk["text"][:300]):
            continue
        final_chunks.extend(
            _split_chunk(chunk["heading"], chunk["text"], section["topic"])
        )

    # Добавляем метаданные
    for chunk in final_chunks:
        chunk["source"] = "pg_docs"
        chunk["section_key"] = section["key"]
        # Текст для эмбеддинга: заголовок + тело
        chunk["text"] = f"[PostgreSQL 16: {section['topic']}] {chunk['heading']}\n{chunk['text']}"

    print(f"{len(final_chunks)} чанков")
    return final_chunks


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    all_chunks: list[dict] = []
    failed: list[str] = []

    for section in SECTIONS:
        try:
            chunks = fetch_section(section)
            all_chunks.extend(chunks)
            time.sleep(0.5)  # вежливая пауза между запросами
        except Exception as e:
            print(f"ОШИБКА: {e}")
            failed.append(section["key"])

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(all_chunks, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    size_kb = OUT_PATH.stat().st_size / 1024
    print(f"\n✓ Сохранено: {OUT_PATH.name}")
    print(f"  Чанков всего: {len(all_chunks)}")
    print(f"  Размер файла: {size_kb:.0f} KB")
    if failed:
        print(f"  Не удалось загрузить: {failed}")

    # Статистика по разделам
    from collections import Counter
    by_section = Counter(c["section_key"] for c in all_chunks)
    for key, count in by_section.most_common():
        print(f"    {key}: {count} чанков")


if __name__ == "__main__":
    main()
