"""
SQL-генератор на базе Llama 3.3 70B (Cerebras API) + RAG-контекст.

Реализует контракт SQLGenerator из baseline1.py.

Использование:
    from generator import GroqSQLGenerator

    gen = GroqSQLGenerator()
    sql = gen.generate("показать топ-10 активных сотрудников по фамилии")
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))
from baseline1 import AuditResult, SQLGenerator
from rag_pipeline.rag_tools import get_generation_context, get_solutions_context

ROOT = Path(__file__).parent.parent

# Cerebras быстрее и без лимитов free-tier; OpenRouter — фолбэк для VM (geo-блок)
# Несколько ключей Cerebras через запятую: CEREBRAS_API_KEYS=key1,key2,key3
_CEREBRAS_KEYS: list[str] = [
    k.strip()
    for k in os.getenv("CEREBRAS_API_KEYS", os.getenv("CEREBRAS_API_KEY", "")).split(",")
    if k.strip()
]

if _CEREBRAS_KEYS:
    _API_BASE = "https://api.cerebras.ai/v1"
    MODEL = os.getenv("CEREBRAS_MODEL", "qwen-3-235b-a22b-instruct-2507")
else:
    _API_BASE = "https://openrouter.ai/api/v1"
    MODEL = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash:free")


def _make_client(key: str) -> "OpenAI":
    proxy = os.getenv("CEREBRAS_PROXY") if _CEREBRAS_KEYS else None
    http = httpx.Client(proxy=proxy, timeout=60) if proxy else None
    base = _API_BASE if _CEREBRAS_KEYS else "https://openrouter.ai/api/v1"
    api_key = key if _CEREBRAS_KEYS else os.getenv("OPENROUTER_API_KEY")
    return OpenAI(base_url=base, api_key=api_key, http_client=http)

TEMPERATURE = 0.1
MAX_TOKENS = 512  # SQL не длиннее 512 токенов — ограничивает thinking mode Qwen3

# Компактная схема — загружается один раз при старте модуля
_SCHEMA_COMPACT: dict = {}
_schema_path = ROOT / "schema_compact.json"
if _schema_path.exists():
    import json as _json
    _SCHEMA_COMPACT = _json.loads(_schema_path.read_text(encoding="utf-8"))


def _schema_summary() -> str:
    """Возвращает только список имён таблиц — экономит токены, предотвращает галлюцинации."""
    if not _SCHEMA_COMPACT:
        return ""
    table_names = ", ".join(_SCHEMA_COMPACT.keys())
    return f"=== ДОПУСТИМЫЕ ТАБЛИЦЫ В БД (использовать ТОЛЬКО эти) ===\n{table_names}"


SYSTEM_PROMPT = """\
/no_think
Ты эксперт по PostgreSQL, работающий с банковской системой GreenData.

Твоя задача — написать корректный SQL-запрос по описанию задачи.

