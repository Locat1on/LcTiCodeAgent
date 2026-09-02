# 本地 Web UI

## 定位

Web UI 是 CLI 之外的第二个表现层，不复制 Agent 实现。二者共用：

- `LiveAgent` / `SimulatedAgent`；
- `AgentEvent` 事件协议；
- `SessionLog` 追加式 JSONL；
- `ApprovalRequest` 与 ALLOW / ASK / DENY 语义；
- Context、Sandbox、Git Preflight 和证据回溯数据。

后端使用 Starlette 与 Uvicorn，不使用 FastAPI、Agent SDK、React 或 Node 构建链。前端为原生 HTML、CSS 和 JavaScript，静态资产随 Python 包分发。

## 运行

模拟模式：

```powershell
.\.venv\Scripts\python.exe -m code_agent.web
```

真实模型：

```powershell
$env:OPENROUTER_API_KEY = "..."
.\.venv\Scripts\python.exe -m code_agent.web --live
```

默认地址为 `http://127.0.0.1:8765`。`--host` 只接受 `127.0.0.1`、`localhost` 或 `::1`，不提供公网监听入口。

### 模型厂商选择

顶部模型区域打开厂商、模型和 API Key 面板。Web UI 支持 OpenRouter、Google Gemini、DeepSeek、OpenAI 和自定义 OpenAI-compatible 接口。密钥既可来自服务端环境变量，也可通过密码输入框发送到 localhost；后者只保存于当前后端进程内存，服务重启后消失。

`POST /api/providers/{provider_id}/credential` 只接受本机 Origin、JSON 请求、合法模型名和不超过 4096 字符的密钥。响应只返回厂商 ID、配置状态和 `stored=process_memory`，不回显密钥。前端提交后立即清空输入，不使用 Local Storage、Session Storage 或 Cookie。`/api/providers` 仍只返回厂商名称、可用状态、模型候选和环境变量名称。

环境变量仍是录屏、自动化和更高安全要求场景的推荐方式，因为这种方式不需要让密钥经过浏览器和本机 HTTP 请求。

切换厂商或模型会创建新会话。`session.started` 记录非敏感的厂商 ID 与模型名，恢复会话时自动重建原 Provider，避免跨厂商混用 reasoning detail 或 tool-call 状态。

### 会话操作

会话行右侧菜单支持重命名和删除。重命名通过追加 `session.renamed` 事件实现，不改写历史 JSONL；列表以最新重命名事件为准。删除将日志移动到 `sessions/.trash/`，因此仍可人工恢复。仍有 WebSocket 连接的会话拒绝删除，用户需先切换到其他会话。

## 事件桥

`LiveAgent` 是同步生成器。Web 服务把一个 turn 放入工作线程，通过 `asyncio.Queue` 把已存在的 `AgentEvent` 送回 WebSocket。浏览器不直接访问模型 Provider，也不能调用本地工具。

```text
Browser task
  -> WebSocket protocol validation
  -> worker thread / LiveAgent.respond
  -> AgentEvent + SessionLog.append
  -> asyncio.Queue
  -> browser event renderer
```

停止是协作式的：服务设置取消标记，在下一个 Agent 事件边界关闭生成器并记录 `turn.completed(reason=user_cancelled)`。它不会强杀 Python 进程。

## 一次性审批

同步 `ApprovalHandler` 通过 `ApprovalBroker` 等待浏览器决定：

1. `tool.approval_required` 写入日志并发送浏览器；
2. Broker 注册对应 `request_id`；
3. 浏览器只能提交 `approved=true/false`；
4. Agent 收到决定后仍执行原有审批后 State Gate 复核；
5. 断开连接、停止任务或 300 秒超时均 fail-closed。

Web UI 不提供永久批准、force push 或任意 refspec。

## 智能输入框

输入建议支持鼠标以及 `↑ / ↓ / Enter / Esc`：

- `/`：执行 Web 命令。当前包含 `/help`、`/status`、`/context`、`/compact`、`/clear`、`/recall`、`/new`；
- `@`：从工作区文件索引选择路径。它只向模型附加路径和“先用 read_file 检查”的说明，不在选择阶段读取文件正文；
- `#`：附加后端生成的结构化证据，包括 `#git-status`、`#git-diff`、`#context`、`#session` 和 `#event:<id>`。

文件索引复用文件工具的忽略目录和敏感路径规则，拒绝 `.env`、私钥、SSH/AWS 凭据、越界路径和不存在的文件。单次消息最多 12 个引用，展开后的证据总量最多 32,000 字符。

用户事件记录三个层次：

- `text`：浏览器中显示的原始任务；
- `references`：结构化引用列表；
- `model_text`：展开证据后真正进入模型上下文的文本。

Session restore 使用 `model_text` 重建上下文。WebSocket 回传用户事件时会移除 `model_text`，避免后端展开的 Git Diff 或 Event 内容无必要地再次进入浏览器；浏览器只显示原始文字与引用标签。

## 本地安全边界

- WebSocket 拒绝非 localhost Origin；
- `TrustedHostMiddleware` 只接受本机 Host；
- CSP 只允许同源静态资源和本机 WebSocket；
- 响应设置 `nosniff`、`no-referrer` 和 `no-store`；
- 动态事件内容全部通过 `textContent` 渲染，不插入事件提供的 HTML；
- 思考区只接收经过分类的 `assistant.reasoning_delta`；WebSocket 会移除原始 `reasoning`、签名、加密块和完整 `reasoning_details`；
- API Key 来自环境变量或 Web 进程内存，不进入 ready 消息、浏览器持久化状态或会话日志；Web 输入方式会在提交瞬间经过浏览器内存与 localhost 请求。

浏览器会看到完成任务所需的代码片段、命令输出和 Git Preflight。这是本机产品界面，不应暴露到不可信网络。

## 视觉系统

批准的概念稿位于：

- `docs/design/web-main-concept.png`
- `docs/design/web-approval-concept.png`

界面使用纸白背景、深蓝手绘线、矢车菊蓝活动状态、薄荷绿成功、杏黄审批、珊瑚红错误和淡紫 Evidence。代码与路径使用等宽字体，全部交互文字保持代码原生。卡通助手仅用于品牌和执行状态，不替代真实 UI。
