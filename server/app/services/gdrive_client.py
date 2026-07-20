"""Google Drive client — uploads the spec PDF + bot-prompt docs to Oranit's Drive.

Auth: a long-lived OAuth **refresh token** (created once, see docs/GOOGLE_DRIVE_SETUP.md).
Env vars:
    GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET
    GOOGLE_REFRESH_TOKEN
    GDRIVE_PARENT_FOLDER_ID   — the folder in Oranit's Drive under which per-client
                                folders are created.

Layout created per client:
    <parent>/<client>/<client> - פגישת אפיון - <date>.pdf   (branded spec)
    <parent>/<client>/<client> - בוט - <date>               (Google Doc, main prompt)
    <parent>/<client>/בסיסי ידע/<kb name>                    (Google Doc per KB)
"""

import io
import logging

from ..config import get_settings


log = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/drive"]
_FOLDER_MIME = "application/vnd.google-apps.folder"
_GDOC_MIME = "application/vnd.google-apps.document"


class GDriveError(RuntimeError):
    pass


def _service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    s = get_settings()
    if not (s.google_client_id and s.google_client_secret and s.google_refresh_token):
        raise GDriveError("Google Drive credentials not configured")
    creds = Credentials(
        token=None,
        refresh_token=s.google_refresh_token,
        client_id=s.google_client_id,
        client_secret=s.google_client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=_SCOPES,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _escape(name: str) -> str:
    return name.replace("\\", "\\\\").replace("'", "\\'")


def find_or_create_folder(svc, name: str, parent_id: str) -> str:
    """Return the id of the child folder `name` under `parent_id`, creating it if needed."""
    q = (
        f"name = '{_escape(name)}' and mimeType = '{_FOLDER_MIME}' "
        f"and '{parent_id}' in parents and trashed = false"
    )
    resp = svc.files().list(q=q, fields="files(id,name)", pageSize=1,
                            supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    files = resp.get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": name, "mimeType": _FOLDER_MIME, "parents": [parent_id]}
    folder = svc.files().create(body=meta, fields="id", supportsAllDrives=True).execute()
    return folder["id"]


def _upload(svc, name: str, data: bytes, mime: str, parent_id: str,
            convert_to_gdoc_mime: str | None = None) -> dict:
    from googleapiclient.http import MediaIoBaseUpload

    body = {"name": name, "parents": [parent_id]}
    if convert_to_gdoc_mime:
        body["mimeType"] = convert_to_gdoc_mime  # ask Drive to convert on import
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)
    f = svc.files().create(
        body=body, media_body=media, fields="id,name,webViewLink", supportsAllDrives=True
    ).execute()
    return f


def upload_pdf(svc, name: str, pdf_bytes: bytes, parent_id: str) -> dict:
    return _upload(svc, name, pdf_bytes, "application/pdf", parent_id)


def upload_gdoc(svc, name: str, text: str, parent_id: str) -> dict:
    """Upload plain text as a native Google Doc (editable)."""
    return _upload(
        svc, name, text.encode("utf-8"), "text/plain", parent_id,
        convert_to_gdoc_mime=_GDOC_MIME,
    )


def upload_client_bundle(
    client_name: str,
    date: str,
    spec_pdf: bytes | None,
    main_prompt_text: str | None,
    knowledge_bases: list[dict] | None,
) -> dict:
    """Create/reuse the client folder and upload all artifacts. Returns links dict."""
    s = get_settings()
    if not s.gdrive_parent_folder_id:
        raise GDriveError("GDRIVE_PARENT_FOLDER_ID not configured")

    svc = _service()
    client_folder = find_or_create_folder(svc, client_name, s.gdrive_parent_folder_id)
    result: dict = {"client_folder_id": client_folder, "spec_pdf": None, "bot_prompt": None, "kb": []}

    if spec_pdf:
        f = upload_pdf(svc, f"{client_name} - פגישת אפיון - {date}.pdf", spec_pdf, client_folder)
        result["spec_pdf"] = {"id": f["id"], "link": f.get("webViewLink")}
        log.info("spec pdf uploaded", extra={"client": client_name, "id": f["id"]})

    if main_prompt_text:
        f = upload_gdoc(svc, f"{client_name} - בוט - {date}", main_prompt_text, client_folder)
        result["bot_prompt"] = {"id": f["id"], "link": f.get("webViewLink")}
        log.info("bot prompt uploaded", extra={"client": client_name, "id": f["id"]})

    if knowledge_bases:
        kb_folder = find_or_create_folder(svc, "בסיסי ידע", client_folder)
        for kb in knowledge_bases:
            f = upload_gdoc(svc, kb["name"], kb["content"], kb_folder)
            result["kb"].append({"name": kb["name"], "id": f["id"], "link": f.get("webViewLink")})
        log.info("knowledge bases uploaded", extra={"client": client_name, "count": len(knowledge_bases)})

    return result
