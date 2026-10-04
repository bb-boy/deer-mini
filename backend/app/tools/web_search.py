"""把 Tavily 搜索接到 Mini 的统一工具接口。

功能：模型需要网页信息时，搜索并返回标题、链接和摘要。
输入：api_key 来自后端配置；call.arguments["query"] 是模型给出的搜索词；
call.id 用于把结果交还给正确的工具调用。context 是本次对话的运行上下文。
输出：ToolResult，成功时 content 是与 DeerFlow 一致的 JSON 数组。
副作用：向 Tavily 发起网络请求并消耗搜索额度；状态和事件仍由 Agent 保存。

参考：DeerFlow backend/packages/harness/deerflow/community/tavily/tools.py
中的 web_search_tool。工具描述保留原文，仅把框架包装换成 Mini 的 Tool 接口。
"""

import asyncio
import json
import logging

import httpx
from tavily import AsyncTavilyClient
from tavily.errors import (
    BadRequestError,
    ForbiddenError,
    InvalidAPIKeyError,
    MissingAPIKeyError,
    TimeoutError as TavilyTimeoutError,
    UsageLimitExceededError,
)

from app.domain.messages import ToolCall
from app.domain.tools import ToolDefinition, ToolResult
from app.runtime.context import RuntimeContext


logger = logging.getLogger(__name__)
MAX_RESULTS = 5
SEARCH_TIMEOUT_SECONDS = 30.0


class WebSearchTool:
    """接收 query，调用 Tavily，再返回统一的 ToolResult。"""

    # 这份说明书发给模型；密钥不会进入说明书或模型参数。
    definition = ToolDefinition(
        name="web_search",
        description="Search the web.",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The query to search for.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )

    def __init__(self, api_key: str) -> None:
        """输入后端密钥并保存在当前对象内；此时还不会发起搜索。"""
        self._api_key = api_key.strip()

    async def execute(self, call: ToolCall, context: RuntimeContext) -> ToolResult:
        """执行一次搜索；context 用于遵守统一接口，本工具不直接操作文件或数据库。"""
        query = call.arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult(
                tool_call_id=call.id,
                name=self.definition.name,
                content="搜索词 query 必须是非空字符串。",
                is_error=True,
            )
        if not self._api_key:
            return ToolResult(
                tool_call_id=call.id,
                name=self.definition.name,
                content="尚未配置 Tavily 搜索密钥 TAVILY_API_KEY。",
                is_error=True,
            )

        try:
            # await 等待网络时会让出执行机会，页面推送和取消操作仍可继续。
            # async with 确保成功、失败或取消后都关闭客户端连接。
            async with asyncio.timeout(SEARCH_TIMEOUT_SECONDS):
                async with AsyncTavilyClient(api_key=self._api_key) as client:
                    response = await client.search(
                        query=query.strip(),
                        max_results=MAX_RESULTS,
                        search_depth="basic",
                        auto_parameters=False,
                        include_answer=False,
                        include_raw_content=False,
                        timeout=SEARCH_TIMEOUT_SECONDS,
                    )

            if not isinstance(response, dict) or not isinstance(response.get("results"), list):
                raise ValueError("Invalid search response")

            normalized_results = []
            for result in response["results"][:MAX_RESULTS]:
                if not isinstance(result, dict) or any(
                    not isinstance(result.get(field), str)
                    for field in ("title", "url", "content")
                ):
                    raise ValueError("Invalid search result")
                # Tavily 把摘要叫 content；DeerFlow 统一命名为 snippet。
                normalized_results.append({
                    "title": result["title"],
                    "url": result["url"],
                    "snippet": result["content"],
                })

            return ToolResult(
                tool_call_id=call.id,
                name=self.definition.name,
                content=json.dumps(normalized_results, indent=2, ensure_ascii=False),
            )
        except (MissingAPIKeyError, InvalidAPIKeyError):
            error_message = "Tavily 密钥不可用，请检查后端的 TAVILY_API_KEY 配置。"
        except UsageLimitExceededError:
            error_message = "Tavily 搜索额度已用完或请求过于频繁，请稍后重试或检查账户额度。"
        except ForbiddenError:
            error_message = "Tavily 拒绝访问，请检查账户权限或额度。"
        except BadRequestError:
            error_message = "Tavily 拒绝了搜索请求，请调整 query 后重试。"
        except (TimeoutError, TavilyTimeoutError, httpx.TimeoutException):
            error_message = "Tavily 搜索超时，请稍后重试。"
        except (KeyError, TypeError, ValueError):
            error_message = "Tavily 返回了无法识别的搜索结果，请稍后重试。"
        except Exception as error:
            # 不转发供应商原始异常，以免其中的请求信息进入对话和日志。
            logger.warning("Tavily 搜索失败（%s）", type(error).__name__)
            error_message = "Tavily 搜索服务暂时不可用，请稍后重试。"

        # asyncio.CancelledError 不属于 Exception，会继续交给 Runtime 收尾。
        return ToolResult(
            tool_call_id=call.id,
            name=self.definition.name,
            content=error_message,
            is_error=True,
        )
