# CLAUDE.md — מדריך ל-Claude Code בפרויקט הזה

פרויקט שמתמלל ומסכם **פגישות זום** ו**שיחות טלפון** של More-Than, ומצמיד סיכום
כ-Note באיש הקשר ב-GoHighLevel (GHL). יש פייפליין אוטומטי שרץ בענן (Render), אבל
לפעמים שיחות "נופלות" ולא מתומללות — ואז משתמשים בסקריפטים שכאן כדי לתקן ידנית.

> **הפייפליין מרפא את עצמו (מאז 06/2026).** שיחת טלפון שנכשלת מקבלת ניסיונות חוזרים
> עם backoff, ומשימת `phone_calls.reconcile` (Celery beat, כל שעה) שולחת מחדש לעיבוד
> כל שיחה תקועה/`failed` עד 5 ניסיונות. כשנגמר קרדיט (429/`insufficient_quota`) נשלח
> מייל לאדמין דרך Resend. שיחה שעברה 5 ניסיונות עוברת לסטטוס סופי `abandoned` ונשלחת
> עליה התראה **פעם אחת בלבד** (מייל + SMS עם ה-`call_job_id`). היא לא תנוסה שוב לבד —
> אחרי שהסיבה תוקנה (למשל קרדיט נטען) מריצים אותה מחדש עם
> `POST /admin/calls/retry?id=<call_job_id>&token=<ADMIN_API_TOKEN>` (או
> `GET /admin/calls/failed` כדי לראות מה ננטש). לכן ה-backfill הידני שכאן הוא
> **גיבוי-אחרון**, לא הצעד הראשון — אם משהו נופל, בדרך כלל הוא יתוקן לבד תוך שעה. הקוד: [server/app/tasks/phone_calls.py](server/app/tasks/phone_calls.py), [server/app/services/notify.py](server/app/services/notify.py).

## 🆘 הבעיה הנפוצה: "שיחה/פגישה לא תומללה"

כשמבקשים ממך לבדוק למה משהו לא תומלל, או לתקן — זה ה-runbook:

### שלב 1 — לוודא שיש קרדיט (הסיבה הכי שכיחה)
התמלול צריך **גם** OpenAI (תמלול) **וגם** Anthropic (סיכום). אם לאחד נגמר הקרדיט,
שיחות נכשלות בשקט. תמיד תבדוק את שניהם קודם. אם אחד מחזיר 429/400 עם
"insufficient_quota" או "credit balance too low" → **צריך להטעין קרדיט** (פעולת
חיוב שהמנהל עושה, לא משהו שאתה יכול): OpenAI ב-platform.openai.com, Anthropic ב-console.anthropic.com.

### שלב 2 — לתקן שיחת טלפון (הכלי המרכזי: `server/scripts/backfill_calls.py`)
הרץ תמיד עם `caffeinate` (שהמחשב לא יירדם) ועם **Haiku** (זול):

```bash
cd server
# שיחה ספציפית לפי message-id:
caffeinate -dimsu ./.venv/bin/python scripts/backfill_calls.py \
    --message-id <ID> --model claude-haiku-4-5-20251001

# כל השיחות החסרות מתאריך מסוים:
caffeinate -dimsu ./.venv/bin/python scripts/backfill_calls.py \
    --since 2026-06-01 --workers 2 --model claude-haiku-4-5-20251001

# רק לבדוק מה חסר (בלי לתמלל, קריאה בלבד):
./.venv/bin/python scripts/backfill_calls.py --since 2026-06-01 --report-missing
```

למצוא איש קשר / שיחה לפי אימייל או טלפון: חפש ב-GHL Contacts API
(`GHL_API_BASE`/contacts/?query=...). הסקריפט מדלג אוטומטית על שיחות קצרות מדקה
ושיחות בלי הקלטה (לא נענו) — זה תקין, אין שם מה לתמלל.

### שלב 3 — פגישת זום (`server/scripts/backfill_meetings.py`)
רלוונטי בעיקר במחשב של גד (הקלטות הזום נמצאות שם). אותו רעיון: `--from`/`--to`/`--workers`.

## ⚠️ מלכודות סביבה (חשוב — אחרת דברים "נתקעים")
- **תמיד stdlib `urllib`, לא ה-SDK של openai/anthropic** — ה-SDK נתקע בייבוא במחשב הזה.
- ה-GHL מאחורי Cloudflare — צריך header של `User-Agent` דפדפני אחרת 403.
- ריצות ארוכות מתות כשהמחשב נרדם → תמיד `caffeinate -dimsu`.
- פלט/state/לוגים נכתבים ל-`~/zghl_backfill/` (מחוץ ל-git ול-iCloud). ניתן לשנות עם `ZGHL_WORK_DIR`.
- ה-`.env` (מפתחות סודיים) **לא** בריפו. צריך להניח אותו ב-`server/.env` — ראה `server/.env.example`.

## 🎥 איזה פגישות זום מתומללות (ולמה פגישה "לא נקלטה")
- מתומללות **רק** פגישות שהכותרת שלהן מכילה אחד מהסוגים ב-`ZOOM_TRANSCRIBE_MEETING_TYPES`
  (ב-Render, ברירת מחדל ב-[server/app/config.py](server/app/config.py)): פגישת התאמה, הדגמה,
  סיכום והתחלת עבודה, הטמעה ראשונה/שנייה, הקמה, תמיכה טכנית, אפיון.
  **פגישת תפעול לא ברשימה → מדולגת.** להוסיף סוג = לערוך את המשתנה ב-Render (בלי deploy).
- פגישה בלי לקוח שקיים ב-GHL (רק צוות) = "פנימית" ומדולגת, גם אם הסוג ברשימה.
- המערכת לא רואה פגישות עתידיות — זום מודיע רק אחרי שההקלטה מוכנה.
- לראות מה דולג: `GET https://zoom-ghl-server.onrender.com/admin/zoom/skipped?token=<ADMIN_API_TOKEN>`
  (גם `/admin/zoom/meetings` לכל הפגישות האחרונות + סטטוס).

## ☁️ סשן ענן (Claude Code מהאפליקציה/דפדפן) — מה צריך כדי לבדוק דברים
בסשן ענן **אין** `.env`, אין גישה ליומן (גוגל/GHL), ואין connector לזום. כדי שאוכל לבדוק בעצמי
צריך להגדיר בסביבת הענן (תפריט הסביבה בכותרת הסשן → Edit → Network secrets / API credentials),
ואז **לפתוח סשן חדש** (סשן קיים לא קולט):
- זום: `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET`
- שרת ה-admin: `ADMIN_API_TOKEN`
- GHL (אופציונלי): `GHL_PRIVATE_TOKEN`, `GHL_LOCATION_ID`
- רשת: לאפשר את `zoom-ghl-server.onrender.com`, `api.zoom.us`, `zoom.us`
  (ב-11/2026 הגישה ל-onrender נחסמה ע"י ה-proxy של הסשן).
לעולם לא לבקש להדביק מפתחות בצ'אט. אם אין גישה — להגיד בפשטות מה חסר ולתת לגד את הקישור
לבדוק בעצמו בדפדפן.

## הגדרה ראשונית במחשב חדש
ראה **[docs/TEAM_SETUP_HE.md](docs/TEAM_SETUP_HE.md)** — התקנת Python+ffmpeg, venv, וקובץ `.env`.

## כללי עבודה
- תשובות והודעות למשתמש **בעברית**; קוד והערות באנגלית.
- לפני "תיקון", קודם לאבחן (קרדיט? הקלטה קיימת? קצרה מדקה?) ולהסביר, ואז לתקן.
