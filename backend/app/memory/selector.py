"""选择模型仅查看编号、名称和描述；正文由后端按快照映射读取。"""

import json
import logging
from collections.abc import Callable

from app.domain.threads import ThreadState
from app.memory.model_calls import call_memory_model, parse_json_object
from app.memory.prompts import SELECTION_PROMPT
from app.memory.store import MemoryStore
from app.model.base import ChatModel
from app.runtime.async_io import run_sync
from app.runtime.context import RuntimeContext
from app.runtime.errors import StatePersistenceError


logger = logging.getLogger(__name__)
INDEX_CHAR_BUDGET = 32_000
BODY_CHAR_BUDGET = 24_000


class MemorySelector:
    """每个 Run 选择一次相关记忆；普通失败降级，关键异常和取消继续传播。"""

    def __init__(self, store: MemoryStore, model_factory: Callable[[], ChatModel],
                 *, timeout: float = 15.0) -> None:
        self.store, self.model_factory, self.timeout = store, model_factory, timeout

    async def select(self, state: ThreadState, context: RuntimeContext) -> str:
        """返回本轮要注入的记忆 JSON；无相关信息或普通失败返回空字符串。"""
        try:
            records = await run_sync(self.store.catalog, state.user_id)
            candidates, mapping = [], []
            used = 2
            for record in records:
                candidate = {"index": len(candidates) + 1, "name": record.name,
                             "description": record.description}
                size = len(json.dumps(candidate, ensure_ascii=False)) + 2
                if used + size > INDEX_CHAR_BUDGET:
                    break
                candidates.append(candidate)
                mapping.append(record.id)
                used += size
            if not candidates:
                return ""
            recent = [m for m in state.messages if m.role in {"user", "assistant"}][-8:]
            users = [m for m in state.messages if m.role == "user"]
            payload = {"candidates": candidates,
                "current_user": users[-1].content[-16_000:] if users else "",
                "conversation": [
                {"role": m.role, "content": m.content[-4000:]} for m in recent
            ]}
            response = await call_memory_model(self.model_factory, prompt=SELECTION_PROMPT,
                payload=payload, state=state, run_id=context.run_id, timeout=self.timeout)
            result = parse_json_object(response.content)
            if set(result) != {"memory_indices"}:
                raise ValueError("记忆选择返回字段无效")
            indices = result["memory_indices"]
            if not isinstance(indices, list) or len(indices) > 5 or any(
                type(index) is not int or not 1 <= index <= len(mapping) for index in indices
            ):
                raise ValueError("记忆选择编号无效")
            selected = []
            used = 2
            for index in dict.fromkeys(indices):
                record = await run_sync(self.store.read, state.user_id, mapping[index - 1])
                if record is None:
                    continue
                item = {"name": record.name, "description": record.description,
                        "type": record.type, "content": record.content}
                size = len(json.dumps(item, ensure_ascii=False)) + 2
                if used + size <= BODY_CHAR_BUDGET:
                    selected.append(item)
                    used += size
            return json.dumps(selected, ensure_ascii=False) if selected else ""
        except StatePersistenceError:
            raise
        except Exception as error:
            logger.warning("记忆选择跳过 thread=%s run=%s reason=%s",
                           context.thread_id, context.run_id, type(error).__name__)
            return ""
