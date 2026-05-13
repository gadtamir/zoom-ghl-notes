from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"
    log_level: str = "INFO"

    database_url: str = "sqlite:///./local.db"
    redis_url: str = "redis://localhost:6379/0"

    openai_api_key: str = ""
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-5"

    ghl_private_token: str = ""
    ghl_location_id: str = ""
    ghl_api_base: str = "https://services.leadconnectorhq.com"
    ghl_api_version: str = "2021-07-28"

    admin_email: str = "gad@morethan.com"
    resend_api_key: str = ""

    max_upload_mb: int = 500
    transcript_chunk_minutes: int = 10
    upload_dir: str = "/tmp/zoom-ghl-uploads"
    allowed_extensions: str = "m4a,mp3,mp4,m4v,mov,wav"

    celery_task_eager: bool = False
    celery_task_time_limit_sec: int = 60 * 60  # 1h hard limit per task


@lru_cache
def get_settings() -> Settings:
    return Settings()
