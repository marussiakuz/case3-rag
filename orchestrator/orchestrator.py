"""
Оркестратор цикла «генератор → аудитор → исправление».

Реализует контракт SQLSecuritySystem из baseline1.py.

Использование:
    from orchestrator import GroqSQLSecuritySystem
    from generator import GroqSQLGenerator
    from auditor import GroqSecurityAuditor

    system = GroqSQLSecuritySystem(
        generator=GroqSQLGenerator(),
        auditor=GroqSecurityAuditor(),
    )
    result = system.run("показать активных сотрудников по подразделениям")
    print(result.final_sql, result.approved, result.iterations_used)

Или через базовую функцию из baseline1.py:
    from baseline1 import run_sql_security_pipeline  # использует заглушки
    # Вместо неё используй run_pipeline() ниже
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))
from baseline1 import (
    AuditResult,
    IterationLog,
    SQLGenerator,
    SQLSecuritySystem,
    SecurityAuditor,
    SystemResult,
    Vulnerability,
)


class GroqSQLSecuritySystem(SQLSecuritySystem):
    """
    Реализация оркестратора цикла генерация → аудит → исправление.

    На каждой итерации:
    1. Генератор создаёт SQL (с учётом предыдущих ошибок)
    2. Аудитор проверяет SQL
    3. Если одобрен — возвращаем результат
    4. Если нет — передаём feedback генератору и повторяем

    Args:
        generator: реализация SQLGenerator
        auditor: реализация SecurityAuditor
        max_iterations: максимум итераций (по умолчанию 5)
    """

    def run(self, task_description: str) -> SystemResult:
        """
        Запускает полный цикл генерации и аудита.

        Args:
            task_description: задача на естественном языке

        Returns:
            SystemResult с финальным SQL, флагом одобрения и полным логом
        """
        start_time = time.time()
        sql_history: list[str] = []
        iterations_log: list[IterationLog] = []
        last_audit: AuditResult | None = None
        tokens_per_iteration: list[int] = []

        for iteration in range(1, self.max_iterations + 1):
            # Генерация
            sql = self.generator.generate(
                task_description=task_description,
                sql_history=sql_history if sql_history else None,
                audit_feedback=last_audit,
                iteration=iteration,
            )

            # Аудит
            audit_result = self.auditor.audit(sql_query=sql)

            # Лог итерации
            revision = _describe_revision(last_audit, iteration)
            iterations_log.append(IterationLog(
                timestamp=datetime.now(),
                iteration=iteration,
                sql_query=sql,
                audit_result=audit_result,
                revision_notes=revision,
            ))

            _print_iteration(iteration, sql, audit_result)

            if audit_result.approved:
                elapsed = time.time() - start_time
                return SystemResult(
                    final_sql=sql,
                    approved=True,
                    iterations_used=iteration,
                    iterations_log=iterations_log,
                    audit_log=_build_audit_log(task_description, iterations_log),
                    metadata={
                        "task_description": task_description,
                        "execution_time_seconds": round(elapsed, 2),
                    },
                )

            sql_history.append(sql)
            last_audit = audit_result

        # Исчерпали итерации — возвращаем последний результат
        elapsed = time.time() - start_time
        return SystemResult(
            final_sql=sql_history[-1] if sql_history else "",
            approved=False,
            iterations_used=self.max_iterations,
            iterations_log=iterations_log,
            audit_log=_build_audit_log(task_description, iterations_log),
            metadata={
                "task_description": task_description,
                "execution_time_seconds": round(elapsed, 2),
                "failure_reason": "max_iterations_reached",
            },
        )


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _describe_revision(last_audit: AuditResult | None, iteration: int) -> str:
    if iteration == 1 or last_audit is None:
        return "Первая генерация"
    classes = [v.vuln_class for v in last_audit.vulnerabilities]
    return f"Исправление после итерации {iteration - 1}: {', '.join(classes) or 'нет уязвимостей'}"


def _build_audit_log(task: str, logs: list[IterationLog]) -> str:
    lines = [f"Задача: {task}", f"Итераций: {len(logs)}", ""]
    for log in logs:
        status = "✅ ОДОБРЕН" if log.audit_result.approved else "❌ ОТКЛОНЁН"
        lines.append(f"Итерация {log.iteration} [{status}] риск={log.audit_result.overall_risk_score:.1f}")
        lines.append(f"  SQL: {log.sql_query[:120]}{'...' if len(log.sql_query) > 120 else ''}")
        for v in log.audit_result.vulnerabilities:
            lines.append(f"  • [{v.vuln_class}] {v.description[:80]}")
        lines.append(f"  Вердикт: {log.audit_result.summary}")
        lines.append("")
    return "\n".join(lines)


def _print_iteration(iteration: int, sql: str, audit: AuditResult) -> None:
    status = "✅ ОДОБРЕН" if audit.approved else "❌ ОТКЛОНЁН"
    print(f"\n  Итерация {iteration}: {status}  риск={audit.overall_risk_score:.1f}/10")
    if audit.vulnerabilities:
        classes = ", ".join(v.vuln_class for v in audit.vulnerabilities)
        print(f"  Уязвимости: {classes}")


# ── Удобная функция запуска ───────────────────────────────────────────────────

def run_pipeline(
    task_description: str,
    max_iterations: int = SQLSecuritySystem.DEFAULT_MAX_ITERATIONS,
    verbose: bool = True,
) -> SystemResult:
    """
    Запускает полный пайплайн с Groq-агентами.

    Args:
        task_description: задача на естественном языке
        max_iterations: максимум итераций (по умолчанию 5)
        verbose: печатать ли прогресс

    Returns:
        SystemResult
    """
    # Импортируем здесь чтобы не было циклических зависимостей
    from generator.generator import GroqSQLGenerator
    from auditor.auditor import GroqSecurityAuditor

    generator = GroqSQLGenerator()
    auditor = GroqSecurityAuditor()
    system = GroqSQLSecuritySystem(
        generator=generator,
        auditor=auditor,
        max_iterations=max_iterations,
    )

    if verbose:
        print(f"\n{'═' * 60}")
        print(f"  Задача: {task_description}")
        print(f"{'═' * 60}")

    result = system.run(task_description)

    if verbose:
        status = "✅ ОДОБРЕН" if result.approved else "❌ НЕ ОДОБРЕН"
        print(f"\n{status} за {result.iterations_used} итераций")
        print(f"\nФинальный SQL:\n{result.final_sql}")
        elapsed = result.metadata.get("execution_time_seconds", 0)
        print(f"\nВремя: {elapsed:.1f} сек")

    return result


# ── Быстрая проверка ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    from rag_pipeline.metrics import format_report, compute_batch

    tasks = [
        "показать топ-10 активных сотрудников с их компанией, отсортированных по фамилии",
        "посчитать количество кредитных договоров по каждой компании за последний год",
    ]

    results = []
    for task in tasks:
        r = run_pipeline(task, max_iterations=3)
        results.append(r)

    print("\n\n" + "═" * 60)
    batch = compute_batch(results)
    print(format_report(batch, title="End-to-end тест оркестратора"))
