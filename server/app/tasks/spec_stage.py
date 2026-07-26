"""Spec-builder stage — runs after `summarize`, only for discovery meetings.

For a "פגישת אפיון" / "הטמעה ראשונה" meeting it:
  1. generates a structured spec (Claude) and a bot prompt (Claude),
  2. renders the spec to a branded PDF,
  3. uploads the PDF + bot-prompt docs + knowledge bases to the client's Drive folder,
  4. returns a text block (+ PDF link) to append to the GHL note (option ג).

Everything here is best-effort: any failure is logged and swallowed so the core
transcribe→summarize→note pipeline is never broken by spec generation.
Reused verbatim by the manual web trigger.
"""

import logging
import re

from sqlalchemy.orm import Session

from ..models import Job
from ..services import spec_client, spec_render, bot_prompt_render


log = logging.getLogger(__name__)

# Zoom folder names that mark a discovery/onboarding meeting we should build for.
_MEETING_MARKERS = ("פגישת אפיון", "הטמעה ראשונה", "פגישת הטמעה")


def is_spec_meeting(topic: str | None) -> bool:
    if not topic:
        return False
    return any(marker in topic for marker in _MEETING_MARKERS)


def _client_name(job: Job) -> str | None:
    """Best available client name: Claude-extracted first, else cleaned topic."""
    if job.extracted_contact_name:
        return re.sub(r"\s*\([^)]*\)", "", job.extracted_contact_name).strip() or None
    return None


# Name of the location's FILE_UPLOAD custom field the spec PDF is attached to.
# Resolved by name at runtime (see GHLClient.find_custom_field) so no id is hardcoded.
SPEC_FILE_FIELD_NAME = "מסמך אפיון"


