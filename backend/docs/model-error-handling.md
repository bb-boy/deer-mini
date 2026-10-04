# 模型错误处理中间件

## 调用顺序

`LeadAgent.before_model` → 准备本轮消息 → `ModelCallLoggingMiddleware` →
`ModelErrorHandlingMiddleware.wrap_model_call` → 模型适配器 → SDK → 服务。

只有完整模型消息返回后才写入 state，执行 after_model、保存 checkpoint，
再执行模型选择的工具。重试调用 call_next，不重跑 before_model 或已执行的工具。
主 Agent 和子 Agent 使用独立中间件实例，SDK max_retries=0。

## 文件职责

- `app/model/errors.py`：按结构化错误代码优先分类；公开错误文字；Retry-After。
- `app/model/call_policy.py`：校验数字配置，读取 DEER_MINI_MODEL_* 环境变量。
- `app/agents/model_error_handling_middleware.py`：尝试、等待、限时、状态事件。
- `app/model/openai_compatible.py`：网络超时、关闭流、完成标记和工具 JSON。
- `app/agents/lead_agent.py`：记录是否向浏览器发送过正文或思考。
- `app/runtime/context.py`：同一 Run 共享的空响应补试额度。

## 分类与次数

次数包含第一次请求。max_attempts 是全局上限，具体分类可以缩小次数。

| 类型 | 总尝试次数上限 |
|---|---:|
| 普通 429、409、425、500、502、503、504、临时连接失败 | 3 |
| 408、读取超时、突发限流、无完成标记的断流 | 2 |
| 空响应 | 2，整个父 Run 的主/子 Agent 合计只能补试一次 |
| 额度不足、鉴权失败、400、404、其他拒绝、格式错误 | 1 |

错误代码/类型优先于 HTTP 状态码；例如 429 + insufficient_quota 不重试。
未知本地错误、关键状态保存失败、取消保持原异常向外传播。
不使用“错误文字有 quota 单词就属于欠费”的宽泛匹配。

## 等待和时间

普通错误：1s、2s，加入 0～0.5s 随机浮动，退避最大 8s。
突发限流：5s 起。Retry-After 支持秒数或 HTTP 日期，作为最短等待时间。
等待超过 60s，或超过剩余模型调用预算，立即失败，不能提前重试。

默认 connect/read/write/pool = 10/120/30/10s；单次尝试最多 300s；
本次模型调用全部尝试和等待合计最多 420s。父 Run/子任务既有总时间限制
继续生效，先到达者优先，不因重试延长。

具体环境变量见 `.env.example`。配置必须为有限正数；max_attempts 为 1～10
整数（分类上限仍生效）。可以为较慢模型提高 read/attempt/call 时限。

## 流式输出

一旦发过非空正文或思考，本次模型调用的后续错误不自动重新生成。
前端保留实时片段并标记生成中断；失败片段不进入模型对话历史。
下次请求只带已确认的完整消息，不把两次生成拼接起来。
没有 finish_reason 的断流视为不完整响应；length/content_filter/格式错误
不能成为成功工具决策；工具参数只有在流完整结束后才解析和执行。
成功、异常、取消都关闭上游流。

## 失败、日志与事件

最终已识别模型失败变成 ModelCallError，保留 reason/attempts/status_code，
公开文字不含上游错误正文。原异常保存在 __cause__，后台常规模型错误日志
不打印 cause traceback。日志只记录身份、类别、状态、次数、等待和安全请求 ID。

model.status 的 phase 为 attempt/retry/complete/failed；model.interrupted
标记失败片段。子任务事件加 subagent. 前缀和 task_id。
前端状态提示独立显示，不加入系统提示词或对话消息。

主 Agent 最终失败 → Runtime 保存 Run error、Thread idle。
子 Agent 最终失败 → Executor 保存子任务 failed → task 返回失败给主 Agent。
不自动重启子任务；取消和持久化失败继续遵循现有 Runtime 边界。

## 验证

定向测试覆盖错误分类、等待、额度、取消、流关闭、中断、子事件映射、
主子共享空响应额度、工具不重放，以及临时 SQLite 中的失败收尾。
测试使用假模型/假流，无付费供应商请求。测试目录中的旧真实模型工厂
测试需要另外设置环境，不在自动回归中执行。
