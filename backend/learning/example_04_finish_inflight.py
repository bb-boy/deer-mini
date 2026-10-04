"""实验 4：给 finish_inflight 加上讲解用的打印，观察每一步。"""

import asyncio
from typing import TypeVar

T = TypeVar("T")


async def finish_inflight(task: asyncio.Task[T]) -> T:
    """输入正在进行的任务；等待其结束再传递外层取消；打印等待过程。"""
    cancelled: asyncio.CancelledError | None = None

    while True:
        try:
            print("finish：进入 await，等待同一个 task")
            result = await asyncio.shield(task)
            print("finish：拿到结果，执行 break")
            break
        except asyncio.CancelledError as error:
            if task.cancelled():
                print("finish：内层 task 自身已取消，立即向上抛出")
                raise
            print("finish：外层收到取消；先记住，再回到循环")
            cancelled = error

    if cancelled is not None:
        print("finish：内层已正常完成，现在向上抛出记住的取消")
        raise cancelled

    print("finish：没有收到取消，正常返回结果")
    return result


async def slow_job() -> int:
    """没有参数；等 1 秒后返回 42；用计时模拟一个可以等待的操作。"""
    print("B：开始工作")
    await asyncio.sleep(1)
    print("B：正常完成，结果是 42")
    return 42


async def demo(cancel_count: int) -> None:
    """输入取消次数 0/1/2；观察返回值或取消；创建任务并打印过程。"""
    print(f"\n本次实验：请求取消 {cancel_count} 次")
    inner_task = asyncio.create_task(slow_job())
    outer_task = asyncio.create_task(finish_inflight(inner_task))

    for number in range(cancel_count):
        await asyncio.sleep(0.2)
        print(f"实验程序：第 {number + 1} 次请求取消外层")
        outer_task.cancel()

    try:
        result = await outer_task
        print("实验程序：正常拿到", result)
    except asyncio.CancelledError:
        print("实验程序：收到向上传递的取消")
        print("实验程序：此时 B 已结束，B 的结果是", inner_task.result())


if __name__ == "__main__":
    asyncio.run(demo(0))
    asyncio.run(demo(1))
    asyncio.run(demo(2))
