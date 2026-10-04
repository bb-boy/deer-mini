"""读取单个网页，把正文交回现有 Agent Loop。

功能：模型选好网址后，提取网页内容以便继续回答。
输入：api_key 来自后端配置；call.arguments["url"] 是完整网页地址；
call.id 关联本次工具调用。context 携带当前用户、对话、Run 与工作目录，
本工具只为统一接口接收它，不直接访问文件、事件或状态保存入口。
输出：ToolResult，成功时是标题和完整提取正文，失败时是已遮住密钥的错误说明。
副作用：请求 Tavily 的正文提取服务并使用额度。SQLite 与实时显示由外层负责。

参考 DeerFlow backend/packages/harness/deerflow/community/tavily/tools.py
的 web_fetch_tool；模型看到的工具说明和参数说明保留原文。
"""

import asyncio
import logging
from urllib.parse import urlsplit

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
FETCH_TIMEOUT_SECONDS = 30.0


class WebFetchTool:
    """接收一个 url，使用 Tavily Extract 读取网页正文。"""

    definition = ToolDefinition(
        name="web_fetch",
        description=(
            "Fetch the contents of a web page at a given URL.\n"
            "Only fetch EXACT URLs that have been provided directly by the user or have been returned in results from the web_search and web_fetch tools.\n"
            "This tool can NOT access content that requires authentication, such as private Google Docs or pages behind login walls.\n"
            "Do NOT add www. to URLs that do NOT have them.\n"
            "URLs must include the schema: https://example.com is a valid URL while example.com is an invalid URL."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch the contents of.",
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    )

    def __init__(self, api_key: str) -> None:
        """输入后台密钥，保存在对象内；构造时不请求网络。"""
        self._api_key = api_key.strip()

    def _error_result(self, call: ToolCall, message: str) -> ToolResult:
        """把错误文字关联到 call.id，并遮住密钥；只做内存处理。"""
        safe_message = message.replace(self._api_key, "[redacted]") if self._api_key else message
        return ToolResult(
            tool_call_id=call.id,
            name=self.definition.name,
            content=safe_message,
            is_error=True,
        )

    async def execute(self, call: ToolCall, context: RuntimeContext) -> ToolResult:
        """读取一个网页并返回结果；状态保存和工具事件仍由外层 Agent 处理。"""
        raw_url = call.arguments.get("url")
        if not isinstance(raw_url, str) or not raw_url.strip():
            return self._error_result(call, "Error: 网页地址 url 必须是非空字符串。")
        url = raw_url.strip()
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in url)
            ):
                raise ValueError("Invalid web URL")
            # 读取 port 属性会检查端口是否为合法数字；不改写原网址。
            _ = parsed.port
        except ValueError:
            return self._error_result(
                call, "Error: 请提供完整的 http:// 或 https:// 网页地址，且不要包含用户名或密码。",
            )
        if not self._api_key:
            return self._error_result(call, "Error: 尚未配置 Tavily 密钥 TAVILY_API_KEY。")

        try:
            # await 等待网页时让出执行机会；成功、失败和取消后都会关闭连接。
            async with asyncio.timeout(FETCH_TIMEOUT_SECONDS):
                async with AsyncTavilyClient(api_key=self._api_key) as client:
                    response = await client.extract(
                        urls=[url],
                        extract_depth="basic",
                        format="markdown",
                        include_images=False,
                        timeout=FETCH_TIMEOUT_SECONDS,
                    )

            if not isinstance(response, dict):
                raise ValueError("Invalid extract response")
            failures = response.get("failed_results", [])
            results = response.get("results", [])
            if not isinstance(failures, list) or not isinstance(results, list):
                raise ValueError("Invalid extract results")

            # 与 DeerFlow 相同：先处理失败记录，再读取第一条成功结果。
            if failures:
                first_failure = failures[0]
                if not isinstance(first_failure, dict) or not isinstance(first_failure.get("error"), str):
                    raise ValueError("Invalid extract failure")
                return self._error_result(call, f"Error: {first_failure['error']}")
            if not results:
                return self._error_result(call, "Error: No results found")

            result = results[0]
            if not isinstance(result, dict):
                raise ValueError("Invalid extracted page")
            content = result.get("raw_content")
            if not isinstance(content, str) or not content.strip():
                return self._error_result(call, "Error: No readable content found")
            title = result.get("title")
            # 缺少标题时使用真实请求网址，不凭空编造网页标题。
            if not isinstance(title, str) or not title.strip():
                title = url
            return ToolResult(
                tool_call_id=call.id,
                name=self.definition.name,
                content=f"# {title}\n\n{content}",
            )
        except (MissingAPIKeyError, InvalidAPIKeyError):
            error_message = "Error: Tavily 密钥不可用，请检查后端的 TAVILY_API_KEY 配置。"
        except UsageLimitExceededError:
            error_message = "Error: Tavily 额度已用完或请求过于频繁，请稍后重试或检查账户额度。"
        except ForbiddenError:
            error_message = "Error: Tavily 拒绝访问，请检查账户权限或额度。"
        except BadRequestError:
            error_message = "Error: Tavily 拒绝了读取请求，请检查网页地址。"
        except (TimeoutError, TavilyTimeoutError, httpx.TimeoutException):
            error_message = "Error: Tavily 网页读取超时，请稍后重试。"
        except (KeyError, TypeError, ValueError):
            error_message = "Error: Tavily 返回了无法识别的网页结果，请稍后重试。"
        except Exception as error:
            # 原始异常可能包含请求凭证；应用日志只记录异常类型。
            logger.warning("Tavily 网页读取失败（%s）", type(error).__name__)
            error_message = "Error: Tavily 网页读取服务暂时不可用，请稍后重试。"

        # CancelledError 不属于 Exception，会继续交给 Runtime 完成取消收尾。
        return self._error_result(call, error_message)
