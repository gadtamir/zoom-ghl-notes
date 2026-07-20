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

    # --- spec-builder (אפיון + פרומפט בוט) ---
    # Kill switch: set to false to disable spec/bot generation (auto + manual)
    # WITHOUT affecting the normal transcribe→summarize→note pipeline.
    spec_builder_enabled: bool = True
    # Google Drive (Oranit's account) for uploading bot prompts + spec PDFs.
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    gdrive_parent_folder_id: str = ""       # parent folder that holds per-client folders
    # Shared secret protecting the manual spec-builder web page (/spec/ui).
    spec_ui_token: str = ""

    admin_email: str = "gad@morethan.com"
    resend_api_key: str = ""
    alert_from_email: str = "alerts@morethan.com"   # must be a Resend-verified sender
    admin_sms_phone: str = "0548088154"             # GHL contact to SMS on alerts ("" = off)

    max_upload_mb: int = 500
    transcript_chunk_minutes: int = 10
    upload_dir: str = "/tmp/zoom-ghl-uploads"
    allowed_extensions: str = "m4a,mp3,mp4,m4v,mov,wav"

    celery_task_eager: bool = False
    celery_task_time_limit_sec: int = 60 * 60  # 1h hard limit per task
    ghl_call_poll_interval_seconds: int = 3 * 60 * 60  # 3 hours
    ghl_call_reconcile_interval_seconds: int = 60 * 60  # 1 hour — re-enqueue stuck calls


@lru_cache
def get_settings() -> Settings:
    return Settings()
