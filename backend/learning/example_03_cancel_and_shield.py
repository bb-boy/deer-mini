"""实验 3：比较 await task 和 await shield(task) 的取消传播。"""

import asyncio


async def slow_job() -> str:
    """没有参数；等待 2 秒后返回文字；只用计时模拟耗时工作。"""
    print("B：开始工作")
    try:
        await asyncio.sleep(2)
    except asyncio.CancelledError:
        print("B：收到取消，工作提前结束")
        raise
    print("B：工作正常完成")
    return "B 的结果"


async def wait_for_job(task: asyncio.Task[str], use_shield: bool) -> str:
    """输入 B 及是否保护它；正常时返回 B 的结果；取消时打印并抛出。"""
    try:
        if use_shield:
            result = await asyncio.shield(task)
        else:
            result = await task
        print("A：正常取得结果")
        return result
    except asyncio.CancelledError:
        print("A：收到取消，退出等待")
        raise


async def demo(use_shield: bool) -> None:
    """输入是否使用 shield；创建 A/B 并取消 A；输出过程，无数据库操作。"""
    print("\n使用 shield：" + str(use_shield))
    inner_task = asyncio.create_task(slow_job())  # B：被等待的任务。
    outer_task = asyncio.create_task(wait_for_job(inner_task, use_shield))  # A。

    await asyncio.sleep(0.3)
    print("实验程序：请求取消 A")
    outer_task.cancel()

    try:
        await outer_task
    except asyncio.CancelledError:
        pass  # 实验程序已知道 A 被取消，继续观察 B。

    print("实验程序：B 是否以取消结束？", inner_task.cancelled())

    if not inner_task.cancelled():
        # 这是实验程序另行等待 B；刚才的 A 已经退出了。
        # 必须等 B 结束再退出程序，否则 asyncio.run 关机时会取消未完任务。
        print("实验程序：A 已退出，现在由实验程序等 B 完成")
        result = await inner_task
        print("实验程序：拿到", result)


if __name__ == "__main__":
    asyncio.run(demo(False))
    asyncio.run(demo(True))
