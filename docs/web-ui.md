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

## 本地安全边界

- WebSocket 拒绝非 localhost Origin；
- `TrustedHostMiddleware` 只接受本机 Host；
- CSP 只允许同源静态资源和本机 WebSocket；
- 响应设置 `nosniff`、`no-referrer` 和 `no-store`；
- 动态事件内容全部通过 `textContent` 渲染，不插入事件提供的 HTML；
- API key 只由后端环境变量读取，不进入 ready 消息、浏览器状态或会话日志。

浏览器会看到完成任务所需的代码片段、命令输出和 Git Preflight。这是本机产品界面，不应暴露到不可信网络。

## 视觉系统

批准的概念稿位于：

- `docs/design/web-main-concept.png`
- `docs/design/web-approval-concept.png`

界面使用纸白背景、深蓝手绘线、矢车菊蓝活动状态、薄荷绿成功、杏黄审批、珊瑚红错误和淡紫 Evidence。代码与路径使用等宽字体，全部交互文字保持代码原生。卡通助手仅用于品牌和执行状态，不替代真实 UI。