def build_and_deliver_to_ghl(
    ghl,
    contact_id: str,
    transcript: str,
    client_name: str | None,
    employee_name: str | None,
    date: str,
) -> dict:
    """Generate the spec for a discovery meeting and deliver it into GHL:
    render a branded PDF, upload it to the GHL Media Library, attach it to the
    contact's file field, and return the spec text to append to the note.

    Best-effort throughout — never raises. The guaranteed deliverable is the spec
    **text** (`note_addition`); the PDF-in-GHL is layered on top and any failure
    there is logged and swallowed so a completed meeting is never lost.
    """
    result: dict = {"ok": False, "note_addition": None, "media_url": None, "error": None}
    if not (transcript or "").strip():
        result["error"] = "no transcript"
        return result

    try:
        spec = spec_client.generate_spec(
            transcript=transcript, client_name=client_name,
            employee_name=employee_name, meeting_date=date,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("spec generation failed", extra={"contact": contact_id})
        result["error"] = f"generation: {exc}"
        return result

    resolved_name = spec.get("client_name") or client_name or "לקוח"
    note_lines = [f"📄 אפיון — {resolved_name} — {date}", "", spec_to_text(spec)]
    result["spec"] = spec

    # --- PDF → GHL media → attach to the contact's file field (all best-effort) ---
    try:
        pdf = spec_render.render_spec_pdf(spec)
        filename = f"אפיון - {resolved_name} - {date}.pdf"
        uploaded = ghl.upload_media(filename, pdf, "application/pdf")
        url = ghl.media_url(uploaded)
        result["media_url"] = url
        if url:
            note_lines += ["", f"📎 מסמך אפיון (PDF): {url}"]
            try:
                field = ghl.find_custom_field(SPEC_FILE_FIELD_NAME, data_type="FILE_UPLOAD")
                if field:
                    ghl.set_contact_custom_field(contact_id, field["id"], url)
                else:
                    log.info("spec file field %r not found — PDF linked in note only",
                             SPEC_FILE_FIELD_NAME, extra={"contact": contact_id})
            except Exception as exc:  # noqa: BLE001 — attaching to the field is a bonus
                log.warning("spec PDF field attach failed (linked in note instead): %s",
                            str(exc)[:200], extra={"contact": contact_id})
    except Exception as exc:  # noqa: BLE001 — PDF is a bonus; the text note still delivers
        log.exception("spec PDF render/upload failed — text note still delivered",
                      extra={"contact": contact_id})
        result["error"] = f"pdf: {exc}"

    result["ok"] = True
    result["note_addition"] = "\n".join(note_lines).strip()
    return result


def _meeting_date(job: Job) -> str:
    return job.meeting_date or job.created_at.strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
#  spec → plain text (for the GHL note, option ג)
# --------------------------------------------------------------------------- #

def spec_to_text(spec: dict) -> str:
    lines: list[str] = []
    lines.append(spec.get("title", "מסמך אפיון לקוח"))
    if spec.get("subtitle"):
        lines.append(spec["subtitle"])
    lines.append("")
    if spec.get("intro"):
        lines.append(spec["intro"])
        lines.append("")

    for section in spec.get("sections", []):
        lines.append(f"{section.get('number', '')}. {section.get('title', '')}".strip())
        for block in section.get("blocks", []):
            if block.get("subhead"):
                lines.append(f"  {block['subhead']}")
            btype = block.get("type")
            if btype == "paragraph":
                lines.append("  " + block.get("text", ""))
            elif btype in ("bullets", "steps"):
                for i, item in enumerate(block.get("items", []), 1):
                    lead = item.get("lead", "")
                    text = item.get("text", "")
                    prefix = f"{i}. " if btype == "steps" else "• "
                    lines.append(f"  {prefix}{(lead + ': ') if lead else ''}{text}".rstrip())
            elif btype == "callout":
                lines.append("  « " + block.get("text", "") + " »")
            elif btype == "pills":
                vals = [i if isinstance(i, str) else (i.get("label") or i.get("text") or "")
                        for i in block.get("items", [])]
                lines.append("  [ " + " | ".join(v for v in vals if v) + " ]")
            elif btype == "table":
                headers = block.get("headers", [])
                if headers:
                    lines.append("  " + " | ".join(headers))
                for row in block.get("rows", []):
                    cells = [c.get("text", "") if isinstance(c, dict) else str(c) for c in row]
                    lines.append("  " + " | ".join(cells))
            elif btype == "cards":
                for c in block.get("items", []):
                    seg = " ".join(filter(None, [c.get("tag"), c.get("title"), c.get("value")]))
                    if c.get("note"):
                        seg += f" ({c['note']})"
                    lines.append("  - " + seg)
        lines.append("")

    if spec.get("footer_note"):
        lines.append(spec["footer_note"])
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------- #
#  main entry
# --------------------------------------------------------------------------- #

def build_for_job(db: Session, job: Job, upload: bool = True) -> dict:
    """Generate spec + bot prompt for a job. Uploads to Drive when `upload` and
    Google is configured. Returns a result dict incl. `note_addition` for the GHL note.

    Never raises — returns {"ok": False, "error": ...} on failure.
    """
    result: dict = {"ok": False, "note_addition": None, "drive": None, "error": None}
    if not job.transcript:
        result["error"] = "no transcript"
        return result

    client_name = _client_name(job)
    date = _meeting_date(job)

    try:
        spec = spec_client.generate_spec(
            transcript=job.transcript,
            client_name=client_name,
            employee_name=job.employee_name,
            meeting_date=date,
        )
        bot = spec_client.generate_bot_prompt(job.transcript, client_name=client_name)
    except Exception as exc:
        log.exception("spec/bot generation failed", extra={"job_id": job.id})
        result["error"] = f"generation: {exc}"
        return result

    resolved_name = spec.get("client_name") or client_name or "לקוח"
    note_lines = [f"📄 אפיון — {resolved_name} — {date}", "", spec_to_text(spec)]

    # PDF + Drive upload (best-effort; note still gets the text if these fail)
    spec_pdf = None
    try:
        spec_pdf = spec_render.render_spec_pdf(spec)
    except Exception:
        log.exception("spec pdf render failed", extra={"job_id": job.id})

    if upload:
        try:
            from ..services import gdrive_client
            main_prompt = bot_prompt_render.format_main_prompt(bot)
            kb_files = bot_prompt_render.knowledge_base_files(bot)
            drive = gdrive_client.upload_client_bundle(
                client_name=resolved_name,
                date=date,
                spec_pdf=spec_pdf,
                main_prompt_text=main_prompt,
                knowledge_bases=kb_files,
            )
            result["drive"] = drive
            if drive.get("spec_pdf", {}).get("link"):
                note_lines += ["", f"📎 מסמך אפיון מעוצב (PDF): {drive['spec_pdf']['link']}"]
            if drive.get("bot_prompt", {}).get("link"):
                note_lines += [f"🤖 פרומפט הבוט (דרייב): {drive['bot_prompt']['link']}"]
        except Exception as exc:
            log.exception("drive upload failed", extra={"job_id": job.id})
            result["error"] = f"drive: {exc}"  # non-fatal; note text still returned

    result["ok"] = True
    result["spec"] = spec
    result["bot"] = bot
    result["note_addition"] = "\n".join(note_lines).strip()
    return result
