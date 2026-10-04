"""使用 DeerFlow 的通用子 Agent 提示词，只删除当前不支持的整段或整句。

来源：deerflow/subagents/builtins/general_purpose.py 的 system_prompt。
输入：本次子 Agent 实际拥有的工具名。
输出：给模型的系统提示词；保留部分不改写措辞。
副作用：无；不会读取配置、调用模型或保存状态。
"""

from collections.abc import Collection


# 原版的 file_editing_workflow 和自定义挂载说明不适用于当前 Mini，故不加入。
GENERAL_PURPOSE_PROMPT = """You are a general-purpose subagent working on a delegated task. Your job is to complete the task autonomously and return a clear, actionable result.

<guidelines>
- Focus on completing the delegated task efficiently
- Use available tools as needed to accomplish the goal
- Think step by step but act decisively
- If you encounter issues, explain them clearly in your response
- Return a concise summary of what you accomplished
- Do NOT ask for clarification - work with the information provided
</guidelines>

<tool_restrictions>
You are a subagent - the `task` tool is NOT available to you.
You must NEVER attempt to call `task` or dispatch further subagents.
Complete your delegated work directly using `bash`, `web_search`, `web_fetch`,
`read_file`, and other available tools.
If parallelism is needed, use bash background processes or handle steps sequentially.
</tool_restrictions>

<output_format>
When you complete the task, provide:
1. A brief summary of what was accomplished
2. Key findings or results
3. Any relevant file paths, data, or artifacts created
4. Issues encountered (if any)
5. Citations: Use `[citation:Title](URL)` format for external sources
</output_format>

<working_directory>
You have access to the same sandbox environment as the parent agent:
- User uploads: `/mnt/user-data/uploads`
- User workspace: `/mnt/user-data/workspace`
- Output files: `/mnt/user-data/outputs`
- Treat `/mnt/user-data/workspace` as the default working directory for coding and file IO
- Prefer relative paths from the workspace, such as `hello.txt`, `../uploads/input.csv`, and `../outputs/result.md`, when writing scripts or shell commands
</working_directory>
"""


def build_subagent_prompt(tool_names: Collection[str]) -> str:
    """输入真实可用工具名，返回裁剪后的原文；只进行内存中的字符串处理。"""
    prompt = GENERAL_PURPOSE_PROMPT
    if not {"bash", "web_search", "web_fetch", "read_file"}.issubset(tool_names):
        # 不改写工具清单；删掉整句，可用工具由真实 tools 参数和上下文提供。
        prompt = prompt.replace(
            "Complete your delegated work directly using `bash`, `web_search`, `web_fetch`,\n"
            "`read_file`, and other available tools.\n", "",
        )
    if "bash" not in tool_names:
        prompt = prompt.replace(
            "If parallelism is needed, use bash background processes or handle steps sequentially.\n", "",
        ).replace(
            "- Prefer relative paths from the workspace, such as `hello.txt`, `../uploads/input.csv`, and `../outputs/result.md`, when writing scripts or shell commands\n", "",
        )
    if not {"web_search", "web_fetch"}.intersection(tool_names):
        prompt = prompt.replace(
            "5. Citations: Use `[citation:Title](URL)` format for external sources\n", "",
        )
    return prompt
