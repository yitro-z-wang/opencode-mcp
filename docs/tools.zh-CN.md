# Tools

[English](tools.md) | **中文**

opencode-mcp [文档](../README.zh-CN.md)的一部分。

这是 `opencode-mcp` 暴露的 16 个 MCP 工具的逐工具参考。参数名、必填标记、默认值与返回字段均取自 `server.py` 中的工具目录与 handler;当 schema 与 handler 不一致时,以 handler 为准,并在行内标注差异。示例均为最小化的 `tools/call` 参数对象。

## 共享参数与约定

- **`server`(string,可选)** —— 目标连接名。解析顺序为 **显式 `server` > 会话路由 > local**:
  - 若设置了 `server`,则使用该连接(未知名称会报错并列出已知连接)。
  - 否则,若存在 `session_id`,则使用为该会话记录的连接(每个触碰会话的工具都会记录/刷新这条路由)。
  - 否则使用 `local`(必要时拉起本地 `opencode serve`)。
- `server` 是 13 个会话/数据工具的 `inputSchema` 的一部分。三个连接管理工具(`connect_server`、`list_servers`、`disconnect_server`)**不接受**该参数:它们直接操作本进程的连接注册表。
- 会话按连接路由;回复总是发往拥有该会话的服务端,因此创建之后通常只给 `session_id` 就够了。
- **失败分类** —— 工具错误以 MCP tool error 形式返回(`isError: true`),其文本以下列之一开头:
  - `[availability]` —— 服务不可达、认证被拒(401/403),或缺少所需环境(例如 `PATH` 上没有 `opencode`、缺少某个环境变量)。处置:修复环境/凭据后重试;它可能是暂时的。
  - `[compatibility]` —— 目标可达,但不是(或不再是)预期的 opencode API(`/api/info` 返回非 JSON、缺少 `version`、对已知端点 `GET` 404)。处置:核实目标/版本;重试同一调用无济于事。版本与基准不一致时,改为在结果上非致命地报告 `api_version_warning`。
  - `[other]` —— 其他任何失败;原始错误原样保留。处置:将错误原样回报开发者。
- 长阻塞调用(`chat`、`wait_session`、`compact`)每秒轮询一次,并遵循 MCP `notifications/cancelled`,返回 `status: "cancelled"`。
- `timeout_secs` 被 `chat`、`wait_session` 和 `compact` 接受,默认 `120`,钳制到 `[1, 3600]`。

## 索引

