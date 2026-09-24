# Tools

**English** | [中文](tools.zh-CN.md)

Part of the [opencode-mcp](../README.md) documentation.

This is the per-tool reference for the 16 MCP tools exposed by `opencode-mcp`. Parameter names, required flags, defaults and return fields come from the tool catalog and handlers in `server.py`; where the schema and the handler disagree, the handler wins and the difference is noted inline. Examples are minimal `tools/call` argument objects.

## Shared parameters and conventions

- **`server` (string, optional)** — target connection name. Resolution order is **explicit `server` > session routing > local**:
  - If `server` is set, that connection is used (an unknown name is an error listing the known connections).
  - Otherwise, if `session_id` is present, the connection recorded for that session is used (every tool that touches a session records/refreshes this route).
  - Otherwise `local` is used (spawning a local `opencode serve` if needed).
- `server` is part of the `inputSchema` of the 13 session/data tools. The three connection-management tools (`connect_server`, `list_servers`, `disconnect_server`) do **not** take it: they operate on this process's connection registry directly.
- Sessions are routed per connection; a reply is always sent to the server that owns the session, so a `session_id` alone is usually enough after creation.
- **Failure classification** — tool errors are returned as MCP tool errors (`isError: true`) whose text starts with one of:
  - `[availability]` — the service is unreachable, authentication was rejected (401/403), or the required environment (e.g. `opencode` on `PATH`, an env var) is missing. Action: fix the environment/credentials or retry; it may be transient.
  - `[compatibility]` — the target is reachable but is not (or no longer) the expected opencode API (non-JSON `/api/info`, missing `version`, `GET` 404 on a known endpoint). Action: verify the target/version; retrying the same call will not help. A version that differs from the baseline is reported non-fatally as `api_version_warning` on the result instead.
  - `[other]` — any other failure; the raw error is preserved verbatim. Action: report it to the developer unchanged.
- Long-blocking calls (`chat`, `wait_session`, `compact`) poll once per second and honor MCP `notifications/cancelled`, which returns `status: "cancelled"`.
- `timeout_secs` is accepted by `chat`, `wait_session` and `compact`, default `120`, clamped to `[1, 3600]`.

## Index

