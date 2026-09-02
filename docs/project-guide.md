# LcTiCodeAgent 项目实现说明

本文面向项目作者和答辩准备，系统说明 LcTiCodeAgent 已实现的功能、内部运行方式、设计理由、验证证据和能力边界。它不是运行手册的简单扩写，而是帮助你理解“这个 Agent 为什么能工作”。

## 1. 项目是什么

LcTiCodeAgent 是一个从零实现的本地编程智能体。用户可以在 CLI 或 Web UI 中描述任务，模型随后通过本地工具检查代码、修改文件、运行验证，并根据工具结果继续推理，直到完成任务或遇到明确的终止条件。

它和普通聊天机器人的核心区别是：模型不只是返回一段代码，而是进入“模型决策—本地执行—证据反馈—继续决策”的闭环。

```text
用户任务
  ↓
模型读取当前上下文并决定下一步
  ↓
输出文本，或请求一个本地 Tool Call
  ↓
安全管线检查权限、参数与状态
  ↓
本地执行工具并产生可审计结果
  ↓
Tool Result 返回模型上下文
  ↓
模型继续操作，或给出最终回答
```

项目没有使用 LangChain、OpenAI Agents SDK、AutoGen 等 Agent 框架，也没有使用厂商托管的代码执行或文件工具。`openai` Python 包只承担 OpenAI-compatible HTTP 客户端的职责，Agent Harness 的主要逻辑由项目自行实现。

## 2. 总体架构

项目可以分成六个相互衔接的部分。

| 层次 | 作用 | 主要模块 |
|---|---|---|
| 交互层 | 接收任务、展示流式事件和审批 | `cli.py`、`ui.py`、`web.py`、`web_static/` |
| Agent Loop | 决定何时请求模型、执行工具和终止 | `live_agent.py` |
| Provider 层 | 调用模型、解析流式输出和 Tool Call | `openrouter.py`、`model.py` |
| 工具与安全层 | 定义工具、执行本地动作并限制权限 | `tools.py`、`security.py`、`command.py`、`network.py`、`git_tools.py` |
| 上下文层 | 管理消息、工作记忆、压缩和恢复 | `context.py`、`summary.py`、`restore.py` |
| 事件与持久化层 | 记录完整过程并支持审计、恢复 | `events.py`、`session.py` |

CLI 和 Web UI 只是两个表现层。它们共用同一个 `LiveAgent`、工具注册表、安全策略、上下文管理器和 JSONL Session 日志，因此 Web UI 不是在另一个 Agent 产品外面包装界面。

## 3. Agent Loop 如何运行

`LiveAgent.respond()` 是整个系统的核心生成器。一次用户任务的大致流程如下。

### 3.1 接收任务

用户文本先作为 `user` 消息加入 `ContextManager`。系统提示词长期固定在上下文首部，规定三种任务模式：

- 回答、解释和评审：只读检查，不修改文件；
- 诊断：说明原因，除非用户同时要求修复，否则不修改；
- 修改或构建：做满足目标的最小改动，并运行验证。

提示词还要求模型先查证、避免无关重构、如实报告工具证据，并在失败后调整方案而不是原样重复调用。

### 3.2 请求模型

每个步骤开始前，Agent 检查上下文预算，必要时执行压缩。随后把当前消息列表和本地工具的 JSON Schema 发送给 Provider。

Provider 以流式方式返回：

- 普通文本增量；
- reasoning summary 或提供商返回的 reasoning text；
- Tool Call 的名称和参数片段；
- token 用量；
- finish reason。

这些内容分别转换为内部 `ModelEvent`，再由 Agent 转换为统一的 `AgentEvent`，供 CLI、WebSocket 和 Session 日志消费。

### 3.3 解析 Tool Call

模型厂商可能把一个 Tool Call 拆成多个流式片段。`ToolCallAccumulator` 按调用索引重组 ID、函数名和 JSON 参数，在流结束后统一检查：