| 工具 | 作用 |
| --- | --- |
| [`create_session`](#create_session) | 创建会话并返回其 `session_id`。 |
| [`chat`](#chat) | 发送 prompt 并等待本轮结束或阻塞。 |
| [`wait_session`](#wait_session) | 等待已有会话进入终态或需交互状态。 |
| [`get_messages`](#get_messages) | 获取格式化消息历史,可选增量拉取。 |
| [`permission_reply`](#permission_reply) | 答复一个权限请求。 |
| [`form_reply`](#form_reply) | 提交一个表单的答案。 |
| [`list_agents`](#list_agents) | 列出 agent 及其解析后的默认模型。 |
| [`interrupt`](#interrupt) | 取消会话中当前正在进行的生成。 |
| [`pending_interactions`](#pending_interactions) | 非阻塞查询待处理权限与表单。 |
| [`list_sessions`](#list_sessions) | 带排序与分页地枚举/搜索会话。 |
| [`compact`](#compact) | 压缩会话上下文并等待结果。 |
| [`get_context`](#get_context) | 读取 token/成本用量与会话元信息。 |
| [`delete_session`](#delete_session) | 删除会话(不可逆,级联到子会话)。 |
| [`connect_server`](#connect_server) | 注册并验证一个远端连接。 |
| [`list_servers`](#list_servers) | 列出全部当前连接。 |
| [`disconnect_server`](#disconnect_server) | 移除一个远端连接。 |

## 终态与子 agent payload 字段

`chat`、`wait_session` 和 `compact` 共用统一的等待核心,因此它们的 payload 共享以下字段:

- `status` —— 取 `succeeded`、`failed`、`interrupted`、`needs_permission`、`needs_form`、`timeout`、`compaction_failed`(仅压缩门禁)、`cancelled` 之一。
- `server` —— 调用解析到的连接。
- `session_id` —— 等待所起始的会话;在 `needs_permission` / `needs_form` 中,这是该请求的**持有**会话,可能是子 agent。
- `root_session_id` —— 出现在子树的 `needs_permission` / `needs_form` payload 中;等待最初起始的会话。
- `last_message_id` —— 下一次 `get_messages(after_message_id=...)` 调用的游标(可能为 `null`)。
- `time_idle` —— 会话的 `time.idle` 值(若上报)(可能为 `null`)。
- `note` —— 可选的人类可读备注(例如 "no new replies this round")。
- `subagents` —— 不含根节点的子树节点列表;每项含 `session_id`、`agent`、`model`、`title`、`outcome`、`active`、`parentID`(任意项可能为 `null`)。
- `pending_subagents` —— 存活子树节点数,或当活动无法核实时为 `null`。
- `subtree_truncated` —— 若深度(`3`)或节点(`64`)上限截断了树则为 `true`。
- `subtree_verified` —— 仅当子树结构与活动被完整核实才为 `true`。为 `false` 时结果 fail-closed:绝不将委派出去的工作判定为 `succeeded`,并附 `note` 说明原因。
- `assistant_text`、`tools_used`、可选 `reasoning` —— 本轮新增的 assistant 输出;由 `chat` 和 `compact` 附带(`wait_session` 不带,它是纯状态原语)。

## 工具参考

### create_session

创建一个新会话并返回其 `session_id`,供后续 `chat` / `get_messages` 调用。

参数:

- `title`(string,可选,无默认值)—— 会话标题。
- `agent`(string,可选,无默认值)—— 要使用的 agent 名称。
- `model_id`(string,可选,无默认值)—— 形如 `providerID/modelID` 的模型,例如 `"anthropic/claude-sonnet-4"`;不含 `/` 的值会报错。它以 opencode 的 `Model.Ref` 形状 `{"providerID", "id"}` 发送。
- `location`(object,可选,无默认值)—— 在指定目录/项目中创建会话。形状为 opencode `Location.PublicRef`:`{"directory": "<absolute path>"}`;给定 `location` 时 `directory` 必填。
- `server`(string,可选)—— 见共享参数。

返回:`{ "session_id", "server", "title", "agent", "model" }`(若服务端未提供,字段可能为 `null`)。

```json
{"name": "create_session", "arguments": {"title": "My task", "model_id": "anthropic/claude-sonnet-4"}}
```

```json
{"name": "create_session", "arguments": {"title": "Work in project", "location": {"directory": "/root/my-project"}}}
```

### chat

向会话发送一个 prompt,并等待本轮结束或出现阻塞交互。

参数:

- `session_id`(string,**必填**)—— 会话 ID(`ses_...`)。
- `text`(string,**必填**)—— prompt 文本;为空或缺失会报错。
- `timeout_secs`(integer,可选,默认 `120`)—— 最长等待,钳制到 `[1, 3600]`。
- `wait_for_subagents`(boolean,可选,`chat` 默认 `false`)—— 为 `true` 时,`succeeded` 还要求整棵子 agent 子树静止,且 `once` / `always` / `reject` 的 `auto_permission` 会作用于子树中每个节点的待处理权限。为 `false` 时,子 agent 状态仍会上报,但不作为成功门禁。
- `auto_permission`(string,可选,枚举 `once` / `always` / `reject` / `manual`)—— 权限处理。**handler 默认:本地连接为 `once`,远端连接为 `manual`**(审批必须留在调用方)。schema 标注的扁平默认值为 `once`;handler 计算与连接相关的默认值。无效值会报错。
- `delivery`(string,可选,枚举 `steer` / `queue`)—— `steer` = 运行中转向(中断当前生成方向);`queue` = 本轮结束后生效。省略则不发送该字段;任何其他值都会报错。
- `files`(array,可选,无默认值)—— 附加到 prompt 的文件;每项为 `{ "uri" (required), "name"?, "description"? }`。缺少 `uri` 会报错。
- `server`(string,可选)—— 见共享参数。

返回:一个终态/子 agent payload(见上文)。`succeeded` 携带 `assistant_text`、`tools_used`、可选 `reasoning`、`last_message_id` 及子树字段;`failed` / `interrupted` 是已判定的结果并立即返回;`needs_permission` 携带 `requests`(`id`、`sessionID`、`action`、`resources`、`save`);`needs_form` 携带 `forms`;`timeout` 携带 `partial_text` 和一个 `diagnostics` 块(`outcome`、`last_message`、待处理计数、活跃子 agent、`suggested_actions`)。

```json
{"name": "chat", "arguments": {"session_id": "ses_abc", "text": "List files in the current directory", "timeout_secs": 120, "auto_permission": "once"}}
```

```json
{"name": "chat", "arguments": {"session_id": "ses_abc", "text": "Continue based on the attachment", "delivery": "steer", "files": [{"uri": "file:///root/a.md", "name": "a.md", "description": "reference"}]}}
```

### wait_session

等待一个已有会话进入终态或需交互状态;纯状态原语,不返回任何消息内容。

参数:

- `session_id`(string,**必填**)—— 会话 ID(`ses_...`)。
- `timeout_secs`(integer,可选,默认 `120`)—— 最长等待,钳制到 `[1, 3600]`。
- `wait_for_subagents`(boolean,可选,**默认 `true`**)—— 仅当整棵子 agent 子树静止时才报告 `succeeded`:子树内没有任何存活节点,也没有任何待处理权限/表单。为 `false` 时应用旧行为,但仍会上报子 agent 状态。
- `server`(string,可选)—— 见共享参数。

返回:一个状态 payload。`succeeded` 时会附加一个 `note`,提示你使用 `get_messages(after_message_id=...)`;它不包含 `assistant_text` / `tools_used`。`failed` / `interrupted` 为终态;`needs_permission` / `needs_form` 为阻塞态(`session_id` 是持有请求的会话,可能是子 agent);`timeout` 携带 `partial_text`(本轮新增的 assistant 文本)和一个 `diagnostics` 块。

```json
{"name": "wait_session", "arguments": {"session_id": "ses_abc", "timeout_secs": 120}}
```

典型组合:答复权限/表单后,调用 `wait_session` 抵达终态,再用 `get_messages(after_message_id=last_message_id)` 拉取新回复。

### get_messages

按时间升序获取格式化的会话消息历史,可选增量拉取。

参数:

- `session_id`(string,**必填**)—— 会话 ID(`ses_...`)。
- `limit`(integer,可选,默认 `50`)—— 最多返回的消息数。
- `after_message_id`(string,可选,无默认值)—— 增量游标:只返回该 id 之后的消息。带游标时只扫描最近 200 条消息;若游标不在其中,则返回完整列表并附一个 `note`。
- `server`(string,可选)—— 见共享参数。

返回:`{ "server", "session_id", "count", "messages": [...], "last_message_id" }` 外加一个可选 `note`。`last_message_id` 是下一次调用的游标。每条消息含 `id`、`type`、`time`;assistant 消息额外含 `agent`、`model`、`text`、可选 `reasoning`、可选 `tools`(`[{name, status}]`)和 `completed`;其他消息携带 `text`(`shell` 还带 `command`)。

```json
{"name": "get_messages", "arguments": {"session_id": "ses_abc", "limit": 20}}
```

```json
{"name": "get_messages", "arguments": {"session_id": "ses_abc", "after_message_id": "msg_123"}}
```

### permission_reply

答复一个权限请求。

参数:

- `session_id`(string,**必填**)—— **持有该请求**的会话 ID。当 `needs_permission` payload 的 `session_id` 是子 agent 会话时,使用该 ID,以便回复被路由到拥有该子 agent 的连接。
- `request_id`(string,**必填**)—— 权限请求 ID(`per_...`)。
- `decision`(string,**必填**,枚举 `once` / `always` / `reject`)—— `once` 仅本次允许;`always` 允许并保存规则;`reject` 拒绝。任何其他值都会报错。
- `message`(string,可选,无默认值)—— 可选说明性备注(仅非空时发送)。
- `server`(string,可选)—— 见共享参数;通常不需要,因为持有的 `session_id` 会路由该调用。

返回:`{ "ok": true, "server", "session_id", "request_id", "decision" }`。

```json
{"name": "permission_reply", "arguments": {"session_id": "ses_abc", "request_id": "per_xyz", "decision": "once"}}
```

若请求已在别处被处理,调用可能报错;可用 `pending_interactions` 重新检查,或继续用 `wait_session` 等待。

### form_reply

提交一个表单的答案。

参数:

- `session_id`(string,**必填**)—— 持有该表单的会话 ID(当持有者是子 agent 时,使用该子 agent 的 ID)。
- `form_id`(string,**必填**)—— 表单 ID(`frm_...`)。
- `answer`(object,**必填**)—— 键为字段 key;值可为 `string` / `number` / `boolean` / `string[]`。非对象值会报错。
- `server`(string,可选)—— 见共享参数;通常不需要,因为持有的 `session_id` 会路由该调用。

返回:`{ "ok": true, "server", "session_id", "form_id", "answer" }`。

```json
{"name": "form_reply", "arguments": {"session_id": "ses_abc", "form_id": "frm_xyz", "answer": {"name": "foo", "count": 3, "tags": ["a", "b"]}}}
```

### list_agents

列出目标 opencode 的全部 agent 及其解析后的默认模型(只读)。

参数:

- `server`(string,可选)—— 见共享参数。

返回:`{ "server", "count", "agents": [ { "name", "mode", "model" } ], "note" }`。`model: null` 表示该 agent 未显式配置模型,运行时回落到位置默认模型。若要将会话固定到某 agent 的模型,请自行传入 `create_session(model_id="providerID/modelID")`;本工具不会替你完成。

```json
{"name": "list_agents", "arguments": {}}
```

### interrupt

中断会话中当前正在进行的生成;在 `chat` 返回 `timeout` 后很有用。

参数:

- `session_id`(string,**必填**)—— 会话 ID(`ses_...`)。
- `server`(string,可选)—— 见共享参数。

返回:`{ "ok": true, "server", "session_id" }`。

```json
{"name": "interrupt", "arguments": {"session_id": "ses_abc"}}
```

### pending_interactions

非阻塞地查询某个会话及其整棵子 agent 子树中当前待处理的人类交互。

参数:

- `session_id`(string,**必填**)—— 根会话 ID(`ses_...`)。
- `server`(string,可选)—— 见共享参数。

返回:`{ "server", "session_id", "root_session_id", "permissions": [...], "forms": [...], "subagents", "pending_subagents", "subtree_truncated", "subtree_verified" }`。权限为 `{id, sessionID, action, resources, save}`(或按会话回退的形状)。表单为 `{id, sessionID, title, fields: [{key, title, type, required, options, description}]}`;只列出待处理表单。若子树核实不可用,则回退到根会话自身的视图并报告 `subtree_verified: false`。

```json
{"name": "pending_interactions", "arguments": {"session_id": "ses_abc"}}
```

### list_sessions

以关键词、排序、目录过滤和游标分页来枚举与搜索已有会话。返回的 `session_id` 是 `chat` 的恢复句柄。

参数(全部可选):

- `search`(string,无默认值)—— 与标题/内容匹配的关键词。
- `limit`(integer,默认 `20`)—— 最大结果数。
- `order`(string,枚举 `asc` / `desc`,默认 `desc`)—— 按更新时间排序(默认最新在前)。
- `directory`(string,无默认值)—— 按工作目录过滤。
- `cursor`(string,无默认值)—— 分页游标,取自先前结果的 `cursor.next`。
- `server`(string,可选)—— 见共享参数。

返回:`{ "server", "count", "sessions": [ { "id", "title", "agent", "model", "parentID", "time": { "updated", "idle"? } } ], "cursor": { "previous", "next" } }`。缺失字段为 `null`;服务端未上报时 `time.idle` 省略。

```json
{"name": "list_sessions", "arguments": {"search": "deploy", "limit": 20, "order": "desc"}}
```

### compact

压缩会话上下文,等待压缩完成并返回结果;在上下文接近上限时主动瘦身很有用,之后可继续对话。

参数:

- `session_id`(string,**必填**)—— 会话 ID(`ses_...`)。
- `timeout_secs`(integer,可选,默认 `120`)—— 最长等待,钳制到 `[1, 3600]`。
- `server`(string,可选)—— 见共享参数。

返回:`{ "status", "server", "session_id", "time_idle", "last_message_id" }` 外加 `assistant_text` / `tools_used`(以及可选 `reasoning`)。`status` 为 `succeeded`(压缩完成)或 `compaction_failed`(压缩消息 `status=failed`),或 `timeout`。

schema/handler 说明:handler 还会读取一个 `auto_permission` 参数(默认与 `chat` 相同:本地 `once`,远端 `manual`),但工具目录并未暴露该参数。以 handler 为准:它确实存在,但调用方不能依赖它被记录/校验。

```json
{"name": "compact", "arguments": {"session_id": "ses_abc", "timeout_secs": 120}}
```

### get_context

读取会话的上下文用量(`tokens` / `cost`)与元信息,例如用于决定是否调用 `compact`。

参数:

- `session_id`(string,**必填**)—— 会话 ID(`ses_...`)。
- `server`(string,可选)—— 见共享参数。

返回:`{ "server", "id", "title", "agent", "model", "parentID", "tokens", "cost", "time": { "updated", "idle" }, "revert" }`。`tokens` / `cost` / `revert` 默认 `null`;`time.idle` 始终存在(可能为 `null`)。

```json
{"name": "get_context", "arguments": {"session_id": "ses_abc"}}
```

### delete_session

删除一个会话。

> **不可逆** —— 删除的会话无法恢复。
> **级联** —— 删除父会话也会删除其所有子会话(删除后访问子会话返回 `404`)。

参数:

- `session_id`(string,**必填**)—— 要删除的会话 ID(`ses_...`)。
- `server`(string,可选)—— 见共享参数。

返回:`{ "ok": true, "server", "session_id" }`。该会话也会从本进程的路由表中移除。

```json
{"name": "delete_session", "arguments": {"session_id": "ses_abc"}}
```

### connect_server

注册并验证一个远端 opencode 连接(仅在本进程内有效;绝不持久化)。

参数:

- `name`(string,**必填**)—— 连接别名;`local` 为保留名,会被拒绝。
- `url`(string,**必填**)—— 公网 http(s) 端点,例如 `https://host:4096`。非 http(s) 协议、无协议 URL、控制字符,以及字面量(或其解析结果)为回环 / 私有 / 链路本地(含 `169.254.169.254` 云元数据段)/ 保留 / 组播 / 未指定的 IPv4/IPv6 地址都会被拒绝(含解析器接受的非标准写法,如 `2130706433`、`0x7f.0.0.1`、`0177.0.0.1`、`127.1` 及 `localhost`)。本地服务器请用 `OPENCODE_URL` / `OPENCODE_PASSWORD` 环境变量。
- `password`(string,可选,无默认值)—— 明文密码。会经 `Authorization` 头发送到 `url`,且会残留在 MCP 请求流中;建议用短期 / 每连接独立的密码。
- 无 `server` 参数。

仅接受调用内传入的明文 `password`:`password_file` / `password_env` 被拒绝,因为调用方是 LLM,文件/env 读取会使其把任意主机文件或环境变量经网络外传。未提供 `password` 时不发送 `Authorization` 头(部分远端使用空用户名/密码)。注册执行创建时硬门禁:不可达 = `[availability]`;可达但非 opencode API = `[compatibility]`;401 = `[availability]`(密码错误);版本不一致附加 `api_version_warning` 而非失败。

返回:`{ "name", "url", "local", "source", "version", "baseline", "baseline_check", "password_source", "api_version_warning"? }`。`password_source` 为 `plaintext` / `none`。

```json
{"name": "connect_server", "arguments": {"name": "build-box", "url": "https://build-box.example.com:4096", "password": "short-lived-per-connection-password"}}
```

### list_servers

列出所有当前连接(本地加动态远端)及其地址、来源、版本与基准检查状态。

参数:无(连 `server` 也没有)。确保本地连接就绪,必要时拉起本地 `serve`。

返回:`{ "count", "servers": [ { "name", "url", "local", "source", "version", "baseline", "baseline_check" } ] }`。`source` 为 `spawned` / `env` / `dynamic`;`baseline_check` 为 `ok` / `mismatch(<version>)` / `unknown`。

```json
{"name": "list_servers", "arguments": {}}
```

### disconnect_server

移除一个动态注册的远端连接。

参数:

- `name`(string,**必填**)—— 连接别名。
- 无 `server` 参数。

本地连接无法移除(尝试会报错)。no-op/未知名称会报错并指向 `list_servers`。移除连接还会清除指向它的会话路由,并杀掉它拉起的进程(若有)。

返回:`{ "ok": true, "removed": "<name>" }`。

```json
{"name": "disconnect_server", "arguments": {"name": "build-box"}}
```
