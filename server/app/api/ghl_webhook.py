"""GHL workflow webhook — lead won/lost → collect sales-meeting transcripts to Drive.

Optional: the server already finds these leads by polling GHL. To push instead, a
workflow with trigger "Pipeline Stage Changed" → action "Webhook" POST to
    https://zoom-ghl-server.onrender.com/webhooks/ghl/deal-closed?token=<token>&outcome=won|lost

GHL workflows can't sign requests, so a shared token in the URL guards the endpoint
(DEAL_CLOSED_WEBHOOK_TOKEN, falling back to ADMIN_API_TOKEN). The handler only
records the deal; the doc is built later by `closed_deals.check`.
"""

import logging
import secrets

from fastapi import APIRouter, HTTPException, Query, Request

from ..config import get_settings
from ..db import SessionLocal
from ..tasks.closed_deals import register_deal


log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks/ghl", tags=["webhooks"])


def _first(d: dict, *keys: str) -> str | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


@router.post("/deal-closed")
async def deal_closed(
    request: Request,
    token: str | None = Query(default=None),
    outcome: str = Query(default="won", pattern="^(won|lost)$"),
) -> dict:
    s = get_settings()
    expected = s.deal_closed_webhook_token or s.admin_api_token
    if not expected:
        raise HTTPException(status_code=503, detail="webhook token not configured")
    if not token or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="bad token")

    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    contact = payload.get("contact") if isinstance(payload.get("contact"), dict) else {}
    opp = payload.get("opportunity") if isinstance(payload.get("opportunity"), dict) else {}

    contact_id = (
        _first(payload, "contact_id", "contactId")
        or _first(contact, "id", "contact_id")
        or request.query_params.get("contact_id")
    )
    if not contact_id:
        log.warning("deal-closed webhook without contact id", extra={"keys": sorted(payload)[:30]})
        raise HTTPException(status_code=422, detail="contact_id missing")

    name = _first(payload, "full_name", "contact_name", "name") or _first(contact, "name", "full_name")
    if not name:
        name = " ".join(x for x in (_first(payload, "first_name"), _first(payload, "last_name")) if x) or None
    opportunity_id = _first(payload, "opportunity_id", "opportunityId") or _first(opp, "id")

    db = SessionLocal()
    try:
        deal, created = register_deal(db, contact_id, name, opportunity_id, outcome=outcome)
        return {"status": "registered" if created else "duplicate", "deal_id": deal.id}
    finally:
        db.close()
