FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x entrypoint.sh

# Railway mounts the volume here and injects PORT at runtime.
ENV GRANTSIGHT_DATA=/data \
    GRANTSIGHT_CACHE=/data/cache \
    GRANTSIGHT_XML_DIR=/data/xml \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Shell form so $PORT expands. Railway injects PORT; 8000 is the local default.
ENTRYPOINT ["./entrypoint.sh"]
