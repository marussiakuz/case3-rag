# Инструкция для Роли A — SQL-паттерны

## Что ты делаешь

Ты наполняешь базу знаний RAG-системы SQL-паттернами, специфичными для схемы GreenData. Генератор SQL (LLM) использует эти паттерны как примеры и шаблоны при создании запросов под задачи пользователей.

**Твой единственный файл:** `rag_pipeline/knowledge_base/generation/pg_patterns.json`

---

## Быстрый старт

```bash
# 1. Сделай изменения в pg_patterns.json
# 2. Пересобери индекс
python rag_pipeline/build_indices.py

# 3. Проверь результат
python -c "
from rag_pipeline.rag_tools import get_generation_context
ctx = get_generation_context('ТВОЙ ЗАПРОС ЗДЕСЬ')
print(ctx)
"
```

---

## Формат одного паттерна

```json
{
  "pattern_id": "уникальный_snake_case_id",
  "pattern_type": "Краткое название типа",
  "description": "Одна строка — когда и зачем использовать этот паттерн",
  "use_case": "Конкретные сценарии применения (можно несколько через запятую)",
  "text": "Основной текст для поиска по смыслу. Объясни: что за паттерн, когда применять, ключевые правила PostgreSQL, связи с таблицами GreenData. Это то, что увидит LLM в контексте.",
  "example": "-- Комментарий что делает пример\nSELECT ..."
}
```

**Обязательные поля:** `pattern_id`, `pattern_type`, `description`, `text`, `example`  
**Необязательное:** `use_case` (но желательно)

---

## Как правильно писать поле `text`

Поле `text` — это то, что система найдёт по запросу пользователя и вставит в контекст генератора. Пиши так, чтобы LLM понял:

1. **Суть паттерна** — что за конструкция PostgreSQL
2. **Когда применять** — при каких задачах пользователя
3. **Ключевые правила** — что обязательно включить/избежать
4. **Связи с GreenData** — какие таблицы/поля задействованы

**Хороший `text`:**
```
Пагинация в PostgreSQL. LIMIT и OFFSET для постраничного вывода.
Всегда добавляй ORDER BY для стабильного порядка при пагинации.
Для подсчёта общего числа строк: SELECT COUNT(*) FROM ...
Cursor-based пагинация для больших таблиц: WHERE id > $last_id ORDER BY id LIMIT $page_size.
```

**Плохой `text`:**
```
Используй LIMIT и OFFSET.
```

---

## Правила для поля `example`

- Используй **только таблицы и колонки из schema.json** (не выдумывай имена)
- Всегда **параметризуй** пользовательский ввод: `$1`, `$2`, а не конкатенация строк
- Всегда добавляй **`LIMIT`** к SELECT-запросам (иначе аудитор снизит оценку)
- Используй **алиасы таблиц**: `FROM sys_employee e`, а не `FROM sys_employee`
- Пиши **реалистичные запросы**, а не абстрактный `SELECT col1, col2 FROM table1`

**Пример правильного SQL в `example`:**
```sql
-- Активные кредитные договоры компании
SELECT
  cc.id,
  cc.credit_contract_number,
  cc.credit_amount,
  cc.credit_start_date,
  cc.credit_end_date
FROM credit_contract cc
WHERE cc.org_id = $1
  AND cc.status = 1
ORDER BY cc.credit_start_date DESC
LIMIT 100;
```

---

## Какие паттерны уже есть (не дублируй)

| pattern_id | Что покрывает |
|------------|---------------|
| `pagination` | LIMIT/OFFSET, cursor-based pagination |
| `join_basic` | INNER JOIN, LEFT JOIN между основными таблицами |
| `aggregate_groupby` | COUNT, SUM, AVG, GROUP BY, HAVING |
| `window_functions` | ROW_NUMBER, RANK, DENSE_RANK, SUM OVER, LAG |
| `cte` | WITH, рекурсивные CTE |
| `date_filter` | CURRENT_DATE, INTERVAL, DATE_TRUNC, EXTRACT |
| `subquery_exists` | EXISTS, NOT EXISTS vs IN |
| `null_handling` | COALESCE, IS NULL, NULLIF |
| `distinct_dedup` | DISTINCT, DISTINCT ON |
| `full_text_search` | ILIKE, tsvector, plainto_tsquery |
| `explain_analyze` | EXPLAIN ANALYZE для диагностики |
| `insert_returning` | INSERT ... RETURNING |
| `transaction_safety` | BEGIN, COMMIT, ROLLBACK, SAVEPOINT |
| `greendata_common_tables` | Обзор 10 ключевых таблиц GreenData |
| `multi_join_greendata` | 4+ таблиц: заявка + статус + компания + аналитик |
| `credit_analytics` | Аналитика по кредитным договорам |
| `employee_org_report` | Отчёты по сотрудникам и подразделениям |
| `update_with_cte` | UPDATE через CTE |
| `window_rank_per_group` | RANK/ROW_NUMBER с деловой логикой |
| `financial_transactions` | Проводки afhd_ac_trans_link |
| `upsert_on_conflict` | INSERT ON CONFLICT DO UPDATE |
| `plpgsql_function` | Хранимые функции на PL/pgSQL |
| `lateral_join` | LATERAL JOIN для коррелированных подзапросов |
| `masking_sensitive` | Маскирование email, phone, inn в SELECT |

