# 文件 Checkpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 按已确认设计提供每轮开始前的消息和文件恢复点，支持预览、整轮恢复、失败恢复及容量清理。

**Architecture:** Thread 当前指针与 execution 历史分离；独立文件对象库保存去重内容；持久化恢复操作协调目录替换与 SQLite 提交。Run、上传、下载、删除和恢复共用 Thread 操作准入，恢复先独占再取消 Run。

**Tech Stack:** Python 3.12、SQLite、FastAPI、React 19、TypeScript、pytest、Vitest。

---

远端基线 `d673fe0`，工作树 `tx:/home/pl/.config/superpowers/worktrees/deer_mini/checkpoint`。本地 `work/checkpoint` 是只同步源码的编辑副本，运行验证在远端隔离工作树中完成。原有未提交设计文档保留。实现不启动服务，不触碰线上数据库和用户文件，不推送或部署。

## 1. 内容对象与文件清单

Files: create `backend/app/filesystem/file_snapshots.py`, `backend/tests/app/filesystem/test_file_snapshots.py`.

- [x] 先写真实临时目录测试，覆盖二进制、空目录、权限、去重、删除、链接、损坏与容量。
- [x] 执行测试观察缺失能力失败，再实现下述接口。

```python
# 清单必须排序、验证三个区域及父目录，禁止链接与保留区域。
class FileSnapshotStore:
    def __init__(self, paths, *, max_entries=50_000, max_manifest_bytes=16 * 1024**2,
                 max_bytes=1024**3): ...
    def capture(self, *, store_objects=True) -> dict: ...
    def validate(self, manifest: dict, *, verify_objects=True) -> None: ...
    def materialize(self, manifest: dict, destination) -> None: ...
    def collect(self, manifests: list[dict]) -> None: ...

# schema_version=1，entries 包含三个根目录。
manifest = {"schema_version": 1, "entries": [
    {"path": "uploads", "kind": "directory", "mode": 448},
    {"path": "workspace/a.bin", "kind": "file", "mode": 384,
     "sha256": "<64 hex>", "size": 3},
]}
```

- [x] 流式读取 + O_NOFOLLOW、读取前后 fstat 和路径身份校验；fsync 对象与父目录；对象不与用户文件硬链接；只收集本库无引用对象。
- [x] 运行 `cd backend && .venv/bin/python -m pytest tests/app/filesystem/test_file_snapshots.py -q`，预期全部通过。

## 2. 持久化模型与当前指针

Files: modify `backend/app/domain/checkpoints.py`, `backend/app/infrastructure/database.py`, `backend/app/repositories/checkpoint_repository.py`; create `backend/app/repositories/file_checkpoint_repository.py`, `backend/tests/app/repositories/test_file_checkpoint_repository.py`.

- [x] 测试先覆盖旧数据库迁移、原子保存指针、指定 Run 只读 execution、跨用户查询、恢复原子提交与去重容量保留。
- [x] checkpoints 增加 kind（execution/turn_start/restore）；迁移初始化 `thread_heads(thread_id, checkpoint_id, revision)`，latest 按指针读取。
- [x] 新增 `file_snapshots`、`file_restore_points`、`file_restore_operations`；保存恢复点同时保存状态 checkpoint 和清单，step 在全部类型中唯一。
- [x] 后台记忆保存 checkpoint 使用 `advance_head=False`，避免旧任务推进指针。

```python
# 核心验收：恢复不改变旧 Run 的执行快照。
assert repo.latest(thread.id, user_id).kind == "restore"
assert repo.latest_for_run(thread.id, run.id, user_id).kind == "execution"
assert restored.state.messages == target.state.messages
```

- [x] 所有写入校验 Thread/Run/state 归属；当前指针与 operation committed 同事务落盘。
- [x] 执行两个 checkpoint repository 测试文件，预期全部通过。

## 3. 持久化恢复与保留策略

Files: create `backend/app/services/file_checkpoint_service.py`, `backend/tests/app/services/test_file_checkpoint_service.py`.

- [x] 用临时数据库与文件编写恢复、幂等、预览过期、容量和故障注入测试。
- [x] `capture_turn(thread, run, state, message)` 在消息追加前建立 turn_start；默认保留 100 点、1 GiB，读取四个 `DEER_MINI_CHECKPOINT_MAX_*` 配置，错误值拒绝启动。
- [x] preview 返回下述结构，restore 同时验证 revision 和文件指纹。

```json
{"restore_point_id":"id","revision":1,"fingerprint":"sha256",
 "created":[],"modified":[],"deleted":[],"removed_messages":2,
 "target_messages":0,"current_messages":2}
```

- [x] 恢复前建立 recovery，固定目标、当前 recovery 与尚未清理的 operation 引用；prepared/applying 持久化先于目录改动。
- [x] staging 包含 new 和 old，三个目录分别 rename 并 fsync；保留 `.tool-results` 历史内容；commit 后才报告成功。
- [x] 未 committed 崩溃从 recovery 快照重建当前文件，committed 操作验证目标并清理；失败置 needs_recovery 并拒绝该 Thread 新写入。
- [x] 清理只删除备份记录与无引用对象，历史消息 checkpoint 永久保留；空间不够时保存旧点并明确失败。
- [x] 执行 service 故障注入测试，每个目录交换阶段与 SQLite 提交失败均能恢复。

