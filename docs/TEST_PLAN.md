# TEST_PLAN — בדיקה מקצה לקצה לפני שחרור לעובדות

לפני שמשחררים את ZoomGHL לאורנית ולרות, לעבור על כל הפריטים. אם משהו לא עובד — לתקן ולוודא לפני שמעבירים הלאה.

מומלץ לעבור בסדר. כל סעיף מסומן `[ ]` — אחרי שעובד תסמן `[x]`.

---

## חלק א — Deploy לשרת

- [ ] **א.1** GitHub repo קיים, public/private כרצוי, branch `main` עם הקוד העדכני
- [ ] **א.2** Render Blueprint נוצר מתוך `render.yaml`. 3 רכיבים מופיעים:
  - [ ] `zoom-ghl-db` (Postgres, status `available`)
  - [ ] `zoom-ghl-redis` (Redis, status `available`)
  - [ ] `zoom-ghl-server` (Web, status `live`)
- [ ] **א.3** כל ה-secrets הוזנו ב-Render → zoom-ghl-server → Environment:
  - [ ] `OPENAI_API_KEY`
  - [ ] `ANTHROPIC_API_KEY`
  - [ ] `GHL_PRIVATE_TOKEN`
  - [ ] `GHL_LOCATION_ID`
- [ ] **א.4** Build הצליח (`Build successful` ב-Events tab; ~5 דקות)
- [ ] **א.5** Health check: `curl https://<your-url>/health` → `{"status":"ok","environment":"production"}`
- [ ] **א.6** השרת ב-Logs מציג JSON תקין (לא traceback)

## חלק ב — יצירת עובדת בדיקה (Test User)

- [ ] **ב.1** ב-Render → zoom-ghl-server → Shell:
  ```bash
  python -m app.cli add-employee --name "Test User"
  ```
- [ ] **ב.2** ה-CLI החזיר API key (`zghl_…`). שמרת אותו.
- [ ] **ב.3** הרצה של `python -m app.cli list-employees` מציגה את Test User עם status `active`

## חלק ג — Uploader: התקנה ובדיקת חיבור (במק שלך)

- [ ] **ג.1** בנית `.dmg` עם `bash uploader/build_macos.sh`. הקובץ `dist/ZoomGHL.dmg` קיים, גודל ~9-15MB.
- [ ] **ג.2** מותקן ב-`/Applications/ZoomGHL.app` (אחרי גרירה מ-DMG)
- [ ] **ג.3** הפעלה ראשונה (קליק-ימני → Open לעקיפת gatekeeper) — חלון הגדרות נפתח עברית, RTL
- [ ] **ג.4** מילאת את כל השדות (שם, API key מסעיף ב.2, URL השרת, תיקיית הקלטות)
- [ ] **ג.5** לחיצה על **"בדוק חיבור"** מציגה `✓ חיבור תקין — 0 jobs קודמים`
- [ ] **ג.6** לחיצה על **"שמירה"** סוגרת את החלון
- [ ] **ג.7** אייקון Z כחול מופיע בסרגל המנו (פינה ימנית עליונה)
- [ ] **ג.8** לחיצה על האייקון מציגה את כל פריטי התפריט בעברית, התפריט מתעדכן
- [ ] **ג.9** קובץ הלוג קיים: `~/Library/Application Support/ZoomGHL/uploader.log` עם שורות `scanner started`, `scanning ...`

## חלק ד — Pipeline מקצה לקצה

