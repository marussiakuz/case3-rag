# Инструкция для Роли B — Классы уязвимостей

## Что ты делаешь

Ты наполняешь базу знаний RAG-системы описаниями уязвимостей SQL-запросов. Аудитор безопасности (SecurityAuditor) использует эту базу, чтобы понимать, что искать в генерируемых SQL и как объяснять проблемы.

**Твой единственный файл:** `rag_pipeline/knowledge_base/security/vuln_classes.json`

---

## Быстрый старт

```bash
# 1. Сделай изменения в vuln_classes.json
# 2. Проверь валидность JSON
python -m json.tool rag_pipeline/knowledge_base/security/vuln_classes.json > /dev/null && echo "OK"

# 3. Пересобери индекс
python rag_pipeline/build_indices.py

# 4. Проверь результат
python -c "
from rag_pipeline.rag_tools import get_security_context
sql = 'DELETE FROM scp_application'
ctx = get_security_context(sql)
print(ctx)
"
```

---

## Формат одной уязвимости

```json
{
  "vuln_class": "UNIQUE_KEY",
  "name": "Человекочитаемое название",
  "risk_score": 7,
  "description": "Одна-две строки: что за уязвимость и как она проявляется",
  "why_dangerous": "Что конкретно может сделать атакующий или что может пойти не так",
  "detection_patterns": [
    "признак 1 — что искать в SQL",
    "признак 2",
    "признак 3"
  ],
  "example_bad": "-- Комментарий почему это плохо\nSQL ПРИМЕР УЯЗВИМОГО КОДА",
  "example_good": "-- Комментарий почему это безопасно\nSQL ПРИМЕР БЕЗОПАСНОГО КОДА",
  "recommendation": "Конкретный actionable совет для разработчика (1-2 предложения)",
  "text": "Полный текст для эмбеддинга: название, риск, признаки, защита. Это ключевое поле — именно по нему система находит уязвимость."
}
```

**Обязательные поля:** все перечисленные выше.

---

## Как правильно писать поле `text`

`text` — главное поле для семантического поиска. По нему система находит уязвимость, когда аудитор анализирует SQL. Включай:

1. **Название + риск** (цифра /10)
2. **Признаки** — что за паттерны в SQL сигнализируют об уязвимости
3. **Последствия** — что плохого произойдёт
4. **Защита** — краткое описание fix'а

**Хороший `text`:**
```
UPDATE DELETE без WHERE. Риск 9/10. Массовое изменение или удаление
всех строк таблицы. Признаки: UPDATE или DELETE без секции WHERE,
UPDATE с WHERE 1=1, TRUNCATE TABLE. Катастрофические последствия:
потеря всех кредитных договоров (credit_contract), заявок (scp_application).
Защита: обязательный WHERE с конкретным условием, фильтрация по primary key.
```

---

## Правила для полей `example_bad` и `example_good`

**Критически важно:** JSON не допускает двойные кавычки `"` внутри строк без экранирования. Следуй этим правилам:

### Правило 1: Не используй двойные кавычки в SQL примерах

```json
// НЕПРАВИЛЬНО — JSON сломается!
"example_bad": "SELECT * FROM sys_employee WHERE name = \" + user_input + \";"

// ПРАВИЛЬНО — используй одинарные кавычки для строк в SQL
"example_bad": "-- УЯЗВИМО\nSELECT * FROM sys_employee WHERE name = '' || user_input || '';"
```

### Правило 2: Экранируй двойные кавычки через `\"`

```json
// Если ОБЯЗАТЕЛЬНО нужны двойные кавычки — экранируй
"example_bad": "-- УЯЗВИМО\nquery = \"SELECT * FROM \" + table_name"
```

### Правило 3: Переносы строк через `\n`

```json
"example_bad": "-- УЯЗВИМО\nDELETE FROM scp_application;\n\n-- УЯЗВИМО\nUPDATE sys_employee SET status = 0;"
```

### Правило 4: Используй реальные таблицы GreenData

```sql
-- ПРАВИЛЬНО: реальные таблицы
DELETE FROM scp_application WHERE id = $1;
SELECT id, name FROM sys_employee WHERE org_id = $1;

-- НЕПРАВИЛЬНО: выдуманные таблицы
DELETE FROM orders WHERE id = $1;
SELECT * FROM users;
```

---

## Какие уязвимости уже есть (не дублируй)

| vuln_class | Название | risk_score | Что покрывает |
|------------|----------|-----------|---------------|
| `SQL_INJ_CLASSIC` | SQL Injection (классический) | 10 | Конкатенация, format() без USING |
| `SQL_INJ_UNION` | Union-based Injection | 9 | UNION SELECT для кражи данных |
| `PLPGSQL_UNSAFE` | PL/pgSQL: небезопасный EXECUTE | 9 | EXECUTE без USING, динамический SQL |
| `DML_NO_WHERE` | UPDATE/DELETE без WHERE | 9 | Массовое изменение/удаление |
| `SQL_INJ_TIME` | Time-based blind Injection | 8 | pg_sleep, CASE WHEN delay |
| `PRIV_ESCALATE` | Privilege Escalation через EXECUTE | 8 | SET ROLE, ALTER USER, GRANT в SQL |
| `DIRECT_SENSITIVE` | Прямой доступ к чувствительным полям | 6 | email, phone, inn, credit_amount без маскирования |
| `SELECT_STAR` | Избыточный SELECT * | 5 | SELECT * раскрывает sensitive поля |
| `NO_PAGINATION` | Отсутствие пагинации/LIMIT | 4 | SELECT без LIMIT → OOM, DoS |

---

## Что можно добавить

### Вариант 1: Новый класс уязвимости