---

## Какие паттерны стоит добавить

Добавляй паттерн, если видишь, что в датасете задач есть запросы, для которых нет подходящего шаблона. Приоритетные направления:

### Если в датасете есть задачи про иерархию
```json
{
  "pattern_id": "recursive_hierarchy",
  "pattern_type": "Рекурсивный обход иерархии",
  "description": "Обход дерева организаций или подразделений через WITH RECURSIVE",
  "use_case": "Найти всех дочерних сотрудников, всю цепочку подразделений",
  ...
}
```

### Если в датасете есть задачи про pivot/crosstab
```json
{
  "pattern_id": "pivot_crosstab",
  "pattern_type": "Сводная таблица (CROSSTAB)",
  "description": "Разворот строк в столбцы с помощью FILTER или crosstab из tablefunc",
  ...
}
```

### Если в датасете есть задачи про JSON-поля
```json
{
  "pattern_id": "jsonb_operations",
  "pattern_type": "Работа с JSONB",
  "description": "Извлечение и фильтрация по JSONB полям в PostgreSQL",
  ...
}
```

---

## Важные таблицы GreenData (справка)

Основные таблицы, которые чаще всего используются в запросах:

| Таблица | Описание | Ключевые поля |
|---------|----------|---------------|
| `sys_employee` | Сотрудники | `id`, `name`, `sur_name`, `org_id`, `status` |
| `sys_company` | Компании/организации | `id`, `name`, `short_name`, `status` |
| `sys_state` | Справочник статусов | `id`, `name` |
| `scp_application` | Кредитные заявки | `id`, `create_date`, `state_id`, `org_id`, `credit_logic_id` |
| `scp_project_ans` | Ответы аналитика по заявке | `id`, `application_id`, `credit_analyst_id`, `credit_amount` |
| `credit_contract` | Кредитные договоры | `id`, `org_id`, `credit_amount`, `credit_start_date`, `credit_end_date` |
| `afhd_ac_trans_link` | Финансовые проводки | `id`, `account_num_id`, `account_date`, `after_amount` |
| `type_loan` | Тип кредита | `id`, `name` |

Полная схема со всеми 60 таблицами — в `schema_compact.json` (читаемый формат) или `schema.json` (полная машиночитаемая версия).

---

## Как тестировать свои паттерны

После добавления паттерна пересобери индекс и проверь, что он находится по нужным запросам:

```python
# test_my_pattern.py
from rag_pipeline.rag_tools import get_generation_context

# Запрос, который должен найти твой паттерн
test_queries = [
    "показать иерархию подразделений",
    "сводная таблица заявок по месяцам",
    "сотрудники в дочерних организациях",
]

for q in test_queries:
    ctx = get_generation_context(q, top_k=3)
    print(f"\n--- Запрос: {q} ---")
    print(ctx[:500])
    print("...")
```

Запустить:
```bash
python test_my_pattern.py
```

---

## Типичные ошибки

### 1. Несуществующие таблицы в примере

```sql
-- НЕПРАВИЛЬНО (таблицы employees нет в GreenData)
SELECT * FROM employees WHERE department_id = $1;

-- ПРАВИЛЬНО
SELECT * FROM sys_employee WHERE org_id = $1 LIMIT 100;
```

### 2. SELECT * без LIMIT

```sql
-- НЕПРАВИЛЬНО (аудитор поставит риск за SELECT * и NO_PAGINATION)
SELECT * FROM scp_application WHERE status = 1;

-- ПРАВИЛЬНО
SELECT id, create_date, state_id, org_id
FROM scp_application
WHERE status = 1
ORDER BY create_date DESC
LIMIT 100;
```

### 3. Конкатенация вместо параметров

```sql
-- НЕПРАВИЛЬНО (SQL injection risk!)
WHERE name = ''' || user_input || '''

-- ПРАВИЛЬНО
WHERE name = $1
```

### 4. Повторяющийся pattern_id

Перед добавлением проверь, что такой `pattern_id` ещё не существует в файле:
```bash
grep -c '"pattern_id"' rag_pipeline/knowledge_base/generation/pg_patterns.json
```

---

## Рабочий процесс (checklist)

- [ ] Изучил список существующих паттернов — не дублирую
- [ ] Добавил паттерн в `pg_patterns.json` (конец массива, перед `]`)
- [ ] Проверил, что JSON валидный: `python -m json.tool rag_pipeline/knowledge_base/generation/pg_patterns.json`
- [ ] Пересобрал индекс: `python rag_pipeline/build_indices.py`
- [ ] Проверил, что паттерн находится по целевому запросу
- [ ] Убедился, что SQL в `example` использует только реальные таблицы GreenData
- [ ] Убедился, что SQL в `example` содержит `LIMIT` (если SELECT) и параметры `$1`/`$2` (если есть фильтр по данным пользователя)
