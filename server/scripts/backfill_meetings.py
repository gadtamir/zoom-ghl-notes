#!/usr/bin/env python3
"""Local backfill: transcribe + summarize + classify Zoom meeting folders into the
gad_meetings_*.md knowledge bases.

Standalone and dependency-free: talks to the OpenAI and Anthropic REST APIs over
stdlib urllib only. The project venv hangs on importing the openai/anthropic SDKs
(pydantic_core's Rust extension wedges on this macOS box), so we skip the SDKs.

Same pipeline shape as the server: gpt-4o-transcribe with ffmpeg chunking for
files over the OpenAI size limit, then a single Claude call that returns
classification + contact name + Hebrew summary.

Idempotent and resumable: a state file tracks finished folders, and the existing
docs are parsed on startup so meetings already present are never re-done.

Usage (run from the server/ dir so .env is found):
    ./.venv/bin/python scripts/backfill_meetings.py --limit 10        # test batch
    ./.venv/bin/python scripts/backfill_meetings.py                   # all remaining
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
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# --- paths -------------------------------------------------------------------
# Output docs + state live in a NON-iCloud working dir. The repo's docs/ folder
# is under ~/Desktop, which iCloud syncs and evicts to dataless placeholders
# mid-run — reads then wedge. We write here and copy back to the repo at the end.
SERVER_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = SERVER_DIR.parent
WORK_DIR = Path(os.environ.get("ZGHL_WORK_DIR") or (Path.home() / "zghl_backfill"))
(WORK_DIR / "docs").mkdir(parents=True, exist_ok=True)
DOCS_DIR = WORK_DIR / "docs"
STATE_PATH = WORK_DIR / "backfill_state.json"
LOG_PATH = WORK_DIR / "backfill.log"

# Gad's local Zoom recordings folder; override with ZGHL_MEETINGS_DIR or --dir.
MEETINGS_DIR = Path(os.environ.get("ZGHL_MEETINGS_DIR")
                    or (Path.home() / "Desktop" / "More than- מור דאן" / "פגישות"))

DOC_BY_CLASS = {
    "demo": DOCS_DIR / "gad_meetings_demos.md",
    "kickoff": DOCS_DIR / "gad_meetings_kickoffs.md",
    "other": DOCS_DIR / "gad_meetings_other.md",
}

EMPLOYEE_NAME = "גד טמיר"
LANGUAGE = "he"
TRANSCRIBE_MODEL = "gpt-4o-transcribe"
CHUNK_MINUTES = 10
MAX_SINGLE_MINUTES = 23  # gpt-4o-transcribe rejects single requests over ~25 min
SAFE_CHUNK_MB = 20
BYTES_PER_MB = 1024 * 1024
# gpt-4o-transcribe list price, USD per audio-minute (for the cost estimate only).
USD_PER_AUDIO_MINUTE = 0.006

# --- logging -----------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_PATH, encoding="utf-8")],
)
log = logging.getLogger("backfill")


# --- .env loading ------------------------------------------------------------
def load_env() -> None:
    # Prefer a local (non-iCloud) copy of .env — reading the repo's server/.env
    # can wedge if iCloud has evicted it to a dataless placeholder.
    env_file = WORK_DIR / ".env"
    if not env_file.exists():
        env_file = SERVER_DIR / ".env"
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


load_env()
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-5")

# running cost/usage tallies
TALLY = {"audio_minutes": 0.0, "in_tokens": 0, "out_tokens": 0}


# --- tiny HTTP helpers (stdlib only) -----------------------------------------
def _http(req: urllib.request.Request, timeout: int, retries: int = 3) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001 — retry transient network/API errors
            last = e
            wait = 2 * (2 ** attempt)
            log.warning("    http error (%s); retry in %ds", str(e)[:160], wait)
            time.sleep(wait)
    raise RuntimeError(f"http failed after {retries} tries: {last}")


def openai_transcribe(path: Path) -> str:
    """Multipart POST to gpt-4o-transcribe, response_format=text → plain transcript."""
    boundary = uuid.uuid4().hex
    ctype = mimetypes.guess_type(path.name)[0] or "audio/m4a"
    parts: list[bytes] = []

    def field(name: str, value: str) -> None:
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())

    field("model", TRANSCRIBE_MODEL)
    field("language", LANGUAGE)
    field("response_format", "text")
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
        f"Content-Type: {ctype}\r\n\r\n".encode()
    )
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions",
        data=body, method="POST",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    return _http(req, timeout=600).decode("utf-8").strip()


def anthropic_message(system: str, user: str, max_tokens: int = 2000) -> tuple[str, int, int]:
    body = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body, method="POST",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    d = json.loads(_http(req, timeout=180))
    text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text").strip()
    u = d.get("usage", {})
    return text, u.get("input_tokens", 0), u.get("output_tokens", 0)


# --- transcription -----------------------------------------------------------
def ensure_local(path: Path, timeout: int = 900) -> None:
    """Force-download an iCloud dataless placeholder and block until fully local.

    The Desktop folder is iCloud-synced, so most audio files are 0-byte
    placeholders. If ffprobe/transcribe opens one mid-download the fault can
    wedge indefinitely, so we materialize it first via brctl and poll the
    physical block count until it covers the logical size.
    """
    st = path.stat()
    if st.st_blocks * 512 >= st.st_size:
        return  # already materialized
    subprocess.run(["brctl", "download", str(path)], capture_output=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = path.stat()
        if st.st_blocks * 512 >= st.st_size:
            return
        time.sleep(2)
    raise TimeoutError(f"iCloud download did not finish in {timeout}s for {path.name}")


def probe_duration_seconds(audio_path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def transcribe_audio(audio_path: Path, duration_s: float) -> str:
    # gpt-4o-transcribe rejects requests over ~25 min of audio (HTTP 400) and
    # over 25 MB. Chunk if EITHER limit would be exceeded — compressed Zoom audio
    # can be 30+ min while still under 20 MB, so a size-only check misses them.
    small_enough = audio_path.stat().st_size <= SAFE_CHUNK_MB * BYTES_PER_MB
    short_enough = duration_s <= MAX_SINGLE_MINUTES * 60
    if small_enough and short_enough:
        return openai_transcribe(audio_path)

    chunk_seconds = CHUNK_MINUTES * 60
    with tempfile.TemporaryDirectory(prefix="zghl-chunks-") as tmpdir:
        out_pattern = Path(tmpdir) / "chunk_%03d.m4a"
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(audio_path),
            "-vn", "-c:a", "aac", "-b:a", "96k", "-ac", "1", "-ar", "16000",
            "-f", "segment", "-segment_time", str(chunk_seconds),
            "-reset_timestamps", "1", str(out_pattern),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"ffmpeg segment failed: {res.stderr.strip()[:500]}")
        chunks = sorted(Path(tmpdir).glob("chunk_*.m4a"))
        log.info("    chunked into %d parts", len(chunks))
        parts = [openai_transcribe(c) for c in chunks]
    return "\n\n".join(p for p in parts if p)


# --- summary + classification (single Claude call) ---------------------------
SYSTEM_PROMPT = """אתה עוזר עסקי של גד תמיר, יזם ישראלי שמנהל את More-Than (CRM + אוטומציות).
לפניך תמלול של פגישת זום של גד או של עובדת שלו עם לקוח/ה.

