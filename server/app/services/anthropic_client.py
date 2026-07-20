"""Anthropic client — summarizes meeting transcripts in Hebrew.

Returns both a Markdown summary and an extracted contact name (used in the GHL
matching stage as a fallback when the meeting_topic doesn't yield a match).

Uses prompt caching on the system prompt — the system prompt is constant across
calls, so caching makes repeat calls ~90% cheaper on that segment.
"""

import logging
import re

from anthropic import Anthropic, APIConnectionError, APIError, RateLimitError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..config import get_settings


log = logging.getLogger(__name__)


SYSTEM_PROMPT = """אתה עוזר עסקי של גד תמיר, יזם ישראלי שמנהל את More-Than (CRM + אוטומציות).
לפניך תמלולים של פגישות זום של עובדות גד עם לקוחות.

המשימה שלך: לתת סיכום קצר וענייני **בעברית בלבד**, בפורמט המדויק הבא:

שם איש קשר משוער: <השם המלא של הלקוח/לקוחה כפי שמופיע בתמלול, או "לא ידוע" אם אי-אפשר לזהות בוודאות>

סיכום
[3-6 שורות שמסכמות את עיקרי הפגישה — מי השתתף אם ברור, על מה דיברו, מה הוחלט]

משימות
- [מה לעשות] - אחראי: [שם או "לא צוין"] - דדליין: [תאריך או "לא צוין"]
- ...

נקודות מפתח
- [תובנה חשובה 1]
- [תובנה חשובה 2]

סוג הפגישה: <אחד מהערכים מהרשימה בלבד>

פירוט מלא
[כאן הפירוט הארוך — ראה "כללי הפירוט המלא" למטה]

כללים:
- עברית בלבד
- ענייני, ללא מילוי או נימוסים מיותרים
- אם אין משימות ברורות — כתוב "אין משימות שזוהו"
- אל תמציא מידע שלא נאמר בתמלול
- אל תוסיף כותרת/הקדמה לפני "שם איש קשר משוער:"
- אל תוסיף הערות אחרי "פירוט מלא" — סיים שם

כללי הפירוט המלא:
החלקים שמעל הם לסריקה מהירה. "פירוט מלא" הוא לעבודה בפועל — ממנו בונים אפיון,
מקימים מערכת, או חוזרים ללקוח שטוען "ביקשתי X". לכן אל תקצר בו: עדיף ארוך ומדויק.

זיהוי סוג הפגישה: שם הפגישה מכיל בדרך כלל את הסוג במפורש — זה המקור הראשון.
אם השם לא מכיל סוג, או שהתוכן סותר אותו בבירור, קבע לפי מה שקרה בפועל וציין
זאת בשורה הראשונה של הפירוט. ערכים אפשריים:

1. **פגישת התאמה / הדגמה** — שיחה ראשונה עם לקוח פוטנציאלי:
   - העסק: תחום, גודל, ותק, כמות לידים/לקוחות
   - איך עובדים היום: כלים קיימים, תהליך המכירה, מי עושה מה
   - הכאב: מה לא עובד, מה גוזל זמן, מה נופל בין הכיסאות
   - **מה הלקוח ביקש שיהיה לו במערכת** — רשימה מפורטת, בקשה-בקשה, כולל
     בקשות שנאמרו אגב אורחא. זה החלק שאסור לפספס ממנו כלום
   - מספרים: מחירים, תקציב, כמויות, לוחות זמנים
   - התנגדויות וחששות, ומה נענה עליהן
   - **מה נאמר ללקוח** — ראה "בלוק ההבטחות" בהמשך. חובה
   - הצעד הבא שסוכם

2. **פגישת סיכום והתחלת עבודה** — הלקוח סוגר ומתחילים:
   - **מה בדיוק נסגר**: היקף, חבילה, מה כלול ומה לא
   - **מחיר ותנאי תשלום** — כל מספר שנאמר
   - **מה נאמר ללקוח** — ראה "בלוק ההבטחות" בהמשך. חובה, וזה החלק
     החשוב ביותר בסוג הזה: זה המסמך שהמטמיע ושירות הלקוחות יסתמכו עליו
   - צרכים ודגשים שהלקוח העלה לקראת ההקמה
   - מה נדרש מהלקוח כדי להתחיל
   - לוח זמנים ותאריך התחלה

3. **פגישת אפיון** — שאפשר יהיה לגזור מכאן מסמך אפיון בלי להאזין להקלטה:
   - תהליכים ופייפליינים: שלבים, שמות, מה מזיז ליד משלב לשלב
   - שדות ונתונים שצריך לאסוף
   - אוטומציות: טריגר → פעולה, לכל אחת בנפרד
   - הודעות ותכנים: מה נשלח, מתי, באיזה ערוץ, ונוסח אם נאמר
   - אינטגרציות וחיבורים למערכות חיצוניות
   - משתמשים והרשאות: מי רואה מה
   - מה נשאר פתוח / דורש החלטה של הלקוח

4. **פגישת הקמה חד פעמית**:
   - מה הוקם והוגדר בפועל, רכיב-רכיב
   - מה לא הוקם ולמה
   - מה נדרש מהלקוח (חומרים, גישות, החלטות)
   - תקלות שהתגלו ואיך טופלו
   - מה הודרך והוסבר ללקוח

5. **פגישת הטמעה ראשונה**:
   - מה הודרך בפועל, נושא-נושא
   - מה הלקוח הצליח לבצע ומה התקשה בו
   - **בקשות ושינויים שהלקוח ביקש** — בציטוט
   - מה נשאר לפגישה הבאה
   - שיעורי בית ללקוח

6. **פגישת הטמעה שנייה** — כמו הראשונה, ובנוסף:
   - מה מהפגישה הקודמת בוצע ומה לא
   - **פערים בין מה שהובטח/אופיין לבין מה שקיים בפועל**
   - האם הלקוח עצמאי בשימוש, ובמה עדיין תלוי בנו

7. **אחר / פנימית / לא ברור** — אם זו לא אחת מהפגישות שלמעלה, כתוב את סוג
   הפגישה כפי שהבנת אותו, ופרט לפי אותו היגיון: מה נאמר, מה סוכם, מה נדרש.
   אם באמת אין תוכן עסקי — כתוב "אין פירוט נוסף" ואל תמציא.

בכל הסוגים: כשלקוח מבקש משהו או כשמבטיחים לו משהו — **צטט את המילים שלו**
במרכאות. ציטוט הוא ההבדל בין סיכום שאפשר להסתמך עליו לבין פרשנות.

בלוק ההבטחות (חובה בסוגים 1 ו-2):
הסיכום הזה עובר לשירות לקוחות ולהטמעות. כשלקוח אומר אחר כך "הבטיחו לי X",
הם צריכים לדעת מהבלוק הזה — בלי להאזין להקלטה — מה בדיוק נאמר לו ובאיזו רמת
ודאות. לכן אל תסנן ואל תרכך: עדיף לכלול אמירה גבולית מאשר להשמיט אותה.

כתוב תחת הכותרת "מה נאמר ללקוח", ומיין לארבע קבוצות. השמט קבוצה רק אם היא ריקה:

**התחייבנו** — נאמר כדבר שיקרה. ציטוט + מי אמר.
   דוגמה: - "אנחנו נחבר לך את הוואטסאפ בהקמה" (סער)

**נאמר בהסתייגות** — הותנה, סויג, או נאמר כהערכה ("בדרך כלל", "אמור לעבוד",
"תלוי ב..."). הלקוח עלול לזכור את זה כהתחייבות מלאה, ולכן חשוב שיופיע בנפרד.
   דוגמה: - "בדרך כלל זה לוקח שבועיים" (סער) — הערכה, לא התחייבות

**נבדוק ונחזור** — נאמר שייבדק מול גורם אחר או שתינתן תשובה בהמשך. **כל פריט
כאן הוא חוב פתוח ללקוח** — אם לא סגרו אותו בפגישה, ציין זאת במפורש.
   דוגמה: - "אני אבדוק עם גד לגבי ההנחה ואחזור אליך" (סער) — לא נסגר בפגישה

**הוצג כיכולת, לא הובטח** — יכולות שהודגמו או תוארו אך לא נכללו במה שנסגר.
זה המקור השכיח ביותר לפער: הלקוח שמע "המערכת יודעת לעשות X" והבין שהוא מקבל X.
אם יכולת הוצגה ואינה בחבילה שנסגרה — **כתוב זאת במפורש בשורה נפרדת**.
   דוגמה: - הוצגו בוטי AI בוואטסאפ — לא נכללו בחבילה הבסיסית שנסגרה

אם באמת לא נאמרה שום הבטחה — כתוב "לא נאמרו התחייבויות". אל תמציא ואל תסיק
הבטחה ממשהו שלא נאמר.
"""

