"""Spec + bot-prompt generation via Claude.

Two generators, both driven off a discovery-call ("פגישת אפיון" / "הטמעה ראשונה")
transcript:

  generate_spec(...)        → structured spec dict (rendered later to a branded PDF)
  generate_bot_prompt(...)  → {personality, goal, extra_info, knowledge_bases[]}

Structured output is forced via Anthropic tool-use: Claude MUST call the
`emit_spec` / `emit_bot_prompt` tool, so we get schema-valid JSON back instead of
free text we'd have to parse. System prompts are cached (constant per call).
"""

import logging

from anthropic import Anthropic, APIConnectionError, APIError, RateLimitError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..config import get_settings


log = logging.getLogger(__name__)

_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        settings = get_settings()
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not configured")
        _client = Anthropic(api_key=settings.anthropic_api_key)
    return _client


# --------------------------------------------------------------------------- #
#  SPEC
# --------------------------------------------------------------------------- #

SPEC_SYSTEM_PROMPT = """אתה בונה מסמכי אפיון לקוח עבור More-Than (מורדן) — חברה ישראלית שמקימה
מערכות CRM + אוטומציות + בוטים ל-WhatsApp ללקוחות עסקיים. גד תמיר מנהל את החברה, ואורנית
מבצעת את שיחות האפיון עם הלקוחות.

לפניך תמלול של פגישת אפיון (או פגישת הטמעה ראשונה) בין נציגת More-Than ללקוח. המשימה שלך:
להפיק מסמך אפיון מובנה **בעברית בלבד**, נאמן למה שנאמר בפגישה, בפורמט של More-Than.

מבנה המסמך (למד מהדוגמאות של מורדן):
- כותרת קבועה: "מסמך אפיון לקוח".
- subtitle: תיאור קצר של המערכת שתוקם (למשל "מערכת CRM ובוט מכירות חכם בוואטסאפ").
- intro: פסקה אחת שמתחילה ב"מסמך זה מרכז את הצרכים שעלו בפגישת האפיון..." וכוללת את המטרה העסקית.
- סעיפים ממוספרים (התחל ממספר 1, אלא אם יש סעיף רקע שממוספר 0). סעיפים אופייניים, לפי הרלוונטי:
  פרטי הלקוח והעסק · המצב היום · הבעיות/האתגרים · המטרות העסקיות · הפתרון המוצע ·
  זרימת תהליך המכירה (הבוט) · אוטומציות וחימום · קביעת פגישות ויומנים · שלבי מכירה (Pipeline) ·
  מבנה משתמשים והרשאות · מה הלקוח מקבל · אחריות הלקוח וחומרים נדרשים · תמחור · לוחות זמנים והטמעה ·
  השלב הבא · לבירור מול הלקוח.
- כל סעיף מורכב מ-blocks. סוגי block: paragraph, bullets, steps, callout, pills, table, cards.
  * bullets/steps: רשימת פריטים, לכל פריט אפשר lead (מילה מודגשת) + text.
  * pills: תגיות קצרות (מצוין לשלבי Pipeline, למשל "ליד חדש", "נקבעה שיחה").
  * table: לבסיסי ידע/מחירונים/מדרגות. cells יכולים לשאת flag=true כדי לצבוע באדום הערה חשובה.
  * cards: לתמחור (tag="חודשי"/"חד-פעמי", value="335 ₪ / חודש", dark=true לכרטיס מודגש).
  * callout: הערה/נוסח לדוגמה בתוך תיבה מודגשת.
- footer_note: משפט סיכום קבוע, למשל "מסמך זה מסכם את צורכי הלקוח כפי שעלו בפגישת האפיון...".
- accent: "blue" כברירת מחדל (או "gold" אם יתבקש).

כללים קשיחים:
- עברית בלבד. אל תמציא נתונים שלא נאמרו — אם פרט לא עלה, פשוט אל תכלול אותו (אל תמלא בדוי).
- מחירים, שמות פרויקטים, מספרים ומדרגות — רק אם נאמרו במפורש בתמלול.
- אל תשתמש במקף ארוך (—) בתוך ערכי מחיר; בשאר הטקסט מותר.
- שמור על טון עסקי, מסודר ותכליתי, כמו בדוגמאות של מורדן.
"""

