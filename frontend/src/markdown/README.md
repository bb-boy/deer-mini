# DeerFlow Markdown 组件迁移

DeerMini 使用 DeerFlow 的实际组件链：

`MessageList → MarkdownContent → SafeMessageResponse → ClipboardSafeStreamdown → Streamdown`

- `components/MarkdownContent.tsx` 来自 DeerFlow `frontend/src/components/workspace/messages/markdown-content.tsx`。保留渐进显示、未闭合 Markdown、流式列表和代码块切换。
- `markdown/components.tsx` 来自 `frontend/src/core/streamdown/components.tsx`，保留消息包装、组件类型适配和 `SafeReasoningContent`。
- 思考区使用上游 `components/ai-elements/reasoning.tsx`，通过 `SafeReasoningContent → ReasoningContent → ClipboardSafeStreamdown` 渲染，使用禁用原始 HTML 的 `reasoningPlugins`。
- 工具过程使用上游 `chain-of-thought.tsx`，并保留配套 `shimmer.tsx`、`ui/collapsible.tsx` 和 `ui/badge.tsx`。`AssistantProcess` 按上游 `MessageGroup` 的顺序与旧步骤折叠逻辑适配 Mini 数据；工具原文在详情内查看。
- `components/ai-elements/streamdown.tsx` 来自同名上游文件，保留剪贴板兼容和单条消息的异常回退。
- `preprocess.ts`、`safe-children.ts`、`mermaid.ts`、`plugins.ts` 来自 `frontend/src/core/streamdown/`。正文使用 `streamdownPluginsWithoutRawHtml`，保留 GFM 表格、数学公式、代码高亮和 Mermaid。
- `clipboard.ts` 来自 `frontend/src/core/clipboard.ts`；`lib/utils.ts` 使用上游的 `cn()`。
- `components/MarkdownLink.tsx` 保留上游安全链接检查和普通链接显示，省略 Mini 没有接入的引用卡片及 `/mnt/` 文件路由。

依赖固定为上游相同的 Streamdown 2.5.0、代码插件 1.1.1、Mermaid 插件 1.0.2。表格复制、下载、全屏和代码复制、高亮控件由这些组件提供。

Mini 适配仅包括相对导入、中文控件文案、现有主题、内部标签名单和默认属性。样式通过 Tailwind Vite 插件生成，但不引入全页 Preflight 重置。全屏控件也继承当前主题。

SSE 接收层按 message_id 分别累积正文与思考，不再运行另一套正文文字缓冲；正文显示节奏交给 `MarkdownContent`。思考片段由 `reasoning.delta` 即时送达，完整内容仍随 Checkpoint 保存；界面收到文字不代表 Run 已成功。

思考开始时展开、结束后自动收起；手动选择优先，并在实时消息归入历史后保留。历史记录没有可靠的计时信息，因此显示“思考过程”，不编造耗时。`Reasoning` 对原版的适配包括关闭动画时机与手动选择保护，`Shimmer` 缓存动画组件以避免每次更新重建。消息列表监听高度变化保持底部跟随；用户向上查看历史时停止跟随。

用户输入和工具标准输出按原文显示，助手正文、工具前说明和思考区使用 Markdown。复制整条消息仍取保存的原文。上述处理只影响显示，不改写持久化内容。

上游 MIT 许可见 `DEERFLOW-LICENSE`。