1. 调用索引必须非负；
2. Call ID 必须存在且不能重复或冲突；
3. 工具名必须存在；
4. 参数必须是合法 JSON；
5. 参数顶层必须是对象。

只有解析完成的调用才会交给 `ToolRegistry`。模型不能绕过注册表直接执行本地函数。

### 3.4 执行工具并继续

Tool Call 经过安全管线后执行，结果被包装为：

```json
{"ok": true, "result": "..."}
```

或：

```json
{"ok": false, "result": "..."}
```

Tool Result 与原 Call ID 配对后加入上下文。Agent 再次请求模型，使模型能够根据真实文件内容、测试退出码或错误信息调整下一步。

### 3.5 终止条件

以下情况会结束当前 turn：

- 模型没有继续请求工具并正常停止；
- 达到最大步骤数，默认 16 步，可配置为 4–64；
- Provider 出现不可恢复错误；
- Tool Call 解析失败；
- Web 层发生错误；
- 用户点击停止，Agent 在下一个事件边界协作式取消。

系统不会因为模型写出“测试已经通过”就认为任务完成。对于代码修改，成功结论必须有退出码为 0 的验证命令作为证据。

## 4. 已实现的本地工具

| 工具 | 功能 | 默认权限 |
|---|---|---|
| `list_files` | 列出工作区文件与目录 | ALLOW |
| `read_file` | 分段读取 UTF-8 文本，并返回整文件 SHA-256 | ALLOW |
| `search_text` | 在工作区执行正则文本搜索 | ALLOW |
| `replace_in_file` | 对已读取文件执行唯一精确替换 | ALLOW |
| `write_file` | 创建新文件，不覆盖已有文件 | ALLOW |
| `run_command` | 运行测试、lint、typecheck、build 或编译命令 | ALLOW |
| `fetch_url` | 获取公开 HTTPS 文本 | ASK |
| `git_status` | 读取 Git 状态 | ALLOW |
| `git_diff` | 读取工作区或指定文件差异 | ALLOW |
| `git_log` | 查看最近提交 | ALLOW |
| `git_commit` | 选择性创建本地提交 | ASK |
| `git_push` | 推送当前 HEAD 到同名远程分支 | ASK |

文件修改采用两个不同工具是有意设计：已有文件只能使用 `replace_in_file`，新文件只能使用 `write_file`。这使“覆盖整个文件”和“意外覆盖已有路径”在工具层没有入口。

`run_command` 不是任意 Shell。它使用参数数组与 `shell=False`，只允许验证相关命令，并把 Python 固定到项目 `.venv`。安装、发布、任意 Python 表达式和网络命令不会通过该工具执行。

## 5. 上下文管理：项目的主要特色

编程任务会产生大量文件内容、搜索结果和测试日志。如果把全部历史永久发送给模型，请求会越来越昂贵；如果简单删除旧消息，又可能丢失修改原因和验证证据。项目为此实现了两级、可追溯的压缩机制。

### 5.1 消息层与工作记忆

模型消息分为三个层次：

- `Pinned`：系统提示词等必须长期保留的内容；
- `Recent`：当前和最近 turn 的原始对话；
- `Evidence`：较早的工具结果，可按确定性规则裁剪。

此外还有独立的结构化 `WorkingMemory`，记录：

- 修改过的文件及最新哈希；
- 已运行命令及退出码；
- 尚未解决的错误。

因此 Web UI 中看到的四类信息，实际是三层模型消息加一份独立工作记忆，而不是四种都直接作为普通聊天消息存放。

### 5.2 Token 估算

`TokenCounter` 使用确定性的宽字符估算：CJK 等宽字符按 1 个单位计数，普通字符约按 0.3 个单位计数，并向上取整。这不是模型 tokenizer 的精确复刻，但会有意偏保守，使压缩宁可略早触发，也不在接近上限时才处理。

