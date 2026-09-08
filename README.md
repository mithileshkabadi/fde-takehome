# fde-takehome

Take-home assessment repo for a Forward Deployed Engineer (FDE) / AI
Integration Engineer role. Four deliverables: an MCP server, an MCP
security gateway, an LLM gateway with streaming PII redaction, and a
token-aware rate limiter with model failover. See [CLAUDE.md](CLAUDE.md)
for the full spec, stack decisions, and conventions.

## Setup

```
make install   # creates .venv with python3.12, installs deps
make test      # runs the test suite
make lint      # ruff check + format check
```

## Layout

```
src/
  mcp_server/     # Task 1 — MCP server, strict validation, stdio transport
  mcp_gateway/    # Task 2 — MCP security gateway (role-based tool auth)
  llm_gateway/    # Task 3 — LLM gateway, streaming PII redaction
  rate_limiter/   # Task 4 — token-aware rate limiter + model failover
mocks/            # mock upstreams used by tests and local runs
  mcp_downstream.py
  llm_provider.py
tests/            # mirrors src/ and mocks/, one test module per module
scripts/
  smoke.sh        # starts both mocks, curls them, verifies the harness
```

## Task 1 — MCP server

`src/mcp_server/` — stdio MCP server exposing `get_customer_record` and
`trigger_refund`, built on the low-level `mcp.server.lowlevel.Server` (not
the high-level `MCPServer`) so that invalid input reaches the client as a
real JSON-RPC protocol error rather than an in-band tool result. See the
module docstring in `src/mcp_server/server.py` for why.

- `src/mcp_server/models.py` — strict Pydantic input schemas (`extra="forbid"`,
  regex-constrained `customer_id`, `amount > 0`, `reason` min length 10).
- `src/mcp_server/server.py` — tool dispatch: schema-invalid input or an
  unknown tool name raises `MCPError` (JSON-RPC `-32602 Invalid params`); a
  well-formed but nonexistent `customer_id` returns a normal
  `CallToolResult(isError=True)`, since the request itself was valid.
- `src/mcp_server/__main__.py` — stdio entry point (`make run-mcp-server` or
  `python -m mcp_server`).
- Tests: `tests/mcp_server/test_models.py` (schema edge cases),
  `test_server.py` (handler-level, incl. asserting `MCPError.code`),
  `test_stdio_e2e.py` (spawns the real subprocess over actual stdio pipes —
  the only way to genuinely verify stdout carries nothing but JSON-RPC).

## Task 2 — MCP security gateway

`src/mcp_gateway/` — HTTP/JSON-RPC reverse proxy in front of
`mocks/mcp_downstream.py`. `tools/list` (and anything else) forwards
transparently; a `tools/call` targeting an `admin_*` tool requires the
caller's Bearer-token role to be `admin`, else it's intercepted locally
(JSON-RPC `-32001 Unauthorized Tool Call`) without the downstream server
ever being contacted.

- `src/mcp_gateway/auth.py` — Bearer token → role. No real identity
  provider for this assessment: a static `TOKEN_ROLES` lookup table
  (`admin-token` → `admin`, `viewer-token` → `viewer`); swap it for a real
  verifier without touching the authorization logic.
- `src/mcp_gateway/app.py` — the proxy. Batches are supported: each item is
  authorized independently, only the authorized subset is forwarded
  downstream in one call, and responses are merged back in original order —
  one blocked call in a batch doesn't block the rest, and doesn't reach the
  downstream server either.
- Run it: `make run-mock-mcp` (port 9001) in one terminal,
  `make run-mcp-gateway` (port 8010) in another — the gateway reads
  `MCP_DOWNSTREAM_URL` (defaults to `http://127.0.0.1:9001/`).
- Tests: `tests/mcp_gateway/test_auth.py` (token/role edge cases),
  `test_app.py` (proxy behavior — uses a spy client wrapping an in-process
  instance of the mock downstream, so tests can assert an unauthorized call
  *never reached* the downstream server, not just that it got the right
  error back).

## Task 3 — LLM gateway (PII redaction)

`src/llm_gateway/` — proxies `POST /v1/completions` to `mocks/llm_provider.py`
and streams the response back, redacting emails, SSNs, and credit card
numbers as `[REDACTED]` in real time — including patterns the upstream
deliberately splits across separate SSE chunks.

