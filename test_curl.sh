#!/usr/bin/env bash
# Smoke tests for the microservice. Run from the project folder while uvicorn is running.
# Usage: bash tests/test_curl.sh [base_url]
set -u
BASE="${1:-http://localhost:8000}"
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "== 1. Health"
curl -s "$BASE/health"; echo; echo

echo "== 2. Generate a post (full sample request)"
curl -s -X POST "$BASE/linkedin" -H "Content-Type: application/json" \
  -d @"$DIR/sample_request.json" | python3 -m json.tool; echo

echo "== 3. Minimal request (all defaults)"
curl -s -X POST "$BASE/linkedin" -H "Content-Type: application/json" -d '{}' \
  | python3 -c "import sys,json; r=json.load(sys.stdin); print({k: r.get(k) for k in ('run_id','mode','confidence','hashtags')})"; echo

echo "== 4. Validation error (expect HTTP 422: num_ideas must be 1-5)"
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST "$BASE/linkedin" \
  -H "Content-Type: application/json" -d '{"num_ideas": 9}'; echo

echo "== 5. Write and read a log row"
curl -s -X POST "$BASE/log" -H "Content-Type: application/json" \
  -d '{"run_id":"curl-test","route":"manual_test","confidence":0.8,"threshold":0.75,"dry_run":true}'; echo
curl -s "$BASE/log?limit=5" | python3 -m json.tool