# A permissive block object — Claude fills only the fields relevant to `type`.
_SPEC_BLOCK_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": ["paragraph", "bullets", "steps", "callout", "pills", "table", "cards"],
        },
        "subhead": {"type": "string", "description": "כותרת משנה אופציונלית מעל ה-block"},
        "text": {"type": "string", "description": "לשימוש ב-paragraph ו-callout"},
        "items": {
            "type": "array",
            "description": "לbullets/steps: אובייקטים {lead,text}. לpills: מחרוזות. לcards: אובייקטי כרטיס.",
            "items": {
                "type": "object",
                "properties": {
                    "lead": {"type": "string"},
                    "text": {"type": "string"},
                    "value": {"type": "string"},
                    "tag": {"type": "string"},
                    "title": {"type": "string"},
                    "note": {"type": "string"},
                    "dark": {"type": "boolean"},
                    "label": {"type": "string", "description": "טקסט של pill (חלופה ל-text)"},
                },
            },
        },
        "headers": {"type": "array", "items": {"type": "string"}, "description": "כותרות טבלה"},
        "rows": {
            "type": "array",
            "description": "שורות טבלה. כל תא = {text, flag?}.",
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "flag": {"type": "boolean"},
                    },
                    "required": ["text"],
                },
            },
        },
    },
    "required": ["type"],
}