界面展示的实际使用量仍优先采用模型返回的 usage。

### 5.3 第一级：确定性工具结果裁剪

上下文估算达到预算的 60% 时，系统优先改写旧 Evidence，而不是调用模型摘要。不同工具采用不同的纯函数规则：

- 成功测试保留 argv、退出码、耗时等结论，删除大段 stdout；
- 失败命令保留更长的 stderr 尾部，避免丢失错误原因；
- `read_file` 保留路径、行范围、首部内容和原事件 ID；
- `search_text` 保留查询、搜索范围和匹配数量；
- `list_files` 保留条目数量与截断状态；
- `fetch_url` 保留 URL、状态、内容类型和正文头部；
- 相同工具与参数的重复调用只保留最新结果，旧结果变成指针。

这一层不依赖模型，规则可重复、可测试，并且重复执行会收敛。

### 5.4 第二级：经事实校验的结构化摘要

上下文继续增长到 75% 时，默认 `validated` 策略把完整旧 turn 发送给模型，要求返回固定 JSON Schema，字段包括：

- 用户目标；
- 已完成事项；
- 设计决策；
- 文件和标识符；
- 命令与退出码；
- 未解决错误；
- 下一步；
- 原始事件 ID。

模型返回后，本地校验器检查字段集合、类型、数量上限，并要求路径、标识符、数字、退出码和 event ID 必须来自源消息。摘要若虚构受保护事实会被拒绝，原上下文继续保留。目标是尽量压回 50%，但 50% 是软目标，不会为了达到比例而破坏当前任务的消息配对。

### 5.5 证据回溯

压缩只改变发给模型的上下文投影，不改写原始 JSONL。裁剪结果保留 `source_event_id`，用户可以通过：

```text
/recall EVENT_ID
```

重新取得完整原始事件。这个设计使压缩后的结论仍然可以追溯到原工具输出。

### 5.6 可比较策略

项目提供四种策略：

- `none`：不压缩；
- `drop_oldest`：直接删除最旧完整 turn；
- `plain_summary`：普通自由文本摘要，不做事实校验；
- `validated`：确定性裁剪加结构化事实校验摘要。

四种策略使用同一个 Provider、Agent Loop、工具和安全管线，便于比较任务成功、token、重复读取和证据准确率，而不是使用互不相干的模拟程序。

### 5.7 为什么当前活跃 turn 不直接压缩

上下文阈值是软预算，不是对当前消息的强制截断。模型在一个 turn 内可能已经声明 Tool Call，但对应 Tool Result 尚未产生；如果此时删除或摘要当前 turn，可能破坏 OpenAI-compatible 协议要求的 assistant tool-call / tool-result 配对。

因此，当前活跃 turn 的原始消息会优先保留。阈值检查仍会产生压缩事件，但在没有安全目标时报告 `changed=false`。Turn 完成后，其工具结果进入 Evidence 层，下一 turn 才能进行确定性裁剪或结构化摘要。

Mini HTTP Cache 彩排验证了这一行为：第一轮只读分析期间，上下文超过演示预算，但系统没有改写活跃 turn；第二轮恢复 Session 后，第一轮成为完整 Evidence，随即发生实际裁剪和摘要。这个设计以消息合法性和证据完整性优先，因此 `context_budget` 应理解为压缩触发预算，而不是任何时刻都不能超过的硬上限。

## 6. Session 日志与确定性恢复

每次会话都对应一份追加式 JSONL。日志中的主要事件包括：

- `session.started`、`session.resumed`、`session.renamed`；
- `user.message`、`assistant.message` 和流式 delta；
- `tool.requested`、审批、开始、完成与失败；
- `context.usage`、清除与压缩事件；
- `turn.completed` 和 `error`。

恢复会话时，`project_session()` 单遍扫描日志，重建 user、assistant 和 tool 消息，并恢复 Tool Call 配对、WorkingMemory 与 token 用量。恢复过程不调用模型、不重新执行工具，也不重新读取文件。

