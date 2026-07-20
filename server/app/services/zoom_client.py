"""Zoom API client — lists cloud recordings account-wide and downloads their audio.

Auth: Server-to-Server OAuth. We exchange the account credentials for a short-lived
access token (~1h) and cache it in-process until shortly before it expires.

Scopes needed on the Zoom app: `recording:read:admin` (list/download any user's
cloud recordings) and `user:read:admin` (resolve the host's name for the note).

We deliberately fetch only the **audio-only M4A** file of each meeting, never the
video: it is a fraction of the size, it is all the transcriber needs, and the
worker runs on a 512MB Render instance. Downloads stream straight to disk for the
same reason — a 1GB recording must never be held in memory.
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..config import get_settings


log = logging.getLogger(__name__)

OAUTH_URL = "https://zoom.us/oauth/token"
API_BASE = "https://api.zoom.us/v2"

# Refresh the token this long before it actually expires, so a long-running
# download never dies mid-flight on a 401.
TOKEN_LEEWAY_SEC = 300

# Zoom returns several files per meeting; this is the one we want.
AUDIO_RECORDING_TYPE = "audio_only"


class ZoomError(RuntimeError):
    pass


class ZoomClient:
    def __init__(self) -> None:
        settings = get_settings()
        if not (settings.zoom_account_id and settings.zoom_client_id and settings.zoom_client_secret):
            raise ZoomError("ZOOM_ACCOUNT_ID / ZOOM_CLIENT_ID / ZOOM_CLIENT_SECRET not configured")
        self._account_id = settings.zoom_account_id
        self._auth = (settings.zoom_client_id, settings.zoom_client_secret)
        self._client = httpx.Client(base_url=API_BASE, timeout=60.0)
        self._token: str | None = None
        self._token_expires_at: datetime = datetime.utcnow()

    def close(self) -> None:
        self._client.close()

    def __enter__(self):  # for `with` use
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- auth ---------------------------------------------------------------
    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, ZoomError)),
    )
    def _access_token(self) -> str:
        """Return a valid access token, fetching a new one only when needed."""
        if self._token and datetime.utcnow() < self._token_expires_at:
            return self._token
        r = httpx.post(
            OAUTH_URL,
            params={"grant_type": "account_credentials", "account_id": self._account_id},
            auth=self._auth,
            timeout=30.0,
        )
        if r.status_code != 200:
            raise ZoomError(f"oauth {r.status_code}: {r.text[:300]}")
        data = r.json()
        token = data.get("access_token")
        if not token:
            raise ZoomError(f"oauth response had no access_token: {str(data)[:200]}")
        expires_in = int(data.get("expires_in", 3600))
        self._token = token
        self._token_expires_at = datetime.utcnow() + timedelta(seconds=max(60, expires_in - TOKEN_LEEWAY_SEC))
        return token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token()}", "Accept": "application/json"}

    # --- recordings ---------------------------------------------------------
    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, ZoomError)),
    )
    def _recordings_page(self, frm: str, to: str, page_token: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {"from": frm, "to": to, "page_size": 300}
        if page_token:
            params["next_page_token"] = page_token
        r = self._client.get(
            # "me" — not the literal account id: passing the id makes Zoom read the
            # call as a master account querying a sub-account and it 400s with
            # `does not contain scopes:[...:master]`, even when the scope is granted.
            "/accounts/me/recordings", params=params, headers=self._headers()
        )
        if r.status_code != 200:
            raise ZoomError(f"list_recordings {r.status_code}: {r.text[:300]}")
        return r.json()

    def list_recordings(self, since: datetime, until: datetime | None = None) -> list[dict[str, Any]]:
        """All cloud recordings across the account between `since` and `until`.

        Zoom caps a single query at a one-month window, so callers should keep the
        range short (the poller uses a couple of days).
        """
        until = until or datetime.utcnow()
        frm, to = since.strftime("%Y-%m-%d"), until.strftime("%Y-%m-%d")
        meetings: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            data = self._recordings_page(frm, to, page_token)
            meetings.extend(data.get("meetings", []))
            page_token = data.get("next_page_token") or None
            if not page_token:
                break
        log.info("zoom recordings listed", extra={"from": frm, "to": to, "meetings": len(meetings)})
        return meetings

    @staticmethod
    def audio_file(meeting: dict[str, Any]) -> dict[str, Any] | None:
        """The audio-only file of a meeting, or None if Zoom didn't produce one.

        A meeting with video but no audio_only entry is unusual but real (e.g. the
        recording is still processing), so callers must handle None rather than
        assume it exists.
        """
        for f in meeting.get("recording_files", []):
            if f.get("recording_type") == AUDIO_RECORDING_TYPE and f.get("download_url"):
                return f
        return None

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, ZoomError)),
    )
    def download_to(self, download_url: str, dest: Path) -> int:
        """Stream a recording file to `dest`. Returns bytes written.

        Streaming (not .content) keeps peak memory flat regardless of file size.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with httpx.stream(
            "GET", download_url, headers=self._headers(), follow_redirects=True, timeout=600.0
        ) as r:
            if r.status_code != 200:
                raise ZoomError(f"download {r.status_code}: {r.text[:200]}")
            with dest.open("wb") as fh:
                for chunk in r.iter_bytes(chunk_size=1024 * 1024):
                    fh.write(chunk)
                    written += len(chunk)
        log.info("zoom recording downloaded", extra={"bytes": written, "path": str(dest)})
        return written

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, ZoomError)),
    )
    def past_meeting_participants(self, meeting_uuid: str) -> list[dict[str, Any]]:
        """Attendees of a finished meeting, for matching them to a GHL contact.

        Zoom UUIDs may contain `/` or start with `//`; such a UUID must be
        double-URL-encoded or the path resolves to the wrong resource.
        """
        uuid = meeting_uuid
        if uuid.startswith("/") or "//" in uuid:
            uuid = quote(quote(uuid, safe=""), safe="")
        else:
            uuid = quote(uuid, safe="")
        participants: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": 300}
            if page_token:
                params["next_page_token"] = page_token
            r = self._client.get(
                f"/past_meetings/{uuid}/participants", params=params, headers=self._headers()
            )
            if r.status_code == 404:
                return []
            if r.status_code != 200:
                raise ZoomError(f"participants {r.status_code}: {r.text[:300]}")
            data = r.json()
            participants.extend(data.get("participants", []))
            page_token = data.get("next_page_token") or None
            if not page_token:
                break
        return participants

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, ZoomError)),
    )
    def delete_recording(self, meeting_uuid: str) -> None:
        """Move a meeting's cloud recording to trash (frees Zoom storage).

        Needs `recording:write:admin`, so this only works with the Server-to-Server
        app — the webhook-only download token cannot delete.
        """
        uuid = quote(quote(meeting_uuid, safe=""), safe="") if ("/" in meeting_uuid) else quote(meeting_uuid, safe="")
        r = self._client.delete(f"/meetings/{uuid}/recordings", headers=self._headers())
        if r.status_code not in (200, 204, 404):
            raise ZoomError(f"delete_recording {r.status_code}: {r.text[:300]}")
        log.info("zoom recording deleted", extra={"meeting_uuid": meeting_uuid})

    # --- users --------------------------------------------------------------
    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, ZoomError)),
    )
    def get_user(self, user_id: str) -> dict[str, Any] | None:
        """Host details (for the note's owner line). Best-effort: None on 404."""
        r = self._client.get(f"/users/{user_id}", headers=self._headers())
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise ZoomError(f"get_user {r.status_code}: {r.text[:300]}")
        return r.json()


def download_with_token(download_url: str, download_token: str, dest: Path) -> int:
    """Stream a recording file using the token Zoom ships with the webhook event.

    This is the whole reason a Webhook-Only app is viable: the `download_token` in
    a `recording.completed` payload authorizes fetching that meeting's files, so no
    OAuth app is required. It is short-lived and read-only — it cannot delete.
    """
    if not download_token:
        raise ZoomError("no download_token supplied with the recording event")
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with httpx.stream(
        "GET",
        download_url,
        headers={"Authorization": f"Bearer {download_token}"},
        follow_redirects=True,
        timeout=600.0,
    ) as r:
        if r.status_code != 200:
            raise ZoomError(f"download {r.status_code}: {r.text[:200]}")
        with dest.open("wb") as fh:
            for chunk in r.iter_bytes(chunk_size=1024 * 1024):
                fh.write(chunk)
                written += len(chunk)
    log.info("zoom recording downloaded (webhook token)", extra={"bytes": written, "path": str(dest)})
    return written