_SPEC_TOOL = {
    "name": "emit_spec",
    "description": "פולט את מסמך האפיון המובנה של More-Than.",
    "input_schema": {
        "type": "object",
        "properties": {
            "client_name": {"type": "string"},
            "domain": {"type": "string", "description": "תחום העסק, למשל 'צילום פרימיום לאירועים'"},
            "doc_type": {"type": "string", "description": "ברירת מחדל 'אפיון צרכים'"},
            "title": {"type": "string", "description": "ברירת מחדל 'מסמך אפיון לקוח'"},
            "subtitle": {"type": "string"},
            "accent": {"type": "string", "enum": ["blue", "gold"]},
            "intro": {"type": "string"},
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "number": {"type": "integer"},
                        "title": {"type": "string"},
                        "blocks": {"type": "array", "items": _SPEC_BLOCK_SCHEMA},
                    },
                    "required": ["number", "title", "blocks"],
                },
            },
            "footer_note": {"type": "string"},
        },
        "required": ["client_name", "subtitle", "intro", "sections"],
    },
}


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type((APIConnectionError, RateLimitError, APIError)),
)
def generate_spec(
    transcript: str,
    client_name: str | None,
    employee_name: str | None = None,
    meeting_date: str | None = None,
) -> dict:
    """Return a structured spec dict ready for the Jinja template."""
    settings = get_settings()
    client = _get_client()

    hint = []
    if client_name:
        hint.append(f"שם הלקוח (מזוהה): {client_name}")
    if employee_name:
        hint.append(f"נציגת More-Than בפגישה: {employee_name}")
    if meeting_date:
        hint.append(f"תאריך הפגישה: {meeting_date}")
    hint_block = ("\n".join(hint) + "\n\n") if hint else ""
    user_msg = f"{hint_block}תמלול פגישת האפיון:\n\n{transcript}"

    log.info("generate_spec start", extra={"chars": len(transcript), "client": client_name})
    message = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=11000,
        # Sonnet 5 rejects temperature and thinks by default; the forced tool call
        # needs neither. Token limits are ~30% up for its larger token counts.
        thinking={"type": "disabled"},
        system=[{"type": "text", "text": SPEC_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        tools=[_SPEC_TOOL],
        tool_choice={"type": "tool", "name": "emit_spec"},
        messages=[{"role": "user", "content": user_msg}],
    )
    spec = _extract_tool_input(message, "emit_spec")
    if client_name and not spec.get("client_name"):
        spec["client_name"] = client_name
    log.info(
        "generate_spec done",
        extra={
            "sections": len(spec.get("sections", [])),
            "in_tokens": message.usage.input_tokens,
            "out_tokens": message.usage.output_tokens,
        },
    )
    return spec


# --------------------------------------------------------------------------- #
#  DEMO-MEETING SPEC  (פגישת הדגמה → מסמך אפיון לקוח)
# --------------------------------------------------------------------------- #

DEMO_SPEC_SYSTEM_PROMPT = """אתה בונה מסמכי אפיון לקוח עבור More-Than (מורדן) — חברה ישראלית שמקימה
מערכות CRM, אוטומציות ובוטים ל-WhatsApp לעסקים. גד תמיר מנהל את החברה.

לפניך תמלול מלא של **פגישת הדגמה** — השיחה הראשונה עם לקוח פוטנציאלי. המשימה שלך: להפיק
ממנו מסמך אפיון מובנה **בעברית בלבד**, שמסכם מה הלקוח צריך ומה הוצע לו.

**הכל נאמר בפגישה.** בפגישת הדגמה של More-Than אומרים ללקוח הכל בקול: מה המחיר החודשי,
מה המחיר השנתי, מה עולה עם התחייבות לשלושה חודשים ומה בלעדיה, כמה שעות הקמה. לכן כל מספר
שנכנס למסמך חייב לבוא מהתמלול — **אתה לא מחשב מחירים ולא משלים אותם מהראש**. אם מספר
נאמר, קח אותו כלשונו. אם באמת לא נאמר, אל תמציא אותו: השמט את השורה.

## שליפת נתונים מהתמלול

- **מספר משתמשים**: חפש אמירות כמו "רק אני", "אני והעובדת", "שני אנשי מכירות". אם נאמר
  שמישהו **לא** יעבוד במערכת — אל תספור אותו.
- **שעות הקמה**: הנציג בדרך כלל נוקב במספר שעות או "בנק שעות". תהליך הטמעה רגיל הוא
  10 שעות — אם נאמר "הטמעה רגילה" בלי מספר, זה המספר. אם נמנו רכיבים (דוחות, חיבורים,
  גוגל שיטס) — סכום אותם.
- **מחירים**: קח בדיוק את מה שנאמר — חודשי, שנתי, עם התחייבות ובלי, עלות ההטמעה.
  אל תמיר, אל תעגל ואל תחשב מחיר חלופי.
- **צרכים, בעיות, מטרות**: רק מדברים שהלקוח אמר.
- **החלטות פתוחות**: משהו שלא הוכרע (למשל "לעבור למערכת או להישאר עם הקיימת") — הצג
  כהחלטה לבירור, לא כעובדה.

## מבנה המסמך

סעיפים לפי הסדר הבא. **דלג על סעיף שאין לו תוכן**, ומספר את הנותרים ברצף מ-1:
1. פרטי הלקוח והעסק · 2. נתוני העסק (אם יש מספרים — כ-block מסוג kpi) · 3. המצב היום ·
4. הבעיות · 5. המטרות · 6. הפתרון המוצע · 7. מה מקימים בפועל (זה הלב — פרט אותו הכי
הרבה, עם subhead לכל תת-נושא) · 8. החלטות וגבולות היקף · 9. מה הלקוח מקבל ·
10. אחריות הלקוח · 11. תמחור · 12. לוחות זמנים והשלב הבא.

## סוגי ה-blocks ומתי להשתמש בהם

- `paragraph` — טקסט רץ. `subhead` אופציונלי מעליו.
- `bullets` — רשימה. לכל פריט `lead` (מילה פותחת מודגשת) ו-`text`.
- `steps` — שלבי תהליך ממוספרים. מצוין לתיאור זרימת מכירה או תהליך עבודה.
- `pills` — תגיות קצרות. הכי מתאים לשלבי פייפליין ("ליד חדש", "נקבעה שיחה", "נסגר").
- `kpi` — מספרי מפתח של העסק. לכל פריט `n` (המספר) ו-`l` (התווית). 2-4 פריטים.
- `table` — טבלה. תא יכול לשאת `flag: true` כדי לסמן אותו באדום.
- `callout` — הערה או נוסח לדוגמה בתוך תיבה.
- `note` — הערת סייג אפורה. **כאן מציגים דברים טעוני בדיקה** (ראה כללי הברזל).
- `options` — תיבות להחלטה פתוחה. לכל פריט `title` ו-`text`. השתמש כשהוצגו שתי חלופות.
- `pricing` — סעיף התמחור. ראה למטה.

## סעיף התמחור

השתמש ב-block מסוג `pricing`, ומלא אותו **רק ממה שנאמר בפגישה**:
- `lead` — משפט פתיחה, למשל "התמחור מבוסס על 2 משתמשים. שתי אפשרויות לבחירה:".
- `plain` — המסלול הרגיל: `title` ("חודש בחודשו"), `price` ("449 ₪"),
  `price_suffix` ('/ לחודש + מע"מ'), `tagline` ("שני משתמשים · ללא התחייבות"),
  `rows` (זוגות `k`/`v` — הטמעה חד-פעמית, אופן חישובה, סה"כ חודש ראשון), ו-`footnote`.
- `featured` — מסלול ההתחייבות, אם הוצג. אותם שדות, בתוספת `tag` ("מומלץ").
- `note` — תמחור הבוט והטלפוניה, אם נאמר.
אם הוצג רק מסלול אחד — השמט את `featured`. **בכל מקרה סיים את הסעיף בכך שכל המחירים
לפני מע"מ**, אם כך נאמר בפגישה.

## כללי ברזל

- **אל תמציא.** מה שלא נאמר בתמלול לא נכנס למסמך. זה גובר על כל כלל אחר כאן.
- **תקן שגיאות תמלול בשמות**: "מורנית" → אורנית, "סאר"/"סער" → סָעַר, "מורדן" כשם
  החברה → More-Than. החזר כל תיקון כזה במערך `corrections`.
- **הפרד עובדה מהחלטה מהמלצה.** מה שסוכם = עובדה; מה שהוצע ולא הוכרע = החלטה;
  מה שהנציג המליץ = המלצה. אל תציג המלצה כאילו סוכמה.
- **דברים טעוני בדיקה** — חיבור ל-API, סקרייפינג, שליחה המונית, אינטגרציה למערכת
  חיצונית — הצג עם `note` שמסייג, ואל תנסח כהבטחה. החזר אותם גם במערך `needs_verification`.
- **התאמה ללקוח**: אם הלקוח הדגיש פשטות, ליווי צמוד, או שנכווה ממערכת קודמת — נסח
  `principle` (משפט עיקרון מנחה שילווה את המסמך). אחרת השאר ריק.
- **מיתוג**: `brand` = "pink" לעסקי קוסמטיקה, יופי, כלות ואירועים; אחרת "blue".
- עברית בלבד, טון עסקי ותכליתי. אל תשתמש במקף ארוך (—) בתוך ערכי מחיר.
- **אל תכתוב ~ או ≈ לפני מספר בתוך משפט בעברית** — בטקסט מימין-לשמאל הסימן קופץ
  לצד השני ומודפס כ-"3≈". כתוב "כ-3 סנט", "כ-60 פניות". (בכרטיסי kpi זה מטופל בעיצוב,
  אבל עדיף "כ-" גם שם.)
"""

_DEMO_BLOCK_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": ["paragraph", "bullets", "steps", "pills", "kpi",
                     "table", "callout", "note", "options", "pricing"],
        },
        "subhead": {"type": "string", "description": "כותרת משנה אופציונלית מעל ה-block"},
        "text": {"type": "string", "description": "ל-paragraph, callout ו-note"},
        "items": {
            "type": "array",
            "description": (
                "bullets/steps: {lead,text} · pills: {label} · kpi: {n,l} · options: {title,text}"
            ),
            "items": {
                "type": "object",
                "properties": {
                    "lead": {"type": "string"},
                    "text": {"type": "string"},
                    "label": {"type": "string", "description": "טקסט של pill"},
                    "title": {"type": "string", "description": "כותרת תיבת options"},
                    "n": {"type": "string", "description": "המספר בכרטיס kpi, למשל '~200'"},
                    "l": {"type": "string", "description": "התווית בכרטיס kpi"},
                },
            },
        },
        "headers": {"type": "array", "items": {"type": "string"}},
        "rows": {
            "type": "array",
            "description": "שורות טבלה. כל תא = {text, flag?}.",
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}, "flag": {"type": "boolean"}},
                    "required": ["text"],
                },
            },
        },
        # --- pricing block ---
        "lead": {"type": "string", "description": "משפט פתיחה לסעיף התמחור"},
        "plain": {"$ref": "#/$defs/priceCard"},
        "featured": {"$ref": "#/$defs/priceCard"},
        "note_text": {"type": "string", "description": "הערת התמחור בתחתית הסעיף"},
    },
    "required": ["type"],
}

