#!/usr/bin/env python3
"""Classify every Vika call in the knowledge base and print aggregate stats.

Parses docs/vika_calls_knowledge.md, sends the calls to Claude in batches to
label each with an outcome / objection / booked flag, then aggregates a funnel
and objection breakdown. Stdlib-only (urllib) — the SDKs hang on import here.
"""

import json
import re
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
DOC = REPO / "docs" / "vika_calls_knowledge.md"
WORK = Path("/Users/gadtamir/zghl_backfill")
OUT_JSON = WORK / "vika_classifications.json"

env = {}
for line in (WORK / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, _, v = line.partition("="); env[k.strip()] = v.strip()
KEY = env["ANTHROPIC_API_KEY"]
MODEL = env.get("ANTHROPIC_MODEL", "claude-opus-4-5")

OUTCOMES = ["booked_zoom_with_gad", "booked_webinar", "callback_scheduled",
            "interested_no_commit", "info_sent", "not_relevant",
            "not_interested", "no_real_conversation", "other"]
OBJECTIONS = ["none", "price_or_setup_cost", "timing_busy", "send_info_first",
              "already_in_process_or_competitor", "needs_to_consult", "not_interested", "other"]

SYS = """You classify outbound SDR phone calls (Hebrew) made by Vika, an appointment-setter
for More-Than (a CRM/automation company). She calls warm leads to qualify them and book
either a webinar or a Zoom demo with Gad (the owner).

For EACH call you receive, return one JSON object with:
- "i": the call index (integer, as given)
- "outcome": one of %s
- "objection": the main objection raised by the lead, one of %s
- "booked": true if the call resulted in a scheduled webinar/zoom/meeting, else false
- "talk_quality": Vika's handling quality 1-5 (5=excellent objection handling & control)

Rules:
- "no_real_conversation" = no answer, voicemail, wrong number, or <2 exchanges.
- Base it ONLY on the transcript. Output ONLY a JSON array, no prose.
""" % (OUTCOMES, OBJECTIONS)


def parse_calls(text: str, since: str | None = None) -> list[dict]:
    calls = []
    blocks = re.split(r"\n## ", text)
    for b in blocks[1:]:
        lines = b.splitlines()
        head = lines[0].strip()
        dm = re.match(r"(\d{4}-\d{2}-\d{2})", head)
        date = dm.group(1) if dm else ""
        if since and date and date < since:
            continue
        m = re.search(r"duration:\s*\*\*(\d+)s\*\*", b)
        dur = int(m.group(1)) if m else 0
        d = re.search(r"direction:\s*\*\*(\w+)\*\*", b)
        direction = d.group(1) if d else "?"
        # transcript = everything after the meta line, up to the trailing ---
        body = b.split("\n\n", 1)[1] if "\n\n" in b else ""
        transcript = body.split("\n---", 1)[0].strip()
        calls.append({"head": head, "date": date, "duration": dur, "direction": direction, "transcript": transcript})
    return calls


def claude(messages_text: str) -> str:
    body = json.dumps({
        "model": MODEL, "max_tokens": 4000, "temperature": 0,
        "system": [{"type": "text", "text": SYS, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": messages_text}],
    }).encode("utf-8")
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"x-api-key": KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.load(r)
            return "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
        except Exception as e:  # noqa: BLE001
            if attempt == 3:
                raise
            time.sleep(2 * (2 ** attempt))


def main() -> None:
    since = sys.argv[1] if len(sys.argv) > 1 else None
    calls = parse_calls(DOC.read_text(encoding="utf-8"), since=since)
    print(f"parsed {len(calls)} calls (since={since})", flush=True)

    labels = {}
    BATCH = 15
    for start in range(0, len(calls), BATCH):
        batch = calls[start:start + BATCH]
        payload = "Classify these calls:\n\n" + "\n\n".join(
            f"[CALL i={start+j}] (duration {c['duration']}s)\n{c['transcript'][:1500]}"
            for j, c in enumerate(batch)
        )
        raw = claude(payload)
        mm = re.search(r"\[.*\]", raw, re.DOTALL)
        if not mm:
            print(f"  batch {start}: no JSON, skipping", flush=True); continue
        try:
            arr = json.loads(mm.group(0))
        except json.JSONDecodeError:
            print(f"  batch {start}: bad JSON", flush=True); continue
        for o in arr:
            if isinstance(o, dict) and "i" in o:
                labels[int(o["i"])] = o
        print(f"  classified {len(labels)}/{len(calls)}", flush=True)

    # attach + save
    for i, c in enumerate(calls):
        c.update(labels.get(i, {}))
    OUT_JSON.write_text(json.dumps(calls, ensure_ascii=False, indent=1), encoding="utf-8")

    # aggregate
    n = len(calls)
    outc = Counter(c.get("outcome", "unlabeled") for c in calls)
    obj = Counter(c.get("objection", "unlabeled") for c in calls)
    booked = sum(1 for c in calls if c.get("booked") is True)
    durs = sorted(c["duration"] for c in calls)
    avg = sum(durs) / n if n else 0
    median = durs[n // 2] if n else 0
    under30 = sum(1 for d in durs if d < 30)
    under60 = sum(1 for d in durs if d < 60)
    over120 = sum(1 for d in durs if d >= 120)
    tq = [c["talk_quality"] for c in calls if isinstance(c.get("talk_quality"), (int, float))]
    avg_tq = sum(tq) / len(tq) if tq else 0

    print("\n" + "=" * 50)
    print(f"TOTAL CALLS: {n}")
    print(f"booked (any meeting/webinar): {booked}  ({booked/n*100:.0f}%)")
    print(f"duration: avg {avg:.0f}s, median {median}s | <30s:{under30} <60s:{under60} >=120s:{over120}")
    print(f"avg talk_quality (1-5): {avg_tq:.2f}")
    print("\nOUTCOMES:")
    for k, v in outc.most_common():
        print(f"  {k}: {v} ({v/n*100:.0f}%)")
    print("\nOBJECTIONS:")
    for k, v in obj.most_common():
        print(f"  {k}: {v} ({v/n*100:.0f}%)")


if __name__ == "__main__":
    main()
