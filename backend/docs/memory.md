# 用户结构化记忆

记忆按 `user_id` 私有隔离，同一用户的不同 Thread 复用。分类只有 `user`（明确的画像）、`feedback`（偏好和纠正）与 `reference`（外部信息位置及用途）；不保存项目进度，不新增 Project 实体。

## 启用与调用成本

`DEER_MINI_MEMORY_ENABLED=true` 默认启用，设为 `false` 可同时关闭召回和自动抽取。无已有记忆时不调用选择模型；成功 Run 仍会安排抽取，模型可以返回空 `changes`。有记忆时，每个 lead Run 的首次主模型调用之前最多进行一次选择调用。内部调用复用该 Run 的模型配置，分别创建、关闭独立客户端，禁用工具和思考输出，不发送到回答的 SSE 文本流。

## 存储与格式

```text
<DEER_MINI_DATA_ROOT>/<user_id>/memories/
  MEMORY.md
  user_<32位UUID十六进制>.md
  feedback_<32位UUID十六进制>.md
  reference_<32位UUID十六进制>.md
```

文件不位于 Thread 工作区，不挂载给 Bash 或普通文件工具，删除 Thread 不删除用户记忆。本模块没有新增公开 API 或记忆管理页面。

正文是事实来源。元数据使用 JSON 字符串值组成的 YAML 兼容 frontmatter，包含以下全部字段，时间必须带时区，ID 与文件名、类型必须一致：

```markdown
---
id: "feedback_0123456789abcdef0123456789abcdef"
type: "feedback"
name: "解释风格"
description: "解释方案时先给结论，再补充理由"
created_at: "2026-10-04T00:00:00+00:00"
updated_at: "2026-10-04T00:00:00+00:00"
source_thread_id: "来源对话ID"
source_run_id: "来源Run ID"
---
解释方案时先给结论，再补充理由。

**Why:** 用户明确要求；没有额外说明原因。
**How to apply:** 解释设计方案时。
```

`MEMORY.md` 包含 JSON 围栏中的元数据数组，额外有 `filename` 和 `stable`，不包含正文。`catalog()` 在健康路径只打开索引并检查正文文件状态；索引缺失、损坏或正文集合/修改时间发生变化时，从合法正文重建。损坏的正文保留在原处并跳过。`read()` 仅打开指定 ID 的正文。手工修改正文后，应保持 frontmatter 合法，文件修改时间必须反映这次修改；本模块不支持直接编辑索引作为事实来源。

`stable = 当前UTC时间 - updated_at >= 48小时`。实际修改名称、描述或正文才更新时间；相同内容重复抽取不改时间与来源。读取、索引修复与 stable 刷新不改正文。stable 只表示两天未更新，不代表内容经过验证，也不影响模型授权或强制召回。

每个用户最多 1,000 条有效记录；正文最多 64 KiB，单文件最多 128 KiB，索引最多 4 MiB，名称最多 200 字符，描述最多 1,000 字符。路径逐级以目录描述符打开，拒绝穿越、符号链接、硬链接和特殊文件。文件通过同目录临时文件、fsync 与原子替换提交，权限为目录 0700、文件 0600（已有目录权限不自动重设）。

## 读取时序

```text
lead before_agent
  → MEMORY.md 元数据快照
  → 选择模型：编号、name、description
  → {"memory_indices": [1, 4]}
  → 后端按此次快照映射永久ID，读取选中正文
  → prepare_model_messages 注入请求副本
  → 主模型与正常工具循环
```

数字编号只在这次请求有效，不是永久 ID。模型不能返回路径。候选按更新时间从新到旧排列，只提供编号、名称和描述，最多 32,000 字符的完整条目。返回必须是最多 5 个正整数，拒绝布尔值、字符串、越界编号与额外字段；重复编号去重并保留顺序。输入包含当前用户消息的末尾 16,000 字符和最近 8 条用户/助手消息（每条末尾 4,000 字符），长文本预算之外的信息本轮不可见。

主模型只收到选中的名称、描述、类型和完整正文。总装配预算为 24,000 字符，超预算整条跳过。记忆通过 `user` 数据消息和边界标记放入请求副本，不写入 checkpoint，不随工具循环重复检索或累积。固定系统规则说明记忆是可能过时的背景，当前明确纠正优先，不能覆盖系统规则或授权操作；reference 不能证明指向的文档已经读过。

选择调用限时 15 秒并计入 Run 总时限，使用现有模型错误分类与独立重试预算。普通超时、解析/读取错误降级为空记忆；外部取消、Run 总超时与 `StatePersistenceError` 继续传播。客户端关闭使用已有 5 秒收尾预算，取消在清理过程中也保持传播。

## 写入时序

```text
Runtime 成功返回（最终checkpoint、Run success及收尾已确认）
  → Coordinator 完成回调
  → MemoryService 后台队列（深拷贝最终状态）
  → 独立抽取模型
  → 校验全部建议
  → 正文提交
  → MEMORY.md 提交
```

由 Coordinator 直接安排独立抽取调用，不通过 `task` 工具，不新增通用工具循环 agent。失败、取消、超时或状态保存失败的 Run 不抽取。同一用户串行，不同用户并行；前一任务失败不阻断下一项。每次抽取重新读取最新索引，最多提供前 5 条、总共 24,000 字符的既有正文。

模型返回 `{"changes": [...]}`，最多 10 条。每条包括 `action`（`add` 或 `update`）、`type`、`name`、`description`、`content` 和 `evidence`；更新额外提供 `memory_id`。evidence 必须是本轮用户消息可见范围内的原文，既往消息或助手陈述不能单独作为依据。所有建议在写入之前检查类型、字段、目标归属、证据、长度与疑似凭据；feedback 必须包含 `**Why:**` 和 `**How to apply:**`。不提供原因时不编造经历。语义是否具有长期价值仍由抽取模型判断，原文匹配不能代替语义验证。

抽取调用限时 30 秒，后台错误仅记录异常类型及 Run 标识，不打印记忆或异常正文，不修改已成功的主 Run。关闭时停止入队，默认等待 5 秒后取消剩余任务，并收尾客户端与进行中的文件操作。单条正文和索引分别原子替换；批量变化不是事务，若磁盘失败，先前已提交的条目不会回滚，后续读取会修复索引。

## 当前限制与验证

当前仅支持 POSIX 文件接口和单进程用户锁。后台队列只在内存中，强制重启可能丢失未完成任务；本版不提供补写、删除/清理接口或持久任务队列。后台更新可能晚于紧接着的下一轮检索。容量与上下文预算之外的条目本轮不可见，长期运行时需另行设计容量清理与队列背压。

测试使用假模型、真实临时文件和 SQLite，覆盖隔离、链接检查、索引修复、48小时边界、编号快照、上下文预算、取消与抽取时机。常规回归排除真实模型测试 `tests/app/model/test_factory.py`。部分已有测试依赖默认开发库初始化，可在隔离副本执行：

```sh
env -u DEER_MINI_DATABASE_PATH -u DEER_MINI_DATA_ROOT .venv/bin/python -c 'from app.infrastructure.database import initialize_database; initialize_database(); import pytest; raise SystemExit(pytest.main(["tests/app", "--ignore=tests/app/model/test_factory.py", "-q"]))'
```

该命令只应在隔离开发环境执行，不在运行服务的目录或 live 数据配置下执行。
