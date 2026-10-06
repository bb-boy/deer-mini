# 记忆保存任务恢复与后台管理

已校验的记忆变更在 SQLite 原子登记并确认提交后，才进入持久恢复范围。服务启动会扫描已登记任务；重试只保存登记时的变更，不重新调用模型，不重放工具。抽取尚未结束、校验失败或登记未确认的任务不具备跨重启恢复保证。

## 运行环境

从 `backend/` 使用当前部署版本的虚拟环境运行：

```sh
.venv/bin/python -m app.memory.admin --help
```

命令面向可信后台运维环境，必须显式指定 `--user`。这只是数据所有权范围，不提供登录或管理员认证。不要将 CLI 暴露为用户可直接调用的远程命令。

CLI 使用 `DEER_MINI_DATABASE_PATH` 指定的绝对路径；未设置时沿用项目的默认 `backend/data/deer_mini.db`。使用前确认当前进程的配置与目标 backend 相同；CLI 不会自动读取服务管理器中的环境变量，也不加载 `.env`。例如，在已授权的目标环境中配置：

```sh
export DEER_MINI_DATABASE_PATH=/absolute/path/to/existing/deer_mini.db
.venv/bin/python -m app.memory.admin --user USER_ID list
```

CLI 只连接已存在的数据库，并要求其中已有 `memory_save_tasks`、`memory_save_items`、`memory_save_attempts` 表。`list` 和 `show` 使用 SQLite 只读连接；写命令使用禁止创建数据库的读写连接。数据库缺失、配置错误或表尚未部署时返回失败，不创建目录、初始化库或执行迁移。不要为使命令可用而在运行中的服务目录启动调试 backend。

CLI 不启动恢复 worker，也不写入用户记忆文件。只有显式使用 `show --include-current` 才读取当前记忆，此时必须设置与目标 backend 相同的绝对 `DEER_MINI_DATA_ROOT`：

```sh
export DEER_MINI_DATA_ROOT=/absolute/path/to/existing/users
.venv/bin/python -m app.memory.admin --user USER_ID show TASK_ID --include-current
```

实际保存仍由使用相同数据库和正确文件目录的 backend 完成。backend 必须保持单进程运行；CLI 与 worker 通过 SQLite 事务协调状态，worker 周期扫描发现新增的待处理项。无需为重试另开 backend，也无需重启服务。如果 backend 未运行，已接受的重试会等待其启动，等待时间仍计入恢复窗口。

## 命令

| 命令 | 行为 |
| --- | --- |
| `--user USER_ID list [--limit 100]` | 列出该用户最近登记的任务，默认最多 100 条，允许 1–1000 条 |
| `--user USER_ID show TASK_ID` | 查看任务、保存项、恢复截止时间、阶段、安全错误字段与尝试历史 |
| `--user USER_ID show TASK_ID --include-content` | 在详情中显式显示仍保留的待保存内容 |
| `--user USER_ID show TASK_ID --include-current` | 在各项的 `current_memory` 中显示对应当前记忆的只读快照，要求配置 `DEER_MINI_DATA_ROOT` |
| `--user USER_ID retry TASK_ID --confirm` | 为符合条件的未完成项开启新的 24 小时恢复窗口，保留旧历史 |
| `--user USER_ID cancel TASK_ID --confirm` | 请求停止未来尝试，保留已经确认成功的结果 |
| `--user USER_ID resolve-conflict TASK_ID OPERATION_ID --confirm` | 保留当前记忆，取消指定的旧冲突保存项 |
| `--user USER_ID cleanup --confirm` | 清理该用户已完全解决、关闭满 30 天的任务记录与尝试历史 |

在每条命令前加 `.venv/bin/python -m app.memory.admin`。所有写命令都要求 `--confirm`，缺少该标志时不会打开数据库。`--include-content` 和 `--include-current` 只用于 `show`，可以同时使用。前者显示 SQLite 中保留的待保存内容，后者显示文件中当前的实际记忆。成功或已取消项的复制内容已被清除，`--include-content` 无法取回；当前文件仍可通过 `--include-current` 查看。

当前记忆的读取先检查任务所属用户，再复用 MemoryStore 的用户目录和路径安全检查；不会创建目录、修改正文或修复索引。如果任一目标尚不存在、正文损坏或无法安全读取，命令非零退出，不输出部分正文；查看默认任务详情仍可独立进行。该快照反映读取时的内容，不锁定后续后台更新。

命令成功将 JSON 写入 stdout。写入任务状态的命令返回 `accepted: true`，表示任务账本事务已确认，不能把它当作记忆保存完成；用 `show` 查看后续实际结果。`cleanup` 返回 `deleted_tasks` 数量。错误写入 stderr，不回显完整异常或传入的私有内容：退出码 `0` 表示查询或账本操作成功，`1` 表示配置、状态、范围或存储失败，`2` 表示参数错误或缺少确认。失败时不能推断数据库或文件一定未改变；重新查询核实状态后再决定下一步。

## 如何判断结果

任务汇总状态包含 `pending`、`partial`、`success`、`failed`、`conflict`、`cancelled`。`partial` 表示已有成功项，但仍须检查每个保存项及 `closed_at`；它也可能表示成功项与取消项混合、任务已经关闭。

