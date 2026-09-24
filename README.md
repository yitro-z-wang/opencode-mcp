# opencode-mcp

**English** | [中文](README.zh-CN.md)

Drive an **opencode** session from any MCP client: create sessions, send prompts, collect results, answer permission requests and forms, and route across local and remote opencode servers. Zero dependencies — one Python file, standard library only.

## Why this exists

Until agents commonly speak ACP (Agent Client Protocol) directly to opencode, driving opencode over its **HTTP API** through MCP is the best available route for an agent to operate opencode — and this server is built for exactly that: every tool maps to a clean, machine-consumable state machine (authoritative terminal states, blocking interaction states, incremental cursors) rather than a UI replica, which makes it a native fit for agent orchestration.

Tested in real use with:

- **opencode v2.0.12** — the development baseline; every capability is live-verified against it
- **a real remote instance** — full end-to-end over the network (connect → create → chat → manual permission → reply → wait → incremental fetch → disconnect)
- **the Hermes agent gateway** — mounted as a tool provider and used in production
- **the oh-my-opencode-slim agent orchestration framework** — hosts the MCP and drives nested opencode sessions through it

## Scope

This is **not** a complete control surface for every aspect of opencode, and it does not try to be. It is deliberately scoped to what an agent actually needs for day-to-day opencode interaction:

- create and resume sessions, send prompts, collect results
- handle the two blocking interactions — permission requests and forms
- manage context (usage inspection, compaction) and session lifecycle
- route across multiple opencode servers (local and remote)

Management-plane surfaces — filesystem, credentials, providers, plugins, terminals, config — are intentionally out of scope. Fewer tools, sharper semantics, less to get wrong.

## Install with your LLM

Paste this into your coding agent:

```text
Install and register opencode-mcp for me:

1. Clone: git clone https://github.com/yitro-z-wang/opencode-mcp ~/opencode-mcp
2. Verify: run `python3 ~/opencode-mcp/test_client.py` — it must report 16 tools and pass.
3. Register with opencode: `opencode mcp add opencode-local -- python3 ~/opencode-mcp/server.py`
4. Reload opencode config, then start a new session and confirm the 16 opencode-local tools are available.

Requirements: Python 3.10+ and the opencode CLI on PATH (v2.0.12 is the development baseline).
Report any errors verbatim; do not retry blindly.
```

Or register it yourself: `opencode mcp add opencode-local -- python3 /path/to/server.py` (any MCP client works; the server speaks stdio).

## Features

- **Zero dependencies** — pure Python 3 standard library, one file, no build step.
- **Multi-server** — an MCP-spawned local `opencode serve` (random port + password, dies with the MCP) or an explicit `OPENCODE_URL`; remotes registered at runtime; sessions route to their own server automatically.
- **Authoritative state, no guessing** — terminal states come from opencode's session outcome, not message-shape heuristics.
- **Multi-agent aware** — delegated subagents cannot be mistaken for "done", and their permission requests are surfaced (see the docs).
- **Failure classification** — every failure is classified `[availability] / [compatibility] / [other]`, with the raw error preserved for reporting.
- **Safe defaults** — remote `chat` defaults to manual permission approval; `connect_server` only accepts a plaintext password in the call (no file/env reads by the MCP caller) and rejects non-public or non-http(s) URLs.
- **Composable primitives** — `wait_session` (pure state) and `get_messages` (incremental cursor) separate waiting from reading.
- **Production-grade runtime** — concurrent request handling, MCP-standard cancellation, bounded waits.

## Tools

| Tool | What it does |
| --- | --- |
| `create_session` | Create a session (optional title / agent / model / location) |
| `chat` | Send a prompt and wait for the result; attachments; `steer` / `queue` delivery; can auto-answer permissions |
| `wait_session` | Pure state wait: `succeeded` / `failed` / `interrupted` / `needs_permission` / `needs_form` / `timeout` |
| `get_messages` | Read the transcript; incremental pulls via `after_message_id` |
| `permission_reply` | Answer a permission request: `once` / `always` / `reject` |
| `form_reply` | Submit a form answer keyed by field |
| `list_agents` | List agents and their resolved default models (read-only) |
| `interrupt` | Stop the current generation |
| `pending_interactions` | Non-blocking check for pending permissions / forms |
| `list_sessions` | Enumerate / search sessions — the resume handle for earlier conversations |
| `compact` | Compact context and wait for completion |
| `get_context` | Token / cost usage and session metadata |
| `delete_session` | Delete a session (irreversible; cascades to child sessions) |
| `connect_server` | Register and validate a remote opencode connection |
| `list_servers` | List connections with version and baseline status |
| `disconnect_server` | Remove a dynamically registered remote connection |

All tools accept an optional `server` parameter; calls carrying a `session_id` are routed automatically to the connection that owns that session. Both `chat` and `wait_session` take `wait_for_subagents` — the default differs between them, see the docs.

## Docs

- [Connection model, environment variables, cancellation](docs/connection-model.md) · [中文](docs/connection-model.zh-CN.md)
- [Sessions, agents and model selection](docs/sessions.md) · [中文](docs/sessions.zh-CN.md)
- [Permission and form flows, permission rules](docs/interactions.md) · [中文](docs/interactions.zh-CN.md)
- [Subagent-aware waiting in multi-agent sessions](docs/subagent-waiting.md) · [中文](docs/subagent-waiting.zh-CN.md)
- [Tool reference](docs/tools.md) · [中文](docs/tools.zh-CN.md)
- [Verified flows and testing](docs/verification.md) · [中文](docs/verification.zh-CN.md)
- [Live scenario test suites](tests/README.md) · [中文](tests/README.zh-CN.md)
- [Design record: remote connections](DESIGN-remote-connections.md) · [中文](DESIGN-remote-connections.zh-CN.md)
