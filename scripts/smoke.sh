#!/usr/bin/env bash
# Starts both mock upstreams, curls them to exercise the interesting paths,
# and tears them down. Run from anywhere: ./scripts/smoke.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_BIN="$REPO_ROOT/.venv/bin"

MCP_PORT=9001
LLM_PORT=9002
MCP_URL="http://127.0.0.1:$MCP_PORT"
LLM_URL="http://127.0.0.1:$LLM_PORT"

MCP_LOG="$(mktemp)"
LLM_LOG="$(mktemp)"
PASS=0
FAIL=0

if [[ ! -x "$VENV_BIN/uvicorn" ]]; then
    echo "error: $VENV_BIN/uvicorn not found — run 'make install' first" >&2
    exit 1
fi

cleanup() {
    [[ -n "${MCP_PID:-}" ]] && kill "$MCP_PID" 2>/dev/null
    [[ -n "${LLM_PID:-}" ]] && kill "$LLM_PID" 2>/dev/null
    rm -f "$MCP_LOG" "$LLM_LOG"
}
trap cleanup EXIT

check() {
    local desc="$1"
    local condition="$2"
    if [[ "$condition" == "0" ]]; then
        echo "  PASS - $desc"
        PASS=$((PASS + 1))
    else
        echo "  FAIL - $desc"
        FAIL=$((FAIL + 1))
    fi
}

echo "Starting mock MCP downstream on :$MCP_PORT ..."
(cd "$REPO_ROOT" && "$VENV_BIN/uvicorn" mocks.mcp_downstream:app --port "$MCP_PORT" >"$MCP_LOG" 2>&1) &
MCP_PID=$!

echo "Starting mock LLM provider on :$LLM_PORT ..."
(cd "$REPO_ROOT" && "$VENV_BIN/uvicorn" mocks.llm_provider:app --port "$LLM_PORT" >"$LLM_LOG" 2>&1) &
LLM_PID=$!

wait_for() {
    local url="$1"
    for _ in $(seq 1 50); do
        curl -s -o /dev/null "$url" && return 0
        sleep 0.1
    done
    return 1
}

if ! wait_for "$MCP_URL/health"; then
    echo "mock MCP downstream failed to start; log:" >&2
    cat "$MCP_LOG" >&2
    exit 1
fi
if ! wait_for "$LLM_URL/health"; then
    echo "mock LLM provider failed to start; log:" >&2
    cat "$LLM_LOG" >&2
    exit 1
fi
echo "Both mocks are up."
echo

echo "== mcp_downstream =="

resp="$(curl -s "$MCP_URL/health")"
check "GET /health -> {\"status\":\"ok\"}" $([[ "$resp" == '{"status":"ok"}' ]] && echo 0 || echo 1)

resp="$(curl -s -X POST "$MCP_URL/" -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')"
check "tools/list includes admin_reset_key" $([[ "$resp" == *admin_reset_key* ]] && echo 0 || echo 1)

resp="$(curl -s -X POST "$MCP_URL/" -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"echo","arguments":{"text":"hello"}}}')"
check "tools/call echo returns the text back" $([[ "$resp" == *'"hello"'* ]] && echo 0 || echo 1)

status="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$MCP_URL/" -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"tools/list"}')"
check "notification (no id) gets HTTP 204" $([[ "$status" == "204" ]] && echo 0 || echo 1)

resp="$(curl -s -X POST "$MCP_URL/" -H 'Content-Type: application/json' \
    -d '[{"jsonrpc":"2.0","id":1,"method":"tools/list"},{"jsonrpc":"2.0","id":2,"method":"tools/list"}]')"
RESP="$resp" python3 -c "
import json, os, sys
try:
    body = json.loads(os.environ['RESP'])
    sys.exit(0 if isinstance(body, list) and len(body) == 2 else 1)
except Exception:
    sys.exit(1)
"
check "batch request returns an array of 2 responses" $?

echo
echo "== llm_provider =="

status="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$LLM_URL/v1/completions" \
    -H 'X-Mock-Behavior: rate_limited' -H 'Content-Type: application/json' -d '{"prompt":"hi"}')"
check "rate_limited behavior returns HTTP 429" $([[ "$status" == "429" ]] && echo 0 || echo 1)

resp="$(curl -s -N -X POST "$LLM_URL/v1/completions?behavior=normal" -H 'Content-Type: application/json' -d '{"prompt":"hi"}')"
check "normal stream ends with [DONE]" $([[ "$resp" == *'[DONE]'* ]] && echo 0 || echo 1)

resp="$(curl -s -N -X POST "$LLM_URL/v1/completions?behavior=pii" -H 'Content-Type: application/json' -d '{"prompt":"hi"}')"
check "pii stream contains split email/SSN/credit-card fragments" $([[ "$resp" == *"john.doe@exam"* && "$resp" == *"123-45-"* && "$resp" == *"4111-1111-1111-"* ]] && echo 0 || echo 1)

echo
echo "$PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
