# CLAUDE.md — מדריך ל-Claude Code בפרויקט הזה

פרויקט שמתמלל ומסכם **פגישות זום** ו**שיחות טלפון** של More-Than, ומצמיד סיכום
כ-Note באיש הקשר ב-GoHighLevel (GHL). יש פייפליין אוטומטי שרץ בענן (Render), אבל
לפעמים שיחות "נופלות" ולא מתומללות — ואז משתמשים בסקריפטים שכאן כדי לתקן ידנית.

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
(`GHL_API_BASE`/contacts/?query=...). הסקריפט מדלג אוטומטית על שיחות <30 שניות
ושיחות בלי הקלטה (לא נענו) — זה תקין, אין שם מה לתמלל.

### שלב 3 — פגישת זום (`server/scripts/backfill_meetings.py`)
רלוונטי בעיקר במחשב של גד (הקלטות הזום נמצאות שם). אותו רעיון: `--from`/`--to`/`--workers`.

## ⚠️ מלכודות סביבה (חשוב — אחרת דברים "נתקעים")
- **תמיד stdlib `urllib`, לא ה-SDK של openai/anthropic** — ה-SDK נתקע בייבוא במחשב הזה.
- ה-GHL מאחורי Cloudflare — צריך header של `User-Agent` דפדפני אחרת 403.
- ריצות ארוכות מתות כשהמחשב נרדם → תמיד `caffeinate -dimsu`.
- פלט/state/לוגים נכתבים ל-`~/zghl_backfill/` (מחוץ ל-git ול-iCloud). ניתן לשנות עם `ZGHL_WORK_DIR`.
- ה-`.env` (מפתחות סודיים) **לא** בריפו. צריך להניח אותו ב-`server/.env` — ראה `server/.env.example`.

## הגדרה ראשונית במחשב חדש
ראה **[docs/TEAM_SETUP_HE.md](docs/TEAM_SETUP_HE.md)** — התקנת Python+ffmpeg, venv, וקובץ `.env`.

## כללי עבודה
- תשובות והודעות למשתמש **בעברית**; קוד והערות באנגלית.
- לפני "תיקון", קודם לאבחן (קרדיט? הקלטה קיימת? <30ש'?) ולהסביר, ואז לתקן.
