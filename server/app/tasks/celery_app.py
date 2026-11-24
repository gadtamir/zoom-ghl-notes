from celery import Celery
from celery.schedules import crontab

from ..config import get_settings
from ..logging_config import configure_logging


settings = get_settings()
configure_logging()

celery_app = Celery(
    "zoom_ghl",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.pipeline", "app.tasks.phone_calls", "app.tasks.zoom_meetings", "app.tasks.closed_deals"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_always_eager=settings.celery_task_eager,
    task_eager_propagates=settings.celery_task_eager,
    task_time_limit=settings.celery_task_time_limit_sec,
    task_soft_time_limit=settings.celery_task_time_limit_sec - 60,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "poll-ghl-calls": {
            "task": "phone_calls.poll",
            "schedule": settings.ghl_call_poll_interval_seconds,
        },
        # Self-healing: re-enqueue calls that failed or got stuck mid-pipeline.
        # Runs between polls so a transient outage recovers without manual backfill.
        "reconcile-stuck-calls": {
            "task": "phone_calls.reconcile",
            "schedule": settings.ghl_call_reconcile_interval_seconds,
        },
        # Safety net: re-discover cloud recordings a dropped webhook would miss.
        "poll-zoom-recordings": {
            "task": "zoom_meetings.poll",
            "schedule": settings.zoom_poll_interval_seconds,
        },
        # Delete Zoom cloud recordings once past their retention window.
        "cleanup-zoom-recordings": {
            "task": "zoom_meetings.cleanup_recordings",
            "schedule": settings.zoom_cleanup_interval_seconds,
        },
        # Won/lost leads: build/refresh each lead's sales-transcript doc in Drive, daily.
        "check-closed-deals": {
            "task": "closed_deals.check",
            # Fixed time of day, not an interval: an interval timer restarts on
            # every deploy, so a daily one might never fire.
            "schedule": crontab(hour=settings.closed_deals_check_hour_utc, minute=7),
        },
    },
)
