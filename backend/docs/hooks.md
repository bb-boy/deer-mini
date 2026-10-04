# deer-mini 的 hook 与中间件

代码位置：tx 上的 /home/pl/deer_mini/backend。

## 1. 四层各自负责什么

| 层 | 文件 | 职责 |
| --- | --- | --- |
| Runtime | app/runtime/agent_runtime.py | Run 状态、超时、取消、保存状态 |
| Agent Loop | app/agents/lead_agent.py、tool_calls.py | 在固定位置调用 hook，安排模型和工具轮次 |
| Manager | app/agents/middleware.py | 遍历中间件列表，构造嵌套的 call_next |
| 具体中间件 | app/agents/*_middleware.py | 实现日志、上下文、错误处理等行为 |

模型不会选择中间件。程序构建 Agent 时决定列表；运行到 hook 位置时，Manager 调用列表成员的对应方法。没覆盖某个方法的中间件使用基类默认实现。

## 2. 一次运行的顺序

以下假设列表是 [A, B]：

```text
before_agent：A → B                         整次运行一次
┌ 每轮模型请求
│ before_model：A → B
│ prepare_model_messages：A → B             整理这次请求的消息
│ wrap_model_call：
│   A 进入 → B 进入 → model.chat → B 返回 → A 返回
│ 模型消息加入 state.messages
│ after_model：B → A
│ 保存状态，发布 message.complete
│
│ 如果模型请求了工具，对每个工具：
│   发布 tool.start
│   wrap_tool_call：
│     A 进入 → B 进入 → ToolExecutor.execute → B 返回 → A 返回
│   工具消息加入 state.messages
│   after_tool：B → A
│   保存状态
│ 本轮结果到齐（或中断记录补齐）后：
│   finalize_tool_results：A → B
│   如有变化，保存状态
│   按调用顺序发布 tool.end
│
│ 工具结果交给下一轮模型；after_model 也可要求继续一轮
└ 直到最终回答或达到轮数上限
after_agent：B → A                         正常结束或异常收尾
关闭模型客户端
```

普通工具按顺序执行；连续 task 调用作为一批并发执行，全部返回后按原调用顺序保存父工具消息。每个工具调用都会经过自己的 wrap_tool_call 链。

## 3. 当前提供的 hook

| Hook | 调用时机 | 返回值 |
| --- | --- | --- |
| before_agent | 整个 Agent 循环开始前 | None |
| before_model | 每轮准备模型请求前 | None |
| prepare_model_messages | 构建模型消息后、请求前 | 这次请求使用的消息列表 |
| wrap_model_call | 包住模型调用 | Message |
| after_model | 模型消息加入 state 后 | True 可要求继续；否则 None/False |
| wrap_tool_call | 包住单次工具执行 | Message |
| after_tool | 工具消息加入 state 后 | None |
| finalize_tool_results | 本轮正常完成或中断消息补齐后 | True 表示结果变化，需要保存 |
| after_agent | Agent 结束、成功或异常时 | None |

wrap 的“前、后”是方法里 await call_next() 前后的代码。这里没有独立 before_tool：工具执行前的检查放在 wrap_tool_call 的 call_next 前；对原始结果的处理放在它后面。after_tool 用于结果已加入 state 后的处理。

prepare_model_messages、after_tool 和 finalize_tool_results 是 deer-mini 保留的扩展 hook。整体采用 DeerFlow 的“固定生命周期位置 + 中间件列表 + 嵌套包装”方式；本项目用 asyncio Manager 调度。

## 4. 中间件如何注册

公共入口：app/agents/middleware_stack.py 的 build_runtime_middlewares()。

```python
[
    ModelCallLoggingMiddleware(),     # 模型边界日志
    ToolCallLoggingMiddleware(),      # 工具边界日志
    ToolResultStorageMiddleware(),    # 大结果全文落盘与预算
    *extra_middlewares,               # 这个 Agent 的专用行为
    ToolErrorHandlingMiddleware(),    # 最内层的工具异常转换
]
```

每次调用构建函数都会创建新的公共中间件实例，避免不同 Agent 共享可变状态。

主 Agent 在 RunCoordinator 中传入：

```python
build_runtime_middlewares([
    WorkspaceContextMiddleware(registry),
    TodoMiddleware(todo_tool),
    SubagentMiddleware(...),
])
```

子 Agent 在 SubagentExecutor 中传入：

```python
build_runtime_middlewares([
    WorkspaceContextMiddleware(registry),
])
```

两者复用同一个 LeadAgent 循环，所以都会经过模型和工具 hook。子 Agent 的工具表单独过滤，不提供 task 等禁用工具；工具可见范围由 ToolRegistry 决定。

直接创建 LeadAgent 时，middlewares=None 使用公共默认列表；显式传入列表（包括空列表）则使用你提供的完整列表。若希望保留公共错误策略，使用 build_runtime_middlewares([你的中间件])。

## 5. call_next 到底是什么

它是 Manager 为当前中间件创建的异步函数。A 收到的函数会进入 B；B 收到的函数会进入真正的模型或工具。

```python
async def wrap_model_call(self, state, context, call_next):
    # 这里运行在模型请求前
    message = await call_next()
    # 这里运行在模型成功返回后
    return message
```

正常中间件调用一次 call_next 并返回结果。也可以直接返回 Message，提前结束这条链。finally 中的代码在成功、异常和取消时都会执行；普通“调用后”代码在抛异常时不会执行。

## 6. 新增中间件示例

下面同时实现两个 hook：每轮模型前记录消息数量，模型返回后记录工具调用数量。日志不包含消息内容。

```python
import logging
from app.agents.middleware import AgentMiddleware

logger = logging.getLogger("uvicorn.error")

class ConversationStatsMiddleware(AgentMiddleware):
    async def before_model(self, state, context):
        logger.info("消息数量=%d", len(state.messages))

    async def after_model(self, state, context, message):
        logger.info("工具调用数量=%d", len(message.tool_calls))
```

把 ConversationStatsMiddleware() 加入 build_runtime_middlewares 的公共列表，就会作用于主、子 Agent。只加入 RunCoordinator 的 extra_middlewares，则只作用于主 Agent。

有状态的中间件应为每个 Agent 创建新实例。修改模型上下文使用 prepare_model_messages 返回新列表；临时提醒不要追加到持久化历史。需要取消和异常时仍执行的清理，可实现 after_agent 或在 wrap 中使用 finally。

## 7. 新增工具时如何划分职责

1. 工具定义参数 schema，供模型了解参数。
2. 工具执行入口校验实际参数。schema 展示给模型不等于已经验证模型返回值。
3. 文件工具负责路径解析、访问范围和读写；网页工具负责 URL 等自己的检查。
4. ToolExecutor 查注册表、执行工具、把 ToolResult 转成 Message。
5. 公共日志、异常转换等策略由外层中间件统一处理。

新工具注册进对应 Agent 的 ToolRegistry 后，正常工具调用会自动经过该 Agent 已注册的 wrap_tool_call 链，无须在工具里逐个调用中间件。

## 8. 错误的边界

- 工具主动返回 ToolResult(is_error=True)：保留错误标记和内容。
- 普通工具异常，如参数错误、文件不存在、未知工具：ToolErrorHandlingMiddleware 返回同一 tool_call_id 的错误消息，让模型决定是否调整参数。
- 中间件自己抛出的异常：继续向外传播，最内层工具异常处理中间件不会捕获位于它外层的异常。
- StatePersistenceError：继续传播，Runtime 停止本次 Run。
- asyncio.CancelledError：继续传播，执行取消及状态收尾。
- 模型请求异常：日志中间件记录异常类型后原样抛出，Runtime 处理失败。现有模型 SDK 的传输重试保持原有行为。

工具错误不会自动固定重试三次。模型是否改参数再调用由后续模型响应决定，Agent 的轮数上限仍生效。

Message.is_error 会写入 Checkpoint、流式完整消息和状态查询响应。旧消息没有该字段时默认 False；发送给模型供应商时仍由适配器生成兼容字段，错误内容通过工具消息正文提供。

## 9. 验证用例

- tests/app/agents/test_hook_lifecycle.py：真实循环的所有 hook 顺序、嵌套返回、提前返回、模型异常与取消。
- tests/app/agents/test_runtime_middleware_stack.py：公共列表、日志不泄露正文、工具错误和致命错误边界。
- tests/app/domain/test_message_error_state.py：错误标记、旧格式兼容、API 响应和执行器转换。
- tests/app/services/test_task_flow.py：Coordinator → 主模型 → task → 子模型/工具 → 主模型，验证主子请求都进入日志中间件。

测试使用模拟模型、临时文件与数据库；真实供应商连接是独立的验证项目。

## 10. 大工具结果

公共 ToolResultStorageMiddleware 对主、子 Agent 的所有已注册工具生效。wrap_tool_call 处理单结果超过 50,000 字符；after_tool 在本轮结果到齐时处理 200,000 字符合计预算；finalize_tool_results 也检查取消后补齐的结果。before_model 检查恢复的旧历史。

全文确认写入磁盘后，消息才换成预览和文件引用。tool.end 在整轮定稿后发布，避免前面已发布的全文与后来落盘的结果不一致。具体规则及读回示例见 [tool-results.md](tool-results.md)。
