# deer_mini Docker Bash Tool 设计

## 目标

让模型通过现有 Tool Registry / Tool Executor 调用 `bash`，在当前 Thread Workspace 中执行真实命令，并把输出作为工具消息返回模型继续推理。任意成功、失败、超时或取消路径都必须清理临时容器。

## 用户可见流程

```text
用户任务 → 模型选择 bash → tool.start → 临时容器执行
→ tool.end → Checkpoint → 工具结果返回模型 → 最终回答
```

## 架构

- `CommandResult` 表示输出、退出码、超时与截断状态。
- `CommandRunner` 隔离工具与具体执行环境。
- `DockerCommandRunner` 每次调用执行一个 `docker run --rm`，只挂载当前 Workspace。
- `BashTool` 校验模型参数、调用 Runner，并格式化 `ToolResult`。
- `RunCoordinator` 在显式启用 Bash 时注册工具，默认不向模型暴露。

## 容器边界

- `--network none`
- `--read-only`
- `--tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m`
- `--cap-drop ALL`
- `--security-opt no-new-privileges`
- 内存 512 MiB（同时限制 memory+swap）、CPU 1、PID 64
- 仅挂载当前 Workspace 到 `/workspace`；路径按 Docker mount 的 CSV 规则引用和转义
- 仅注入 `HOME`、`LANG`、`PYTHONUNBUFFERED`
- 使用 Workspace 所有者 UID/GID

## 生命周期

Runner 为每次调用生成唯一 `deer-mini-...` 容器名。正常结束依靠 `--rm` 删除；超时或协程取消时先终止并回收本地 `docker run` 客户端，再执行 `docker rm -f <name>`。这个顺序避免“停止命令先到、容器随后才创建”的竞态；如果删除失败，会报告清理错误而不声称容器已停止。输出持续排空但只保留固定头尾，避免无限输出占满内存。

## 配置

Bash 默认关闭。设置 `DEER_MINI_BASH_ENABLED=true` 后注册。镜像、超时、输出大小、内存、CPU、PID 和网络均可由环境变量覆盖，非法配置在应用启动时失败。

## 简化边界

第一版不实现 DeerFlow 的持久 Sandbox、温热池、多 Provider、远程文件同步、密钥注入或 Redis 所有权。`read_file` 继续由后端安全读取共享 Workspace。

## 验证

- 单元测试覆盖参数、输出、非零退出、截断、超时、启动取消、清理失败与路径边界。
- 注册测试覆盖默认关闭和显式启用。
- 可选真实 Docker 测试写入 Workspace、确认容器清理，并验证超时后无残留容器。
- 现有 Agent / Runtime / API 回归必须通过。
