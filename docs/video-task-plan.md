# 视频主任务：Mini HTTP Cache 完整修复流程

## 1. 目标

该任务用于在一个连续开发流程中展示 LcTiCodeAgent 的主要能力，而不是生成 benchmark 排名：

- 多轮 Agent Loop；
- reasoning 与工具轨迹；
- 失败测试复现；
- 跨模块文件编辑与 SHA-256 State Gate；
- 确定性工具结果裁剪；
- 经事实校验的结构化摘要；
- Event 回溯与 WorkingMemory；
- Session 日志恢复；
- Provider 错误可见化和恢复；
- 全量测试、Git status/diff；
- 可选的 Git commit 一次性审批。

任务采用仓库级 issue 形式，所有要求和 64 项测试在开始前固定，其中 59 项是完整回归，5 项是从相同行为中抽取的视频 acceptance 对比。测试没有 sleep，原始流程耗时来自真实模型分析、跨模块修改、验证和上下文处理。

## 2. 工作区

```text
tmp/video-task-http-cache-20260901
```

这是独立 Git 仓库，不影响 LcTiCodeAgent 正式仓库。当前已恢复到干净的失败基线。

## 3. Issue 内容

`mini_cache.CachedClient` 是一个依赖注入 Transport 的本地 HTTP 缓存。生产 issue 要求修复：

- Header 大小写不敏感查找与合并；
- `Cache-Control`、quoted `max-age`、`no-store`、`stale-if-error`；
- `Vary` 规范化、去重和多变体隔离；
- URL scheme/host、默认端口、fragment、query 和空 path 规范化；
- 请求 `no-cache` 与 `only-if-cached`；
- 响应 `Age` 和 304 条件重验证；
- variant 级 LRU 容量限制；
- 不修改调用方 headers、公共 dataclass 或测试。

原始版本运行 64 项测试时有 35 个 FAIL 和 10 个 ERROR。

## 4. 启动配置

录屏前在不被录制的终端中设置：

```powershell
$env:OPENROUTER_API_KEY = "你的密钥"
$env:LCTI_MAX_STEPS = "64"
$env:LCTI_CONTEXT_BUDGET = "16000"

.\.venv\Scripts\python.exe -m code_agent.web `
  --workspace tmp\video-task-http-cache-20260901 `
  --session-root tmp\video-recording-sessions `
  --live
```

打开 `http://127.0.0.1:8765/`。视频中不要展示设置 API Key 的终端。

16k 是本次演示预算，用于在合理任务长度内展示与默认 32k 相同的压缩机制，不改变压缩算法或阈值比例。

## 5. Turn 1：只读分析

发送：

```text
只读分析 README.md 描述的全部 Mini HTTP Cache production issues。先运行 `python -m unittest tests.test_acceptance -v` 展示代表性行为，再检查实现和所有测试，运行完整 unittest 复现失败，梳理需要修改的模块、状态不变量和实施顺序。当前阶段不要修改任何文件，也不要提交；最后给出基于证据的修复计划。
```

预期展示：

- `list_files`、`read_file` 和失败的 `run_command`；
- acceptance 测试固定显示 4 个 FAIL 和 1 个 ERROR；
- 64 项测试中的失败分类；
- Context 使用量增长；
- Git 仍为 clean；
- 最终形成 URL → Header/Policy → Store/LRU → Client 的实施计划。

彩排耗时：124.567 秒。

## 6. Session 恢复

Turn 1 完成后刷新页面，从左侧会话列表重新打开刚才的 Session。页面应恢复完整事件历史和 Context 状态。

这一步不是重新发送旧消息，而是从追加式 JSONL 确定性重建模型消息、WorkingMemory、tool-call 配对和 token 用量。

## 7. Turn 2：实施修复

发送：

```text
现在按上一轮计划实施完整修复。不要修改 mini_cache/models.py、tests 或公共 dataclasses；保持零外部依赖。先重新运行 `python -m unittest tests.test_acceptance -v` 展示行为变化，再运行完整 unittest 直到全绿，检查 Git status 和 diff，并在最终回答中只报告有工具证据支持的结果。
```

