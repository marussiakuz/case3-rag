"""
Мета-аудитор на базе Qwen3 235B (Cerebras API).

Внешний контур анализа — не часть продуктовой архитектуры.
Задача: анализировать SystemResult, выявлять паттерны ошибок генератора и аудитора,
сохранять разборы в RAG-индекс 'solutions' для дистилляции знания в слабые модели.

Два полезных эффекта:
  (a) Закрывает вопрос «как генератор учится между итерациями» — через RAG-поиск
      по похожим задачам с уже разобранными ошибками.
  (b) Дистилляция знания сильной модели (Qwen3 235B) в подсказки для генератора.

Использование:
    from meta_auditor.opus_reviewer import OpusMetaAuditor

    reviewer = OpusMetaAuditor()
    analysis = reviewer.review_and_save(
        task_description="показать топ-10 активных сотрудников по фамилии",
        result=system_result,
    )
    print(analysis["lesson_for_generator"])
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np
from cerebras.cloud.sdk import Cerebras
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer

load_dotenv()

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from baseline1 import SystemResult
from db.rag_store import insert_embedding

MODEL = "qwen-3-235b-a22b-instruct-2507"
TEMPERATURE = 0.0
MAX_TOKENS = 1024
INDEX_NAME = "solutions"
EMBED_MODEL = "intfloat/multilingual-e5-small"
PASSAGE_PREFIX = "passage: "

SYSTEM_PROMPT = """\
/no_think
Ты старший эксперт по безопасности SQL и архитектуре LLM-систем.

Тебе передают лог работы автоматической системы генерации SQL:
- Генератор (Qwen3) создаёт SQL по текстовому описанию.
- Аудитор (Qwen3) проверяет SQL на уязвимости.
- Они итерируют до 5 раз, пока аудитор не одобрит запрос.

Твоя задача — внешний мета-анализ:
1. Определить, где и почему генератор ошибался.
2. Оценить, был ли аудитор прав (не пропустил ли уязвимости, не был ли слишком строг).
3. Сформулировать идеальный подход к этой задаче.
4. Написать чёткий урок для будущих попыток генератора.

Верни ТОЛЬКО JSON без markdown:
{
  "task_type": "тип задачи: simple_select | join | aggregation | window_function | cte | plpgsql | mixed",
  "generator_errors": ["паттерн ошибки 1", "паттерн ошибки 2"],
  "auditor_verdict": "correct | too_strict | too_lenient | partially_correct",
  "auditor_notes": "объяснение вердикта по аудитору (1-2 предложения)",
  "correct_sql_approach": "как должен выглядеть правильный SQL для этой задачи (1-3 предложения)",
  "lesson_for_generator": "конкретный и применимый урок для генератора (2-4 предложения)",
  "searchable_text": "полный текст для индексирования: тип задачи + ошибки + урок (5-8 предложений)"
}

