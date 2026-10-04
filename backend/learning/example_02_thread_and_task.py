"""实验 2：同步工作交给线程后，外层任务还能继续打印进度。"""

import asyncio
import time


# def slow_add(a: int, b: int) -> int:
#     """输入两个整数；等 2 秒后返回和；打印过程，不访问数据库。"""
#     print("工作线程：开始计算，模拟耗时 2 秒")
#     time.sleep(2)  # 同步等待：占用执行这个函数的线程。
#     print("工作线程：计算完成")
#     return a + b


def slow_add(a, b):
    total = 0
    for i in range(1000):
        print(f"工作线程：正在计算，当前 i={i}")
        total += i
    return a + b


async def main() -> None:
    """没有参数；安排工作并等到结果；副作用是创建任务和终端输出。"""
    # 这一步只得到协程对象，还没有安排 slow_add 开始执行。
    operation = asyncio.to_thread(slow_add, 3, 4)
    print("外层：已经准备好 operation，还没有创建 Task")

    # Task 是跟踪这项工作的任务对象，不是工作线程本身。
    task = asyncio.create_task(operation)

    for number in range(1, 4):
        print(f"外层：正在做别的事，第 {number} 次")
        # 异步等待会把执行机会交给调度器。
        await asyncio.sleep(0.0001)

    print("外层：现在开始等待 task 的结果")
    result = await task
    print("外层：拿到结果", result)


if __name__ == "__main__":
    asyncio.run(main())
