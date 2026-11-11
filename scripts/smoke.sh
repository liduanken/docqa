#!/usr/bin/env bash
# End-to-end check against a running service: answer with sources, refusal, size limit.
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8080}"
CURL=(curl -sS --noproxy 'localhost,127.0.0.1')

DOC="Kubernetes clusters are upgraded every quarter. Upgrades go to dev before prod. Every upgrade needs a rollback plan."

echo "== upload document"
DOC_ID=$("${CURL[@]}" -X POST "$BASE_URL/v1/documents" -H 'content-type: application/json' \
  -d "{\"text\": \"$DOC\"}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["document_id"])')
echo "document_id=$DOC_ID"

echo "== answerable question (expect an answer with sources)"
"${CURL[@]}" -X POST "$BASE_URL/v1/documents/$DOC_ID/questions" -H 'content-type: application/json' \
  -d '{"question": "How often are clusters upgraded?"}' | python3 -m json.tool

echo "== unanswerable question (expect a refusal)"
"${CURL[@]}" -X POST "$BASE_URL/v1/documents/$DOC_ID/questions" -H 'content-type: application/json' \
  -d '{"question": "What is the office wifi password?"}' | python3 -m json.tool

echo "== oversized document (expect 413)"
python3 -c 'import json; print(json.dumps({"text": "x" * 200000}))' |
  "${CURL[@]}" -o /dev/null -w '%{http_code}\n' -X POST "$BASE_URL/v1/documents" \
    -H 'content-type: application/json' --data-binary @-
