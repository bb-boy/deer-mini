# Agent Loop API 接入设计

## 目标

让调用方通过 HTTP 创建 Thread 和 Run。创建 Run 后，接口立即返回 `run_id`，后台真正执行：

```text
用户消息
→ AgentRuntime
→ LeadAgent
→ 模型决定回答或调用工具
→ ToolExecutor 执行工具
→ 工具结果回到模型
→ Checkpoint / RunEvent 写入 SQLite
→ StreamBridge 通过 SSE 推送事件
```

本次只增加单进程、单机版 HTTP 外壳，不引入 Redis、Celery、认证系统或多进程事件同步。

## HTTP 接口

### POST `/api/threads`

请求字段：

- `user_id`：当前任务属于哪个用户；第一版由请求显式传入。
- `title`：可选的对话标题。

成功返回 `201 Created`，内容包含 Thread 的 `id`、`user_id`、`title`、`status` 和 `workspace_path`。

副作用：写入 `threads` 表，并创建该 Thread 独立的 `workspace`、`uploads` 和 `outputs` 目录。

### POST `/api/threads/{thread_id}/runs`

请求字段：

- `user_id`：用于验证 Thread 所有权。
- `message`：本次交给 Agent 的用户消息。
- `model_name`：模型配置名，默认 `ustc-deepseek-flash`。
- `thinking_enabled`：是否启用模型思考模式，默认 `false`。
- `reasoning_effort`：可选的推理强度。

成功返回 `202 Accepted`，内容是新建的 pending Run。HTTP 请求只完成本地组装和任务注册，不等待远程模型回答。

副作用：写入 `runs` 表，并把真正的 Agent 执行注册为当前 FastAPI 进程中的后台 `asyncio.Task`。

### GET `/api/threads/{thread_id}/runs/{run_id}`

查询参数：

- `user_id`：用于验证 Thread 和 Run 所有权。

成功返回 Run 当前状态、模型配置、错误信息和各时间字段。调用只读取 SQLite。

### GET `/api/threads/{thread_id}/runs/{run_id}/events`

查询参数：

- `user_id`：用于验证 Thread 和 Run 所有权。

请求头：

- `Last-Event-ID`：可选；同一服务进程内重连时，从下一条缓存事件继续。

成功返回 `text/event-stream`。每条消息使用标准 SSE 的 `id`、`event` 和 JSON `data` 字段；心跳使用 SSE 注释，避免代理把空闲连接关闭。

副作用：保持 HTTP 连接并订阅共享 `MemoryStreamBridge`，不写数据库。

## 组件设计

### FastAPI 应用

新增 `app/main.py` 创建 FastAPI 实例，在启动时调用 `initialize_database()`。共享对象放在 `app.state`，避免每个请求各自创建 StreamBridge，导致发布者和订阅者看不到彼此。

### RunCoordinator

新增应用层协调器，职责仅限于把已有模块接起来，并对“创建 Run 后一定有后台任务接手”负责。

输入：`user_id`、`thread_id`、`message` 和模型配置。

输出：新建的 Run；创建后台任务后立即返回，不等待最终模型回答。

副作用：

- 先用 `ModelFactory` 验证配置并创建本次 Run 独立的模型客户端；
- 创建 `ToolRegistry`、`ToolExecutor` 和 `LeadAgent`；
- 注册真实 `ReadFileTool`；
- 通过 `RunService` 创建 pending Run；
- 调用共享 `AgentRuntime.run()`；
- 用集合保存后台 Task 的强引用，完成后自动移除；
- 读取已完成 Task 的异常，避免 `Task exception was never retrieved`。真正的 Run 错误状态和 `run.error` 事件仍由 AgentRuntime 负责。

`LeadAgent` 是每个 Run 独立创建的，因此它在结束时关闭自己的模型客户端不会影响其他 Run。

如果模型配置或环境变量无效，协调器会在写入 Run 前失败，因此不会留下永远无法启动的 pending Run。如果模型客户端已经创建、但随后本地组装失败，协调器负责关闭该客户端。

### 请求与响应模型

新增 Pydantic 模型负责 JSON 校验和序列化。HTTP 层不直接暴露 SQLite Row，也不把数据库 Repository 传入 Agent。

### SSE 编码

路由把 `StreamEvent` 转换为：

```text
id: 3
event: run_event
data: {"event_type":"tool.start", ...}

```

事件内容中的中文使用 UTF-8 JSON，不转成 `\uXXXX`。响应设置禁止缓存，并关闭反向代理缓冲提示头。

## 错误处理

- Thread、Run 不存在或不属于 `user_id`：返回 `404`，不泄露其他用户资源是否存在。
- 请求字段无效：由 FastAPI 返回 `422`。
- 不支持的 `model_name` 或模型环境变量缺失：创建 Run 前验证并返回 `400`，避免留下无法启动的 pending Run。
- Agent、模型或工具运行中失败：HTTP 创建接口已经返回；AgentRuntime 写入 `run.error`，把 Run 置为 `error`，并结束 SSE。
- 后台任务被取消：由协调器确保任务异常被读取；第一版不增加用户主动取消接口。

## 并发与生命周期限制

- 多个 Run 可以在同一进程中并发执行，每个 Run 使用独立模型客户端和状态。
- `MemoryStreamBridge` 和后台任务集合只属于当前 Uvicorn 进程，因此第一版必须使用单 worker 启动。
- 服务重启后，SQLite 中的 RunEvent 和 Checkpoint 仍存在，但内存中的 SSE 缓存和正在执行的任务不会恢复。
- 生产级任务恢复、跨进程 Stream 和认证不在本次范围内。

## 测试设计

先写失败测试，再实现接口：

1. 创建 Thread 返回 `201`，SQLite 和工作目录都存在。
2. 创建 Run 返回 `202`，不会等待 Agent 完成。
3. 使用可控工具调用模型和真实 `ReadFileTool`，验证 Run 最终成功，消息经过 `assistant(tool_call) → tool → assistant`。
4. SSE 包含 metadata、文字、工具开始、工具结束和结束事件，并能自然结束订阅。
5. 模型异常时 Run 变成 `error`，SSE 收到错误事件后结束。
6. 错误 `user_id` 无法读取 Run 或订阅事件。
7. 真实 USTC 模型测试继续由 `RUN_LIVE_AGENT_TEST=1` 显式开启，不进入默认快速测试集。

## 依赖和启动方式

把已经安装的直接依赖写入 `requirements.txt`：

- `fastapi==0.141.1`
- `uvicorn[standard]==0.52.4`
- `httpx==0.28.1`（API 测试客户端）

开发启动命令：

```bash
cd /home/pl/deer_mini/backend
PYTHONPATH=. .venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

第一版不使用 `--workers`，否则不同进程的 MemoryStreamBridge 无法共享事件。

## 本次不实现

- 登录、Token 和多用户认证；
- 文件上传接口；已有文件仍由当前 Thread workspace 提供；
- Run 取消、重试和服务重启恢复；
- Redis、消息队列和多个 Uvicorn worker；
- 前端页面。
