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

## 文本搜索边界

`search_text`在工作区内做大小写敏感正则搜索：

- 搜索根必须解析到工作区内且是目录，指向 `.git`、`.venv`、`node_modules`、`tmp`、`sessions` 等排除目录的根被拒绝；
- 隐藏文件与隐藏目录一律跳过（包括 `.env` 等凭据文件），另排除 `node_modules`、`tmp`、`sessions`、`__pycache__` 和 `*.egg-info` 目录；
- 跳过二进制（前1024字节含 NUL）、超过 1 MB 的文件和无法按 UTF-8 解码的文件；
- 匹配结果按 path/line/snippet 返回，单条 snippet 截断到 200 字符，默认最多 50 条（上限 200），序列化结果超过 24000 字符时从尾部截断；
- 命中路径再经过一遍凭据文件过滤（`.pem`、`id_rsa` 等）；
- 优先调用本地 ripgrep（`rg`，固定参数数组、`--no-ignore`、输出上限与 15 秒超时，退出码只接受 0/1），找不到 `rg` 可执行文件时回退到纯 Python 遍历，两引擎语义一致。

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