_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        settings = get_settings()
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not configured")
        _client = Anthropic(api_key=settings.anthropic_api_key)
    return _client


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type((APIConnectionError, RateLimitError, APIError)),
)
def summarize_meeting(
    transcript: str, employee_name: str, meeting_topic: str | None = None
) -> tuple[str, str | None]:
    """Return (summary_markdown, extracted_contact_name_or_None).

    `meeting_topic` is the Zoom title. It usually names the meeting type outright
    ("<לקוח> + פגישת אפיון : <עובד> - More-Than"), which is what selects the
    checklist the detailed section is written against — so pass it when known.
    """
    settings = get_settings()
    client = _get_client()

    header = f"שם העובדת שהעלתה את ההקלטה: {employee_name}"
    if meeting_topic:
        header += f"\nשם הפגישה בזום: {meeting_topic}"
    user_msg = f"{header}\n\nתמלול הפגישה:\n\n{transcript}"

    log.info("summarize start", extra={"chars": len(transcript), "employee": employee_name})
    message = client.messages.create(
        model=settings.anthropic_model,
        # The "פירוט מלא" section is deliberately exhaustive — at 2000 the output
        # was truncated mid-section, losing exactly the requirements it exists to
        # capture. Hebrew also costs noticeably more tokens per word than English.
        max_tokens=8000,
        temperature=0.3,
        system=[
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
        ],
        messages=[{"role": "user", "content": user_msg}],
    )

    raw_text = "".join(block.text for block in message.content if hasattr(block, "text")).strip()
    log.info(
        "summarize done",
        extra={
            "out_chars": len(raw_text),
            "in_tokens": message.usage.input_tokens,
            "out_tokens": message.usage.output_tokens,
            "cache_read": getattr(message.usage, "cache_read_input_tokens", 0),
            "cache_create": getattr(message.usage, "cache_creation_input_tokens", 0),
        },
    )

    contact_name, summary = _split_contact_and_summary(raw_text)
    return summary, contact_name


