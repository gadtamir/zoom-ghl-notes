#!/usr/bin/env python3
"""Standalone re-processor for GHL phone calls that never got transcribed.

The production pipeline (Render + Postgres) marks a failed call's CallJob row
and then DEDUPES on ghl_message_id, so calls that failed while the OpenAI
account was out of quota never get retried automatically. This script bypasses
the production DB entirely: it scans GHL directly over urllib, finds call
messages since a cutoff date, skips any that already carry a "☎️ סיכום שיחה"
note, and runs download → transcribe → summarize → create-note for the rest.

Dependency-free (stdlib urllib only) for the same reason as backfill_meetings:
the project's openai/anthropic/httpx SDKs hang on import on this machine.

Usage (from server/):
    ./.venv/bin/python scripts/backfill_calls.py --since 2026-05-31 --list-only
    ./.venv/bin/python scripts/backfill_calls.py --since 2026-05-31 --workers 2
"""

import argparse
import json
import logging
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# --- paths / config ----------------------------------------------------------
# Work dir (state + logs + optional .env) lives OUTSIDE the repo so it isn't
# touched by git or iCloud sync. Override with ZGHL_WORK_DIR; defaults to
# ~/zghl_backfill and is created automatically — so this runs on any machine.
SERVER_DIR = Path(__file__).resolve().parent.parent
WORK_DIR = Path(os.environ.get("ZGHL_WORK_DIR") or (Path.home() / "zghl_backfill"))
WORK_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = WORK_DIR / "calls_state.json"
LOG_PATH = WORK_DIR / "calls_backfill.log"

MIN_DURATION_SEC = 30
CALL_TYPES = {"TYPE_CALL", "TYPE_CAMPAIGN_CALL"}
TRANSCRIBE_MODEL = "gpt-4o-transcribe"
CHUNK_MINUTES = 10
MAX_SINGLE_MINUTES = 23
SAFE_CHUNK_MB = 20
BYTES_PER_MB = 1024 * 1024
NOTE_MARKER = "☎️ סיכום שיחה"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_PATH, encoding="utf-8")],
)
log = logging.getLogger("calls")


def load_env() -> dict:
    env_file = WORK_DIR / ".env"
    if not env_file.exists():
        env_file = SERVER_DIR / ".env"
    env = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


ENV = load_env()
OPENAI_API_KEY = ENV["OPENAI_API_KEY"]
ANTHROPIC_API_KEY = ENV["ANTHROPIC_API_KEY"]
ANTHROPIC_MODEL = ENV.get("ANTHROPIC_MODEL", "claude-opus-4-5")
GHL_BASE = ENV["GHL_API_BASE"]
GHL_LOC = ENV["GHL_LOCATION_ID"]
GHL_TOK = ENV["GHL_PRIVATE_TOKEN"]
GHL_VER = ENV.get("GHL_API_VERSION", "2021-07-28")
# A browser-ish UA — GHL's Cloudflare blocks the default python-urllib UA.
UA = "Mozilla/5.0 zghl-calls"

TALLY = {"audio_minutes": 0.0, "in_tokens": 0, "out_tokens": 0}


# --- GHL (urllib) ------------------------------------------------------------
def ghl_get_json(path: str, params: dict | None = None, version: str = GHL_VER):
    url = GHL_BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {GHL_TOK}", "Version": version,
        "Accept": "application/json", "User-Agent": UA,
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2 * (2 ** attempt)); continue
            try:
                return e.code, json.loads(e.read().decode())
            except Exception:
                return e.code, "(non-json error)"
        except Exception as e:  # noqa: BLE001
            if attempt < 2:
                time.sleep(2 * (2 ** attempt)); continue
            raise


def ghl_download_recording(message_id: str) -> bytes | None:
    """Return WAV bytes, or None if there's no recording (non-200)."""
    url = f"{GHL_BASE}/conversations/messages/{message_id}/locations/{GHL_LOC}/recording"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {GHL_TOK}", "Version": "2021-04-15", "User-Agent": UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read() if r.status == 200 else None
    except urllib.error.HTTPError:
        return None


