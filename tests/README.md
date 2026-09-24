# opencode-mcp live tests

**English** | [中文](README.zh-CN.md)

Live, end-to-end suites for `server.py`. They spawn the real MCP over stdio,
talk to a real `opencode` server and exercise the connection layer, the
permission/form loops, the subagent subtree semantics and the spawned-serve
lifecycle.

## Prerequisites

- Python 3.10 or newer. The suites use the standard library only: no pytest and
  no third-party packages.
- `opencode` on `PATH` for the local/spawn scenarios and for the whole
  lifecycle suite. If you would rather test against an already running server,
  set `OPENCODE_TEST_URL` instead.
- For `live_subagent_test.py`: an agent configuration that can delegate to a
  child session. The suite probes this politely and skips when it cannot.

## Running

Each suite is standalone and prints one line per scenario plus a summary line:

```sh
python3 tests/live_core_test.py
python3 tests/live_subagent_test.py
python3 tests/live_lifecycle_test.py
python3 tests/security_review_test.py   # no opencode server needed
```

The process exit code is `1` only when at least one scenario **fails**. Skips
keep the exit code at `0`, so an environment that cannot support a scenario
never turns into a red build.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `OPENCODE_TEST_URL` | Point the MCP at an existing opencode server (for example `http://127.0.0.1:4096`). When unset, the MCP spawns its own private `serve`. |
| `OPENCODE_TEST_PASSWORD` | Password for `OPENCODE_TEST_URL`. When unset, no password is forwarded. |
| `OPENCODE_TEST_REMOTE_URL` | A remote opencode instance for the remote scenarios. When unset, those scenarios report SKIP. |
| `OPENCODE_TEST_REMOTE_PASSWORD` | Password for `OPENCODE_TEST_REMOTE_URL` (optional; only sent when set). |
| `OPENCODE_TEST_AGENT` | Agent used when creating sessions. When unset, the server's own default is used. |
| `OPENCODE_TEST_MODEL` | Optional model in `providerID/modelID` form. When unset, no model is pinned. |
| `OPENCODE_TEST_TIMEOUT` | Per-scenario wait budget in seconds. Default `60`. |

The helper maps `OPENCODE_TEST_URL` / `OPENCODE_TEST_PASSWORD` onto the MCP's
own `OPENCODE_URL` / `OPENCODE_PASSWORD` when it spawns the server, so the
suite exercises the product's normal env-connection path.

## Coverage

### `live_core_test.py`

- Explicit-env connection vs MCP-spawned local connection (`source=env` vs
  `source=spawned`).
- Failure classification: an unreachable port is an `[availability]` error, a
  reachable non-opencode HTTP endpoint is a `[compatibility]` error, and wrong
  credentials are an `[availability]` error.
- Duplicate connection name and unknown connection name errors; the local
  connection cannot be disconnected.
- Session auto-routing across two registered connections.
- `chat` happy path; `wait_session` terminal state and the incremental
  `get_messages` cursor.
- Manual permission loop (`chat` → `needs_permission` → `permission_reply` →
  `wait_session`); form loop (`pending_interactions` → `form_reply`); automatic
  permission.
- `wait_session` timeout while still generating, and `interrupted` after an
  interrupt.
- `notifications/cancelled` returning promptly and discarding the chat
  response; concurrent `pending_interactions` while a chat is in flight.
- `get_context` and `compact`.
- Remote end-to-end loop (connect → create → chat → manual permission → reply →
  wait → incremental `get_messages` → disconnect), only with the remote env set.

### `live_subagent_test.py`

All scenarios require a delegation-capable environment; each reports SKIP when
no child session appears within the bounded probe window.

- Manual mode: `wait_session(wait_for_subagents=true)` does not report a
  premature `succeeded`; it returns `needs_permission` whose `session_id` is the
  subagent and which carries `root_session_id`; the reply uses the subagent id.
- `chat` default (`wait_for_subagents` unset/false): returns `succeeded` but
  reports `pending_subagents >= 1` and a non-empty `subagents` list.
- Automatic mode: `auto_permission="once"` answers the subagent's request and
  the wait eventually reaches `succeeded`.
- Form ownership: a subagent blocked on a form yields `needs_form` whose
  `session_id` and `forms[].sessionID` are the subagent; `form_reply` uses the
  subagent id.
