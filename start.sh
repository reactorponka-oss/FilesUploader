#!/bin/bash
set -euo pipefail

: "${BOT_TOKEN:?BOT_TOKEN is required}"
: "${TELEGRAM_API_ID:?TELEGRAM_API_ID is required}"
: "${TELEGRAM_API_HASH:?TELEGRAM_API_HASH is required}"
: "${CHANNEL_USERNAME:?CHANNEL_USERNAME is required}"
: "${OWNER_ID:?OWNER_ID is required}"

mkdir -p "${DATA_DIR:-/app/data}/botapi" "${DATA_DIR:-/app/data}/jobs"

telegram-bot-api \
  --api-id="${TELEGRAM_API_ID}" \
  --api-hash="${TELEGRAM_API_HASH}" \
  --local \
  --http-port=8081 \
  --dir="${DATA_DIR:-/app/data}/botapi" &
API_PID=$!

cleanup() {
  kill "${API_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

python3 - <<'PY'
import socket, time, sys
for _ in range(120):
    try:
        with socket.create_connection(("127.0.0.1", 8081), timeout=1):
            print("Local Bot API is ready", flush=True)
            break
    except OSError:
        time.sleep(1)
else:
    print("Local Bot API did not start on port 8081", file=sys.stderr)
    sys.exit(1)
PY

python3 /app/main.py &
BOT_PID=$!

wait -n "${API_PID}" "${BOT_PID}"
exit_code=$?
kill "${API_PID}" "${BOT_PID}" 2>/dev/null || true
wait || true
exit "${exit_code}"
