FROM python:3.12-slim

LABEL org.opencontainers.image.title="Fundus" \
      org.opencontainers.image.description="Selbst gehostete Recherche-Plattform" \
      org.opencontainers.image.source="https://github.com/AntoineBurtz/fundus"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONUNBUFFERED=1 \
    FUNDUS_DATA_DIR=/data \
    TZ=Europe/Berlin

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# Python-Pakete + Chromium für Screenshot/PDF (inkl. Schriften und Systembibliotheken)
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*
COPY app ./app

VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
