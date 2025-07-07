FROM python:3.13

WORKDIR /app

COPY requirements.txt .

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove gcc

COPY . .

ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
