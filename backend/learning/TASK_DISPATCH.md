# 主 Agent 怎样真正使用子 Agent

这一部分让主 Agent 能把独立工作交给子 Agent，收到真实结果后继续回答。例如：“分别阅读两个报告，再比较结论。”现在两份报告可以由两个子 Agent 同时读取，各自使用真实工具，主 Agent 最后综合结果。

本步已接入远程 `/home/pl/deer_mini/backend`。运行主线是：

```text
用户消息 → 原有 Run / Runtime → 主模型返回多个 task 调用
→ ToolExecutor → TaskTool → SubagentDispatcher
→ SubagentExecutor → 子模型 ↔ read_file / 已启用的其他工具
→ 子任务结果保存 → 主对话收到对应的工具消息 → 主模型继续回答
```

## 先对应 DeerFlow 的真实源码

这些路径相对于 `/home/pl/sp/deer-flow/backend/packages/harness/deerflow/`：

| DeerFlow 位置 | 解决的问题 | Mini 保留的行为 |
| --- | --- | --- |
| `tools/builtins/task_tool.py` 的 `task_tool()` | 给主模型一个委派入口，并补齐后台上下文 | 模型只填任务参数，后台确定用户、对话、Run 和工作区；结果关联原工具调用 |
| `subagents/executor.py` 的 `execute_async()` | 调度独立的子 Agent 执行并跟踪结果 | 每项工作使用独立模型客户端和消息，支持取消、超时、失败与最终结果 |
| `agents/middlewares/subagent_limit_middleware.py` | 防止一次任务过度委派 | 同时运行数量和当前 Run 的累计任务数都有上限 |
| `agents/lead_agent/prompt.py` 的 `_build_subagent_section()` | 让模型判断何时委派、避免依赖和文件冲突、综合结果 | 固定规则直接摘录原版，删除未实现的能力相关段落或句子 |

DeerFlow 的调度部分使用线程池和单独线程中的事件循环。Mini 的模型与工具本来已经通过异步接口执行，所以用现有事件循环中的 TaskGroup 和 Semaphore 承担调度。独立子消息、并发上限、生命周期、结果回到主模型这些核心行为仍保留。

有一处明确的适配：DeerFlow 会裁掉超出本轮上限的调用；Mini 将累计额度内的额外工作单保存为 pending 并排队，累计额度耗尽则返回工具错误。每个原始工具调用都会得到对应回复，因此提示词中删除了“超额调用被丢弃”的句子。

## 1. TaskTool：接收模型的工作要求

可以把它理解为“接单窗口”。它先检查模型有没有把工作说清楚，再把工作交给调度器。

- **功能**：注册到主 Agent 的统一工具表，让模型真正能调用 `task`。
- **输入**：`call.id` 是模型这次工具调用的编号；`call.arguments.description` 是展示用的简短名称；`prompt` 是子 Agent 需要知道的任务、文件路径、背景和预期结果；`subagent_type` 当前只能是 `general-purpose`。`context.user_id` 指明谁的任务，`thread_id` 指明哪段对话，`run_id` 指明哪次执行，`workspace_path` 指明可操作的文件目录；这些身份由后台提供。
- **输出**：一份 `ToolResult`，其中 `tool_call_id` 仍对应原调用编号。统一的 ToolExecutor 会把它转换成主模型能接收的工具消息。
- **副作用**：TaskTool 本身校验参数并创建工作单；通过 Dispatcher 启动执行、保存状态和发送事件。

主模型提供的参数形如：

```json
{
  "description": "读取报告 A",
  "prompt": "用 read_file 读取 uploads/A.txt，提取三个主要结论，返回对应原文依据。",
  "subagent_type": "general-purpose"
}
```

子 Agent 不会自动得到主对话的全部历史。因此，主模型需要把必要背景写进 `prompt`。两者仍使用同一个 Thread 工作区，独立的是消息和模型客户端。

实现位置：`app/tools/task.py`。

## 2. SubagentMiddleware：等状态恢复后再接线

创建工具表时，Runtime 还没有恢复这次对话的完整状态。这个钩子负责等 Runtime 准备好后，再把 TaskTool 接到正确的父状态上。

- **功能**：在主 Agent 开始、第一次请求模型之前，构造本 Run 专属的 Dispatcher。
- **输入**：构造时提供 `task_tool`、`tool_registry`、`model_factory`；前两项分别是委派入口与真实工具表，最后一项是“按需创建新模型客户端”的方法。`max_concurrent`、`max_total` 是同时执行数量和累计额度；`timeout_seconds`、`max_tool_rounds` 限制子任务耗时和工具轮数；`thinking_enabled`、`reasoning_effort` 沿用父模型的推理设置。`before_agent(state, context)` 收到已恢复的父状态与本次运行上下文。
- **输出**：TaskTool 绑定这个 Run 的 Dispatcher。
- **副作用**：此时只建立内存对象和联系，子模型会等实际拿到执行名额时才创建。

实现位置：`app/subagents/middleware.py`；接线位置：`app/services/run_coordinator.py`。

## 3. SubagentDispatcher：三张座位，最多六张工作单

把它想成一个有三张座位的工作室。工作单先登记，只有拿到座位的人开始干活。做完一项，就把座位还给排队的人。

