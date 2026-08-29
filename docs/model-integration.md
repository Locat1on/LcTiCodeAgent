# OpenRouter 模型接入

## 固定配置

- 模型：`google/gemini-3.7-flash`
- 接口：OpenRouter OpenAI-compatible Chat Completions
- 上下文实验预算：32,000 token
- 数据策略：请求只路由到 `data_collection=deny` 的上游
- 失败重试：默认 2 次，可用 `LCTI_OPENROUTER_RETRIES` 配置为 0–5

固定模型 slug，而不使用自动指向最新版本的别名，以便复现实验。

## 边界

项目仅使用普通模型客户端发送消息和接收流。以下逻辑均由项目自行实现：

- 流式文本处理
- Tool Call 参数分片重组
- JSON 参数校验
- 多步模型循环
- 本地工具执行
- Tool Result 回传
- 循环终止与错误归一化
- 会话事件持久化

项目不使用 OpenRouter Agent SDK、Server Tools、托管代码执行或托管文件工具。

## 流式工具调用

Tool Call 的 ID、名称和 JSON 参数可能分散在多个流式响应块中。`ToolCallAccumulator` 按调用索引收集片段，在流结束后验证：

1. 调用索引非负；
2. Tool Call ID 存在且不冲突；
3. 函数名存在；
4. 参数是合法 JSON；
5. 参数顶层必须是对象。

验证通过后才进入本地 `ToolRegistry`。模型不能直接执行任何函数。

## 错误与重试语义

Provider 将 HTTP 408、409、429、5xx 以及无 HTTP 状态的连接类错误视为暂时故障，采用 0.5 秒起步的有限指数退避。普通模型流和结构化摘要请求共用同一重试预算；OpenAI 客户端内置重试被关闭，避免两层重试叠加后失去上限。

流式请求只有在尚未向上层发出文本或 usage 事件时才能重试。Tool Call 参数片段在流完整结束前不会交给 `LiveAgent`，因此此时重建请求不会重复执行工具。一旦已经显示部分文本或 usage，后续故障立即转为 `OpenRouterRequestError`，不重放内容。

上游返回 `finish_reason="error"` 时不再作为正常结束透传：空响应可在预算内重试；已有部分输出则立即产生可见的 `error` 事件，当前 turn 以 `model_error` 结束。HTTP 400、401、403 等确定性请求或权限错误不重试，错误消息只保留异常类型和 HTTP 状态，不记录上游响应正文。

## 当前本地工具

- `list_files`：限制在工作区，忽略 Git、虚拟环境、缓存和构建元数据。
- `read_file`：限制在工作区，每次最多200行，并拒绝凭据文件、二进制文件和大文件。
- `replace_in_file`：只允许对已读取内容进行唯一精确替换，并使用同目录原子写入。
- `write_file`：只能创建新文件，拒绝覆盖已有路径。
- `run_command`：以参数数组执行，不经过 Shell；只允许测试、检查、构建和编译命令。

路径在执行前会解析为绝对真实路径，解析结果超出工作区时拒绝执行。
