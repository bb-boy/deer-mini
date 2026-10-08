# 本机 MCP 与角色路由

本文件只改变子 Agent 的调用方式，模块需求、工作树、开发/审查职责、证据和交付规则继续遵循 SKILL.md。

## 配置

先确认 `PROJECT_ROOT`（原项目根目录），再调用 `module-agents.list_roles(project_root=PROJECT_ROOT)`。工具实际名称可能是带服务器前缀的 `...list_roles`。项目配置 `.agents/module-agents/roles.toml` 优先于用户全局配置；同名角色整项覆盖，未指定的角色为 inherit。本机默认开发者为 study/glm-5.3-flash，其他角色 inherit，用户可以自行修改。

角色支持 `mode`、`provider`、`model`、`effort`。custom 的 provider/model 必填；inherit 不填 provider/model。effort 可选，填写时传递指定强度；custom 省略时用模型默认，inherit 省略时跟随主 Agent。实际供应商不支持某个强度时报告错误，不擅自改模型。

供应商配置只引用 `base_url_env`、`api_key_env` 变量名，服务优先从 PROJECT_ROOT/.env 获取变量，缺失才回退进程环境。不要向工具参数或消息中传 API key。不要在终端回显 .env 或完整 provider 错误。

## 自定义角色

调用 `spawn_worker`，提供：

- `project_root`：PROJECT_ROOT 的绝对路径；从原项目读取配置与 .env。
- `cwd`：模块工作树 MODULE_WORKTREE 的绝对路径。非 Git 项目使用指定 MODULE_DIR。服务允许项目内目录或同一 Git 仓库的其他工作树。
- `role`：例如 module-developer。
- `instructions_file`：本 Skill 的对应角色 TOML 的绝对路径。
- `task`：任务、文档及版本、验收、文件所有权、测试命令、已有改动和授权范围；路径均用绝对路径。
- `request_id`：本次派发唯一标识；仅在同一个请求重试时复用。
- 可选 `sandbox`：read-only 或 workspace-write；角色 TOML 要求 read-only 时不可放宽。

保存返回的 `worker_id` 和 `run_id`。worker 创建后固定角色、供应商地址、模型、effort 与指令快照；修改配置仅影响新 worker。一个 worker 同时执行一轮任务。

有独立工作时继续处理；需要结果时用 `wait_workers(worker_ids, cursor, timeout_ms=30000)`，保存同一 worker 集合的返回游标。等待超时不取消任务，未完成时继续有界等待；不能因一次等待超时结束尚未完成的模块工作。它使用 app-server 事件唤醒，不要每秒调用 get_worker 轮询。它不保证主动唤醒已结束的主聊天。

开发完成后按 Skill 安排独立 reviewer。修复用 `send_task(worker_id, task, request_id)`，沿用开发者上下文；不要重新 spawn 原开发任务。BUSY 表示仍在执行，需要等待或明确取消；UNKNOWN/RECOVERY_REQUIRED 时先 get_worker 查看证据，再显式 recover_worker 对齐持久化状态，不自动重放工具。

取消使用 `cancel_run(worker_id, run_id)`；后续终止事件确认取消。关闭使用 close_worker，只允许已明确结束的 worker，保留本地记录。

`completed` 只代表模型一轮正常结束。开发者的 DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED，以及 reviewer 的审查结论仍按原 Skill 检查，不能把协议结束当作验收通过。主聊天返回摘要，必要时用 get_worker(log_cursor) 读取脱敏执行记录。

## 继承角色

使用原生子代理继承当前主 Agent 的供应商和模型。确保选用的原生角色文件没有固定模型，且全局子代理默认不造成不符合要求的覆盖；若无法保证，明确报告限制，不用猜测设置冒充继承。

如果角色配置填写 effort，使用原生工具支持的推理强度参数；未填写则沿用当前主 Agent 的强度。需要独立上下文时 `fork_turns="none"`，并提供角色指令与必要任务材料。原生角色/工具无法支持所需参数时先报告，不静默忽略。

reviewer 第一次不能继承开发者完整会话；复审复用原 reviewer。按原 Skill 在审查期间暂停相关写入。不要把原生 agent ID 传给 MCP worker 工具，也不要把 MCP worker ID 传给原生子代理工具。
