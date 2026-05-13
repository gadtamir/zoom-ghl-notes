# ADMIN — מדריך תפעולי לגד

המסמך הזה הוא ה-runbook לתחזוקת השירות. אם משהו לא עובד או שעובדת חדשה הצטרפה — תתחיל פה.

---

## משאבים חיצוניים

| שירות | קישור | למה |
|---|---|---|
| GitHub repo | https://github.com/gadtamir/zoom-ghl-notes | קוד מקור (private) |
| Render dashboard | https://dashboard.render.com | hosting + לוגים + shell + secrets |
| GHL | https://app.gohighlevel.com | האפליקציה שאליה זורם ה-output |
| OpenAI | https://platform.openai.com/usage | ניטור צריכת תמלול |
| Anthropic | https://console.anthropic.com/usage | ניטור צריכת סיכום |

---

## תהליכים נפוצים

### 1. הוספת עובדת חדשה

```bash
# ב-Render dashboard:
# zoom-ghl-server → Shell
python -m app.cli add-employee --name "אורנית"
```

ה-CLI יחזיר API key חד-פעמי (`zghl_…`). **שמור מיד** — לא יוצג שנית.

שלח לעובדת 2 דברים:
- קובץ ההתקנה (`.dmg` או `.exe`)
- ה-API key

הפנה אותה ל-[EMPLOYEE_README](EMPLOYEE_README.md).

### 2. רוטציית API key (אם דלף)

```bash
python -m app.cli list-employees           # לקבל את ה-id
python -m app.cli rotate-key --id <emp-id>
```

ה-CLI יחזיר key חדש. עדכן את העובדת — היא תפתח את ZoomGHL → הגדרות → תכניס את החדש.

### 3. השבתת עובדת (עזיבת חברה וכו')

```bash
python -m app.cli deactivate --id <emp-id>
```

ה-key הקודם מפסיק לעבוד מיידית. ה-jobs ההיסטוריים שלה נשארים במסד הנתונים.

### 4. רשימת jobs / סטטוס

```bash
python -m app.cli list-jobs                              # 30 האחרונים
python -m app.cli list-jobs --status failed --limit 50   # רק כשלונות
python -m app.cli list-jobs --employee "אורנית"          # לפי עובדת
```

### 5. צפייה בלוגים

ב-Render → zoom-ghl-server → **Logs**. מסונן כ-JSON, מציגים את שלבי ה-pipeline (`convert`, `transcribe`, `summarize`, `ghl`) עם `job_id` ו-`employee`. אפשר ל-`Cmd+F` חיפוש לפי `job_id` או `failed`.

### 6. בדיקת job ספציפי (full payload)

```bash
# Via API:
curl -H "X-API-Key: <ANY_EMPLOYEE_KEY_OR_ADMIN>" \
  https://zoom-ghl-server.onrender.com/jobs/<job-id>
```

מחזיר transcript + summary + extracted_contact_name + ghl_contact_id + status.

### 7. עדכון הקוד

```bash
# מקומי:
cd "..."
# ערוך, בדוק
git add -A
git commit -m "fix: ..."
git push
```

Render מוגדר עם `autoDeploy: true` — push ל-main → build חדש מתחיל אוטומטית. עוקב ב-Render → Events.

### 8. הוספת מפתחות API חדשים / רוטציה של סודיים

ב-Render → zoom-ghl-server → **Environment** → ערוך / הוסף. אחרי שמירה: **Manual Deploy** → **Deploy latest commit** (rebuild) או **Restart Service** (אותו image, env חדש).

---

## ארכיטקטורה — מה רץ איפה

```
┌─ עובדת (מק/וינדוס)
│  ZoomGHL.app (tray)  ── scan כל 30 דק' ──┐
└──────────────────────────────────────────┘
                                            │ HTTPS POST /upload
                                            ▼
┌─ Render.com (Frankfurt)
│  ┌───────────────────────────────────────────┐
│  │ zoom-ghl-server (Docker, web+worker בכלי) │
│  │   uvicorn  ← /upload, /jobs, /health      │
│  │   celery   ← pipeline.run                 │
│  └─────────┬───────────┬─────────────────────┘
│            │           │                      
│            │           ▼                      
│  ┌─────────▼─────┐  ┌──────────────┐         
│  │ Postgres free │  │ Redis free   │         
│  └───────────────┘  └──────────────┘
└─────────────────────────────────────
        │              │             │
        ▼              ▼             ▼
   OpenAI         Anthropic         GHL
   (transcribe)   (summarize)       (note)
```

