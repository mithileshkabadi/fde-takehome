# fde-takehome — engineering notes

## Project purpose

Assessment submission implementing four components against a single
specification ("FDE Assessment Questions - MCP & LLM Gateways"): an MCP
server (Task 1), an MCP security gateway (Task 2), an LLM gateway with
streaming PII redaction (Task 3), and a token-aware rate limiter with model
failover (Task 4). Two mock upstreams (`mocks/mcp_downstream.py`,
`mocks/llm_provider.py`) stand in for a real MCP server and a real LLM
provider so Tasks 2–4 have something concrete to run against.

The specification's overview line states "5 practical technical tasks";
only four are detailed in the document. No separate fifth task was located
in the supplied material. This repository implements the four detailed
tasks.

See `README.md` for the full architecture, request flows, task-by-task
explanation, and manual verification commands. This file is the engineering
decision log: why things are built the way they are, not what they do.

## Stack

- Python 3.12. The `mcp` SDK dependency requires >=3.10; the system
  `python3` on the reference machine was 3.9.6 and could not be used —
  the virtualenv is built explicitly with `python3.12`.
- FastAPI + uvicorn for every HTTP-facing service (both gateways, the rate
  limiter, both mocks).
- httpx for all outbound HTTP calls, in both application code and tests
  (including `httpx.ASGITransport` for in-process test clients against a
  mock's FastAPI app, avoiding real sockets in the test suite).
- Pydantic v2 for schema validation.
- pytest, with the synchronous `fastapi.testclient.TestClient` throughout
  — including for SSE streaming responses via `client.stream(...)` — so no
  `pytest-asyncio` dependency was added; async code under test is driven
  directly with `asyncio.run(...)` where a unit test needs to call a
  coroutine outside a FastAPI request cycle.
- Ruff for both linting and formatting (single tool, replaces
  black/flake8/isort).
- Task 4's SQLite access is stdlib `sqlite3` from a thread
  (`asyncio.to_thread`) rather than `aiosqlite`, to avoid an extra
  dependency for a single table with modest concurrency requirements.
- Dependencies are pinned to exact versions in `pyproject.toml`.

## Architecture decisions

- **Package-per-task layout under `src/`.** `mcp_server/`, `mcp_gateway/`,
  `llm_gateway/`, `rate_limiter/` are independent packages, each runnable
  and testable on its own. `mocks/` is a separate top-level package, not
  nested under `src/`, since it is test/development infrastructure rather
  than a deliverable.
- **Mocks built before the components that depend on them.** Both mock
  upstreams were implemented first, with their controllable failure modes
  (`rate_limited`, `slow`, `pii`) designed around what Tasks 2–4 would need
  to exercise, so the gateways always had a real, deterministic upstream to
  run against rather than being tested purely with hand-built fakes.
- **Task 1 uses the low-level MCP `Server`, not the high-level
  `MCPServer`.** On the installed SDK version (`mcp==2.1.1`), the
  high-level server's tool dispatch (`_handle_call_tool`) deliberately
  converts every exception except `MCPError` — including a Pydantic
  `ValidationError` from a tool's own declared schema — into a successful
  `CallToolResult(is_error=True)`, matching the current MCP specification's
  convention that tool execution failures are reported in-band. That is the
  opposite of this assessment's explicit requirement to reject invalid
  input with standard JSON-RPC error codes. The low-level `Server` has no
  such interception: an `MCPError` raised from the `on_call_tool` handler
  propagates to the SDK's JSON-RPC dispatcher and is serialized as a
  genuine protocol-level `error` response.
- **Task 4 rate limiting is a sliding-window log, not a fixed-window
  counter.** Every accepted request is recorded as
  `(tenant_key, timestamp, tokens)`; a check sums a tenant's tokens within
  the trailing window and evicts expired rows on every call. A fixed-window
  counter was rejected because a client can burst up to roughly double the
  limit's worth of tokens across a window boundary under that scheme.
- **Task 3's redactor holds back a fixed 64-character tail rather than
  buffering the response or redacting each chunk independently.**
  Redacting each chunk in isolation silently misses any pattern split
  across a chunk boundary, which the mock's `pii` behavior is designed to
  exercise. Buffering the full response would satisfy correctness but
  violate the task's explicit memory and latency requirements. The
  holdback size must be at least as large as the longest pattern matched
  (credit card, ~19 characters with separators); 64 was chosen with margin
  for realistic email lengths while keeping the added latency small.
- **Task 4's `RouterConfig` is a dependency, not a module-level constant.**
  Primary URL, secondary URL, and timeout are wrapped in a small dataclass
  exposed through a FastAPI dependency (`get_router_config`), matching the
  same override pattern already used for the HTTP client and the rate
  limiter, so tests substitute configuration the same way everywhere in the
  repository rather than mutating global state.

## Protocol and security decisions

- **JSON-RPC protocol errors vs. in-band tool errors (Task 1).**
  Schema-invalid input or an unknown tool name is a protocol error
  (`MCPError`, JSON-RPC `-32602 Invalid params`). A well-formed but
  unfulfillable request (valid `customer_id` format, not present in the
  fixture store) is a normal, successful result with
  `CallToolResult(is_error=True)`, since the request itself was valid. Only
  the first category uses a protocol-level error.
- **Fail-closed authorization (Task 2).** An `admin_*` tool call is denied
  by default; only an explicit `admin` role admits it. A denied call is
  intercepted before any downstream request is constructed — the
  downstream server is never contacted for it, including inside a batch
  that also contains authorized calls.
- **Authorization rule generalized beyond the two named methods (Task
  2).** The specification's example is "`tools/list` forwards
  transparently, `tools/call` to `admin_*` needs admin role." Implemented
  as "every method forwards except an unauthorized `admin_*` tool call":
  blocking every method that is not literally `tools/list` would break a
  real client's `initialize`/`ping` handshake, which the specification does
  not intend to block.
- **Redaction operates on extracted content, not raw bytes (Task 3).**
  The gateway parses each upstream SSE event and redacts only the
  extracted `delta.content` text, then re-wraps the redacted text in a new
  SSE frame. Redacting raw bytes across event boundaries would risk a
  match spanning two separate JSON envelopes and corrupting one.
- **Error sanitization on failover (Task 4).** All failure paths in
  `complete_with_failover` funnel through a single `RouterError`, whose
  message is the only thing that reaches the client. Upstream status
  codes, connection errors, and other internal detail are logged
  server-side and never included in the response body.
- **stdout is reserved for protocol output (Task 1, all HTTP services by
  extension).** All logging goes through the `logging` module configured
  onto `sys.stderr`; no code path uses bare `print()`. The stdio
  transport's own `stdio_server()` additionally diverts the process's real
  file descriptor 1 to stderr for the duration of the connection and writes
  the JSON-RPC wire through a separate, private descriptor, so the
  convention is enforced structurally, not only by discipline.

## Testing conventions

- **Tests live alongside the module they cover**, written in the same
  change that introduces the module. `tests/<package>/test_<module>.py`
  mirrors `src/<package>/<module>.py`; the same pattern applies to
  `mocks/` under `tests/mocks/`.
- **`tests/__init__.py` is required and must stay.** Without it, pytest's
  default "prepend" import mode treats `tests/mocks/` as *the* `mocks`
  package (since `tests/` would otherwise be the first directory without an
  `__init__.py`, and the dotted module name is built from there down),
  shadowing the real top-level `mocks/` package and breaking
  `from mocks.foo import ...` in test files with a confusing
  `ModuleNotFoundError`. The same risk applies to any future
  `tests/<x>/` directory whose name collides with a real top-level package.
- **Four verification layers, kept distinct**: unit tests (a single
  function or class), integration tests (a full FastAPI app in-process
  against an in-process mock via `httpx.ASGITransport`), live multi-process
  checks (real `uvicorn` processes exercised with `curl` or a real
  subprocess), and the smoke script (`scripts/smoke.sh`, both mocks started
  as real processes and checked directly). An in-process test client does
  not fully substitute for exercising real, separate processes over a real
  transport — this repository caught two defects only that way (see dated
  log below): a shell-scripting bug in `smoke.sh`'s `check()` invocation,
  and a PII redaction marker split across two SSE frames.
- **Manual verification commands are kept accurate to the actual running
  configuration** — real ports, real environment variable names, no
  placeholder values — since a command that does not match the code is
  worse than no command. See `README.md` for the current set.

## Dated design decisions

- 2026-09-04 — Repository scaffolded: package-per-task layout, both mock
  upstreams built first, `tests/__init__.py` requirement discovered and
  fixed (see Testing conventions). Full harness verified end to end.
- 2026-09-04 — Task 1 implemented on `mcp==2.1.1`. `FastMCP` no longer
  exists in this major version (renamed to `mcp.server.mcpserver.MCPServer`
  with materially different tool-error behavior); the low-level `Server`
  was used instead so that schema-invalid input surfaces as a JSON-RPC
  protocol error rather than an in-band tool result — see Architecture
  decisions and Protocol and security decisions above. Verified with a real
  subprocess stdio session (`tests/mcp_server/test_stdio_e2e.py`), not only
  handler-level unit tests, since only a real subprocess can confirm what
  actually reaches the process's stdout.
- 2026-09-06 — Task 2 implemented. Bearer-token-to-role mapping documented
  as a static stand-in (`mcp_gateway/auth.py`); authorization rule
  generalized beyond the specification's two named methods; batch support
  extended to the gateway since the downstream mock already supports it,
  with per-item authorization proven by a test asserting a blocked call
  inside a mixed batch never reaches the downstream server.
- 2026-09-07 — Task 3 implemented: bounded holdback-buffer redactor (see
  Architecture decisions). A defect was found only by running the gateway
  against the real mock over actual HTTP, not by the in-process test
  suite: the naive `buffer[:-HOLDBACK]` cut could land inside the literal
  `[REDACTED]` marker itself, splitting it across two SSE emissions (for
  example, one event ending `"...[REDA"` and the next starting
  `"CTED]..."`). Not a PII leak — the fully concatenated stream was always
  correct, which is why the existing reassembly-based tests passed despite
  the defect — but a marker torn across two emissions is incorrect on the
  wire for any consumer that does not reassemble first. Fixed by pulling
  the split point back before any `[REDACTED]` occurrence it would
  otherwise cut through, with a regression test added asserting no
  emission ends or starts with a partial marker fragment.
- 2026-09-07 — Task 4 implemented, the last of the four. Token cost is
  caller-declared (`max_tokens`) rather than estimated from prompt length,
  since no tokenizer is in scope and an estimate would not account for
  response tokens. The rate limiter's per-tenant `asyncio.Lock` closes a
  check-then-act race under concurrency, validated by a test firing 30
  concurrent requests at a 1,000-token budget and asserting the accepted
  total is exactly 1,000. Primary/secondary configuration was initially
  wired as module-level globals that tests mutated directly; refactored to
  a `RouterConfig` dataclass behind its own dependency before being
  considered complete, for consistency with the override pattern used
  elsewhere in the repository. Failover was verified with three separate
  live processes (primary, secondary, router): a primary configured to
  sleep 2 seconds against a 1-second timeout correctly failed over in
  approximately 1.1 seconds wall-clock.

## Known limitations

- The MCP gateway's Bearer-token-to-role mapping is a static lookup table,
  a stand-in for a real identity provider, documented in
  `mcp_gateway/auth.py` and swappable without touching the authorization
  logic.
- Both mock upstreams are test infrastructure, not reference
  implementations of a real MCP server or LLM provider.
- Task 4 accepts `max_tokens` as the declared cost of a request because no
  tokenizer is in scope; the rate limiter enforces budget against that
  declared value, not a computed one.
- Task 4's router collects the full completion from a provider rather than
  streaming it back to the client — its evaluation criteria are about
  rate-limiter accounting and failover mechanics, not streaming delivery,
  which Task 3 already demonstrates.
- The rate limiter's primary/secondary URLs and timeout are read from
  environment variables once at process start; there is no per-request
  override of upstream target, unlike the Task 3 gateway's forwarding of
  the mock's own behavior-selection header and query parameter.