- `src/llm_gateway/redactor.py` — `PiiStreamRedactor`: a bounded
  "holdback" buffer (64 chars) that never grows with the length of the
  response. Each `feed()` re-scans only `<held-back tail> + <new chunk>`
  and emits everything except the last 64 characters, which might still be
  the start of an in-progress match; `flush()` releases the final tail when
  the stream ends. Documented trade-off: 64 was chosen as the smallest
  holdback that safely covers all three pattern types (SSN 11 chars, credit
  card ~19, realistic emails) — bigger is safer against very long split
  matches, but directly costs latency, since nothing can be emitted until
  more than 64 characters have arrived.
- `src/llm_gateway/app.py` — redaction runs on the *extracted* delta text
  from each SSE event, not the raw bytes, so a match can never straddle
  (and corrupt) the JSON envelope between two events. A non-200 upstream
  response (e.g. the mock's `rate_limited` behavior) is detected before
  committing to a streaming response and passed through as-is; timeout and
  failover for a slow/failing upstream is explicitly out of scope — that's
  Task 4.
- Run it: `make run-mock-llm` (port 9002), `make run-llm-gateway` (port
  8020) — reads `LLM_UPSTREAM_URL` (defaults to
  `http://127.0.0.1:9002/v1/completions`).
- Tests: `tests/llm_gateway/test_redactor.py` (redactor unit tests,
  including a regression test that the `[REDACTED]` marker itself is never
  torn across two emissions — see the design log) and `test_app.py`
  (end-to-end through the gateway against the in-process mock, including
  asserting no raw PII appears in any individual wire event, not just the
  final reassembled text).

## Task 4 — Rate limiter & model failover

`src/rate_limiter/` — `POST /v1/completions` gates on a per-tenant token
budget before ever calling a model provider, then routes the request through
a primary model with automatic failover to a secondary.

- The caller declares `max_tokens` in the request body (mirrors OpenAI's
  API) — no tokenizer in scope, so the request states its own cost.
- `src/rate_limiter/limiter.py` — `TokenRateLimiter`: a sliding-window
  *log* (not a fixed-window counter) backed by on-disk SQLite — every
  accepted request records `(tenant, timestamp, tokens)`; a check sums the
  trailing 60s window and evicts older rows on every call, avoiding the
  classic fixed-window boundary-burst bug. The check-then-record sequence
  is a genuine race under concurrency, so each tenant's calls are
  serialized through a per-tenant `asyncio.Lock` — proven by a test that
  fires 30 concurrent requests at a 1000-token budget and asserts the
  accepted total is exactly 1000, never more.
- `src/rate_limiter/router.py` — `complete_with_failover`: races the
  primary against a 3s timeout (`asyncio.wait_for`, which also handles
  cancelling the loser); a primary 429 or timeout retries once against the
  secondary (itself bounded by the same timeout). Any failure beyond that
  raises `RouterError`, whose message is the *only* thing that reaches the
  client — upstream status codes, connection errors, and exception detail
  are logged server-side but never leaked into the response.
- Run it: `make run-mock-llm` (port 9002, primary), `make
  run-mock-llm-secondary` (port 9003, secondary — same mock, second port),
  `make run-rate-limiter` (port 8030).
- Verified with three genuinely separate live processes, not just the test
  suite: a primary configured to sleep 2s against a 1s failover timeout
  correctly returned `"model":"secondary"` in ~1.1s wall-clock, with the
  gateway's own logs showing the primary's `TimeoutError` and the clean
  fallback.
- Tests: `tests/rate_limiter/test_limiter.py` (budget accounting, sliding
  eviction via an injectable clock, the concurrency test above),
  `test_router.py` (429/timeout failover against the real mock, sanitized
  error message when both providers fail), `test_app.py` (end-to-end,
  including a spy proving a rate-limited request never reaches a model
  provider at all).

## Mock upstreams

Two mocks back the gateway tasks so they have something real to run
against:

- `mocks/mcp_downstream.py` — HTTP JSON-RPC MCP server (`tools/list`,
  `tools/call`), one `admin_reset_key` tool plus two normal tools, batch
  request and notification handling per JSON-RPC 2.0.
- `mocks/llm_provider.py` — SSE-streaming completion endpoint. Behavior is
  selected per-request via the `X-Mock-Behavior` header or `behavior` query
  param: `normal`, `rate_limited` (429), `slow` (exceeds a timeout), `pii`
  (streams synthetic emails/SSNs/credit card numbers split across chunk
  boundaries).

Run them with `make run-mock-mcp` / `make run-mock-llm`, or exercise both
at once with `make smoke`.
