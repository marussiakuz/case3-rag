# GreenData SQL Security System — Кейс 3

Многоагентная система генерации и аудита безопасности SQL-запросов для PostgreSQL.  
Хакатон «Проектный практикум 2026», в команде Роль C — RAG-инфраструктура.

---

## Структура репозитория

```
.
├── baseline1.py                  # Контракты заказчика (не менять сигнатуры!)
├── data_model.sql                # DDL схемы тестовой БД GreenData
├── schema.json                   # Машиночитаемая схема (генерируется автоматически)
├── schema_compact.json           # Компактная схема для инжекции в LLM-контекст
├── requirements.txt
└── rag_pipeline/
    ├── schema_parser.py          # DDL → schema.json / schema_compact.json
    ├── build_indices.py          # Строит FAISS-индексы из knowledge_base
    ├── rag_tools.py              # Публичный API для оркестратора
    ├── knowledge_base/
    │   ├── generation/
    │   │   └── pg_patterns.json  # PostgreSQL паттерны и best practices
    │   └── security/
    │       └── vuln_classes.json # 9 классов уязвимостей с примерами
    └── indices/                  # FAISS-индексы (gitignored, строятся локально)
        ├── generation.faiss
        ├── generation_meta.json
        ├── security.faiss
        └── security_meta.json
```

---

## Быстрый старт

### 1. Установить зависимости

```bash
pip install -r requirements.txt
```

### 2. Сгенерировать schema.json из DDL

```bash
python rag_pipeline/schema_parser.py
```

Создаёт два файла в корне:
- `schema.json` (576 KB) — полная схема со всеми метаданными
- `schema_compact.json` (174 KB, ~44k токенов) — компактная версия для LLM-контекста

### 3. Построить FAISS-индексы

```bash
python rag_pipeline/build_indices.py
```

Загружает модель `intfloat/multilingual-e5-small` (~90 MB, поддерживает русский), индексирует:
- 60 таблиц из schema.json
- 14 PostgreSQL-паттернов из `knowledge_base/generation/pg_patterns.json`
- 9 классов уязвимостей (18 документов) из `knowledge_base/security/vuln_classes.json`

Время: ~30 секунд на CPU.

---

## Использование RAG-инструментов (для оркестратора)

```python
from rag_pipeline.rag_tools import (
    get_generation_context,
    get_security_context,
    get_table_context,
    get_sensitive_fields,
)

# Контекст для SQL-генератора (релевантные таблицы + PostgreSQL паттерны)
ctx = get_generation_context("показать активных сотрудников по подразделениям за последний месяц")
# → строка для инжекции в system prompt генератора

# Контекст для аудитора безопасности (релевантные классы уязвимостей)
sql = "DELETE FROM scp_application"
sec_ctx = get_security_context(sql)
# → строка с описанием релевантных уязвимостей для судьи

# Описания конкретных таблиц по имени (без семантического поиска)
tbl_ctx = get_table_context(["sys_employee", "sys_company"])

# Словарь sensitive-полей для SecurityAuditor (DIRECT_SENSITIVE)
sensitive = get_sensitive_fields()
# → {'sys_employee': ['email', 'phone', ...], 'sys_company': ['inn', ...], ...}
```

---

## Что содержит schema.json

Для каждой из 60 таблиц:

```json
{
  "sys_employee": {
    "comment": "Сотрудник",
    "primary_key": ["id"],
    "columns": {
      "id":    {"type": "bigint", "nullable": false, "comment": "Код", "is_sensitive": false},
      "email": {"type": "character varying(2000)", "nullable": true, "comment": "Email", "is_sensitive": true}
    },
    "foreign_keys": [
      {"columns": ["org_id"], "references_table": "sys_company", "references_columns": ["id"]}
    ],
    "text_description": "Таблица: sys_employee\nОписание: Сотрудник\n..."
  }
}
```

Дополнительно в корне объекта:
- `metadata` — дата генерации, диалект, число таблиц/колонок
- `sensitive_fields_summary` — сводка sensitive-полей по всей схеме (16 таблиц)

---

## Классы уязвимостей (из baseline)

| Ключ | Название | Риск |
|------|----------|------|
| `SQL_INJ_CLASSIC` | SQL Injection (классический) | 10 |
| `SQL_INJ_UNION` | Union-based Injection | 9 |
| `PLPGSQL_UNSAFE` | PL/pgSQL: небезопасный EXECUTE | 9 |
| `DML_NO_WHERE` | UPDATE/DELETE без WHERE | 9 |
| `SQL_INJ_TIME` | Time-based blind Injection | 8 |
| `PRIV_ESCALATE` | Privilege Escalation через EXECUTE | 8 |
| `DIRECT_SENSITIVE` | Прямой доступ к чувствительным полям | 6 |
| `SELECT_STAR` | Избыточный SELECT * | 5 |
| `NO_PAGINATION` | Отсутствие пагинации/LIMIT | 4 |

Порог одобрения: `overall_risk_score < 4.0` (из `SecurityAuditor.RISK_THRESHOLD`).

---

## Контракты (baseline1.py)

Оркестратор должен реализовать три метода, не меняя их сигнатуры:

```python
SQLGenerator.generate(task_description, sql_history, audit_feedback, iteration) -> str
SecurityAuditor.audit(sql_query, db_schema) -> AuditResult
SQLSecuritySystem.run(task_description) -> SystemResult
```

Точка входа для тестирования заказчиком:

```python
from baseline1 import run_sql_security_pipeline

result = run_sql_security_pipeline(
    task_description="показать активных клиентов за последний квартал",
    db_schema=json.load(open("schema.json"))["tables"],
    max_iterations=5,
    generator_kwargs={"model": "gpt-4o-mini", "api_key": "..."},
    auditor_kwargs={},
)
print(result.final_sql)
print(result.audit_log)
```

---

## Sensitive-поля в схеме GreenData

Автоматически определяются парсером. Всего 16 таблиц:

| Таблица | Sensitive-поля |
|---------|---------------|
| `sys_employee` | email, phone, email_confirmed, phone_confirmed, inner_emp_phone |
| `sys_company` | inn, attr_email, contact_phone |
| `credit_contract` | credit_contract_number, credit_amount, credit_start_date, credit_end_date, uid_credit |
| `scp_project_ans` | credit_analyst_id, is_blanc_credit, exp_limit_credit_rub, credit_group_id |
| `scp_sec_check_res` | credit_history_comm, sf_credit_history_comm |
| ... | (и ещё 11 таблиц) |

---

## Что добавить в индексы (для команды)

**`knowledge_base/generation/pg_patterns.json`** — добавляй новые паттерны по мере написания датасета:
```json
{"pattern_id": "my_pattern", "pattern_type": "...", "description": "...", "text": "...", "example": "..."}
```

**`knowledge_base/security/vuln_classes.json`** — Роль B добавляет примеры из OWASP/реальных атак.

После добавления документов — пересобрать индексы:
```bash
python rag_pipeline/build_indices.py
```
