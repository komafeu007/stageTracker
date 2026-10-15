FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    SECRETS_DIR=/secrets

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.getenv('APP_PORT','8000')}/api/health\", timeout=4)" || exit 1

# Un seul worker : la synchro Gmail tourne dans un thread de fond de ce processus
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
