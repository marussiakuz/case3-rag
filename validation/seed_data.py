"""
Заливает тестовые данные в PostgreSQL greendata (10 строк на таблицу).

Запуск:
    .venv/bin/python validation/seed_data.py
"""

from __future__ import annotations

import os
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent.parent

DB_CONFIG = {
    "host": os.getenv("PG_HOST", "localhost"),
    "port": int(os.getenv("PG_PORT", "5432")),
    "dbname": os.getenv("PG_DB", "greendata"),
    "user": os.getenv("PG_USER", "postgres"),
    "password": os.getenv("PG_PASSWORD", "iamroot"),
}

ROWS_PER_TABLE = 100
RU_NAMES = ["Иванов", "Петров", "Сидоров", "Козлов", "Новиков",
            "Морозов", "Попов", "Лебедев", "Соколов", "Волков"]
RU_FIRST = ["Александр", "Сергей", "Дмитрий", "Андрей", "Алексей",
            "Михаил", "Николай", "Иван", "Павел", "Артём"]
RU_SECOND = ["Александрович", "Сергеевич", "Дмитриевич", "Андреевич",
             "Алексеевич", "Михайлович", "Николаевич", "Иванович"]
COMPANIES = ["ООО ГринДата", "АО ПромБанк", "ПАО ФинансГрупп",
             "ЗАО ТехноКредит", "ООО ИнвестКапитал", "АО РусФинанс",
             "ПАО БанкСервис", "ООО КредитПлюс", "АО ДатаБанк", "ООО АльфаФин"]


def _rand_date(start_year: int = 2020, end_year: int = 2024) -> datetime:
    start = datetime(start_year, 1, 1)
    end = datetime(end_year, 12, 31)
    return start + timedelta(seconds=random.randint(0, int((end - start).total_seconds())))


def _fake_value(col_name: str, data_type: str, row_idx: int) -> object:
    """Генерирует значение для колонки на основе типа и имени."""
    col = col_name.lower()

    # PRIMARY KEY — всегда row_idx
    if col == "id":
        return row_idx

    # FK и bigint-ссылки — берём из диапазона 1..ROWS_PER_TABLE
    if data_type == "bigint":
        return random.randint(1, ROWS_PER_TABLE)

    if data_type == "smallint":
        if any(x in col for x in ("is_", "status", "sign_", "flag", "locked", "confirmed", "system")):
            return random.choice([0, 1])
        return random.randint(0, 5)

    if data_type == "numeric":
        return round(random.uniform(1000.0, 10_000_000.0), 2)

    if data_type in ("timestamp without time zone", "timestamp with time zone"):
        return _rand_date()

    if data_type == "character varying":
        if "name" in col and "ru" in col:
            return random.choice(RU_NAMES) + " " + random.choice(RU_FIRST)
        if "sur_name" in col or col == "name":
            return random.choice(RU_NAMES)
        if "first_name" in col:
            return random.choice(RU_FIRST)
        if "second_name" in col:
            return random.choice(RU_SECOND)
        if "email" in col:
            return f"user{row_idx}@greendata.ru"
        if "phone" in col:
            return f"+7 (9{random.randint(10,99)}) {random.randint(100,999)}-{random.randint(10,99)}-{random.randint(10,99)}"
        if "inn" in col:
            return str(random.randint(1000000000, 9999999999))
        if "company" in col or col in ("short_name",):
            return random.choice(COMPANIES)
        if "city" in col:
            return random.choice(["Москва", "Санкт-Петербург", "Казань", "Новосибирск"])
        if "street" in col or "addr" in col:
            return f"ул. Ленина, {random.randint(1, 100)}"
        return f"значение_{col}_{row_idx}"

    # text, jsonb и прочие
    return f"data_{row_idx}"


def get_tables(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """)
        return [r[0] for r in cur.fetchall()]


def get_columns(conn, table: str) -> list[tuple[str, str, str]]:
    """Возвращает (column_name, data_type, is_nullable) для таблицы."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
        """, (table,))
        return cur.fetchall()


def seed_table(conn, table: str, columns: list[tuple]) -> int:
    col_names = [c[0] for c in columns]
    placeholders = ", ".join(["%s"] * len(col_names))
    col_list = ", ".join(col_names)
    sql = f"INSERT INTO public.{table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"

    inserted = 0
    with conn.cursor() as cur:
        for i in range(1, ROWS_PER_TABLE + 1):
            values = [_fake_value(c[0], c[1], i) for c in columns]
            try:
                cur.execute(sql, values)
                inserted += 1
            except Exception:
                conn.rollback()
                break
        else:
            conn.commit()
    return inserted


def main() -> None:
    print(f"Подключение к {DB_CONFIG['dbname']}@{DB_CONFIG['host']}...")
    conn = psycopg2.connect(**DB_CONFIG)

    # Отключаем FK-триггеры на время вставки
    with conn.cursor() as cur:
        cur.execute("SET session_replication_role = replica;")
    conn.commit()

    tables = get_tables(conn)
    print(f"Таблиц: {len(tables)}, строк на таблицу: {ROWS_PER_TABLE}\n")

    errors = []
    for idx, table in enumerate(tables, 1):
        columns = get_columns(conn, table)
        inserted = seed_table(conn, table, columns)
        status = "✓" if inserted > 0 else "✗"
        print(f"[{idx:02d}/{len(tables)}] {table:<40} {status} {inserted} строк")
        if inserted == 0:
            errors.append(table)

    # Восстанавливаем FK-триггеры
    with conn.cursor() as cur:
        cur.execute("SET session_replication_role = DEFAULT;")
    conn.commit()
    conn.close()

    print(f"\n{'═' * 50}")
    total = len(tables) * ROWS_PER_TABLE
    print(f"✅ Готово. Ожидаемо строк: {total}, пропущено таблиц: {len(errors)}")
    if errors:
        print(f"   Ошибки: {', '.join(errors)}")


if __name__ == "__main__":
    main()