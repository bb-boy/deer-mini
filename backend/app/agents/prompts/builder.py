from app.agents.prompts.template import SYSTEM_PROMPT_TEMPLATE

from pathlib import Path
from app.agents.prompts.soul import get_agent_soul, load_agent_soul


# 从当前源码位置定位 backend，避免启动目录不同导致找错文件。
SOUL_PATH = Path(__file__).resolve().parents[3] / "SOUL.md"


def apply_prompt_template(*, agent_name: str | None = None) -> str:
    """把助手名称填入当前已经迁移的提示词模板。"""
    return SYSTEM_PROMPT_TEMPLATE.format(
        # 沿用 DeerFlow 的规则：没有名称时使用默认名称。
        agent_name=agent_name or "deer_mini",
        soul=get_agent_soul(SOUL_PATH),
    )
