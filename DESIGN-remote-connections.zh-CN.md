# 设计定稿:多 opencode 连接(已实施)

[English](DESIGN-remote-connections.md) | **中文**

状态:**已实施**(2026-09-22)。connect_server / list_servers / disconnect_server + 全工具 `server` 参数 + session 自动路由 + 本地专属拉起 + 失败分类均已落地并通过 live 验证。

## 决策记录(2026-09-22,与用户确认)

| 决策点 | 结论 | 备注 |
|---|---|---|
| 本地连接模型 | **MCP 拉起专属 serve(已定案)** | 无 `server` 参数时:连本地 → 不通则 MCP 拉起 `opencode serve --port <随机高位端口>`,密码随机生成经 `OPENCODE_SERVER_PASSWORD` 注入(实测有效且不打印);子进程模式(随 MCP 实例生命周期,自清理,多实例靠随机端口互不冲突);**禁止任何推断性自发现(含 service.json)**;PATH 无 opencode → 可用性错误明示"用户环境问题",不重试;`OPENCODE_URL` 显式设置时跳过拉起直连(密码取 `OPENCODE_PASSWORD`,缺省 `opencode`) |
| 寻址模型 | 命名别名 + session 自动路由 | 工具加可选 `server` 参数(缺省 local);`create_session(server=X)` 记录 `ses_→X` 映射,后续带 session_id 的调用免传;不做隐式 current 切换 |
| 句柄语义 | 无 fd/socket;别名即句柄 | MCP 调用方(LLM)只能持有字符串;`ses_` id 全局唯一,session 不需要回传连接 handle |
| 持久化 | **仅动态,永不支持静态配置** | "记忆常用连接不是工具的责任,工具不该代替 memory 和 skill 的位置"——连接由调用方按需建立,进程重启即失,由调用方自行重连 |
| 密码通道 | **仅调用内明文**(2026-09-24 安全修订) | 原为 `文件 > env > 明文`。文件/env 通道是 LLM 调用方对任意主机文件/环境变量的读取,且其值会经 `Authorization` 头发送到调用方自选的 url——一次调用即可外传凭据,故动态连接一律拒绝。本地服务器用进程环境变量 `OPENCODE_URL` / `OPENCODE_PASSWORD` 指定(操作者可控)。未提供 `password` 时不发送 Authorization 头(实测存在无需认证/空凭据的远端形态;另注意:凭据错误时请求可能落到 Web UI 回退,连接器会以 compatibility 报错提示检查密码) |
| 版本基准与漂移裁定 | **已定案** | ①创建时检查:connect_server / local 首连即硬门禁(连不上=可用性错;无 version=兼容性错);②后续调用仅当失败后重查版本号,据此分类失败:可用性 / 兼容性 / 其他(其他需 dump 具体报错便于回报开发者);③告警 per-(connection, session),按已告警版本去重(新会话可见、同会话不轰炸、版本变化后新会话按新版本告警);④显式状态面:connect_server 返回与 list_servers |
| 远端权限默认 | **manual(已定案)** | 远端连接 chat 默认 `auto_permission="manual"`(审批过程必在调用方,强于 once);`once/always/reject` 均保留、由调用方显式选用,**不额外限制 always**;本地维持 once 默认;交互式审批 UI 归调用方,MCP 只负责把 needs_permission 详情给足 |

## 工具面(实施时)

- `connect_server(name, url, password?)` — 建连即验证(url 须为公网 http(s) 端点,完整安全模型见 `docs/connection-model.md`),返回 `{name, version, baseline_check}`
- `list_servers()` — local + 动态远端:版本、基准状态
- `disconnect_server(name)` — 移除(local 不可删)
- 全部工具:可选 `server` 参数 + session 自动路由

## 内部重构面

全局 `CONFIG` → 连接对象字典 `{name: {url, password, version, warned}}`;`http_request` / `poll_session` / `ensure_connected` 全部带连接上下文;版本告警事件按连接隔离。

## 远端前提

远端机器 `opencode serve`(可达地址 + 密码)或 SSH 隧道;远端会话的工具执行发生在**远端文件系统**,权限审批语义按远端配置。