如果程序在工具执行中途退出，日志可能存在已声明但没有 Tool Result 的调用。恢复器会合成一条明确的失败结果，告诉模型该工具没有执行，避免生成不合法的 assistant/tool 消息序列。

审批不会被恢复为长期权限。恢复后模型若再次请求高风险动作，必须重新获得一次性批准。

## 7. 安全管线如何工作

项目不把安全性寄托在系统提示词上。每个 Tool Call 都经过模型外部的固定管线：

```text
Tool Call
  → Capability Gate
  → ALLOW / ASK / DENY
  → 参数、路径与状态检查
  → 必要时等待用户一次性批准
  → 批准后重新检查状态
  → 本地执行
  → Evidence Ledger
  → Tool Result 返回模型
```

### 7.1 Capability Gate

- `ALLOW`：工作区读取、搜索、受限编辑、验证命令和 Git 只读；
- `ASK`：HTTPS 请求、Git commit 和 Git push；
- `DENY`：未知工具或策略中没有注册的能力。

工具名大小写变体、尾随空格或模型在参数中伪造 `approved=true` 都不能绕过注册表和审批处理器。

### 7.2 文件边界与 State Gate

所有路径先解析为绝对真实路径，再确认仍位于工作区。系统拒绝 `.env`、私钥、SSH 和云凭据路径，并跳过二进制、大文件和隐藏目录。

`read_file` 返回整个文件的 SHA-256。模型调用 `replace_in_file` 时必须提交这个哈希。执行前系统重新计算当前文件哈希；如果文件在读取后发生变化，本次修改被拒绝，模型必须重新读取。这解决了“模型依据旧内容修改新文件”的状态竞争问题。

### 7.3 命令边界

命令通过 `subprocess` 参数数组执行，不经过 Shell。子进程只获得必要环境变量，不继承模型 API Key，并带有超时与输出长度限制。工具结果记录实际 argv、退出码、耗时和是否超时。

### 7.4 网络边界

`fetch_url` 只允许 HTTPS GET/HEAD，拒绝 URL 凭据、私有地址、非文本响应和超限响应。请求与重定向目标都会重新检查，并且每次调用都需要用户批准。

它是受控文本获取工具，不是强 SSRF 隔离。DNS 检查与实际连接之间仍存在恶意 rebinding 的理论窗口。

### 7.5 Git 写操作

`git_commit` 只允许提交模型明确列出的文件。审批前展示 HEAD、状态、文件哈希、Diff 摘要、无关已暂存文件和凭据扫描结果；批准后重新计算状态令牌，任何变化都会使批准失效。

`git_push` 只允许把当前 HEAD 推送到已有远程的同名分支。工具没有 force、mirror、delete、tag 或任意 refspec 参数，并在审批前扫描待推送 HEAD 中的高置信度凭据。

## 8. 模型接入与可靠性

默认模型是 OpenRouter 上的 `google/gemini-3.7-flash`。Web UI 还可以选择服务端已经配置的：

- Google Gemini 直连；
- DeepSeek；
- OpenAI；
- 自定义 HTTPS OpenAI-compatible 接口。

API Key 可以来自服务端环境变量，也可以通过 Web 密码输入框发送到 localhost。Web 输入的密钥只保存在当前后端进程内存，不修改 `os.environ`，服务重启后消失。`/api/providers` 不返回密钥；浏览器也不会把密钥写入 Local Storage、Cookie 或 Session 日志。环境变量方式仍更适合录屏和自动化，因为密钥不需要经过浏览器。

切换厂商会新建会话。`session.started` 保存非敏感的 provider ID 和模型名，恢复旧会话时使用原 Provider，避免把某家模型的 reasoning detail 或 Tool Call 状态混入另一家。

### 8.1 有限重试

