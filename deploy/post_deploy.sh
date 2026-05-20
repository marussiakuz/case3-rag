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
# CPU-only torch — вдвое меньше, чем CUDA-версия по умолчанию
.venv/bin/pip install -q torch --index-url https://download.pytorch.org/whl/cpu
.venv/bin/pip install -q -r requirements.txt

# Пересобираем RAG-индексы только если изменились knowledge_base файлы.
# CI передаёт переменную REBUILD_RAG=true когда нужна пересборка.
if [ "${REBUILD_RAG:-false}" = "true" ]; then
    echo "[post_deploy] Пересборка RAG-индексов..."
    .venv/bin/python rag_pipeline/build_indices.py
fi

# Не прерываем запущенную валидацию
VALIDATION_RUNNING=false
if [ -f "validation/progress.json" ]; then
    VALIDATION_RUNNING=$(.venv/bin/python -c \
        "import json; d=json.load(open('validation/progress.json')); print('true' if d.get('running') else 'false')" \
        2>/dev/null || echo "false")
fi

if [ "$VALIDATION_RUNNING" = "true" ]; then
    echo "[post_deploy] Валидация запущена — перезапуск сервиса отложен."
    echo "[post_deploy] Новый код уже на диске. Перезапустите вручную после завершения валидации:"
    echo "  sudo systemctl restart greendata"
else
    echo "[post_deploy] Перезапуск сервиса..."
    sudo systemctl restart greendata
    sudo systemctl status greendata --no-pager -l
fi

echo "[post_deploy] Готово."
