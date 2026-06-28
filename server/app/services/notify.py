"""Admin alerting via Resend.

Used to flag conditions a human must act on — chiefly API credit running out
(OpenAI/Anthropic), which silently fails the whole pipeline until reloaded.

Deliberately best-effort: a failed alert must never crash the calling task, so
every error here is swallowed after logging. Uses stdlib urllib (not an SDK) to
stay consistent with the rest of the codebase's HTTP approach.
"""

import json
import logging
import urllib.error
import urllib.request

import redis

from ..config import get_settings


log = logging.getLogger(__name__)

_ALERT_TTL_SEC = 30 * 60   # don't re-send the same alert key more than twice an hour


def _throttled(key: str) -> bool:
    """True if an alert under `key` was already sent within the TTL window.

    Prevents a burst of failing calls (e.g. 50 in one credit-out window) from
    sending 50 identical emails. Fails open — if Redis is unreachable we'd
    rather send a duplicate than swallow the alert.
    """
    try:
        r = redis.Redis.from_url(get_settings().redis_url)
        # set(nx=True) returns True only if the key did NOT already exist.
        return not bool(r.set(f"alert:{key}", "1", nx=True, ex=_ALERT_TTL_SEC))
    except Exception as exc:  # noqa: BLE001 — throttling must never block alerting
        log.warning("alert throttle check failed — sending anyway: %s", exc)
        return False


def alert_admin(
    subject: str,
    body: str,
    sms_text: str | None = None,
    throttle_key: str | None = None,
) -> None:
    """Notify the admin across all configured channels (SMS via GHL + email).

    Throttling is applied ONCE here (not per channel) so a single throttle_key
    suppresses the whole alert, not just the first channel that fires.
    """
    if throttle_key and _throttled(throttle_key):
        log.info("admin alert throttled, skipping: %s", throttle_key)
        return
    send_admin_sms(sms_text or subject)
    send_admin_alert(subject, body)


def _digits(value: str | None) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def _il_e164(phone: str) -> str:
    """Normalize an Israeli number to E.164 (+972...). GHL's contact search only
    matches the stored E.164 form, not the local 0xx form."""
    d = _digits(phone)
    if d.startswith("0"):
        d = "972" + d[1:]
    elif not d.startswith("972"):
        d = "972" + d
    return "+" + d


def send_admin_sms(message: str, throttle_key: str | None = None) -> None:
    """SMS the admin via GHL. No-op (logged) if not configured/contact missing.

    Resolves the GHL contact by phone (admin_sms_phone), then sends an SMS on
    the conversations API. Best-effort: never raises into the caller.
    """
    if throttle_key and _throttled(throttle_key):
        log.info("admin SMS throttled, skipping: %s", throttle_key)
        return

    settings = get_settings()
    phone = settings.admin_sms_phone
    if not phone:
        return
    # Imported here (not at module top) to avoid pulling httpx/GHL on every
    # import of notify, and to keep the import graph flat.
    from .ghl_client import GHLClient, GHLError

    target = _digits(phone)[-9:]   # last 9 digits — robust to +972 / 0 prefixes
    try:
        with GHLClient() as ghl:
            # GHL only matches the stored E.164 form, so query that first.
            contact_id = None
            for query in (_il_e164(phone), phone):
                contacts = ghl.search_contacts(query=query, limit=10)
                contact_id = next(
                    (c.get("id") for c in contacts if _digits(c.get("phone")).endswith(target)),
                    None,
                )
                if contact_id:
                    break
            if not contact_id:
                log.error("admin SMS: no GHL contact matches phone %s", phone)
                return
            ghl.send_sms(contact_id, message)
            log.info("admin SMS sent to %s (contact %s)", phone, contact_id)
    except GHLError as exc:
        log.error("admin SMS failed (GHL): %s", exc)
    except Exception as exc:  # noqa: BLE001 — alerting is best-effort
        log.error("admin SMS failed: %s", exc)


def send_admin_alert(subject: str, body: str, throttle_key: str | None = None) -> None:
    """Email the admin. No-op (with a log line) if not configured or throttled."""
    if throttle_key and _throttled(throttle_key):
        log.info("admin alert throttled, skipping: %s", throttle_key)
        return

    settings = get_settings()
    if not settings.resend_api_key:
        log.warning("RESEND_API_KEY not set — cannot send admin alert: %s", subject)
        return

    payload = json.dumps({
        "from": settings.alert_from_email,
        "to": [settings.admin_email],
        "subject": subject,
        "text": body,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {settings.resend_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            log.info("admin alert sent (HTTP %s): %s", resp.status, subject)
    except urllib.error.HTTPError as exc:
        log.error("admin alert failed: HTTP %s %s", exc.code, exc.read().decode()[:300])
    except Exception as exc:  # noqa: BLE001 — alerting is best-effort
        log.error("admin alert failed: %s", exc)


# Substrings that indicate an out-of-credit / quota condition (as opposed to a
# transient throughput 429). Matched case-insensitively against the exception.
_CREDIT_MARKERS = (
    "insufficient_quota",
    "credit balance is too low",
    "credit balance too low",
    "exceeded your current quota",
    "billing",
)


def is_credit_error(exc: object) -> bool:
    """Heuristic: does this exception look like 'out of credit / quota'?"""
    s = str(exc).lower()
    return any(marker in s for marker in _CREDIT_MARKERS)
