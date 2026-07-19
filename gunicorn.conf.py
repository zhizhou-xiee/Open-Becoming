"""Portable Gunicorn defaults for a single-instance Open-Becoming server."""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = max(1, int(os.environ.get("WEB_CONCURRENCY", "1")))
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "gthread")
threads = max(1, int(os.environ.get("GUNICORN_THREADS", "8")))
timeout = max(30, int(os.environ.get("GUNICORN_TIMEOUT", "300")))
accesslog = "-"
errorlog = "-"
preload_app = False
