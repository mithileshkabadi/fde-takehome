# fde-takehome

Take-home assessment repo for a Forward Deployed Engineer (FDE) / AI
Integration Engineer role. Source spec: "FDE Assessment Questions - MCP &
LLM Gateways.pdf". The spec's overview line claims "5 practical technical
tasks" but only 4 are actually detailed in the document — this repo tracks
those 4 as the deliverables. Worth double-checking against the original doc
in case a Task 5 exists elsewhere.

## The four tasks

### Task 1 — MCP server with strict validation & transport handling
Runnable MCP server (Python, official `mcp` SDK) exposing two tools:
- `get_customer_record` — input `customer_id: str` formatted `CUST-XXXXX`.
- `trigger_refund` — inputs `customer_id`, `amount` (positive float),
  `reason` (string, min length 10).

Requirements: strict Pydantic schema validation with standard MCP JSON-RPC
error codes on invalid input; stdio transport; stdout reserved *exclusively*
for JSON-RPC messages, all logs/debug output on stderr.

Evaluation criteria:
- **STDIO isolation** — stdout is pure JSON-RPC, no stray `print`/`console.log`.
- **Protocol compliance** — correct JSON-RPC error mapping and execution flow.
- **Validation** — robust schemas, solid edge-case handling for malformed input.

### Task 2 — MCP security gateway (tool filtering & auth)
HTTP/JSON-RPC reverse proxy sitting between an AI agent client and a
downstream MCP server. Reads `Bearer <token>` to derive a role (`admin` /
`viewer`). `tools/list` forwards transparently. `tools/call` inspects
`params.name`: if it starts with `admin_`, the caller must have role
`admin`, else intercept and return JSON-RPC error `-32001 Unauthorized Tool
Call` without touching the downstream server.

Evaluation criteria:
- Correct JSON-RPC wire format parsing.
- Clean proxy middleware / request-response forwarding.
- Fine-grained, method-level authorization logic and clean error handling.

### Task 3 — LLM gateway streaming guardrail (PII redaction)
LLM Gateway proxy endpoint that routes text generation requests to an LLM
provider and streams the response back to the client, redacting PII
(emails, SSNs, credit card numbers → `[REDACTED]`) from the stream in real
time, including patterns that straddle chunk boundaries.

Evaluation criteria:
- Efficient async stream chunking and buffer state management.
- Performant string/regex matching over partial streams.
- Memory efficiency and low latency (must not buffer the whole response;
  minimize TTFT).

### Task 4 — Rate limiting & model fallback router
Resilient model-routing module for an LLM Gateway: token-aware sliding
window rate limiter (e.g. 50,000 tokens/minute per tenant API key);
failover to a secondary model provider on primary 429 or >3000ms timeout;
standardized error payloads that never leak upstream stack traces or
internal details; on-disk SQLite for persistence.

Evaluation criteria:
- Async concurrency handling and timeout race conditions.
- Accurate rate-limiter state eviction and token tracking.
- Graceful fallback mechanics and standardized error sanitization.

## Stack decisions

- Python 3.12 (the repo's `mcp` SDK dependency needs >=3.10; system
  `python3` on this machine is 3.9.6 and cannot be used — build the venv
  with `python3.12`, confirmed available at `/opt/local/bin/python3.12`).
- FastAPI + uvicorn for all HTTP-facing services (gateway, mocks).
- httpx for all outbound HTTP calls (proxying, tests).
- Pydantic v2 for all schema validation.
- pytest for tests (sync `fastapi.testclient.TestClient`, including for
  SSE streaming responses via `client.stream(...)` — no `pytest-asyncio`
  dependency unless a later task genuinely needs an async test).
- Ruff for linting and formatting (single tool, replaces
  black/flake8/isort).
- Task 4's SQLite access is stdlib `sqlite3` from a threadpool
  (`asyncio.to_thread`), not `aiosqlite` — chosen to avoid an extra
  dependency; revisit if contention under concurrent load becomes a
  problem.
- Dependencies are pinned to exact versions in `pyproject.toml` (checked
  against live PyPI at scaffold time, 2026-09-04) for reproducibility.

## Conventions

