"""识别供应商请求错误，给运行层提供不含上游正文的错误消息。"""

import math
import re
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
import httpx2
import openai


@dataclass
class ModelCallProgress:
    message_id: str = ""
    visible_output: bool = False


model_call_progress: ContextVar[ModelCallProgress | None] = ContextVar("model_call_progress", default=None)


class EmptyModelResponseError(RuntimeError):
    pass


class IncompleteModelResponseError(RuntimeError):
    """流结束但没有完成标记；不得执行已收集的工具碎片。"""


class ModelRequestTimeoutError(RuntimeError):
    """中间件自己的单次尝试时限到达。"""


class InvalidModelResponseError(RuntimeError):
    def __init__(self, reason="invalid_response"):
        self.reason = reason
        super().__init__("模型响应格式无效" if reason == "invalid_response" else "模型响应未完整完成")


@dataclass(frozen=True)
class ErrorDecision:
    reason: str
    retryable: bool
    max_attempts: int
    status_code: int | None = None


PUBLIC_REASONS = {
    "rate_limit": "模型服务限流",
    "burst_rate": "模型服务限制突发请求",
    "transient": "模型服务暂时不可用",
    "connection": "模型服务连接失败",
    "timeout": "模型请求超时",
    "empty_response": "模型返回了空响应",
    "incomplete_response": "模型连接结束，但响应没有完整完成",
    "stream_interrupted": "模型生成中断，已收到的片段未作为完整回复保存",
    "quota": "模型服务额度不足，请检查账户额度",
    "auth": "模型服务鉴权或权限检查失败，请检查配置",
    "context_length": "模型输入超过上下文限制",
    "not_found": "模型或接口不存在，请检查配置",
    "request": "模型请求被拒绝，请检查参数和模型配置",
    "invalid_response": "模型返回的响应或工具参数格式无效",
    "response_length": "模型输出达到长度限制，响应未完整完成",
    "content_filter": "模型服务拒绝生成这次回复",
    "call_timeout": "模型调用达到总时间限制",
}


class ModelCallError(RuntimeError):
    """Runtime 可以直接展示的安全异常；原异常只保留在 __cause__。"""

    def __init__(self, reason, attempts, *, status_code=None):
        self.reason, self.attempts, self.status_code = reason, attempts, status_code
        super().__init__(f"{PUBLIC_REASONS[reason]}（已尝试 {attempts} 次）")


def classify_model_error(error: Exception) -> ErrorDecision | None:
    # 本地错误不按上游请求故障处理。
    if isinstance(error, EmptyModelResponseError):
        return ErrorDecision("empty_response", True, 2)
    if isinstance(error, IncompleteModelResponseError):
        return ErrorDecision("incomplete_response", True, 2)
    if isinstance(error, InvalidModelResponseError):
        return ErrorDecision(error.reason, False, 1)
    if isinstance(error, (ModelRequestTimeoutError, openai.APITimeoutError, httpx.TimeoutException, httpx2.TimeoutException)):
        return ErrorDecision("timeout", True, 2)
    if isinstance(error, (openai.APIConnectionError, httpx.NetworkError, httpx.RemoteProtocolError, httpx2.NetworkError, httpx2.RemoteProtocolError)):
        return ErrorDecision("connection", True, 3)
    if not isinstance(error, openai.APIStatusError):
        return None

    body = error.body if isinstance(error.body, dict) else {}
    if isinstance(body.get("error"), dict):
        body = body["error"]
    codes = {str(body.get(key) or "").lower() for key in ("code", "type")}
    detail = str(body.get("message") or "").lower()
    status = error.status_code

    if codes & {"insufficient_quota", "quota_exceeded", "insufficient_balance", "billing_hard_limit_reached", "balance_not_enough"} or status == 402 or any(
        phrase in detail for phrase in ("insufficient quota", "insufficient balance", "余额不足", "额度不足", "欠费")
    ):
        return ErrorDecision("quota", False, 1, status)
    if codes & {"invalid_api_key", "authentication_error", "permission_denied", "unauthorized"} or status in {401, 403}:
        return ErrorDecision("auth", False, 1, status)
    if "context_length_exceeded" in codes:
        return ErrorDecision("context_length", False, 1, status)
    if status == 429:
        if "limit_burst_rate" in codes or "limit_burst_rate" in detail:
            return ErrorDecision("burst_rate", True, 2, status)
        return ErrorDecision("rate_limit", True, 3, status)
    if status == 408:
        return ErrorDecision("timeout", True, 2, status)
    if status in {409, 425, 500, 502, 503, 504}:
        return ErrorDecision("transient", True, 3, status)
    return ErrorDecision("not_found" if status == 404 else "request", False, 1, status)


def retry_after_seconds(error: Exception, *, now=None) -> float | None:
    response = getattr(error, "response", None)
    value = getattr(response, "headers", {}).get("retry-after")
    if not value:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            seconds = (deadline - (now or datetime.now(timezone.utc))).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def safe_request_id(error: Exception) -> str | None:
    value = getattr(error, "request_id", None)
    return value if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.:\-]{1,128}", value) else None
