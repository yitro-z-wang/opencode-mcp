# 连接模型:本地拉起 + 多服务器

[English](connection-model.md) | **中文**

opencode-mcp [文档](../README.zh-CN.md)的一部分。

**禁止推断性自发现(含 service.json)。** 本地连接两种形态:

1. **显式直连**:`OPENCODE_URL` 设置时,local 直接指向该地址(密码取 `OPENCODE_PASSWORD`,缺省 `opencode`)。
2. **专属拉起**(默认):首次需要 local 时,MCP 自己拉起一个 `opencode serve`——**随机高位端口 + 随机密码**(经 `OPENCODE_SERVER_PASSWORD` 注入),子进程模式随本 MCP 实例退出:**每个 MCP 进程最多拉起一个**(进程内单例),并在 stdin EOF(host 正常关闭)与 `SIGTERM` / `SIGINT` / `SIGHUP` 时被杀掉并回收。多个 MCP 实例靠随机端口互不冲突,也不会累积——每次重启都会清理自己的子进程。唯一缺口是 `SIGKILL`:任何平台的进程都无法拦截它,被硬杀时可能残留一个 serve(后续不会认领它——按设计不做推断性发现)。`PATH` 中无 `opencode` 时返回可用性错误(用户环境问题,不重试)。

**多服务器**:`connect_server(name, url, password?)` 注册远端(仅进程内有效,不持久化);全部工具带可选 `server` 参数(缺省 local);带 `session_id` 的调用自动路由到创建该会话的连接。安全模型:url 须为公网 http(s) 端点——该调用来自 MCP 客户端(生产环境为 LLM),未校验的 URL + 调用方提供的凭据 = 一次调用即成的 SSRF/凭据外传原语。校验**在注册时与动态连接的每次请求之前都执行**(把 DNS rebinding 时间窗收窄到单个在途请求),拒绝其他协议、无协议 URL、控制字符,以及字面量(或其解析结果)为回环/私有/链路本地(含 `169.254.169.254` 云元数据段)/保留/组播/未指定的 IPv4/IPv6 地址(含 `2130706433`、`0x7f.0.0.1`、`0177.0.0.1`、`127.1` 等非标准写法与 `localhost` 类名称)。主机名校验为**失败关闭**:解析到*混合*公网/私网记录集,或无任何记录可解析为地址,均被拒绝。所有 HTTP 流量走**不跟随重定向**的 opener(CPython 默认处理器会把 `Authorization` 头复制到未校验的 `Location` 目标,故注册端点的任何 3xx 都使请求失败而非跟随)。DoS 边界:2xx 响应体上限 1 MiB(声明与实际均查)、错误体上限 64 KiB,超限中止一律 `[other]`,不会被重分类为服务器故障。凭据:仅接受调用内明文 `password`;`password_file` / `password_env` 被拒绝(调用方为 LLM,文件/env 读取会把任意主机文件/环境变量经网络外传);未提供时不发送 Authorization 头(存在空用户名/密码的远端)。本地服务器的文件/env 凭据由操作者用 `OPENCODE_URL` / `OPENCODE_PASSWORD` 环境变量指定。

**版本基准与失败分类**:每个连接创建时硬门禁(连不上=`[availability]`;可达但非 opencode API=`[compatibility]`);调用失败后重查版本,据此分类为 `[availability] / [compatibility] / [other]`(other 附完整原始报错,可原样回报开发者)。版本与基准不一致时,向触碰该连接会话的**第一个工具结果**注入一次 `api_version_warning`(按 (连接, 会话, 版本) 去重,新会话可见、同会话不轰炸)。

**权限默认**:本地连接 chat 默认 `auto_permission="once"`;**远端连接默认 `manual`**(审批过程必在调用方);`once/always/reject` 均可显式选用。

### 环境变量

- `OPENCODE_URL`:显式指定 local 直连地址(跳过拉起),例如 `http://127.0.0.1:4096`。
- `OPENCODE_PASSWORD`:local 直连的 HTTP Basic 密码;用户名固定 `opencode`,缺省 `opencode`。
- `OPENCODE_MCP_WORKERS`:请求处理线程数,默认 `4`。设为 `1` 则退化为严格串行。
- `OPENCODE_MCP_BASELINE_VERSION`:开发基准版本覆盖(默认 `2.0.12`,主要用于测试)。

## 并发与取消

- **并发**:每个 JSON-RPC 请求在独立工作线程中处理(`OPENCODE_MCP_WORKERS` 控制,默认 4)。`chat` / `wait_session` 这类长阻塞调用不会卡住其他工具调用。
- **取消**:支持 MCP 标准的 `notifications/cancelled`。调用方取消 `chat` / `wait_session` 后,轮询会在 1 秒内停止(该请求不再回写响应;正在途中的单次 HTTP 请求最长 30 秒自然超时)。

## 版本基准与告警

版本与告警机制已并入「连接模型:本地拉起 + 多服务器」一节:每连接创建时硬门禁、失败后重查版本并分类(availability / compatibility / other)、告警按 (连接, 会话, 版本) 去重注入。基准版本默认 `2.0.12`,可用 `OPENCODE_MCP_BASELINE_VERSION` 覆盖(主要用于测试)。
