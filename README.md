# LcTiCodeAgent

一个从零实现的可对话编程智能体，提供终端与本地 Web UI。本项目不依赖任何 Agent 框架或 SDK，模型交互、会话历史、上下文管理、工具执行、循环终止与错误处理均由项目自行实现。

当前已建立追加式事件日志、可对话终端 UI、OpenRouter 流式 Function Calling、本地文件与受限命令工具、工作区文本搜索、Workspace Policy Sandbox 权限管线、两级上下文压缩（确定性工具裁剪 + 经本地事实校验的结构化 LLM 摘要）、从会话日志确定性恢复模型上下文的会话恢复，以及覆盖注入、伪造证据与命令变体的安全红队回归套件。

## 当前功能

- 交互式终端会话
- Starlette + WebSocket 本地 Web UI，与 CLI 共用 `AgentEvent`、Agent Loop 和 JSONL 日志
- Web 智能输入框：`/` 命令面板、`@` 安全文件引用、`#` Git/Context/Session/Event 证据引用
- 流式显示 Agent 回复
- 显示工具调用、成功、失败和审批状态
- 显示上下文 token 使用量
- 将全部事件持久化为 JSONL
- 分层上下文管理：60% 确定性工具裁剪；75% 触发固定 Schema 的结构化摘要并以 50% 为软目标；摘要的路径、标识符、数字、exit code 和事件 ID 均在本地校验
- 可切换压缩策略：`none / drop_oldest / plain_summary / validated`，用于同 Agent Loop 的公平对比
- `/recall EVENT_ID` 从追加式 JSONL 取回被摘要的原始证据；`/compact` 手动压缩；`/context` 分层统计
- 会话恢复：`--resume <SESSION_ID>` 从 JSONL 日志确定性重建模型上下文、工作记忆和 token 用量
- `/help`、`/status`、`/context`、`/compact`、`/clear`、`/exit` 命令
- `--demo` 非交互演示模式，便于测试和录制
- 多厂商 OpenAI-compatible 流式 Function Calling：OpenRouter、Google Gemini、DeepSeek、OpenAI 与自定义兼容接口
- OpenRouter 暂时故障有限重试；`finish_reason="error"` 显式转为可见错误，已输出内容绝不自动重放
- Gemini 推理视图：可配置 reasoning effort，流式展示提供商返回的摘要或推理文本；加密块与完整详情仅保存在本地并回传模型
- 本地读取、工作区正则文本搜索、精确替换、新建文件和验证命令工具
- 默认启用的轻量Workspace Policy Sandbox
- `ALLOW / ASK / DENY` 权限管线和单次CLI审批
- 文件编辑State Gate：`replace_in_file` 绑定读取时SHA-256，过期编辑被拒绝
- 受控HTTPS文本获取与Git只读检查工具
- 带Preflight、状态令牌和单次审批的选择性Git commit/push
- 安全红队回归：脚本化对抗测试覆盖提示注入、隐藏Unicode指令、伪造完成、重复调用与命令变体，详见 [docs/redteam.md](docs/redteam.md)

## 运行

```powershell
py -V:3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m code_agent
```

运行模拟演示：

```powershell
.\.venv\Scripts\python.exe -m code_agent --demo
```

运行本地 Web UI（默认模拟模式）：

```powershell
.\.venv\Scripts\python.exe -m code_agent.web
```

启动真实 Web Agent：

```powershell
$env:OPENROUTER_API_KEY = "你的密钥"
.\.venv\Scripts\python.exe -m code_agent.web --live
```

Web UI 的模型区域可切换服务端已配置的 API 厂商与模型，切换时创建新会话。密钥不会进入浏览器存储或 Session JSONL。除默认 OpenRouter 外，可在启动前设置：

```powershell
$env:GEMINI_API_KEY = "..."       # Google Gemini direct
$env:DEEPSEEK_API_KEY = "..."     # DeepSeek
$env:OPENAI_API_KEY = "..."       # OpenAI
```

