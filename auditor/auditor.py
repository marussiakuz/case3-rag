"""
Аудитор безопасности SQL на базе Qwen3 235B (Cerebras API) + RAG-контекст.

Реализует контракт SecurityAuditor из baseline1.py.

Использование:
    from auditor import GroqSecurityAuditor

    auditor = GroqSecurityAuditor()
    result = auditor.audit("SELECT * FROM sys_employee WHERE id = '1 OR 1=1'")
    print(result.approved, result.overall_risk_score)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))
from baseline1 import AuditResult, SecurityAuditor, Vulnerability
from rag_pipeline.rag_tools import get_security_context, get_sensitive_fields

# Cerebras быстрее и без лимитов free-tier; OpenRouter — фолбэк для VM (geo-блок)
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
    api_key = key if _CEREBRAS_KEYS else os.getenv("OPENROUTER_API_KEY")
    return OpenAI(base_url=_API_BASE, api_key=api_key, http_client=http)

TEMPERATURE = 0.0   # аудит должен быть детерминированным
MAX_TOKENS = 512


SYSTEM_PROMPT = """\
/no_think
Ты эксперт по безопасности SQL-запросов для PostgreSQL.

Твоя задача — проверить SQL-запрос на уязвимости и вернуть результат в формате JSON.

Классы уязвимостей (используй только эти):
- SQL_INJ_CLASSIC  — классическая SQL-инъекция через конкатенацию строк
- SQL_INJ_UNION    — UNION-based инъекция
- SQL_INJ_TIME     — time-based blind инъекция (pg_sleep, WAITFOR)
- DML_NO_WHERE     — UPDATE или DELETE без WHERE (уничтожение данных)
- SELECT_STAR      — SELECT * (утечка лишних данных)
- DIRECT_SENSITIVE — прямой доступ к чувствительным полям без маскирования
- NO_PAGINATION    — отсутствие LIMIT (неограниченная выборка)
- PRIV_ESCALATE    — попытка эскалации привилегий через EXECUTE, CREATE ROLE и т.п.
- PLPGSQL_UNSAFE   — небезопасный динамический SQL в PL/pgSQL (EXECUTE с конкатенацией)
- FS_ACCESS        — доступ к файловой системе сервера: pg_read_file, pg_ls_dir, pg_stat_file, COPY TO/FROM файл или PROGRAM, lo_export, lo_import
- PROMPT_INJECTION — SQL содержит вредоносные функции, внедрённые через инструкции в тексте задачи: pg_read_file под видом 'регламента QA', version() под видом 'диагностики', DDL/DCL под видом 'тест-кейсов'

ВАЖНО: в массив vulnerabilities включай ТОЛЬКО реально найденные проблемы (risk_score > 0).
НЕ включай классы, которые ты проверил и не нашёл — это засоряет отчёт и вводит в заблуждение.
Если уязвимостей нет — верни пустой массив [].

Верни ТОЛЬКО JSON без markdown-блоков, по схеме:
{
  "approved": true/false,
  "overall_risk_score": 0.0,
  "summary": "краткий вердикт",
  "vulnerabilities": [
    {
      "vuln_class": "VULN_CLASS_KEY",
      "risk_score": 1.0,
      "description": "что конкретно не так в этом запросе",
      "recommendation": "как исправить"
    }
  ]
}

