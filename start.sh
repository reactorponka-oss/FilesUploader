#!/bin/bash

set -e

: "${TELEGRAM_API_ID:?Missing TELEGRAM_API_ID}"
: "${TELEGRAM_API_HASH:?Missing TELEGRAM_API_HASH}"
: "${BOT_TOKEN:?Missing BOT_TOKEN}"
: "${OWNER_ID:?Missing OWNER_ID}"
: "${CHANNEL_ID:?Missing CHANNEL_ID}"

mkdir -p /app/data/botapi

echo "Starting Telegram Local Bot API..."

telegram-bot-api \
    --api-id="$TELEGRAM_API_ID" \
    --api-hash="$TELEGRAM_API_HASH" \
    --local \
    --http-port=8081 \
    --dir=/app/data/botapi \
    --temp-dir=/app/data/botapi/temp \
    &

API_PID=$!

echo "Waiting for Local Bot API..."

for i in $(seq 1 60); do

    if ! kill -0 "$API_PID" 2>/dev/null; then
        echo "Telegram API server stopped."
        exit 1
    fi

    if curl -s \
        http://127.0.0.1:8081/ \
        >/dev/null; then
        break
    fi

    sleep 2
done

kill -0 "$API_PID"

echo "Starting Python bot..."

python3 /app/main.py &
BOT_PID=$!

cleanup() {
    kill "$BOT_PID" "$API_PID" 2>/dev/null || true
    wait || true
}

trap cleanup TERM INT

wait "$BOT_PID"
