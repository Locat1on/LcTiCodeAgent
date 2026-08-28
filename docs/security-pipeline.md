# Tool Security Pipeline

每个模型Tool Call必须经过同一条执行管线：

```text
Tool Call
  -> Capability Gate
  -> ALLOW / ASK / DENY
  -> optional user approval
  -> local execution
  -> Evidence Ledger
  -> Tool Result
```

## 决策

- `ALLOW`：工作区读取、工作区文本搜索、受限原子编辑、本地验证命令和Git只读检查；
- `ASK`：HTTPS访问以及后续的Git提交、推送等跨边界操作；
- `DENY`：未知工具和不在能力策略中的操作。

`ASK`只支持批准本次调用。输入中断、无交互终端或用户拒绝时均按拒绝处理，工具主体不会执行。

## 受控网络

`fetch_url`只允许HTTPS GET/HEAD，拒绝URL凭据、非公开IP、非文本响应和超限响应。每次调用都需要用户批准，重定向目标会重新检查。该实现不携带浏览器Cookie或宿主机API key。

当前实现会在请求前和重定向后检查DNS结果，但没有把连接固定到已验证IP，因此不把它视为能够抵御恶意DNS rebinding的强SSRF边界。

## Git读取

`git_status`、`git_diff`和`git_log`使用固定参数数组，不经过Shell，也不触发Git网络和写操作。路径参数仍受工作区边界检查。

## Git写入和推送

`git_commit`只提交明确列出的工作区文件。Preflight包含HEAD、文件哈希、状态、Diff摘要、无关已暂存文件和高置信度凭据扫描。审批后重新计算状态令牌；任何变化都会使原审批失效。

`git_push`只允许当前HEAD推送到已有远程的同名分支，命令固定为 `git push --porcelain <remote> HEAD:refs/heads/<branch>`。工具Schema不提供force、mirror、delete、tag或任意refspec入口。Preflight显示远程URL、分支、HEAD、远程HEAD、待推送提交和涉及文件，并对HEAD内容执行凭据扫描。

## 审计

会话日志分别记录：

- `tool.requested`
- `tool.approval_required`
- `tool.approval_decided`
- `tool.started`
- `tool.completed`或`tool.failed`

审批请求ID将请求与最终决定关联。拒绝路径同样产生Tool Result，使模型能够调整方案，而不是假设动作成功。
