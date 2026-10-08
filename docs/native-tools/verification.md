# 原生工具验证记录

开发位置：`tx:/home/pl/deer_mini/.worktree/native-tools`，分支 `native-tools`。
BASE_SHA / 当前 HEAD：`e25a655f330e5783e63f1025182529294ce8e40b`。任务改动尚未提交，审查必须包含全部未跟踪文件及工作树差异。

## 隔离环境

- 后端工作树独立 Python 3.12 `.venv`，按 requirements.txt 安装。
- 后端命令从工作树 backend 运行；导入时设置 `DEER_MINI_DATABASE_PATH=/home/pl/deer_mini/.worktree/native-tools/backend/data/native-tools-validation.sqlite3`、`DEER_MINI_DATA_ROOT=/home/pl/deer_mini/.worktree/native-tools/backend/data/native-tools-validation-users`。tests/app/conftest.py 为每条测试替换临时数据库/目录；worker 使用同工作树下单独的 worker 路径。
- 前端工作树独立 node_modules，`/opt/deer-mini-runtime/bin/pnpm install --frozen-lockfile` 成功；pnpm 10.30.3。
- 没有启动/修改线上服务，没有接触线上数据库或用户文件，没有真实模型/Docker/Tavily 请求。

## 已执行的验证

| 检查 | 结果 |
| --- | --- |
| 修改前 `.venv/bin/python -m pytest tests/app --ignore=tests/app/model/test_factory.py -q` | 856 passed, 13 skipped；164.32s |
| 注册与提示测试先行（功能实现前） | 8 failed, 9 passed；确认缺少新工具/可用工具指导导致失败 |
| worker 原生行为测试先行（实现前） | 51 failed, 5 passed；缺少模块、Read 行号/安全能力 |
| 新工具注册、端到端循环、提示与大结果落盘测试 | 36 passed；6.87s |
| `tests/app/agents/test_native_tools_integration.py`（含实际 Runtime 持久化故障） | 3 passed；1.40s |
| worker `.venv/bin/python -m pytest tests/app/tools -q` | 213 passed；30.16s（含关键异常显式传播测试） |
| `/opt/deer-mini-runtime/bin/pnpm typecheck` | 退出码 0 |
| `/opt/deer-mini-runtime/bin/pnpm test` | 15 个文件、125 tests passed |
| `/opt/deer-mini-runtime/bin/pnpm build` | 退出码 0，Vite 构建成功 |

后端有 langsmith 已弃用导入警告；基线已存在。常规回归排除真实模型 test_factory.py 与 learning tests。跳过项与完整回归结果将在最终状态记录。

## 验收证据位置

- `backend/tests/app/tools/test_native_file_tools.py`：三目录/虚拟路径、拒绝逃逸/符号链接/硬链接/特殊文件、原子创建/覆盖、精确替换歧义、GB18030/UTF16/CRLF、无末尾换行 diff、大 diff、并发写改、取消等待、提交前后故障、搜索时间/深度/数量/输入输出预算与跳过样本。
- `backend/tests/app/agents/test_native_tools_integration.py`：真实注册表与 LeadAgent/公共中间件执行七次文件调用；明确的 is_error 工具消息；真实文件 checkpoint 恢复；已替换但无法确认持久化时 Runtime 标 error 且模型不重试。
- `backend/tests/app/services/test_run_coordinator_tools.py`：原生工具常驻，Bash 与 Tavily 保留配置开关。
- 现有 Read 与 tool_result_storage 回归：保留编码、base/offset 和完整选中内容落盘；不恢复旧 10000 字符截断。

## 最终回归与独立审查

首轮完整回归 930 passed、13 skipped、3 failed（167.36s）：失败均为 test_task_flow.py/test_todo_flow.py 子模型将工具列表写死为两项，与新增常驻工具不符。两处断言已更新，相关两文件 7 passed（4.55s）。

独立审查初轮无 Critical/Important，确认 Read 默认换行规范化与 Edit 精确多行 CRLF 匹配存在可用性细节。补 preserve_newlines=false 可选参数、编辑前保留换行/关闭行号的提示，以及 Read→Edit 的 CRLF/CR 契约测试；默认兼容行为不变。worker 针对性回归 95 passed（12.95s），待独立复审与最终完整回归。

最终冻结后完整后端回归：

```sh
.venv/bin/python -m pytest tests/app --ignore=tests/app/model/test_factory.py -q -rs --tb=short
```

结果 **943 passed, 13 skipped, 1 warning，172.80s，退出码 0**。13 个跳过项为 11 个显式开关的真实 Docker 测试和 2 个真实模型测试；未启用外部服务验收。warning 是基线已存在的 langsmith 弃用导入警告。前端代码自此前 typecheck/125 tests/build 成功后未再变动。

独立 reviewer 在最终冻结版本复跑 7 个受影响测试文件，**120 passed，29.04s**；独立重算源码/测试/依赖 digest 一致；两份设计文档一致且 diff --check 通过。此前 CRLF/CR Minor 已通过 preserve_newlines 通路及行为测试解决，无未解决审查意见。最终独立 Assessment：**Ready to merge? Yes**；无未解决 Critical、Important 或 Minor。审查角色为 deer-mini-module-reviewer，结论为代码合并条件评估，不构成合并或部署授权。

交付状态：需求与验收项全部落实，工作树保留；本任务不提交、推送、合并或部署。无未完成实现项，真实外部服务验证未执行。


源代码/测试/依赖差异 SHA-256：`dafcea035f931b35cf4be3fb772f44f0c28866de0e6273ff5c46e2be331a5c32`（按文件名排序，拼接文件名与内容，NUL 分隔；不含 docs）。

最终交付格式检查清除 integration 测试文件末尾一个空行，AST 一致，业务代码与上述测试执行版本不变。

## 2026-10-07 用户授权补充真实外部验收

此前“未执行真实外部服务”的记录为当时交付状态。用户后续要求执行真实 Docker 和模型验收：已有 Docker 11 passed；默认 SiliconFlow 非思考/思考完整验收 2 passed、99.55 秒，四个真实 Run 均 success，普通写入错误后续跑、原生/Bash 文件共享、CRLF、精确报告、文件/消息恢复和 scope 清理均通过。新增真实测试默认关闭（2 skipped），默认模式连同原有原生集成回归为 3 passed/2 skipped。

独立复审确认新测试 Code Quality PASS、默认配置验收可 APPROVE。既有 USTC 两项真实测试因 tx TCP 建连超时未通过，完整外部范围不能宣称全部通过；未修改供应商或线上服务配置。执行版本与仅证据 helper 调整的最终版本分别记录并绑定摘要。详细结果、复现方式、失败历史及限制见 [外部验收记录](external-validation.md) 和 external-validation-manifest.json。
