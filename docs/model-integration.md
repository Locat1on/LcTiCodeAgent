# OpenRouter 模型接入

## 固定配置

- 模型：`google/gemini-3.7-flash`
- 接口：OpenRouter OpenAI-compatible Chat Completions
- 上下文实验预算：32,000 token
- 数据策略：请求只路由到 `data_collection=deny` 的上游
- 失败重试：默认 2 次，可用 `LCTI_OPENROUTER_RETRIES` 配置为 0–5
- 推理强度：默认 `medium`，可用 `LCTI_REASONING_EFFORT` 配置为 `minimal / low / medium / high`

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

## Gemini 思考摘要

普通 Chat Completions 请求携带 OpenRouter `reasoning` 配置，且 `exclude=false`。Provider 按流读取 `delta.reasoning_details`：

- `reasoning.summary`：作为 `assistant.reasoning_delta(kind=summary)` 发送 UI；
- `reasoning.text`：若本轮没有 summary，则作为 `assistant.reasoning_delta(kind=provider_text)` 发送，并明确标注为提供商返回文本；
- `reasoning.encrypted`：不展示；
- 所有 detail 按上游顺序完整保存在 `assistant.message`，并在工具结果后的下一次模型请求中原样回传。

工具调用会暂停同一段模型响应。OpenRouter 要求 reasoning detail 序列保持顺序且不得改写，因此 `ContextManager` 将其作为 Assistant 消息的一部分；Session restore 也从 JSONL 重建这些字段。旧轮次被结构化压缩后，对应 reasoning detail 随完整旧消息一起移除，原始日志仍可审计。

WebSocket 对浏览器执行最小披露：浏览器只收到本轮允许显示的摘要或 provider text，不会收到 `reasoning`、签名、加密块或完整 `reasoning_details`。完整详情仅存在本地 JSONL 与后端内存。当前 Gemini 3.7 Flash 的 OpenRouter 路由在真实冒烟中返回 `reasoning.text` 而非 `reasoning.summary`，因此 UI 使用不同标题，避免把它误称为摘要或完整思维链。

## 当前本地工具

- `list_files`：限制在工作区，忽略 Git、虚拟环境、缓存和构建元数据。
- `read_file`：限制在工作区，每次最多200行，并拒绝凭据文件、二进制文件和大文件。
- `replace_in_file`：只允许对已读取内容进行唯一精确替换，并使用同目录原子写入。
- `write_file`：只能创建新文件，拒绝覆盖已有路径。
- `run_command`：以参数数组执行，不经过 Shell；只允许测试、检查、构建和编译命令。

路径在执行前会解析为绝对真实路径，解析结果超出工作区时拒绝执行。
