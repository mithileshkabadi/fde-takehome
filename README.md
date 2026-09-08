# fde-takehome

Assessment submission for a Forward Deployed Engineer (FDE) / AI Integration
Engineer role. The system is a small AI integration gateway that
demonstrates four capabilities commonly required when putting MCP servers
and LLM providers behind a controlled edge: strict tool-input validation
over stdio, role-based authorization for tool calls, real-time PII
redaction on a streaming LLM response, and tenant-aware rate limiting with
provider failover.

Each capability is implemented as an independent, runnable service backed
by its own test suite, plus two mock upstreams that stand in for a real MCP
server and a real LLM provider so the gateways have something concrete to
run against.

## 1. Project Overview

The repository implements four components against a single assessment
specification:

- An **MCP server** (Task 1) that exposes two tools over the stdio
  transport, with strict Pydantic validation and JSON-RPC-correct error
  behavior.
- An **MCP security gateway** (Task 2) that sits in front of an MCP server
  and enforces role-based authorization on tool calls, without weakening
  the underlying JSON-RPC contract.
- An **LLM gateway** (Task 3) that proxies a streaming completion and
  redacts PII from the response in real time, without buffering the full
  response.
- A **rate limiter and failover router** (Task 4) that enforces a
  per-tenant token budget and fails over between two model providers.