- Multiple parallel subagents: each pending request is reported with its own
  owning session id.
- Remote (only with the remote env set): `needs_permission` names the remote
  subagent and a `permission_reply` **without** an explicit `server` still
  reaches the right connection.

### `live_lifecycle_test.py`

With a clean environment the MCP spawns its own `serve`. The suite forces that
path via `list_servers`, locates the child process and asserts it is gone within
a few seconds for each shutdown path: stdin EOF, `SIGTERM` and `SIGINT`. The
whole suite reports SKIP when `opencode` is not on `PATH`.

### `security_review_test.py`

Offline verification of the `connect_server` credential / URL policy and the
DoS bounds (response size cap, stdin line cap, worker clamp). It spawns the
MCP over stdio and asserts every rejection **before** any network connection
is attempted, so it runs without an opencode instance:

- `password_file` / `password_env` are rejected (no LLM-directed file/env
  reads) and removed from the tool schema.
- Non-http(s) schemes, schemeless URLs and control characters are rejected.
- Loopback / private / link-local (incl. cloud metadata) / reserved /
  multicast IPv4 and IPv6 literal hosts are rejected, including non-standard
  encodings the system resolver accepts (decimal / hex / octal / short-form);
  public hosts and hostnames pass validation and reach the availability gate.
- Fail-closed DNS: a mixed public+private record set is rejected; a record set
  with nothing parseable as an address is rejected (monkeypatched
  `getaddrinfo`, no real DNS).
- `MAX_RESPONSE_BYTES`, `MAX_STDIN_LINE`, `MAX_WORKERS` exist and behave.

### `regression_review_test.py`

Second-round regression suite (independent review, 2026-09-24). Uses local
`ThreadingHTTPServer` instances on 127.0.0.1 only (plus monkeypatched DNS for
unit-level checks); no opencode instance required:

- **[HIGH]** 302-redirect SSRF: a local server that 302-redirects
  `/api/info` to an internal tracking target must **never** be followed —
  the no-redirect opener raises `HTTPError` and the internal target receives
  zero requests (the `Authorization` header is not replayed).
- **[HIGH]** uncapped error body: a 404 with a 2 MiB body is handled with a
  short error (body read capped at 64 KiB); a 200 with a lying 4 MiB
  `Content-Length` is rejected **before** any read.
- **[MED]** cap-abort classification: `check_response_size` raises `[other]`,
  and `_ensure_version` re-raises `OpenCodeError` verbatim (never reclassified
  as `[availability]`).
- **[MED]** re-validation at request time: `http_request` re-runs
  `_validate_remote_url` per request on dynamic connections (narrows the
  DNS-rebinding window to a single in-flight request) and exempts local connections.
- **[LOW]** `_is_disallowed_ip` full table: CGNAT 100.64/10, 6/8, 7/8,
  IPv4-mapped and IPv4-compatible IPv6 unwrapping.
- **[MED]** (final review) minimal spawn env: `_spawn_local_serve` must not
  inherit the full process environment (no `dict(os.environ)`), and must set
  exactly `PATH` + `HOME` + `OPENCODE_SERVER_PASSWORD`.
- **[LOW]** (final review) hostile `notifications/cancelled` with non-dict or
  missing `params` must not kill the stdin reader thread; the MCP process must
  stay alive and still answer `tools/list` afterwards.

## Skip behaviour
- `opencode` not on `PATH` and no `OPENCODE_TEST_URL` → the local scenarios and
  the lifecycle suite report SKIP.
- No `OPENCODE_TEST_URL` → the explicit-env scenario reports SKIP; the
  MCP-spawned scenario still runs.
- No local password available → scenarios that need direct HTTP API access
  (permission loop, form loop, session routing, duplicate name, remote manual
  permission) report SKIP.
- No remote environment → the remote scenarios report SKIP.
- Subagent environment cannot delegate → the subagent scenarios report SKIP
  with a hint to set `OPENCODE_TEST_AGENT` to a delegation-capable agent.

When `OPENCODE_TEST_URL` is not set, the helper discovers the MCP-spawned local
server from `list_servers` and recovers the random child password from the
spawned process environment (best effort) so the direct-API scenarios can run
without any extra setup. When that recovery is not possible, those scenarios
simply SKIP.
