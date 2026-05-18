"""
Метрики для оценки системы SQL-генерации и аудита безопасности.

Принимает SystemResult или список SystemResult из baseline1.py.

Метрики:
  - Execution Accuracy (структурное сходство с эталонным SQL)
  - Суммарный и финальный риск-скор
  - Доля одобренных запросов (approval rate)
  - Среднее число итераций до approve
  - Динамика снижения риска по итерациям
  - Токены и время (из SystemResult.metadata)

Использование:
    from rag_pipeline.metrics import compute_single, compute_batch, format_report

    m = compute_single(result, reference_sql="SELECT id FROM scp_application LIMIT 100")
    report = format_report(compute_batch([result1, result2], reference_sqls=[ref1, ref2]))
    print(report)

Поля metadata в SystemResult, которые влияют на метрики:
    metadata["task_id"]                — строковый идентификатор задачи
    metadata["executed_correctly"]     — bool, выполнен ли запрос корректно в БД
    metadata["tokens_used"]            — int, суммарные токены по всем итерациям
    metadata["tokens_per_iteration"]   — list[int], токены по каждой итерации
    metadata["execution_time_seconds"] — float, время выполнения всего прогона
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))
from baseline1 import AuditResult, IterationLog, SystemResult, Vulnerability

try:
    import sqlparse
    _SQLPARSE_AVAILABLE = True
except ImportError:
    _SQLPARSE_AVAILABLE = False


# ── Датаклассы метрик ──────────────────────────────────────────────────────────

@dataclass
class SingleRunMetrics:
    """Метрики одного прогона системы."""

    task_id: str
    approved: bool
    iterations_used: int

    final_risk_score: float
    initial_risk_score: float
    risk_reduction: float
    risk_per_iteration: list[float]

    vuln_classes_found: list[str]

    execution_accuracy: float | None      # token-level F1 vs reference_sql
    exact_match: bool | None             # нормализованное точное совпадение
    executed_correctly: bool | None      # из metadata["executed_correctly"]

    tokens_total: int | None             # из metadata["tokens_used"]
    tokens_per_iteration: list[int]      # из metadata["tokens_per_iteration"]
    time_seconds: float | None           # из metadata["execution_time_seconds"]

    final_sql: str


@dataclass
class BatchMetrics:
    """Агрегированные метрики по набору прогонов."""

    n_total: int
    n_approved: int
    approval_rate: float

    avg_iterations: float
    avg_iterations_approved: float | None   # None если нет ни одного одобренного
    max_iterations: int

    avg_final_risk: float
    avg_initial_risk: float
    avg_risk_reduction: float

    execution_accuracy: float | None        # средний EA (только если были reference_sqls)
    exact_match_rate: float | None

    avg_tokens: float | None
    avg_time_seconds: float | None

    vuln_class_frequency: dict[str, int]
    risk_dynamics: list[float]              # средний риск по позиции итерации

    runs: list[SingleRunMetrics]


# ── SQL-нормализация и сходство ────────────────────────────────────────────────

def _normalize_sql(sql: str) -> str:
    """Нормализует SQL для сравнения: lowercase, убирает комментарии и лишние пробелы."""
    if _SQLPARSE_AVAILABLE:
        parsed = sqlparse.format(
            sql,
            strip_comments=True,
            strip_whitespace=True,
            keyword_case="upper",
            identifier_case="lower",
        )
    else:
        parsed = sql

    # Убираем точку с запятой в конце, лишние пробелы
    normalized = re.sub(r"\s+", " ", parsed).strip().rstrip(";").strip()
    return normalized


def _sql_tokens(sql: str) -> list[str]:
    """Разбивает нормализованный SQL на токены (слова)."""
    normalized = _normalize_sql(sql)
    return re.findall(r"\w+", normalized.lower())


def sql_token_f1(predicted: str, reference: str) -> float:
    """
    Token-level F1 между предсказанным и эталонным SQL.

    F1 = 2 * precision * recall / (precision + recall)
    где precision = |pred ∩ ref| / |pred|, recall = |pred ∩ ref| / |ref|

    Возвращает 0.0..1.0 (1.0 = полное совпадение по токенам).
    """
    pred_tokens = Counter(_sql_tokens(predicted))
    ref_tokens = Counter(_sql_tokens(reference))

    if not pred_tokens or not ref_tokens:
        return 0.0

    common = sum((pred_tokens & ref_tokens).values())
    precision = common / sum(pred_tokens.values())
    recall = common / sum(ref_tokens.values())

    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def sql_exact_match(predicted: str, reference: str) -> bool:
    """Нормализованное точное совпадение двух SQL-запросов."""
    return _normalize_sql(predicted) == _normalize_sql(reference)


# ── Вычисление метрик одного прогона ──────────────────────────────────────────

def compute_single(
    result: SystemResult,
    reference_sql: str | None = None,
    task_id: str | None = None,
) -> SingleRunMetrics:
    """
    Вычисляет метрики для одного SystemResult.

    Args:
        result: результат прогона системы
        reference_sql: эталонный SQL для вычисления Execution Accuracy (опционально)
        task_id: идентификатор задачи (если не задан — берётся из metadata["task_id"])

    Returns:
        SingleRunMetrics с заполненными полями
    """
    meta = result.metadata or {}

    # Идентификатор задачи
    tid = task_id or str(meta.get("task_id", "unknown"))

    # Риски по итерациям
    risk_per_iter = [
        log.audit_result.overall_risk_score
        for log in result.iterations_log
    ]
    initial_risk = risk_per_iter[0] if risk_per_iter else 0.0
    final_risk = risk_per_iter[-1] if risk_per_iter else 0.0
    risk_reduction = max(0.0, initial_risk - final_risk)

    # Все найденные классы уязвимостей (из всех итераций)
    vuln_classes: list[str] = []
    for log in result.iterations_log:
        for v in log.audit_result.vulnerabilities:
            vuln_classes.append(v.vuln_class)

    # Execution Accuracy
    ea: float | None = None
    em: bool | None = None
    if reference_sql:
        ea = sql_token_f1(result.final_sql, reference_sql)
        em = sql_exact_match(result.final_sql, reference_sql)

    # Из metadata
    executed_correctly: bool | None = meta.get("executed_correctly")
    tokens_total: int | None = meta.get("tokens_used")
    tokens_per_iter: list[int] = meta.get("tokens_per_iteration", [])
    time_sec: float | None = meta.get("execution_time_seconds")

    return SingleRunMetrics(
        task_id=tid,
        approved=result.approved,
        iterations_used=result.iterations_used,
        final_risk_score=final_risk,
        initial_risk_score=initial_risk,
        risk_reduction=risk_reduction,
        risk_per_iteration=risk_per_iter,
        vuln_classes_found=vuln_classes,
        execution_accuracy=ea,
        exact_match=em,
        executed_correctly=executed_correctly,
        tokens_total=tokens_total,
        tokens_per_iteration=tokens_per_iter,
        time_seconds=time_sec,
        final_sql=result.final_sql,
    )


# ── Вычисление агрегированных метрик ──────────────────────────────────────────

def compute_batch(
    results: list[SystemResult],
    reference_sqls: list[str] | None = None,
    task_ids: list[str] | None = None,
) -> BatchMetrics:
    """
    Вычисляет агрегированные метрики по набору прогонов.

    Args:
        results: список SystemResult
        reference_sqls: список эталонных SQL (параллельно results, опционально)
        task_ids: список идентификаторов задач (опционально)

    Returns:
        BatchMetrics с агрегированными значениями
    """
    if not results:
        raise ValueError("results не может быть пустым")

    refs = reference_sqls or [None] * len(results)
    ids = task_ids or [None] * len(results)

    runs = [
        compute_single(r, ref, tid)
        for r, ref, tid in zip(results, refs, ids)
    ]

    n_total = len(runs)
    n_approved = sum(1 for r in runs if r.approved)
    approval_rate = n_approved / n_total

    # Итерации
    all_iters = [r.iterations_used for r in runs]
    avg_iters = statistics.mean(all_iters)
    max_iters = max(all_iters)
    approved_iters = [r.iterations_used for r in runs if r.approved]
    avg_iters_approved = statistics.mean(approved_iters) if approved_iters else None

    # Риск
    avg_final_risk = statistics.mean(r.final_risk_score for r in runs)
    avg_initial_risk = statistics.mean(r.initial_risk_score for r in runs)
    avg_risk_reduction = statistics.mean(r.risk_reduction for r in runs)

    # Execution Accuracy (только для runs с заполненным EA)
    ea_values = [r.execution_accuracy for r in runs if r.execution_accuracy is not None]
    ea_mean = statistics.mean(ea_values) if ea_values else None
    em_values = [r.exact_match for r in runs if r.exact_match is not None]
    em_rate = sum(em_values) / len(em_values) if em_values else None

    # Токены и время
    token_values = [r.tokens_total for r in runs if r.tokens_total is not None]
    avg_tokens = statistics.mean(token_values) if token_values else None
    time_values = [r.time_seconds for r in runs if r.time_seconds is not None]
    avg_time = statistics.mean(time_values) if time_values else None

    # Частота классов уязвимостей
    all_vulns: list[str] = []
    for r in runs:
        all_vulns.extend(r.vuln_classes_found)
    vuln_freq = dict(Counter(all_vulns).most_common())

    # Динамика риска: средний риск по позиции итерации
    max_depth = max((len(r.risk_per_iteration) for r in runs), default=0)
    risk_dynamics: list[float] = []
    for pos in range(max_depth):
        values_at_pos = [
            r.risk_per_iteration[pos]
            for r in runs
            if pos < len(r.risk_per_iteration)
        ]
        risk_dynamics.append(statistics.mean(values_at_pos) if values_at_pos else 0.0)

    return BatchMetrics(
        n_total=n_total,
        n_approved=n_approved,
        approval_rate=approval_rate,
        avg_iterations=avg_iters,
        avg_iterations_approved=avg_iters_approved,
        max_iterations=max_iters,
        avg_final_risk=avg_final_risk,
        avg_initial_risk=avg_initial_risk,
        avg_risk_reduction=avg_risk_reduction,
        execution_accuracy=ea_mean,
        exact_match_rate=em_rate,
        avg_tokens=avg_tokens,
        avg_time_seconds=avg_time,
        vuln_class_frequency=vuln_freq,
        risk_dynamics=risk_dynamics,
        runs=runs,
    )


# ── Форматирование отчёта ──────────────────────────────────────────────────────

def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _opt(value: float | None, fmt: str = ".2f") -> str:
    return f"{value:{fmt}}" if value is not None else "—"


def format_report(batch: BatchMetrics, title: str = "Отчёт по прогону системы") -> str:
    """
    Возвращает читаемый текстовый отчёт по BatchMetrics.

    Args:
        batch: результат compute_batch()
        title: заголовок отчёта

    Returns:
        Многострочная строка с отчётом
    """
    lines: list[str] = []
    sep = "═" * 60

    lines.append(sep)
    lines.append(f"  {title}")
    lines.append(sep)

    lines.append("\n── ОБЩАЯ СТАТИСТИКА ─────────────────────────────────────")
    lines.append(f"  Всего прогонов:              {batch.n_total}")
    lines.append(f"  Одобрено:                    {batch.n_approved} ({_pct(batch.approval_rate)})")
    lines.append(f"  Отклонено:                   {batch.n_total - batch.n_approved} ({_pct(1 - batch.approval_rate)})")

    lines.append("\n── ИТЕРАЦИИ ─────────────────────────────────────────────")
    lines.append(f"  Среднее итераций (все):      {batch.avg_iterations:.2f}")
    if batch.avg_iterations_approved is not None:
        lines.append(f"  Среднее итераций (одобрен):  {batch.avg_iterations_approved:.2f}")
    else:
        lines.append("  Среднее итераций (одобрен):  — (нет одобренных)")
    lines.append(f"  Максимум итераций:           {batch.max_iterations}")

    lines.append("\n── РИСК-СКОР ────────────────────────────────────────────")
    lines.append(f"  Начальный (до исправлений):  {batch.avg_initial_risk:.2f} / 10")
    lines.append(f"  Финальный:                   {batch.avg_final_risk:.2f} / 10")
    lines.append(f"  Среднее снижение риска:      {batch.avg_risk_reduction:.2f}")

    if batch.risk_dynamics:
        lines.append("\n  Динамика риска по итерациям:")
        for i, risk in enumerate(batch.risk_dynamics, start=1):
            bar = "█" * int(risk) + "░" * (10 - int(risk))
            lines.append(f"    Итерация {i}: {risk:4.2f}  {bar}")

    lines.append("\n── КАЧЕСТВО ГЕНЕРАЦИИ ───────────────────────────────────")
    if batch.execution_accuracy is not None:
        lines.append(f"  Execution Accuracy (F1):     {batch.execution_accuracy:.3f}")
        lines.append(f"  Exact Match Rate:            {_opt(batch.exact_match_rate, '.1%')}")
    else:
        lines.append("  Execution Accuracy:          — (reference SQL не предоставлен)")

    lines.append("\n── НАЙДЕННЫЕ УЯЗВИМОСТИ ─────────────────────────────────")
    if batch.vuln_class_frequency:
        for vuln_class, count in batch.vuln_class_frequency.items():
            lines.append(f"  {vuln_class:<28} {count:>4} раз")
    else:
        lines.append("  Уязвимостей не обнаружено")

    lines.append("\n── РЕСУРСЫ ──────────────────────────────────────────────")
    lines.append(f"  Среднее токенов/прогон:      {_opt(batch.avg_tokens, '.0f')}")
    lines.append(f"  Среднее время/прогон (сек):  {_opt(batch.avg_time_seconds, '.2f')}")

    lines.append("\n── ПО ЗАДАЧАМ ───────────────────────────────────────────")
    header = f"  {'Задача':<20} {'Апрув':>6} {'Итер':>5} {'Риск нач':>9} {'Риск фин':>9} {'EA':>6}"
    lines.append(header)
    lines.append("  " + "─" * 58)
    for r in batch.runs:
        ea_str = f"{r.execution_accuracy:.2f}" if r.execution_accuracy is not None else "  —  "
        approved_str = "✓" if r.approved else "✗"
        lines.append(
            f"  {r.task_id:<20} {approved_str:>6} {r.iterations_used:>5}"
            f" {r.initial_risk_score:>9.2f} {r.final_risk_score:>9.2f} {ea_str:>6}"
        )

    lines.append("\n" + sep)
    return "\n".join(lines)


# ── Быстрая проверка ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    from datetime import datetime

    def _mock_vuln(vuln_class: str, risk: float) -> Vulnerability:
        return Vulnerability(
            vuln_class=vuln_class,
            risk_score=risk,
            description="Тест",
            recommendation="Исправить",
        )

    def _mock_audit(approved: bool, risk: float, vulns: list[Vulnerability]) -> AuditResult:
        return AuditResult(
            approved=approved,
            vulnerabilities=vulns,
            overall_risk_score=risk,
            summary="Тест аудита",
        )

    # Прогон 1: одобрен за 2 итерации
    result1 = SystemResult(
        final_sql="SELECT id, name FROM sys_employee WHERE status = 1 LIMIT 50",
        approved=True,
        iterations_used=2,
        iterations_log=[
            IterationLog(
                timestamp=datetime.now(),
                iteration=1,
                sql_query="SELECT * FROM sys_employee",
                audit_result=_mock_audit(False, 5.0, [
                    _mock_vuln("SELECT_STAR", 5.0),
                    _mock_vuln("NO_PAGINATION", 4.0),
                ]),
            ),
            IterationLog(
                timestamp=datetime.now(),
                iteration=2,
                sql_query="SELECT id, name FROM sys_employee WHERE status = 1 LIMIT 50",
                audit_result=_mock_audit(True, 0.0, []),
            ),
        ],
        audit_log="Итерация 1: SELECT_STAR, NO_PAGINATION. Итерация 2: одобрен.",
        metadata={
            "task_id": "task_001",
            "tokens_used": 1200,
            "execution_time_seconds": 3.4,
        },
    )

    # Прогон 2: отклонён после 5 итераций
    result2 = SystemResult(
        final_sql="SELECT * FROM sys_employee WHERE name = '' OR '1'='1'",
        approved=False,
        iterations_used=5,
        iterations_log=[
            IterationLog(
                timestamp=datetime.now(),
                iteration=i,
                sql_query=f"SELECT * FROM sys_employee -- iter {i}",
                audit_result=_mock_audit(False, 10.0 - i * 0.5, [
                    _mock_vuln("SQL_INJ_CLASSIC", 10.0),
                ]),
            )
            for i in range(1, 6)
        ],
        audit_log="SQL-инъекция не исправлена за 5 итераций.",
        metadata={
            "task_id": "task_002",
            "tokens_used": 3500,
            "execution_time_seconds": 12.1,
        },
    )

    reference_sqls = [
        "SELECT id, name FROM sys_employee WHERE status = 1 LIMIT 50",
        "SELECT id, name FROM sys_employee WHERE status = 1 LIMIT 50",
    ]

    batch = compute_batch([result1, result2], reference_sqls=reference_sqls)
    print(format_report(batch, title="Тестовый прогон"))
