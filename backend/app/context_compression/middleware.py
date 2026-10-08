"""在最后的模型请求准备阶段应用裁剪、空闲清理和摘要，保持完整本地历史。

输入为已注入系统/工作区/记忆/清单信息的请求；输出为本次 API 视图。
压缩元数据经过 Checkpoint 确认才发布。摘要复用模型请求重试边界，无工具、无回答流。
每个主/子 Agent 使用独立实例，不能共享绑定的 Snip 工具或计数。
"""

import asyncio
import json
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace

from app.agents.middleware import AgentMiddleware
from app.agents.model_error_handling_middleware import ModelErrorHandlingMiddleware
from app.context_compression.budget import (
    calibrated_tokens, conservative_ratio, record_usage, remember_calibration,
    request_tokens, reset_request_baseline, text_tokens,
)
from app.context_compression.policy import CompressionPolicy
from app.context_compression.prompts import COMPACT_PROMPT, SECTIONS
from app.context_compression.state import CompressionState, commit_compression
from app.context_compression.view import is_recoverable_tool, active_view, decorate_snip, eligible_ids, groups
from app.domain.common import new_id
from app.domain.messages import Message
from app.domain.threads import ThreadState
from app.filesystem import tool_result_store
from app.model.base import ChatModel
from app.model.errors import ModelCallProgress, model_call_progress
from app.runtime.async_io import run_sync
from app.runtime.context import ModelRetryBudget, RuntimeContext
from app.runtime.errors import StatePersistenceError
from app.tools.registry import ToolRegistry
from app.tools.snip import SnipTool


class ContextBudgetExceeded(RuntimeError):
    """压缩后仍不能容纳受保护信息，停止而不静默丢弃或重放工具。"""


def validate_summary(message: Message, limit: int) -> str:
    text = message.content.strip()
    if message.tool_calls or message.is_error or message.role != "assistant":
        raise ValueError("Auto-Compact 只能返回交接摘要文本，不能调用工具")
    if not text.startswith("<summary>") or not text.endswith("</summary>") or text.count("<summary>") != 1:
        raise ValueError("Auto-Compact 缺少完整 summary 标签")
    headings = []
    for i, title in enumerate(SECTIONS, 1):
        match = re.search(rf"(?m)^\s*{i}\.\s+{re.escape(title)}\s*$", text)
        if match is None or len(re.findall(rf"(?m)^\s*{i}\.\s+{re.escape(title)}\s*$", text)) != 1:
            raise ValueError("Auto-Compact 摘要缺少规定的九段结构")
        headings.append(match.start())
    if headings != sorted(headings) or text_tokens(text) > limit:
        raise ValueError("Auto-Compact 摘要顺序或长度无效")
    boundaries = headings[1:] + [text.rfind("</summary>")]
    for start, end in zip(headings, boundaries):
        if not text[start:end].strip().split("\n", 1)[-1].strip() or "\n" not in text[start:end].strip():
            raise ValueError("Auto-Compact 摘要章节不能为空")
    return text


def transcript(messages: list[Message]) -> str:
    parts = []
    for message in messages:
        parts.append(f"[{message.role} message_id={message.id} is_error={message.is_error}]\n{message.content}")
        if message.tool_calls:
            parts.append("tool_calls=" + json.dumps([c.to_dict() for c in message.tool_calls], ensure_ascii=False))
        if message.reasoning_content:
            parts.append("reasoning_content=" + message.reasoning_content)
        if message.tool_result_file:
            parts.append("tool_result_file=" + message.tool_result_file)
        if message.tool_call_id:
            parts.append("tool_call_id=" + message.tool_call_id)
    return "\n\n".join(parts)


