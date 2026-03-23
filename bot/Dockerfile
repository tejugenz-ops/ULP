FROM python:3.12-slim

# Install system dependencies for fast file processing + gcc for tgcrypto
RUN apt-get update && apt-get install -y --no-install-recommends \
    ripgrep \
    aria2 \
    p7zip-full \
    unrar-free \
    curl \
    gcc \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/

# Create data directory (Railway volume mounts here)
RUN mkdir -p /data/processing

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 8080

CMD ["python", "-m", "bot.main"]
