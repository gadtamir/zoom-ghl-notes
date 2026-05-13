from celery import Celery

from ..config import get_settings
from ..logging_config import configure_logging


settings = get_settings()
configure_logging()

celery_app = Celery(
    "zoom_ghl",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.pipeline"],
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
)
