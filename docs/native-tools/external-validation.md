# 原生工具真实 Docker 与模型验收

日期：2026-10-07（Asia/Shanghai）。用户已明确授权补做真实外部服务验收。

**默认 SiliconFlow 模型的非思考、思考两种模式验收通过；已有 11 项真实 Docker 验证通过。USTC 两项模型验收因 tx 到服务端的 TCP 连接超时未通过，因此不能宣称所有供应商的外部验收全部通过。**

## 范围与结果

沿用 `design.md` 的原生工具行为、安全和失败边界。本轮只新增验收测试与验证文档，保留同一 native-tools 工作树。

| 验收 | 结果与证据 |
| --- | --- |
| AC-LIVE-1：真实 Docker 挂载、隔离、超时取消、持久工作区、checkpoint 恢复 | 11 passed，退出码 0，pytest 16.45 秒；`live-docker-evidence.json` |
| AC-LIVE-2：既有真实 USTC Agent 的 Read/Bash 两个测试 | 两项均失败，ModelCallError，无 HTTP 状态码；DNS 正常、TCP 5 秒超时、未进入 TLS；`existing-live-agent-evidence.json`、`provider-connectivity-evidence.json` |
| AC-LIVE-3：默认真实模型的 Read/Glob/Grep/Edit/Write、普通工具错误后继续、CRLF 编辑、成果内容、两种思考模式 | 2 passed，退出码 0，99.55 秒；`live-model-evidence.json`；真正收到思考内容 |
| AC-LIVE-4：真实 Bash 与原生工具共享文件、跨轮状态、成功持久化和首轮起点恢复 | 两种模式各两轮 Run 均 success；精确文件字节、报告三行内容、恢复 committed/cleaned、原始文件和空消息恢复均断言通过 |
| AC-LIVE-5：临时数据隔离、显式开关、随机 Docker scope、秘密保护与清理 | 默认关闭 2 skipped/退出码 0；真实数据均来自临时目录，tracing 关闭，SDK retries=0，模型重试次数=1；各测试 scope 清理后 Docker 查询为空 |

默认模型配置为 `siliconflow-deepseek-flash`。非思考两轮分别 6/7 次模型请求和 6/7 次工具调用；思考两轮分别 6/5 次模型请求和 6/7 次工具调用，实际思考内容为 1304/4476 字符。每轮保留产品现有 8 轮模型调用上限。第二轮出现预期的 write_file already_exists 工具错误，模型消费该错误后调用 edit_file 恢复并继续完成。

原生/Bash 共享文件按明确虚拟绝对路径操作；成果报告精确为三行 `status=new`、`shared=seen-by-native`、`collision=recovered`（均带行末换行，共 53 字节）。恢复第一轮 turn_start 后，source/collision 原字节还原，新增 shared/report 删除，消息列表为空。

独立短场景补证：默认模型真实 public Coordinator→Runtime→六工具，4 次模型请求、6 次工具调用，7.59 秒，Run success、CRLF/outputs 精确字节、文件和消息恢复全部通过，无 scope 残留；`basic-live-chain-evidence.json`。另有 1 次独立真实流式 smoke 成功（2.39 秒、7 个文本片段）；`default-provider-smoke-evidence.json`。

普通回归补查：原有原生集成 3 passed、新增真实测试默认 2 skipped，退出码 0；`default-mode-regression-evidence.json`。langsmith 弃用导入 warning 为此前基线已存在。

## 数据与容器隔离

在 app 导入前固定绝对 DEER_MINI_DATABASE_PATH 和 DEER_MINI_DATA_ROOT；pytest fixture 清空环境后，新测试再次固定临时路径，再使用 dotenv override=False 读取凭据。没有将 .env、数据库或运行日志作为交付文件。测试关闭 LangSmith tracing，不输出密钥和完整供应商错误。

11 项既有 Docker 验证前后记录确认原有容器 ID/State 一致、无新增容器。附加真实模型测试使用随机 native-live-test-* scope，仅清理自身 scope；结束后查询无容器。完整模型阶段没有保存全体生产容器的前后原始快照，不补造这份证据；隔离与清理范围另外由源码审查及 scope 查询验证。