## 4. 执行协调、容器与历史结果保护

Files: create `backend/app/services/thread_operations.py`; modify `backend/app/services/run_coordinator.py`, `backend/app/runtime/agent_runtime.py`, `backend/app/sandbox/manager.py`, `backend/app/sandbox/docker_runner.py`, `backend/app/filesystem/thread_paths.py`, `backend/app/main.py`; add tests in runtime/services/sandbox.

- [x] 写测试证明开始 Run、上传、恢复、删除竞争时只有一个写入者；恢复等取消时不持有 Run 收尾需要的锁。
- [x] Run 预约从创建前到真正执行结束；上传在活动 Run 期间冲突；恢复登记独占后取消任务并停止容器。
- [x] `AgentRuntime` 在追加用户消息前调用 capture；备份错误不调用模型，仍正确记录 Run 失败。
- [x] 容器每轮开始前与结束后严格停止，失败保留登记阻止后续写入；恢复启动检查先于对外 API。
- [x] `.tool-results` 增加 Docker 嵌套只读挂载，模型普通写路径禁止保留目录和祖先替换。
- [x] 执行现有 runtime、services、sandbox 测试和新增竞争测试。

## 5. HTTP 接口

Files: modify `backend/app/api/routes.py`, `backend/app/api/schemas.py`; create `backend/tests/app/api/test_file_checkpoints.py`.

- [x] 先测试所有新路由跨用户返回 404、预览过期 409、恢复状态查询与重复操作。
- [x] 新增 `GET /threads/{id}/restore-points`, `POST /threads/{id}/restore-points/{point}/preview`, `POST /threads/{id}/restore`, `GET /threads/{id}/restore-operations/{operation}`，均带 user_id。

```json
{"operation_id":"uuid","restore_point_id":"id","revision":1,"fingerprint":"sha256"}
```

恢复点字段为 id、run_id、kind、created_at、message、available、unavailable_reason；操作返回 operation_id、thread_id、restore_point_id、recovery_point_id、status、error、created_at、updated_at。只在 committed 时显示完成。

- [x] HTTP 断连不取消持久化收尾；文件列表/下载在恢复期间返回 409；下载准入保持到响应传输结束。
- [x] 保持既有成功响应结构，执行 API 回归。

## 6. 前端恢复入口与缓存清理

Files: modify `frontend/src/api/client.ts`, `frontend/src/api/types.ts`, `frontend/src/hooks/useAgentRun.ts`, `frontend/src/App.tsx`; create `frontend/src/components/RestorePoints.tsx` and tests.

- [x] 测试范围说明、首轮入口、预览、409 刷新、操作查询及恢复失败提示。
- [x] 对话工具栏提供恢复入口，按点列出用户消息和时间；旧 Run 没文件备份显示原因。
- [x] 确认文案明确三个目录、后续上传、环境与长期记忆；进行中可停止执行后重新预览恢复。
- [x] 成功后断开旧 SSE、清空缓存、刷新消息/任务/文件，关闭旧文件预览；Thread 切换时丢弃旧异步响应。
- [x] `pnpm typecheck && pnpm test && pnpm build` 通过。

## 7. 最终审查与交付

- [x] 独立审查设计覆盖率、数据安全、取消边界、失败重启和 UI 缓存失效；修复发现的问题后复测。
- [x] 后端 `.venv/bin/python -m pytest tests/app --ignore=tests/app/model/test_factory.py`，排除真实模型和 learning 测试。
- [x] 检查远端 `git diff`、`git diff --check`、未跟踪文件与文档同步，记录真实测试结果。
- [x] 更新配置与使用文档，交付可审查改动；提交/推送/部署按用户授权执行。


## 实施记录（2026-10-05）

- 已实现文件对象库、事务状态指针、恢复日志、容量策略、Run/文件访问协调、Bash 停止证明和前端恢复入口。
- 文件对象库与前后端分别完成独立审查；已修复只读目录移动/清理、prepared 前临时文件回收、终态清理重试、启动失败错误清除容器标记等问题。
- 浏览器网络结果不明时保留 operation_id；409、507、普通 HTTP 随机编号与浏览器存储失败均有回归。
- 后台长期记忆原本使用禁止保存 Thread checkpoint 的上下文，无需额外改写记忆模块。保留 `advance_head=False` 接口用于明确的历史写入。
- 普通模型文件工具当前仅有读取入口，唯一文件写入能力 Bash 已通过嵌套只读挂载保护 `.tool-results`。
- 用户请求的实现保留在 checkpoint 工作树，尚未提交、推送或部署。验证结果和配置说明见 `backend/docs/file-checkpoints.md`。