Добавь новый объект в массив. Хорошие кандидаты из OWASP/реальной практики:

**`SCHEMA_LEAK`** — раскрытие структуры БД через `information_schema`
```json
{
  "vuln_class": "SCHEMA_LEAK",
  "name": "Разведка схемы через information_schema",
  "risk_score": 7,
  ...
  "example_bad": "-- УЯЗВИМО: атакующий узнаёт все таблицы\nSELECT table_name FROM information_schema.tables\nWHERE table_schema = 'public';",
  ...
}
```

**`EXCESSIVE_PRIVILEGE`** — запрос данных за пределами своей области (нарушение мандата)
```json
{
  "vuln_class": "EXCESSIVE_PRIVILEGE",
  "name": "Избыточный доступ к данным вне мандата",
  "risk_score": 6,
  ...
}
```

**`TIMING_ORACLE`** — утечка информации через время ответа (без pg_sleep)
```json
{
  "vuln_class": "TIMING_ORACLE",
  "name": "Timing oracle через условные выражения",
  "risk_score": 7,
  ...
}
```

**`FORCE_INDEX_BYPASS`** — запросы, намеренно обходящие индексы
```json
{
  "vuln_class": "FORCE_INDEX_BYPASS",
  "name": "Обход индексов через функции над колонками",
  "risk_score": 4,
  ...
}
```

### Вариант 2: Расширить примеры существующего класса

В текущей версии у каждого класса один `example_bad` и один `example_good`. Можно сделать примеры богаче, добавив **больше контекста в `text`** или добавив ещё один сценарий.

Например, для `DIRECT_SENSITIVE` добавить в `text`:
```
Дополнительные признаки: SELECT с полями credit_amount, credit_contract_number, uid_credit
из credit_contract без маскирования. Поля is_blanc_credit, exp_limit_credit_rub из scp_project_ans.
Поля credit_history_comm из scp_sec_check_res.
```

---

## Шкала risk_score

| Диапазон | Смысл | Примеры |
|----------|-------|---------|
| 9–10 | Критический: полная компрометация или потеря данных | SQL injection, удаление без WHERE |
| 7–8 | Высокий: серьёзная уязвимость | privilege escalation, time-based blind |
| 5–6 | Средний: раскрытие чувствительных данных | SELECT * из таблиц с sensitive полями |
| 3–4 | Низкий: производительность / minor security | SELECT без LIMIT, обход индексов |
| 1–2 | Информационный: предупреждение о стиле | |

**Порог одобрения аудитором:** `overall_risk_score < 4.0` (из `SecurityAuditor.RISK_THRESHOLD`). Если итоговый риск ≥ 4.0 — запрос отклоняется.

---

## Как тестировать свои изменения

```python
# test_my_vuln.py
from rag_pipeline.rag_tools import get_security_context

# SQL с уязвимостью, которую ты добавил
test_cases = [
    "SELECT table_name FROM information_schema.tables",
    "DELETE FROM scp_application",
    "SELECT * FROM sys_employee WHERE id = 1 UNION SELECT inn, attr_email FROM sys_company",
]

for sql in test_cases:
    print(f"\n--- SQL: {sql[:60]} ---")
    ctx = get_security_context(sql, top_k=3)
    print(ctx[:800])
    print("...")
```

---

## Как проверить валидность JSON перед пушем

```bash
# Проверка синтаксиса JSON
python -m json.tool rag_pipeline/knowledge_base/security/vuln_classes.json > /dev/null

# Если OK — вывод пустой, exit code 0
# Если ошибка — покажет строку и позицию проблемы

# Проверить количество классов
python -c "
import json
data = json.load(open('rag_pipeline/knowledge_base/security/vuln_classes.json'))
print(f'Классов уязвимостей: {len(data)}')
for v in data:
    print(f'  {v[\"vuln_class\"]:25s} risk={v[\"risk_score\"]}')
"
```

---

## Рабочий процесс (checklist)

- [ ] Изучил список существующих 9 классов — не дублирую
- [ ] Добавил новый класс / расширил существующий в `vuln_classes.json`
- [ ] Убедился, что `text` содержит: название, риск X/10, признаки, последствия, защиту
- [ ] В `example_bad`/`example_good` используются только таблицы из GreenData
- [ ] Нет незащищённых двойных кавычек внутри JSON-строк
- [ ] Проверил JSON: `python -m json.tool rag_pipeline/knowledge_base/security/vuln_classes.json`
- [ ] Пересобрал индекс: `python rag_pipeline/build_indices.py`
- [ ] Убедился, что новая уязвимость находится по тестовому SQL

---

## Типичные ошибки

### 1. Сломанный JSON (самая частая проблема!)

```json
// НЕПРАВИЛЬНО — незакрытая строка
"example_bad": "SELECT * FROM t WHERE name = "admin";"

// НЕПРАВИЛЬНО — trailing comma после последнего элемента
{
  "recommendation": "...",   ← запятая здесь ломает JSON
}

// ПРАВИЛЬНО
{
  "recommendation": "..."    ← без запятой у последнего поля
}
```

### 2. `vuln_class` не совпадает с ключами из `SecurityAuditor.VULN_CLASSES`

Если добавляешь **новый** класс (не из исходных 9), он не будет автоматически распознан аудитором как "официальный". Но в RAG-контексте он всё равно будет полезен для LLM-судьи — он получит описание уязвимости и будет учитывать её при оценке. Новый ключ надо также добавить в `VULN_CLASSES` в `baseline1.py` (согласовать с командой).

### 3. Слишком короткий `text`

`text` используется для создания embedding-вектора. Чем он информативнее — тем точнее поиск. Минимум 3-4 предложения с конкретикой.