_CONTACT_RE = re.compile(r"^\s*שם איש קשר משוער\s*:\s*(.+?)\s*$", re.MULTILINE)


def _split_contact_and_summary(text: str) -> tuple[str | None, str]:
    m = _CONTACT_RE.search(text)
    if not m:
        return None, text
    name = m.group(1).strip()
    summary = _CONTACT_RE.sub("", text, count=1).strip()
    if name in ("לא ידוע", "לא צוין", "-", ""):
        return None, summary
    return name, summary


PHONE_CALL_SYSTEM_PROMPT = """אתה עוזר עסקי של גד תמיר, יזם ישראלי שמנהל את More-Than (CRM + אוטומציות).
לפניך תמלול של שיחת טלפון בין עובדת/עובד של More-Than לבין לקוח/ה.

המשימה שלך: לתת סיכום קצר, ענייני, **בעברית בלבד**, בפורמט המדויק הבא:

סיכום
[2-4 שורות שמסכמות את עיקרי השיחה — מה היה הנושא, מה היה הקונטקסט, מה הוחלט]

נושא השיחה
[משפט אחד מתמצת — תמיכה / מכירה / אונבורדינג / חידוש / תלונה / אחר]

משימות
- [מה לעשות] - אחראי: [שם או "לא צוין"] - דדליין: [תאריך או "לא צוין"]
- ...

נקודות לעיבוד פנימי
- [תובנה חשובה 1]
- [תובנה חשובה 2]

כללים:
- עברית בלבד
- ענייני וקצר — שיחות טלפון בד"כ קצרות מפגישות, גם הסיכום צריך להיות קצר
- אם אין משימות ברורות — כתוב "אין משימות שזוהו"
- אל תמציא מידע שלא נאמר בשיחה
- אל תוסיף כותרת/הקדמה לפני "סיכום"
"""


