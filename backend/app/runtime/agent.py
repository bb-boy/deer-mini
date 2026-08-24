

from typing import Protocol

from app.domain.threads import ThreadState
from app.runtime.context import RuntimeContext



class Agent(Protocol):
    """
    Agent 是一个协议，定义了 Agent 的行为和属性。
    """

    async def run(self, 
                state: ThreadState,
                context: RuntimeContext) -> ThreadState:
       
        ...