- **stdout is sacred.** Every process in this repo (the stdio MCP server
  above all, but gateways and mocks too) must never write anything but
  protocol/response bytes to stdout. All logging — `logging` module
  configured with a `StreamHandler(sys.stderr)` — goes to stderr. Never use
  bare `print()` for anything other than an intentional protocol write.
- **Tests live alongside the module they cover.** Every module gets tests
  written in the same change that introduces it — not deferred to a later
  pass. Mirror `src/<package>/foo.py` under `tests/<package>/test_foo.py`
  (see `mocks/` + `tests/mocks/` for the pattern already in place).

## Design decisions

(Running log — add an entry each time we make a nontrivial call during
implementation, most recent last.)

- 2026-09-04 — Scaffolded repo: package-per-task layout under `src/`, mock
  upstreams (`mocks/mcp_downstream.py`, `mocks/llm_provider.py`) built
  first so Tasks 2 and 3 have something real to run against. No task
  logic implemented yet.
- 2026-09-04 — `tests/__init__.py` is required and must stay. Without it,
  pytest's default "prepend" import mode treats `tests/mocks/` as *the*
  `mocks` package (since `tests/` had no `__init__.py` to keep walking
  past), which shadows the real top-level `mocks/` package and breaks
  `from mocks.foo import ...` in test files with a confusing
  `ModuleNotFoundError`. Keep every future `tests/<x>/` directory name
  distinct from a real top-level package name, or keep `tests/__init__.py`
  in place — both are needed in general, but the `__init__.py` is the fix
  that matters here.
- 2026-09-04 — Verified the full harness end to end: `make install` (venv
  via python3.12), `make lint` (ruff check + format, clean), `make test`
  (20/20 passing), `./scripts/smoke.sh` (8/8 passing) all green.
- 2026-09-04 — Task 1 implemented on `mcp==2.1.1`, which turned out to have
  a materially different API from the 1.x SDK: `FastMCP` is gone
  (`mcp.server.fastmcp` raises `ModuleNotFoundError` on import with a
  migration pointer), replaced by `mcp.server.mcpserver.MCPServer`. More
  importantly, `MCPServer`'s `_handle_call_tool` deliberately catches every
  exception except `MCPError` — including the `ToolError`/`ValidationError`
  it raises itself when a tool's declared argument schema doesn't match —
  and turns it into a *successful* `CallToolResult(isError=True)`, per the
  current MCP spec's convention that tool execution failures are in-band,
  not protocol errors. That's the opposite of what the assessment spec asks
  for ("Reject invalid formats with standard MCP JSON-RPC error codes").
  So Task 1 uses `mcp.server.lowlevel.Server` instead: we validate arguments
  ourselves against the Pydantic model and explicitly raise
  `mcp.shared.exceptions.MCPError(code=INVALID_PARAMS, ...)`, which the
  low-level dispatcher (`handler_exception_to_error_data` in
  `mcp/shared/jsonrpc_dispatcher.py`) serializes as a genuine JSON-RPC
  `error` response — confirmed by an actual subprocess test
  (`tests/mcp_server/test_stdio_e2e.py`), not just a unit test. A
  well-formed but nonexistent `customer_id` is treated differently: that's
  a valid request that can't be fulfilled, so it returns a normal
  `CallToolResult(isError=True)`, matching current MCP convention — only
  schema-invalid input and unknown tool names get the protocol-level error.
  Also confirmed (by reading `mcp/server/stdio.py`) that `stdio_server()` in
  this SDK version already hardens stdout itself: while serving, it diverts
  the process's real fd 1 to stderr and drives the JSON-RPC wire through a
  private duplicated descriptor, so a stray `print()` anywhere lands on
  stderr, not the wire, by construction — the "never print to stdout"
  convention is now defense in depth on top of that, not the only guard.
