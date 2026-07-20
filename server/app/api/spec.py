"""Manual spec-builder trigger — a tiny internal web page + JSON endpoints.

  GET  /spec/ui?token=...                 → the operator page (recent meetings + buttons)
  GET  /spec/api/jobs?token=...           → recent jobs as JSON
  POST /spec/api/generate/{job_id}?token= → generate spec+prompt for one job, post GHL note

Protected by a shared secret (`SPEC_UI_TOKEN`). Intended for internal use only.
Generation runs inline (2 Claude calls + PDF + Drive upload → up to ~1 minute).
"""

import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from ..config import get_settings
from ..db import SessionLocal
from ..models import Job
from ..tasks.ghl import attach_note
from ..tasks.spec_stage import build_for_job, is_spec_meeting


log = logging.getLogger(__name__)
router = APIRouter(prefix="/spec", tags=["spec"])


def _check_token(token: str | None) -> None:
    expected = get_settings().spec_ui_token
    if not expected:
        raise HTTPException(status_code=503, detail="SPEC_UI_TOKEN not configured")
    if token != expected:
        raise HTTPException(status_code=401, detail="bad token")


@router.get("/api/jobs")
def recent_jobs(token: str | None = Query(default=None), limit: int = 40) -> JSONResponse:
    _check_token(token)
    db = SessionLocal()
    try:
        jobs = db.query(Job).order_by(Job.created_at.desc()).limit(limit).all()
        out = [
            {
                "id": j.id,
                "topic": j.meeting_topic,
                "client": j.extracted_contact_name,
                "date": j.meeting_date or j.created_at.strftime("%Y-%m-%d"),
                "status": j.status.value,
                "is_spec_meeting": is_spec_meeting(j.meeting_topic),
                "has_transcript": bool(j.transcript),
            }
            for j in jobs
        ]
        return JSONResponse(out)
    finally:
        db.close()


@router.post("/api/generate/{job_id}")
def generate(job_id: str, token: str | None = Query(default=None)) -> JSONResponse:
    _check_token(token)
    if not get_settings().spec_builder_enabled:
        raise HTTPException(status_code=503, detail="spec-builder disabled (SPEC_BUILDER_ENABLED=false)")
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        if not job.transcript:
            raise HTTPException(status_code=400, detail="job has no transcript yet")

        result = build_for_job(db, job, upload=True)
        note_posted = False
        if result.get("note_addition"):
            try:
                attach_note(db, job, extra_note=result["note_addition"])
                note_posted = True
            except Exception as exc:  # noqa: BLE001
                log.exception("manual attach_note failed", extra={"job_id": job_id})
                result.setdefault("error", f"note: {exc}")

        return JSONResponse(
            {
                "ok": result.get("ok", False),
                "error": result.get("error"),
                "note_posted": note_posted,
                "drive": result.get("drive"),
            }
        )
    finally:
        db.close()


@router.get("/ui", response_class=HTMLResponse)
def ui(token: str | None = Query(default=None)) -> HTMLResponse:
    _check_token(token)
    return HTMLResponse(_PAGE.replace("__TOKEN__", token or ""))


_PAGE = """<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>More-Than · יצירת אפיון ופרומפט</title>
<style>
  body { font-family: 'Segoe UI', Arial, sans-serif; background:#f4f6fa; color:#24303f; margin:0; padding:24px; }
  h1 { color:#1e2a44; font-size:22px; }
  .sub { color:#7a8494; margin-bottom:18px; font-size:14px; }
  table { width:100%; border-collapse:collapse; background:#fff; border-radius:12px; overflow:hidden; box-shadow:0 1px 4px rgba(0,0,0,.06); }
  th, td { padding:11px 14px; text-align:right; border-bottom:1px solid #eef1f5; font-size:14px; }
  th { background:#1e2a44; color:#fff; font-weight:600; }
  tr:last-child td { border-bottom:none; }
  .spec { background:#eaf0fd; }
  .badge { font-size:11px; padding:2px 9px; border-radius:999px; background:#eef1f5; color:#7a8494; }
  .badge.on { background:#2f5fd6; color:#fff; }
  button { background:#2f5fd6; color:#fff; border:none; border-radius:8px; padding:8px 14px; font-size:13px; cursor:pointer; }
  button:disabled { background:#a9b6d6; cursor:default; }
  .msg { font-size:12px; margin-top:4px; }
  .ok { color:#1e8e4e; } .err { color:#c0392b; }
  a { color:#2f5fd6; }
</style>
</head>
<body>
  <h1>יצירת אפיון ופרומפט — More-Than</h1>
  <div class="sub">פגישות אפיון / הטמעה ראשונה מסומנות. לחצו "צור מסמכים" כדי לייצר אפיון (הערה ב-GHL) ופרומפט (Drive).</div>
  <table id="tbl">
    <thead><tr><th>פגישה</th><th>לקוח</th><th>תאריך</th><th>סטטוס</th><th>סוג</th><th></th></tr></thead>
    <tbody id="rows"><tr><td colspan="6">טוען…</td></tr></tbody>
  </table>

<script>
const TOKEN = "__TOKEN__";
async function load() {
  const r = await fetch(`/spec/api/jobs?token=${encodeURIComponent(TOKEN)}`);
  const jobs = await r.json();
  const rows = document.getElementById('rows');
  rows.innerHTML = '';
  for (const j of jobs) {
    const tr = document.createElement('tr');
    if (j.is_spec_meeting) tr.className = 'spec';
    tr.innerHTML = `
      <td>${escapeHtml(j.topic || '')}</td>
      <td>${escapeHtml(j.client || '')}</td>
      <td>${j.date}</td>
      <td>${j.status}</td>
      <td>${j.is_spec_meeting ? '<span class="badge on">אפיון/הטמעה</span>' : '<span class="badge">אחר</span>'}</td>
      <td></td>`;
    const cell = tr.lastElementChild;
    const btn = document.createElement('button');
    btn.textContent = 'צור מסמכים';
    btn.disabled = !j.has_transcript;
    if (!j.has_transcript) btn.title = 'אין עדיין תמלול';
    btn.onclick = () => generate(j.id, btn, cell);
    cell.appendChild(btn);
    rows.appendChild(tr);
  }
}
async function generate(id, btn, cell) {
  btn.disabled = true; btn.textContent = 'מייצר…';
  const msg = document.createElement('div'); msg.className = 'msg'; cell.appendChild(msg);
  try {
    const r = await fetch(`/spec/api/generate/${id}?token=${encodeURIComponent(TOKEN)}`, {method:'POST'});
    const d = await r.json();
    if (d.ok) {
      let links = '';
      if (d.drive && d.drive.spec_pdf) links += ` · <a href="${d.drive.spec_pdf.link}" target="_blank">אפיון PDF</a>`;
      if (d.drive && d.drive.bot_prompt) links += ` · <a href="${d.drive.bot_prompt.link}" target="_blank">פרומפט</a>`;
      msg.className = 'msg ok';
      msg.innerHTML = `נוצר${d.note_posted ? ' · הערה נכתבה ב-GHL' : ''}${links}`;
      btn.textContent = 'בוצע';
    } else {
      msg.className = 'msg err'; msg.textContent = 'שגיאה: ' + (d.error || 'לא ידוע');
      btn.disabled = false; btn.textContent = 'נסה שוב';
    }
  } catch (e) {
    msg.className = 'msg err'; msg.textContent = 'שגיאת רשת: ' + e;
    btn.disabled = false; btn.textContent = 'נסה שוב';
  }
}
function escapeHtml(s){return s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
load();
</script>
</body>
</html>"""