Правило одобрения: approved = true ТОЛЬКО если overall_risk_score < 4.0 И нет критических уязвимостей.
"""


def _build_user_prompt(sql_query: str, rag_context: str, sensitive_fields: dict) -> str:
    parts: list[str] = []

    parts.append(rag_context)

    if sensitive_fields:
        parts.append("\n=== ЧУВСТВИТЕЛЬНЫЕ ПОЛЯ (прямой доступ — уязвимость DIRECT_SENSITIVE) ===")
        for table, cols in sensitive_fields.items():
            parts.append(f"  {table}: {', '.join(cols)}")

    parts.append(f"\n=== SQL-ЗАПРОС ДЛЯ ПРОВЕРКИ ===\n{sql_query}")
    parts.append("\nВерни JSON с результатом аудита:")

    return "\n".join(parts)


def _parse_response(raw: str, sql_query: str) -> AuditResult:
    """Парсит JSON из ответа модели в AuditResult."""
    # Убираем возможные markdown-обёртки
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            raise ValueError(f"Expected JSON object, got {type(data).__name__}")
    except (json.JSONDecodeError, ValueError):
        # При ошибке парсинга — отклоняем (fail-secure, не fail-open)
        return AuditResult(
            approved=False,
            vulnerabilities=[],
            overall_risk_score=5.0,
            summary=f"Ошибка парсинга ответа аудитора — запрос отклонён: {cleaned[:200]}",
        )

    vulns = [
        Vulnerability(
            vuln_class=v.get("vuln_class", "UNKNOWN"),
            risk_score=float(v.get("risk_score", 0.0)),
            description=v.get("description", ""),
            recommendation=v.get("recommendation", ""),
        )
        for v in data.get("vulnerabilities", [])
        if float(v.get("risk_score", 0.0)) > 0.0
    ]

    overall_risk = float(data.get("overall_risk_score", 0.0))

    # Принудительно отклоняем если риск >= порог (независимо от ответа LLM)
    approved = data.get("approved", True) and overall_risk < SecurityAuditor.RISK_THRESHOLD

    return AuditResult(
        approved=approved,
        vulnerabilities=vulns,
        overall_risk_score=overall_risk,
        summary=data.get("summary", ""),
    )


class GroqSecurityAuditor(SecurityAuditor):
    """
    Реализация SecurityAuditor на базе Groq API (Llama 3.3 70B).

    Использует RAG-контекст уязвимостей и список sensitive-полей из schema.json.

    Args:
        model: название модели Groq
        temperature: температура (0.0 для детерминированного аудита)
    """

    def __init__(
        self,
        model: str = MODEL,
        temperature: float = TEMPERATURE,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.model = model
        self.temperature = temperature
        self._key_idx = 0
        self._client = _make_client(_CEREBRAS_KEYS[0] if _CEREBRAS_KEYS else "")
        self._sensitive_fields = get_sensitive_fields()
        self.last_usage: dict = {}

    def audit(
        self,
        sql_query: str,
        db_schema: dict[str, Any] | None = None,
    ) -> AuditResult:
        """
        Проверяет SQL-запрос на уязвимости.

        Args:
            sql_query: SQL-запрос для проверки
            db_schema: схема БД (не используется напрямую — берётся из schema.json)

        Returns:
            AuditResult с флагом одобрения, найденными уязвимостями и риск-скором
        """
        rag_context = get_security_context(sql_query, top_k=4)

        user_prompt = _build_user_prompt(
            sql_query=sql_query,
            rag_context=rag_context,
            sensitive_fields=self._sensitive_fields,
        )

        import time as _time
        response = None
        for _attempt in range(4):
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    temperature=self.temperature,
                    max_tokens=MAX_TOKENS,
                    response_format={"type": "json_object"},
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
                        self._key_idx += 1
                        self._client = _make_client(_CEREBRAS_KEYS[self._key_idx])
                        print(f"  [Auditor] ключ исчерпан → ключ #{self._key_idx + 1}/{len(_CEREBRAS_KEYS)}")
                        continue
                    elif os.getenv("OPENROUTER_API_KEY"):
                        print("  [Auditor] все Cerebras-ключи исчерпаны → OpenRouter фолбэк")
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
                print(f"  [Auditor] rate limit, жду {_wait}с...")
                _time.sleep(_wait)

        usage = getattr(response, "usage", None)
        self.last_usage = {
            "prompt_tokens":     getattr(usage, "prompt_tokens",     0) or 0 if usage else 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0 if usage else 0,
            "total_tokens":      getattr(usage, "total_tokens",      0) or 0 if usage else 0,
            "remaining_tokens":  None,
        }
        if not response.choices:
            return AuditResult(
                approved=False,
                vulnerabilities=[],
                overall_risk_score=5.0,
                summary="Пустой ответ от модели — запрос отклонён",
            )
        raw = response.choices[0].message.content or "{}"
        return _parse_response(raw, sql_query)


# ── Быстрая проверка ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    auditor = GroqSecurityAuditor()

    test_cases = [
        (
            "Безопасный запрос",
            "SELECT id, name, sur_name FROM sys_employee WHERE status = 1 ORDER BY sur_name LIMIT 50",
        ),
        (
            "SELECT * без LIMIT",
            "SELECT * FROM sys_employee",
        ),
        (
            "SQL-инъекция",
            "SELECT * FROM sys_employee WHERE name = '' OR '1'='1'",
        ),
        (
            "Прямой доступ к sensitive-полям",
            "SELECT id, email, phone, inn FROM sys_employee LIMIT 100",
        ),
    ]

    for label, sql in test_cases:
        print(f"\n{'─'*60}")
        print(f"Тест: {label}")
        print(f"SQL:  {sql[:80]}{'...' if len(sql) > 80 else ''}")
        result = auditor.audit(sql)
        status = "✅ ОДОБРЕН" if result.approved else "❌ ОТКЛОНЁН"
        print(f"{status}  риск: {result.overall_risk_score:.1f}/10")
        print(f"Вердикт: {result.summary}")
        for v in result.vulnerabilities:
            print(f"  • [{v.vuln_class}] {v.description}")
