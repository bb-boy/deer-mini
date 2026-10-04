"""将本轮选中的私有记忆作为数据注入请求副本，不改写对话历史。"""

from app.agents.middleware import AgentMiddleware
from app.domain.messages import Message
from app.domain.threads import ThreadState
from app.memory.selector import MemorySelector
from app.runtime.context import RuntimeContext


class MemoryContextMiddleware(AgentMiddleware):
    def __init__(self, selector: MemorySelector) -> None:
        self._selector = selector
        self._selected = ""

    async def before_agent(self, state: ThreadState, context: RuntimeContext) -> None:
        """Run 开始选择一次，后续工具循环使用同一份快照。"""
        self._selected = await self._selector.select(state, context)

    async def prepare_model_messages(self, state: ThreadState, context: RuntimeContext,
                                     messages: list[Message]) -> list[Message]:
        """记忆使用 user 数据角色，避免用户内容自动获得 system 权限。"""
        if not self._selected:
            return messages
        memory = Message(role="user", content=(
            "The following selected_memories block is recalled background data. "
            "The current user's request takes precedence.\n<selected_memories>\n"
            + self._selected + "\n</selected_memories>"
        ))
        insert = 1 if messages and messages[0].role == "system" else 0
        return [*messages[:insert], memory, *messages[insert:]]