`web+worker באותו container` — קריטי שיישאר כך, כי שניהם חולקים `/tmp` (איפה שהקובץ נשמר זמנית בין הקליטה ל-Celery task). אם תפצל לשני services — צריך S3/R2 לקבצים.

---

## טיפול בכשלים

### Job בסטטוס `failed`

1. בדוק `error_message` — מספר שורות שמסבירות מאיזה שלב הוא נפל.
2. שלבים נפוצים שנכשלים:
   - `convert: ffmpeg failed` → בעיה בקובץ (פגום, encoding לא נתמך). מחק ידנית.
   - `transcribe: APIError 429` → rate limit של OpenAI. ה-tenacity ניסה 3x. נסה בעוד שעה.
   - `summarize: APIError 529` → Anthropic overload. נסה שוב.
   - `ghl: 401` → ה-`GHL_PRIVATE_TOKEN` פג / נמחק. צור Private Integration חדש ב-GHL ועדכן ב-Render env.

### Job בסטטוס `unmatched`

הסיכום בוצע אבל לא נמצא איש קשר ב-GHL לשם שחולץ.
- בדוק `extracted_contact_name` ו-`meeting_topic` בפרטי ה-job.
- אם הסיכום עצמו טוב — צור איש קשר ב-GHL ידנית, וצרף ידנית את ה-note (אפשר להעתיק מ-`summary`).
- שיפור עתידי: CLI command `retry-ghl --id <job> --contact-id <ghl-id>` שיוצר את ה-note על איש קשר ספציפי.

### "השרת לא עונה" / Render בעיה

- בדוק https://status.render.com — לפעמים יש incident.
- ב-Render dashboard → השירות → לחץ **Restart service**.

---

## ניטור עלות

| API | תעריף משוער | פגישה של שעה |
|---|---|---|
| OpenAI `gpt-4o-transcribe` | ~$0.006/דקה | $0.36 |
| Anthropic `claude-opus-4-5` | ~$15/M input + $75/M output | $0.10-0.20 |
| Render Web Starter | $7/חודש | קבוע |
| Render Postgres Free | $0 | קבוע (1GB) |
| Render Redis Free | $0 | קבוע (25MB) |

**הערכה**: 50 פגישות שעה/חודש ≈ $7 (infra) + $25 (APIs) = $32 לחודש. ~120 ש"ח.

עקוב חודשי ב:
- https://platform.openai.com/usage
- https://console.anthropic.com/usage

---

## שיפורים עתידיים (אם יעלה הצורך)

- **דשבורד אדמין web** ב-`/admin` עם password (היה ב-spec המקורי, נשאר אופציונלי)
- **Retry job CLI** — `python -m app.cli retry-job --id <id>` שמכניס ל-Celery שוב
- **Auto-start חוצה-פלטפורמות** ב-uploader (LaunchAgent + Run reg key) — כרגע ידני
- **Slack/Email התראה** על failed jobs (Resend API מוכן כ-env var, צריך לחבר)
- **S3 קבצים זמניים** במקום /tmp — אם נצטרך לפצל web/worker לסקייל
- **Long-term storage** — ניקוי jobs ישנים מ-DB (TTL 6 חודשים?)

---

## פתרון תקלות מהיר

| תופעה | ראשון לבדוק |
|---|---|
| כל ה-jobs failed | OpenAI/Anthropic/GHL keys בסביבת Render — נסה curl ידני |
| Jobs נתקעים ב-`received` | Worker process לא רץ? בדוק Render logs ל-celery |
| Jobs נתקעים ב-`converted` | חסר ffmpeg ב-image? לא אמור לקרות, ה-Dockerfile מתקין |
| `Build failed` ב-Render | בדוק `requirements.txt` — אם הוספת חבילה שאין לה wheel ל-linux/glibc |
| העובדת לא רואה note | חפש את ה-`job_id` בלוגים → `extracted_contact_name` → חפש ב-GHL search ידני |
