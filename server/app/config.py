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
    # mini is half the price of gpt-4o-transcribe; switch back from the Render
    # dashboard (no deploy) if Hebrew quality isn't good enough.
    openai_transcribe_model: str = "gpt-4o-mini-transcribe"
    # Sales meetings (and the spec / bot prompt built from them) are worth the
    # better model; every meeting whose title contains one of these markers gets
    # `openai_quality_transcribe_model`. Comma-separated; "" = everything on mini.
    openai_quality_transcribe_model: str = "gpt-4o-transcribe"
    quality_transcribe_meeting_markers: str = "הדגמה,התאמה,אפיון,הטמעה ראשונה"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    # Phone-call summaries are short and formulaic — Haiku is ~20x cheaper than
    # Sonnet and good enough. Meetings and specs stay on `anthropic_model`.
    anthropic_call_model: str = "claude-haiku-5-5"

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
    zoom_min_duration_minutes: int = 10  # skip meetings shorter than this
    # Only meetings whose Zoom title names one of these types are transcribed.
    # Everything else — internal calls, ad-hoc "<name>'s Zoom Meeting", follow-up
    # sessions booked without a type — is left alone before any paid work happens,
    # which is where most of the transcription bill used to go. Comma-separated so
    # a new meeting type can be added from the Render dashboard without a deploy;
    # set to "" to transcribe every meeting (the old behaviour).
    zoom_transcribe_meeting_types: str = (
        "פגישת התאמה,פגישת הדגמה,פגישת סיכום והתחלת עבודה,"
        "פגישת הטמעה ראשונה,פגישת הטמעה שנייה,פגישת הטמעה שניה,"
        "פגישת הקמה,פגישת תמיכה טכנית,פגישת אפיון"
    )
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
    # How far back (in 30-day windows) the sweep looks for recordings to delete.
    zoom_cleanup_sweep_months: int = 12

    # --- spec-builder (אפיון + פרומפט בוט) ---
    # Kill switch: set to false to disable spec/bot generation (auto + manual)
    # WITHOUT affecting the normal transcribe→summarize→note pipeline.
    spec_builder_enabled: bool = True
    # Google Drive (Oranit's account) for uploading bot prompts + spec PDFs.
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    gdrive_parent_folder_id: str = ""       # parent folder that holds per-client folders
    # --- won / lost leads → Drive transcript doc ---
    # When an opportunity in pipeline "ליד ראשי" reaches "ליד נסגר" (won) or
    # "לא רלוונטי" (lost), we build one Google Doc per lead with the full transcripts
    # of its sales meetings, what the lead said they want solved, and the
    # opportunity's "why" field — in the won or the lost Drive folder.
    closed_deals_enabled: bool = True
    closed_deals_won_folder_id: str = "128vcH6e-M9SQD3gnvtHj9dmdwLi7AfrH"
    closed_deals_lost_folder_id: str = "1EQcXEGFWgSnYMedwyk1jogMjn48yXN8v"
    # Which meetings count as "sales meetings". Matched as substrings of the Zoom
    # title, like ZOOM_TRANSCRIBE_MEETING_TYPES. Comma-separated.
    closed_deals_meeting_types: str = "פגישת הדגמה,פגישת התאמה,פגישת סיכום והתחלת עבודה"
    # The check runs once a day (hour in UTC; 20 = 22:00/23:00 in Israel). A lead
    # moved within the last hour is left for the next run — its call may still be
    # transcribing. The doc stays open for the window so a reason field filled in
    # a day or two later still makes it in.
    closed_deals_check_hour_utc: int = 20
    closed_deals_initial_wait_minutes: int = 60
    closed_deals_window_hours: int = 72
    # The server finds these leads itself by polling GHL (no workflow needed):
    # opportunities in the pipeline/stage whose stage changed within the lookback.
    closed_deals_pipeline_name: str = "ליד ראשי"
    closed_deals_stage_name: str = "ליד נסגר"
    closed_deals_lost_stage_name: str = "לא רלוונטי"
    closed_deals_poll_lookback_hours: int = 72
    # Opportunity custom fields (by name) holding the reason.
    closed_deals_won_reason_field: str = "למה הליד נסגר"
    closed_deals_lost_reason_field: str = "למה הליד לא נסגר"
    # Model for the "what does the lead want solved" extraction. Empty = the cheap
    # call-summary model.
    closed_deals_pain_model: str = ""
    # Guards the GHL webhook (GHL workflows can't sign requests, so the token rides
    # in the URL). Falls back to ADMIN_API_TOKEN when empty.
    deal_closed_webhook_token: str = ""
    # Shared secret protecting the manual spec-builder web page (/spec/ui).
    spec_ui_token: str = ""
    # Token that guards the /admin endpoints (status + manual recovery). Empty
    # disables them entirely (503) so admin is never reachable unprotected.
    admin_api_token: str = ""

    admin_email: str = "gadtamir1301@gmail.com"
    resend_api_key: str = ""
    alert_from_email: str = "alerts@morethan.com"   # must be a Resend-verified sender
    admin_sms_phone: str = "0548088154"             # GHL contact to SMS on alerts ("" = off)

    max_upload_mb: int = 500
    transcript_chunk_minutes: int = 10
    # Transcription is billed per audio minute, so long silences (hold music
    # gaps, everyone muted) are cut before upload. Only gaps longer than
    # `silence_trim_min_seconds` below `silence_trim_threshold_db` are removed,
    # and half a second of each is kept so words never run together.
    silence_trim_enabled: bool = True
    silence_trim_min_seconds: float = 2.0
    silence_trim_threshold_db: int = -45
    upload_dir: str = "/tmp/zoom-ghl-uploads"
    allowed_extensions: str = "m4a,mp3,mp4,m4v,mov,wav"

    celery_task_eager: bool = False
    celery_task_time_limit_sec: int = 60 * 60  # 1h hard limit per task
    ghl_call_poll_interval_seconds: int = 3 * 60 * 60  # 3 hours
    ghl_call_reconcile_interval_seconds: int = 60 * 60  # 1 hour — re-enqueue stuck calls
    zoom_poll_interval_seconds: int = 15 * 60           # 15 minutes — pull new cloud recordings
    zoom_poll_lookback_hours: int = 48                  # overlap window; dedup is by meeting uuid
    zoom_max_attempts: int = 6                          # poller stops re-enqueueing a failed meeting after this many runs


@lru_cache
def get_settings() -> Settings:
    return Settings()
