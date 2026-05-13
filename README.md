# Zoom → GHL Notes

אוטומציה שלוקחת הקלטות פגישות זום של עובדות, מתמללת, מסכמת בעברית, ומצרפת Note לאיש הקשר המתאים ב-GoHighLevel.

## מבנה הפרויקט

```
.
├── server/          ← Backend (FastAPI + Celery + ffmpeg + Whisper + Claude + GHL)
│   ├── app/         ← הקוד עצמו
│   ├── Dockerfile
│   ├── supervisord.conf
│   ├── entrypoint.sh
│   ├── requirements.txt
│   └── README.md    ← הוראות הרצה מקומית + deploy ל-Render
├── uploader/        ← (שלב הבא) אפליקציית desktop לעובדות — macOS + Windows
├── render.yaml      ← Render Blueprint לפריסה בלחיצה
└── README.md        ← הקובץ הזה
```

## מצב הפיתוח

| שלב | סטטוס |
|---|---|
| 1. Server skeleton + DB models + API key auth | ✅ |
| 2. Upload endpoint + Celery pipeline | ✅ |
| 3. Transcription (OpenAI gpt-4o-transcribe) | ✅ |
| 4. Summarization (Claude opus-4-5) | ✅ |
| 5. GHL contact matching + Note creation | ✅ |
| 6. Render deploy (Blueprint) | ✅ קבצים מוכנים, ממתין ל-push + deploy |
| 7. Uploader CLI | ⏳ |
| 8. Uploader GUI tray | ⏳ |
| 9. macOS .dmg + Windows .exe | ⏳ |
| 10. README לעובדות + test plan | ⏳ |

## Quickstart לפיתוח מקומי

ראה [server/README.md](server/README.md). תקצור:

```bash
brew install ffmpeg redis python@3.12
cd server
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # ערוך את המפתחות
python -m app.cli init-db
python -m app.cli add-employee --name "Test User"  # שמור את ה-API key שיוחזר
redis-server --daemonize yes
celery -A app.tasks.celery_app:celery_app worker --loglevel=info &
uvicorn app.main:app --port 8000
```

## Pipeline

```
[Uploader (desktop)] ──upload──→ [POST /upload]
                                      │
                                      ▼
                              [Celery task queue]
                                      │
                                      ▼
              received → converted (ffmpeg)
                       → transcribed (OpenAI gpt-4o-transcribe)
                       → summarized (Claude opus-4-5 — extracts contact name too)
                       → completed (GHL note created)
                              ↘ unmatched (no GHL contact found — summary kept in DB)
                              ↘ failed   (stage error, error_message stored)
```
