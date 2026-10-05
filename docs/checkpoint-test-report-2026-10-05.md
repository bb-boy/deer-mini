# 文件恢复点补充测试记录

日期：2026-10-05（Asia/Shanghai）。目标分支：远端 `checkpoint`。

## 测试范围与结果

| 验证 | 环境 | 结果 |
| --- | --- | --- |
| 真实 Docker 集成 | tx，Docker 29.1.3，python:3.12-slim | 6 项通过，真实模型用例排除 |
| 后端完整回归 | tx，Python 3.12.13，隔离数据 | 708 项通过，13 项跳过 |
| 后端补充回归 | 本地 Python 3.12.14，独立虚拟环境 | 707 项通过，14 项跳过 |
| 前端完整回归 | tx / 本地，pnpm 10.30.3；远端单 worker、640 MiB 堆上限 | 15 个文件、125 项通过 |
| 前端类型检查与构建 | 本地隔离开发目录 | 通过，保留已有大体积 chunk 警告 |
| 浏览器操作与真实 API | Chrome、React、FastAPI、临时 SQLite 和文件目录 | 下列场景通过 |

本地后端跳过项包含需要真实 Docker、真实模型或 Linux/root 权限的用例。
Docker 测试先在远端运行完成；随后 SSH/Tailscale 中断，浏览器测试改在本地完成。
浏览器使用固定模型回复和仅能运行预定义命令的临时执行器；生产路由、LeadAgent、Runtime、SSE、文件快照和恢复服务保持真实。
未调用真实模型服务，未部署或合并，未访问线上数据库和用户目录。

## 真实 Docker 场景

1. 容器后台持续写入时，下一轮保存快照前先停止旧容器。
2. 新一轮完成前停止后台写入；恢复后不会再次出现被删除文件。
3. 前台持续写入被取消后，Run 为 interrupted，容器清理完成，消息与文件可以恢复。
4. `.tool-results` 在真实容器中只读，`.checkpoints` 不暴露给容器。
5. Agent 工具循环可取得真实 Docker 命令输出。
6. 超时和取消会删除对应容器（现有用例复验）。

测试使用随机容器标签，仅清理测试拥有的容器。新增可复验用例位于
`backend/tests/app/sandbox/test_checkpoint_docker_live.py`。

```sh
# 在 backend/ 执行；不运行真实模型
PYTHON_DOTENV_DISABLED=1 RUN_DOCKER_SANDBOX_TEST=1 .venv/bin/python -m pytest \
  tests/app/sandbox/test_checkpoint_docker_live.py \
  tests/app/agents/test_bash_agent_loop_live.py -k 'not live_model' -v
```

## 浏览器场景

- 从页面发送两轮任务，真实写入 v1/v2 文件，确认消息、工具事件和文件列表更新。
- 预览后通过上传 API 新增文件；确认返回 409，已有消息和文件保持不变，面板刷新差异并要求重新确认。
- 从 v2 恢复至 v1；消息回退、文本覆盖、空目录和二进制内容恢复、后续新增及上传文件删除。
- 刷新浏览器后仍保持恢复结果，旧回复没有重新出现。
- 选择“恢复前备份”撤销恢复；v2 消息、文件和后上传文件重新出现。
- 运行持续写入任务，从恢复面板选择恢复点；先取消运行，确认写入停止，再恢复消息和文件。恢复操作凭证清除，面板关闭。
- 恢复首轮开始前：消息与公开文件列表均为空；随后重新发送任务，正常完成。
- 最终浏览器控制台没有未处理的 error/warn。

## 测试自身的问题与修正

首次在干净的本地环境跑回归，发现三个旧测试依赖已初始化的默认数据库。
`backend/tests/app/conftest.py` 已增加自动 fixture，为每个测试初始化临时数据库和文件根目录，清除继承的存储环境变量；单个测试仍可显式替换配置。

另一个模拟 Docker 超时用例只给 Python CLI 0.1 秒启动时间。本机实测启动耗时可达 0.396 秒，导致尚未输出 started 就超时。
`backend/tests/app/sandbox/test_docker_runner.py` 将该测试的超时改为 1 秒，仍验证 60 秒 sleep 被中断及容器清理；生产超时配置未改变。

两项修正后，完整后端回归通过。此次没有修改生产功能代码。

## 同步与清理状态

- 新增 Docker 用例已同步到远端并执行通过。
- 连接恢复后，两处测试修正已同步至远端 `checkpoint`。
- 本地临时前后端和测试浏览器页已关闭；远端已确认没有遗留测试服务或测试容器。
- 远端内存总计 1963 MiB，复验前可用约 1270–1290 MiB。测试已串行完成：后端运行时可用约 1174 MiB，前端运行时约 983 MiB。前端使用 `NODE_OPTIONS=--max-old-space-size=640 pnpm exec vitest run --maxWorkers=1`。
- 远端测试目录：`/tmp/checkpoint-integration.3lSb6j`；仅监听回环端口 18065、15175。
- 本地测试目录：`/tmp/checkpoint-integration.3lSb6j`；仅监听回环端口 18066、15176。
- 最新远端日志：`remote-backend-final.log`、`remote-docker-final.log`，在远端测试目录；前端日志为 `remote-frontend-final.log`。
- 本地最终回归日志：`local-regression-backend-final.log`、`local-frontend-tests.log`、`local-build.log`，均在本地测试目录。
