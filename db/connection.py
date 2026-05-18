"""
Подключение к gd_app — служебной БД приложения (история запросов + RAG-эмбеддинги).
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg2
import psycopg2.pool
from dotenv import load_dotenv

load_dotenv()

_APP_DB_CONFIG = {
    "host":     os.getenv("PG_HOST",     "localhost"),
    "port":     int(os.getenv("PG_PORT", "5432")),
    "dbname":   "gd_app",
    "user":     os.getenv("PG_USER",     "postgres"),
    "password": os.getenv("PG_PASSWORD", "iamroot"),
}

_pool: psycopg2.pool.SimpleConnectionPool | None = None


def get_pool() -> psycopg2.pool.SimpleConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.SimpleConnectionPool(1, 5, **_APP_DB_CONFIG)
    return _pool


def get_conn():
    return get_pool().getconn()


def put_conn(conn) -> None:
    get_pool().putconn(conn)