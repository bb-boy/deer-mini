"""验证官方 SDK 的真实请求/响应处理；HTTP 传输受控，不消耗真实额度。"""

import asyncio
import json

import httpx
import pytest

from app.domain.messages import ToolCall
from app.runtime.context import RuntimeContext
from app.tools import web_fetch
from app.tools.web_fetch import WebFetchTool


FAKE_KEY = "tvly-test-not-a-real-key"
PAGE_URL = "https://example.com/article?keep=%2F#section"


def test_long_error_preserves_complete_redacted_content():
    error = "错误详情" * 15_000 + FAKE_KEY
    result = WebFetchTool(FAKE_KEY)._error_result(make_call(), error)
    assert result.is_error
    assert result.content == "错误详情" * 15_000 + "[redacted]"


@pytest.fixture
def install_transport(monkeypatch):
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


def make_call(url=PAGE_URL):
    return ToolCall(id="call-fetch", name="web_fetch", arguments={"url": url})


def make_context():
    return RuntimeContext(
        user_id="fetch-user", thread_id="fetch-thread", run_id="fetch-run",
        workspace_path="/unused", record_event=None, save_checkpoint=None,
    )


def test_fetch_uses_exact_url_and_returns_complete_body(install_transport):
    requests = []
    body = "正文" * 3000

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"results": [
            {"title": "文章标题", "url": PAGE_URL, "raw_content": body},
            {"title": "其他结果", "raw_content": "不应被返回"},
        ], "failed_results": []})

    clients = install_transport(respond)
    result = asyncio.run(WebFetchTool(FAKE_KEY).execute(make_call(), make_context()))
    assert not result.is_error
    assert result.tool_call_id == "call-fetch"
    assert result.name == "web_fetch"
    assert result.content == "# 文章标题\n\n" + body
    assert len(requests) == 1
    assert str(requests[0].url) == "https://api.tavily.com/extract"
    assert requests[0].headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert json.loads(requests[0].content) == {
        "urls": [PAGE_URL], "extract_depth": "basic", "format": "markdown",
        "include_images": False, "timeout": 30,
    }
    assert clients and all(client.is_closed for client in clients)


@pytest.mark.parametrize("title", [None, "", "  "])
def test_missing_title_uses_requested_url(title, install_transport):
    install_transport(lambda request: httpx.Response(200, json={
        "results": [{"title": title, "raw_content": "仍然可以阅读的正文"}],
    }))
    result = asyncio.run(WebFetchTool(FAKE_KEY).execute(make_call(), make_context()))
    assert not result.is_error
    assert result.content == f"# {PAGE_URL}\n\n仍然可以阅读的正文"


def test_failure_takes_priority_and_redacts_credential(install_transport, caplog):
    clients = install_transport(lambda request: httpx.Response(200, json={
        "failed_results": [{"url": PAGE_URL, "error": f"Page blocked; debug key={FAKE_KEY}"}],
        "results": [{"title": "unused", "raw_content": "不应被返回"}],
    }))
    result = asyncio.run(WebFetchTool(FAKE_KEY).execute(make_call(), make_context()))
    assert result.is_error
    assert result.content == "Error: Page blocked; debug key=[redacted]"
    assert FAKE_KEY not in result.content + caplog.text
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("response", [{}, {"results": [], "failed_results": []}])
def test_empty_result_is_an_error(response, install_transport):
    install_transport(lambda request: httpx.Response(200, json=response))
    result = asyncio.run(WebFetchTool(FAKE_KEY).execute(make_call(), make_context()))
    assert result.is_error
    assert result.content == "Error: No results found"


@pytest.mark.parametrize("content", [None, "", " \n "])
def test_empty_body_is_not_reported_as_a_success(content, install_transport):
    install_transport(lambda request: httpx.Response(200, json={
        "results": [{"title": "Title", "raw_content": content}],
    }))
    result = asyncio.run(WebFetchTool(FAKE_KEY).execute(make_call(), make_context()))
    assert result.is_error
    assert result.content == "Error: No readable content found"


@pytest.mark.parametrize("url", [
    None, "", "  ", 42, [], "example.com", "file:///etc/passwd", "ftp://example.com/a",
    "https://", "https://example.com:bad", "https://example.com:99999",
    "https://user:password@example.com", "https://example.com/a b",
    "https://example.com/a\nb", "https://[invalid",
])
def test_invalid_url_is_rejected_before_network(url, install_transport):
    clients = install_transport(lambda request: pytest.fail("无效网址不应请求 Tavily"))
    result = asyncio.run(WebFetchTool(FAKE_KEY).execute(make_call(url), make_context()))
    assert result.is_error
    assert "Error:" in result.content
    assert clients == []


def test_missing_key_is_rejected_before_network(install_transport):
    clients = install_transport(lambda request: pytest.fail("缺少密钥不应请求 Tavily"))
    result = asyncio.run(WebFetchTool(" ").execute(make_call(), make_context()))
    assert result.is_error
    assert "尚未配置" in result.content
    assert clients == []


@pytest.mark.parametrize(("status", "expected"), [
    (401, "密钥不可用"), (429, "额度"), (403, "拒绝访问"),
    (400, "检查网页地址"), (500, "暂时不可用"),
])
def test_provider_errors_are_sanitized(status, expected, install_transport, caplog):
    clients = install_transport(lambda request: httpx.Response(
        status, json={"detail": {"error": f"upstream error with {FAKE_KEY}"}},
    ))
    result = asyncio.run(WebFetchTool(FAKE_KEY).execute(make_call(), make_context()))
    assert result.is_error
    assert expected in result.content
    assert FAKE_KEY not in result.content + caplog.text
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("response", [
    [], {"results": None}, {"failed_results": None},
    {"results": [None]}, {"failed_results": [{"error": None}]},
])
def test_invalid_response_returns_a_tool_error(response, install_transport):
    clients = install_transport(lambda request: httpx.Response(200, json=response))
    result = asyncio.run(WebFetchTool(FAKE_KEY).execute(make_call(), make_context()))
    assert result.is_error
    assert result.content.startswith("Error:")
    assert FAKE_KEY not in result.content
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize(("error_type", "expected"), [
    (httpx.ConnectError, "暂时不可用"), (httpx.ReadTimeout, "超时"),
])
def test_network_errors_are_sanitized(error_type, expected, install_transport, caplog):
    def fail(request):
        raise error_type(f"connection failed: {FAKE_KEY}", request=request)

    clients = install_transport(fail)
    result = asyncio.run(WebFetchTool(FAKE_KEY).execute(make_call(), make_context()))
    assert result.is_error
    assert expected in result.content
    assert FAKE_KEY not in result.content + caplog.text
    assert all(client.is_closed for client in clients)


def test_timeout_closes_client(monkeypatch, install_transport):
    async def never_returns(request):
        await asyncio.Event().wait()

    clients = install_transport(never_returns)
    monkeypatch.setattr(web_fetch, "FETCH_TIMEOUT_SECONDS", 0.02)

    async def execute():
        async with asyncio.timeout(1):
            return await WebFetchTool(FAKE_KEY).execute(make_call(), make_context())

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
        task = asyncio.create_task(WebFetchTool(FAKE_KEY).execute(make_call(), make_context()))
        async with asyncio.timeout(1):
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert clients[0].is_closed

    asyncio.run(execute())
