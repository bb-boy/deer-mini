# deer_mini 前端设计

## 用户可见功能

用户打开页面后能看到自己的 Thread 列表；选择对话会从最新 Checkpoint 恢复完整消息，并显示历史 Run 和 Workspace 文件。发送消息会创建真实 Run，通过 SSE 逐字显示模型输出和工具步骤；运行中可以停止。页面刷新后会查询最近 Run：活跃 Run 重新连接 SSE，终态 Run 直接恢复持久化状态。

## 技术边界

- 使用 Vite、React、TypeScript，前端位于独立 `frontend/` 目录。
- 开发环境将 `/api` 代理到 `http://127.0.0.1:8000`，避免额外 CORS 配置。
- 不复制 DeerFlow 的 Next.js、LangGraph SDK、认证和复杂状态库。
- 不在前端保存 API Key；模型 Key 只存在后端 `.env`。
- 当前 Mini 是单用户演示形态，`user_id` 在页面中可编辑并保存到 localStorage。

## 模块

### API Client

- 功能：调用 Thread、Run、Checkpoint 和文件 REST 接口。
- 输入：`user_id`、`thread_id`、`run_id`、消息、模型选项或浏览器 File。
- 输出：TypeScript 类型化对象。
- 副作用：发起 HTTP 请求；上传会修改 Thread Workspace，创建/取消 Run 会修改后端状态。

### Run Stream

- 功能：订阅一次 Run 的持久化/实时事件，并在终态事件到达后主动关闭 EventSource。
- 输入：Thread、Run、用户 ID 和事件回调。
- 输出：顺序 RunEvent；自动忽略重复事件 ID。
- 副作用：保持一个浏览器 SSE 连接；网络中断由 EventSource 使用 Last-Event-ID 自动重连。

### useAgentRun

- 功能：协调发送、流式文字、工具时间线、取消和刷新恢复。
- 输入：当前 Thread、用户消息、模型配置。
- 输出：当前 Run、草稿文本、工具事件、错误和 busy 状态。
- 副作用：调用 Run API 和 SSE；终态时要求页面重新读取 Checkpoint、Run 和文件。

### 页面组件

- `ThreadSidebar`：用户 ID、创建对话、选择历史对话。
- `MessageList`：展示用户、模型、工具消息和实时回答。
- `ChatComposer`：消息、模型、思考开关、发送/停止。
- `RunTimeline`：展示工具开始/结束与错误状态。
- `WorkspaceFiles`：上传、列表和下载文件。

组件只接收数据和回调，不直接调用后端。

## 错误与恢复

- REST 非 2xx 统一解析 FastAPI `detail` 并显示在页面顶部。
- SSE 断线显示“正在重连”；原生 EventSource 自动重连。
- 收到 `run.end/error/timeout/interrupted` 后关闭 SSE，并以 SQLite 最新 Checkpoint 覆盖临时流状态。
- 切换 Thread 或卸载组件时关闭旧 EventSource，防止串流到错误对话。
- Thread 尚无 Checkpoint 时按空消息处理，不把 404 当错误。

## 页面布局

桌面端为三栏：左侧 Thread，中央消息和输入框，右侧 Run/文件；窄屏改为单列，右侧信息移到消息下方。视觉使用深蓝灰背景、青绿色状态强调和温暖的消息卡片，不引入 UI 组件库。

## 验证

- Vitest 验证 API URL/请求体、SSE 去重和终态关闭、核心组件渲染。
- TypeScript 类型检查和 Vite production build 必须通过。
- 使用隔离后端数据库和浏览器完成创建 Thread、历史恢复、文件上传/下载的页面冒烟检查；不调用真实模型即可验证基础 UI，真实 Run 再单独验证。
