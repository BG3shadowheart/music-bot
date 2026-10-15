FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STATE_FILE=/data/state.json

RUN apt-get update \
    && apt-get install -y --no-install-recommends libopus0 libsodium23 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY main.py .

RUN mkdir -p /data

CMD ["python", "main.py"]
