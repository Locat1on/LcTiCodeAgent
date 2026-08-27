# OpenRouter 模型接入

## 固定配置

- 模型：`google/gemini-3.7-flash`
- 接口：OpenRouter OpenAI-compatible Chat Completions
- 上下文实验预算：32,000 token
- 数据策略：请求只路由到 `data_collection=deny` 的上游

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

## 当前本地工具

- `list_files`：限制在工作区，忽略 Git、虚拟环境、缓存和构建元数据。
- `read_file`：限制在工作区，每次最多200行，并拒绝凭据文件、二进制文件和大文件。

路径在执行前会解析为绝对真实路径，解析结果超出工作区时拒绝执行。
