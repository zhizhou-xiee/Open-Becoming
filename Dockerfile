FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    DB_PATH=/data/becoming.db \
    BECOMING_MEMORY_DIR=/data/memory \
    UPLOAD_ROOT=/data/uploads \
    MUSIC_LIBRARY_DIR=/data/music_library

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system becoming \
    && useradd --system --gid becoming --home-dir /app becoming

COPY . .
RUN mkdir -p /data \
    && chown -R becoming:becoming /app /data \
    && chmod +x /app/docker-entrypoint.sh

# No VOLUME declaration — Railway mounts volumes externally with root ownership,
# which breaks the non-root user. The entrypoint fixes /data permissions at startup.
EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"]
