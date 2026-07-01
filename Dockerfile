FROM python:3.12-slim

# Шрифты для кириллицы/эмодзи в графиках + tzdata для часового пояса (TZ)
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot

# БД и кэш курсов монтируются как volume в /data.
ENV DB_PATH=/data/finance.db \
    TZ=Europe/Prague \
    PYTHONUNBUFFERED=1

CMD ["python", "-m", "bot.main"]
