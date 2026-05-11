"""
Парсер data_model.sql → schema.json

Извлекает из PostgreSQL DDL:
- таблицы: имя, комментарий, колонки, PK, FK
- для каждой колонки: тип, nullable, комментарий, флаг sensitive
- сводку sensitive-полей по всей схеме (нужна SecurityAuditor)
- текстовое описание каждой таблицы (нужна RAG-индексации)

Запуск:
    python rag_pipeline/schema_parser.py
    → schema.json в корне проекта
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path


# Паттерны имён колонок, которые считаются чувствительными (для DIRECT_SENSITIVE)
SENSITIVE_NAME_PATTERNS = [
    "password", "passwd", "pwd", "secret", "token",
    "api_key", "hash", "salt", "ssn", "card_number",
    "credit", "pin", "cvv", "passport", "inn", "snils",
    "private", "personal", "email", "phone",
]


def _is_sensitive(col_name: str) -> bool:
    col_lower = col_name.lower()
    return any(p in col_lower for p in SENSITIVE_NAME_PATTERNS)


def _clean_comment(raw: str) -> str:
    """
    Убирает:
    - экранирование одинарных кавычек ('')
    - внутренние метаданные GreenData: SysTypeAttrEffective{...}, SysObjTypeEffective{...}
    """
    text = raw.replace("''", "'").strip()
    # Обрезаем всё начиная с мусорного суффикса GreenData
    text = re.sub(r",?\s*Sys(?:Type|Obj)(?:Attr|Type)?Effective\{.*", "", text, flags=re.DOTALL)
    text = re.sub(r",?\s*Abstract\w+Effective\{.*", "", text, flags=re.DOTALL)
    return text.strip().strip(",")


def _parse_column_line(line: str) -> tuple[str, str, bool] | None:
    """
    Парсит одну строку определения колонки внутри CREATE TABLE.
    Возвращает (col_name, col_type, not_null) или None если строка — не колонка.
    """
    line = line.strip().rstrip(",")
    if not line or line.startswith("--"):
        return None

    skip_keywords = ("CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN", ")")
    if any(line.upper().startswith(kw) for kw in skip_keywords):
        return None

    not_null = bool(re.search(r"\bNOT NULL\b", line, re.IGNORECASE))

    # Убираем NOT NULL и DEFAULT ... чтобы вычленить чистый тип
    clean = re.sub(r"\s+NOT NULL\b", "", line, flags=re.IGNORECASE)
    clean = re.sub(r"\s+DEFAULT\s+\S+", "", clean, flags=re.IGNORECASE)
    clean = clean.strip()

    parts = clean.split(None, 1)
    if len(parts) < 2:
        return None

    col_name = parts[0]
    col_type = parts[1].strip()

    # Исключаем служебные слова, которые могут попасть первым токеном
    if col_name.upper() in ("ALTER", "ADD", "CREATE", "DROP", "GRANT"):
        return None

    return col_name, col_type, not_null


def _build_text_description(table_name: str, table_data: dict) -> str:
    """
    Создаёт человекочитаемое описание таблицы для RAG-индексации.
    Каждая таблица → один документ в index_generation.
    """
    lines = [f"Таблица: {table_name}"]
    if table_data["comment"]:
        lines.append(f"Описание: {table_data['comment']}")
    if table_data["primary_key"]:
        lines.append(f"Первичный ключ: {', '.join(table_data['primary_key'])}")
    if table_data["foreign_keys"]:
        fk_lines = [
            f"  - {'.'.join(fk['columns'])} → {fk['references_table']}.{'.'.join(fk['references_columns'])}"
            for fk in table_data["foreign_keys"]
        ]
        lines.append("Внешние ключи:\n" + "\n".join(fk_lines))
    lines.append("Колонки:")
    for col_name, col in table_data["columns"].items():
        nullable = "" if col["nullable"] else " NOT NULL"
        comment = f"  — {col['comment']}" if col["comment"] else ""
        sensitive = " [SENSITIVE]" if col["is_sensitive"] else ""
        lines.append(f"  {col_name}: {col['type']}{nullable}{sensitive}{comment}")
    return "\n".join(lines)


def parse_schema(sql_path: str | Path) -> dict:
    sql = Path(sql_path).read_text(encoding="utf-8")
    schema: dict[str, dict] = {}

    # ── 1. CREATE TABLE ──────────────────────────────────────────────────────
    table_re = re.compile(
        r"CREATE TABLE public\.(\w+)\s*\((.*?)\);",
        re.DOTALL,
    )
    for m in table_re.finditer(sql):
        table_name = m.group(1)
        columns: dict[str, dict] = {}
        for raw_line in m.group(2).splitlines():
            parsed = _parse_column_line(raw_line)
            if parsed is None:
                continue
            col_name, col_type, not_null = parsed
            columns[col_name] = {
                "type": col_type,
                "nullable": not not_null,
                "comment": "",
                "is_sensitive": _is_sensitive(col_name),
            }
        schema[table_name] = {
            "comment": "",
            "columns": columns,
            "primary_key": [],
            "foreign_keys": [],
            "text_description": "",  # заполним в конце
        }

    # ── 2. COMMENT ON TABLE ──────────────────────────────────────────────────
    tbl_comment_re = re.compile(
        r"COMMENT ON TABLE public\.(\w+)\s+IS\s+'(.*?)';",
        re.DOTALL,
    )
    for m in tbl_comment_re.finditer(sql):
        tname = m.group(1)
        if tname in schema:
            schema[tname]["comment"] = _clean_comment(m.group(2))

    # ── 3. COMMENT ON COLUMN ─────────────────────────────────────────────────
    col_comment_re = re.compile(
        r"COMMENT ON COLUMN public\.(\w+)\.(\w+)\s+IS\s+'(.*?)';",
        re.DOTALL,
    )
    for m in col_comment_re.finditer(sql):
        tname, cname = m.group(1), m.group(2)
        if tname in schema and cname in schema[tname]["columns"]:
            schema[tname]["columns"][cname]["comment"] = _clean_comment(m.group(3))

    # ── 4. PRIMARY KEY ───────────────────────────────────────────────────────
    pk_re = re.compile(
        r"ALTER TABLE ONLY public\.(\w+)\s+ADD CONSTRAINT \w+ PRIMARY KEY \(([\w,\s]+)\);",
        re.IGNORECASE,
    )
    for m in pk_re.finditer(sql):
        tname = m.group(1)
        if tname in schema:
            schema[tname]["primary_key"] = [c.strip() for c in m.group(2).split(",")]

    # ── 5. FOREIGN KEY (только активные — не закомментированные) ────────────
    fk_re = re.compile(
        r"^ALTER TABLE ONLY public\.(\w+)\s+ADD CONSTRAINT (\w+) FOREIGN KEY \(([\w,\s]+)\)"
        r"\s+REFERENCES public\.(\w+)\(([\w,\s]+)\);",
        re.IGNORECASE | re.MULTILINE,
    )
    for m in fk_re.finditer(sql):
        tname = m.group(1)
        if tname in schema:
            schema[tname]["foreign_keys"].append(
                {
                    "columns": [c.strip() for c in m.group(3).split(",")],
                    "references_table": m.group(4),
                    "references_columns": [c.strip() for c in m.group(5).split(",")],
                }
            )

    # ── 6. Текстовые описания для RAG ────────────────────────────────────────
    for tname, tdata in schema.items():
        tdata["text_description"] = _build_text_description(tname, tdata)

    return schema


def build_output(schema: dict, sql_path: str | Path) -> dict:
    """Собирает финальный JSON с метаданными и сводкой sensitive-полей."""
    total_columns = sum(len(t["columns"]) for t in schema.values())

    sensitive_map: dict[str, list[str]] = {}
    for tname, tdata in schema.items():
        sens_cols = [c for c, v in tdata["columns"].items() if v["is_sensitive"]]
        if sens_cols:
            sensitive_map[tname] = sens_cols

    return {
        "metadata": {
            "source_file": str(sql_path),
            "parsed_at": datetime.now().isoformat(timespec="seconds"),
            "total_tables": len(schema),
            "total_columns": total_columns,
            "dialect": "PostgreSQL",
            "schema": "public",
        },
        "sensitive_fields_summary": sensitive_map,
        "tables": schema,
    }


def build_compact_output(schema: dict) -> dict:
    """
    Компактное представление схемы для инжекции в LLM-контекст.

    Формат для каждой таблицы:
        "table_name": {
            "desc": "...",
            "pk": ["id"],
            "cols": {"col": "type [NOT NULL] [SENSITIVE] — comment", ...},
            "fk":  ["col → other_table.col", ...]
        }

    Размер ~5–10x меньше полного schema.json.
    """
    compact: dict[str, dict] = {}
    for tname, tdata in schema.items():
        cols_compact: dict[str, str] = {}
        for cname, col in tdata["columns"].items():
            parts = [col["type"]]
            if not col["nullable"]:
                parts.append("NOT NULL")
            if col["is_sensitive"]:
                parts.append("[SENSITIVE]")
            if col["comment"]:
                parts.append(f"— {col['comment']}")
            cols_compact[cname] = " ".join(parts)

        fk_compact = [
            f"{'.'.join(fk['columns'])} → {fk['references_table']}.{'.'.join(fk['references_columns'])}"
            for fk in tdata["foreign_keys"]
        ]

        compact[tname] = {
            "desc": tdata["comment"],
            "pk": tdata["primary_key"],
            "cols": cols_compact,
        }
        if fk_compact:
            compact[tname]["fk"] = fk_compact

    return compact


def main() -> None:
    root = Path(__file__).parent.parent          # корень проекта
    sql_path = root / "data_model.sql"
    out_path = root / "schema.json"
    compact_path = root / "schema_compact.json"

    if not sql_path.exists():
        print(f"[ERROR] Файл не найден: {sql_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Парсим {sql_path} ...")
    schema = parse_schema(sql_path)

    # Полный schema.json
    output = build_output(schema, sql_path)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    # Компактный schema_compact.json для инжекции в LLM-контекст
    compact = build_compact_output(schema)
    compact_path.write_text(json.dumps(compact, ensure_ascii=False, indent=2), encoding="utf-8")

    meta = output["metadata"]
    sens = output["sensitive_fields_summary"]
    size_full = out_path.stat().st_size / 1024
    size_compact = compact_path.stat().st_size / 1024

    print(f"✓ schema.json         → {out_path} ({size_full:.0f} KB, ~{size_full*1024/4:.0f} токенов)")
    print(f"✓ schema_compact.json → {compact_path} ({size_compact:.0f} KB, ~{size_compact*1024/4:.0f} токенов)")
    print(f"  Таблиц:   {meta['total_tables']}")
    print(f"  Колонок:  {meta['total_columns']}")
    print(f"  Sensitive-поля в {len(sens)} таблицах:")
    for tname, cols in sens.items():
        print(f"    {tname}: {cols}")


if __name__ == "__main__":
    main()
