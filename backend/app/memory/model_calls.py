"""记忆内部模型调用：独立重试预算、有界超时，不向主回答流输出内容。"""

import asyncio
import json
import logging
import math
from collections.abc import Callable

from app.agents.model_error_handling_middleware import ModelErrorHandlingMiddleware
from app.domain.messages import Message
from app.domain.threads import ThreadState
from app.model.base import ChatModel
from app.model.errors import ModelCallProgress, model_call_progress
from app.model.lifecycle import close_chat_model
from app.runtime.context import RuntimeContext


logger = logging.getLogger(__name__)


async def call_memory_model(factory: Callable[[], ChatModel], *, prompt: str,
                            payload: dict, state: ThreadState, run_id: str,
                            timeout: float) -> Message:
    """输入结构化数据并返回完整消息；只重试本次无工具的内部请求。

    调用使用独立客户端和重试预算。取消/错误原样传播，始终有界释放客户端。
    不使用主 Run 的流回调，不打印可能包含私有记忆的异常正文。
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("记忆调用 timeout 必须为正的有限数字")

    async def ignore_event(*args, **kwargs):
        return None

    async def forbidden_save(*args, **kwargs):
        raise RuntimeError("记忆模型不能保存 Thread checkpoint")

    context = RuntimeContext(user_id=state.user_id, thread_id=state.thread_id,
                             run_id=run_id, workspace_path=state.workspace_path,
                             record_event=ignore_event, save_checkpoint=forbidden_save)
    messages = [Message(role="system", content=prompt),
                Message(role="user", content=json.dumps(payload, ensure_ascii=False))]
    model: ChatModel | None = None
    original: BaseException | None = None
    token = model_call_progress.set(ModelCallProgress())
    try:
        async with asyncio.timeout(timeout):
            model = factory()

            async def request() -> Message:
                return await model.chat(messages=messages, tools=[], thinking_enabled=False,
                                        reasoning_effort=None)

            result = await ModelErrorHandlingMiddleware().wrap_model_call(state, context, request)
            if result.tool_calls:
                raise ValueError("记忆模型不得返回工具调用")
            return result
    except BaseException as error:
        original = error
        raise
    finally:
        model_call_progress.reset(token)
        if model is not None:
            try:
                await close_chat_model(model)
            except asyncio.CancelledError:
                # 清理期间的新取消仍须优先，不能被此前的普通选择错误降级。
                raise
            except Exception as error:
                if original is None:
                    raise
                logger.warning("记忆模型清理失败 reason=%s", type(error).__name__)


def parse_json_object(content: str, *, max_chars: int = 128_000) -> dict:
    """接受纯 JSON 或单层 JSON 代码围栏；拒绝残缺、额外文本和超大输出。"""
    if len(content) > max_chars:
        raise ValueError("记忆模型输出超限")
    text = content.strip()
    if text.startswith("```json\n") and text.endswith("\n```"):
        text = text[8:-4].strip()
    elif text.startswith("```\n") and text.endswith("\n```"):
        text = text[4:-4].strip()
    try:
        result = json.loads(text)
    except (ValueError, RecursionError):
        raise ValueError("记忆模型必须返回有效 JSON") from None
    if not isinstance(result, dict):
        raise ValueError("记忆模型必须返回 JSON 对象")
    return result
