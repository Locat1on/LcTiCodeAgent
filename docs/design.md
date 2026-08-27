# 第一阶段设计

## 目标

第一阶段建立不依赖具体模型和终端框架的事件边界，验证以下最小交互闭环：

```text
用户输入 -> Agent 产生事件 -> TUI 渲染 -> JSONL 持久化 -> 等待下一轮输入
```

本阶段不实现模型 API、代码工具、上下文压缩算法和操作系统级 Sandbox。模拟 Agent 只用于验证交互与事件协议，不作为最终 Agent 能力。

## 设计原则

1. **核心逻辑自行实现。** 不引入任何 Agent 框架或 SDK。
2. **事件是统一边界。** Agent Core 只产生事件；TUI、日志和后续上下文管理共同消费事件。
3. **日志追加而不改写。** 每个事件写入独立 JSON 行，为恢复、回放、分叉和压缩提供事实来源。
4. **UI 不承载业务状态。** UI 可替换，不影响 Agent Loop。
5. **安全默认。** 当前日志不记录环境变量；未来敏感信息保护在事件进入日志前执行。

## 会话层次

```text
Session
  Turn      一次用户输入到 Agent 完成回复
    Step    一次模型请求及其工具调用（第二阶段实现）
      Tool  一次本地工具执行（第二阶段实现）
```

事件均可携带 `session_id`、`turn_id` 和 `step_id`。本阶段的模拟事件已经保留这些字段，以避免后续更改日志格式。

## 事件类型

| 事件 | 含义 |
|---|---|
| `session.started` | 会话启动 |
| `user.message` | 用户消息 |
| `assistant.delta` | 流式回复片段 |
| `assistant.message` | 完整回复 |
| `tool.requested` | 模型请求调用工具 |
| `tool.approval_required` | 工具需要用户批准 |
| `tool.started` | 工具开始执行 |
| `tool.completed` | 工具执行成功 |
| `tool.failed` | 工具执行失败 |
| `context.usage` | 上下文 token 使用量 |
| `context.cleared` | 用户清除对话上下文 |
| `context.compaction_started` | 上下文开始压缩 |
| `context.compaction_completed` | 上下文压缩完成 |
| `turn.completed` | 当前 Turn 完成 |
| `error` | 非工具类错误 |

## 后续扩展

第二阶段的模型循环、第三阶段的压缩器和安全执行层只能通过新增事件或消费既有事件接入，不得把模型客户端、文件系统操作或 Docker 调用耦合进 TUI。

