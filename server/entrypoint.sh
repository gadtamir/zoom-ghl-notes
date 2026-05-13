#!/bin/sh
# Render container entrypoint: idempotent schema init, then hand off to supervisord.
set -e

echo "[entrypoint] ensuring DB schema is up to date..."
python -m app.cli init-db

echo "[entrypoint] starting supervisord (uvicorn + celery worker)..."
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/zoom-ghl.conf
