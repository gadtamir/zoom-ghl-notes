# Onboarding — Zoom→GHL Notes

> **For Claude:** if you're reading this in a fresh session, this doc gives you everything you need to continue work on the project without re-discovering it. Use it as ground truth for what exists today, then verify against the current code / live system before making changes.
>
> **For Ruth and Ornit:** open this in VS Code with Claude Code, and tell Claude what you want to do. Claude will read this first and understand the project.

---

## What this project does

Automation built for More-Than (Gad Tamir's CRM/automations company on GoHighLevel). Two pipelines:

1. **Zoom meeting summaries** — employees record client meetings in Zoom (local recording). A small desktop app (`uploader/`) watches the recordings folder and POSTs each new file to our server. The server transcribes (OpenAI), summarizes (Claude), matches a GHL contact, and creates a Hebrew Note on that contact.

2. **GHL phone-call summaries** — a Celery beat task polls GHL conversations every 3 hours, finds new `TYPE_CALL` messages, downloads the recording, transcribes/summarizes, and creates a Note on the contact.

Hebrew-first: client speech is Hebrew, summaries to GHL are Hebrew, all user-facing copy in the uploader is Hebrew.

## Architecture

```
┌─ Employees' Macs / Windows ─────────────┐
│   ZoomGHL.app (system tray)             │
│   Scans recordings folder every 30 min  │
│   POSTs new files with X-API-Key        │
└──────────────┬──────────────────────────┘
               │ HTTPS
               ▼
┌─ Render.com (Frankfurt) ────────────────────────────┐
│  zoom-ghl-server  (Docker, web + celery + beat)      │
│    uvicorn   → /upload, /jobs, /health               │
│    celery    → pipeline.run (Zoom), phone_calls.*    │
│    beat      → schedules poll-ghl-calls every 3h     │
└──────┬──────────┬──────────┬─────────────────────────┘
       │          │          │
       ▼          ▼          ▼
  Postgres     Redis      External APIs
  (jobs DB)   (broker)    OpenAI, Anthropic, GHL
```

Web + worker + beat all share one container (via `supervisord`) — they need `/tmp` together for transient files.

## Production state (as of 2026-05-19)

| Item | Value |
|---|---|
| Live URL | `https://zoom-ghl-server.onrender.com` |
| Service ID | `srv-d8330otckfvc73es0nr0` (Frankfurt, Starter $7/mo) |
| Postgres | `dpg-d8330d5ckfvc73es0cdg-a` (free, 90-day expiration risk) |
| Redis | `red-d8330d5ckfvc73es0ca0` (free) |
| Auto-deploy | push to `main` → Render rebuilds (~3 min) |
| GHL Location ID | `kkQWunWJWgtVKqXuEKxm` (More-Than) |

## Repo layout

```
server/
  app/
    api/         — FastAPI routers
      upload.py    POST /upload (Zoom uploader)
      jobs.py      GET /jobs, /jobs/{id}
    tasks/       — Celery tasks
      celery_app.py     Celery config + beat schedule
      pipeline.py       Zoom recording pipeline
      phone_calls.py    GHL call discovery + processing
      transcribe.py     ffmpeg-segment chunking + OpenAI
      summarize.py      Claude opus-4-5
      ghl.py            Contact matching + note creation
      media.py          ffmpeg wrappers
    services/
      ghl_client.py     GHL HTTP client (contacts, calls, calendar, notes)
      openai_client.py
      anthropic_client.py
    models.py    SQLAlchemy: Employee, Job, CallJob
    auth.py      X-API-Key auth (SHA-256 hashed keys)
    cli.py       Admin CLI (typer): add-employee, list-jobs,
                 retry-match, process-call, etc.
  Dockerfile, supervisord.conf, entrypoint.sh, requirements.txt
  render.yaml         Blueprint

uploader/             Desktop app (PyInstaller for macOS + Windows)
  src/
    tray_app.py       pystray system tray
    settings_window.py Tk config UI
    watcher.py        Scans Zoom folder for new recordings
    uploader.py       POSTs to server
    db.py             SQLite tracking what was uploaded
    paths.py          Cross-platform config/log paths
  build_macos.sh, build_windows.bat

docs/
  ADMIN.md            Operations runbook (in Hebrew)
  EMPLOYEE_README.md  Install + use guide for employees
  TEST_PLAN.md        E2E test plan
```

## Key conventions / gotchas

1. **Audio chunking must use `ffmpeg -f segment`** — never pydub. pydub loads the whole file into RAM (27 MB m4a → 300+ MB PCM) and OOM-kills the worker on the 512 MB Starter container. See [transcribe.py:32](server/app/tasks/transcribe.py#L32).

2. **Celery concurrency = 1, max-tasks-per-child = 20** — anything higher OOM'd. See [supervisord.conf](server/supervisord.conf).

3. **Hebrew folder names in Zoom topics** — format is `<date> <client>  +  <meeting type> _ <employee> - <company>`. Server splits on `+`/`_` (but NOT `-`, to preserve names like "More-Than") and tries each segment as a contact search. See [ghl.py:_split_topic_into_candidates](server/app/tasks/ghl.py).

4. **Contact-name source priority**: folder name segments first; transcript-extracted name is last-resort fallback only.

5. **GHL phone-call duration bug**: GHL sometimes returns `meta.call.duration=null` and `recordingUrl=false` even when the recording is downloadable. Don't skip on `duration=None` — let `download_call_recording` decide via 422. Fixed in commit `342ff32`.

6. **Push to `main` triggers an auto-deploy**. There's no staging environment. Be deliberate.

## Admin CLI (run via Render Shell)

```bash
python -m app.cli add-employee --name "<name>"        # create employee + API key
python -m app.cli list-employees
python -m app.cli rotate-key --id <emp-id>
python -m app.cli list-jobs [--employee X] [--status Y]
python -m app.cli retry-match --id <job-id>           # re-run only GHL match on a summarized job
python -m app.cli process-call --message-id <ghl-msg> # one-shot manual call processing
```

## Diagnosing failures

- **Render logs** (web service → "Logs"): all Python logging goes to stdout, structured JSON. Search by `job_id` / `call_job_id` to follow a single item's stages.
- **`/jobs/{id}`** (with employee's API key): returns status, transcript, summary, error_message.
- **Render Shell** (web service → "Shell"): run any CLI command directly against prod DB.
- **DB via Render API**: do not pull connection creds into chat — use Shell instead.

## Active threads / what's in flight

- **Ruth's uploader install (paused)** — Ruth installed ZoomGHL on her Mac but no `/upload` request reached the server. Waiting for her local `uploader.log` (tray → "פתח לוג") to diagnose.
- **Phone-call duration fix (just deployed `342ff32`)** — two specific calls today need manual re-processing:
  ```bash
  python -m app.cli process-call --message-id w8kJSEHhDXV5wE53shwz  # contact אור
  python -m app.cli process-call --message-id 1EKuwOSNfmS5KHrQySPX  # contact יהושע
  ```
- **GHL Calendar lookup (planned, not started)** — additional contact-match signal using employee's GHL user_id + meeting timestamp.

## Style preferences (from prior conversations)

- Respond in Hebrew to Gad and the team; keep code, commits, log messages in English.
- For multi-stage work, pause + report after each stage.
- Use AskUserQuestion checkboxes for architectural decisions.
- Verify against current code before recommending — memory ≠ truth.