预期压缩证据：

- 确定性裁剪：`50,700 → 33,965` estimated tokens，18 条工具结果被裁剪；
- 结构化摘要：`45,558 → 13,597` estimated tokens，44 个上下文条目被摘要，`validation=passed`；
- 如果某次摘要事实校验被拒绝，原上下文会保留，后续模型请求继续运行。

预期开发结果：

- 修改 `client.py`、`headers.py`、`policy.py`、`store.py`、`url.py`；
- acceptance 测试变为 5 个 `ok`；
- 64 项测试全部通过；
- `models.py` 和全部测试保持不变；
- Git diff 仅包含 5 个实现文件。

彩排耗时：196.749 秒。彩排在测试通过后的最终模型请求遇到 HTTP 429，错误被明确记录，没有误报完成。

## 8. Turn 3：仅在中断时恢复收尾

如果 Turn 2 正常产生最终回答，不需要 Turn 3。如果出现 Provider 错误或录屏主动中断，重新打开同一 Session 后发送：

```text
继续完成刚才中断的收尾。不要再修改实现，除非验证失败；重新确认完整 unittest、Git status 和 diff，然后给出最终任务总结。不要提交。
```

彩排中的 Turn 3 再次执行了：

- 确定性裁剪：`35,174 → 24,119`；
- 结构化摘要：`24,119 → 1,626`，38 个条目被摘要；
- 64 项测试全绿；
- Git status/diff 完整；
- 最终 turn 正常 `stop`。

彩排耗时：21.094 秒。

## 9. 总体验收结果

| 项目 | 结果 |
|---|---|
| 总耗时 | 342.410 秒（5 分 42 秒） |
| 核心彩排测试 | 59 / 59 通过 |
| 新增 acceptance gold 验证 | 5 / 5 通过 |
| 最终录制测试总数 | 64 |
| 修改文件 | 5 个实现文件 |
| 测试改动 | 无 |
| 确定性压缩 | `changed=true` |
| 结构化摘要 | `validation=passed, changed=true` |
| Session restore | 通过 |
| Provider 错误可见化 | 通过 |
| 中断后继续 | 通过 |
| 最终回答 | 完整 |

## 10. 直观展示修复前后

在 Turn 1 和 Turn 2 分别展开同一条工具命令：

```powershell
python -m unittest tests.test_acceptance -v
```

修复前：

```text
language variants                         FAIL
equivalent URLs                           FAIL
no-store eviction                         FAIL
only-if-cached                            FAIL
stale-if-error                            ERROR
```

修复后：

```text
language variants                         ok
equivalent URLs                           ok
no-store eviction                         ok
only-if-cached                            ok
stale-if-error                            ok
```

这比只展示总测试数更直观：同一组用户行为从错误结果变为正确结果。随后展示 `git_diff`，形成“行为差异 → 代码差异 → 全量回归”三层证据。

## 11. 可选：展示 Git 审批

最终回答后发送：

```text
请将当前 5 个实现文件提交为一个本地 Git commit，提交信息为 fix HTTP cache semantics。
```

Web UI 出现审批卡片后点击“拒绝”，展示：

- Preflight 的文件、HEAD、Diff 与 Secret Scan；
- 一次性审批；
- 拒绝后工具不执行；
- 模型收到明确 Tool Result，而不是假设提交成功。

拒绝审批可以保持任务仓库仍处于可检查状态。

## 12. 两分钟剪辑结构

- 0–12 秒：项目定位与 issue；
- 12–35 秒：Turn 1 展示 acceptance 的 4 FAIL + 1 ERROR，并快速带过完整失败分类；
- 35–55 秒：Session 恢复与 Context 面板；
- 55–85 秒：确定性裁剪、结构化摘要和跨模块编辑；
- 85–105 秒：同一 acceptance 变为 5 个 `ok`、64 项测试通过、Git diff 和最终回答；
- 105–118 秒：Git 审批卡片并拒绝；
- 118–120 秒：总结“可恢复上下文 + 模型外部安全管线”。

剪辑可以去除等待和重复读取，但不得伪造工具结果或隐藏失败后修复的真实过程。
