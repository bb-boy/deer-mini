# Tavily 搜索接入

接入后，可以在对话中说：“请搜索 Python asyncio 官方文档，给我标题和链接。”模型会真实调用搜索工具，读到返回的结果后再回答；页面会显示“搜索网页”的过程。

## 先看 DeerFlow 做了什么

远程源码 `/home/pl/sp/deer-flow/backend/packages/harness/deerflow/community/tavily/tools.py` 中的 `web_search_tool(query)` 接收搜索词，通过 Tavily 搜索，然后把返回的 `content` 改名为 `snippet`。每条结果保留 `title`、`url`、`snippet`。

Mini 保留工具调用、真实搜索和结果交回模型的行为。工具适配成已有的 `Tool` 接口，用官方异步客户端等待网络；不需要为了调用 Tavily 更换 Agent Runtime 或引入框架工具包装。

```mermaid
flowchart LR
    A[用户消息] --> B[模型选择 web_search]
    B --> C[ToolExecutor 找到 WebSearchTool]
    C --> D[Tavily 搜索]
    D --> E[标题、链接、摘要]
    E --> F[工具消息与 Checkpoint]
    F --> G[模型根据来源回答]
```

## WebSearchTool 的职责

- **功能**：`backend/app/tools/web_search.py` 中的 `WebSearchTool` 是搜索适配器。像翻译员一样，它把模型的调用转成 Tavily 请求，再把 Tavily 的结果转成现有 Agent 能处理的消息。
- **输入**：构造时的 `api_key` 是后端访问 Tavily 的凭证；执行时的 `call.id` 标识这一次工具调用，`call.name` 是 `web_search`，`call.arguments["query"]` 是搜索词。`context` 携带当前用户、对话、执行编号、工作目录以及事件和状态保存入口；本工具遵守统一接口，但不直接使用这些入口。
- **输出**：`ToolResult` 的 `tool_call_id` 对应原调用，`name` 是 `web_search`；成功时 `content` 是最多 5 条搜索结果组成的 JSON 文字，没有结果时是 `[]`；失败时是错误说明，且 `is_error=True`。
- **副作用**：请求 Tavily 并使用搜索额度。`__init__` 只保存密钥，`execute` 才发起网络请求。工具自己不写 SQLite、不操作工作目录，也不直接推送页面。

搜索结果的字段含义：

| 字段 | 内容 | 用途 |
| --- | --- | --- |
| `title` | 网页标题 | 帮助判断网页主题 |
| `url` | 网页地址 | 给回答提供可点击的来源 |
| `snippet` | Tavily 返回的搜索摘要 | 让模型理解搜索结果的大致内容 |

这里得到的是搜索摘要。读取某个网页的正文由已接入的 `web_fetch` 完成，详见同目录的 `TAVILY_WEB_FETCH.md`。

## 上下游怎么连接

`RunCoordinator._build_tool_registry()` 从后端环境读取 `TAVILY_API_KEY`；有非空密钥时，把 `WebSearchTool` 放进工具注册表。注册表就像菜单：模型只能看到工具名称、说明和 `query` 参数，看不到密钥。

模型决定搜索后，已有的 `ToolExecutor.execute()` 调用工具，并把结果转成 `Message(role="tool")`。`LeadAgent._run_loop()` 保存这条工具消息，再把它放进下一次模型请求。现有 Runtime 负责 SQLite 快照、Run 状态与实时事件收尾。

`apply_prompt_template()` 新增 `web_search_enabled` 输入，表示本次执行是否注册了搜索。输出仍是系统提示词；它会读取原有 `SOUL.md` 并在内存中拼装文字，不写数据库。启用搜索时才拼入 `citations.py` 中摘自 DeerFlow 原版的引用规则，要求模型在使用搜索信息时提供来源链接。工具描述也保留 DeerFlow 的英文原文。

## 为什么使用异步请求

当 Tavily 花 3 秒搜索时，`await client.search(...)` 会暂停当前搜索，腾出执行机会让后端推送页面消息或响应取消操作。返回后再继续处理结果。`async with` 会在成功、失败或取消时关闭连接。

单次搜索最多等待 30 秒，使用 basic 搜索；不请求 Tavily 代写答案或返回整页正文。超时、无效密钥、额度不足和服务故障会变成工具错误消息，交回模型处理。用户取消 Run 时，取消信号会继续交给 Runtime 收尾。供应商原始错误不写进对话，避免其中夹带请求凭证。

## 配置与运行

真实密钥只写入 `/home/pl/deer_mini/backend/.env` 的 `TAVILY_API_KEY`；该文件由 Git 忽略。仓库中的 `.env.example` 只保留空字段。修改配置后重启后端：

```bash
cd /home/pl/deer_mini/backend
.venv/bin/python -m app.main
```

在页面发送：“请调用 web_search 搜索 Python asyncio 官方文档，只给出一个结果的标题和链接。”应能看到“搜索网页”步骤、搜索参数、工具结果和带链接的回答。

## 验证

自动测试使用受控 HTTP 响应，不消耗真实搜索额度：

```bash
cd /home/pl/deer_mini/backend
.venv/bin/python -m pytest tests/app/tools/test_web_search.py tests/app/services/test_run_coordinator_tools.py tests/app/services/test_web_search_flow.py -q
```

测试覆盖搜索请求与结果转换、空结果、无效输入、密钥及额度错误、超时和取消、连接关闭，以及完整的“模型 → 搜索 → 模型 → SQLite → Stream”流程。真实联网验证另用隔离的临时数据库运行，避免给已有对话添加测试记录。

完成后用 `git status --short` 查看改动文件，再用 `git diff` 阅读具体修改。确认功能后，可以单独提交搜索接入相关文件；密钥不属于提交内容。
