# LcTiCodeAgent

一个从零实现的可对话终端编程智能体。本项目不依赖任何 Agent 框架或 SDK，模型交互、会话历史、上下文管理、工具执行、循环终止与错误处理均由项目自行实现。

当前完成第一阶段：以追加式事件日志为核心，建立可对话终端 UI 和可替换的模拟 Agent 事件流。后续阶段将在同一事件协议上接入模型原生 tool calling、本地工具、任务感知上下文压缩与安全执行环境。

## 当前功能

- 交互式终端会话
- 流式显示 Agent 回复
- 显示工具调用、成功、失败和审批状态
- 显示上下文 token 使用量
- 将全部事件持久化为 JSONL
- `/help`、`/status`、`/context`、`/clear`、`/exit` 命令
- `--demo` 非交互演示模式，便于测试和录制

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

运行测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

API key 将在接入真实模型后仅通过环境变量读取，不会写入仓库、日志或视频。
