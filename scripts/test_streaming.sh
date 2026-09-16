#!/usr/bin/env bash
# Test Phase 4c SSE streaming against a running server.
# Usage: bash scripts/test_streaming.sh
# Requires: curl, jq (optional), server running on :8000

set -euo pipefail

BASE="${TESSERA_BASE_URL:-http://127.0.0.1:8000}"
EMAIL="${TESSERA_DEMO_EMAIL:-stream-demo@tessera.local}"
PASS="${TESSERA_DEMO_PASS:-stream-demo-pw}"

echo "[streaming-test] target: $BASE"

# Sign up (ignore "already exists") then log in
curl -s -o /dev/null -X POST "$BASE/auth/signup" \
  -H "Content-Type: application/json" \
  -d "{\"email\": \"$EMAIL\", \"password\": \"$PASS\"}"

TOKEN=$(curl -s -X POST "$BASE/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"email\": \"$EMAIL\", \"password\": \"$PASS\"}" \
  | grep -o '"token":"[^"]*"' | cut -d'"' -f4)

if [ -z "$TOKEN" ]; then
  echo "[streaming-test] ERROR: could not obtain token" >&2
  exit 1
fi
echo "[streaming-test] authenticated"

echo ""
echo "[streaming-test] --- SSE stream for: 'What is this knowledge base about?' ---"
curl -s -N -X POST "$BASE/ask" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"question": "What is this knowledge base about?"}' | head -30

echo ""
echo "[streaming-test] done"