class ContextCompressionMiddleware(AgentMiddleware):
    def __init__(self, model: ChatModel, registry: ToolRegistry, *, model_key: str,
                 policy: CompressionPolicy | None = None, clock: Callable[[], float] = time.time) -> None:
        self.model, self.registry, self.model_key = model, registry, model_key
        self.policy = policy or CompressionPolicy.from_env()
        self.clock = clock
        self.replay_reasoning = getattr(getattr(model, "_profile", None), "replay_reasoning_content", True)
        self._request_estimate = 0
        self._summary_ratio = 1.0

    async def before_agent(self, state: ThreadState, context: RuntimeContext) -> None:
        if (state.user_id, state.thread_id) != (context.user_id, context.thread_id):
            raise ValueError("压缩状态不属于当前上下文")
        if state.workspace_path not in {None, context.workspace_path}:
            raise ValueError("压缩工作目录不匹配")
        known = {message.id for message in state.messages}
        metadata = state.compression
        if not (set(metadata.snipped_ids) | set(metadata.summarized_ids) | set(metadata.cleared_results)) <= known:
            raise ValueError("压缩记录引用了不存在的消息")
        tool = self.registry.get("snip")
        if isinstance(tool, SnipTool):
            tool.bind(state, context)

    def _estimate(self, messages: list[Message]) -> int:
        return request_tokens(messages, self.registry.definitions(), replay_reasoning=self.replay_reasoning)

    def _view(self, messages: list[Message], state: ThreadState, metadata: CompressionState,
              *, reminder: bool = False) -> list[Message]:
        view = active_view(messages, metadata)
        if self.policy.enabled and self.policy.snip_enabled and self.registry.get("snip"):
            view = decorate_snip(view, state.messages, reminder=reminder)
        return view

    async def _clear_idle(self, state: ThreadState, context: RuntimeContext) -> None:
        previous = state.compression
        if previous.last_api_at is None or self.clock() - previous.last_api_at <= self.policy.idle_seconds:
            return
        hidden = set(previous.snipped_ids) | set(previous.summarized_ids)
        results = []
        for group in groups(state.messages):
            calls = {call.id: call.name for call in group[0].tool_calls}
            for message in group[1:]:
                if message.id not in hidden and not message.is_error and is_recoverable_tool(calls.get(message.tool_call_id, "")):
                    results.append(message)
        eligible = eligible_ids(state.messages)
        candidate = previous.model_copy(deep=True)
        for message in results[:-self.policy.keep_tool_results]:
            if message.id not in eligible or message.id in candidate.cleared_results:
                continue
            path = message.tool_result_file or tool_result_store.new_result_path()
            try:
                if message.tool_result_file:
                    await run_sync(tool_result_store.read_chars, context.workspace_path, path, 0, 1)
                else:
                    await run_sync(tool_result_store.save_text, context.workspace_path, path, message.content)
            except (OSError, ValueError, RuntimeError) as error:
                raise StatePersistenceError("旧工具结果未能确认保存，不能清理") from error
            candidate.cleared_results[message.id] = path
        if candidate != previous:
            reset_request_baseline(candidate)
            await commit_compression(state, context, candidate)

    async def prepare_model_messages(self, state: ThreadState, context: RuntimeContext,
                                     messages: list[Message]) -> list[Message]:
        if self.policy.enabled:
            await self._clear_idle(state, context)
        metadata = state.compression.model_copy(deep=True)
        reminder = False
        if self.policy.enabled and self.policy.snip_enabled:
            start = next((i + 1 for i, m in enumerate(state.messages) if m.id == metadata.growth_anchor_id), 0)
            metadata.growth_tokens += request_tokens(state.messages[start:], [], replay_reasoning=self.replay_reasoning) if state.messages[start:] else 0
            metadata.growth_anchor_id = state.messages[-1].id if state.messages else None
            if metadata.growth_tokens >= self.policy.reminder_tokens:
                reminder = True
                metadata.growth_tokens = 0
        if metadata != state.compression:
            await commit_compression(state, context, metadata)
        view = self._view(messages, state, metadata, reminder=reminder)
        estimate = self._estimate(view)
        used = calibrated_tokens(estimate, metadata, self.model_key)
        if self.policy.enabled and self.policy.context_window - used <= self.policy.compact_remaining:
            view = await self._compact(state, context, messages, used)
        self._request_estimate = self._estimate(view)
        return view

    async def _mark_attempt(self, state: ThreadState, context: RuntimeContext) -> None:
        candidate = state.compression.model_copy(deep=True)
        candidate.last_api_at = float(self.clock())
        await commit_compression(state, context, candidate)

    async def wrap_model_call(self, state: ThreadState, context: RuntimeContext,
                              call_next: Callable[[], Awaitable[Message]]) -> Message:
        await self._mark_attempt(state, context)
        response = await call_next()
        usage = getattr(self.model, "last_prompt_tokens", None)
        metadata = state.compression.model_copy(deep=True)
        record_usage(metadata, self.model_key, self._request_estimate, usage)
        # after_model 的外层 Checkpoint 将与回答一起持久化 usage；不引入回复前的额外提交。
        state.compression = metadata
        return response

    async def _summary_call(self, state: ThreadState, context: RuntimeContext, messages: list[Message]) -> str:
        async def silent_event(*args: object, **kwargs: object) -> None:
            return None

        quiet_context = replace(context, record_event=silent_event, model_retry_budget=ModelRetryBudget())
        progress_token = model_call_progress.set(ModelCallProgress())
        try:
            async def request() -> Message:
                await self._mark_attempt(state, context)
                return await self.model.chat(messages=messages, tools=[], thinking_enabled=False,
                    reasoning_effort=None, max_output_tokens=self.policy.summary_tokens)

            response = await ModelErrorHandlingMiddleware().wrap_model_call(state, quiet_context, request)
            summary = validate_summary(response, self.policy.summary_tokens)
            usage = getattr(self.model, "last_prompt_tokens", None)
            if type(usage) is int and usage > 0:
                estimate = request_tokens(messages, [], replay_reasoning=False)
                self._summary_ratio = max(self._summary_ratio, usage / estimate)
                metadata = state.compression.model_copy(deep=True)
                remember_calibration(metadata, self.model_key, self._summary_ratio)
                if metadata != state.compression:
                    await commit_compression(state, context, metadata)
            return summary
        finally:
            model_call_progress.reset(progress_token)

    async def _compact(self, state: ThreadState, context: RuntimeContext, messages: list[Message],
                       before_tokens: int) -> list[Message]:
        hidden = set(state.compression.snipped_ids) | set(state.compression.summarized_ids)
        selected = eligible_ids(state.messages) - hidden
        if not selected:
            raise ContextBudgetExceeded("当前上下文已接近容量，受保护内容无法自动压缩；请缩小当前输入或新建对话")
        await context.record_event("model.status", {
            "phase": "compacting", "message_id": "context-compaction",
            "message": "正在整理上下文，保存交接摘要后继续…",
        })
        try:
            source = transcript(active_view(messages, state.compression))
            # 按每次实际序列化请求切块，包含前序摘要、JSON 转义、系统提示及输出预算。
            # 顺序覆盖全部源字符，不能只取前缀冒充完整总结。
            self._summary_ratio = conservative_ratio(state.compression, self.model_key)
            summary = ""
            while source:
                def inputs_for(length: int) -> list[Message]:
                    return [Message(role="system", content=COMPACT_PROMPT), Message(role="user", content=(
                        "前序交接摘要（若有）：\n" + summary + "\n\n接续历史资料：\n" + source[:length]
                    ))]

                reserve = math.ceil(self.policy.summary_tokens * self._summary_ratio)
                limit = self.policy.context_window - reserve - 256
                lo, hi = 0, min(len(source), limit * 3)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if math.ceil(request_tokens(inputs_for(mid), [], replay_reasoning=False) * self._summary_ratio) <= limit:
                        lo = mid
                    else:
                        hi = mid - 1
                if not lo:
                    raise ContextBudgetExceeded("摘要提示与前序摘要已超过输入预算")
                summary = await self._summary_call(state, context, inputs_for(lo))
                source = source[lo:]
            previous = state.compression
            candidate = CompressionState.model_validate({**previous.model_dump(),
                "summary": summary, "summary_id": new_id(),
                "summarized_ids": [m.id for m in state.messages if m.id in selected | set(previous.summarized_ids)],
                "growth_tokens": 0, "growth_anchor_id": state.messages[-1].id if state.messages else None,
            })
            remember_calibration(candidate, self.model_key, self._summary_ratio)
            reset_request_baseline(candidate)
            projected = self._view(messages, state, candidate)
            estimate = self._estimate(projected)
            after_tokens = calibrated_tokens(estimate, candidate, self.model_key)
            if after_tokens >= before_tokens or self.policy.context_window - after_tokens <= self.policy.compact_remaining:
                raise ContextBudgetExceeded("摘要后受保护内容仍超出上下文预算，原始历史和已有压缩状态已保留")
            await commit_compression(state, context, candidate)
        except BaseException:
            # 辅助状态清理不得掩盖模型失败、取消或关键保存异常。
            try:
                await context.record_event("model.status", {"phase": "complete", "message_id": "context-compaction"})
            except (Exception, asyncio.CancelledError):
                pass
            raise
        await context.record_event("model.status", {"phase": "complete", "message_id": "context-compaction"})
        return projected
