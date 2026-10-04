from app.agents.prompts.template import SYSTEM_PROMPT_TEMPLATE

from pathlib import Path
from app.agents.prompts.soul import get_agent_soul
# 从新文件取出文件规则字符串，供下面拼装提示词使用。
from app.agents.prompts.working_directory import WORKING_DIRECTORY_PROMPT
# 导入固定的任务处理原则。
from app.agents.prompts.thinking_style import THINKING_STYLE_PROMPT
from app.agents.prompts.citations import CITATIONS_PROMPT
from app.agents.prompts.subagent_system import build_subagent_section
from app.memory.prompts import MEMORY_SYSTEM_PROMPT

# 从当前源码位置定位 backend，避免启动目录不同导致找错文件。
SOUL_PATH = Path(__file__).resolve().parents[3] / "SOUL.md"


def apply_prompt_template(
    *, agent_name: str | None = None, web_search_enabled: bool = False,
    subagent_enabled: bool = False,
) -> str:
    """组装提示词；只有真实注册了搜索/委派工具，才加入相应规则。"""



    return SYSTEM_PROMPT_TEMPLATE.format(
        # 没有名称时使用默认名称。
        agent_name=agent_name or "deer_mini",

        #注入agent的职责
        soul=get_agent_soul(SOUL_PATH),

        #注入agent文件读取规则
        working_directory=WORKING_DIRECTORY_PROMPT,

        # 填入模板中的 {thinking_style}，说明如何使用信息并交付结果。
        thinking_style=THINKING_STYLE_PROMPT,

        # 搜索可用时，才要求模型按 DeerFlow 原版规则引用真实来源。
        citations=CITATIONS_PROMPT if web_search_enabled else "",
        subagent_system=build_subagent_section() if subagent_enabled else "",
    ) + "\n" + MEMORY_SYSTEM_PROMPT
