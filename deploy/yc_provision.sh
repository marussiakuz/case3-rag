#!/usr/bin/env bash
# Одноразовый скрипт: создаёт VM в Yandex Cloud и выводит IP.
# Запускать локально с установленным yc CLI (https://cloud.yandex.ru/docs/cli/quickstart).
#
# Перед запуском:
#   yc init      # авторизация и выбор каталога
#
# Использование:
#   bash deploy/yc_provision.sh

set -euo pipefail

FOLDER_NAME="default"
VM_NAME="greendata-app"
ZONE="ru-central1-a"
PLATFORM="standard-v3"      # Intel Ice Lake
CORES=2
MEMORY=4                    # GB
DISK_SIZE=30                # GB
IMAGE_FAMILY="ubuntu-2204-lts"

# ── Каталог ───────────────────────────────────────────────────────────────────
FOLDER_ID=$(yc resource-manager folder get --name "${FOLDER_NAME}" --format json | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "Используем каталог '${FOLDER_NAME}' (id = ${FOLDER_ID})"

# ── SSH-ключ ──────────────────────────────────────────────────────────────────
SSH_KEY_PATH="${HOME}/.ssh/greendata_yc"
if [ ! -f "${SSH_KEY_PATH}" ]; then
    echo "Генерируем SSH-ключ ${SSH_KEY_PATH}..."
    ssh-keygen -t ed25519 -C "greendata-ci" -f "${SSH_KEY_PATH}" -N ""
fi
PUB_KEY=$(cat "${SSH_KEY_PATH}.pub")
echo "  Публичный ключ: ${SSH_KEY_PATH}.pub"

# ── VM ────────────────────────────────────────────────────────────────────────
echo "Создаём VM '${VM_NAME}'..."
yc compute instance create \
    --name "${VM_NAME}" \
    --folder-name "${FOLDER_NAME}" \
    --zone "${ZONE}" \
    --platform "${PLATFORM}" \
    --cores "${CORES}" \
    --memory "${MEMORY}GB" \
    --create-boot-disk "size=${DISK_SIZE}GB,type=network-ssd,image-family=${IMAGE_FAMILY},image-folder-id=standard-images" \
    --network-interface "subnet-name=default-${ZONE},nat-ip-version=ipv4" \
    --metadata "ssh-keys=ubuntu:${PUB_KEY}"

echo "  Ждём пока VM поднимется..."
sleep 30

VM_IP=$(yc compute instance get \
    --name "${VM_NAME}" \
    --folder-name "${FOLDER_NAME}" \
    --format json | python3 -c "
import sys, json
d = json.load(sys.stdin)
ifaces = d['network_interfaces']
print(ifaces[0]['primary_v4_address']['one_to_one_nat']['address'])
")

echo ""
echo "✅ VM создана!"
echo "   IP: ${VM_IP}"
echo "   SSH: ssh -i ${SSH_KEY_PATH} greendata@${VM_IP}"
echo ""
echo "── Следующие шаги ──────────────────────────────────────────────"
echo ""
echo "1. Первоначальная настройка VM (один раз):"
echo "   scp -i ${SSH_KEY_PATH} deploy/setup.sh deploy/greendata.service greendata@${VM_IP}:~/"
echo "   ssh -i ${SSH_KEY_PATH} greendata@${VM_IP} 'sudo bash ~/setup.sh'"
echo ""
echo "2. Добавь переменные в GitLab → Settings → CI/CD → Variables:"
echo "   YC_VM_HOST      = ${VM_IP}"
echo "   YC_VM_USER      = ubuntu"
echo "   YC_SSH_KEY      = $(cat ${SSH_KEY_PATH})   ← type: File, masked"
echo "   CEREBRAS_API_KEY = <твой ключ>   ← masked"
echo "   GROQ_API_KEY     = <твой ключ>   ← masked"
echo "   ANTHROPIC_API_KEY = <твой ключ>  ← masked"
echo "   PG_PASSWORD      = iamroot        ← masked"
echo ""
echo "3. Push в master — CI задеплоит приложение автоматически."
echo "   Приложение будет доступно: http://${VM_IP}"
