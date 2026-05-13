"""GoHighLevel API client — searches contacts by name and creates notes.

Auth: Private Integration Token in `Authorization: Bearer pit-...`
Version header: `Version: 2021-07-28`
Scope: a single Location (sub-account) — passed as `locationId` on every call.
"""

import logging
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..config import get_settings


log = logging.getLogger(__name__)


class GHLError(RuntimeError):
    pass


class GHLClient:
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.ghl_private_token or not settings.ghl_location_id:
            raise GHLError("GHL_PRIVATE_TOKEN or GHL_LOCATION_ID not configured")
        self._location_id = settings.ghl_location_id
        self._client = httpx.Client(
            base_url=settings.ghl_api_base,
            headers={
                "Authorization": f"Bearer {settings.ghl_private_token}",
                "Version": settings.ghl_api_version,
                "Accept": "application/json",
            },
            timeout=30.0,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):  # for `with` use
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, GHLError)),
    )
    def search_contacts(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Substring/fuzzy search by name/email/phone."""
        params = {"locationId": self._location_id, "query": query, "limit": limit}
        r = self._client.get("/contacts/", params=params)
        if r.status_code != 200:
            raise GHLError(f"search_contacts {r.status_code}: {r.text[:300]}")
        data = r.json()
        return data.get("contacts", [])

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, GHLError)),
    )
    def create_note(self, contact_id: str, body: str, user_id: str | None = None) -> dict[str, Any]:
        """Attach a note to a contact. Returns the created note object."""
        payload: dict[str, Any] = {"body": body}
        if user_id:
            payload["userId"] = user_id
        r = self._client.post(f"/contacts/{contact_id}/notes", json=payload)
        if r.status_code not in (200, 201):
            raise GHLError(f"create_note {r.status_code}: {r.text[:300]}")
        data = r.json()
        # API returns {"note": {...}} on success.
        return data.get("note", data)
