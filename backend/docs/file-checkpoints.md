# 文件恢复点与整轮恢复

每轮在加入用户消息、调用模型或工具之前，保存当时的完整 ThreadState 和三个文件区域的清单。文件内容使用 SHA-256 寻址，多个恢复点共用不变的内容对象。执行中的消息 checkpoint 继续保存，但不会每次扫描文件。

## 使用

在对话工具栏打开“恢复点”，选择“本轮开始前”或某次“恢复前备份”。如仍在运行，界面先停止运行，再重新取得预览。预览列出文件新增、覆盖、删除，以及撤销的消息数量；确认后恢复消息、任务和文件。

覆盖 `uploads`、`workspace`、`outputs` 的普通文件和目录，包括二进制、空目录和普通权限位。之后上传的附件也可能删除。环境、外部服务和用户级长期记忆不回滚；历史 Run 与事件保留。旧轮次没有文件备份，或备份已按容量规则清理时，会显示不可恢复原因。

Bash 每轮结束后停止容器。`workspace/.tool-results` 是保留的历史资料，不计入用户文件清单；恢复时保留内容，Bash 通过嵌套只读挂载读取它。三个目录以外的容器安装与进程状态不恢复。

## 配置

以下环境变量必须为正整数，非法值拒绝启动：

| 变量 | 默认值 | 含义 |
| --- | ---: | --- |
| `DEER_MINI_CHECKPOINT_MAX_POINTS` | 100 | 每个 Thread 的可选恢复点数，含恢复前备份 |
| `DEER_MINI_CHECKPOINT_MAX_BYTES` | 1073741824 | 保留清单与去重内容对象的合计字节数 |
| `DEER_MINI_CHECKPOINT_MAX_ENTRIES` | 50000 | 单份清单的文件与目录条目数，含三个根目录 |
| `DEER_MINI_CHECKPOINT_MAX_MANIFEST_BYTES` | 16777216 | 单份清单编码后的最大字节数 |

容量策略先计算可淘汰的旧点，再在新恢复点持久化事务中移除旧引用，最后回收无引用对象。目标点、未清理操作引用和最近一次恢复前备份受保护。若无法容纳新点则明确失败，保留原有效恢复点；工作文件和历史消息 checkpoint 不因清理而删除。

备份对象、临时对象、恢复 staging、旧目录和工具历史复制会临时需要更多磁盘空间；归档上限不是磁盘配额。每个文件写入与恢复构建前检查可用空间，实际磁盘写入失败仍按失败处理。

## 持久化和故障处理

数据库保存 `thread_heads` 当前指针以及 revision，独立于历史 checkpoint 时间顺序。恢复会新增 `restore` checkpoint 并切换指针；下一轮从此状态继续，旧 Run 的 SSE 仍只读取该 Run 的 `execution` 快照。

预览同时绑定 revision 和文件内容/权限指纹。预览后状态变化会返回 409，必须重新预览并确认。恢复请求使用 operation_id 幂等；网络中断时前端持续查询同一个编号，不能直接创建另一次恢复。

恢复先保存 recovery 点，构建目标 staging，再保存 prepared/applying 操作日志。三个根目录分别交换并 fsync；最后在一个 SQLite 事务内保存消息恢复 checkpoint、推进指针并标记 committed。只有 committed 且 cleaned 才由界面宣告完成。

服务重启先停止遗留容器，再处理恢复日志，最后开放 API：

- prepared/applying：从 recovery 快照重建恢复前文件。
- committed：验证目标文件并完成清理。
- rolled_back：验证恢复前文件并完成清理，不重复应用已删除的 staging。
- 无操作引用的 staging：回收 prepared 登记前崩溃留下的私有临时文件。
- 无法确认：保留记录并禁止对应 Thread 的执行、上传、删除、再次恢复和普通文件访问；修复磁盘、权限或对象问题后重启重试，不把中断报告为成功。

`runtime_flags.sandbox_state` 在启用 Bash 会话前标记 active，只有确认启动时的遗留容器清理和退出时的容器停止均成功，才写 clear。缺少 Docker CLI 且有 active 标记或旧 Bash 历史时，启动拒绝继续；从未启用 Bash 的部署不依赖 Docker。不要手工改这个标记绕过停止验证。

所有新接口使用既有 Thread 归属校验。应用仍是单进程设计；服务外手工并发写入不受应用锁控制，检测到扫描变化会失败。

## API

接口使用 `user_id` 归属参数，与当前 API 一致；不代表新增登录认证。

- `GET /api/threads/{thread_id}/restore-points`
- `POST /api/threads/{thread_id}/restore-points/{point_id}/preview`
- `POST /api/threads/{thread_id}/restore`，body 为 `operation_id`、`restore_point_id`、`revision`、`fingerprint`
- `GET /api/threads/{thread_id}/restore-operations/{operation_id}`

恢复与准备预览期间阻止其他写入；执行期间上传返回冲突。下载持有读访问直到传输完成，避免传输中替换目录。HTTP 请求取消会等待已经开始的持久化工作结束；进程崩溃由上述日志恢复。

## 验证与部署边界

回归只使用临时数据库、临时文件、mock 模型和容器。包含二进制/权限/链接/去重/容量测试、目录交换各阶段和提交后的崩溃、取消请求、重启重复清理、操作归属与预览冲突，以及真实降权进程中的只读目录恢复。

```bash
cd backend
.venv/bin/python -m pytest tests/app --ignore=tests/app/model/test_factory.py
```

```bash
cd frontend
pnpm typecheck
pnpm test
pnpm build
```

开发与验证在独立 checkpoint 工作树完成；不自动变更 systemd、Caddy、线上数据库或发布目录。此改动需要在明确授权的部署流程中应用数据库迁移后才会上线。


本次交付验证（2026-10-05）：后端 708 passed / 10 skipped；前端 125 passed；typecheck、build 和 diff whitespace 检查通过。独立前后端审查问题均已修复。保留现有 LangSmith 弃用提示与 Vite 大 chunk 提示；未调用真实模型服务或执行线上部署，也未进行浏览器实机与真实 Docker 联调。
