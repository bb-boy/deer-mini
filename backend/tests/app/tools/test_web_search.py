"""验证真实 Tavily SDK 的请求边界；HTTP 响应受控，不消耗搜索额度。"""

import asyncio
import json

import httpx
import pytest

from app.domain.messages import ToolCall
from app.runtime.context import RuntimeContext
from app.tools import web_search
from app.tools.web_search import WebSearchTool


FAKE_KEY = "tvly-test-not-a-real-key"


@pytest.fixture
def install_transport(monkeypatch):
    """只替换网络传输，保留 SDK 的请求、响应处理与连接关闭逻辑。"""
    original_client = httpx.AsyncClient

    def install(handler):
        clients = []

        def create_client(*args, **kwargs):
            kwargs.pop("mounts", None)
            kwargs.update(transport=httpx.MockTransport(handler), trust_env=False)
            client = original_client(*args, **kwargs)
            clients.append(client)
            return client

        monkeypatch.setattr(httpx, "AsyncClient", create_client)
        return clients

    return install


def make_call(query="Python asyncio"):
    return ToolCall(id="call-search", name="web_search", arguments={"query": query})


def make_context():
    return RuntimeContext(
        user_id="search-user", thread_id="search-thread", run_id="search-run",
        workspace_path="/unused", record_event=None, save_checkpoint=None,
    )


def test_search_returns_deerflow_format_and_closes_client(install_transport):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "results": [
                {"title": f"文档 {i}", "url": f"https://example.com/{i}",
                 "content": "搜索摘要", "raw_content": "不应返回的整页正文"}
                for i in range(7)
            ],
            "answer": "不应代替主模型的回答",
        })

    clients = install_transport(respond)
    result = asyncio.run(WebSearchTool(FAKE_KEY).execute(make_call("  Python asyncio  "), make_context()))

    assert result.is_error is False
    assert result.tool_call_id == "call-search"
    assert result.name == "web_search"
    assert json.loads(result.content) == [
        {"title": f"文档 {i}", "url": f"https://example.com/{i}", "snippet": "搜索摘要"}
        for i in range(5)
    ]
    assert "文档" in result.content
    assert FAKE_KEY not in result.content
    assert len(requests) == 1
    assert str(requests[0].url) == "https://api.tavily.com/search"
    assert requests[0].headers["authorization"] == f"Bearer {FAKE_KEY}"
    body = json.loads(requests[0].content)
    assert body == {
        "query": "Python asyncio", "max_results": 5, "search_depth": "basic",
        "auto_parameters": False, "include_answer": False, "include_raw_content": False,
    }
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("response", [{"results": []}, {}])
def test_empty_search_is_a_valid_empty_result(response, install_transport):
    # 官方 SDK 的 search() 会把缺少 results 字段的字典规范化为 results=[]。
    install_transport(lambda request: httpx.Response(200, json=response))
    result = asyncio.run(WebSearchTool(FAKE_KEY).execute(make_call(), make_context()))
    assert not result.is_error
    assert json.loads(result.content) == []


@pytest.mark.parametrize("query", [None, "", "  ", 42, [], {}])
def test_invalid_query_does_not_open_a_network_client(query, install_transport):
    clients = install_transport(lambda request: pytest.fail("无效输入不应请求 Tavily"))
    result = asyncio.run(WebSearchTool(FAKE_KEY).execute(make_call(query), make_context()))
    assert result.is_error
    assert "query 必须是非空字符串" in result.content
    assert clients == []


def test_missing_key_does_not_open_a_network_client(install_transport):
    clients = install_transport(lambda request: pytest.fail("没有密钥不应请求 Tavily"))
    result = asyncio.run(WebSearchTool("  ").execute(make_call(), make_context()))
    assert result.is_error
    assert "尚未配置" in result.content
    assert clients == []


@pytest.mark.parametrize(("status", "expected"), [
    (401, "密钥不可用"), (429, "额度"), (403, "拒绝访问"),
    (400, "调整 query"), (500, "暂时不可用"),
])
def test_provider_errors_are_useful_without_leaking_details(
    status, expected, install_transport, caplog,
):
    clients = install_transport(lambda request: httpx.Response(
        status, json={"detail": {"error": f"upstream error with {FAKE_KEY}"}},
    ))
    result = asyncio.run(WebSearchTool(FAKE_KEY).execute(make_call(), make_context()))
    assert result.is_error
    assert expected in result.content
    assert FAKE_KEY not in result.content
    assert FAKE_KEY not in caplog.text
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("response", [
    [], {"results": None}, {"results": [{"title": "missing url"}]},
    {"results": [{"title": "title", "url": "https://example.com", "content": None}]},
])
def test_malformed_results_are_reported_as_errors(response, install_transport):
    clients = install_transport(lambda request: httpx.Response(200, json=response))
    result = asyncio.run(WebSearchTool(FAKE_KEY).execute(make_call(), make_context()))
    assert result.is_error
    # 非字典响应可能先被 SDK 拒绝；两条路径都应返回可读的工具错误。
    assert "Tavily" in result.content
    assert result.tool_call_id == "call-search"
    assert FAKE_KEY not in result.content
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize(("error_type", "expected"), [
    (httpx.ConnectError, "暂时不可用"), (httpx.ReadTimeout, "超时"),
])
def test_network_errors_close_client(error_type, expected, install_transport, caplog):
    def fail(request):
        raise error_type(f"connection failed: {FAKE_KEY}", request=request)

    clients = install_transport(fail)
    result = asyncio.run(WebSearchTool(FAKE_KEY).execute(make_call(), make_context()))
    assert result.is_error
    assert expected in result.content
    assert FAKE_KEY not in result.content + caplog.text
    assert all(client.is_closed for client in clients)


def test_overall_timeout_bounds_wait_and_closes_client(monkeypatch, install_transport):
    async def never_returns(request):
        await asyncio.Event().wait()

    clients = install_transport(never_returns)
    monkeypatch.setattr(web_search, "SEARCH_TIMEOUT_SECONDS", 0.02)

    async def execute():
        async with asyncio.timeout(1):
            return await WebSearchTool(FAKE_KEY).execute(make_call(), make_context())

    result = asyncio.run(execute())
    assert result.is_error
    assert "超时" in result.content
    assert all(client.is_closed for client in clients)


def test_cancellation_propagates_and_closes_client(install_transport):
    async def execute():
        started = asyncio.Event()

        async def wait_for_cancellation(request):
            started.set()
            await asyncio.Event().wait()

        clients = install_transport(wait_for_cancellation)
        task = asyncio.create_task(WebSearchTool(FAKE_KEY).execute(make_call(), make_context()))
        async with asyncio.timeout(1):
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert len(clients) == 1
        assert clients[0].is_closed

    asyncio.run(execute())
