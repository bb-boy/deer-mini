"""模型关闭必须有界，清理异常不能抢走外部取消。"""

import asyncio

import pytest

from app.model.lifecycle import close_chat_model


def test_close_failure_is_returned_to_owner():
    error = OSError("连接关闭失败")

    class Model:
        async def close(self):
            raise error

    async def scenario():
        with pytest.raises(OSError) as caught:
            await close_chat_model(Model())
        assert caught.value is error

    asyncio.run(scenario())


@pytest.mark.parametrize("close_raises", [False, True])
def test_external_cancel_waits_for_close_and_keeps_cancel_reason(close_raises):
    async def scenario():
        entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Model:
            async def close(self):
                entered.set()
                await release.wait()
                closed.set()
                if close_raises:
                    raise OSError("比取消更晚的清理错误")

        worker = asyncio.create_task(close_chat_model(Model()))
        await entered.wait()
        worker.cancel("原始取消")
        await asyncio.sleep(0)
        assert not worker.done()
        release.set()
        with pytest.raises(asyncio.CancelledError, match="原始取消"):
            await worker
        assert closed.is_set()

    asyncio.run(scenario())


def test_close_timeout_is_bounded_even_when_client_suppresses_first_cancel():
    async def scenario():
        ignored_cancel, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Model:
            async def close(self):
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    ignored_cancel.set()
                    await release.wait()
                finally:
                    closed.set()

        try:
            with pytest.raises(TimeoutError, match="模型连接关闭超过"):
                await asyncio.wait_for(close_chat_model(Model(), 0.02), 0.5)
            await asyncio.wait_for(ignored_cancel.wait(), 0.5)
            assert not closed.is_set()
        finally:
            release.set()
            await asyncio.wait_for(closed.wait(), 0.5)

    asyncio.run(scenario())
