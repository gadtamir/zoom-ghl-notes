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

כללים:
- עברית בלבד
- ענייני, ללא מילוי או נימוסים מיותרים
- אם אין משימות ברורות — כתוב "אין משימות שזוהו"
- אל תמציא מידע שלא נאמר בתמלול
- אל תוסיף כותרת/הקדמה לפני "שם איש קשר משוער:"
- אל תוסיף הערות אחרי "נקודות מפתח" — סיים שם
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
def summarize_meeting(transcript: str, employee_name: str) -> tuple[str, str | None]:
    """Return (summary_markdown, extracted_contact_name_or_None)."""
    settings = get_settings()
    client = _get_client()

    user_msg = f"שם העובדת שהעלתה את ההקלטה: {employee_name}\n\nתמלול הפגישה:\n\n{transcript}"

    log.info("summarize start", extra={"chars": len(transcript), "employee": employee_name})
    message = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=2000,
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
