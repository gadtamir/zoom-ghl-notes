# Spec-builder — אפיון + פרומפט בוט אוטומטי

הרחבה של תהליך העיבוד: לפגישות **אפיון / הטמעה ראשונה**, אחרי התמלול, המערכת מייצרת
אוטומטית שני תוצרים ומפיצה אותם ליעדים שונים.

```
תמלול (קיים) → summarize (קיים)
                     │
                     ▼  (רק אם שם הפגישה מכיל "פגישת אפיון" / "הטמעה ראשונה")
              spec_stage
                ├─ Claude → מסמך אפיון מובנה → PDF ממותג → הערת GHL (טקסט + קישור ל-PDF)
                └─ Claude → פרומפט בוט (אישיות/מטרה/מידע נוסף) + בסיסי ידע → Google Drive
```

## מה מיוצר ולאן

| תוצר | יעד | שם הקובץ |
|------|-----|----------|
| מסמך אפיון (PDF ממותג) | תיקיית הלקוח ב-Drive + קישור בהערת GHL | `{לקוח} - פגישת אפיון - {תאריך}.pdf` |
| טקסט האפיון | גוף הערת GHL על כרטיס הלקוח | — |
| פרומפט בוט | תיקיית הלקוח ב-Drive (Google Doc) | `{לקוח} - בוט - {תאריך}` |
| בסיסי ידע | תת-תיקייה "בסיסי ידע" של הלקוח (Doc לכל בסיס) | `{שם הבסיס}` |

מבנה ב-Drive (מתחת ל-`GDRIVE_PARENT_FOLDER_ID`):
```
{לקוח}/
  {לקוח} - פגישת אפיון - {תאריך}.pdf
  {לקוח} - בוט - {תאריך}            ← Google Doc
  בסיסי ידע/
    {בסיס 1}, {בסיס 2}, ...          ← Google Doc לכל אחד
```

## שני מצבי הפעלה

1. **אוטומטי** — כל פגישת אפיון/הטמעה שעוברת בתהליך העיבוד מייצרת את התוצרים לבד.
2. **ידני** — דף אינטרנט: `https://<server>/spec/ui?token=<SPEC_UI_TOKEN>`
   מציג פגישות אחרונות (אפיון/הטמעה מסומנות), וכפתור "צור מסמכים" לכל אחת.

הכל **best-effort**: אם הפקת האפיון/הדרייב נכשלת, תהליך התמלול→סיכום→הערה הרגיל לא נשבר.

## משתני סביבה חדשים (Render → service env)

| משתנה | תיאור |
|-------|-------|
| `GOOGLE_CLIENT_ID` | OAuth client id של אפליקציית Google |
| `GOOGLE_CLIENT_SECRET` | OAuth client secret |
| `GOOGLE_REFRESH_TOKEN` | refresh token לחשבון של אורנית (ראה למטה) |
| `GDRIVE_PARENT_FOLDER_ID` | ה-id של תיקיית האב ב-Drive של אורנית |
| `SPEC_UI_TOKEN` | סוד להגנת דף ההפעלה הידנית (המצא מחרוזת אקראית) |

בלי משתני ה-Google, האפיון עדיין ייכתב כטקסט בהערת GHL — רק ההעלאה ל-Drive והקישור ל-PDF ידלגו.

## הפקת הרשאת Google (פעם אחת)

1. ב-[Google Cloud Console](https://console.cloud.google.com): צור פרויקט → הפעל **Google Drive API**.
2. צור **OAuth client id** מסוג **Desktop app** → הורד `client_secret.json`.
3. מקומית, מחוברת לחשבון **Oranitmorethan@gmail.com**:
   ```
   pip install google-auth-oauthlib google-api-python-client
   python server/scripts/google_oauth_setup.py client_secret.json
   ```
   אשר בדפדפן — הסקריפט ידפיס `GOOGLE_CLIENT_ID/SECRET/REFRESH_TOKEN`.
4. ב-Drive, צור תיקיית אב (למשל "לקוחות — אפיון ובוטים"), פתח אותה, וה-id נמצא ב-URL
   (`.../folders/<זה ה-id>`). הכנס ל-`GDRIVE_PARENT_FOLDER_ID`.
5. הזן את כל הערכים ב-Render → **Manual Deploy** / redeploy.

## פריסה

`git push` → Render בונה מחדש (autoDeploy). ה-Dockerfile מתקין Chromium ל-Playwright
(`playwright install --with-deps chromium`) — הבנייה הראשונה ארוכה יותר (~2–3 דקות נוספות).

## קבצים שנוספו

```
server/app/services/spec_client.py          — הפקת אפיון + פרומפט (Claude, tool-use)
server/app/services/spec_render.py          — Jinja2 → HTML → PDF (Playwright/Chromium)
server/app/services/bot_prompt_render.py     — פורמט טקסט הפרומפט
server/app/services/gdrive_client.py         — העלאה ל-Google Drive
server/app/templates/spec.html               — התבנית הממותגת (RTL)
server/app/tasks/spec_stage.py               — השלב: מזהה, מייצר, מעלה, מכין הערה
server/app/api/spec.py                        — דף ההפעלה הידנית + endpoints
server/scripts/google_oauth_setup.py          — קבלת refresh token (פעם אחת)
```

שינויים: `tasks/pipeline.py` (קריאה לשלב), `tasks/ghl.py` (הוספת טקסט להערה),
`config.py`, `main.py`, `requirements.txt`, `Dockerfile`, `render.yaml`.
