FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/data /app/logs \
    && chown -R app:app /app

USER app

CMD ["python", "-m", "anki_telegram_bot.main"]