The scope is intentionally narrow: each task is implemented to satisfy its
stated requirements and evaluation criteria, backed by mocks and tests, not
extended into a general-purpose platform. There is no persistent identity
provider, no real LLM backend, and no production deployment tooling — those
are explicitly out of scope and called out in [Known Scope /
Limitations](#13-known-scope--limitations).

## 2. Architecture

Tasks 1–3 sit on one request path (an MCP or LLM client talking through a
gateway to a mock upstream); Task 4 is a separate, self-contained routing
path with its own persistence.

```
                                  ┌──────────────────────┐
                                  │       AI / Client      │
                                  └────────────┬────────────┘
              ┌────────────────────────────────┼────────────────────────────────┐
              │                                 │                                 │
              ▼                                 ▼                                 ▼
   ┌───────────────────────┐       ┌───────────────────────┐       ┌───────────────────────┐
   │   MCP Server (Task 1)  │       │  MCP Gateway (Task 2)  │       │  LLM Gateway (Task 3)  │
   │   stdio / JSON-RPC      │       │  HTTP :8010             │       │  HTTP :8020             │
   │   get_customer_record   │       │  Bearer token -> role   │       │  POST /v1/completions   │
   │   trigger_refund        │       │  authorize tools/call   │       │  SSE + PII redaction    │
   └───────────────────────┘       └────────────┬────────────┘       └────────────┬────────────┘
                                                  │ authorized calls only            │ proxied SSE stream
                                                  ▼                                   ▼
                                       ┌───────────────────────┐         ┌───────────────────────┐
                                       │   Mock MCP Server       │         │  Mock LLM Provider      │
                                       │   HTTP :9001             │         │  HTTP :9002              │
                                       └───────────────────────┘         └───────────────────────┘
```

```
                                  ┌──────────────────────┐
                                  │       AI / Client      │
                                  └────────────┬────────────┘
                                                │ POST /v1/completions, max_tokens
                                                ▼
                                   ┌────────────────────────┐
                                   │  Rate Limiter / Router   │
                                   │  (Task 4)  HTTP :8030     │
                                   └──────┬────────────┬──────┘
                    tenant budget check,   │            │  primary first, then
                    before any call        │            │  secondary on 429 or >3s timeout
                                           ▼            ▼
                              ┌───────────────────┐  ┌───────────────────────┐
                              │  SQLite             │  │  Mock LLM Provider      │
                              │  rate_limiter.db     │  │  primary   :9002         │
                              │  per-tenant token log│  │  secondary :9003         │
                              └───────────────────┘  └───────────────────────┘
```

**Request flow, MCP path:** a client sends JSON-RPC to the MCP gateway
(`:8010`); `tools/list` and any non-admin `tools/call` are forwarded
unchanged to the mock MCP server (`:9001`); a `tools/call` targeting an
`admin_*` tool is checked against the caller's role first, and only
forwarded if that role is `admin` — otherwise the gateway returns an error
itself and the mock server is never contacted.

**Request flow, LLM path:** a client sends a completion request to the LLM
gateway (`:8020`), which proxies it to the mock LLM provider (`:9002`) and
streams the SSE response back, rewriting each chunk's text through a
redaction filter before it reaches the client.

**Request flow, rate-limited path:** a client sends a completion request
with a declared `max_tokens` cost to the router (`:8030`). The router
checks and records that cost against the tenant's sliding 60-second budget
in SQLite before contacting any provider. If the budget allows it, the
router calls the primary provider (`:9002`); on a 429 or a timeout past 3
seconds it retries once against the secondary (`:9003`).

## 3. Assessment Task Mapping

| Task | Component | What it demonstrates | Verification |
|---|---|---|---|
| 1 | `src/mcp_server/` | MCP tool serving over stdio, strict input validation, protocol-correct JSON-RPC error codes | `tests/mcp_server/` (36 tests) — model-level, handler-level, and a real subprocess stdio session |
| 2 | `src/mcp_gateway/` | MCP authorization: Bearer-token role mapping, method-level tool-call filtering, local interception | `tests/mcp_gateway/` (19 tests) — plus live HTTP verification against a running downstream |
| 3 | `src/llm_gateway/` | LLM guardrail: real-time PII redaction over an SSE stream, bounded buffering | `tests/llm_gateway/` (13 tests) — plus live streaming verification |
| 4 | `src/rate_limiter/` | Token-aware rate limiting, SQLite-backed sliding window, primary/secondary failover | `tests/rate_limiter/` (21 tests) — plus a live three-process verification (primary, secondary, router) |

The supplied assessment specification's overview line states "5 practical
technical tasks," but only four tasks are detailed in the document. This
repository implements the four detailed tasks. No separate fifth task was
found in the supplied material.

## 4. Task 1 — Custom MCP Server

`src/mcp_server/` runs over the stdio transport and exposes two tools:

- `get_customer_record(customer_id)` — looks up a customer by ID.
- `trigger_refund(customer_id, amount, reason)` — records a refund.

**Validation** (`src/mcp_server/models.py`): both tools validate their
arguments with Pydantic models built with `extra="forbid"`, so an
unrecognized field is rejected rather than silently ignored.
`customer_id` must match `^CUST-\d{5}$`; `amount` must be greater than
zero; `reason` must be at least 10 characters. Every constraint is
expressed as a `Field`, so it round-trips into the JSON Schema returned by
`tools/list` via `model_json_schema()` — the schema a client sees is the
same schema enforced server-side.

**JSON-RPC behavior**: the server is built on
`mcp.server.lowlevel.Server` rather than the SDK's higher-level
`MCPServer`. The higher-level server treats a tool-argument schema failure
as an in-band tool execution error (`CallToolResult(is_error=True)`), which
matches the current MCP specification's convention for tool errors but not
this assessment's explicit requirement to reject invalid input with
"standard MCP JSON-RPC error codes." The low-level server has no such
interception: `mcp_server/server.py` validates arguments itself and raises
`mcp.shared.exceptions.MCPError(code=INVALID_PARAMS)` (`-32602`) on a
schema failure or an unknown tool name, which the SDK's JSON-RPC dispatcher
serializes as a genuine protocol-level `error` response.

A well-formed but nonexistent `customer_id` (valid format, not in the
in-memory fixture store) is handled differently: the request itself was
valid, so it returns a normal, successful result with
`CallToolResult(is_error=True)` and an explanatory message, rather than a
protocol error. Only schema-invalid input and unknown tool names produce a
JSON-RPC `error` object.

**stdout/stderr separation**: all logging goes through the `logging` module
configured onto `sys.stderr`; no code path uses bare `print()`. The SDK's
own `stdio_server()` transport additionally diverts the process's real file
descriptor 1 to stderr for the duration of the connection and writes the
JSON-RPC wire through a separate, private descriptor — so even a stray
write to stdout from a dependency would land on stderr, not the wire.

**Testing**: `tests/mcp_server/test_models.py` covers schema edge cases
(malformed customer IDs, boundary values for amount and reason length,
rejected extra fields). `test_server.py` exercises the tool handlers
directly, asserting the JSON-RPC error code for invalid input.
`test_stdio_e2e.py` spawns the server as a real subprocess and drives it
over actual stdin/stdout pipes — the only way to verify that stdout carries
nothing but JSON-RPC, since a unit test calling handlers directly cannot
observe what actually reaches the process's stdout.

## 5. Task 2 — MCP Security Gateway

`src/mcp_gateway/` is an HTTP/JSON-RPC reverse proxy in front of an MCP
server, enforcing a single authorization rule at the tool-call boundary.

**Bearer token → role**: `src/mcp_gateway/auth.py` reads the incoming
`Authorization: Bearer <token>` header and maps the token to a role through
a static lookup table (`admin-token` → `admin`, `viewer-token` → `viewer`).
A missing header, a malformed scheme, or an unrecognized token all resolve
to no role.

**Authorization rule**: `tools/list`, and every other method, is forwarded
to the downstream server unchanged. A `tools/call` whose `params.name`
starts with `admin_` is checked against the caller's role; if the role is
not `admin`, the gateway intercepts the call locally and returns JSON-RPC
error `-32001 Unauthorized Tool Call` — the downstream server is never
contacted for that call. This is a fail-closed design: the default for an
`admin_*` tool is denial, not permission.

**Batch handling**: the downstream mock supports JSON-RPC batch arrays, so
the gateway does too. Each item in a batch is authorized independently;
only the authorized subset is forwarded downstream in a single call, and
responses are merged back into the original order. A blocked call inside a
batch does not block the other items in that batch, and — like a
standalone blocked call — never reaches the downstream server.

**Why unauthorized calls never reach downstream**: authorization happens
before any downstream request is constructed. The gateway partitions
incoming items into a "forward" set and a "local response" set first, and
only the forward set is sent onward; a blocked item's response is
synthesized locally from the `-32001` error, never from a downstream call.

**Testing**: `tests/mcp_gateway/test_auth.py` covers token-to-role mapping
edge cases. `tests/mcp_gateway/test_app.py` verifies proxy behavior with a
spy client wrapping an in-process instance of the mock downstream, so tests
assert that an unauthorized call *never reached* the downstream server —
not only that the caller received the right error. This was additionally
verified live: two separate processes (the mock downstream on `:9001` and
the gateway on `:8010`) were run and exercised over real HTTP with `curl`
for the unauthorized, viewer, and admin-authorized cases (commands in
[Manual Verification Guide](#11-manual-verification-guide)).

## 6. Task 3 — LLM Streaming PII Guardrail

`src/llm_gateway/` proxies `POST /v1/completions` to an LLM provider and
streams the response back over SSE, redacting emails, SSNs, and credit card
numbers as `[REDACTED]` in real time.

**Streaming mechanics**: the gateway opens a streaming request to the
upstream provider and iterates its SSE lines as they arrive
(`httpx.Response.aiter_lines()`). For each `data: ` line that is not the
terminal `[DONE]` marker, it extracts `choices[0].delta.content` — the
actual text delta — and feeds only that text into the redactor. Redaction
is applied to this extracted text stream, not to the raw SSE bytes, so a
match can never straddle and corrupt a JSON envelope between two separate
events.

**Bounded streaming state**: `src/llm_gateway/redactor.py` implements
`PiiStreamRedactor` with a fixed 64-character "holdback" buffer. Each
`feed()` call re-scans only the held-back tail plus the newly arrived
chunk — never the whole response so far — and emits everything except the
last 64 characters, which might still be the start of an in-progress
match; `flush()` releases that final tail once the stream ends. Buffer size
is therefore bounded by a constant, not by response length.

**Handling PII split across chunk boundaries**: the mock LLM provider's
`pii` behavior deliberately splits each PII pattern across two SSE chunks
to exercise this case. One concrete example, taken directly from
`mocks/llm_provider.py`'s `PII_CHUNKS` fixture:

```
chunk 1: "Contact John at john.doe@exam"
chunk 2: "ple.com or call regarding SSN 123-45-"
chunk 3: "6789. His card 4111-1111-1111-"
chunk 4: "1111 was charged. Thanks!"
```

The email `john.doe@example.com` is split between chunks 1 and 2, the SSN
`123-45-6789` between chunks 2 and 3, and the credit card number
`4111-1111-1111-1111` between chunks 3 and 4. Because the redactor holds
back the last 64 characters rather than redacting each chunk in isolation,
all three patterns are still fully matched once their completing
characters arrive, and the client-visible stream reads: `"Contact John at
[REDACTED] or call regarding SSN [REDACTED]. His card [REDACTED] was
charged. Thanks!"` — with no raw email, SSN, or credit card number ever
appearing in any individual SSE event sent to the client.

**Why full-response buffering is avoided**: the task requires the gateway
not accumulate the full response in memory and to minimize time-to-first-
token. Buffering the entire response before redacting would satisfy
correctness but defeat both requirements; the 64-character holdback bounds
memory to a small constant and lets safe content flow to the client as
soon as it is confirmed safe, rather than only at the end of the stream.

**Non-streaming failure passthrough**: a non-200 upstream response (for
example, the mock's `rate_limited` behavior, a plain JSON 429 body) is
detected before the gateway commits to a streaming response, and is passed
through to the client unchanged rather than being wrapped in SSE framing.

**Testing**: `tests/llm_gateway/test_redactor.py` tests the redactor in
isolation, including a regression test asserting that the literal
`[REDACTED]` marker itself is never torn across two emissions (an earlier
implementation could split the marker at the holdback boundary; concatenated
output was always correct, but the wire-level artifact was fixed once
observed in a live run — see `CLAUDE.md`). `tests/llm_gateway/test_app.py`
exercises the gateway end-to-end against an in-process mock, asserting that
no raw PII appears in any individual wire event, not only in the final
reassembled text.

## 7. Task 4 — Rate Limiting and Model Failover

`src/rate_limiter/` gates `POST /v1/completions` on a per-tenant token
budget before contacting any model provider, then routes the request
through a primary provider with automatic failover to a secondary.

**Tenant identification**: the tenant key is taken directly from the
`Authorization: Bearer <token>` header (the token value itself, not a role
lookup); a request with no such header is tracked under the tenant key
`anonymous`.

**`max_tokens` as request cost**: the caller declares the token cost of a
request via a `max_tokens` field in the request body, mirroring the
convention used by common completion APIs. No tokenizer is in scope for
this assessment, so the request states its own cost rather than the
gateway estimating one from prompt length. A missing or non-positive
`max_tokens` is rejected with a 400 before any rate-limit check runs.

**Sliding window, not fixed window**: `src/rate_limiter/limiter.py`
implements `TokenRateLimiter` as a sliding-window *log*: every accepted
request is recorded as `(tenant_key, timestamp, tokens)`, and a check sums
a tenant's tokens within the trailing 60-second window, evicting expired
rows on every call. This avoids the standard fixed-window counter bug,
where a client can burst up to roughly double the limit's worth of tokens
across a window boundary. The default budget is 50,000 tokens per minute
per tenant (`RATE_LIMIT_TOKENS_PER_MINUTE`).

**SQLite persistence and eviction**: usage is persisted on disk in a
`token_usage` table (default path `rate_limiter.db`), indexed on
`(tenant_key, ts)`. Each check first deletes rows older than the window for
that tenant, then sums what remains, so state does not grow unbounded over
time.

**Concurrency protection**: the check-then-record sequence is a
check-then-act race under concurrency — two requests arriving at the same
time could each read "under budget" before either has recorded its usage,
together admitting more than the limit. Each tenant's calls are serialized
through a per-tenant `asyncio.Lock` (not a single global lock, which would
unnecessarily serialize unrelated tenants against each other). This is
validated directly: a test fires 30 concurrent requests at a 1,000-token
budget in 100-token increments and asserts exactly 10 are accepted and the
recorded total is exactly 1,000, never more.

**Primary/secondary failover**: `src/rate_limiter/router.py` calls the
primary provider first, bounded by a 3-second timeout
(`asyncio.wait_for`, which also handles cancelling the losing attempt on
timeout). A primary response of HTTP 429, or a timeout past 3 seconds,
triggers one retry against the secondary provider — itself bounded by the
same timeout, so a hung secondary cannot hang the request indefinitely
either.

**Sanitized failure behavior**: if both providers fail, the router raises
a single `RouterError` whose message is the only thing that reaches the
client. Upstream status codes, connection errors, and other internal
detail are logged server-side but never included in the response body; the
client receives a generic HTTP 503 with a fixed, sanitized message.

**Dependency-injected configuration**: primary URL, secondary URL, and
timeout are read from environment variables once at process start
(`PRIMARY_MODEL_URL`, `SECONDARY_MODEL_URL`, `FAILOVER_TIMEOUT_S`) into a
`RouterConfig` dataclass, exposed to the endpoint through a FastAPI
dependency (`get_router_config`) rather than as module-level constants read
directly in the handler. This keeps configuration swappable — including in
tests, which override the dependency rather than mutating global state.

**Testing**: `tests/rate_limiter/test_limiter.py` covers budget accounting,
sliding-window eviction (via an injectable clock, so the test does not
depend on wall-clock timing), and the concurrency test described above.
`tests/rate_limiter/test_router.py` exercises 429 and timeout failover
against the real mock provider, and asserts the sanitized error message
when both providers fail. `tests/rate_limiter/test_app.py` verifies the
endpoint end-to-end, including a spy proving a rate-limited request never
reaches a model provider at all. This was additionally verified live with
three separate processes — a primary configured to sleep 2 seconds against
a 1-second failover timeout, a secondary, and the router — which correctly
returned `"model":"secondary"` in approximately 1.1 seconds wall-clock,
consistent with the timeout deadline rather than the primary's full delay.

## 8. Request Flows

**A. MCP normal request** (`tools/list`, or a `tools/call` to a non-admin tool)

```
Client -> MCP Gateway (:8010): POST / {"method":"tools/list"}
MCP Gateway -> Mock MCP Server (:9001): forwarded unchanged
Mock MCP Server -> MCP Gateway: {"result":{"tools":[...]}}
MCP Gateway -> Client: {"result":{"tools":[...]}}
```

**B. MCP unauthorized admin request**

```
Client -> MCP Gateway (:8010): POST / tools/call "admin_reset_key" (Bearer viewer-token)
MCP Gateway: role=viewer, tool requires admin -> intercept, do not forward
MCP Gateway -> Client: {"error":{"code":-32001,"message":"Unauthorized Tool Call"}}
```

**C. LLM PII streaming**

```
Client -> LLM Gateway (:8020): POST /v1/completions?behavior=pii
LLM Gateway -> Mock LLM Provider (:9002): forwarded request
Mock LLM Provider -> LLM Gateway: SSE chunk "Contact John at john.doe@exam"
Mock LLM Provider -> LLM Gateway: SSE chunk "ple.com or call regarding SSN 123-45-"
LLM Gateway: redactor holds back the tail until each match completes
LLM Gateway -> Client: SSE chunk "Contact John at "
LLM Gateway -> Client: SSE chunk "[REDACTED] or call re..."
LLM Gateway -> Client: data: [DONE]
```

**D. Primary 429 → secondary fallback**

```
Client -> Rate Limiter (:8030): POST /v1/completions {max_tokens: 100}
Rate Limiter: tenant budget check passes, usage recorded
Rate Limiter -> Primary (:9002): POST /v1/completions
Primary -> Rate Limiter: HTTP 429
Rate Limiter -> Secondary (:9003): POST /v1/completions
Secondary -> Rate Limiter: HTTP 200 + completion
Rate Limiter -> Client: {"content":"...","model":"secondary",...}
```

**E. Primary timeout → secondary fallback**

```
Client -> Rate Limiter (:8030): POST /v1/completions {max_tokens: 100}
Rate Limiter -> Primary (:9002): POST /v1/completions
Primary: has not responded after 3s (asyncio.wait_for deadline)
Rate Limiter: cancel primary attempt, TimeoutError
Rate Limiter -> Secondary (:9003): POST /v1/completions
Secondary -> Rate Limiter: HTTP 200 + completion
Rate Limiter -> Client: {"content":"...","model":"secondary",...}
```

**F. Rate limit exceeded → upstream not called**

```
Client -> Rate Limiter (:8030): POST /v1/completions {max_tokens: 60000}
Rate Limiter: sliding-window check fails (60000 > 50000 tokens/min budget)
Rate Limiter -> Client: HTTP 429 {"error":{"code":"rate_limit_exceeded",...}}
(no request is made to any model provider)
```

## 9. Security and Reliability Decisions

- **Fail closed for unauthorized admin tools.** The MCP gateway's default
  for an `admin_*` tool call is denial; only an explicit `admin` role
  admits it, and the downstream server is never contacted for a denied
  call.
- **Protocol-safe stdout.** The MCP server never writes to stdout outside
  the SDK's own JSON-RPC serialization; all logging is routed to stderr,
  reinforced by the transport's own fd-diversion behavior.
- **Bounded stream buffering.** The PII redactor holds a fixed-size
  (64-character) buffer regardless of response length, so memory use does
  not scale with the length of a completion.
- **Per-tenant concurrency control.** Rate-limit accounting is serialized
  per tenant with `asyncio.Lock`, closing a check-then-act race without
  serializing unrelated tenants against each other.
- **Upstream error sanitization.** Both the MCP gateway's downstream
  failure path and the rate limiter's router return fixed, generic error
  messages to the client; upstream status codes and connection detail are
  logged server-side only.
- **Timeout-bounded failover.** Both the primary and secondary provider
  calls in the router are bounded by the same timeout, so a hung secondary
  cannot hang a request indefinitely after the primary has already failed.
- **SQLite-backed, on-disk usage state.** Token usage survives a process
  restart, and old usage is evicted on every check rather than retained
  indefinitely.

## 10. Testing and Verification

Four layers of verification exist in this repository, and they are
deliberately kept distinct because different bugs only surface at
different layers — this repository's own design log documents one bug (a
PII marker split across SSE frames) that passed every in-process test but
was only visible when driving the gateway with a real HTTP client against a
separately running process.

- **Unit tests** — exercise a single function or class directly: Pydantic
  model validation (`tests/mcp_server/test_models.py`), token-to-role
  mapping (`tests/mcp_gateway/test_auth.py`), the redactor's holdback logic
  (`tests/llm_gateway/test_redactor.py`), and rate-limiter accounting
  (`tests/rate_limiter/test_limiter.py`).
- **Integration tests** — exercise a full FastAPI app in-process, usually
  against an in-process instance of a mock upstream via
  `httpx.ASGITransport`, so no real sockets are opened: `test_app.py` in
  each of `mcp_gateway`, `llm_gateway`, and `rate_limiter`, plus
  `test_server.py` for the MCP server's handlers and `test_router.py` for
  the failover logic.
- **Live multi-process checks** — the same components run as real,
  separately started `uvicorn` processes and exercised with `curl` or a
  real stdio subprocess, to verify behavior that an in-process test client
  cannot observe (actual stdout isolation, actual wall-clock timeout
  behavior, actual HTTP round trips between independent processes). These
  are captured as commands in [Manual Verification
  Guide](#11-manual-verification-guide); `tests/mcp_server/test_stdio_e2e.py`
  is the one such check that is also automated, since a real subprocess is
  the only way to verify stdout purity.
- **Smoke tests** — `scripts/smoke.sh` starts both mock upstreams as real
  processes and runs eight `curl`-based checks against them directly (not
  against the gateways), verifying the harness itself is sound before
  relying on it as a foundation for the gateway tests.

Current verified results:

```
make test    -> 109 passed
make lint    -> ruff check: all checks passed; ruff format --check: clean
scripts/smoke.sh -> 8 passed, 0 failed
```

Test counts by area: MCP server 36, MCP gateway 19, LLM gateway 13, rate
limiter 21, mocks 20.

## 11. Manual Verification Guide

Commands assume `make install` has been run and use the ports and
environment variables actually read by the code. Each block should be run
in its own terminal; commands are grouped by which processes need to be
running first.

### Task 2 — MCP gateway (authorization)

```bash
# terminal 1
make run-mock-mcp        # mock MCP server on :9001

# terminal 2
make run-mcp-gateway     # gateway on :8010, MCP_DOWNSTREAM_URL defaults to :9001

# terminal 3 — tools/list forwards transparently
curl -s -X POST http://127.0.0.1:8010/ \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# viewer calling a normal (non-admin) tool — allowed
curl -s -X POST http://127.0.0.1:8010/ \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer viewer-token' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"echo","arguments":{"text":"hello"}}}'

# viewer calling an admin tool — blocked with -32001, downstream never contacted
curl -s -X POST http://127.0.0.1:8010/ \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer viewer-token' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{"account_id":"acct-1"}}}'

# admin calling the same admin tool — allowed
curl -s -X POST http://127.0.0.1:8010/ \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer admin-token' \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{"account_id":"acct-1"}}}'
```

### Task 3 — LLM gateway (streaming PII redaction)

```bash
# terminal 1
make run-mock-llm        # mock LLM provider on :9002

# terminal 2
make run-llm-gateway     # gateway on :8020, LLM_UPSTREAM_URL defaults to :9002

# terminal 3 — normal completion, streamed through unchanged
curl -s -N -X POST "http://127.0.0.1:8020/v1/completions?behavior=normal" \
  -H 'Content-Type: application/json' -d '{"prompt":"hi"}'

# PII redaction — email, SSN, and credit card number replaced with [REDACTED]
curl -s -N -X POST "http://127.0.0.1:8020/v1/completions?behavior=pii" \
  -H 'Content-Type: application/json' -d '{"prompt":"hi"}'
```

### Task 4 — rate limiter and failover

The plain `make run-rate-limiter` target uses the default primary
(`:9002`) and secondary (`:9003`) URLs with no per-request behavior
override — unlike the Task 3 gateway, the router does not forward a
`behavior` query parameter to the upstream. To exercise the failure paths,
start the router with `PRIMARY_MODEL_URL` / `SECONDARY_MODEL_URL` pointing
at the mock's behavior-selecting query string directly.

```bash
# terminal 1
make run-mock-llm             # primary mock on :9002

# terminal 2
make run-mock-llm-secondary   # secondary mock on :9003

# terminal 3 — primary succeeds
make run-rate-limiter         # router on :8030
curl -s -X POST http://127.0.0.1:8030/v1/completions \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer tenant-1' \
  -d '{"prompt":"hi","max_tokens":100}'
```

```bash
# terminal 3 — primary 429s, router falls back to secondary
PRIMARY_MODEL_URL="http://127.0.0.1:9002/v1/completions?behavior=rate_limited" \
SECONDARY_MODEL_URL="http://127.0.0.1:9003/v1/completions?behavior=normal" \
.venv/bin/uvicorn rate_limiter.app:app --port 8030

curl -s -X POST http://127.0.0.1:8030/v1/completions \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer tenant-2' \
  -d '{"prompt":"hi","max_tokens":100}'
# -> {"content":"...","model":"secondary","tenant_tokens_used":100}
```

```bash
# terminal 3 — primary is slow (2s), router times out at 1s and falls back
FAILOVER_TIMEOUT_S=1.0 \
PRIMARY_MODEL_URL="http://127.0.0.1:9002/v1/completions?behavior=slow&delay_ms=2000" \
SECONDARY_MODEL_URL="http://127.0.0.1:9003/v1/completions?behavior=normal" \
.venv/bin/uvicorn rate_limiter.app:app --port 8030

time curl -s -X POST http://127.0.0.1:8030/v1/completions \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer tenant-3' \
  -d '{"prompt":"hi","max_tokens":100}'
# -> "model":"secondary", elapsed ~1s (not the primary's 2s delay)
```

```bash
# terminal 3 — a single request declaring more than the 50,000/min budget
make run-rate-limiter
curl -s -X POST http://127.0.0.1:8030/v1/completions \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer tenant-4' \
  -d '{"prompt":"hi","max_tokens":60000}'
# -> HTTP 429 {"error":{"code":"rate_limit_exceeded",...}}, no provider is called
```

```bash
# terminal 3 — both providers unavailable: sanitized 503
PRIMARY_MODEL_URL="http://127.0.0.1:9002/v1/completions?behavior=rate_limited" \
SECONDARY_MODEL_URL="http://127.0.0.1:9003/v1/completions?behavior=rate_limited" \
.venv/bin/uvicorn rate_limiter.app:app --port 8030

curl -s -X POST http://127.0.0.1:8030/v1/completions \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer tenant-5' \
  -d '{"prompt":"hi","max_tokens":100}'
# -> HTTP 503 {"error":{"code":"upstream_unavailable","message":"The model provider is
#    temporarily unavailable. Please try again shortly."}} — no upstream detail present
```

## 12. Design Decisions / Trade-offs

| Decision | Reason | Trade-off |
|---|---|---|
| stdio transport for the MCP server | Required by the assessment; also the standard MCP transport for a locally-invoked tool server | Requires strict output-stream discipline; any stray stdout write corrupts the protocol stream |
| Low-level MCP `Server`, not the high-level `MCPServer` | The high-level server reports schema-invalid input as an in-band tool result, not a JSON-RPC protocol error, which the assessment explicitly requires | More manual wiring of tool dispatch and validation than the high-level API provides |
| Static token → role lookup table | No identity provider is in scope for this assessment | Not representative of a real authentication system; swappable behind the same function signature |
| SQLite instead of an external store | Explicit requirement ("on-disk SQLite"); avoids standing up external infrastructure | Single-process, single-file persistence; not horizontally scalable |
| Sliding-window log instead of fixed-window counter | Avoids the fixed-window bug where usage can burst to roughly double the limit across a window boundary | More storage and per-check work than a fixed-window counter (bounded by eviction, but not free) |
| Per-tenant `asyncio.Lock` for rate-limit accounting | Closes a genuine check-then-act race under concurrency without serializing unrelated tenants | Adds a small amount of coordination overhead per request; locks are held in-process only, not shared across multiple router instances |
| Bounded (64-character) holdback buffer for PII redaction | Bounds memory to a constant regardless of response length and avoids buffering the full response | Nothing can be emitted until more than 64 characters have accumulated, adding a small, fixed amount of latency before the first safe output |
| Dependency-injected `RouterConfig` | Matches the dependency-override pattern already used for the HTTP client and rate limiter, keeping configuration testable | One additional indirection layer versus reading module-level constants directly |
| `max_tokens` declared by the caller | No tokenizer is in scope; the request states its own cost rather than the gateway estimating one | Relies on the caller declaring an honest cost; nothing here validates that the declared cost matches actual usage |

## 13. Known Scope / Limitations

- The MCP gateway's Bearer-token-to-role mapping (`mcp_gateway/auth.py`) is
  a static lookup table, a stand-in for a real identity provider. It is
  documented in the module itself and is swappable without touching the
  authorization logic.
- `mocks/mcp_downstream.py` and `mocks/llm_provider.py` are test
  infrastructure, not reference implementations of a real MCP server or
  LLM provider. Their behavior (fixed tool set, synthetic completions,
  selectable failure modes) exists specifically to give the gateways and
  the rate limiter something deterministic to run against.
- Task 4 accepts `max_tokens` as the declared cost of a request because no
  tokenizer is in scope for this assessment; the rate limiter enforces
  budget against that declared value, not a computed one.
- Task 4's router collects the full completion from a provider rather than
  streaming it back to the client. The assessment's Task 4 evaluation
  criteria are about rate-limiter accounting and failover mechanics, not
  streaming delivery — which Task 3 already demonstrates — so Task 4 was
  scoped to a request/response shape that keeps its own tests focused on
  that behavior.
- The rate limiter's primary/secondary URLs and timeout are read from
  environment variables once at process start; there is no per-request
  override of upstream target, unlike the Task 3 gateway's forwarding of
  the mock's own behavior-selection header and query parameter.

## 14. Running the Project

```bash
make install   # creates .venv (python3.12), installs the package and dev dependencies
make test      # runs the full test suite (109 tests)
make lint      # ruff check + ruff format --check
make smoke     # starts both mocks and runs scripts/smoke.sh against them
```

Services and the ports they listen on:

| Service | Command | Port |
|---|---|---|
| Mock MCP server | `make run-mock-mcp` | 9001 |
| Mock LLM provider (primary) | `make run-mock-llm` | 9002 |
| Mock LLM provider (secondary) | `make run-mock-llm-secondary` | 9003 |
| MCP security gateway | `make run-mcp-gateway` | 8010 |
| LLM gateway | `make run-llm-gateway` | 8020 |
| Rate limiter / router | `make run-rate-limiter` | 8030 |
| MCP server (Task 1) | `make run-mcp-server` | stdio, no port |

## 15. Submission Checklist

- [x] All four detailed tasks implemented (Tasks 1–4)
- [x] Test suite passing (`make test` → 109 passed)
- [x] Lint clean (`make lint` → ruff check and format, no findings)
- [x] Smoke tests passing (`scripts/smoke.sh` → 8 passed, 0 failed)
- [x] README reviewed against the current implementation
- [ ] Repository committed with a clean `git status` immediately before
      submission
- [x] Assessment specification checked for a separate Task 5 — none found;
      see [Assessment Task Mapping](#3-assessment-task-mapping)

## Project Layout

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

See `CLAUDE.md` for the engineering decision log — stack rationale,
protocol-level decisions, and the dated record of trade-offs made while
implementing each task.
