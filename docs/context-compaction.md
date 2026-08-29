# 两级、可追溯的上下文压缩

## 定位

本项目的特色目标是：

> Task-aware, evidence-preserving context compaction for coding agents.

实现分为两级：第一层是**确定性工具结果裁剪**，第二层是**经本地事实校验的结构化 LLM 摘要**。第一层不调用模型，只原地改写旧工具结果；第二层在上下文继续增长时，用固定 JSON Schema 汇总完整旧轮次。两层都保留原事件 ID，原始 JSONL 永不改写。

## 分层

`ContextManager` 把消息条目分为三层：

| 层 | 内容 | 压缩行为 |
|---|---|---|
| Pinned | 系统提示 | 永不压缩 |
| Recent | 最后一条用户消息之后的所有条目 | 永不压缩 |
| Evidence | 更早轮次的条目 | 工具结果按规则改写 |

逻辑上的四层为 Pinned、Structured Working Memory、Recent Raw 和 Recoverable Evidence。`WorkingMemory` 确定性收集修改文件与哈希、已验证命令与退出码、未解决错误；结构化摘要作为 Pinned 消息注入，Recent Raw 不参与压缩，旧证据可由事件 ID 从日志取回。

## Token 估算

`TokenCounter` 是确定性宽字符估算：CJK 及更宽字符按 1 个单位，其他字符按 0.3，向上取整且至少为 1。它是**触发器**而非计费依据，故意偏向高估，使裁剪偏早触发；展示用的 token 数仍采用模型上报值。

## 触发

- 自动第一层：每个模型步骤开始前，估算 token 超过预算 60% 时执行确定性裁剪。
- 自动第二层：进入该步骤前超过 75%，且第一层后仍高于 50%，调用同一 OpenRouter 模型生成结构化摘要，目标压回 50%。若 Recent Raw 本身已超过 50%，事件会如实记录 `target_met=false`。
- 手动：`/compact` 先执行第一层；若仍高于 50%，再尝试第二层。
- 预算来自 `LCTI_CONTEXT_BUDGET`（默认 32000）。

## 结构化摘要与事实校验

`code_agent/summary.py` 提供固定的 `SUMMARY_SCHEMA`，只接受以下字段：目标、已完成事项、决策、文件与标识符、命令与退出码、未解决错误、下一步，以及对应事件 ID。OpenRouter 请求把完整 Schema 与旧上下文一同发送，使用 `json_object` 保证 JSON 语法、温度为 0；提供商返回后由本地校验器严格执行字段、长度和事实约束。因此安全边界不依赖上游是否实现某个 `strict` 方言。

模型返回后先在本地检查：

- 顶层和每类条目的字段必须与固定 JSON Schema 完全一致，额外字段被拒绝；
- 路径、代码标识符、命令参数、数字和 exit code 必须逐项出现在被摘要的原消息中；
- 每个 `event_id` 必须属于本次被摘要的工具结果；
- 只有校验通过才原子替换旧完整轮次；失败时保留原上下文，并在压缩事件中记录 `validation=rejected`。

结构化摘要事件含摘要本身，因此 `--resume` 会重新执行相同本地校验并重建压缩后的上下文；被篡改的会话日志不会静默进入模型上下文。

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
| `search_text` | 保留 query、路径、引擎、检索文件数、returned、truncated 与命中数量，丢弃 matches 列表 |
| `fetch_url` | 保留 url、status、content_type、字节数；正文截断到 600 字符 |
| 其他大 JSON | 超长字符串字段截断到 600 字符 |

信封小于 400 字符的条目不裁剪；已裁剪条目不再重复处理（幂等）。

## 证据恢复

每个裁剪或结构化摘要都保存原 `tool.completed` / `tool.failed` 事件的 `event_id`。会话 JSONL 永不改写，`SessionLog.recall(event_id)` 或终端命令 `/recall EVENT_ID` 可取回完整原始结果。`tool.completed` 事件负载本身带有完整工具输出，因此恢复不依赖内存状态。

## 不变量

- 第一层永不删除或重排消息。第二层只原子替换最后一条用户消息之前的完整旧轮次，不会留下失配的 assistant/tool 消息。
- 投影逐字节稳定：`ContextManager.messages()` 输出与原实现的 OpenAI chat 格式一致，每次调用重建新字典，外部无法通过旧引用污染内部状态。
- 压缩事件本身也进入审计日志，报告含前后估算、按规则计数和被裁剪事件 ID。

## 命令

- `/context`：显示模型上报用量、估算用量、各层条目与 token 数、工作记忆统计。
- `/compact`：立即执行第一层；若仍超过 50% 则继续结构化摘要。
- `/recall EVENT_ID`：从追加式会话日志显示某个原始事件。

## 对比实验

运行 `python -m experiments.context_compaction`，可在同一确定性夹具上比较 `no_compression`、`drop_oldest`、`plain_summary` 和 `validated_structured_summary`。统一采集估算 token、压缩率、关键事实召回率、事件 ID 召回率、工具消息配对有效性和校验状态。该夹具用于验证机制与指标管线，不冒充真实模型质量基准；后续可用真实任务日志扩充样本。

## 已知局限

- Token 估算是启发式，非精确分词。
- 事实校验保证受保护字段来自原事件，但不能证明自由文本语义等价；因此提示词要求“不确定则省略”，并保留事件级回溯。
- 目标 50% 是软目标：当前轮次受到保护，若其自身过大则不会为了达标而破坏 Recent Raw。
