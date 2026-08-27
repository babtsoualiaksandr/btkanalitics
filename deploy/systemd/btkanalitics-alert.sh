#!/bin/bash
# Отправляет алерт в Telegram при падении systemd-сервиса (см. OnFailure=
# в btkanalitics-web/-celery-worker/-celery-beat.service).
#
# Сознательно НЕ зависит от Django/Celery/venv — чистый bash + curl,
# чтобы алерт срабатывал даже если сам Python-процесс приложения
# полностью упал и не может отправить сообщение через свой собственный
# telegram_client.py.
set -euo pipefail

ENV_FILE="/home/sshbeltelecom/va/btkanalitics/.env"
UNIT_NAME="${1:-unknown}"

if [ ! -f "$ENV_FILE" ]; then
    logger -t btkanalitics-alert "env file not found: $ENV_FILE"
    exit 0
fi

# Не делаем `source .env` целиком — там встречаются значения с пробелами/
# спецсимволами без кавычек (валидно для python-decouple, невалидно для bash).
# Достаём только два нужных значения.
BOT_TOKEN=$(grep -m1 '^TLG_BOT_TOKEN=' "$ENV_FILE" | cut -d'=' -f2-)
ADMIN_IDS=$(grep -m1 '^TLG_CHAT_ID_ADMINS=' "$ENV_FILE" | cut -d'=' -f2-)

if [ -z "$BOT_TOKEN" ] || [ -z "$ADMIN_IDS" ]; then
    logger -t btkanalitics-alert "TLG_BOT_TOKEN/TLG_CHAT_ID_ADMINS не найдены в $ENV_FILE"
    exit 0
fi

TEXT="⚠️ btkanalitics: сервис ${UNIT_NAME} упал ($(date '+%Y-%m-%d %H:%M:%S %Z'))"

IFS=',' read -ra IDS <<< "$ADMIN_IDS"
for chat_id in "${IDS[@]}"; do
    chat_id="$(echo -n "$chat_id" | tr -d '[:space:]')"
    [ -z "$chat_id" ] && continue
    curl -s -m 10 -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        -d "chat_id=${chat_id}" \
        --data-urlencode "text=${TEXT}" \
        > /dev/null || logger -t btkanalitics-alert "не удалось отправить алерт для chat_id=${chat_id}"
done
