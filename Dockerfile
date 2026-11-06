
# Build official Telegram Local Bot API server
FROM debian:bookworm-slim AS telegram-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    cmake \
    g++ \
    make \
    zlib1g-dev \
    libssl-dev \
    gperf \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

RUN git clone --recursive https://github.com/tdlib/telegram-bot-api.git

WORKDIR /build/telegram-bot-api

RUN mkdir build && cd build && \
    cmake -DCMAKE_BUILD_TYPE=Release .. && \
    cmake --build . --target install -j2


# Runtime image
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1
ENV LOCAL_API=http://127.0.0.1:8081
ENV DATA_DIR=/app/data

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    p7zip-full \
    ca-certificates \
    libssl3 \
    zlib1g \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=telegram-builder /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

WORKDIR /app

COPY main.py /app/main.py

RUN pip install --no-cache-dir requests

RUN mkdir -p /app/data/botapi /app/data/work

EXPOSE 8081

CMD ["bash", "-c", "telegram-bot-api --api-id=\"$TELEGRAM_API_ID\" --api-hash=\"$TELEGRAM_API_HASH\" --local --http-port=8081 --dir=/app/data/botapi & API_PID=$!; for i in $(seq 1 90); do if python -c 'import os,urllib.request,json,sys; u=\"http://127.0.0.1:8081/bot\"+os.environ[\"BOT_TOKEN\"]+\"/getMe\"; d=json.load(urllib.request.urlopen(u,timeout=2)); sys.exit(0 if d.get(\"ok\") else 1)' 2>/dev/null; then break; fi; sleep 2; done; python /app/main.py"]