| Tool | Purpose |
| --- | --- |
| [`create_session`](#create_session) | Create a session and return its `session_id`. |
| [`chat`](#chat) | Send a prompt and wait for the round to finish or block. |
| [`wait_session`](#wait_session) | Wait for an existing session to reach a terminal or needs-interaction state. |
| [`get_messages`](#get_messages) | Fetch formatted message history, optionally incrementally. |
| [`permission_reply`](#permission_reply) | Answer one permission request. |
| [`form_reply`](#form_reply) | Submit one form's answers. |
| [`list_agents`](#list_agents) | List agents and their resolved default models. |
| [`interrupt`](#interrupt) | Cancel the generation currently running in a session. |
| [`pending_interactions`](#pending_interactions) | Non-blocking check for pending permissions and forms. |
| [`list_sessions`](#list_sessions) | Enumerate/search sessions with ordering and pagination. |
| [`compact`](#compact) | Compact a session's context and wait for the result. |
| [`get_context`](#get_context) | Read token/cost usage and session metadata. |
| [`delete_session`](#delete_session) | Delete a session (irreversible, cascades to children). |
| [`connect_server`](#connect_server) | Register and validate a remote connection. |
| [`list_servers`](#list_servers) | List all current connections. |
| [`disconnect_server`](#disconnect_server) | Remove a remote connection. |

## Terminal and subagent payload fields

`chat`, `wait_session` and `compact` share the unified wait core, so their payloads share these fields:

- `status` — one of `succeeded`, `failed`, `interrupted`, `needs_permission`, `needs_form`, `timeout`, `compaction_failed` (compaction gate only), `cancelled`.
- `server` — the connection the call resolved to.
- `session_id` — the session the wait was started on; in `needs_permission` / `needs_form` this is the request's **owning** session, which may be a subagent.
- `root_session_id` — present in the subtree `needs_permission` / `needs_form` payloads; the session the wait started on.
- `last_message_id` — cursor for the next `get_messages(after_message_id=...)` call (may be `null`).
- `time_idle` — the session's `time.idle` value if reported (may be `null`).
- `note` — optional human-readable remark (e.g. "no new replies this round").
- `subagents` — list of subtree nodes excluding the root; each entry has `session_id`, `agent`, `model`, `title`, `outcome`, `active`, `parentID` (any may be `null`).
- `pending_subagents` — count of live subtree nodes, or `null` when activity cannot be verified.
- `subtree_truncated` — `true` if the depth (`3`) or node (`64`) cap cut the tree.
- `subtree_verified` — `true` only when the subtree structure and activity were fully verified. When `false`, the result is fail-closed: `succeeded` is never claimed for delegated work, and a `note` explains why.
- `assistant_text`, `tools_used`, optional `reasoning` — this round's new assistant output; attached by `chat` and `compact` (not by `wait_session`, which is a pure state primitive).

## Tool reference

### create_session

Create a new conversation session and return its `session_id` for later `chat` / `get_messages` calls.

Parameters:

- `title` (string, optional, no default) — session title.
- `agent` (string, optional, no default) — name of the agent to use.
- `model_id` (string, optional, no default) — model as `providerID/modelID`, e.g. `"anthropic/claude-sonnet-4"`; a value without `/` is an error. It is sent as opencode's `Model.Ref` shape `{"providerID", "id"}`.
- `location` (object, optional, no default) — create the session in a given directory/project. Shape is opencode `Location.PublicRef`: `{"directory": "<absolute path>"}`; `directory` is required when `location` is given.
- `server` (string, optional) — see shared parameters.

Returns: `{ "session_id", "server", "title", "agent", "model" }` (fields may be `null` if the server omitted them).

```json
{"name": "create_session", "arguments": {"title": "My task", "model_id": "anthropic/claude-sonnet-4"}}
```

```json
{"name": "create_session", "arguments": {"title": "Work in project", "location": {"directory": "/root/my-project"}}}
```

### chat

Send a prompt to a session and wait for the round to finish or for a blocking interaction.

Parameters:

- `session_id` (string, **required**) — session ID (`ses_...`).
- `text` (string, **required**) — prompt text; an empty or missing value is an error.
- `timeout_secs` (integer, optional, default `120`) — maximum wait, clamped to `[1, 3600]`.
- `wait_for_subagents` (boolean, optional, default `false` for `chat`) — when `true`, `succeeded` additionally requires the whole subagent subtree to be quiescent, and `auto_permission` in `once` / `always` / `reject` is applied to pending permissions of every subtree node. When `false`, subagent state is still reported but does not gate success.
- `auto_permission` (string, optional, enum `once` / `always` / `reject` / `manual`) — permission handling. **Handler default: `once` on the local connection, `manual` on remote connections** (approval must stay with the caller). The schema advertises a flat default of `once`; the handler computes the connection-dependent default. An invalid value is an error.
- `delivery` (string, optional, enum `steer` / `queue`) — `steer` = steer while running (interrupts the current generation direction); `queue` = take effect after this round ends. If omitted the field is not sent; any other value is an error.
- `files` (array, optional, no default) — files attached to the prompt; each item is `{ "uri" (required), "name"?, "description"? }`. A missing `uri` is an error.
- `server` (string, optional) — see shared parameters.

Return: a terminal/subagent payload (see above). `succeeded` carries `assistant_text`, `tools_used`, optional `reasoning`, `last_message_id` and subtree fields; `failed` / `interrupted` are decided outcomes and return immediately; `needs_permission` carries `requests` (`id`, `sessionID`, `action`, `resources`, `save`); `needs_form` carries `forms`; `timeout` carries `partial_text` and a `diagnostics` block (`outcome`, `last_message`, pending counts, active subagents, `suggested_actions`).

```json
{"name": "chat", "arguments": {"session_id": "ses_abc", "text": "List files in the current directory", "timeout_secs": 120, "auto_permission": "once"}}
```

```json
{"name": "chat", "arguments": {"session_id": "ses_abc", "text": "Continue based on the attachment", "delivery": "steer", "files": [{"uri": "file:///root/a.md", "name": "a.md", "description": "reference"}]}}
```

### wait_session

Wait for an existing session to reach a terminal or needs-interaction state; a pure state primitive that returns no message content.

Parameters:

- `session_id` (string, **required**) — session ID (`ses_...`).
- `timeout_secs` (integer, optional, default `120`) — maximum wait, clamped to `[1, 3600]`.
- `wait_for_subagents` (boolean, optional, **default `true`**) — `succeeded` is only reported when the whole subagent subtree is quiescent: no live node and no pending permission/form anywhere in the subtree. When `false`, the legacy behavior applies but subagent state is still reported.
- `server` (string, optional) — see shared parameters.

Return: a status payload. On `succeeded` it adds a `note` telling you to use `get_messages(after_message_id=...)`; it does not include `assistant_text` / `tools_used`. `failed` / `interrupted` are terminal; `needs_permission` / `needs_form` are blocking (`session_id` is the owning session, possibly a subagent); `timeout` carries `partial_text` (this round's new assistant text) and a `diagnostics` block.

```json
{"name": "wait_session", "arguments": {"session_id": "ses_abc", "timeout_secs": 120}}
```

Typical combination: after replying to a permission/form, call `wait_session` to reach a terminal state, then `get_messages(after_message_id=last_message_id)` to pull the new replies.

### get_messages

Fetch formatted session message history in ascending time order, with optional incremental fetching.

Parameters:

- `session_id` (string, **required**) — session ID (`ses_...`).
- `limit` (integer, optional, default `50`) — maximum number of messages returned.
- `after_message_id` (string, optional, no default) — incremental cursor: return only messages after this id. With a cursor, only the latest 200 messages are scanned; if the cursor is not among them, the full list is returned along with a `note`.
- `server` (string, optional) — see shared parameters.

Returns: `{ "server", "session_id", "count", "messages": [...], "last_message_id" }` plus an optional `note`. `last_message_id` is the cursor for the next call. Each message has `id`, `type`, `time`; assistant messages add `agent`, `model`, `text`, optional `reasoning`, optional `tools` (`[{name, status}]`) and `completed`; other messages carry `text` (and `command` for `shell`).

```json
{"name": "get_messages", "arguments": {"session_id": "ses_abc", "limit": 20}}
```

```json
{"name": "get_messages", "arguments": {"session_id": "ses_abc", "after_message_id": "msg_123"}}
```

### permission_reply

Answer one permission request.

Parameters:

- `session_id` (string, **required**) — ID of the session that **owns the request**. When a `needs_permission` payload's `session_id` is a subagent session, use that ID so the reply is routed to the connection owning the subagent.
- `request_id` (string, **required**) — permission request ID (`per_...`).
- `decision` (string, **required**, enum `once` / `always` / `reject`) — `once` allows this time only; `always` allows and saves the rule; `reject` denies. Any other value is an error.
- `message` (string, optional, no default) — optional explanatory note (sent only when non-empty).
- `server` (string, optional) — see shared parameters; normally unnecessary because the owning `session_id` routes the call.

Returns: `{ "ok": true, "server", "session_id", "request_id", "decision" }`.

```json
{"name": "permission_reply", "arguments": {"session_id": "ses_abc", "request_id": "per_xyz", "decision": "once"}}
```

If the request was already handled elsewhere the call may error; re-check with `pending_interactions` or keep waiting with `wait_session`.

### form_reply

Submit the answers for one form.

Parameters:

- `session_id` (string, **required**) — ID of the session that owns the form (use the subagent's ID when it is the owner).
- `form_id` (string, **required**) — form ID (`frm_...`).
- `answer` (object, **required**) — keys are field keys; values may be `string` / `number` / `boolean` / `string[]`. A non-object value is an error.
- `server` (string, optional) — see shared parameters; normally unnecessary because the owning `session_id` routes the call.

Returns: `{ "ok": true, "server", "session_id", "form_id", "answer" }`.

```json
{"name": "form_reply", "arguments": {"session_id": "ses_abc", "form_id": "frm_xyz", "answer": {"name": "foo", "count": 3, "tags": ["a", "b"]}}}
```

### list_agents

List all agents of the target opencode and their resolved default models (read-only).

Parameters:

- `server` (string, optional) — see shared parameters.

Returns: `{ "server", "count", "agents": [ { "name", "mode", "model" } ], "note" }`. `model: null` means the agent has no explicitly configured model and falls back to the position default model at runtime. To pin a session to an agent's model, pass the value yourself as `create_session(model_id="providerID/modelID")`; this tool does not do it for you.

```json
{"name": "list_agents", "arguments": {}}
```

### interrupt

Interrupt the generation currently in progress in a session; useful after `chat` returns `timeout`.

Parameters:

- `session_id` (string, **required**) — session ID (`ses_...`).
- `server` (string, optional) — see shared parameters.

Returns: `{ "ok": true, "server", "session_id" }`.

```json
{"name": "interrupt", "arguments": {"session_id": "ses_abc"}}
```

### pending_interactions

Query, without blocking, the human interactions currently pending in a session and its whole subagent subtree.

Parameters:

- `session_id` (string, **required**) — root session ID (`ses_...`).
- `server` (string, optional) — see shared parameters.

Returns: `{ "server", "session_id", "root_session_id", "permissions": [...], "forms": [...], "subagents", "pending_subagents", "subtree_truncated", "subtree_verified" }`. Permissions are `{id, sessionID, action, resources, save}` (or the per-session fallback shape). Forms are `{id, sessionID, title, fields: [{key, title, type, required, options, description}]}`; only pending forms are listed. If subtree verification is unavailable, it falls back to the root session's own view and reports `subtree_verified: false`.

```json
{"name": "pending_interactions", "arguments": {"session_id": "ses_abc"}}
```

### list_sessions

Enumerate and search existing sessions with keyword, ordering, directory filtering and cursor pagination. The returned `session_id` is the resume handle for `chat`.

Parameters (all optional):

- `search` (string, no default) — keyword matched against title/content.
- `limit` (integer, default `20`) — maximum number of results.
- `order` (string, enum `asc` / `desc`, default `desc`) — sort by update time (default newest first).
- `directory` (string, no default) — filter by working directory.
- `cursor` (string, no default) — pagination cursor, taken from a previous result's `cursor.next`.
- `server` (string, optional) — see shared parameters.

Returns: `{ "server", "count", "sessions": [ { "id", "title", "agent", "model", "parentID", "time": { "updated", "idle"? } } ], "cursor": { "previous", "next" } }`. Missing fields are `null`; `time.idle` is omitted when the server does not report it.

```json
{"name": "list_sessions", "arguments": {"search": "deploy", "limit": 20, "order": "desc"}}
```

### compact

Compact a session's context, wait for the compaction to finish and return the result; useful to proactively trim when the context nears its limit, after which you can keep chatting.

Parameters:

- `session_id` (string, **required**) — session ID (`ses_...`).
- `timeout_secs` (integer, optional, default `120`) — maximum wait, clamped to `[1, 3600]`.
- `server` (string, optional) — see shared parameters.

Returns: `{ "status", "server", "session_id", "time_idle", "last_message_id" }` plus `assistant_text` / `tools_used` (and optional `reasoning`). `status` is `succeeded` (compaction completed) or `compaction_failed` (compaction message `status=failed`), or `timeout`.

Note on schema/handler: the handler also reads an `auto_permission` argument (defaulting like `chat`: `once` locally, `manual` on remote), but the tool catalog does not expose that parameter. Trust the handler: it exists, but callers cannot rely on it being documented/validated.

```json
{"name": "compact", "arguments": {"session_id": "ses_abc", "timeout_secs": 120}}
```

### get_context

Read a session's context usage (`tokens` / `cost`) and metadata, e.g. to decide whether to call `compact`.

Parameters:

- `session_id` (string, **required**) — session ID (`ses_...`).
- `server` (string, optional) — see shared parameters.

Returns: `{ "server", "id", "title", "agent", "model", "parentID", "tokens", "cost", "time": { "updated", "idle" }, "revert" }`. `tokens` / `cost` / `revert` default to `null`; `time.idle` is always present (may be `null`).

```json
{"name": "get_context", "arguments": {"session_id": "ses_abc"}}
```

### delete_session

Delete a session.

> **Irreversible** — a deleted session cannot be recovered.
> **Cascades** — deleting a parent also deletes all of its child sessions (after deletion, a child access returns `404`).

Parameters:

- `session_id` (string, **required**) — ID of the session to delete (`ses_...`).
- `server` (string, optional) — see shared parameters.

Returns: `{ "ok": true, "server", "session_id" }`. The session is also dropped from this process's routing table.

```json
{"name": "delete_session", "arguments": {"session_id": "ses_abc"}}
```

### connect_server

Register and validate a remote opencode connection (valid only within this process; never persisted).

Parameters:

- `name` (string, **required**) — connection alias; `local` is reserved and rejected.
- `url` (string, **required**) — public http(s) endpoint, e.g. `https://host:4096`. Non-http(s) schemes, schemeless URLs, control characters, and hosts that are — or resolve to — loopback / private / link-local (incl. the `169.254.169.254` cloud-metadata range) / reserved / multicast / unspecified IPv4 or IPv6 addresses are rejected (including non-standard encodings the resolver accepts, e.g. `2130706433`, `0x7f.0.0.1`, `0177.0.0.1`, `127.1`, and `localhost`). For a locally run server use the `OPENCODE_URL` / `OPENCODE_PASSWORD` environment variables instead.
- `password` (string, optional, no default) — plaintext password. It is sent in the `Authorization` header to the `url` and also remains in the MCP request stream; prefer short-lived or per-connection passwords.
- No `server` parameter.

Only a plaintext `password` in the call is accepted: `password_file` / `password_env` are rejected, because the caller is an LLM and a file/env read would let it exfiltrate any host file or environment variable over the network. When no `password` is given, no `Authorization` header is sent (some remotes use an empty username/password). Registration performs a creation-time hard gate: unreachable = `[availability]`; reachable but not the opencode API = `[compatibility]`; 401 = `[availability]` (wrong password); a differing version adds an `api_version_warning` instead of failing.

Returns: `{ "name", "url", "local", "source", "version", "baseline", "baseline_check", "password_source", "api_version_warning"? }`. `password_source` is `plaintext` / `none`.

```json
{"name": "connect_server", "arguments": {"name": "build-box", "url": "https://build-box.example.com:4096", "password": "short-lived-per-connection-password"}}
```

### list_servers

List all current connections (local plus dynamic remotes) with their address, source, version and baseline check status.

Parameters: none (not even `server`). Ensures the local connection is ready, spawning the local `serve` if necessary.

Returns: `{ "count", "servers": [ { "name", "url", "local", "source", "version", "baseline", "baseline_check" } ] }`. `source` is `spawned` / `env` / `dynamic`; `baseline_check` is `ok` / `mismatch(<version>)` / `unknown`.

```json
{"name": "list_servers", "arguments": {}}
```

### disconnect_server

Remove a dynamically registered remote connection.

Parameters:

- `name` (string, **required**) — connection alias.
- No `server` parameter.

The local connection cannot be removed (attempting it is an error). No-op/unknown name is an error pointing to `list_servers`. Removing a connection also clears the session routes that pointed at it and kills its spawned process if any.

Returns: `{ "ok": true, "removed": "<name>" }`.

```json
{"name": "disconnect_server", "arguments": {"name": "build-box"}}
```
