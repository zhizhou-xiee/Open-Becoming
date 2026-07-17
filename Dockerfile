FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    DB_PATH=/data/becoming.db \
    BECOMING_MEMORY_DIR=/data/memory \
    UPLOAD_ROOT=/data/uploads

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system becoming \
    && useradd --system --gid becoming --home-dir /app becoming

COPY . .
RUN mkdir -p /data \
    && chown -R becoming:becoming /app /data

USER becoming
VOLUME ["/data"]
EXPOSE 8000

CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"]
