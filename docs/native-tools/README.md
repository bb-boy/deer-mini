# deer-mini 原生文件工具

工具直接在后端 ToolRegistry 中注册，经 ToolExecutor 和公共中间件执行；不需要 MCP 服务。Bash 仍要求 Docker 和显式启用，web_search / web_fetch 仍要求 Tavily 配置。

## 常用调用

先按文件名或内容定位：

```json
{"name":"glob","arguments":{"path":"workspace","pattern":"**/*.py"}}
{"name":"grep","arguments":{"path":"workspace","glob":"**/*.py","pattern":"class Agent","literal":true}}
```

读取需要的部分（base 是起始行，offset 是行数）：

```json
{"name":"read_file","arguments":{"path":"workspace/app.py","base":10,"offset":30,"line_numbers":true}}
```

精确编辑前，用 preserve_newlines=true、line_numbers=false 读取原文（可同样指定 base/offset）。这会保留 CRLF/CR；默认读取会将它们规范化为 LF，多行文本直接复制到 old_string 可能不匹配。不要将展示行号放入 old_string。

```json
{"name":"read_file","arguments":{"path":"workspace/app.py","preserve_newlines":true,"line_numbers":false}}
```

精确编辑已有文本，再将完整成果写入交付区：

```json
{"name":"edit_file","arguments":{"path":"workspace/app.py","old_string":"timeout = 10","new_string":"timeout = 30"}}
{"name":"write_file","arguments":{"path":"outputs/report.md","content":"# 结果\n\n完成分析。\n"}}
```

- edit_file 默认只接受唯一精确匹配；出现多处匹配会返回错误，可扩大 old_string 上下文，或明确设置 replace_all=true。空 old_string 不允许。
- write_file 默认拒绝覆盖；需要完整重写已存在文件时显式设置 overwrite=true。两个原生写操作不能同时通过“文件不存在”的检查。
- read_file / edit_file / write_file 支持 utf-8、gb18030、utf-16；编辑保留未改动的换行和编码。带行号读取时，行号是展示信息，不能复制进 old_string。
- Read 保持原 TextIO 的换行和行号语义（CRLF/CR 读取时规范化为 LF），Grep 与它使用相同行号。
- 编辑/覆盖保留普通权限位（含可执行位），不保留 setuid/setgid；大文件 diff 使用标明 whole_file 的完整替换 hunk，避免计算最小差异耗时过长。
- read_file 的默认结果为选中文本；大工具结果由现有中间件保存并通过 read_tool_result 分页获取。

## 路径和结果边界

所有文件均属于当前 Thread 的 workspace、uploads、outputs。`notes.txt` 等价于 `workspace/notes.txt`，也可使用 `/mnt/user-data/workspace/notes.txt`。不接受服务器路径、其他 Thread 的文件、符号链接、硬链接或特殊文件。

Glob 和 Grep 使用相对于 path 的模式；`**/*.py` 同时匹配该目录内和更深层的 Python 文件。无匹配为成功。响应中的 truncated/skipped 表明资源限制或未扫描项目；出现这些标记时不能断言整个目录不存在匹配，应缩小 path/glob 后继续搜索。普通隐藏文件可检索，依赖目录和工具内部文件会被跳过。

单文件读、写、编辑上限为 8 MiB。Grep 搜索 UTF-8 文本，每文件最多 1 MiB，总读取最多 32 MiB；默认返回最多 100 条，可用 limit 调整至最多 1000 条。搜索有目录规模、深度及时间预算，正则匹配有超时保护；Grep 匹配 JSON 预算为 4 MiB，超过会明确报告 output_limit。大量跳过项只保留路径样本，并返回总数与样本截断标志。

原生修改使用持久化原子替换，并串行同一 Thread 的原生写/改。任意 Bash 命令仍可直接修改工作文件，不保证与原生工具相互锁定。写入取消会等待在途 IO 收尾；因此取消不表示自动回滚，用户仍可使用现有 turn 文件恢复点恢复本轮变更。

## 验证

需求见 [design.md](design.md)，实施步骤见 [实现计划](../superpowers/plans/2026-10-06-native-tools.md)，交付测试与审查证据见 [verification.md](verification.md)。所有回归使用临时数据与 mock 模型；本模块未部署到运行中的服务。