- [ ] **ד.1** הקלטת פגישה אמיתית קצרה ב-זום (1-2 דקות). דבר על איש קשר אמיתי שקיים אצלך ב-GHL, או "שרה" / "ברק" שיש להם רשומות.
- [ ] **ד.2** סיים את ההקלטה. זום יוצר תיקייה ב-`~/Documents/Zoom/<תאריך_שעה> <topic>/audio_only.m4a`
- [ ] **ד.3** מהתפריט של ZoomGHL → **"סרוק עכשיו"** (לא לחכות 30 דק')
- [ ] **ד.4** תוך 10-30 שניות הסטטוס בתפריט: `✓ העלאה אחרונה: <זמן>`
- [ ] **ד.5** התיקייה עברה ל-`~/Documents/Zoom/uploaded/`
- [ ] **ד.6** ב-Render Logs רואים סדרת שלבים מאותו `job_id`: `scanning` → `upload stored` → `ffmpeg start/done` → `transcribe start/done` → `summarize done` → `contact matched` → `note created`
- [ ] **ד.7** ה-Job ב-API מציג `status: completed` עם `ghl_contact_id` ו-`ghl_note_id`:
  ```bash
  curl -H "X-API-Key: zghl_..." https://<url>/jobs?limit=1
  ```
- [ ] **ד.8** ב-GHL → contact הרלוונטי → tab Notes → Note חדש עם הכותרת `📞 סיכום פגישת זום - <תאריך> - הועלה ע"י Test User`
- [ ] **ד.9** התוכן בעברית עם שלושת הסקשנים: סיכום / משימות / נקודות מפתח
- [ ] **ד.10** הסיכום נכון (לא הזוי, לא ערבוב שפות, משימות הגיוניות)

## חלק ה — Edge cases

- [ ] **ה.1** הקלטה ארוכה — פגישה 60+ דקות. ודא שאינה נחתכת. תמלול שלם.
- [ ] **ה.2** איש קשר שאינו קיים בGHL — צור תיקייה עם שם דמיוני ("שיחה עם שם בדוי לבדיקה") והעלה. צפי: `status=unmatched`, summary נשמר ב-DB אבל אין note ב-GHL.
- [ ] **ה.3** קובץ לא נתמך — נסה `.txt`. צפי: `415 Unsupported Media Type` ב-uploader log.
- [ ] **ה.4** API key לא תקין — שנה ב-הגדרות ל-key מומצא, סרוק. צפי: `Auth failed: 401`.
- [ ] **ה.5** אין רשת — נתק WiFi, סרוק. צפי: error ב-log, ה-job נשאר כ-`failed` ב-SQLite מקומי, יחזור בסבב הבא.
- [ ] **ה.6** Restart של המק — האייקון חוזר אוטומטית? (רק אם הגדרת auto-start כמתואר ב-EMPLOYEE_README).

## חלק ו — תפעול

- [ ] **ו.1** רוטציית API key עובדת: `python -m app.cli rotate-key --id <id>`, מעדכנים בהגדרות, סורקים — עובד עם החדש.
- [ ] **ו.2** השבתת עובדת עובדת: `deactivate`, ה-key הקודם מחזיר 401.
- [ ] **ו.3** `list-jobs --status failed` מציג את הכשלים מהבדיקות.

## חלק ז — Final go/no-go

- [ ] **ז.1** עברת על כל הפריטים לעיל, הצלחות בלבד.
- [ ] **ז.2** יש לך API key מוכן לאורנית.
- [ ] **ז.3** יש לך API key מוכן לרות.
- [ ] **ז.4** קובץ `.dmg` שלם (לא ה-Test User אלא build חדש מ-main העדכני).
- [ ] **ז.5** קישור ל-`docs/EMPLOYEE_README.md` מוכן לשליחה.

✅ **כשהכל מסומן — מותר לשלוח להן.**

---

## אם משהו לא עובד באמצע — צ'ק-ליסט מהיר

| תופעה | היכן לבדוק |
|---|---|
| כפתור "בדוק חיבור" מחזיר ❌ | URL/key/אינטרנט |
| `scanning` בלוג אבל לא מעלה | תיקייה ריקה? קובץ עדיין נכתב (idle filter)? |
| `received` נתקע | celery worker מת — בדוק Render logs |
| `transcribe failed` | OpenAI key/quota |
| `summarize failed` | Anthropic key/quota |
| `unmatched` כל הזמן | meeting_topic תקין? extracted_contact_name נכון? איש קשר באמת קיים ב-GHL? |
| המק נחסם ב-Gatekeeper | קליק-ימני → Open. אם זה חוזר — חתימת ה-app (Apple Developer ID) — לא חתום בגרסה זו |
