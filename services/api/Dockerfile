FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CENTAURAI_POCKET_HOST=0.0.0.0 \
    CENTAURAI_POCKET_PORT=8718 \
    CENTAURAI_POCKET_DATA_DIR=/data

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && groupadd --system --gid 10001 pocket \
    && useradd --system --uid 10001 --gid pocket --home-dir /nonexistent pocket \
    && mkdir -p /data /imports \
    && chown -R pocket:pocket /data /app

USER pocket

EXPOSE 8718

VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8718/api/v1/health', timeout=2)"]

CMD ["centaur-pocket-api"]
