# deer_mini Vite React 前端 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建能接入 Thread、Run、Checkpoint、SSE、取消和 Workspace 文件 API 的聊天页面。

**Architecture:** 类型化 API Client 隔离 HTTP，Run Stream 隔离 EventSource，`useAgentRun` 负责一次运行状态机，App 负责 Thread 选择和持久化数据刷新，展示组件保持无网络副作用。Vite 开发代理同源转发 `/api`。

**Tech Stack:** Vite 8、React 19、TypeScript 5.9、Vitest 4、Testing Library。

---

### Task 1: 工程配置和失败测试

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/tsconfig*.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/index.html`
- Create: `frontend/src/test/setup.ts`
- Create: `frontend/src/api/client.test.ts`
- Create: `frontend/src/api/run-stream.test.ts`
- Create: `frontend/src/components/MessageList.test.tsx`
- Create: `frontend/src/components/WorkspaceFiles.test.tsx`

- [x] 安装 React/Vite/TypeScript/Vitest 依赖并生成 pnpm lockfile。
- [x] 编写 API 请求、SSE 生命周期和组件测试。
- [x] 运行 Vitest，确认因实现模块缺失而失败。

### Task 2: 类型化 API 和 SSE

**Files:**
- Create: `frontend/src/api/types.ts`
- Create: `frontend/src/api/client.ts`
- Create: `frontend/src/api/run-stream.ts`

- [x] 定义 Thread、Run、Message、Checkpoint、WorkspaceFile 和 RunEvent 类型。
- [x] 实现 REST 请求、FastAPI detail 错误解析、404 空 Checkpoint 语义。
- [x] 实现 EventSource URL、事件解析、ID 去重、终态主动关闭和断线回调。
- [x] 运行 API/SSE 测试变绿。

### Task 3: 展示组件

**Files:**
- Create: `frontend/src/components/ThreadSidebar.tsx`
- Create: `frontend/src/components/MessageList.tsx`
- Create: `frontend/src/components/ChatComposer.tsx`
- Create: `frontend/src/components/RunTimeline.tsx`
- Create: `frontend/src/components/WorkspaceFiles.tsx`

- [x] 实现无网络副作用的 Thread、消息、输入、工具步骤和文件组件。
- [x] 上传 input 支持选择后立即回调并清空自身。
- [x] 运行组件测试变绿。

### Task 4: Run Hook 和页面集成

**Files:**
- Create: `frontend/src/hooks/useAgentRun.ts`
- Create: `frontend/src/App.tsx`
- Create: `frontend/src/main.tsx`
- Create: `frontend/src/styles.css`
- Create: `frontend/src/vite-env.d.ts`

- [x] 加载/切换 Thread 时读取状态、Run 和文件。
- [x] 发送消息时按需创建 Thread，再创建 Run 并连接 SSE。
- [x] 累积 `text.delta`，展示工具事件，终态后刷新持久化状态。
- [x] 恢复最近 pending/running Run；取消后刷新状态。
- [x] 实现三栏响应式页面和可访问表单标签。

### Task 5: 验证

**Files:**
- Verify only

- [x] `pnpm test`、`pnpm typecheck`、`pnpm build` 通过。
- [x] 隔离启动后端与 Vite，浏览器检查 Thread 创建、历史空态、文件上传和下载。
- [x] 运行后端关键回归、`git diff --check`，确认真实 active Run 仍为 93。
- [x] 不提交 Git。