Provider 把 HTTP 408、409、429、5xx 和连接类故障视为暂时错误，默认最多重试两次，并采用有限指数退避。OpenAI 客户端自身的自动重试被关闭，避免两层重试叠加。

只有在尚未向用户输出文本或 usage、也尚未执行工具时才允许重试。一旦已经显示部分输出，失败立即转成可见错误，不会重放已显示文本或造成工具重复执行。

### 8.2 推理展示

OpenRouter 可能返回：

- `reasoning.summary`：可展示摘要；
- `reasoning.text`：提供商返回的推理文本；
- `reasoning.encrypted`：加密状态块。

Web UI 只展示前两类，并明确区分“摘要”和“提供商返回文本”；加密块、签名和完整 `reasoning_details` 不发送给浏览器。展示内容不能等同于模型完整内部思维链。

## 9. CLI 功能

CLI 使用 Rich 构建交互界面，支持流式回复、工具状态、审批和上下文用量。主要命令包括：

- `/help`：查看命令；
- `/status`：查看 Session、模型和事件状态；
- `/context`：查看上下文分层和 WorkingMemory；
- `/compact`：手动触发当前策略的压缩；
- `/recall EVENT_ID`：回溯原始事件；
- `/clear`：清除模型上下文但保留追加式日志；
- `/exit`：退出。

CLI 支持 `--prompt` 单任务模式、`--resume` 恢复会话和 `--demo` 离线演示。

## 10. Web UI 功能

Web 后端使用 Starlette 与 Uvicorn，前端使用原生 HTML、CSS 和 JavaScript，没有引入 FastAPI、React 或 Node 构建链。

`LiveAgent` 是同步生成器。Web 服务把 turn 放入工作线程，通过 `asyncio.Queue` 把 `AgentEvent` 送到 WebSocket，再由浏览器渲染。浏览器不直接持有 Provider，也不能直接调用本地工具。

Web UI 提供：

- 流式用户、助手、reasoning 和工具轨迹；
- 工具详情、退出码、耗时与错误；
- 网络和 Git 操作的一次性审批卡片；
- Context、安全与 Git 检查器；
- 会话列表、新建、恢复、重命名和软删除；
- 模型厂商与模型选择；
- 协作式停止；
- 响应式侧栏。

### 10.1 智能输入框

- `/`：选择 Web 命令；
- `@`：选择工作区文件，仅附加路径和“先读取”的指示，不在选择阶段发送文件正文；
- `#`：附加 Git 状态、Diff、Context、Session 或指定 Event 等结构化证据。

引用在日志中保留 `text / references / model_text` 三层表示。浏览器显示用户原始文本和引用标签，真正展开的证据进入 `model_text`；恢复会话时使用 `model_text`，但 WebSocket 不把展开后的大段证据再次返回浏览器。

### 10.2 Web 安全

- 只允许绑定 `127.0.0.1`、`localhost` 或 `::1`；
- WebSocket 拒绝非本机 Origin；
- Trusted Host 与 CSP 限制访问面；
- 动态内容使用 `textContent`，不把模型文本当 HTML 插入；
- 响应设置 `nosniff`、`no-referrer` 和 `no-store`；
- 断开、停止或审批超时均按拒绝处理。

## 11. Mini HTTP Cache：一个展示完整功能的真实任务

为了同时验证长任务、上下文压缩、安全编辑和中断恢复，项目构造了一个独立的 Mini HTTP Cache 仓库。它采用仓库级 issue 形式，不属于公开 benchmark 数据集，也没有向模型提供参考补丁。

### 11.1 任务内容

Agent 需要修复一个跨模块 HTTP 缓存实现，涉及：