Обязательные правила:
1. Возвращай ТОЛЬКО SQL-запрос — никаких пояснений, markdown-блоков, комментариев, точки с запятой.
2. Всегда добавляй LIMIT 100, если в задаче не указано другое число.
3. Никогда не используй SELECT *.
4. Если в задаче явно указано конкретное значение (например, id = 100, статус = 1), вставляй его напрямую. Плейсхолдеры ($1, $2, ...) — только для значений, которые в задаче не указаны.
5. Используй ТОЛЬКО таблицы и колонки из контекста схемы ниже — никаких других таблиц.
6. Запрос должен быть читаемым: выравнивание, переносы строк.
7. Выбирай колонки: первичный ключ (id) + все колонки, явно упомянутые в задаче + колонки из условий WHERE/фильтров, если они логически являются частью результата (например, если фильтруешь по status=1 — включи status в SELECT). Не добавляй служебные ключи внешних связей (type_id, org_id, created_emp_id) если задача о них явно не спрашивает.
8. Если задача явно требует определённую технику SQL (оконные функции, CTE, подзапрос), используй именно её — не упрощай до GROUP BY или других подходов.
9. Для имён объектов используй колонку `name` (не `name__ru`, не `name__en`), если задача не требует конкретный язык. Для дат создания — `create_date`, для дат изменения — `last_modified_date`.
"""


def _primary_table(rag_context: str) -> str:
    """Извлекает имя первой (наиболее релевантной) таблицы из RAG-контекста."""
    for line in rag_context.splitlines():
        if line.startswith("Таблица:"):
            return line.split(":", 1)[1].strip()
    return ""


def _build_user_prompt(
    task_description: str,
    rag_context: str,
    sql_history: list[str] | None,
    audit_feedback: AuditResult | None,
    iteration: int,
    solutions_context: str = "",
) -> str:
    parts: list[str] = []

    # Уроки из похожих задач (от Opus мета-аудита) — первыми, до схемы
    if iteration == 1 and solutions_context:
        parts.append(solutions_context)

    # Полная схема всегда первой — модель знает какие таблицы существуют
    schema = _schema_summary()
    if schema:
        parts.append(schema)

    parts.append(rag_context)

    if iteration > 1 and sql_history:
        parts.append("\n=== ПРЕДЫДУЩИЕ ПОПЫТКИ (не повторять) ===")
        for i, prev_sql in enumerate(sql_history, start=1):
            parts.append(f"Попытка {i}:\n{prev_sql}")

    if audit_feedback and audit_feedback.vulnerabilities:
        parts.append("\n=== ПРОБЛЕМЫ В ПОСЛЕДНЕМ ЗАПРОСЕ (исправить) ===")
        for v in audit_feedback.vulnerabilities:
            parts.append(
                f"• [{v.vuln_class}] риск {v.risk_score}/10\n"
                f"  Проблема: {v.description}\n"
                f"  Исправление: {v.recommendation}"
            )

    primary = _primary_table(rag_context)
    hint = f" (основная таблица: {primary})" if primary else ""
    parts.append(f"\n=== ЗАДАЧА{hint} ===\n{task_description}")
    parts.append("\nНапиши SQL-запрос:")

    return "\n".join(parts)


def _clean_sql(raw: str) -> str:
    """Убирает markdown-блоки, точки с запятой и лишние пробелы из ответа модели."""
    cleaned = re.sub(r"```(?:sql)?", "", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"```", "", cleaned)
    # Модель иногда пишет "ORDER BY col;\nLIMIT N" — точка с запятой разбивает запрос
    cleaned = re.sub(r";+", "", cleaned)
    return cleaned.strip()


class GroqSQLGenerator(SQLGenerator):
    """
    Реализация SQLGenerator на базе OpenRouter API (Qwen3 235B).

    Args:
        db_schema: схема БД (не используется напрямую — RAG обращается к schema.json)
        model: название модели OpenRouter
        temperature: температура генерации (по умолчанию 0.1 для детерминизма)
    """

    def __init__(
        self,
        db_schema: dict[str, Any] | None = None,
        model: str = MODEL,
        temperature: float = TEMPERATURE,
        **kwargs: Any,
    ) -> None:
        super().__init__(db_schema=db_schema, **kwargs)
        self.model = model
        self.temperature = temperature
        self._key_idx = 0
        self._client = _make_client(_CEREBRAS_KEYS[0] if _CEREBRAS_KEYS else "")
        self.last_usage: dict = {}
        proxy = os.getenv("CEREBRAS_PROXY") if _CEREBRAS_KEYS else None
        backend = f"Cerebras×{len(_CEREBRAS_KEYS)}" + (" (proxy)" if proxy else "") if _CEREBRAS_KEYS else "OpenRouter"
        print(f"  [Generator] {backend} · модель: {self.model}")

    def generate(
        self,
        task_description: str,
        sql_history: list[str] | None = None,
        audit_feedback: AuditResult | None = None,
        iteration: int = 1,
    ) -> str:
        """
        Генерирует SQL-запрос по описанию задачи.

        Args:
            task_description: задача на естественном языке
            sql_history: список предыдущих SQL-запросов (неудачные попытки)
            audit_feedback: результат аудита последнего запроса
            iteration: номер текущей итерации

        Returns:
            Строка с SQL-запросом
        """
        rag_context = get_generation_context(task_description, top_k=6)
        solutions_context = get_solutions_context(task_description, top_k=3)

        user_prompt = _build_user_prompt(
            task_description=task_description,
            rag_context=rag_context,
            sql_history=sql_history,
            audit_feedback=audit_feedback,
            iteration=iteration,
            solutions_context=solutions_context,
        )

        import time as _time
        response = None
        for _attempt in range(4):
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    temperature=self.temperature,
                    max_tokens=MAX_TOKENS,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                )
                break
            except Exception as _e:
                err = str(_e)
                _is_quota = "daily" in err.lower() or "token_limit" in err.lower() or "quota" in err.lower()
                if _is_quota and _CEREBRAS_KEYS:
                    if self._key_idx + 1 < len(_CEREBRAS_KEYS):
                        # Следующий Cerebras-ключ
                        self._key_idx += 1
                        self._client = _make_client(_CEREBRAS_KEYS[self._key_idx])
                        print(f"  [Generator] ключ исчерпан → ключ #{self._key_idx + 1}/{len(_CEREBRAS_KEYS)}")
                        continue
                    elif os.getenv("OPENROUTER_API_KEY"):
                        # Все Cerebras-ключи исчерпаны → фолбэк на OpenRouter
                        print("  [Generator] все Cerebras-ключи исчерпаны → OpenRouter фолбэк")
                        self.model = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash:free")
                        self._client = OpenAI(
                            base_url="https://openrouter.ai/api/v1",
                            api_key=os.getenv("OPENROUTER_API_KEY"),
                        )
                        continue
                if _attempt == 3:
                    raise
                _wait = 5
                try:
                    _wait = int(err.split("retry_after_seconds\": ")[1].split(",")[0].split(".")[0]) + 2
                except Exception:
                    pass
                print(f"  [Generator] rate limit, жду {_wait}с...")
                _time.sleep(_wait)

        if response is None:
            raise RuntimeError("Все попытки обращения к API завершились неудачей")

        usage = getattr(response, "usage", None)
        self.last_usage = {
            "prompt_tokens":     getattr(usage, "prompt_tokens",     0) or 0 if usage else 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0 if usage else 0,
            "total_tokens":      getattr(usage, "total_tokens",      0) or 0 if usage else 0,
            "remaining_tokens":  None,
        }
        raw = response.choices[0].message.content or ""
        return _clean_sql(raw)


# ── Быстрая проверка ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    gen = GroqSQLGenerator()

    tasks = [
        "показать топ-10 активных сотрудников, отсортированных по фамилии",
        "вывести все кредитные договоры за последние 30 дней с суммой больше 1 миллиона",
        "посчитать количество заявок по каждому статусу за текущий квартал",
    ]

    for task in tasks:
        print(f"\nЗадача: {task}")
        print("─" * 60)
        sql = gen.generate(task)
        print(sql)
