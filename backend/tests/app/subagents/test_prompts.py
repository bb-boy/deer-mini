"""固定提示词保留上游约束，并且不会宣传未注册的工具。"""

import pytest

from app.subagents.prompts import build_subagent_prompt


@pytest.mark.parametrize("tools", [[], ["read_file"], ["read_file", "web_fetch"], ["read_file", "bash", "web_search", "web_fetch"]])
def test_prompt_only_describes_supported_capabilities(tools):
    prompt = build_subagent_prompt(tools)
    assert "You are a general-purpose subagent working on a delegated task." in prompt
    assert "You must NEVER attempt to call `task` or dispatch further subagents." in prompt
    assert "Do NOT ask for clarification - work with the information provided" in prompt
    assert "str_replace" not in prompt and "write_file" not in prompt
    assert "file_editing_workflow" not in prompt and "custom mounts" not in prompt
    if "bash" not in tools:
        assert "bash" not in prompt and "shell commands" not in prompt
    if not {"web_fetch", "web_search"}.intersection(tools):
        assert "[citation:" not in prompt
    else:
        assert "5. Citations: Use `[citation:Title](URL)` format for external sources" in prompt


def test_full_registered_tool_set_preserves_deerflow_tool_sentence():
    prompt = build_subagent_prompt(["read_file", "bash", "web_search", "web_fetch"])
    assert (
        "Complete your delegated work directly using `bash`, `web_search`, `web_fetch`,\n"
        "`read_file`, and other available tools."
    ) in prompt
