# LcTiCodeAgent

一个从零实现的可对话终端编程智能体。本项目不依赖任何 Agent 框架或 SDK，模型交互、会话历史、上下文管理、工具执行、循环终止与错误处理均由项目自行实现。

当前已建立追加式事件日志、可对话终端 UI，并接入 OpenRouter 流式 Function Calling、本地文件工具和受限命令执行。后续将在同一事件协议上继续实现任务感知上下文压缩与安全执行环境。

## 当前功能

- 交互式终端会话
- 流式显示 Agent 回复
- 显示工具调用、成功、失败和审批状态
- 显示上下文 token 使用量
- 将全部事件持久化为 JSONL
- `/help`、`/status`、`/context`、`/clear`、`/exit` 命令
- `--demo` 非交互演示模式，便于测试和录制
- OpenRouter 上 `google/gemini-3.7-flash` 的流式 Function Calling
- 本地读取、精确替换、新建文件和验证命令工具

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

运行测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

API key 仅通过环境变量读取，不会写入仓库、日志或视频。