המשימה: לסווג את הפגישה, לזהות איש קשר, ולתת סיכום קצר וענייני **בעברית בלבד**, בפורמט המדויק הבא:

סיווג: <demo|kickoff|other>
שם איש קשר משוער: <השם המלא של הלקוח/לקוחה כפי שמופיע בתמלול, או "לא ידוע">

סיכום
[3-6 שורות שמסכמות את עיקרי הפגישה — מי השתתף אם ברור, על מה דיברו, מה הוחלט]

משימות
- [מה לעשות] - אחראי: [שם או "לא צוין"] - דדליין: [תאריך או "לא צוין"]

נקודות מפתח
- [תובנה חשובה 1]
- [תובנה חשובה 2]

הגדרת הסיווג:
- demo = פגישת מכירה / התאמה / הדגמה של המערכת ללקוח פוטנציאלי
- kickoff = פגישת סיכום / התחלת עבודה / אונבורדינג ללקוח שסגר
- other = כל דבר אחר (פגישה פנימית, ייעוץ, לא ברור)

כללים:
- עברית בלבד
- ענייני, ללא מילוי או נימוסים מיותרים
- אם אין משימות ברורות — כתוב "אין משימות שזוהו"
- אל תמציא מידע שלא נאמר בתמלול
- התחל מיד בשורת "סיווג:" — בלי הקדמה
- אל תוסיף הערות אחרי "נקודות מפתח" — סיים שם
"""

_CLASS_RE = re.compile(r"^\s*סיווג\s*:\s*(demo|kickoff|other)\b", re.MULTILINE)
_CONTACT_RE = re.compile(r"^\s*שם איש קשר משוער\s*:\s*(.+?)\s*$", re.MULTILINE)


def summarize_and_classify(transcript: str, folder_guess: str) -> tuple[str, str, str | None]:
    user_msg = (
        f"שם העובד/ת: {EMPLOYEE_NAME}\n"
        f"סיווג משוער לפי שם התיקייה: {folder_guess}. אשר או תקן לפי התוכן בפועל.\n\n"
        f"תמלול הפגישה:\n\n{transcript}"
    )
    raw, in_tok, out_tok = anthropic_message(SYSTEM_PROMPT, user_msg, max_tokens=2000)
    TALLY["in_tokens"] += in_tok
    TALLY["out_tokens"] += out_tok

    m_cls = _CLASS_RE.search(raw)
    classification = m_cls.group(1) if m_cls else folder_guess

    contact = None
    m_contact = _CONTACT_RE.search(raw)
    if m_contact:
        name = m_contact.group(1).strip()
        if name not in ("לא ידוע", "לא צוין", "-", ""):
            contact = name

    summary = _CLASS_RE.sub("", raw, count=1)
    summary = _CONTACT_RE.sub("", summary, count=1).strip()
    return classification, summary, contact


# --- folder helpers ----------------------------------------------------------
def folder_class_guess(folder_name: str) -> str:
    n = folder_name
    if any(k in n for k in ("התאמה", "הדגמה", "דמו", "demo")):
        return "demo"
    if any(k in n for k in ("סיכום", "התחלת עבודה", "אונבורדינג", "kickoff")):
        return "kickoff"
    return "other"


_TS_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+\d{2}\.\d{2}\.\d{2}\s+(.*)$")


def parse_folder(folder_name: str) -> tuple[str, str]:
    m = _TS_PREFIX_RE.match(folder_name)
    if m:
        return m.group(1), m.group(2).strip()
    return "", folder_name


def find_audio(meeting_dir: Path) -> Path | None:
    cands = sorted(meeting_dir.glob("audio*.m4a"), key=lambda p: p.stat().st_size, reverse=True)
    if not cands:
        cands = sorted(meeting_dir.glob("*.m4a"), key=lambda p: p.stat().st_size, reverse=True)
    return cands[0] if cands else None


# --- state -------------------------------------------------------------------
def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"done": [], "failed": []}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def seed_done_from_docs() -> set[str]:
    seen: set[str] = set()
    pat = re.compile(r"folder:\s*`([^`]+)`")
    for doc in DOC_BY_CLASS.values():
        if doc.exists():
            seen.update(pat.findall(doc.read_text(encoding="utf-8")))
    return seen


# --- doc writing -------------------------------------------------------------
def append_entry(classification: str, folder_name: str, iso_date: str, display: str,
                 minutes: int, summary: str) -> None:
    doc = DOC_BY_CLASS[classification]
    block = (
        f"\n## {iso_date or '????'} — {display}\n\n"
        f"- duration: **{minutes} min**, classification: **{classification}**, "
        f"folder: `{folder_name}`\n\n"
        f"### סיכום\n\n{summary}\n"
    )
    with doc.open("a", encoding="utf-8") as f:
        f.write(block)


# --- worker (one meeting) ----------------------------------------------------
def process_meeting(meeting_dir: Path, audio: Path, idx: int, total: int,
                    state: dict, lock: "threading.Lock") -> bool:
    folder = meeting_dir.name
    iso_date, display = parse_folder(folder)
    guess = folder_class_guess(folder)
    short = display[:28]
    log.info("[%d/%d] start: %s", idx, total, short)
    t0 = time.time()
    try:
        ensure_local(audio)
        dur_s = probe_duration_seconds(audio)
        minutes = max(1, round(dur_s / 60))
        transcript = transcribe_audio(audio, dur_s)
        if not transcript.strip():
            raise RuntimeError("empty transcript")
        classification, summary, contact = summarize_and_classify(transcript, guess)
        with lock:
            append_entry(classification, folder, iso_date, display, minutes, summary)
            state["done"].append(folder)
            if folder in state["failed"]:
                state["failed"].remove(folder)  # recovered on retry
            save_state(state)
            TALLY["audio_minutes"] += dur_s / 60.0
        log.info("[%d/%d] ✓ %s | %s | %dmin | contact: %s | %.0fs",
                 idx, total, classification, short, minutes, contact or "—", time.time() - t0)
        return True
    except Exception as e:  # noqa: BLE001 — per-meeting isolation, keep going
        log.error("[%d/%d] ✗ FAILED %s: %s", idx, total, short, e)
        with lock:
            if folder not in state["failed"]:
                state["failed"].append(folder)
            save_state(state)
        return False


# --- main --------------------------------------------------------------------
def in_range(folder_name: str, ym_from: str, ym_to: str) -> bool:
    iso_date, _ = parse_folder(folder_name)
    if not iso_date:
        return False
    ym = iso_date[:7]  # YYYY-MM
    return ym_from <= ym <= ym_to


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max meetings to process (0 = all)")
    ap.add_argument("--dir", default=str(MEETINGS_DIR), help="meetings root folder")
    ap.add_argument("--from", dest="ym_from", default="2025-09", help="earliest YYYY-MM (inclusive)")
    ap.add_argument("--to", dest="ym_to", default="2026-02", help="latest YYYY-MM (inclusive)")
    ap.add_argument("--workers", type=int, default=4, help="concurrent meetings")
    args = ap.parse_args()

    root = Path(args.dir)
    state = load_state()
    done = set(state["done"]) | seed_done_from_docs()
    log.info("already done (state + docs): %d folders", len(done))
    log.info("date range: %s .. %s | workers: %d", args.ym_from, args.ym_to, args.workers)

    pending = []
    for d in sorted([p for p in root.iterdir() if p.is_dir()]):
        if d.name in done:
            continue
        if not in_range(d.name, args.ym_from, args.ym_to):
            continue
        audio = find_audio(d)
        if audio is None:
            continue  # empty / duplicate folder, nothing to transcribe
        pending.append((d, audio))

    log.info("pending meetings in range with audio: %d", len(pending))
    if args.limit:
        pending = pending[: args.limit]
        log.info("limited to %d for this run", len(pending))

    total = len(pending)
    lock = threading.Lock()
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(process_meeting, md, au, i, total, state, lock)
            for i, (md, au) in enumerate(pending, 1)
        ]
        for f in as_completed(futures):
            results.append(f.result())

    processed = sum(1 for r in results if r)
    est_cost = TALLY["audio_minutes"] * USD_PER_AUDIO_MINUTE
    log.info("=" * 60)
    log.info("DONE. processed=%d/%d  failed_total=%d", processed, total, len(state["failed"]))
    log.info("audio minutes this run: %.1f  → est. OpenAI transcription ~$%.2f",
             TALLY["audio_minutes"], est_cost)
    log.info("Claude tokens this run: in=%d out=%d", TALLY["in_tokens"], TALLY["out_tokens"])


if __name__ == "__main__":
    main()
