# Agent Runtime 设计

## 目标

`AgentRuntime` 协调一次已经创建的 Run。它恢复 Thread 状态，记录运行事件，调用可替换的 Agent，并在成功或失败时保存结果与更新 Run 状态。

它保留 DeerFlow Worker 的核心职责：运行上下文、状态恢复、生命周期事件、流式通知和失败收尾；不复刻 LangGraph、扩展系统、追踪和多租户生产配置。

参考实现：`/home/pl/sp/deer-flow/backend/packages/harness/deerflow/runtime/runs/worker.py` 的 `run_agent()`。

## 边界

### AgentRuntime

- 输入：`user_id`、`thread_id`、`run_id`、用户消息和一个 Agent。
- 读取最新 Checkpoint；没有 Checkpoint 时从 Thread 的工作目录创建空 `ThreadState`。
- 将用户消息加入状态并保存 Checkpoint。
- 将 Run 标记为 `running`，并记录 `run.start`。
- 调用 Agent，保存 Agent 返回的 `ThreadState`。
- 成功时记录 `run.end` 并将 Run 标记为 `success`。
- 捕获异常时记录 `run.error` 并将 Run 标记为 `error`，然后重新抛出异常给 API 层。

### Agent

Agent 不依赖 DeepSeek、LangGraph 或 SQLite。它接收当前 `ThreadState` 和 `RuntimeContext`，完成推理后返回新的 `ThreadState`。

未来的真实 Agent Loop 会在 Agent 内部实现：LLM 直接回答，或发起工具调用；工具结果作为消息回到 LLM，再继续推理。

### RuntimeContext

向 Agent 提供本次运行的受控能力：

- 身份：`user_id`、`thread_id`、`run_id`、`workspace_path`。
- `record_event`：持久化并实时广播 `RunEvent`。
- `save_checkpoint`：在 LLM 输出、工具结果等关键步骤保存完整 `ThreadState`。

Agent 不直接访问 Repository，避免推理逻辑、数据库和流传输耦合。

## 数据流

```text
用户消息
→ AgentRuntime 恢复状态
→ 保存用户消息 Checkpoint
→ Run running + run.start
→ Agent.run(state, context)
→ 保存最终/关键状态
→ run.end + Run success
```

发生异常时：

```text
Agent 异常
→ run.error
→ Run error
→ API 层接收异常
```

## 明确不在本模块中实现的内容

- DeepSeek 或其他模型的实际请求。
- Tool Registry、Tool Executor 与工具调用循环。
- FastAPI 路由、SSE 响应格式和浏览器界面。
- LangGraph、LangChain、认证、监控追踪。

这些模块将通过 Agent 接口和 RuntimeContext 接入，不改变本模块的职责。
