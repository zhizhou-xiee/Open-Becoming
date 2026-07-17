"""Portable Gunicorn defaults for a single-instance Open-Becoming server."""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = max(1, int(os.environ.get("WEB_CONCURRENCY", "1")))
timeout = max(30, int(os.environ.get("GUNICORN_TIMEOUT", "300")))
accesslog = "-"
errorlog = "-"
preload_app = False
