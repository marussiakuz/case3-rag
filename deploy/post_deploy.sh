#!/usr/bin/env bash
# Запускается на VM после каждого деплоя CI/CD.
# Устанавливает зависимости и пересобирает RAG-индексы если нужно.

set -euo pipefail
APP_DIR="/opt/greendata"
cd "${APP_DIR}"

echo "[post_deploy] Установка зависимостей..."
if [ ! -d ".venv" ]; then
    python3.11 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

# Пересобираем RAG-индексы только если изменились knowledge_base файлы.
# CI передаёт переменную REBUILD_RAG=true когда нужна пересборка.
if [ "${REBUILD_RAG:-false}" = "true" ]; then
    echo "[post_deploy] Пересборка RAG-индексов..."
    .venv/bin/python rag_pipeline/build_indices.py
fi

echo "[post_deploy] Перезапуск сервиса..."
systemctl restart greendata
systemctl status greendata --no-pager -l

echo "[post_deploy] Готово."