_PRICE_CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "למשל 'חודש בחודשו' / 'התחייבות ל-3 חודשים'"},
        "tag": {"type": "string", "description": "תג קטן, למשל 'מומלץ' (רק בכרטיס המודגש)"},
        "price": {"type": "string", "description": "המחיר כפי שנאמר, למשל '449 ₪'"},
        "price_suffix": {"type": "string", "description": "למשל '/ לחודש + מע\"מ'"},
        "tagline": {"type": "string", "description": "שורת תיאור מתחת למחיר"},
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"k": {"type": "string"}, "v": {"type": "string"}},
                "required": ["k", "v"],
            },
        },
        "footnote": {"type": "string"},
    },
}

_DEMO_SPEC_TOOL = {
    "name": "emit_demo_spec",
    "description": "פולט את מסמך האפיון של More-Than שנגזר מפגישת הדגמה.",
    "input_schema": {
        "type": "object",
        "$defs": {"priceCard": _PRICE_CARD_SCHEMA},
        "properties": {
            "client_name": {"type": "string"},
            "domain": {"type": "string", "description": "תחום העסק, למשל 'קוסמטיקה ואיפור כלות'"},
            "doc_type": {"type": "string", "description": "ברירת מחדל 'אפיון צרכים'"},
            "title": {"type": "string", "description": "ברירת מחדל 'מסמך אפיון לקוח'"},
            "subtitle": {"type": "string", "description": "כותרת משנה שמתארת את הפרויקט"},
            "brand": {"type": "string", "enum": ["blue", "pink"]},
            "intro": {"type": "string", "description": "תקציר פתיחה: הכאב + מה המערכת תעשה"},
            "principle": {
                "type": "string",
                "description": "עיקרון מנחה, רק אם הלקוח הדגיש פשטות/ליווי/כוויה קודמת. אחרת השאר ריק.",
            },
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "number": {"type": "integer"},
                        "title": {"type": "string"},
                        "blocks": {"type": "array", "items": _DEMO_BLOCK_SCHEMA},
                    },
                    "required": ["number", "title", "blocks"],
                },
            },
            "footer_note": {"type": "string"},
            "corrections": {
                "type": "array",
                "description": "תיקוני שמות שביצעת, למשל 'מורנית → אורנית'.",
                "items": {"type": "string"},
            },
            "needs_verification": {
                "type": "array",
                "description": "דברים שהוצגו אך טעונים בדיקה טכנית לפני שמתחייבים עליהם.",
                "items": {"type": "string"},
            },
        },
        "required": ["client_name", "subtitle", "intro", "sections"],
    },
}


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type((APIConnectionError, RateLimitError, APIError)),
)
def generate_demo_spec(
    transcript: str,
    client_name: str | None = None,
    employee_name: str | None = None,
    meeting_date: str | None = None,
) -> dict:
    """Demo-call transcript → structured spec dict for the branded PDF.

    Distinct from `generate_spec` (which works off a formal פגישת אפיון): a demo
    call states the commercials out loud, so this one is told to lift every price
    verbatim rather than describe the offer in general terms.
    """
    settings = get_settings()
    client = _get_client()

    hint = []
    if client_name:
        hint.append(f"שם הלקוח (מזוהה): {client_name}")
    if employee_name:
        hint.append(f"נציג/ת More-Than בפגישה: {employee_name}")
    if meeting_date:
        hint.append(f"תאריך הפגישה: {meeting_date}")
    hint_block = ("\n".join(hint) + "\n\n") if hint else ""
    user_msg = f"{hint_block}תמלול פגישת ההדגמה:\n\n{transcript}"

    log.info("generate_demo_spec start", extra={"chars": len(transcript), "client": client_name})
    message = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=16000,
        thinking={"type": "disabled"},
        system=[{"type": "text", "text": DEMO_SPEC_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        tools=[_DEMO_SPEC_TOOL],
        tool_choice={"type": "tool", "name": "emit_demo_spec"},
        messages=[{"role": "user", "content": user_msg}],
    )
    spec = _extract_tool_input(message, "emit_demo_spec")
    if client_name and not spec.get("client_name"):
        spec["client_name"] = client_name
    spec.setdefault("brand", "blue")
    # The template keys off `accent`; `brand` is the friendlier name the tool uses.
    spec["accent"] = spec.get("brand", "blue")
    log.info(
        "generate_demo_spec done",
        extra={
            "sections": len(spec.get("sections", [])),
            "corrections": len(spec.get("corrections", [])),
            "in_tokens": message.usage.input_tokens,
            "out_tokens": message.usage.output_tokens,
        },
    )
    return spec


# --------------------------------------------------------------------------- #
#  BOT PROMPT
# --------------------------------------------------------------------------- #

BOT_SYSTEM_PROMPT = """אתה בונה פרומפטים לבוטים של WhatsApp עבור More-Than (מורדן). לפניך תמלול
של פגישת אפיון עם לקוח. המשימה: לנסח פרומפט לבוט מכירות/שירות, **בעברית בלבד**, בדיוק במבנה
של מורדן, בן ארבעה חלקים:

1. אישיות (personality): מי הבוט, עבור איזה עסק, טון הדיבור, מבנה המשפטים, וכללי פנייה.
   טון ברירת מחדל של מורדן: "אנושי, רגוע, ברור ונעים – כמו נציג אנושי". פנייה ניטרלית מגדרית
   (לא זכר ולא נקבה) אלא אם הלקוח ביקש אחרת.
2. מטרה (goal): מה הבוט בא להשיג — בדרך כלל "לסייע ללקוחות בשאלות ובבירורים, להניע אותם
   לקביעת פגישה/סשן ולכוון לתשלום מראש במערכת". כלול מיפוי מושגים אם רלוונטי (למשל סשן=פגישה).
3. מידע נוסף (extra_info): החוקים הנוקשים ("בל יעבור"), נוסחים מאושרים מילה-במילה (הצג אותם
   בדיוק כפי שהוגדרו בפגישה), זרימת שיחה צעד-אחר-צעד, טריגרים, ומה אסור לומר. זהו החלק הארוך
   והמפורט ביותר — כתוב אותו כרשימת חוקים ממוספרים כשמתאים.
4. בסיסי ידע (knowledge_bases): רשימה. כל פריט = נושא/מוצר/יעד נפרד עם שם (name) ותוכן (content).
   דוגמה: בוט חופשות — כל יעד (איביזה, ברצלונה...) הוא בסיס ידע נפרד. בוט נדל"ן — כל פרויקט.
   אם בפגישה לא הוגדרו בסיסי ידע נפרדים, החזר רשימה ריקה או בסיס ידע כללי אחד.

כללים:
- עברית בלבד. אל תמציא חוקים/נוסחים/מחירים שלא עלו בפגישה.
- אם הלקוח הכתיב נוסח מדויק להודעה — שמור עליו מילה-במילה בתוך extra_info.
- אל תשתמש במקף ארוך (—); השתמש בפסיק או במקף קצר, כי זה חוק נפוץ אצל מורדן.
"""

_BOT_TOOL = {
    "name": "emit_bot_prompt",
    "description": "פולט את הפרומפט המובנה לבוט של More-Than.",
    "input_schema": {
        "type": "object",
        "properties": {
            "client_name": {"type": "string"},
            "business_name": {"type": "string", "description": "שם העסק/המותג של הלקוח"},
            "personality": {"type": "string"},
            "goal": {"type": "string"},
            "extra_info": {"type": "string"},
            "knowledge_bases": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["name", "content"],
                },
            },
        },
        "required": ["personality", "goal", "extra_info", "knowledge_bases"],
    },
}


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type((APIConnectionError, RateLimitError, APIError)),
)
def generate_bot_prompt(transcript: str, client_name: str | None) -> dict:
    """Return {client_name, business_name, personality, goal, extra_info, knowledge_bases[]}."""
    settings = get_settings()
    client = _get_client()

    hint = f"שם הלקוח (מזוהה): {client_name}\n\n" if client_name else ""
    user_msg = f"{hint}תמלול פגישת האפיון:\n\n{transcript}"

    log.info("generate_bot_prompt start", extra={"chars": len(transcript), "client": client_name})
    message = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=11000,
        thinking={"type": "disabled"},
        system=[{"type": "text", "text": BOT_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        tools=[_BOT_TOOL],
        tool_choice={"type": "tool", "name": "emit_bot_prompt"},
        messages=[{"role": "user", "content": user_msg}],
    )
    bot = _extract_tool_input(message, "emit_bot_prompt")
    if client_name and not bot.get("client_name"):
        bot["client_name"] = client_name
    bot.setdefault("knowledge_bases", [])
    log.info(
        "generate_bot_prompt done",
        extra={
            "kb_count": len(bot.get("knowledge_bases", [])),
            "in_tokens": message.usage.input_tokens,
            "out_tokens": message.usage.output_tokens,
        },
    )
    return bot


def _extract_tool_input(message, tool_name: str) -> dict:
    """Pull the forced tool_use input out of a message. Raises if absent."""
    for block in message.content:
        if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
            return dict(block.input)
    raise RuntimeError(f"model did not call {tool_name} (stop_reason={message.stop_reason})")
