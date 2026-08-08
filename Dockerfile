FROM python:3.12-slim

# Tidak ada pip install: bot ini murni standard library.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/app/data/vps.db \
    MOCK_STATE_PATH=/app/data/mock_provider.json

WORKDIR /app

COPY bot.py ./
COPY vpsbot ./vpsbot

RUN mkdir -p /app/data
VOLUME ["/app/data"]

CMD ["python3", "bot.py"]
