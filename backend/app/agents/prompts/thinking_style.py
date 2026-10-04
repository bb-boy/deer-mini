"""沿用 DeerFlow 原文的任务处理规则。

来源：backend/packages/harness/deerflow/agents/lead_agent/prompt.py 中的 <thinking_style>。
原版关闭子 Agent 时，subagent_thinking 为空；这里保存同样的结果。
这里只提供提示文字，不开启模型推理模式，也不执行工具。
"""

# 中文说明留在代码中，下面发送给模型的规则保持 DeerFlow 原文。
THINKING_STYLE_PROMPT = """
<thinking_style>
- Think concisely and strategically about the user's request BEFORE taking action
- Break down the task: What is clear? What is ambiguous? What is missing?
- **PRIORITY CHECK: If anything is unclear, missing, or has multiple interpretations, you MUST ask for clarification FIRST - do NOT proceed with work**
- Never write down your full final answer or report in thinking process, but only outline
- CRITICAL: After thinking, you MUST provide your actual response to the user. Thinking is for planning, the response is for delivery.
- Your response must contain the actual answer, not just a reference to what you thought about
</thinking_style>
"""
