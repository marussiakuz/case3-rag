# GreenData SQL Security System — Кейс 3

Многоагентная система генерации и аудита безопасности SQL-запросов для PostgreSQL.  
Хакатон «Проектный практикум», команда Роль C — RAG-инфраструктура.

---

## Структура репозитория

```
.
├── baseline1.py                  # Контракты заказчика (не менять сигнатуры!)
├── data_model.sql                # DDL схемы тестовой БД GreenData
├── schema.json                   # Машиночитаемая схема (генерируется автоматически)
├── schema_compact.json           # Компактная схема для инжекции в LLM-контекст
├── requirements.txt
├── docs/
│   ├── overview.md               # Обзор архитектуры для всей команды
│   ├── role_a_guide.md           # Инструкция для Роли A (SQL-паттерны)
│   └── role_b_guide.md           # Инструкция для Роли B (уязвимости)
└── rag_pipeline/
    ├── schema_parser.py          # DDL → schema.json / schema_compact.json
    ├── build_indices.py          # Строит FAISS-индексы из knowledge_base
    ├── rag_tools.py              # Публичный API для оркестратора
    ├── fetch_pg_docs.py          # Скачивает и нарезает разделы PostgreSQL 16 docs
    ├── knowledge_base/
    │   ├── generation/
    │   │   ├── pg_patterns.json  # 24 кастомных SQL-паттерна под схему GreenData
    │   │   └── pg_docs.json      # 55 чанков из PostgreSQL 16 docs (генерируется)
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

### 3. (Опционально) Скачать PostgreSQL 16 docs

```bash
python rag_pipeline/fetch_pg_docs.py
```

Скачивает 6 разделов документации PostgreSQL 16 и нарезает на чанки:

| Раздел | Содержание | Чанков |
|--------|-----------|--------|
| `queries-with` | CTE (WITH Queries), рекурсивные запросы | 11 |
| `queries-table-expressions` | JOIN, подзапросы, LATERAL | 12 |
| `functions-window` | Оконные функции (ROW_NUMBER, RANK, LAG…) | 3 |
| `functions-aggregate` | COUNT, SUM, AVG, FILTER, WITHIN GROUP… | 4 |
| `functions-datetime` | DATE_TRUNC, EXTRACT, интервалы, AT TIME ZONE | 13 |
| `plpgsql-statements` | PL/pgSQL: EXECUTE, USING, циклы, исключения | 12 |

Результат сохраняется в `knowledge_base/generation/pg_docs.json` и автоматически подхватывается при пересборке индекса. Для обновления до последней версии docs — просто запусти скрипт повторно.

### 4. Построить FAISS-индексы

```bash
python rag_pipeline/build_indices.py
```

Загружает модель `intfloat/multilingual-e5-small` (~90 MB, поддерживает русский и SQL).

**Generation index — 3 источника, 139 векторов:**

| Источник | Документов | Статус |
|----------|-----------|--------|
| Описания таблиц из `schema.json` | 60 | ✅ готово |
| Кастомные SQL-паттерны (`pg_patterns.json`) | 24 | ✅ готово, Роль A дополняет |
| Чанки PostgreSQL 16 docs (`pg_docs.json`) | 55 | ✅ готово, скачивается автоматически |

**Security index — 18 векторов** (9 классов уязвимостей × 2 документа: описание + пример).

Время сборки: ~30 секунд на CPU.

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

## Как расширять knowledge base (для команды)

### Роль A — добавить SQL-паттерны

Добавляй объекты в `knowledge_base/generation/pg_patterns.json`:

```json
{
  "pattern_id": "уникальный_id",
  "pattern_type": "Тип паттерна",
  "description": "Краткое описание когда использовать",
  "text": "Основной текст для эмбеддинга: объяснение, правила, особенности PostgreSQL",
  "example": "-- SQL-пример\nSELECT ..."
}
```

Уже готовы 24 паттерна (пагинация, JOIN, CTE, оконные функции, агрегаты, даты, LATERAL, UPDATE через CTE, маскирование sensitive-полей, PL/pgSQL и др.). Роль A добавляет паттерны под конкретные запросы из датасета.

### Роль B — добавить уязвимости

Добавляй объекты в `knowledge_base/security/vuln_classes.json` — дополнительные классы из OWASP/реальных атак или расширенные примеры к существующим 9 классам.

### После любых изменений — пересобрать индексы

```bash
python rag_pipeline/build_indices.py
```

Для обновления PG docs (если вышла новая версия PostgreSQL):

```bash
python rag_pipeline/fetch_pg_docs.py
python rag_pipeline/build_indices.py
```
