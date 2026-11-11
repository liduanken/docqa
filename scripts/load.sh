#!/usr/bin/env bash
# Steady question load for DURATION seconds; prints a histogram of HTTP status codes.
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8080}"
DURATION="${DURATION:-60}"
INTERVAL="${INTERVAL:-0.2}"
CURL=(curl -sS --noproxy 'localhost,127.0.0.1')

DOC_ID=$("${CURL[@]}" -X POST "$BASE_URL/v1/documents" -H 'content-type: application/json' \
  -d '{"text": "Kubernetes clusters are upgraded every quarter. Upgrades go to dev before prod."}' |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["document_id"])')

CODES=$(mktemp)
trap 'rm -f "$CODES"' EXIT
END=$(( $(date +%s) + DURATION ))
while [ "$(date +%s)" -lt "$END" ]; do
  "${CURL[@]}" -o /dev/null -w '%{http_code}\n' -X POST "$BASE_URL/v1/documents/$DOC_ID/questions" \
    -H 'content-type: application/json' -d '{"question": "How often are clusters upgraded?"}' >>"$CODES" || echo 000 >>"$CODES"
  sleep "$INTERVAL"
done
sort "$CODES" | uniq -c