| 保存项状态 | 含义与处理 |
| --- | --- |
| `pending` | 已登记，等待 worker 处理 |
| `running` | 已开始一次处理；不要据此推断文件尚未写入 |
| `waiting` | 遇到可恢复故障，等待 `next_attempt_at`；不会持续占用用户的写入位置 |
| `verifying` | 先核实已发生的写入，不确定结果不能当作成功或盲目重写 |
| `success` | 保存结果已确认；恢复不重新执行成功项 |
| `failed` | 自动尝试已停止；解决原因后可对符合条件的普通失败重试 |
| `conflict` | 目标已有其他版本；不自动覆盖，人工决定是否保留当前记忆并取消旧项 |
| `cancelled` | 停止未来保存；不回滚已经保存的记忆 |

首次恢复截止时间从确认登记开始计算，默认 24 小时，包含排队与退避时间。重启保持原绝对截止时间。到期后不再启动新的自动内容写入；已开始的提交仍需要完成并核实，不确定项保留必要信息。手动重试为符合条件的项开启新窗口，历史中的 `window` 区分不同批次。

普通重试不能绕过版本冲突、非法访问或内容校验。任务来源 Thread 已删除或任务已经取消时，不能重新恢复。对混合结果始终逐项检查，不把任务重试理解为整批重新执行。

## 常见处理流程

先用 `list` 找到任务，再用 `show` 检查项状态、阶段、错误分类、下一次尝试时间和历史。权限、磁盘空间等环境故障应先在已授权范围内修复；还在自动恢复窗口内的任务会按照退避策略继续处理。普通失败符合重试条件时，执行 `retry ... --confirm`，随后再次查看状态。

正文已写入但索引失败，或文件已保存而 SQLite 结果登记失败时，worker 会依据操作 ID、版本和摘要核实，补齐尚未完成的记录。不要因账本显示未完成就手工复制正文，也不要直接修改数据库把项标成成功。核实不能重复新增记忆或刷新已经生效内容的更新时间。

出现冲突时，先检查任务详情，再按需使用 `show TASK_ID --include-content --include-current` 对照待保存内容与当前记忆。若决定保留当前记忆，执行 `resolve-conflict ... --confirm`。该命令只取消旧项，没有强制覆盖选项，也不会锁定或改写刚才查看的当前内容。

取消请求和来源 Thread 删除会阻止未来尝试，但已开始的同步提交可能完成。成功项保留成功结果，在途项核实后再清除复制内容。取消任务不删除真实记忆文件。

## 内容、历史与隐私

默认输出不包含记忆正文、名称、描述或原话证据；显式详情可以包含待保存内容或当前记忆，应仅在需要时使用，并注意终端录屏、输出重定向等可能保留其内容。错误信息只使用安全分类，不包含提供商完整响应或异常正文。

成功或取消后的复制内容由应用清除；尚未解决的失败、冲突或不确定项保留恢复所需信息。只有全部项为成功或取消、且任务关闭满 30 天时，`cleanup` 才会删除任务及其历史。清理限定当前用户，不删除实际用户记忆文件。应用层删除不等于 SQLite、WAL 或备份介质的物理安全擦除。

本能力不增加记忆管理 HTTP API、SSE 事件或聊天界面状态。

## 隔离验证

回归测试只使用临时 SQLite 和测试数据，不连接线上数据库、用户目录或真实模型服务。从隔离开发环境的 `backend/` 执行：

```sh
.venv/bin/python -m pytest tests/app/memory/test_admin.py -q
```

测试验证显式确认、用户隔离、默认隐藏内容、当前记忆只读查看及损坏时安全失败、已有数据库检查、重试新窗口、保留当前记忆的冲突处理及历史清理。线上表结构与服务配置的发布属于另行授权的部署工作。

## 调度配置

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `DEER_MINI_MEMORY_RECOVERY_WINDOW_SECONDS` | `86400` | 新任务自动恢复窗口，包含排队时间 |
| `DEER_MINI_MEMORY_RETRY_BASE_SECONDS` | `30` | 首次保存失败后的基础等待时间 |
| `DEER_MINI_MEMORY_RETRY_MAX_SECONDS` | `3600` | 指数退避的最大间隔 |
| `DEER_MINI_MEMORY_MAX_ATTEMPTS` | `12` | 每窗口最多启动的保存尝试，预算用尽可以早于窗口结束 |
| `DEER_MINI_MEMORY_SAVE_CONCURRENCY` | `4` | 同时处理的用户数量，同一用户串行保存 |
| `DEER_MINI_MEMORY_POLL_SECONDS` | `5` | 后台扫描间隔 |

空间与权限等环境故障不会快速循环重试。保存预算或窗口用尽后，结果不确定的项仍可做一次只读核实；核实不写新正文或索引，无法核实的项保留必要内容等待处理。每次开始尝试先登记历史，进程中断或结果登记失败的尝试标记为 `unconfirmed`，后续核实保留原尝试时间。

后台每小时清理一次全部已解决且关闭满 30 天的任务历史；也可用限定用户的 `cleanup` 命令提前触发同样的到期检查。未解决的失败、冲突或待核实项不会被清理。
