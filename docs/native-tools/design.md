# 原生工具需求与设计（2026-10-06）

## 需求来源与范围
用户希望 deer-mini 自己实现原生工具，参考 Serena 的工具体验；随后明确要求「新建一个工具开发工作树，在这个工作树里完成开发」。沿用此前讨论的 Read / Bash / Grep / Glob / WebSearch / Edit / Write 范围。此文档固化已讨论范围与实现决定，不将 MCP、LSP 或符号重构加入本次开发。

- 实际主仓库：`tx:/home/pl/deer_mini`。
- 开发工作树：`tx:/home/pl/deer_mini/.worktree/native-tools`，分支 `native-tools`。
- BASE_SHA：`e25a655f330e5783e63f1025182529294ce8e40b`。
- 主仓库需求文档：`/home/pl/deer_mini/docs/native-tools/design.md`；工作树保存同一版本以随代码交付。
- 不部署、不修改运行中服务或线上数据、不推送或合并。

## 用户行为与工具接口
五个原生文件工具始终注册，无需 Docker、外部密钥或 MCP。Bash 和网页工具继续按现有条件注册。复用 ToolRegistry、ToolExecutor、错误处理、大结果落盘以及原有文件 checkpoint。

| 工具 | 参数与行为 |
| --- | --- |
| `read_file` | 保留 `path`、`encoding`（utf-8/gb18030/utf-16）、`base`（1 起始行）、`offset`（读取行数）；默认返回原始选中内容。新增 `line_numbers=false`，启用后显示真实行号；`preserve_newlines=false` 保持历史通用换行行为，设 true 可读取精确 CRLF/CR 供后续编辑。完整选中结果交公共落盘中间件，不能恢复旧版 10000 字符截断。 |
| `glob` | `pattern` 非空相对模式，`path="workspace"`，`limit=100`（1–1000）；递归匹配普通文件，`**/*.py` 包含顶层文件；返回规范 Thread 路径列表及 truncated/skipped 信息。模式不能越界。 |
| `grep` | `pattern` 非空，`path="workspace"`（文件或目录），`glob="**/*"`，`literal=false`，`case_sensitive=true`，`context_lines=0`（0–5），`limit=100`（1–1000）；逐行文本搜索，返回路径、1 起始行号、文本及上下文。无匹配是成功；错误正则是工具错误；不完整搜索必须标识。 |
| `write_file` | `path`、`content`、`overwrite=false`、`encoding="utf-8"`；创建必要父目录；默认拒绝覆盖，显式 overwrite 才可替换已存在普通文件；原子写入成功且持久化确认后返回规范路径、写入字节数。 |
| `edit_file` | `path`、非空 `old_string`、`new_string`、`replace_all=false`、`encoding="utf-8"`；精确字面替换；找不到或默认模式有多处匹配时拒绝且不改变原文件。支持替换全部，保留编码与未改动换行字节；成功返回路径、替换次数和统一 diff（大结果可落盘）。 |

参数用 Pydantic 严格校验，拒绝额外字段、布尔冒充整数和空白路径。面向模型使用现有 snake_case 命名。只实现独立行为，不复制 Serena GPL 源码。

## 文件与执行边界
- 所有输入通过当前 RuntimeContext 的 ThreadPaths 解析，允许 workspace/uploads/outputs 和 /mnt/user-data 虚拟路径；禁止逃逸、符号链接、特殊文件和硬链接。错误文本不暴露服务器绝对路径。
- 复用 `storage.file_io` 的目录描述符、NOFOLLOW、普通文件检查、AtomicWriter；不以一次 resolve 后普通 open 代替安全 IO。
- 不向外暴露内部 `.tool-results` 和存储暂存文件。递归搜索跳过 `.git`、`.venv`、`node_modules`、`__pycache__` 和 `.tool-results` 等内部/依赖目录，并报告跳过情况。普通隐藏文件可搜索。
- read/write/edit 单文件上限 8 MiB，超出返回明确错误；grep 每文件 1 MiB、总读取 32 MiB、遍历最多 10000 项和深度 64；明确报告资源上限、二进制、编码或权限导致的未扫描项目，不伪装成完整无匹配；跳过详情最多保留 100 条且最多 256 KiB，返回 skipped_count/skipped_truncated 标志。Grep 的匹配 JSON 和 Glob 文件列表 JSON 各限 4 MiB，超出返回 output_limit/truncated。
- grep 用带 timeout 的 `regex` 库（直接声明依赖），限制模式长度 4096、单次匹配及总执行时间，避免事件循环阻塞。Glob 不依赖系统 rg。
- 同一 Thread 的原生写/改操作串行，校验到写入处于同一临界区，防止两个子任务都通过“不存在”或读旧值后丢失更新；不声称与任意 Bash 写入互斥。
- 写入任务取消时先等待在途 IO 收尾，再传递 CancelledError；StatePersistenceError 不可吞掉。提交后/提交状态不确定的存储失败必须向 Runtime 传播，不能记成功或可重试的普通工具错误。
- 编辑/覆盖保留普通权限位（含可执行位），不保留 setuid/setgid；新建文件采用私有默认权限。
- 小文件返回上下文 unified diff；超过 128 Ki 字符或 2000 行时返回线性生成的完整文件替换 hunk，标明 diff_format=whole_file；正确标注缺少末尾换行。
- Read 默认保持原 TextIO 的通用换行和按行语义；Grep 行号与之保持一致。审查发现多行 CRLF 编辑需取得原始换行，因此增加 preserve_newlines=true 选项；编辑前指导模型使用该选项并关闭行号，不改变 Edit 精确匹配语义。
- 文件变更属于现有 turn 文件 checkpoint，不引入另一套 undo 协议。

## 集成与验收
1. 实际注册的是 read_file_rewrite.ReadFileTool，保留其现有错误和选行兼容性；旧未注册模块不做无关重构。
2. 无 Docker/无 Tavily 配置时仍能搜索、创建并编辑文件；原有条件注册测试继续成立。
3. 用户执行过程显示「匹配文件」「搜索内容」「修改文件」「写入文件」和路径/模式提示；API/SSE 结构保持现有协议。
4. 模型获得可用工具对应指导，先定位再读取/修改，交付成果写入 outputs；不推荐不可用工具。
5. 临时 Thread 数据中验证三种路径区域、别的 Thread/外部路径隔离、链接/特殊文件、参数边界、编码/换行、歧义替换、显式覆盖、搜索不完整报告、取消和持久化失败、并发以及 checkpoint 恢复。
6. 后端常规回归排除 tests/app/model/test_factory.py；前端 typecheck、test、build。全部使用独立工作树依赖、临时数据和 mock 模型，不触碰线上数据。
7. 独立 reviewer 对照此文档检查需求、代码质量和集成；Critical/Important 修复并复审后交付。

参考：https://github.com/oraios/serena 及 https://oraios.github.io/serena/01-about/035_tools.html 。借鉴明确的路径、检索、局部读取和可核对编辑结果设计，保持 deer-mini 原生执行模型。
