LcTiCodeAgent

项目仓库：https://github.com/Locat1on/LcTiCodeAgent

一、项目简介
LcTiCodeAgent 是一个从零实现的可对话编程智能体，提供命令行和本地 Web UI。项目未使用 Agent 框架或服务端托管工具；模型输出解析、Agent 循环、上下文、工具执行和会话持久化均自行实现。

二、运行方法（Windows，Python 3.12）
1. py -V:3.12 -m venv .venv
2. .\.venv\Scripts\python.exe -m pip install -e .
3. 可设置环境变量 OPENROUTER_API_KEY，或启动后在模型配置中输入密钥。
4. 启动 Web UI：.\.venv\Scripts\python.exe -m code_agent.web --live
5. 浏览器访问 http://127.0.0.1:8765/

命令行模式：.\.venv\Scripts\python.exe -m code_agent --live

三、特色功能
1. 分层上下文：按预算执行确定性裁剪和经本地事实校验的结构化摘要；关键事实检查来源，原始事件可回溯。
2. 可恢复会话：交互写入追加式 JSONL，重启后可恢复上下文、工作记忆和工具证据。
3. 安全管线：文件限定在工作区；命令使用参数数组与白名单；编辑绑定读取时 SHA-256；网络和 Git 写操作需要一次性批准。
4. Web UI：流式展示回复、推理摘要、工具、测试、审批、上下文和 Git Preflight；支持会话管理。
5. 多厂商接口：支持 OpenRouter、Google Gemini、DeepSeek、OpenAI 和自定义兼容 API；Web 输入的密钥仅存于后端内存，不写入浏览器存储或会话日志。

四、说明
默认使用 OpenRouter 上的 Google Gemini 3.7 Flash。Workspace Policy Sandbox 属于应用层隔离，不等同于容器或操作系统级沙箱。
