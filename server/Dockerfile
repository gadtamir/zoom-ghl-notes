FROM python:3.12-slim

# Install ffmpeg (needed for media conversion + pydub chunking)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates supervisor \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app ./app
COPY supervisord.conf /etc/supervisor/conf.d/zoom-ghl.conf
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

# Render injects PORT. Default for local docker run.
ENV PORT=8000
EXPOSE 8000

# entrypoint runs DB init then hands off to supervisord (uvicorn + celery)
CMD ["./entrypoint.sh"]
