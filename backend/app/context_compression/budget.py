"""离线请求估算与服务端 usage 校准；不是供应商专用 tokenizer 的精确计数。"""

import json
import math

from app.context_compression.state import CompressionState
from app.domain.messages import Message
from app.domain.tools import ToolDefinition
from app.model.openai_compatible import OpenAICompatibleModel


def text_tokens(text: str) -> int:
    """ASCII 约每 3 字符一 token，非 ASCII 约每 2 UTF-8 字节一 token。

    没有 usage 时使用偏保守的离线估计，避免下载 tokenizer 或发送计数请求。
    文本组成/模型会影响误差，不能以此承诺准确的上下文余量。
    """
    ascii_chars = sum(char.isascii() for char in text)
    non_ascii_bytes = len(text.encode("utf-8", errors="replace")) - ascii_chars
    return math.ceil(ascii_chars / 3 + non_ascii_bytes / 2)


def request_tokens(messages: list[Message], tools: list[ToolDefinition], *, replay_reasoning: bool) -> int:
    payload = []
    for message in messages:
        value = OpenAICompatibleModel._message_to_api(message)
        if not replay_reasoning:
            value.pop("reasoning_content", None)
        payload.append(value)
    serialized = json.dumps({"messages": payload, "tools": [
        OpenAICompatibleModel._tool_to_api(tool) for tool in tools
    ]}, ensure_ascii=False, separators=(",", ":"))
    return text_tokens(serialized) + 8 * len(messages) + 16


def conservative_ratio(state: CompressionState, model_key: str) -> float:
    """取当前模型已知的保守比率，旧快照可直接从尚存的 usage 基准推导。"""
    ratio = state.calibration_ratio if state.calibration_model == model_key else 1.0
    if state.usage_model == model_key and state.last_prompt_tokens and state.last_request_estimate:
        ratio = max(ratio, state.last_prompt_tokens / state.last_request_estimate)
    return max(1.0, ratio)


def remember_calibration(state: CompressionState, model_key: str, ratio: float) -> None:
    """记录当前模型已知的低估比例；裁剪正文或缺失 usage 都不能降低它。"""
    state.calibration_ratio = max(conservative_ratio(state, model_key), ratio)
    state.calibration_model = model_key


def calibrated_tokens(estimate: int, state: CompressionState, model_key: str) -> int:
    return math.ceil(estimate * conservative_ratio(state, model_key))


def reset_request_baseline(state: CompressionState) -> None:
    """视图变化后清除旧请求基准，但先将旧快照中的校准信息独立保留。

    摘要可能已在另一个模型上取得更新的校准；此时不能用旧主请求覆盖它。
    主/子各自保存这两个字段，调用方仍须经过同一状态提交边界。
    """
    if state.usage_model and state.calibration_model in {None, state.usage_model}:
        remember_calibration(state, state.usage_model, conservative_ratio(state, state.usage_model))
    state.last_prompt_tokens = None
    state.last_request_estimate = 0


def record_usage(state: CompressionState, model_key: str, estimate: int, usage: int | None) -> None:
    """更新本次主请求 usage；无有效 usage 时继续沿用当前模型的已知保守比例。"""
    reset_request_baseline(state)
    state.usage_model = model_key
    state.last_request_estimate = estimate
    state.last_prompt_tokens = usage if type(usage) is int and usage > 0 else None
    if state.last_prompt_tokens is not None and estimate > 0:
        remember_calibration(state, model_key, state.last_prompt_tokens / estimate)
