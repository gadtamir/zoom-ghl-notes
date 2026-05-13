# Zoom → GHL Notes — Server

חלק השרת של המערכת. מקבל קבצי הקלטות זום מה-Uploaders, מתמלל, מסכם, יוצר Note ב-GHL.

> **סטטוס:** שלבים 1-5 הושלמו. Pipeline מלא: `received → converted → transcribed → summarized → completed` (או `unmatched` / `failed`). מוכן ל-deploy ב-Render.

## דרישות מקדימות

- Python 3.12
- `ffmpeg` על ה-PATH (`brew install ffmpeg` / `apt install ffmpeg`)
- Redis לפיתוח מקומי (`brew install redis` / `apt install redis-server`)

## הפעלה מקומית

```bash
cd server
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # ערוך לפי הצורך — לפיתוח אפשר להשאיר את הברירות
python -m app.cli init-db
```

צריך 3 תהליכים רצים בו-זמנית (בטרמינלים נפרדים):

```bash
# 1. Redis (broker ל-Celery)
redis-server --daemonize yes

# 2. Celery worker
source .venv/bin/activate
celery -A app.tasks.celery_app:celery_app worker --loglevel=info --concurrency=1

# 3. FastAPI server
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000
```

בדיקה:
```bash
curl http://localhost:8000/health
```

## ניהול עובדות

יצירת עובדת חדשה והנפקת API key (מוצג רק פעם אחת):
```bash
python -m app.cli add-employee --name "שרה"
```

רשימת עובדות / jobs:
```bash
python -m app.cli list-employees
python -m app.cli list-jobs
python -m app.cli list-jobs --employee שרה --status failed
```

ניהול:
```bash
python -m app.cli deactivate --id <employee-id>
python -m app.cli rotate-key --id <employee-id>
```

## API

| Endpoint | Auth | תיאור |
|---|---|---|
| `GET /health` | - | בריאות |
| `POST /upload` | `X-API-Key` | קליטת קובץ אודיו/וידאו + מטה-דאטה, מחזיר `job_id` ומעלה למסלול ברקע |
| `GET /jobs` | `X-API-Key` | רשימת ה-jobs של אותה עובדת |
| `GET /jobs/{id}` | `X-API-Key` | פירוט job בודד (כולל תמלול וסיכום כשיהיו) |

### דוגמה — העלאת קובץ

```bash
curl -X POST http://localhost:8000/upload \
  -H "X-API-Key: zghl_..." \
  -F "file=@/path/to/recording.mp4" \
  -F "original_filename=audio_only.m4a" \
  -F "meeting_topic=2026-05-13 11.27.00 שיחה עם שרה לוי" \
  -F "meeting_date=2026-05-13"
```

תגובה (202 Accepted):
```json
{"job_id":"a17acfd2-...","status":"received","bytes":32233}
```

מעקב סטטוס: `GET /jobs/{job_id}`. השדה `status` עובר דרך:
`received` → `converted` → `transcribed` → `summarized` → `matched` → `completed`
(או `unmatched` / `failed` בהתאם).

## מבנה
```
server/app/
  config.py       Pydantic settings
  db.py           SQLAlchemy engine / session / Base
  models.py       Employee, Job
  auth.py         X-API-Key auth + key generation
  logging_config  JSON logger ל-stdout
  cli.py          Admin CLI (typer)
  api/upload.py   POST /upload  (streaming, מאמת ext, יוצר Job, מעלה לתור)
  api/jobs.py     GET /jobs, GET /jobs/{id}
  tasks/
    celery_app.py Celery instance עם Redis broker
    media.py      ffmpeg helpers (video→audio)
    pipeline.py   run_pipeline orchestrator
  services/       OpenAI / Anthropic / GHL clients (שלבים 3-5)
  main.py         FastAPI app
```

## אבטחה
- מפתחות API נשמרים כ-SHA-256 hash (256 ביט אנטרופיה — אין צורך ב-bcrypt).
- אסור לרשום את המפתח ב-DB כטקסט. ה-CLI מציג אותו פעם אחת.
- כל בקשה מאומתת לפי `X-API-Key` ומשויכת לעובדת — `/jobs` מחזיר רק job-ים של אותה עובדת.

## Deploy ל-Render

הקבצים הנדרשים כבר במקום:
- [render.yaml](../render.yaml) (ב-repo root) — Blueprint שמגדיר Web service + Postgres + Redis
- [Dockerfile](Dockerfile) — image מבוסס python:3.12-slim עם ffmpeg + supervisord
- [supervisord.conf](supervisord.conf) — מריץ uvicorn + celery worker באותו container (חולקים `/tmp`)
- [entrypoint.sh](entrypoint.sh) — מריץ `python -m app.cli init-db` ואז supervisord

### תהליך deploy
1. **Push ל-GitHub** (השלב הבא בפרויקט — ייעשה בנפרד).
2. ב-Render Dashboard:
   - `New +` → `Blueprint`
   - חבר את ה-repo
   - Render יזהה את `render.yaml` ויציע ליצור 3 services (Web + Postgres + Redis)
3. Render יבקש להזין את ה-**secrets** באופן ידני (`sync: false` ב-Blueprint):
   - `OPENAI_API_KEY`
   - `ANTHROPIC_API_KEY`
   - `GHL_PRIVATE_TOKEN`
   - `GHL_LOCATION_ID`
   - `RESEND_API_KEY` (אופציונלי, רק לאימייל)
4. לחץ Apply. Render בונה ומפעיל. Build הראשון לוקח ~5 דק'.
5. ה-Web service יקבל URL כמו `https://zoom-ghl-server.onrender.com`. בדיקה: `curl https://<your-url>/health`.

### ניהול אחרי deploy
- **לוגים**: ב-Render dashboard → Service → Logs (JSON structured logs).
- **יצירת עובדת חדשה**: Service → Shell → `python -m app.cli add-employee --name "אורנית"`. ה-CLI מחזיר API key חד-פעמי שצריך לתת לעובדת.
- **בדיקת jobs**: Shell → `python -m app.cli list-jobs --status failed --limit 20`.
- **rotate API key**: Shell → `python -m app.cli rotate-key --id <emp-id>`.

### עלויות צפויות
- Postgres Free: 0₪ (1GB)
- Redis Free: 0₪ (25MB)
- Web service Starter: ~$7/חודש (512MB RAM, sufficient לקצב הזה)
- **סה"כ infra**: ~$7/חודש = ~26 ש"ח
- שימוש ב-APIs (OpenAI + Anthropic + GHL): פר-שימוש. הערכה גסה לפגישה של שעה: ~$0.5 (תמלול $0.36 + סיכום $0.15).

### הערה לגבי ארכיטקטורה
ה-worker וה-web רצים באותו container (supervisord). הסיבה: ה-pipeline שומר קבצי אודיו זמניים ב-`/tmp`, וב-Render שני services לא יכולים לחלוק disk. לעומס נמוך (פגישות בודדות ביום) זה מצוין. אם בעתיד יהיה צורך לסקייל — אפשר לעבור ל-S3/R2 לאחסון, ולפצל את ה-services.