Правила:
- Будь конкретен: называй таблицы, колонки, SQL-паттерны.
- lesson_for_generator должен начинаться с действия: «Для задач типа X нужно ...»
- searchable_text — это то, что будет найдено при поиске по похожей задаче в будущем.
"""


class OpusMetaAuditor:
    """
    Мета-аудитор на базе Qwen3 235B (Cerebras API).

    Анализирует SystemResult и сохраняет разборы в RAG-индекс 'solutions'.
    Не является частью продуктовой архитектуры — запускается как внешний контур.
    """

    def __init__(self, model: str = MODEL) -> None:
        self._client = Cerebras(api_key=os.getenv("CEREBRAS_API_KEY"))
        self.model = model
        self._embed_model: SentenceTransformer | None = None

    def _get_embed_model(self) -> SentenceTransformer:
        if self._embed_model is None:
            self._embed_model = SentenceTransformer(EMBED_MODEL)
        return self._embed_model

    def _embed(self, text: str) -> np.ndarray:
        model = self._get_embed_model()
        return model.encode(
            [PASSAGE_PREFIX + text],
            normalize_embeddings=True,
        ).astype("float32")[0]

    def _build_user_prompt(self, task_description: str, result: SystemResult) -> str:
        parts: list[str] = [f"Задача пользователя: {task_description}\n"]
        parts.append(f"Итого итераций: {result.iterations_used}")
        parts.append(f"Финальный статус: {'ОДОБРЕН' if result.approved else 'ОТКЛОНЁН'}\n")

        for log in result.iterations_log:
            status = "✅ одобрен" if log.audit_result.approved else "❌ отклонён"
            parts.append(f"── Итерация {log.iteration} [{status}] ──")
            parts.append(f"SQL:\n{log.sql_query}")
            parts.append(f"Риск-скор: {log.audit_result.overall_risk_score:.1f}/10")
            parts.append(f"Вердикт: {log.audit_result.summary}")
            if log.audit_result.vulnerabilities:
                parts.append("Уязвимости:")
                for v in log.audit_result.vulnerabilities:
                    parts.append(
                        f"  [{v.vuln_class}] риск {v.risk_score}/10 — "
                        f"{v.description} / {v.recommendation}"
                    )
            parts.append("")

        parts.append(f"Финальный SQL:\n{result.final_sql}")
        parts.append("\nВерни JSON с мета-анализом:")
        return "\n".join(parts)

    def review(self, task_description: str, result: SystemResult) -> dict[str, Any]:
        """
        Анализирует SystemResult через Qwen3 235B.

        Returns:
            Словарь: task_type, generator_errors, auditor_verdict,
            auditor_notes, correct_sql_approach, lesson_for_generator, searchable_text
        """
        user_prompt = self._build_user_prompt(task_description, result)

        response = self._client.chat.completions.create(
            model=self.model,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )

        raw = response.choices[0].message.content or "{}"
        try:
            analysis = json.loads(raw)
        except json.JSONDecodeError:
            analysis = {
                "task_type": "unknown",
                "generator_errors": [],
                "auditor_verdict": "unknown",
                "auditor_notes": "Ошибка парсинга ответа мета-аудитора",
                "correct_sql_approach": "",
                "lesson_for_generator": "",
                "searchable_text": f"Задача: {task_description}. Ошибка мета-анализа.",
            }

        analysis["task_description"] = task_description
        analysis["approved"] = result.approved
        analysis["iterations_used"] = result.iterations_used
        return analysis

    def save_to_rag(self, task_description: str, analysis: dict[str, Any]) -> None:
        """Сохраняет анализ в RAG-индекс 'solutions'."""
        searchable_text = analysis.get("searchable_text", "")
        if not searchable_text:
            return

        metadata = {
            "source": "meta_audit",
            "task_description": task_description,
            "task_type": analysis.get("task_type", "unknown"),
            "generator_errors": analysis.get("generator_errors", []),
            "auditor_verdict": analysis.get("auditor_verdict", ""),
            "correct_sql_approach": analysis.get("correct_sql_approach", ""),
            "lesson_for_generator": analysis.get("lesson_for_generator", ""),
            "approved": analysis.get("approved", False),
            "iterations_used": analysis.get("iterations_used", 0),
            "text": searchable_text,
        }

        insert_embedding(INDEX_NAME, searchable_text, self._embed(searchable_text), metadata)
        print(f"  [MetaAudit] Урок сохранён → '{task_description[:60]}'")

    def review_and_save(
        self, task_description: str, result: SystemResult
    ) -> dict[str, Any]:
        """Анализирует SystemResult и сразу сохраняет урок в RAG."""
        analysis = self.review(task_description, result)
        self.save_to_rag(task_description, analysis)
        return analysis


# ── Быстрая проверка ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    from datetime import datetime
    from baseline1 import AuditResult, IterationLog, Vulnerability

    mock_result = SystemResult(
        final_sql="SELECT id, name, sur_name FROM sys_employee WHERE status = 1 ORDER BY sur_name LIMIT 50",
        approved=True,
        iterations_used=2,
        iterations_log=[
            IterationLog(
                timestamp=datetime.now(),
                iteration=1,
                sql_query="SELECT * FROM sys_employee",
                audit_result=AuditResult(
                    approved=False,
                    vulnerabilities=[
                        Vulnerability("SELECT_STAR", 5.0, "SELECT * возвращает все колонки", "Указать колонки явно"),
                        Vulnerability("NO_PAGINATION", 4.0, "Отсутствует LIMIT", "Добавить LIMIT"),
                    ],
                    overall_risk_score=5.0,
                    summary="Отклонён: SELECT * и нет LIMIT",
                ),
                revision_notes="Первая генерация",
            ),
            IterationLog(
                timestamp=datetime.now(),
                iteration=2,
                sql_query="SELECT id, name, sur_name FROM sys_employee WHERE status = 1 ORDER BY sur_name LIMIT 50",
                audit_result=AuditResult(approved=True, vulnerabilities=[], overall_risk_score=0.0, summary="OK"),
                revision_notes="Исправлено",
            ),
        ],
        audit_log="",
        metadata={},
    )

    reviewer = OpusMetaAuditor()
    analysis = reviewer.review_and_save(
        task_description="показать топ-50 активных сотрудников по фамилии",
        result=mock_result,
    )
    print(f"\nТип: {analysis['task_type']}")
    print(f"Ошибки: {analysis['generator_errors']}")
    print(f"Урок: {analysis['lesson_for_generator']}")
