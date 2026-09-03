# Memory Stream 延迟清理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run 结束后保留短暂 SSE 重连窗口，随后自动释放该 Run 的内存事件和清理任务。

**Architecture:** `MemoryStreamBridge.publish_end()` 负责标记结束并幂等调度延迟清理；清理任务捕获当时的 `_RunStream` 身份，避免误删同 ID 的新对象；应用 shutdown 在 Coordinator 收尾后调用 `bridge.close()` 统一取消并等待清理任务。SQLite 事件继续承担长期回放。

**Tech Stack:** Python 3.12、asyncio、pytest。

---

### Task 1: 写失败测试

**Files:**
- Modify: `backend/tests/app/runtime/test_stream_bridge.py`

- [x] 移除测试对真实 Thread、Run 和 SQLite 的依赖，只使用字符串 run_id。
- [x] 验证 publish/subscribe/end 的原行为。
- [x] 验证保留时间内 stream 存在，到期后消失。
- [x] 验证已连接订阅者在字典清理后仍能收到尾部事件并结束。
- [x] 验证重复 `publish_end()` 只创建一个清理任务。
- [x] 验证 `close()` 清空 stream 并回收所有任务。
- [x] 运行并确认因新接口尚不存在而失败。

### Task 2: 实现清理生命周期

**Files:**
- Modify: `backend/app/runtime/stream_bridge.py`

- [x] 构造函数接受 `retention_seconds`，默认读取 `DEER_MINI_STREAM_RETENTION_SECONDS=60`。
- [x] 增加 `stream_exists()`、`cleanup()` 与 `close()`。
- [x] `publish_end()` 幂等创建一个命名 asyncio Task。
- [x] 清理时只删除最初捕获的 stream 对象，避免延迟任务误删替换对象。
- [x] done callback 读取任务异常，避免 asyncio 的未处理异常警告。

### Task 3: 接入应用关闭

**Files:**
- Modify: `backend/app/main.py`
- Modify: `backend/.env.example`

- [x] 在 `coordinator.shutdown()` 后调用 `stream_bridge.close()`。
- [x] 增加 60 秒保留窗口配置说明。

### Task 4: 验证

**Files:**
- Verify only

- [x] Stream 测试从失败变为通过。
- [x] 隔离运行 Runtime、API、原子事务和文件回归测试。
- [x] 执行 `compileall`、`git diff --check`。
- [x] 只读确认真实数据库 active Run 仍为 93；不启动正式 API，不提交 Git。
