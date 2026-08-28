# 确定性上下文压缩（第一层）

## 定位

本项目的特色目标是：

> Task-aware, evidence-preserving context compaction for coding agents.

当前实现的是第一层：**确定性工具结果裁剪**。它不调用模型做摘要，不删除任何消息，只把旧的大块工具结果原地改写为保留证据指针的摘要。压缩触发、裁剪规则和产物全部确定、可测试、可复现。

## 分层

`ContextManager` 把消息条目分为三层：

| 层 | 内容 | 压缩行为 |
|---|---|---|
| Pinned | 系统提示 | 永不压缩 |
| Recent | 最后一条用户消息之后的所有条目 | 永不压缩 |
| Evidence | 更早轮次的条目 | 工具结果按规则改写 |

规划中的四层上下文（Pinned / Structured Working Memory / Recent Raw / Recoverable Evidence）中，结构化工作记忆目前由 `WorkingMemory` 在压缩时确定性收集（修改过的文件与哈希、已验证命令与退出码、未解决错误），但尚未注入提示词；摘要压缩留待后续阶段。

## Token 估算

`TokenCounter` 是确定性宽字符估算：CJK 及更宽字符按 1 个单位，其他字符按 0.3，向上取整且至少为 1。它是**触发器**而非计费依据，故意偏向高估，使裁剪偏早触发；展示用的 token 数仍采用模型上报值。

## 触发

- 自动：每个模型步骤开始前，估算 token 超过预算 60% 时执行压缩，产生 `context.compaction_started` 与 `context.compaction_completed` 事件；未超过则不产生任何事件。
- 手动：`/compact` 立即执行一次压缩，无论是否达到阈值。
- 预算来自 `LCTI_CONTEXT_BUDGET`（默认 32000）。

## 裁剪规则

裁剪只改写 `role=tool` 消息的 `content`，保留 `{"ok", "result"}` 信封：

| 规则 | 行为 |
|---|---|
| 重复折叠 | 相同工具与参数的重复调用只保留最新，早期改写为指向原事件的指针 |
| `run_command` 成功 | 保留 argv、exit_code、耗时；stdout/stderr 正文替换为长度说明 |
| `run_command` 失败 | 保留 argv、exit_code、stdout 尾部 400 字符、stderr 尾部 2000 字符（失败堆栈） |
| `git_status`/`git_diff`/`git_log` | 与命令同构；成功时保留 stdout 头部 1200 字符（分支/HEAD 等首行证据） |
| `read_file` | 保留路径、行范围、原事件 ID 和前 3 行，正文降级 |
| `list_files` | 保留条目数量与截断标记 |
| `fetch_url` | 保留 url、status、content_type、字节数；正文截断到 600 字符 |
| 其他大 JSON | 超长字符串字段截断到 600 字符 |

信封小于 400 字符的条目不裁剪；已裁剪条目不再重复处理（幂等）。

## 证据恢复

每个裁剪摘要内嵌原 `tool.completed` 事件的 `event_id`。会话 JSONL 永不改写，`SessionLog.recall(event_id)` 可取回完整原始结果。`tool.completed` 事件负载本身带有完整工具输出，因此恢复不依赖内存状态。

## 不变量

- 永不删除或重排消息：assistant 的每个 `tool_calls` 始终有配对的 `tool` 消息，裁剪后模型请求依然合法。
- 投影逐字节稳定：`ContextManager.messages()` 输出与原实现的 OpenAI chat 格式一致，每次调用重建新字典，外部无法通过旧引用污染内部状态。
- 压缩事件本身也进入审计日志，报告含前后估算、按规则计数和被裁剪事件 ID。

## 命令

- `/context`：显示模型上报用量、估算用量、各层条目与 token 数、工作记忆统计。
- `/compact`：立即执行确定性裁剪。

## 已知局限

- Token 估算是启发式，非精确分词。
- 第一层只改写不删除；压缩率受限于工具结果占比。
- 尚无模型驱动的结构化摘要、固定 JSON Schema 校验和从日志恢复会话。
