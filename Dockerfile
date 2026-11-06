# Build Telegram's official Local Bot API server.
FROM debian:bookworm-slim AS api-builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    git cmake g++ make gperf zlib1g-dev libssl-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --depth 1 --recursive https://github.com/tdlib/telegram-bot-api.git
WORKDIR /src/telegram-bot-api
RUN cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build build --target install -j2

FROM debian:bookworm-slim
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv ffmpeg ca-certificates libssl3 zlib1g libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=api-builder /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api
WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt
ENV PATH="/opt/venv/bin:$PATH"
COPY main.py start.sh ./
RUN chmod +x /app/start.sh && mkdir -p /app/data
CMD ["/app/start.sh"]
