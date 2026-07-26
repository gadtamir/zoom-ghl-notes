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
    def list_users(self) -> list[dict[str, Any]]:
        """Location users (staff). Used to map a meeting host's email to a userId."""
        r = self._client.get("/users/", params={"locationId": self._location_id})
        if r.status_code != 200:
            raise GHLError(f"list_users {r.status_code}: {r.text[:300]}")
        return r.json().get("users", [])

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, GHLError)),
    )
    def calendar_events(self, user_id: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        """A user's calendar events in [start_ms, end_ms] (epoch millis).

        Each event carries a `contactId` and, for our bookings, the Zoom join URL
        in `address` — so a recording's meeting id resolves straight to a contact,
        with no name lookup and no ambiguity.
        """
        params = {
            "locationId": self._location_id,
            "userId": user_id,
            "startTime": str(start_ms),
            "endTime": str(end_ms),
        }
        r = self._client.get("/calendars/events", params=params)
        if r.status_code != 200:
            raise GHLError(f"calendar_events {r.status_code}: {r.text[:300]}")
        return r.json().get("events", [])

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, GHLError)),
    )
    def get_contact_appointments(self, contact_id: str) -> list[dict[str, Any]]:
        """Return all appointments (calendar events) attached to this contact."""
        r = self._client.get(f"/contacts/{contact_id}/appointments/")
        if r.status_code == 404:
            return []
        if r.status_code != 200:
            raise GHLError(f"get_contact_appointments {r.status_code}: {r.text[:300]}")
        data = r.json()
        return data.get("events", data.get("appointments", []))

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, GHLError)),
    )
    def search_conversations(
        self,
        limit: int = 25,
        start_after_date: int | None = None,
        contact_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Page of conversations sorted desc by lastMessageDate.

        If `contact_id` is provided, results are restricted to that contact.
        """
        params: dict[str, Any] = {"locationId": self._location_id, "limit": limit}
        if start_after_date is not None:
            params["startAfterDate"] = start_after_date
        if contact_id:
            params["contactId"] = contact_id
        r = self._client.get("/conversations/search", params=params, headers={"Version": "2021-04-15"})
        if r.status_code != 200:
            raise GHLError(f"search_conversations {r.status_code}: {r.text[:300]}")
        return r.json().get("conversations", [])

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, GHLError)),
    )
    def list_pipelines(self) -> list[dict[str, Any]]:
        """All opportunity pipelines (each carries its `stages` list inline)."""
        r = self._client.get(
            "/opportunities/pipelines",
            params={"locationId": self._location_id},
        )
        if r.status_code != 200:
            raise GHLError(f"list_pipelines {r.status_code}: {r.text[:300]}")
        return r.json().get("pipelines", [])

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, GHLError)),
    )
    def opportunities_page(
        self,
        pipeline_id: str,
        limit: int = 100,
        start_after: int | None = None,
        start_after_id: str | None = None,
    ) -> dict[str, Any]:
        """One page of opportunities. Returns the raw payload incl. `meta.nextPage*`."""
        params: dict[str, Any] = {
            "location_id": self._location_id,
            "pipeline_id": pipeline_id,
            "limit": limit,
        }
        if start_after is not None and start_after_id is not None:
            params["startAfter"] = start_after
            params["startAfterId"] = start_after_id
        r = self._client.get("/opportunities/search", params=params)
        if r.status_code != 200:
            raise GHLError(f"opportunities_page {r.status_code}: {r.text[:300]}")
        return r.json()

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, GHLError)),
    )
    def list_messages(self, conversation_id: str, limit: int = 100) -> list[dict[str, Any]]:
        r = self._client.get(
            f"/conversations/{conversation_id}/messages",
            params={"limit": limit},
            headers={"Version": "2021-04-15"},
        )
        if r.status_code != 200:
            raise GHLError(f"list_messages {r.status_code}: {r.text[:300]}")
        data = r.json()
        msgs = data.get("messages")
        if isinstance(msgs, dict):
            return msgs.get("messages", [])
        return msgs or []

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=10),
        retry=retry_if_exception_type((httpx.HTTPError, GHLError)),
    )
    def get_message(self, message_id: str) -> dict[str, Any]:
        r = self._client.get(
            f"/conversations/messages/{message_id}",
            headers={"Version": "2021-04-15"},
        )
        if r.status_code != 200:
            raise GHLError(f"get_message {r.status_code}: {r.text[:300]}")
        data = r.json()
        return data.get("message", data)

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, GHLError)),
    )
    def download_call_recording(self, message_id: str) -> bytes:
        """Fetch the WAV recording bytes for a TYPE_CALL message."""
        r = self._client.get(
            f"/conversations/messages/{message_id}/locations/{self._location_id}/recording",
            headers={"Version": "2021-04-15"},
        )
        if r.status_code != 200:
            raise GHLError(f"download_call_recording {r.status_code}: {r.text[:200]}")
        return r.content

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=10),
        retry=retry_if_exception_type((httpx.HTTPError, GHLError)),
    )
    def get_user(self, user_id: str) -> dict[str, Any] | None:
        r = self._client.get(f"/users/{user_id}")
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise GHLError(f"get_user {r.status_code}: {r.text[:200]}")
        return r.json()

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

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, GHLError)),
    )
    def upload_media(
        self, filename: str, content: bytes, content_type: str = "application/pdf"
    ) -> dict[str, Any]:
        """Upload a file to the location's Media Library. Returns the API object
        (carries the hosted file URL under `url`/`fileUrl` and an id under
        `fileId`/`_id`). Multipart, so the JSON `Accept` header is fine but the
        client's default content-type must not be forced — httpx sets the
        multipart boundary itself when `files=` is passed.
        """
        r = self._client.post(
            "/medias/upload-file",
            files={"file": (filename, content, content_type)},
        )
        if r.status_code not in (200, 201):
            raise GHLError(f"upload_media {r.status_code}: {r.text[:300]}")
        return r.json()

    @staticmethod
    def media_url(uploaded: dict[str, Any]) -> str | None:
        """Best-effort hosted URL from an upload_media() response."""
        return uploaded.get("url") or uploaded.get("fileUrl") or uploaded.get("link")

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, GHLError)),
    )
    def find_custom_field(self, name: str, data_type: str | None = None) -> dict[str, Any] | None:
        """Return the location custom field matching `name` (and `data_type` if
        given), or None. Used to resolve the 'מסמך אפיון' file field without
        hardcoding its id."""
        r = self._client.get(f"/locations/{self._location_id}/customFields")
        if r.status_code != 200:
            raise GHLError(f"custom_fields {r.status_code}: {r.text[:200]}")
        for f in r.json().get("customFields", []):
            if f.get("name") == name and (data_type is None or f.get("dataType") == data_type):
                return f
        return None

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, GHLError)),
    )
    def set_contact_custom_field(self, contact_id: str, field_id: str, value: Any) -> dict[str, Any]:
        """Set one custom field on a contact (PUT /contacts/{id}). `value` shape
        depends on the field's dataType — a bare URL string for FILE_UPLOAD."""
        payload = {"customFields": [{"id": field_id, "field_value": value}]}
        r = self._client.put(f"/contacts/{contact_id}", json=payload)
        if r.status_code not in (200, 201):
            raise GHLError(f"set_custom_field {r.status_code}: {r.text[:300]}")
        return r.json()

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, GHLError)),
    )
    def send_sms(self, contact_id: str, message: str) -> dict[str, Any]:
        """Send an SMS to a contact via the conversations API.

        The conversations endpoints use Version 2021-04-15 (not the default
        2021-07-28), so we override the header per request.
        """
        payload = {"type": "SMS", "contactId": contact_id, "message": message}
        r = self._client.post(
            "/conversations/messages",
            json=payload,
            headers={"Version": "2021-04-15"},
        )
        if r.status_code not in (200, 201):
            raise GHLError(f"send_sms {r.status_code}: {r.text[:300]}")
        return r.json()
