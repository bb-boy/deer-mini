"""sandbox 包的入口：让其他模块方便地导入执行器和结果类型。

sandbox 可以理解为供 Agent 运行命令的独立工作间，本项目用 Docker 容器实现。
它解决的问题：Agent 需要真正运行脚本，并访问当前对话的资料和输出文件。

一条真实调用链：
用户发消息 → Runtime 登记本轮 → Agent 选择 bash → BashTool.execute()
→ ThreadSandboxManager.run() → DockerCommandRunner.run_in_container()
→ 命令输出交回 Agent → 上游继续保存状态、给浏览器返回结果。

阅读顺序见同目录 README.md；每个文件里的注释都使用“读取上传文件、生成结果”这个例子。
本文件集中导出一些名字；import app.sandbox 本身不会启动容器或执行 Bash。
"""

# 从 base.py 导入结果单 CommandResult，以及执行器的方法约定 CommandRunner。
from app.sandbox.base import CommandResult, CommandRunner
# 圆括号允许把一次导入拆成多行；下面每行是要从 docker_runner.py 取出的一个名字。
from app.sandbox.docker_runner import (
    DockerCommandRunner,  # 负责实际启动 Docker 命令、收集输出、处理超时和清理。
    DockerRunnerConfig,  # 保存镜像、内存限制、超时等配置。
    SandboxCleanupError,  # 表示“尝试清理后，仍无法确认容器已删除”的异常类型。
    load_docker_runner_from_env,  # 从进程环境变量读取配置，按开关创建执行器。
)

# __all__ 主要规定 from app.sandbox import * 会导入哪些名字。
# 它不是访问权限限制；管理器仍可从 app.sandbox.manager 显式导入。
__all__ = [
    "CommandResult",  # 对外提供命令结果的统一格式。
    "CommandRunner",  # 对外提供执行命令的方法约定。
    "DockerCommandRunner",  # 对外提供 Docker 执行器。
    "DockerRunnerConfig",  # 对外提供创建执行器时使用的配置类型。
    "SandboxCleanupError",  # 调用方需要时可以专门识别容器清理失败。
    "load_docker_runner_from_env",  # 对外提供按环境配置创建执行器的函数。
]