def ghl_get_message(message_id: str) -> dict | None:
    st, d = ghl_get_json(f"/conversations/messages/{message_id}", version="2021-04-15")
    if st == 200 and isinstance(d, dict):
        return d.get("message", d)
    return None


def ghl_get_user_name(user_id: str | None) -> str | None:
    if not user_id:
        return None
    st, d = ghl_get_json(f"/users/{user_id}")
    if st == 200 and isinstance(d, dict):
        return d.get("name") or " ".join(filter(None, [d.get("firstName"), d.get("lastName")])) or None
    return None


def ghl_contact_notes(contact_id: str) -> list[dict]:
    st, d = ghl_get_json(f"/contacts/{contact_id}/notes", version="2021-07-28")
    return d.get("notes", []) if (st == 200 and isinstance(d, dict)) else []


def ghl_create_note(contact_id: str, body: str) -> str | None:
    url = f"{GHL_BASE}/contacts/{contact_id}/notes"
    data = json.dumps({"body": body}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"Bearer {GHL_TOK}", "Version": "2021-07-28",
        "Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA,
    })
    with urllib.request.urlopen(req, timeout=40) as r:
        d = json.load(r)
        return (d.get("note") or d).get("id")


# --- OpenAI / Anthropic (urllib) ---------------------------------------------
def _http(req, timeout, retries=4):
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (2 ** attempt))
    raise RuntimeError(f"http failed after {retries} tries: {last}")


def openai_transcribe(path: Path) -> str:
    boundary = uuid.uuid4().hex
    ctype = mimetypes.guess_type(path.name)[0] or "audio/wav"
    parts = []

    def field(n, v):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{n}\"\r\n\r\n{v}\r\n".encode())

    field("model", TRANSCRIBE_MODEL)
    field("language", "he")
    field("response_format", "text")
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
        f"Content-Type: {ctype}\r\n\r\n".encode()
    )
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions", data=b"".join(parts), method="POST",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    return _http(req, timeout=600).decode("utf-8").strip()


def probe_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def transcribe(path: Path, duration_s: float) -> str:
    small = path.stat().st_size <= SAFE_CHUNK_MB * BYTES_PER_MB
    short = duration_s <= MAX_SINGLE_MINUTES * 60
    if small and short:
        return openai_transcribe(path)
    with tempfile.TemporaryDirectory(prefix="zghl-call-") as tmp:
        out = Path(tmp) / "chunk_%03d.m4a"
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-vn",
               "-c:a", "aac", "-b:a", "96k", "-ac", "1", "-ar", "16000",
               "-f", "segment", "-segment_time", str(CHUNK_MINUTES * 60),
               "-reset_timestamps", "1", str(out)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {res.stderr[:300]}")
        chunks = sorted(Path(tmp).glob("chunk_*.m4a"))
        return "\n\n".join(p for p in (openai_transcribe(c) for c in chunks) if p)


PHONE_PROMPT = """אתה עוזר עסקי של גד תמיר, יזם ישראלי שמנהל את More-Than (CRM + אוטומציות).
לפניך תמלול של שיחת טלפון בין עובדת/עובד של More-Than לבין לקוח/ה.

המשימה שלך: לתת סיכום קצר, ענייני, **בעברית בלבד**, בפורמט המדויק הבא:

סיכום
[2-4 שורות שמסכמות את עיקרי השיחה — מה היה הנושא, מה היה הקונטקסט, מה הוחלט]

נושא השיחה
[משפט אחד מתמצת — תמיכה / מכירה / אונבורדינג / חידוש / תלונה / אחר]

משימות
- [מה לעשות] - אחראי: [שם או "לא צוין"] - דדליין: [תאריך או "לא צוין"]
- ...

נקודות לעיבוד פנימי
- [תובנה חשובה 1]
- [תובנה חשובה 2]

כללים:
- עברית בלבד
- ענייני וקצר — שיחות טלפון בד"כ קצרות מפגישות, גם הסיכום צריך להיות קצר
- אם אין משימות ברורות — כתוב "אין משימות שזוהו"
- אל תמציא מידע שלא נאמר בשיחה
- אל תוסיף כותרת/הקדמה לפני "סיכום"
"""