def summarize_phone_call(transcript: str, employee_name: str, duration_seconds: int) -> str:
    """Hebrew summary of a phone-call transcript. Different prompt/format from meeting summaries."""
    settings = get_settings()
    client = _get_client()
    user_msg = (
        f"שם העובדת/העובד: {employee_name}\n"
        f"משך השיחה: {duration_seconds // 60} דקות ו-{duration_seconds % 60} שניות\n\n"
        f"תמלול השיחה:\n\n{transcript}"
    )
    log.info("summarize_phone_call start", extra={"chars": len(transcript), "employee": employee_name, "duration": duration_seconds})
    message = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=1500,
        temperature=0.3,
        system=[{"type": "text", "text": PHONE_CALL_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in message.content if hasattr(b, "text")).strip()
    log.info(
        "summarize_phone_call done",
        extra={
            "out_chars": len(text),
            "in_tokens": message.usage.input_tokens,
            "out_tokens": message.usage.output_tokens,
        },
    )
    return text


_TRANSLITERATE_SYSTEM = (
    "You produce common name spelling variants for cross-language lookups.\n"
    "Given a personal name (Hebrew or English/Latin), return 1-4 alternate spellings "
    "in the OTHER script that are commonly used for the same name in Israel.\n"
    "Examples:\n"
    "  אביאור → Avior, Aviour\n"
    "  דניאל → Daniel, Dani\n"
    "  Avi  → אבי, אבי\n"
    "  Sarah → שרה\n"
    "Return ONLY a JSON array of strings, no prose. If the input is not a recognizable "
    "personal name, return []."
)


@retry(
    reraise=True,
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception_type((APIConnectionError, RateLimitError, APIError)),
)
def transliterate_name(name: str) -> list[str]:
    """Return common spelling variants of `name` in the other script (Latin <-> Hebrew).

    Empty list if Claude can't or shouldn't variate. Tiny call — cached system prompt,
    output is just a short JSON array.
    """
    if not name or len(name.strip()) < 2:
        return []
    settings = get_settings()
    client = _get_client()

    import json as _json
    message = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=120,
        temperature=0.2,
        system=[{"type": "text", "text": _TRANSLITERATE_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": name.strip()}],
    )
    raw = "".join(b.text for b in message.content if hasattr(b, "text")).strip()
    log.info("transliterate", extra={"name": name, "raw": raw[:200]})

    # Pull the first JSON array out — Claude may occasionally wrap in ```json fences.
    m = re.search(r"\[[^\]]*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        out = _json.loads(m.group(0))
    except _json.JSONDecodeError:
        return []
    if not isinstance(out, list):
        return []
    return [str(x).strip() for x in out if isinstance(x, str) and x.strip() and x.strip().lower() != name.strip().lower()]
