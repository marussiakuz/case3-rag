"""
Генерирует валидационный датасет: 60 таблиц × 10 задач = 600 пар (task, reference_sql).

Поддерживает возобновление: если таблица уже есть в dataset.json — пропускается.
При rate limit — ждёт и повторяет автоматически.

Вывод: validation/dataset.json

Запуск:
    .venv/bin/python validation/generate_dataset.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from cerebras.cloud.sdk import Cerebras
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

OUTPUT_PATH = ROOT / "validation" / "dataset.json"
# Cerebras: llama-3.3-70b без жёстких суточных лимитов Groq
MODEL = "qwen-3-235b-a22b-instruct-2507"
TASKS_PER_TABLE = 10
RATE_LIMIT_DELAY = 1.0      # базовая пауза между запросами (Cerebras более свободный)
RETRY_DELAYS = [5, 15, 30]  # паузы при rate limit (сек)


def _load_existing() -> tuple[list[dict], set[str]]:
    """Загружает уже сгенерированные пары и возвращает множество готовых таблиц."""
    if OUTPUT_PATH.exists():
        data = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        done_tables = {item["table"] for item in data}
        return data, done_tables
    return [], set()


def _save(dataset: list[dict]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(dataset, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_schema() -> dict:
    return json.loads((ROOT / "schema_compact.json").read_text(encoding="utf-8"))


def _table_prompt(table_name: str, table_info: dict) -> str:
    cols = table_info.get("cols", {})
    fk = table_info.get("fk", [])
    desc = table_info.get("desc", "")
    pk = table_info.get("pk", "id")

    col_lines = "\n".join(f"  {col}: {spec}" for col, spec in cols.items())
    fk_lines = "\n".join(f"  {f}" for f in fk) if fk else "  нет"

    return f"""Таблица PostgreSQL: {table_name}
Описание: {desc}
Первичный ключ: {pk}

Колонки:
{col_lines}

Внешние ключи:
{fk_lines}

Сгенерируй ровно {TASKS_PER_TABLE} разнообразных пар (задача на русском, SQL-запрос).

Покрытие сложности:
- 4 простых (simple): SELECT с WHERE, ORDER BY, LIMIT — одна таблица
- 3 средних (medium): JOIN с одной связанной таблицей через FK, GROUP BY или агрегация
- 3 сложных (complex): CTE, оконные функции, подзапросы или несколько JOIN

Правила для SQL:
- Только валидный PostgreSQL
- Всегда LIMIT (максимум 1000)
- Никогда SELECT *
- Используй только колонки из схемы выше
- Для JOIN используй только FK из схемы

Верни JSON-объект с ключом "pairs":
{{"pairs": [{{"task": "...", "sql": "SELECT ...", "complexity": "simple|medium|complex"}}, ...]}}"""


def generate_for_table(
    client: Cerebras,
    table_name: str,
    table_info: dict,
) -> list[dict]:
    prompt = _table_prompt(table_name, table_info)

    for attempt, retry_delay in enumerate([0] + RETRY_DELAYS):
        if retry_delay:
            print(f"\n    ⏳ Ожидаем {retry_delay} сек (попытка {attempt + 1})...", end=" ")
            time.sleep(retry_delay)

        try:
            response = client.chat.completions.create(
                model=MODEL,
                temperature=0.3,
                max_tokens=2048,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Ты эксперт по PostgreSQL. Генерируй валидационные пары task+SQL. "
                            "Отвечай ТОЛЬКО JSON с ключом 'pairs'."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
            )

            raw = response.choices[0].message.content or "{}"
            data = json.loads(raw)

            pairs = data.get("pairs", [])
            if isinstance(data, list):
                pairs = data

            result = []
            for i, pair in enumerate(pairs[:TASKS_PER_TABLE]):
                if "task" in pair and "sql" in pair:
                    result.append({
                        "task_id": f"{table_name}_{i+1:02d}",
                        "table": table_name,
                        "task": pair["task"],
                        "reference_sql": pair["sql"].strip(),
                        "complexity": pair.get("complexity", "simple"),
                    })
            return result

        except Exception as e:
            err = str(e)
            if "429" in err or "rate limit" in err.lower() or "rate_limit" in err.lower():
                if attempt < len(RETRY_DELAYS):
                    print(f"\n    ⚠️  Rate Limit", end="")
                    continue
                print(f"\n    ❌ Исчерпаны все попытки для {table_name}")
                return []
            print(f"\n    ❌ Ошибка: {e}")
            return []

    return []


def main() -> None:
    client = Cerebras(api_key=os.getenv("CEREBRAS_API_KEY"))
    schema = _load_schema()
    tables = list(schema.items())

    # Загружаем уже готовые данные (для возобновления)
    dataset, done_tables = _load_existing()
    remaining = [(t, info) for t, info in tables if t not in done_tables]

    if done_tables:
        print(f"▶ Возобновление: {len(done_tables)} таблиц уже готовы, осталось {len(remaining)}")
    else:
        print(f"▶ Старт: {len(tables)} таблиц × {TASKS_PER_TABLE} задач = {len(tables) * TASKS_PER_TABLE} пар")

    print(f"  Модель: {MODEL}")
    print(f"  Пауза между запросами: {RATE_LIMIT_DELAY} сек\n")

    errors: list[str] = []
    total = len(remaining)

    already_done = len(done_tables)
    for idx, (table_name, table_info) in enumerate(remaining, start=1):
        overall_idx = already_done + idx
        print(f"[{overall_idx:02d}/{len(tables)}] {table_name} ...", end=" ", flush=True)

        pairs = generate_for_table(client, table_name, table_info)

        if pairs:
            dataset.extend(pairs)
            done_tables.add(table_name)
            _save(dataset)  # сохраняем после каждой таблицы
            s = sum(1 for p in pairs if p["complexity"] == "simple")
            m = sum(1 for p in pairs if p["complexity"] == "medium")
            c = sum(1 for p in pairs if p["complexity"] == "complex")
            print(f"✓ {len(pairs)} пар (simple={s}, medium={m}, complex={c})")
        else:
            errors.append(table_name)
            print("✗ пропущено")

        if idx < total:
            time.sleep(RATE_LIMIT_DELAY)

    print(f"\n{'═' * 50}")
    print(f"✅ Датасет сохранён: {OUTPUT_PATH}")
    print(f"   Всего пар:   {len(dataset)}")
    print(f"   Simple:      {sum(1 for d in dataset if d['complexity'] == 'simple')}")
    print(f"   Medium:      {sum(1 for d in dataset if d['complexity'] == 'medium')}")
    print(f"   Complex:     {sum(1 for d in dataset if d['complexity'] == 'complex')}")
    if errors:
        print(f"   Пропущено ({len(errors)}): {', '.join(errors)}")
        print(f"   Повтори запуск — пропущенные таблицы будут добавлены автоматически.")


if __name__ == "__main__":
    main()
