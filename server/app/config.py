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

    # Zoom Server-to-Server OAuth (cloud recordings). Empty = Zoom polling disabled.
    zoom_account_id: str = ""
    zoom_client_id: str = ""
    zoom_client_secret: str = ""
    # Webhook Only app: signs every event with HMAC. Empty = /webhooks/zoom rejects all.
    zoom_webhook_secret_token: str = ""
    zoom_min_duration_minutes: int = 5  # skip meetings shorter than this
    # Zoom finishes the audio-only (M4A) track a while after `recording.completed`
    # fires, so an absent audio file usually means "not ready yet", not "never".
    # Defer (don't terminally skip) up to this many processing attempts — with the
    # 15-min poller, that's a few hours' grace for the M4A to appear before we give
    # up on a genuinely audio-less recording.
    zoom_audio_wait_max_attempts: int = 12
    # Participants on these domains are colleagues, never "the customer" — so a
    # teammate who also exists in GHL can't hijack the note. Comma-separated.
    zoom_internal_domains: str = "morethan.com"
    # The team signs in to Zoom with personal Gmail addresses, so the domain list
    # above can't recognise them — list those addresses explicitly. Comma-separated.
    zoom_internal_emails: str = (
        "gadtamir1301@gmail.com,saarlapid33@gmail.com,"
        "ruth.morethan@gmail.com,oranitmorethan@gmail.com"
    )
    # Cloud storage is capped, so processed recordings are deleted from Zoom — but
    # only after a grace window, so a call worth keeping (e.g. a closed deal) can
    # be saved first. Deletion goes to Zoom's trash, itself recoverable ~30 days.
    # 0 (or negative) disables auto-deletion entirely.
    zoom_recording_retention_days: int = 30
    zoom_cleanup_interval_seconds: int = 24 * 60 * 60  # sweep once a day

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
    # Token that guards the /admin endpoints (status + manual recovery). Empty
    # disables them entirely (503) so admin is never reachable unprotected.
    admin_api_token: str = ""

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
    zoom_poll_interval_seconds: int = 15 * 60           # 15 minutes — pull new cloud recordings
    zoom_poll_lookback_hours: int = 48                  # overlap window; dedup is by meeting uuid


@lru_cache
def get_settings() -> Settings:
    return Settings()
