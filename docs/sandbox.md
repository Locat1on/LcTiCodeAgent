# Workspace Policy Sandbox

## 定位

本项目采用轻量策略级Sandbox，不要求Docker、WSL、虚拟机或管理员初始化。目标是降低模型误操作风险并提供清晰、可审计的执行边界，而不是抵御恶意代码突破操作系统隔离。

## 文件边界

- 所有路径先解析为真实绝对路径；
- 解析结果必须位于工作区；
- 拒绝读取或写入 `.env`、私钥、SSH和云凭据目录；
- `replace_in_file`要求旧文本唯一匹配；
- `write_file`只能创建新文件，不能覆盖；
- 文件写入使用同目录临时文件和原子替换；
- 修改结果记录前后SHA-256。

## 命令边界

- 使用 `subprocess` 参数数组和 `shell=False`；
- 只允许测试、编译、lint、typecheck和build命令；
- 拒绝任意Python代码、安装、网络和发布命令；
- Python命令固定到项目 `.venv` 解释器；
- 子进程只获得必要环境变量，API key不会传入；
- 设置1至120秒超时；
- stdout和stderr分别限制为16000字符；
- 工具结果记录退出码、超时状态、耗时和实际argv。

## 已知限制

验证命令仍在当前Windows用户身份下执行。命令白名单可以阻止模型直接请求危险命令，但测试代码本身仍可能读取宿主机文件或访问网络。因此，该功能应称为Workspace Policy Sandbox，而不是OS-enforced Sandbox。

如需运行不可信第三方仓库，应另行使用容器、虚拟机或成熟的系统级Sandbox。
