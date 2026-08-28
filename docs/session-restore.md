# 会话恢复：从 JSONL 日志重建模型上下文

## 定位

追加式 JSONL 会话日志（`SessionLog`）是模型上下文的**单一事实来源**。`--resume <SESSION_ID>` 用一次确定性投影把日志重放为完整的 `ContextManager` 状态：模型消息、结构化工作记忆、token 用量。恢复过程不调用模型、不执行工具、不重新读取文件——只做纯投影，同一份日志永远得到同一份上下文。

```powershell
.\.venv\Scripts\python.exe -m code_agent --live --resume <SESSION_ID> --prompt "继续刚才的任务"
```

`--resume` 必须与 `--live` 同用（模拟器不持有模型状态）。恢复后的事件继续**追加**进同一份 JSONL，`/status` 的事件计数连续。

## 投影规则

`code_agent/restore.py` 的 `project_session` 对日志做单遍扫描：

| 日志事件 | 投影行为 |
|---|---|
| `session.started` / `session.resumed` | 校验首事件为 `session.started`、所有事件属于同一会话；不进入上下文 |
| `user.message` | 重建 user 消息 |
| `assistant.message` | 重建 assistant 消息（含 payload 中的 `tool_calls`，与在线格式逐字一致） |
| `tool.requested` | 记住 `call_id → arguments`，供 tool 消息重建 |
| `tool.completed` / `tool.failed` | 重建 tool 消息：`{"ok": <是否成功>, "result": <content>}`，并保留 `source_event_id` 证据指针 |
| `context.usage` | 取最后一条的 `used_tokens` |
| `context.cleared` | 重置为全新上下文并清零 token——`/clear` 之前的消息不复活 |
| deltas、审批事件、压缩事件、`turn.completed`、`error` | 不进入上下文 |

投影完成后重建分层与工作记忆（`WorkingMemory`：修改过的文件与哈希、已验证命令、未解决错误），与在线 Agent 每轮结束时的状态逐字段一致。测试 `test_restore_rebuilds_identical_context_from_recorded_log` 以黄金等价锁定：恢复后的 `messages()`、工作记忆、`used_tokens` 与原 Agent 完全相等（含 CJK 内容的字节级一致）。

### 自描述的 assistant 消息

自本轮起，`assistant.message` 事件 payload 携带 OpenAI 格式的 `tool_calls`，日志因此可以独立重放。早于该格式的日志在投影到"未声明的工具调用"时会得到可诊断的 `RestoreError`（提示日志早于可恢复格式或已损坏），而不是在模型请求时才失败。

### 中断的工具调用

进程在工具执行中途崩溃（含审批进行中）会留下没有结果的 `tool_calls`。投影在收尾时为每个已声明但无结果的调用合成一条 tool 消息：

```json
{"ok": false, "result": "tool execution was interrupted before a result was recorded; session restored - re-run the tool call if still needed"}
```

模型因此明确知道该调用**未执行**，会重新发起；这保证恢复后的消息序列始终满足 OpenAI tool_calls/tool 配对约束。

## 审批语义

审批是一次性的：每次 ASK 工具调用都现场走 `approval_handler`，系统不持有任何持久审批状态。恢复**不复活**任何审批——日志中的 `tool.approval_required` / `tool.approval_decided` 只是审计记录，不进入模型上下文。已批准但未执行的中断调用按上一节合成"未执行"消息；模型再次请求时照常重新申请。测试 `test_restore_does_not_resurrect_approvals` 锁定该行为。

## 压缩不重放

`context.compaction_*` 事件只是报告，不含改写后的内容。恢复得到的是**未裁剪的超集**：原始工具结果全文。若超过 60% 预算，下一步的 `_maybe_compact` 会按确定性规则重新裁剪——规则是纯函数，重复执行收敛。文件 State Gate 在恢复后依然生效：上下文中的 `sha256` 若已过期（文件在两次会话之间被改动），`replace_in_file` 会拒绝并要求重读。

## 损坏日志

`SessionLog.load` 对无法解析的行抛出 `ValueError`，错误信息包含**行号**（"invalid session event at line N"），从不静默跳过。payload 形状错误（缺字段、类型错）在投影时包装为 `RestoreError` 并附带事件序号。CLI 捕获这两类错误并以退出码 2 干净退出。

## 防护

- session ID 校验 `[A-Za-z0-9-]+`，阻止通过 `--resume` 构造穿越路径。
- 会话文件不存在时给出明确错误而非空日志歧义。
- workspace 或模型与日志记录不一致时打印警告（不硬失败，容忍目录迁移与换模型）。

## 已知边界

- 只支持携带 `tool_calls` 的新格式日志（2026-08 起）；旧日志在需要配对处报错。
- 恢复不重放压缩，首个后续步骤可能触发一次重新裁剪（确定性，无信息损失）。
- `--resume` 恢复的是模型上下文与工作记忆，不是进程内的工具副作用；外部世界（文件、Git）以当前实际状态为准，State Gate 保证编辑前重新对齐。