模型名分别可通过 `LCTI_GOOGLE_MODEL`、`LCTI_DEEPSEEK_MODEL` 和 `LCTI_OPENAI_MODEL` 覆盖。自定义 OpenAI-compatible 接口使用 `LCTI_COMPAT_API_KEY`、`LCTI_COMPAT_BASE_URL` 与 `LCTI_COMPAT_MODEL`，其中 Base URL 必须为 HTTPS。

浏览器访问 `http://127.0.0.1:8765`。服务只允许绑定本机回环地址；模型密钥始终留在后端。Web UI 支持流式事件、会话恢复、停止、上下文压缩、证据回溯、一次性审批，以及 `/ @ #` 键盘建议。详见 [docs/web-ui.md](docs/web-ui.md)。

使用 OpenRouter 运行真实单任务：

```powershell
$env:OPENROUTER_API_KEY = "你的密钥"
.\.venv\Scripts\python.exe -m code_agent --live --prompt "概括这个项目的目录结构"
```

默认模型为固定版本 `google/gemini-3.7-flash`。模型只能请求工具；文件读取和执行均由本项目在本地完成。项目不使用 OpenRouter Agent SDK、Server Tools 或任何托管代码执行能力。

模型循环默认最多 16 步，可通过 `LCTI_MAX_STEPS` 在 4–64 之间调整。结构化摘要复用同一模型，请求中携带固定 JSON Schema，并由本地验证器严格执行；摘要失败或事实校验不通过时，Agent 保留原上下文继续运行。

OpenRouter 默认最多重试 2 次，可通过 `LCTI_OPENROUTER_RETRIES` 设置为 0–5。重试仅发生在尚未输出文本或执行工具的请求上。

Gemini reasoning effort 默认为 `medium`，可通过 `LCTI_REASONING_EFFORT` 设置为 `minimal`、`low`、`medium` 或 `high`。真实验证中 `low` 只返回签名，而 `medium` 返回了可读 `reasoning.text`；提高等级会增加延迟和输出 token。Web UI 优先展示 `reasoning.summary`；若 OpenRouter 只返回 `reasoning.text`，则以“模型推理（提供商返回）”明确标注。两者都不能等同于完整内部思维链。

运行内置修复任务：

```powershell
.\.venv\Scripts\python.exe -m code_agent --workspace examples\buggy_average --live --prompt "修复 average 对空列表除零的问题，空列表应返回 0.0；不要修改测试，并运行测试验证。"
```

恢复之前的会话并继续对话：

```powershell
.\.venv\Scripts\python.exe -m code_agent --live --resume <SESSION_ID> --prompt "继续刚才的任务"
```

`SESSION_ID` 为启动时头部显示的会话 ID；恢复过程从该会话的 JSONL 日志重建模型消息、工作记忆与 token 用量，并追加写入同一日志。详见 [docs/session-restore.md](docs/session-restore.md)。

Workspace Policy Sandbox无需Docker、WSL或管理员权限。它限制所有文件工具只能访问工作区，拒绝凭据文件和歧义修改；命令采用参数数组并设置白名单、环境变量白名单、超时和输出上限。该模式不提供内核级文件系统或网络隔离，不能替代容器、虚拟机或操作系统Sandbox。

运行测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

运行四种压缩策略的确定性指标夹具：

```powershell
.\.venv\Scripts\python.exe -m experiments.context_compaction
```

运行真实多轮 `buggy_average` 四策略评测（会产生 OpenRouter 调用并写入被忽略的 `tmp/`）：

```powershell
.\.venv\Scripts\python.exe -m experiments.context_benchmark
```

2026-08-31 的单次真实结果中，四种策略均完成修复并通过测试；`validated` 的最大单次压缩比为 `0.2315`，但因一次摘要被事实校验拒绝，其总 prompt token 并未优于基线。详见 [评测报告](docs/evaluation/context-benchmark-20260831.md)。

API key 仅通过环境变量读取，不会写入仓库、浏览器存储、Session 日志或视频。OpenRouter 请求额外限制为 `data_collection=deny`；直连其他厂商时，数据策略由对应厂商账户与服务条款决定。
