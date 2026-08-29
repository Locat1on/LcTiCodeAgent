# LcTiCodeAgent

一个从零实现的可对话终端编程智能体。本项目不依赖任何 Agent 框架或 SDK，模型交互、会话历史、上下文管理、工具执行、循环终止与错误处理均由项目自行实现。

当前已建立追加式事件日志、可对话终端 UI、OpenRouter 流式 Function Calling、本地文件与受限命令工具、工作区文本搜索、Workspace Policy Sandbox 权限管线、任务感知的确定性上下文压缩（第一层：工具结果裁剪，证据可经会话日志恢复）、从会话日志确定性恢复模型上下文的会话恢复，以及覆盖注入、伪造证据与命令变体的安全红队回归套件。

## 当前功能

- 交互式终端会话
- 流式显示 Agent 回复
- 显示工具调用、成功、失败和审批状态
- 显示上下文 token 使用量
- 将全部事件持久化为 JSONL
- 分层上下文管理：确定性工具结果裁剪、60% 预算自动压缩、`/compact` 手动压缩、`/context` 分层统计
- 会话恢复：`--resume <SESSION_ID>` 从 JSONL 日志确定性重建模型上下文、工作记忆和 token 用量
- `/help`、`/status`、`/context`、`/compact`、`/clear`、`/exit` 命令
- `--demo` 非交互演示模式，便于测试和录制
- OpenRouter 上 `google/gemini-3.7-flash` 的流式 Function Calling
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

使用 OpenRouter 运行真实单任务：

```powershell
$env:OPENROUTER_API_KEY = "你的密钥"
.\.venv\Scripts\python.exe -m code_agent --live --prompt "概括这个项目的目录结构"
```

默认模型为固定版本 `google/gemini-3.7-flash`。模型只能请求工具；文件读取和执行均由本项目在本地完成。项目不使用 OpenRouter Agent SDK、Server Tools 或任何托管代码执行能力。

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

API key 仅通过环境变量读取，不会写入仓库、日志或视频。
