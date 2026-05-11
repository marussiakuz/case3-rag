# Архитектура системы — краткий обзор для команды

## Что мы строим

Многоагентная система: пользователь описывает задачу текстом → система генерирует SQL → аудитор проверяет безопасность → если не прошло, LLM переписывает → цикл до 5 раз.

```
Задача (текст)
     │
     ▼
┌─────────────────┐
│  SQLGenerator   │ ← использует RAG: таблицы GreenData + SQL-паттерны
│  (Роль A вход)  │
└────────┬────────┘
         │ SQL
         ▼
┌─────────────────┐
│ SecurityAuditor │ ← использует RAG: классы уязвимостей
│  (Роль B вход)  │
└────────┬────────┘
         │ AuditResult (approved/rejected + vulnerabilities)
         ▼
   approved? ──YES──▶ SystemResult (final_sql, audit_log)
       │
      NO (итерация < 5)
       │
       └─────────────▶ SQLGenerator (следующая итерация с feedback)
```

---

## Файлы системы

```
Green Data/
├── baseline1.py              # Контракты (НЕ МЕНЯТЬ сигнатуры!)
├── data_model.sql            # DDL схемы GreenData (60 таблиц)
├── schema.json               # Машиночитаемая схема (генерируется)
├── schema_compact.json       # Компактная схема для LLM (генерируется)
├── requirements.txt
│
├── docs/                     # Ты здесь
│   ├── overview.md           # Этот файл
│   ├── role_a_guide.md       # Инструкция для Роли A (SQL-паттерны)
│   └── role_b_guide.md       # Инструкция для Роли B (уязвимости)
│
└── rag_pipeline/             # RAG-инфраструктура (Роль C)
    ├── schema_parser.py      # DDL → schema.json
    ├── build_indices.py      # Строит FAISS-индексы
    ├── fetch_pg_docs.py      # Скачивает PostgreSQL 16 docs
    ├── rag_tools.py          # Публичный API для оркестратора
    │
    ├── knowledge_base/
    │   ├── generation/
    │   │   ├── pg_patterns.json  # ← ФАЙЛ РОЛИ A (SQL-паттерны)
    │   │   └── pg_docs.json      # PostgreSQL 16 docs (авто)
    │   └── security/
    │       └── vuln_classes.json # ← ФАЙЛ РОЛИ B (уязвимости)
    │
    └── indices/              # FAISS-индексы (gitignored, собираются локально)
```

---

## Что делает каждая роль

| Роль | Задача | Файл |
|------|--------|------|
| **A** | Добавляет SQL-паттерны для генератора | `rag_pipeline/knowledge_base/generation/pg_patterns.json` |
| **B** | Добавляет/расширяет классы уязвимостей | `rag_pipeline/knowledge_base/security/vuln_classes.json` |
| **C** | RAG-инфраструктура (уже сделано) | `rag_pipeline/` |
| **Оркестратор** | Реализует контракты из baseline1.py | Своя папка |

---

## После любых изменений в knowledge_base

```bash
# Пересобрать FAISS-индексы (обязательно!)
python rag_pipeline/build_indices.py
```

Только после пересборки изменения в `pg_patterns.json` и `vuln_classes.json` вступят в силу.

---

## API для оркестратора

```python
from rag_pipeline.rag_tools import (
    get_generation_context,  # контекст для SQLGenerator
    get_security_context,    # контекст для SecurityAuditor
    get_table_context,       # описания конкретных таблиц
    get_sensitive_fields,    # словарь sensitive-полей
)

# Перед вызовом generate():
ctx = get_generation_context("активные заявки за последний месяц")

# Перед вызовом audit():
sec_ctx = get_security_context("DELETE FROM scp_application")
sensitive = get_sensitive_fields()
```

---

## Baseline контракты (не менять!)

```python
# Входы и выходы зафиксированы заказчиком в baseline1.py
SQLGenerator.generate(task_description, sql_history, audit_feedback, iteration) → str
SecurityAuditor.audit(sql_query, db_schema) → AuditResult
SQLSecuritySystem.run(task_description) → SystemResult
```

`RISK_THRESHOLD = 4.0` — порог одобрения: если `overall_risk_score < 4.0`, SQL одобрен.  
`DEFAULT_MAX_ITERATIONS = 5` — максимальное число попыток генератора.

---

## Быстрый старт (первый запуск)

```bash
# 1. Установить зависимости
pip install -r requirements.txt

# 2. Сгенерировать schema.json из DDL
python rag_pipeline/schema_parser.py

# 3. (Опционально) Скачать PostgreSQL 16 docs
python rag_pipeline/fetch_pg_docs.py

# 4. Собрать FAISS-индексы (~30 сек на CPU)
python rag_pipeline/build_indices.py
```
