# Connection model: MCP-spawned local + multi-server

**English** | [中文](connection-model.zh-CN.md)

Part of the [opencode-mcp](../README.md) documentation.

**No inferential service discovery (including `service.json`).** The local connection takes one of two forms:

1. **Explicit direct connect**: when `OPENCODE_URL` is set, `local` points at that address (password from `OPENCODE_PASSWORD`, default `opencode`).
2. **MCP-spawned serve** (default): the first time `local` is needed, the MCP spawns its own `opencode serve` — **random high port + random password** injected via `OPENCODE_SERVER_PASSWORD`. It runs as a child process and **dies with the MCP instance**: exactly one serve is spawned per MCP process (a per-process singleton), and it is killed and reaped on stdin EOF (normal host shutdown) and on `SIGTERM` / `SIGINT` / `SIGHUP`. Multiple MCP instances never collide thanks to the random ports — and never accumulate, since each restart cleans up after itself. The one gap is `SIGKILL`, which no process can intercept on any platform: a hard-killed MCP can leave one stale serve behind (it is not adopted later — no inferential discovery, by design). If `opencode` is not on `PATH`, the call fails with an availability error (user environment issue, no retries).

**Multi-server**: `connect_server(name, url, password?)` registers a remote connection (process-lifetime only, never persisted). Security model: the url must be a public `http(s)` endpoint — the call comes from the MCP client (in production an LLM), so an unvalidated URL + a caller-supplied credential would be a one-call SSRF / credential-exfiltration primitive. Validation (applied at registration **and re-applied before every request** on a dynamic connection, which narrows the DNS-rebinding window to a single in-flight request) rejects other schemes, schemeless URLs, control characters, and hosts that are — or resolve to — loopback / private / link-local (including the 169.254.169.254 cloud-metadata range) / reserved / multicast / unspecified IPv4 or IPv6 addresses, including non-standard encodings the system resolver accepts (decimal `2130706433`, hex `0x7f.0.0.1`, octal `0177.0.0.1`, short-form `127.1`) and `localhost`-style names. The hostname check is fail-closed: a name that resolves to a *mixed* public/private record set is rejected (an attacker can order the private record first), as is a set where no record parses as an address. All HTTP traffic goes through a no-redirect opener: CPython's default redirect handler would replay the `Authorization` header to an unvalidated `Location` target, so any 3xx from a registered endpoint fails the request instead of being followed. DoS bounds: every 2xx body is capped at 1 MiB (declared and actual size), HTTP error bodies are capped at 64 KiB, and a cap abort is classified `[other]`, never reclassified as a server failure. Credentials: only a plaintext `password` in the call is accepted; `password_file` / `password_env` are rejected (they would let the caller read arbitrary host files / env vars and have them sent over the network). When no password is given, no `Authorization` header is sent (some remotes accept no auth). A human who wants file/env credentials for a locally run server uses `OPENCODE_URL` / `OPENCODE_PASSWORD` in the process environment, which the operator controls directly.

**Version baseline and failure classification**: every connection is hard-gated at creation (unreachable = `[availability]`; reachable but not an opencode API = `[compatibility]`). After a request failure the version is re-queried and the failure is classified as `[availability] / [compatibility] / [other]` — `other` carries the full original error for reporting. When a server version differs from the development baseline, a single `api_version_warning` is injected into the first tool result touching each (connection, session, version) pair — new sessions see it, the same session is never spammed.

**Permission defaults**: local `chat` defaults to `auto_permission="once"`; **remote connections default to `manual`** (approval must live with the caller); `once/always/reject` can always be chosen explicitly.

### Environment variables

- `OPENCODE_URL`: explicit local address (skips spawning), e.g. `http://127.0.0.1:4096`.
- `OPENCODE_PASSWORD`: HTTP Basic password for the explicit local connection; username is always `opencode`, default password `opencode`.
- `OPENCODE_MCP_WORKERS`: worker threads for request handling, default `4` (set to `1` for strict serialization).
- `OPENCODE_MCP_BASELINE_VERSION`: overrides the development baseline (default `2.0.12`, mainly for testing).

## Concurrency and cancellation

- Each JSON-RPC request is handled in its own worker thread (`OPENCODE_MCP_WORKERS`, default 4). Long-blocking calls such as `chat` / `wait_session` never block other tool calls.
- MCP-standard `notifications/cancelled` is honored: cancelling `chat` / `wait_session` stops polling within 1 second (a single in-flight HTTP request can take up to its 30-second timeout to unwind).
