# opencode-mcp

[English](README.md) | **中文**

让任意 MCP 客户端驱动一个 **opencode** 会话:创建会话、发送 prompt、收集结果、答复权限请求与表单,并在本地/远端多个 opencode 服务端之间路由。零依赖 —— 单文件纯 Python 标准库。

**为什么需要它**:在 agent 普遍以 ACP(Agent Client Protocol)客户端身份直连 opencode 之前,通过 **HTTP API** 以 MCP 驱动 opencode 是 agent 操作它的最佳方案 —— 本 server 就是为此而建:每个工具对应一套干净、机器可消费的状态机(权威终态、阻塞交互态、增量游标),而不是界面的复刻,天然适合 agent 编排。

实测环境:

- **opencode v2.0.12** —— 开发基准,全部能力均对其 live 验证
- **真实远端实例** —— 完整网络端到端(连接 → 建会话 → 对话 → manual 权限 → 答复 → 等待 → 增量拉取 → 断连)
- **Hermes agent gateway** —— 作为工具提供方挂载并在生产使用
- **oh-my-opencode-slim 编排框架** —— 托管本 MCP 并借此驱动嵌套 opencode 会话

## 范围

这**不是**面面俱到的 opencode 控制面,也不打算是。范围刻意收敛到 agent 日常操作 opencode 真正需要的能力:

- 创建/恢复会话、发送 prompt、收集结果
- 处理两种阻塞交互 —— 权限请求与表单
- 上下文管理(用量查看、压缩)与会话生命周期
- 多 opencode 服务端(本地与远端)的路由

文件系统、凭据、provider、插件、终端、配置等管理面**刻意不做**。工具更少、语义更锋利、出错面更小。

## 用 LLM 安装

把下面这段交给你的编码 agent:

```text
帮我安装并注册 opencode-mcp:

1. 克隆: git clone https://github.com/yitro-z-wang/opencode-mcp ~/opencode-mcp
2. 验证: 运行 `python3 ~/opencode-mcp/test_client.py` —— 必须报告 16 个工具且通过。
3. 注册到 opencode: `opencode mcp add opencode-local -- python3 ~/opencode-mcp/server.py`
4. 重载 opencode 配置,然后新开一个会话确认 16 个 opencode-local 工具可用。

要求: Python 3.10+ 且 opencode CLI 在 PATH 上(v2.0.12 为开发基准)。
出错请原样回报,不要盲目重试。
```

也可以自己注册:`opencode mcp add opencode-local -- python3 /path/to/server.py`(任何 MCP 客户端均可,本 server 走 stdio)。

## 特性

- **零依赖** —— 纯 Python 3 标准库,单文件,无构建步骤
- **多服务器** —— MCP 专属拉起本地 `opencode serve`(随机端口+随机密码,随 MCP 退出)或 `OPENCODE_URL` 显式直连;远端运行时注册;会话自动路由到归属服务端
- **权威状态,不做猜测** —— 终态取自 opencode 的会话 outcome,而非消息形状推断
- **多 agent 感知** —— 被委派的子 agent 不会被误判为“已完成”,其权限请求会被上报(见文档)
- **失败分类** —— 每次失败归类为 `[availability] / `[compatibility]` / `[other]`,并保留原始报错便于回报
- **安全默认** —— 远端 `chat` 默认手动审批;`connect_server` 仅接受调用内明文密码(不读取文件/环境变量),并拒绝非公网或非 http(s) 的 URL
- **可组合原语** —— `wait_session`(纯状态)与 `get_messages`(增量游标)把“等待”和“读取”分离
- **生产级运行时** —— 并发请求处理、MCP 标准取消、有界等待

## 工具

| 工具 | 作用 |
| --- | --- |
| `create_session` | 创建会话(可选 title / agent / model / location) |
| `chat` | 发送 prompt 并等待结果;支持附件与 `steer` / `queue` 投递;可自动答复权限 |
| `wait_session` | 纯状态等待:`succeeded` / `failed` / `interrupted` / `needs_permission` / `needs_form` / `timeout` |
| `get_messages` | 读取消息记录;经 `after_message_id` 增量拉取 |
| `permission_reply` | 答复权限请求:`once` / `always` / `reject` |
| `form_reply` | 按字段提交表单答案 |
| `list_agents` | 列出 agent 及其解析后的默认模型(只读) |
| `interrupt` | 中断当前生成 |
| `pending_interactions` | 非阻塞查询待处理权限/表单 |
| `list_sessions` | 枚举/搜索会话 —— 恢复历史话题的句柄 |
| `compact` | 压缩上下文并等待完成 |
| `get_context` | token/成本用量与会话元信息 |
| `delete_session` | 删除会话(不可逆,级联删除子会话) |
| `connect_server` | 注册并验证远端 opencode 连接 |
| `list_servers` | 列出全部连接及版本/基准状态 |
| `disconnect_server` | 移除动态注册的远端连接 |

全部工具支持可选 `server` 参数;带 `session_id` 的调用自动路由到该会话所属连接。`chat` 与 `wait_session` 都接受 `wait_for_subagents`,但两者默认值不同 —— 详见文档。

## 文档

- [连接模型、环境变量、取消](docs/connection-model.zh-CN.md) · [English](docs/connection-model.md)
- [会话、agent 与模型选择](docs/sessions.zh-CN.md) · [English](docs/sessions.md)
- [权限与表单流程、权限规则](docs/interactions.zh-CN.md) · [English](docs/interactions.md)
- [多 agent 会话中的子会话感知等待](docs/subagent-waiting.zh-CN.md) · [English](docs/subagent-waiting.md)
- [工具参数参考](docs/tools.zh-CN.md) · [English](docs/tools.md)
- [已验证流程与测试](docs/verification.zh-CN.md) · [English](docs/verification.md)
- [场景测试套件](tests/README.zh-CN.md) · [English](tests/README.md)
- [设计记录:远端连接](DESIGN-remote-connections.zh-CN.md) · [English](DESIGN-remote-connections.md)