- Header 大小写不敏感查询和合并；
- `Cache-Control`、quoted `max-age`、`no-store` 和 `stale-if-error`；
- `Vary` 规范化、多变体隔离和驱逐；
- URL scheme/host、默认端口、fragment、query 和空 path 规范化；
- 请求 `no-cache` 与 `only-if-cached`；
- 响应 `Age`、ETag 和 304 重验证；
- variant 级 LRU 容量限制；
- 不修改公共 dataclass、调用方 header 映射或测试。

录制任务包含 64 项固定测试：59 项完整回归和 5 项代表性 acceptance tests。原始实现有 35 个 FAIL 和 10 个 ERROR。

### 11.2 直观的修复前后对比

同一条命令：

```powershell
python -m unittest tests.test_acceptance -v
```

修复前显示：

```text
equivalent URLs       FAIL
language variants     FAIL
no-store eviction     FAIL
only-if-cached        FAIL
stale-if-error        ERROR
```

其中包括中文请求错误得到 `b'english'`、`only-if-cached` 仍访问源站，以及 `no-store` 后旧缓存仍存在等可理解的行为错误。

参考修复后，同一组测试变为 5 个 `ok`，完整套件显示 `Ran 64 tests ... OK`。随后 Git diff 只包含 5 个实现文件，形成“行为差异—代码差异—完整回归”三层证据。

### 11.3 Turn 1：只读分析

第一轮要求模型只读检查 issue、实现和全部测试，并复现失败，不允许编辑。Agent 运行 `list_files`、`read_file` 和完整 unittest，最终输出 URL → Header/Policy → Store/LRU → Client 的实施顺序。

这一步验证了：

- 任务模式能够约束“分析时不修改”；
- 失败命令被如实记录；
- Git 状态保持 clean；
- 大量文件与测试结果进入当前上下文；
- 全部事件写入追加式 JSONL。

彩排耗时为 124.567 秒。使用 16k 演示预算时，第一轮上下文超过阈值，但因为当前 turn 尚未完成，系统按消息配对不变量保留原始内容。

### 11.4 Session 恢复与 Turn 2 实施

第二轮从同一 Session 日志恢复，而不是重新把旧内容复制给模型。`project_session()` 重建 user、assistant、tool 消息、WorkingMemory、token 和 Tool Call 配对。第一轮此时已经成为可压缩的 Evidence。

Turn 2 开始后产生了真实压缩证据：

| 类型 | 压缩前 | 压缩后 | 结果 |
|---|---:|---:|---|
| 确定性工具裁剪 | 50,700 | 33,965 | 18 条工具结果被裁剪 |
| 结构化事实摘要 | 45,558 | 13,597 | 44 个上下文条目被摘要，校验通过 |

在此之前，若模型摘要没有通过路径、数字、退出码或 event ID 的来源校验，摘要会被拒绝，原上下文继续保留。后续一次合法摘要通过后才真正替换旧 turn。

随后 Agent 依次修改：

- `mini_cache/url.py`；
- `mini_cache/headers.py`；
- `mini_cache/policy.py`；
- `mini_cache/store.py`；
- `mini_cache/client.py`。

每个已有文件都先通过 `read_file` 取得 SHA-256，再由 `replace_in_file` 进行状态绑定编辑；修改后先运行模块级测试，再运行完整套件。

### 11.5 Provider 错误与 Turn 3 收尾

彩排中 Turn 2 已完成 59 项核心测试，但最终模型请求遇到 OpenRouter HTTP 429。Provider 没有把错误当成正常结束，也没有声称任务完成；日志产生可见模型错误和 `turn.completed`。

第三轮再次恢复相同 Session，只要求重新验证并完成收尾。恢复时又发生：

| 类型 | 压缩前 | 压缩后 | 结果 |
|---|---:|---:|---|
| 确定性工具裁剪 | 35,174 | 24,119 | 12 条工具结果被裁剪 |
| 结构化事实摘要 | 24,119 | 1,626 | 38 个上下文条目被摘要，校验通过 |

Agent 随后重新运行完整测试、检查 Git status/diff，并生成最终回答。三轮彩排累计 342.410 秒，即 5 分 42 秒。

