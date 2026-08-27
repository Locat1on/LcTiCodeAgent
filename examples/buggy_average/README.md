# Buggy Average

这是 LcTiCodeAgent 的端到端修复任务。`average()` 对普通列表工作正常，但传入空列表时会触发除零异常。

验收要求：

- 空列表返回 `0.0`；
- 不修改测试；
- `python -m unittest discover -s tests -v` 全部通过。