def summarize_call(transcript: str, employee_name: str, duration_s: int) -> str:
    user = (f"שם העובדת/העובד: {employee_name}\n"
            f"משך השיחה: {duration_s // 60} דקות ו-{duration_s % 60} שניות\n\n"
            f"תמלול השיחה:\n\n{transcript}")
    body = json.dumps({
        "model": ANTHROPIC_MODEL, "max_tokens": 1500, "temperature": 0.3,
        "system": [{"type": "text", "text": PHONE_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
    )
    d = json.loads(_http(req, timeout=180))
    u = d.get("usage", {})
    TALLY["in_tokens"] += u.get("input_tokens", 0)
    TALLY["out_tokens"] += u.get("output_tokens", 0)
    return "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text").strip()


# --- helpers -----------------------------------------------------------------
def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def format_note(direction: str | None, started: datetime | None, duration_s: int,
                owner: str | None, summary: str) -> str:
    when = started.strftime("%Y-%m-%d %H:%M") if started else "?"
    dur = f"{duration_s // 60} דק' {duration_s % 60} שניות"
    dir_he = {"inbound": "נכנסת", "outbound": "יוצאת"}.get(direction or "", direction or "")
    title = f"{NOTE_MARKER} {dir_he} - {when} ({dur}) - {owner or '—'}"
    return f"{title}\n\n{summary or '(אין סיכום זמין)'}"


def already_noted(contact_id: str, started: datetime | None) -> bool:
    """True if a call-summary note for this call's timestamp already exists."""
    if started is None:
        return False
    stamp = started.strftime("%Y-%m-%d %H:%M")
    for n in ghl_contact_notes(contact_id):
        body = n.get("body") or ""
        if NOTE_MARKER in body and stamp in body:
            return True
    return False


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"done": [], "skipped": [], "failed": []}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# --- discovery ---------------------------------------------------------------
def discover_calls(since: datetime, max_pages: int = 200) -> list[dict]:
    """All call messages (TYPE_CALL/TYPE_CAMPAIGN_CALL) added on/after `since`."""
    since_ms = int(since.timestamp() * 1000)
    calls: list[dict] = []
    seen_msg: set[str] = set()
    start_after = None
    pages = 0
    convs_scanned = 0
    while pages < max_pages:
        pages += 1
        params = {"locationId": GHL_LOC, "limit": 25}
        if start_after is not None:
            params["startAfterDate"] = start_after
        st, d = ghl_get_json("/conversations/search", params, version="2021-04-15")
        convs = d.get("conversations", []) if isinstance(d, dict) else []
        if not convs:
            break
        last_date = None
        for c in convs:
            convs_scanned += 1
            last_date = c.get("lastMessageDate") or c.get("dateUpdated") or last_date
            st2, d2 = ghl_get_json(f"/conversations/{c['id']}/messages", {"limit": 100}, version="2021-04-15")
            msgs = d2.get("messages") if isinstance(d2, dict) else None
            if isinstance(msgs, dict):
                msgs = msgs.get("messages", [])
            for m in (msgs or []):
                if m.get("messageType") not in CALL_TYPES:
                    continue
                added = parse_iso(m.get("dateAdded"))
                if added and added < since:
                    continue
                mid = m.get("id")
                if not mid or mid in seen_msg:
                    continue
                seen_msg.add(mid)
                calls.append({
                    "message_id": mid,
                    "contact_id": m.get("contactId") or c.get("contactId") or "",
                    "user_id": m.get("userId"),
                    "direction": m.get("direction"),
                    "duration": (m.get("meta") or {}).get("call", {}).get("duration"),
                    "date": m.get("dateAdded"),
                    "started": added,
                })
        log.info("  scan page %d: %d convs, %d calls so far (last_date<since=%s)",
                 pages, convs_scanned, len(calls), bool(last_date and last_date < since_ms))
        if last_date is None or last_date < since_ms:
            break
        start_after = last_date
    return calls


# --- worker ------------------------------------------------------------------
def process_call(call: dict, idx: int, total: int, state: dict, lock) -> str:
    mid = call["message_id"]
    short = (call.get("contact_id") or "?")[:10]
    log.info("[%d/%d] start call %s (contact %s, %s)", idx, total, mid, short, call.get("date"))
    try:
        dur = call.get("duration")
        if dur is not None and dur < MIN_DURATION_SEC:
            with lock:
                state["skipped"].append(mid); save_state(state)
            log.info("[%d/%d] ⏭ skip — %ss < %ss", idx, total, dur, MIN_DURATION_SEC)
            return "skipped"
        if call["contact_id"] and already_noted(call["contact_id"], call["started"]):
            with lock:
                state["skipped"].append(mid); save_state(state)
            log.info("[%d/%d] ⏭ skip — summary note already exists", idx, total)
            return "skipped"

        audio = ghl_download_recording(mid)
        if not audio:
            with lock:
                state["skipped"].append(mid); save_state(state)
            log.info("[%d/%d] ⏭ skip — no recording", idx, total)
            return "skipped"

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tf.write(audio); wav = Path(tf.name)
        try:
            dur_s = probe_duration(wav)
            transcript = transcribe(wav, dur_s)
        finally:
            wav.unlink(missing_ok=True)
        if not transcript.strip():
            raise RuntimeError("empty transcript")

        owner = ghl_get_user_name(call.get("user_id"))
        duration_int = int(call.get("duration") or round(dur_s))
        summary = summarize_call(transcript, owner or "(לא ידוע)", duration_int)
        body = format_note(call.get("direction"), call["started"], duration_int, owner, summary)
        if not call["contact_id"]:
            raise RuntimeError("no contact_id — cannot attach note")
        note_id = ghl_create_note(call["contact_id"], body)
        with lock:
            TALLY["audio_minutes"] += dur_s / 60.0
            state["done"].append(mid); save_state(state)
        log.info("[%d/%d] ✓ noted %s | %s | owner %s | %dmin",
                 idx, total, note_id, short, owner or "—", max(1, round(dur_s / 60)))
        return "done"
    except Exception as e:  # noqa: BLE001
        log.error("[%d/%d] ✗ FAILED %s: %s", idx, total, mid, str(e)[:200])
        with lock:
            if mid not in state["failed"]:
                state["failed"].append(mid); save_state(state)
        return "failed"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-05-31", help="earliest call date YYYY-MM-DD")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--list-only", action="store_true", help="discover + report, don't process")
    ap.add_argument("--report-missing", action="store_true", help="discover + list calls lacking a summary note (read-only, no transcription)")
    ap.add_argument("--message-id", help="process a single GHL call message id and exit")
    ap.add_argument("--ids-file", help="process a newline-delimited file of GHL call message ids (skips slow discovery)")
    ap.add_argument("--model", help="override the Anthropic summary model (e.g. claude-haiku-4-5-20251001)")
    args = ap.parse_args()

    if args.model:
        global ANTHROPIC_MODEL
        ANTHROPIC_MODEL = args.model
        log.info("summary model overridden → %s", ANTHROPIC_MODEL)

    state = load_state()
    lock = threading.Lock()

    if args.message_id:
        m = ghl_get_message(args.message_id)
        if not m:
            log.error("message %s not found", args.message_id)
            return
        call = {
            "message_id": args.message_id,
            "contact_id": m.get("contactId") or "",
            "user_id": m.get("userId"),
            "direction": m.get("direction"),
            "duration": (m.get("meta") or {}).get("call", {}).get("duration"),
            "date": m.get("dateAdded"),
            "started": parse_iso(m.get("dateAdded")),
        }
        res = process_call(call, 1, 1, state, lock)
        log.info("single-call result: %s", res)
        return

    if args.ids_file:
        ids = [x.strip() for x in Path(args.ids_file).read_text().splitlines() if x.strip()]
        ids = [i for i in ids if i not in set(state["done"]) | set(state["skipped"])]
        log.info("processing %d call ids from %s (workers=%d)", len(ids), args.ids_file, args.workers)

        def fetch_and_process(mid: str, idx: int) -> str:
            m = ghl_get_message(mid)
            if not m:
                log.error("[%d/%d] message %s not found", idx, len(ids), mid)
                return "failed"
            call = {
                "message_id": mid, "contact_id": m.get("contactId") or "",
                "user_id": m.get("userId"), "direction": m.get("direction"),
                "duration": (m.get("meta") or {}).get("call", {}).get("duration"),
                "date": m.get("dateAdded"), "started": parse_iso(m.get("dateAdded")),
            }
            return process_call(call, idx, len(ids), state, lock)

        results = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = [pool.submit(fetch_and_process, mid, i) for i, mid in enumerate(ids, 1)]
            for f in as_completed(futs):
                results.append(f.result())
        log.info("=" * 60)
        log.info("DONE. noted=%d skipped=%d failed=%d (of %d)",
                 results.count("done"), results.count("skipped"), results.count("failed"), len(ids))
        log.info("audio minutes: %.1f → est OpenAI ~$%.2f", TALLY["audio_minutes"], TALLY["audio_minutes"] * 0.006)
        log.info("Claude tokens: in=%d out=%d", TALLY["in_tokens"], TALLY["out_tokens"])
        return

    since = datetime.fromisoformat(args.since)
    log.info("discovering calls since %s …", since.isoformat())
    calls = discover_calls(since)
    log.info("found %d call messages since %s", len(calls), args.since)

    if args.report_missing:
        missing = []
        for c in calls:
            dur = c.get("duration")
            if dur is not None and dur < MIN_DURATION_SEC:
                continue  # too short — intentionally not transcribed
            if c["contact_id"] and already_noted(c["contact_id"], c["started"]):
                continue  # already has a summary note
            missing.append(c)
        log.info("calls WITHOUT a summary note (>=%ss): %d", MIN_DURATION_SEC, len(missing))
        for c in sorted(missing, key=lambda x: x.get("date") or ""):
            log.info("  MISSING %s | %s | dur=%s | %s | contact=%s",
                     c.get("date"), c.get("direction"), c.get("duration"),
                     c["message_id"], (c["contact_id"] or "?"))
        return

    state = load_state()
    done_set = set(state["done"]) | set(state["skipped"])
    pending = [c for c in calls if c["message_id"] not in done_set]
    log.info("pending (not already processed): %d", len(pending))

    # quick breakdown
    short_calls = sum(1 for c in pending if c.get("duration") is not None and c["duration"] < MIN_DURATION_SEC)
    log.info("  of which <%ss (will skip): %d", MIN_DURATION_SEC, short_calls)

    if args.limit:
        pending = pending[: args.limit]
    if args.list_only:
        for c in pending:
            log.info("  CALL %s | contact %s | dur %s | %s | %s",
                     c["message_id"], (c["contact_id"] or "?")[:10], c.get("duration"),
                     c.get("direction"), c.get("date"))
        return

    total = len(pending)
    lock = threading.Lock()
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(process_call, c, i, total, state, lock) for i, c in enumerate(pending, 1)]
        for f in as_completed(futs):
            results.append(f.result())

    done = results.count("done")
    skipped = results.count("skipped")
    failed = results.count("failed")
    log.info("=" * 60)
    log.info("DONE. noted=%d skipped=%d failed=%d (of %d)", done, skipped, failed, total)
    log.info("audio minutes: %.1f → est OpenAI ~$%.2f", TALLY["audio_minutes"], TALLY["audio_minutes"] * 0.006)
    log.info("Claude tokens: in=%d out=%d", TALLY["in_tokens"], TALLY["out_tokens"])


if __name__ == "__main__":
    main()
