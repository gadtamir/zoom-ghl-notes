#!/usr/bin/env bash
# Post-deploy health check for the phone-call pipeline.
#
# Verifies, against GHL directly (not the DB), that recent calls have summary
# notes — i.e. nothing is stuck. Run a few minutes after a Render deploy, and
# again ~1h later (after the self-healing `reconcile` beat has had a chance to
# run). If the only "MISSING" lines are <30s / no-recording calls, you're clean.
#
# Usage:  ./scripts/healthcheck_calls.sh [YYYY-MM-DD]
#         (defaults to the last 2 days)

set -euo pipefail
cd "$(dirname "$0")/.."

SINCE="${1:-$(python3 -c 'import datetime; print((datetime.date.today()-datetime.timedelta(days=2)).isoformat())')}"

echo "▶ Checking for calls missing a summary since ${SINCE} (read-only)…"
caffeinate -dimsu ./.venv/bin/python scripts/backfill_calls.py \
    --since "${SINCE}" --report-missing

echo
echo "Reminder: a 'MISSING' line with dur<30 or 'no recording' is EXPECTED and fine."
echo "If real calls (dur>=30) are missing AND the pipeline is deployed, run a backfill:"
echo "  caffeinate -dimsu ./.venv/bin/python scripts/backfill_calls.py \\"
echo "      --since ${SINCE} --workers 2 --model claude-haiku-4-5-20251001"