- **功能**：登记 pending 工作单、限制同时执行数量、检查累计额度、等待结果。
- **输入**：`parent_state` 保存父消息和全部子工作单；`context` 提供当前身份、`record_event` 事件入口与 `save_checkpoint` 保存入口；`tool_registry` 提供可继承的真实工具；`model_factory` 按需创建独立模型；两个限额、子任务超时、工具轮数和推理设置与上面一节含义相同。`execute(candidate, context)` 接收一张尚未执行的工作单，并再次核对它是否属于本 Run。
- **输出**：已完成或失败的工具结果；排队与执行期间也会更新工作单状态。
- **副作用**：通过受控入口写 Checkpoint、发送 `subagent.status` 事件，再调用真实 SubagentExecutor。

默认值在 `app/subagents/limits.py`：

| 限制 | 含义 |
| --- | --- |
| 同时 3 个 | 第 4 个已经登记的子任务先等待，有名额后再启动 |
| 每个 Run 累计 6 个 | 跨多轮工具调用累计；失败和取消也占用额度 |
| 子执行默认 120 秒 | 从拿到执行名额后开始计时；排队时间仍受父 Run 总超时约束 |

上一次 Run 的工作单会保留供查看，但不消耗新 Run 的六个额度。这些限额是**每个父 Run 的限额**，不是整个服务所有对话合计的限额。

Semaphore 是 Python 提供的“名额计数器”。下面这两行接收已登记的 `task`，返回它的真实结果；进入时取一个名额，退出时自动归还，失败时也会归还：

```python
async with self._semaphore:
    return await self._executor.execute(task)
```

`await` 表示“等待时把执行机会交给其他异步工作”。它不会把整个服务停住，所以另外两个子 Agent 仍可以等待网络或处理工具结果。

## 4. 工具循环：真正启动一批，再交回结果

只有名额计数器还不够。如果主 Agent 仍然“执行 A、等 A 完成、再执行 B”，它们仍然是顺序执行。

- **功能**：`execute_tool_calls()` 将连续的 `task` 调用同时启动，普通工具仍依次执行。
- **输入**：`state` 是父对话；`context` 提供保存与事件入口；`calls` 是模型这一轮的完整工具调用列表；`round_number` 用来标记第几轮；`executor` 是统一 ToolExecutor；`middleware` 在工具结果加入父消息后执行钩子。
- **输出**：按原调用顺序补齐父状态中的工具消息。
- **副作用**：调用真实工具、发出工具事件、保存完整状态；异常时等待同批任务收尾，再向 Runtime 交还异常。

TaskGroup 是 Python 的“一组共同管理的异步任务”。这一组必须全部退出，主 Agent 才会继续下一个阶段。下面的 `indices` 是本批调用在原列表里的位置；`invoke(index)` 经过统一 ToolExecutor 执行该调用；`commit(index)` 把结果加入父消息、运行钩子并保存 Checkpoint。

```python
async with asyncio.TaskGroup() as group:
    workers = [group.create_task(invoke(index)) for index in indices]

# 整批结束后，按模型原先的顺序保存对应结果。
for index in indices:
    await commit(index)
```

例如模型返回 `task A、task B、bash、task C`：A 和 B 可以同时执行；这批结束后才运行 bash；bash 完成后再开始 C。这样保留了普通文件操作的执行顺序。

实现位置：`app/agents/tool_calls.py`，由 `LeadAgent._run_loop()` 调用。

## 状态为什么不会互相覆盖

三个子 Agent 可以同时等待模型或工具，但“合并状态并保存完整父快照”需要排队。Dispatcher 登记工作单和 Executor 保存子消息，共用同一把锁。

子任务运行期间，父工具消息暂不追加。等整批结束后，再把结果按原调用顺序加入父消息。这样不会出现“子 A 保存了旧父状态，把刚加入的子 B 结果覆盖掉”的问题。

锁只保护登记与状态保存，不包住整个模型调用。否则会把并发又变成顺序执行。

## 失败和取消时保留什么

- 普通子模型失败：工作单保存为 failed，错误作为该次 task 的工具结果返回；其他子任务继续，主模型可以综合已有结果或在剩余额度内调整任务。
- 用户取消：正在执行的子任务收到取消；排队任务不再创建模型，也保存为 cancelled；已完成的结果仍保留。全部子任务退出后，父 Runtime 才归还运行环境。
- Checkpoint 保存失败：`StatePersistenceError` 穿过 TaskTool 和统一 ToolExecutor，由父 Runtime 确认 Run 失败；不能把关键状态缺失包装成普通工具文字后宣告成功。
- 进程中断留下工具消息空缺：下一轮会插入明确的“返回结果未确认”记录，再向模型发送新用户消息。不会自动重新执行可能已经产生副作用的工具，也不会只凭跨 Run 可能重复的调用编号猜测历史结果。

最后一项由 `repair_interrupted_tool_history(state)` 完成：输入恢复的消息，输出是否修补过；它只改内存消息，由 LeadAgent 调用 `save_checkpoint` 保存后再请求模型。

实时子事件与辅助日志沿用原 Runtime 的分工。文字可以立即发送，关键消息保存在 Checkpoint；辅助日志遇到 SQLite 锁竞争会有限重试，仍失败则告警。不能把“看到实时文字”或“Stream 已关闭”理解为整次 Run 已成功。

## 可以这样验证

在同一对话上传两份文件后发送：

> 请使用两个子 Agent，分别读取已上传的 A.txt 和 B.txt，各提取三个要点。两个任务独立执行，收到两者结果后，再由你比较结论。

模型接收到 task 工具说明和 DeerFlow 原版委派规则后，就能在原有 Agent Loop 中完成这条调用链。普通问题仍由模型判断是否值得委派。

子任务共享工作目录，因此委派范围必须独立。本步没有自动文件冲突检测；不要让两个子任务同时修改同一个文件。
