# 对话附件、生成结果与文件预览

用户上传资料后，文件保存在该 Thread 的 uploads；Agent 用 `read_file("uploads/资料.txt")` 读取。Bash 从 `/mnt/user-data/workspace` 开始，结果应写到 `../outputs/结果.md`。任务结束后，右侧文件面板刷新并显示可预览、可下载的结果。

## 对照 DeerFlow

- `backend/app/gateway/routers/uploads.py::upload_files()` 把上传保存到 Thread 的 uploads；Mini 沿用这个目录职责。
- `backend/packages/harness/deerflow/config/paths.py::resolve_virtual_path()` 将模型路径映射到当前 Thread；Mini 扩展已有 ThreadPaths，接受清晰的区域前缀。
- `backend/packages/harness/deerflow/agents/middlewares/uploads_middleware.py::UploadsMiddleware` 告诉模型有哪些附件；Mini 在已有上下文钩子中列出最多 50 个附件名称，不读取内容，名称按 JSON 数据编码。
- `frontend/src/components/workspace/artifacts/artifact-file-list.tsx` 与 `artifact-file-detail.tsx` 提供文件卡片、预览、源码切换和下载；Mini 采用相同主要交互。
- Mini 不引入 LangGraph、远程文件同步、文档自动转换、在线编辑和 Skills 安装。真实工具执行、工具结果回到模型、Checkpoint 保存继续走现有调用链。

## 路径约定

| 路径示例 | 指向当前 Thread 的位置 |
| --- | --- |
| `uploads/资料.txt` | 上传资料区 |
| `outputs/结果.md` | 生成结果区 |
| `workspace/script.py` | 工作区 |
| `report.txt` | 兼容旧调用，仍指向 workspace/report.txt |
| `/mnt/user-data/uploads/资料.txt` | 兼容已有虚拟绝对路径 |

第一级的 uploads、outputs、workspace 是保留的区域名，不自动搜索同名文件。旧的 workspace/outputs/result.txt 仍保留在磁盘，并通过 `workspace/outputs/result.txt` 访问；旧的裸 `outputs/result.txt` 地址现在表示真正的输出区。所有新列表项都返回明确的区域前缀。

## 主要模块的输入、输出与副作用

### ThreadPaths.resolve_agent_path

- 功能：为上传、读取、下载统一确定文件属于哪个区域。
- 输入：构造对象时传入服务器为当前 Thread 保存的 `thread_dir`；调用时传入 `raw_path`，如 uploads/资料.txt。模型不决定服务器根目录。
- 输出：当前 Thread 对应文件的绝对路径；不安全路径抛出 ValueError。
- 副作用：只检查路径及符号链接，不读取内容、不创建文件、不写 SQLite。

拒绝 `..`、服务器任意绝对路径、符号链接和超出指定区域的路径。这是路径访问边界检查，不是完整的恶意进程并发隔离方案。

### WorkspaceFileService

- 功能：接收上传、列出三个区域的文件、解析下载目标。
- 输入：`workspace_path` 来自已校验用户归属的 Thread；`filename` 是上传文件名；`chunks` 是浏览器分块传来的字节；下载的 `relative_path` 来自文件列表。
- 输出：上传/列表返回名称、区域前缀路径、大小、修改时间；下载返回验证后的文件路径。
- 副作用：上传写入 uploads，临时文件完整写完后原子替换；列表和路径解析只读磁盘。保留原来的 10 MiB 默认上传限制和同名替换行为。

### WorkspaceContextMiddleware

- 功能：每次 Run 开始时让模型知道当前 Thread 的可用附件和路径规则。
- 输入：`state` 是该对话当前消息状态，`context` 提供用户、对话、本轮执行与工作目录。
- 输出：更新本轮 system 上下文，旧 Run 的上下文先移除。
- 副作用：只读附件名称并修改内存状态；不直接访问 SQLite，后续状态由原 Runtime/Agent 的保存流程持久化。列表失败记应用日志，不伪造文件内容。

### WorkspaceFiles / FilePreview

- 功能：生成结果、上传资料、工作文件显示为卡片，点击进入详情，下载按钮独立于预览。
- 输入：`files` 是当前对话的文件元数据；`disabled` 表示能否上传；`downloadUrl` 根据当前用户和 Thread 构造地址；`onUpload` 接入已有上传流程。预览接收选中文件 `file`、请求地址 `url` 和返回操作 `onBack`。
- 输出：卡片、预览内容、加载/错误提示；Markdown 和 HTML 可切换源码。
- 副作用：上传经已有 API 写文件；预览只读请求，关闭或切换对话时取消请求并释放图片 URL，不修改服务器文件。

预览支持 UTF-8 文本、Markdown、HTML、常见图片；上限 5 MiB，其他类型可下载。HTML 放在无脚本权限的 iframe 中，外部资源不加载。`read_file` 仍只解码 UTF-8 文本，最多向模型返回 10000 字符，不等于能自动解析 PDF/Word。

## 手动验证

1. 上传一份文本资料，文件卡片显示 `uploads/资料.txt`。
2. 发送“读取 uploads/资料.txt，基于内容生成总结，保存到 outputs/总结.md”。启用 Bash 时 Agent 可真正创建文件。
3. 运行完成后打开“文件 → 生成结果”，点击总结卡片检查 Markdown 预览、源码和下载内容。
4. 切换另一段对话，不能继续显示上一段对话的预览。
5. 在上传尚未完成时切换对话：新对话不能显示旧文件卡片，旧上传完成后也不能覆盖当前列表。

2026-09-14 收尾验证：后端 152 项通过；前端 77 项通过并完成构建；另用真实 Docker 验证工具循环将文件处理结果送回模型接口。实际运行的 HTTP 服务也已验证上传、Tool Registry / Tool Executor 中的 read_file、Docker 输出、列表、下载和旧文件访问，验证用 Thread 与文件随后清理。本次没有访问真实模型服务。

切换对话时，App 在显示前清空旧文件卡片；上传与随后的列表请求返回时，会核对当前用户和 Thread，只有仍属于当前对话的结果才更新面板。补充的两项检查分别复现了修改前的残留卡片与延迟上传覆盖问题。

## Git 检查

在项目根目录使用 `git status --short` 查看哪些文件改变，再用 `git diff` 阅读实际修改。确认后将本次文件流转相关文件单独提交；已有的全局样式修改属于其他工作，不应混入此次提交。
