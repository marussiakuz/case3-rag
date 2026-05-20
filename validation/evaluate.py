"""
Запускает генератор на валидационном датасете и считает метрики.

Метрики:
  - Execution Accuracy (реальное выполнение SQL на PostgreSQL) — цель ≥ 0.7
  - Среднее время генерации — цель ≤ 30 сек
  - Breakdown по сложности: simple / medium / complex

  EA считается по совпадению строк (id-match) без учёта набора колонок.
  strict_execution_accuracy — строгое совпадение всех колонок.

Вывод: validation/results.json + текстовый отчёт в консоль

Запуск:
    # Полный датасет (600 запросов)
    .venv/bin/python validation/evaluate.py

    # Быстрый тест на подмножестве
    .venv/bin/python validation/evaluate.py --limit 30

    # Только определённая таблица
    .venv/bin/python validation/evaluate.py --table sys_employee
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from statistics import mean

import psycopg2
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

DATASET_PATH = ROOT / "validation" / "dataset.json"
RESULTS_PATH = ROOT / "validation" / "results.json"
PROGRESS_PATH = ROOT / "validation" / "progress.json"

DB_CONFIG = {
    "host": os.getenv("PG_HOST", "localhost"),
    "port": int(os.getenv("PG_PORT", "5432")),
    "dbname": os.getenv("PG_DB", "greendata"),
    "user": os.getenv("PG_USER", "postgres"),
    "password": os.getenv("PG_PASSWORD", "iamroot"),
}

# EVAL_MODE=generator  — только генератор (быстро, для измерения EA)
# EVAL_MODE=orchestrator — полный пайплайн с аудитором (медленнее, для демо)
EVAL_MODE = os.getenv("EVAL_MODE", "generator")

SQL_TIMEOUT_MS = 5000  # 5 секунд на выполнение одного SQL

# Загружаем список таблиц и колонок из БД один раз при старте
_KNOWN_TABLES: set[str] = set()
_KNOWN_COLUMNS: set[str] = set()


def _load_schema_from_db(conn) -> None:
    global _KNOWN_TABLES, _KNOWN_COLUMNS
    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
        _KNOWN_TABLES = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public'")
        _KNOWN_COLUMNS = {r[0] for r in cur.fetchall()}


def _validate_sql(sql: str) -> str | None:
    """Проверяет что SQL не использует несуществующие таблицы. Возвращает ошибку или None."""
    if not _KNOWN_TABLES:
        return None
    # Имена CTE (WITH name AS ...) — не таблицы, не должны проверяться
    cte_names = set(re.findall(r'\bWITH\s+(?:RECURSIVE\s+)?(\w+)\s+AS\s*\(', sql, re.IGNORECASE))
    cte_names |= set(re.findall(r',\s*(\w+)\s+AS\s*\(', sql, re.IGNORECASE))
    cte_names = {n.lower() for n in cte_names}

    table_refs = re.findall(
        r'(?:FROM|JOIN)\s+(?:public\.)?([a-zA-Z_][a-zA-Z0-9_]*)',
        sql, re.IGNORECASE
    )
    unknown = [
        t for t in table_refs
        if t.lower() not in _KNOWN_TABLES and t.lower() not in cte_names
    ]
    if unknown:
        return f"hallucinated tables: {', '.join(unknown)}"
    return None


def _check_indices() -> bool:
    try:
        import psycopg2 as _pg
        conn = _pg.connect(
            host=os.getenv("PG_HOST", "localhost"),
            port=int(os.getenv("PG_PORT", "5432")),
            dbname="gd_app",
            user=os.getenv("PG_USER", "postgres"),
            password=os.getenv("PG_PASSWORD", ""),
        )
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM rag_embeddings WHERE index_name='generation'")
            count = cur.fetchone()[0]
        conn.close()
        if count == 0:
            print("❌ RAG-индекс 'generation' пуст. Запусти сначала:")
            print("   .venv/bin/python rag_pipeline/build_indices.py")
            return False
        return True
    except Exception as e:
        print(f"❌ Ошибка проверки RAG-индекса: {e}")
        return False


def _substitute_placeholders(sql: str) -> str:
    """Заменяет $1, $2, ... на тестовые значения для выполнения."""
    return re.sub(r"\$\d+", "1", sql)


def _execute_sql(conn, sql: str) -> tuple[list[tuple], str | None]:
    """Выполняет SQL и возвращает (строки, ошибка)."""
    sql_exec = _substitute_placeholders(sql)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {SQL_TIMEOUT_MS};")
            cur.execute(sql_exec)
            rows = cur.fetchall()
            return rows, None
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return [], str(e)


def _results_match(rows_gen: list[tuple], rows_ref: list[tuple]) -> bool:
    """Сравнивает результаты без учёта порядка строк."""
    try:
        return set(rows_gen) == set(rows_ref)
    except TypeError:
        try:
            return sorted(str(r) for r in rows_gen) == sorted(str(r) for r in rows_ref)
        except Exception:
            return False


def _rows_match_by_id(rows_gen: list[tuple], rows_ref: list[tuple]) -> bool:
    """Row-level EA: сравнивает только первый столбец (id). Показывает правильность WHERE/JOIN
    независимо от того, совпадает ли набор остальных колонок."""
    if not rows_gen and not rows_ref:
        return True
    if len(rows_gen) != len(rows_ref):
        return False
    try:
        return {r[0] for r in rows_gen} == {r[0] for r in rows_ref}
    except Exception:
        return False


def run_evaluation(
    limit: int | None = None,
    table_filter: str | None = None,
) -> None:
    if not _check_indices():
        return

    if not DATASET_PATH.exists():
        print("❌ Датасет не найден. Запусти сначала:")
        print("   .venv/bin/python validation/generate_dataset.py")
        return

    if EVAL_MODE == "orchestrator":
        from orchestrator.orchestrator import run_pipeline
    else:
        from generator.generator import GroqSQLGenerator
        _generator = GroqSQLGenerator()

    dataset: list[dict] = json.loads(DATASET_PATH.read_text(encoding="utf-8"))

    if table_filter:
        dataset = [d for d in dataset if d["table"] == table_filter]
        print(f"Фильтр по таблице: {table_filter} ({len(dataset)} запросов)")

    if limit:
        dataset = dataset[:limit]
        print(f"Ограничение: первые {limit} запросов")

    total = len(dataset)
    mode_label = "генератор + аудитор, до 3 итераций" if EVAL_MODE == "orchestrator" else "только генератор"
    print(f"\nЗапускаем оценку: {total} запросов ({mode_label})")
    print(f"{'─' * 60}")

    conn = psycopg2.connect(**DB_CONFIG)
    _load_schema_from_db(conn)
    results: list[dict] = []
    run_t0 = time.time()

    PROGRESS_PATH.write_text(
        json.dumps({"running": True, "current": 0, "total": total}),
        encoding="utf-8",
    )

    for idx, item in enumerate(dataset, start=1):
        task_id = item["task_id"]
        task = item["task"]
        reference_sql = item["reference_sql"]
        complexity = item.get("complexity", "simple")

        print(f"[{idx:03d}/{total}] {task_id} ({complexity}) ... ", end="", flush=True)

        t0 = time.time()
        try:
            if EVAL_MODE == "orchestrator":
                pipeline_result = run_pipeline(task, max_iterations=3, verbose=False)
                generated_sql = pipeline_result.final_sql
                iterations_used = pipeline_result.iterations_used
                approved = pipeline_result.approved
            else:
                generated_sql = _generator.generate(task_description=task)
                iterations_used = 1
                approved = None

                # Retry если генератор придумал несуществующие таблицы (до 2 попыток)
                sql_history = [generated_sql]
                for _retry in range(2):
                    hallucination_check = _validate_sql(generated_sql)
                    if not hallucination_check:
                        break
                    bad_tables = hallucination_check.replace("hallucinated tables: ", "")
                    retry_feedback = type("AuditResult", (), {
                        "vulnerabilities": [type("V", (), {
                            "vuln_class": "HALLUCINATION",
                            "risk_score": 9.0,
                            "description": f"Таблицы не существуют в БД: {bad_tables}",
                            "recommendation": "Используй ТОЛЬКО таблицы из контекста схемы выше",
                        })()],
                    })()
                    generated_sql = _generator.generate(
                        task_description=task,
                        sql_history=sql_history,
                        audit_feedback=retry_feedback,
                        iteration=iterations_used + 1,
                    )
                    sql_history.append(generated_sql)
                    iterations_used += 1

            elapsed = time.time() - t0

            # Валидация: проверяем имена таблиц до выполнения
            hallucination = _validate_sql(generated_sql)

            # Выполняем оба SQL на реальной БД
            rows_gen, err_gen = _execute_sql(conn, generated_sql)
            rows_ref, err_ref = _execute_sql(conn, reference_sql)

            match = False
            row_match = False
            strict_ea = 0.0
            ea = 0.0
            if hallucination:
                exec_note = f"hallucination: {hallucination}"
                err_gen = err_gen or hallucination
            elif err_gen:
                exec_note = f"gen_error: {err_gen[:80]}"
            elif err_ref:
                exec_note = f"ref_error: {err_ref[:80]}"
            else:
                match = _results_match(rows_gen, rows_ref)
                row_match = _rows_match_by_id(rows_gen, rows_ref)
                strict_ea = 1.0 if match else 0.0
                ea = 1.0 if row_match else 0.0
                exec_note = f"gen={len(rows_gen)} строк, ref={len(rows_ref)} строк"

            results.append({
                "task_id": task_id,
                "table": item["table"],
                "complexity": complexity,
                "task": task,
                "reference_sql": reference_sql,
                "generated_sql": generated_sql,
                "execution_accuracy": ea,
                "strict_execution_accuracy": strict_ea,
                "exact_match": match,
                "gen_rows": len(rows_gen),
                "ref_rows": len(rows_ref),
                "gen_error": err_gen,
                "ref_error": err_ref,
                "iterations_used": iterations_used,
                "auditor_approved": approved,
                "time_seconds": round(elapsed, 2),
                "error": None,
            })

            status = "✅" if ea else "❌"
            iter_note = f" iter={iterations_used}" if iterations_used > 1 else ""
            time_warn = " ⏱" if elapsed > 30 else ""
            print(f"{status} EA={ea:.0f}(строки)/{strict_ea:.0f}(строго)  {exec_note}{iter_note}  t={elapsed:.1f}s{time_warn}")

        except Exception as e:
            elapsed = time.time() - t0
            print(f"❌ ERROR: {e}")
            results.append({
                "task_id": task_id,
                "table": item["table"],
                "complexity": complexity,
                "task": task,
                "reference_sql": reference_sql,
                "generated_sql": "",
                "execution_accuracy": 0.0,
                "strict_execution_accuracy": 0.0,
                "exact_match": False,
                "gen_rows": 0,
                "ref_rows": 0,
                "gen_error": None,
                "ref_error": None,
                "time_seconds": round(elapsed, 2),
                "error": str(e),
            })

        PROGRESS_PATH.write_text(
            json.dumps({"running": True, "current": idx, "total": total}),
            encoding="utf-8",
        )
        if idx % 10 == 0:
            _save_results(results)

    conn.close()
    duration = time.time() - run_t0
    _save_results(results)
    valid_fin = [r for r in results if r.get("error") is None]
    avg_iter = round(mean(r.get("iterations_used", 1) for r in valid_fin), 2) if valid_fin else 1.0
    PROGRESS_PATH.write_text(
        json.dumps({
            "running": False,
            "current": total,
            "total": total,
            "duration_seconds": round(duration, 1),
            "avg_iterations": avg_iter,
        }),
        encoding="utf-8",
    )
    _print_report(results)
    _save_run_to_db(results, duration)


def _save_results(results: list[dict]) -> None:
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _print_report(results: list[dict]) -> None:
    valid = [r for r in results if r["error"] is None]
    errors = [r for r in results if r["error"] is not None]

    if not valid:
        print("\n❌ Нет валидных результатов")
        return

    ea_values = [r["execution_accuracy"] for r in valid]
    strict_ea_values = [r.get("strict_execution_accuracy", r["execution_accuracy"]) for r in valid]
    time_values = [r["time_seconds"] for r in valid]

    overall_ea = mean(ea_values)
    overall_strict_ea = mean(strict_ea_values)
    avg_time = mean(time_values)
    max_time = max(time_values)
    over_30s = sum(1 for t in time_values if t > 30)

    ea_target = "✅" if overall_ea >= 0.7 else "❌"
    time_target = "✅" if avg_time <= 30 else "❌"

    gen_errors = [r for r in valid if r.get("gen_error")]
    ref_errors = [r for r in valid if r.get("ref_error")]

    print(f"\n{'═' * 60}")
    print("  ОТЧЁТ ПО ВАЛИДАЦИИ")
    print(f"{'═' * 60}")
    print(f"\n  Всего запросов:         {len(results)}")
    print(f"  Успешно:                {len(valid)}")
    print(f"  Ошибок генератора:      {len(errors)}")
    print(f"  Ошибок SQL (generated): {len(gen_errors)}")
    print(f"  Ошибок SQL (reference): {len(ref_errors)}")

    print(f"\n── EXECUTION ACCURACY ─────────────────────────────────")
    print(f"  EA по строкам (id-match): {overall_ea:.4f}  {ea_target} (цель ≥ 0.70)")
    print(f"  EA строгая (все колонки): {overall_strict_ea:.4f}  {'✅' if overall_strict_ea >= 0.7 else '❌'}")
    print(f"  Совпали по строкам:       {sum(ea_values):.0f} / {len(valid)} ({overall_ea:.1%})")
    print(f"  Совпали строго:           {sum(strict_ea_values):.0f} / {len(valid)} ({overall_strict_ea:.1%})")
    n_col_mismatch = sum(1 for ea, sea in zip(ea_values, strict_ea_values) if sea == 0 and ea == 1)
    if n_col_mismatch:
        print(f"  Расхождение по колонкам: {n_col_mismatch} (правильные строки, разные колонки)")

    print(f"\n── ВРЕМЯ ВЫПОЛНЕНИЯ ───────────────────────────────────")
    print(f"  Среднее время:          {avg_time:.2f} сек  {time_target} (цель ≤ 30 сек)")
    print(f"  Максимум:               {max_time:.2f} сек")
    print(f"  Превысили 30 сек:       {over_30s} запросов")

    print(f"\n── ПО СЛОЖНОСТИ ───────────────────────────────────────")
    for complexity in ("simple", "medium", "complex"):
        subset = [r for r in valid if r["complexity"] == complexity]
        if subset:
            c_ea = mean(r["execution_accuracy"] for r in subset)
            c_strict_ea = mean(r.get("strict_execution_accuracy", r["execution_accuracy"]) for r in subset)
            c_time = mean(r["time_seconds"] for r in subset)
            target = "✅" if c_ea >= 0.7 else "❌"
            print(f"  {complexity:<8} n={len(subset):>3}   EA={c_ea:.3f} {target}  strict={c_strict_ea:.3f}  avg_time={c_time:.1f}s")

    # Провальные запросы (EA по строкам = 0)
    failed = [r for r in valid if r["execution_accuracy"] == 0.0]
    print(f"\n── ПРОВАЛЬНЫЕ ЗАПРОСЫ (EA=0) ──────────────────────────")
    for r in failed[:5]:
        note = ""
        if r.get("gen_error"):
            note = f" [ошибка SQL: {r['gen_error'][:50]}]"
        elif r.get("ref_rows") == 0 and r.get("gen_rows") == 0:
            note = " [оба вернули 0 строк — но разные запросы]"
        else:
            note = f" [gen={r.get('gen_rows',0)} стр, ref={r.get('ref_rows',0)} стр]"
        print(f"  {r['task_id']:<30}{note}")
        print(f"    {r['task'][:70]}")

    # Успешные запросы (EA по строкам = 1)
    passed = [r for r in valid if r["execution_accuracy"] == 1.0]
    print(f"\n── СОВПАВШИЕ ЗАПРОСЫ (EA=1) ───────────────────────────")
    for r in passed[:5]:
        print(f"  {r['task_id']:<30} gen={r.get('gen_rows',0)} стр = ref={r.get('ref_rows',0)} стр")

    print(f"\n  Результаты сохранены: {RESULTS_PATH}")
    print(f"{'═' * 60}")


def _save_run_to_db(results: list[dict], duration: float) -> None:
    """Сохраняет сводную статистику прогона в gd_app.validation_runs."""
    try:
        from db.validation_runs import save_run
        valid = [r for r in results if r.get("error") is None]
        if not valid:
            return

        def _avg_ea(subset):
            return round(mean(r["execution_accuracy"] for r in subset), 4) if subset else None

        def _for_complexity(c):
            return _avg_ea([r for r in valid if r.get("complexity") == c])

        save_run({
            "total_queries":             len(results),
            "completed_queries":         len(valid),
            "execution_accuracy":        round(mean(r["execution_accuracy"] for r in valid), 4),
            "strict_execution_accuracy": round(mean(r.get("strict_execution_accuracy", 0.0) for r in valid), 4),
            "avg_time_seconds":          round(mean(r["time_seconds"] for r in valid), 2),
            "n_errors":                  len([r for r in results if r.get("error")]),
            "simple_ea":                 _for_complexity("simple"),
            "medium_ea":                 _for_complexity("medium"),
            "complex_ea":                _for_complexity("complex"),
            "duration_seconds":          round(duration, 1),
        })
        print("  [DB] Прогон сохранён в validation_runs")
    except Exception as e:
        print(f"  [Warn] Не удалось сохранить прогон в БД: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Оценка генератора на валидационном датасете")
    parser.add_argument("--limit", type=int, default=None, help="Ограничить количество запросов")
    parser.add_argument("--table", type=str, default=None, help="Только конкретная таблица")
    args = parser.parse_args()

    run_evaluation(limit=args.limit, table_filter=args.table)