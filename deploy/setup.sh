#!/usr/bin/env bash
# Первоначальная настройка VM для GreenData SQL Security System.
# Запускается один раз от root сразу после создания VM в Yandex Cloud.
#
# Что делает:
#   - Устанавливает Python 3.11, PostgreSQL 16, nginx
#   - Создаёт БД gd_app + схему
#   - Деплоит приложение в /opt/greendata
#   - Регистрирует systemd-сервис

set -euo pipefail

APP_DIR="/opt/greendata"
APP_USER="greendata"
PG_APP_DB="gd_app"
PG_DATA_DB="greendata"
PG_PASS="${PG_PASSWORD:-iamroot}"

echo "=== [1/7] Обновление пакетов ==="
apt-get update -q && apt-get upgrade -yq

echo "=== [2/7] Python 3.11, git, curl ==="
apt-get install -yq software-properties-common
add-apt-repository -y ppa:deadsnakes/ppa
apt-get install -yq python3.11 python3.11-venv python3.11-dev git curl build-essential libpq-dev

echo "=== [3/7] PostgreSQL 16 ==="
if ! dpkg -l postgresql-16 &>/dev/null; then
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | gpg --batch --no-tty --dearmor -o /usr/share/keyrings/postgresql.gpg
    echo "deb [signed-by=/usr/share/keyrings/postgresql.gpg] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list
    apt-get update -q
fi
DEBIAN_FRONTEND=noninteractive apt-get install -yq postgresql-16 nginx

# Настраиваем postgres
sudo -u postgres psql -c "ALTER USER postgres WITH PASSWORD '${PG_PASS}';"
sudo -u postgres createdb "${PG_APP_DB}" 2>/dev/null || true
sudo -u postgres createdb "${PG_DATA_DB}" 2>/dev/null || true

# Схема gd_app (RAG + история запросов)
sudo -u postgres psql -d "${PG_APP_DB}" <<'SQL'
CREATE TABLE IF NOT EXISTS rag_embeddings (
    id          BIGSERIAL PRIMARY KEY,
    index_name  TEXT        NOT NULL,
    text        TEXT        NOT NULL,
    metadata    JSONB       NOT NULL DEFAULT '{}',
    embedding   FLOAT4[]    NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rag_index_name ON rag_embeddings (index_name);

CREATE TABLE IF NOT EXISTS query_history (
    id              BIGSERIAL PRIMARY KEY,
    query           TEXT,
    sql             TEXT,
    gen_time        FLOAT,
    tokens_total    INTEGER,
    risk_score      FLOAT,
    approved        BOOLEAN,
    summary         TEXT,
    vulnerabilities JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
SQL

echo "  PostgreSQL настроен."

echo "=== [4/7] Системный пользователь и директория ==="
id -u "${APP_USER}" &>/dev/null || useradd -m -s /bin/bash "${APP_USER}"
mkdir -p "${APP_DIR}"
chown "${APP_USER}:${APP_USER}" "${APP_DIR}"

echo "=== [5/7] Клонирование репозитория ==="
# Репозиторий клонируется CI/CD, здесь только инициализация структуры
if [ ! -f "${APP_DIR}/requirements.txt" ]; then
    echo "  WARN: Репозиторий ещё не задеплоен. После первого CI-пуша запусти:"
    echo "    /opt/greendata/deploy/post_deploy.sh"
fi

echo "=== [6/7] Systemd сервис ==="
# Ищем greendata.service рядом со скриптом или в ~/
SERVICE_SRC="$(dirname "$0")/greendata.service"
[ -f "${SERVICE_SRC}" ] || SERVICE_SRC="${HOME}/greendata.service"
cp "${SERVICE_SRC}" /etc/systemd/system/greendata.service

# Env-файлы для сервиса
mkdir -p /etc/greendata
cat > /etc/greendata/env <<ENVEOF
PG_HOST=localhost
PG_PORT=5432
PG_USER=postgres
PG_PASSWORD=${PG_PASS}
ENVEOF
chmod 600 /etc/greendata/env

# Секреты заполнит CI при первом деплое
touch /etc/greendata/secrets
chmod 600 /etc/greendata/secrets

systemctl daemon-reload
systemctl enable greendata
# Не стартуем сервис — кода ещё нет, CI запустит после деплоя

echo "=== [7/7] nginx — проброс 80 → 8501 ==="
apt-get install -yq nginx
cat > /etc/nginx/sites-available/greendata <<'NGINX'
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass         http://127.0.0.1:8501;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host $host;
        proxy_read_timeout 86400;
    }
}
NGINX
ln -sf /etc/nginx/sites-available/greendata /etc/nginx/sites-enabled/greendata
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

# Разрешаем ubuntu управлять сервисом и запускать post_deploy без пароля
echo "ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl restart greendata, /bin/systemctl status greendata, /bin/systemctl start greendata, /usr/bin/tee /etc/greendata/secrets" \
    > /etc/sudoers.d/greendata-ci
chmod 440 /etc/sudoers.d/greendata-ci

# Разрешаем ubuntu писать в /opt/greendata
chown ubuntu:ubuntu "${APP_DIR}"

echo ""
echo "✅ VM настроена."
echo "   Следующий шаг — добавь в GitLab CI переменные (см. README) и запусти пайплайн."
