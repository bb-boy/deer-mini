from app.agents.prompts.template import SYSTEM_PROMPT_TEMPLATE


def apply_prompt_template(*, agent_name: str | None = None) -> str:
    """把助手名称填入当前已经迁移的提示词模板。"""
    return SYSTEM_PROMPT_TEMPLATE.format(
        # 沿用 DeerFlow 的规则：没有名称时使用默认名称。
        agent_name=agent_name or "deer_mini",
    )
