"""主、子 Agent 共用的中间件列表；每次构建都创建独立实例。"""

from collections.abc import Sequence

from app.agents.middleware import AgentMiddleware
from app.agents.model_call_logging_middleware import ModelCallLoggingMiddleware
from app.agents.model_error_handling_middleware import ModelErrorHandlingMiddleware
from app.agents.tool_call_logging_middleware import ToolCallLoggingMiddleware
from app.agents.tool_error_handling_middleware import ToolErrorHandlingMiddleware
from app.agents.tool_result_storage_middleware import ToolResultStorageMiddleware


def build_runtime_middlewares(
    extra_middlewares: Sequence[AgentMiddleware] = (),
) -> list[AgentMiddleware]:
    return [
        ModelCallLoggingMiddleware(),
        ModelErrorHandlingMiddleware(),
        ToolCallLoggingMiddleware(),
        ToolResultStorageMiddleware(),
        *extra_middlewares,
        # 放在最内层：把工具自身的异常转换成结果，供外层继续处理。
        ToolErrorHandlingMiddleware(),
    ]