## 代码版本与证据

工作树：`tx:/home/pl/deer_mini/.worktree/native-tools`，分支 native-tools，HEAD/BASE `e25a655f330e5783e63f1025182529294ce8e40b`。

原有 16 个源代码/测试/依赖变更文件摘要仍为 `dafcea035f931b35cf4be3fb772f44f0c28866de0e6273ff5c46e2be331a5c32`，本轮未改这些文件。此前 943 passed/13 skipped 与前端验证仍适用于原实现。

新真实测试：`backend/tests/app/agents/test_native_tools_live.py`。

- 两项真实测试执行版本 SHA-256：`286d43e6b744eb0460aff5caa68e13aa32b250a392adaee9ca6cd13367c9bd66`。
- 最终测试文件 SHA-256：`77a939515370c34e923e6b22ccbbcd160ec1aec14cb6e8fa2059c3d3f261f6ab`。
- 真实执行后仅修正历史证据汇总 helper；主测试函数 AST 摘要前后均为 `3bc267ff8f0156f798c67ef4018a3d354165a32a6bb8daec4716e6668415e490`。未重新调用模型；helper 独立临时 JSON 验证通过，最终默认 skip 检查 2 skipped/0.65 秒/退出 0。此差异没有改变真实验收行为与断言。

验证 JSON 的内容摘要见 `external-validation-manifest.json`。

## 复现方式

所有命令从本工作树 backend 执行。必须在独立进程、app 导入前固定临时绝对数据库/用户目录，显式读取凭据并关闭 tracing。新真实测试默认关闭；不要在 routine regression 中启用真实服务。

已有 Docker 用例：

```sh
RUN_DOCKER_SANDBOX_TEST=1 .venv/bin/python -m pytest tests/app/sandbox/test_thread_sandbox_live.py tests/app/sandbox/test_checkpoint_docker_live.py tests/app/agents/test_bash_agent_loop_live.py -k 'not live_model' -q -rs --tb=short
```

新模型完整链的隔离驱动示例：

```python
import logging, os, tempfile
from pathlib import Path
from dotenv import load_dotenv

logging.disable(logging.CRITICAL)
with tempfile.TemporaryDirectory(prefix="native-tools-live-") as directory:
    root = Path(directory)
    os.environ.update({
        "DEER_MINI_DATABASE_PATH": str(root / "driver.sqlite3"),
        "DEER_MINI_DATA_ROOT": str(root / "users"),
        "RUN_NATIVE_TOOLS_LIVE_TEST": "1",
        "DEER_MINI_MEMORY_ENABLED": "false",
        "LANGSMITH_TRACING": "false",
        "LANGCHAIN_TRACING_V2": "false",
        "LANGSMITH_API_KEY": "",
    })
    load_dotenv("/home/pl/deer_mini/backend/.env", override=False)
    import pytest
    raise SystemExit(pytest.main([
        "tests/app/agents/test_native_tools_live.py", "-q", "--tb=short",
        "--basetemp", str(root / "pytest"),
    ]))
```

USTC 原有两项节点分别为 `tests/app/agents/test_lead_agent.py::test_agent_loop_with_live_model` 和 `tests/app/agents/test_bash_agent_loop_live.py::test_live_model_can_choose_real_docker_bash`，需要 RUN_LIVE_AGENT_TEST=1 与 RUN_DOCKER_SANDBOX_TEST=1；当前 tx 网络连接问题解决后才能复验。

## 失败历史与审查

保留了前置凭据未加载、长场景达到轮数上限、测试路径/cleaned类型断言等失败记录。部分早期未保存 trace 的请求计数明确标 unknown；有逐 Run trace 的记录按真实计数汇总。失败 Run 不改写为 success。

独立审查发现并修复验收记录跨 Run 覆盖、报告内容断言和历史 trace 汇总问题。独立复审结论：Code Quality PASS；默认 SiliconFlow 配置范围的 Spec Compliance PASS，限定该范围 APPROVE；包含 USTC 的完整范围 Spec Compliance FAIL、成功验收证据不足。USTC 外部连接失败仍保留，不视为已通过。审查没有授予合并或部署权限。