- 2026-09-06 — Task 2 implemented. Two decisions worth recording:
  (1) Bearer-token → role mapping is a static in-memory dict
  (`mcp_gateway/auth.py`) since there's no real identity provider in scope —
  documented in the module docstring as a stand-in, swappable without
  touching the authorization logic. (2) The spec's rule ("`tools/list`
  forwards transparently, `tools/call` to `admin_*` needs admin role") is
  generalized to "everything forwards except an unauthorized `admin_*` tool
  call": blocking every method that isn't literally `tools/list` would break
  a real client's `initialize`/`ping` handshake, which the spec surely
  doesn't intend. Also extended proxying to JSON-RPC batches (the downstream
  mock already supports them): each item in a batch is authorized
  independently, only the authorized subset goes downstream in one call,
  responses are merged back in original order — proven with a test that
  sends one allowed + one blocked call in the same batch and asserts the
  downstream only ever saw the allowed one.
- 2026-09-07 — Task 3 implemented: a bounded "holdback buffer" redactor
  (`llm_gateway/redactor.py`). Each `feed()` re-runs the redaction regex
  over `<held-back tail> + <new chunk>` — a small, bounded string, never the
  whole response — and only ever emits everything except the last
  `HOLDBACK` (64) characters, which might still be the start of an
  in-progress match. Chosen over buffering the whole response (violates the
  spec directly) and over a naive "redact each chunk independently"
  approach (silently misses any pattern split across a chunk boundary,
  which the mock's `pii` behavior deliberately exercises). `HOLDBACK=64` is
  a documented trade-off: must be at least the longest pattern we match
  (credit card ~19 chars is the longest), and larger values are safer
  against very long splits but directly cost latency, since nothing can be
  emitted until more than 64 characters have arrived. Redaction runs on the
  *extracted* SSE delta content, not raw bytes, so a match can never
  straddle two JSON envelopes and corrupt one.
  Manually running the gateway against the real mock over actual HTTP (not
  just the in-process test client) surfaced a real bug the tests didn't
  catch: the naive `buffer[:-HOLDBACK]` cut could land inside the literal
  `"[REDACTED]"` marker itself, e.g. one SSE event ending in `"...[REDA"`
  and the next starting with `"CTED]..."`. Not a PII leak (concatenated,
  the stream was always correct — that's why the existing reassembly-based
  tests passed despite the bug), but it looks broken to any consumer that
  doesn't reassemble the whole stream before reading it. Fixed by pulling
  the split point back before a `[REDACTED]` occurrence it would otherwise
  cut through (`PiiStreamRedactor._safe_split_point`), plus a regression
  test asserting no emission ends/starts with a partial marker fragment.
  Worth remembering generally: an in-process TestClient run isn't a full
  substitute for actually curling two real separate processes — this is
  the second bug this project has only surfaced that way (the smoke.sh
  `check()`-argument bug earlier was the first).
- 2026-09-07 — Task 4 implemented, the last of the four. Token cost is
  caller-declared (`max_tokens` in the request body, OpenAI-style) rather
  than estimated from prompt length — confirmed with the user, since
  there's no real tokenizer in scope and an estimate wouldn't account for
  response tokens anyway. The rate limiter is a sliding-window *log* in
  SQLite (record every accepted request's tokens+timestamp, sum the
  trailing 60s window, evict older rows each check) rather than a
  fixed-window counter, specifically to avoid the standard fixed-window bug
  where a client can burst up to 2x the limit across a window boundary. A
  fresh `sqlite3` connection per call (not a shared one) sidesteps
  same-thread restrictions under `asyncio.to_thread`; a per-tenant
  `asyncio.Lock` closes the real race, which is check-then-record
  (TOCTOU) under concurrency, not a SQLite locking problem — proven with a
  test firing 30 concurrent requests at a 1000-token budget and asserting
  the accepted total is exactly 1000.
  Initially wired primary/secondary URLs as module-level globals that
  tests mutated directly for override — inconsistent with the
  dependency-override pattern used everywhere else in this repo (Tasks 2
  and 3), and fragile (order-dependent test pollution risk). Refactored to
  a `RouterConfig` dataclass behind its own `Depends()`, matching the
  established pattern, before calling it done.
  Verified failover with three genuinely separate live processes (primary,
  secondary, gateway), not just the test suite: primary configured to
  sleep 2s against a 1s timeout correctly failed over in ~1.1s wall-clock,
  confirmed via the gateway's own log line
  (`primary model failed (TimeoutError()); failing over to secondary`).