### 11.6 这个任务展示了什么

这不是单纯展示模型会写缓存代码，而是把项目能力连成完整闭环：

```text
只读分析与失败复现
→ JSONL 持久化
→ Session restore
→ Evidence 层确定性裁剪
→ 结构化摘要与事实校验
→ SHA-256 绑定编辑
→ 模块测试与完整回归
→ Provider 429 显式错误
→ 再次恢复、压缩与验证
→ Git diff 和证据化最终回答
```

Web 录制时还可以在最后请求 Git commit，并在审批卡片中选择拒绝，展示 Git Preflight、Secret Scan、一次性 ASK 权限与拒绝后的 Tool Result。

## 12. 测试与验证证据

截至 2026-09-02，项目有 192 项自动化测试，覆盖：

- 事件序列化与 Session 日志；
- Tool Call 流式重组和异常参数；
- 文件、搜索、命令、网络和 Git 工具；
- ALLOW/ASK/DENY 与审批状态令牌；
- 确定性裁剪、结构化摘要和事实校验；
- Session restore 与中断 Tool Call；
- OpenRouter 重试和 reasoning detail；
- WebSocket、引用、审批、会话操作和 Provider 选择；
- 提示注入、隐藏 Unicode、伪造成功和重复 Call ID 等红队场景。

这里的 191 项是 LcTiCodeAgent 自身测试，不包含视频任务仓库。视频任务另有 64 项行为与回归测试；隔离 gold 实现已验证 64/64 通过，录制基线固定为 4 个 acceptance FAIL、1 个 ERROR，并保留干净 Git 状态。

真实 OpenRouter 证据包括：`buggy_average` 修复、四种上下文策略对比，以及 Mini HTTP Cache 的多轮完整彩排。后者实际执行了两级上下文压缩、Session restore、Provider 错误恢复和跨 5 个文件的修复流程。

## 13. 已知边界

理解限制和理解功能同样重要。

1. Workspace Policy 是应用层 Sandbox，不是容器或内核隔离。白名单可以阻止模型直接请求危险命令，但被允许执行的测试代码仍以当前 Windows 用户身份运行。
2. 结构化摘要能检查关键事实来源，但不能证明所有自由文本语义完全等价。
3. 当前真实上下文对比是单任务、单次运行；`validated` 有较强的单次压缩比例，但总 prompt token 没有稳定优于基线。
4. Google、DeepSeek 和 OpenAI 直连已完成配置与协议测试，但真实端到端证据目前主要来自 OpenRouter。
5. 文件编辑强调精确替换和新建，不适合超大规模自动重构；项目也没有提供任意文件删除工具。
6. Web 停止是事件边界上的协作式取消，不会强杀正在进行的 Python 调用。
7. 会话删除属于软删除，日志进入 `.trash`；目前没有 Web 回收站，恢复需要人工操作。
8. 上下文预算是软触发线。为保护当前 turn 的 Tool Call 配对，单个超长活跃 turn 可能暂时超过预算，只有完整旧 turn 才能安全进入 Evidence 裁剪或结构化摘要。

## 14. 如何概括项目的设计主线

项目可以用一句话概括：

> LcTiCodeAgent 通过自行实现的 Agent Loop 驱动本地编程工具，并以可恢复上下文和模型外部安全管线保证长任务中的证据连续性与操作可控性。

答辩时最值得强调的两个核心点是：

1. **可验证、可恢复的上下文管理**：压缩不是不可逆删除，摘要中的关键事实有来源约束，原始事件始终可回溯；
2. **不依赖模型自觉的安全管线**：权限、路径、状态、审批和 Git 边界由本地代码强制执行，即使模型受到提示注入也不能直接越过工具入口。

Web UI、Session 管理和多厂商 API 是支撑这两条主线的产品工程能力，而不是彼此孤立的功能堆叠。
