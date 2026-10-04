# 用五个小实验理解 async_io.py

实际项目与实验文件均在 **inA**：`/home/pl/deer_mini/backend/learning/`。这些文件配有一份本地阅读副本，便于在 Codex 中打开；第五个实验必须在 inA 的项目中运行，以便导入真实的 run_sync。

这五个脚本已在 inA 的 Python 3.10 中逐个运行，退出码均为 0。只使用 Python 自带的模块，无需安装依赖。

先在终端进入项目：

```bash
ssh inA
cd /home/pl/deer_mini/backend
```

如果终端已经连到 inA，只执行 cd 那行。下面的 python3 -m 命令表示按模块名运行 learning 目录中的对应 Python 文件。

每次只运行一个例子。先看打印，再按“动手改一处”修改一次，然后重新运行。先从第一个开始。

`if __name__ == "__main__":` 下面的代码是直接运行脚本时的入口。异步例子的 `asyncio.run(...)` 负责启动并运行异步入口。学习当前例子时，先关注打印和被标出的核心调用。

## 1. 普通函数怎样接收参数，T 怎样描述结果

先体验普通的函数调用。暂时不接触线程和取消：把函数本身与参数分开交给一个包装函数，观察参数怎样转交、结果怎样原样返回。

`TypeVar("T")` 创建一个名叫 T 的类型占位符，左边的 `T` 变量保存它。它只用于类型说明。`Callable[..., T]` 表示传进来的函数正常返回 T 类型的结果，`-> T` 表示包装函数正常返回同种类型的结果。T 本身不会变成这次得到的整数或字符串。

**add(a, b=0)**

- **功能**：计算两个整数的和。
- **输入**：a、b 是本次相加的两个数；b 不传时使用 0。
- **输出**：一个整数，例如 3 + 4 得到 7。
- **副作用**：只做内存计算。

**greet(name, prefix='你好')**

- **功能**：生成一段招呼文字，展示同步函数也能返回字符串。
- **输入**：name 是称呼谁；prefix 是招呼用语。
- **输出**：例如“早上好，小明”。
- **副作用**：只做内存计算。

**call_function(function, *args, **kwargs)**

- **功能**：接收一个函数，替调用者转交参数并交还结果。这是理解 run_sync 外形的第一步。
- **输入**：function 是要调用的函数，例如 add；args 收集按位置传入的参数，例如 3；kwargs 收集带名字的参数，例如 b=4。
- **输出**：function 正常返回的实际结果；它的结果类型与 call_function 的返回类型都用 T 表示。
- **副作用**：打印函数名、收到的参数和结果；然后执行传入函数的行为。

运行：

```bash
python3 -m learning.example_01_parameters
```

完整代码：

```python
"""实验 1：先用普通函数理解 function、*args、**kwargs 和 T。"""

from collections.abc import Callable
from typing import TypeVar

# T 是类型说明中的占位符，不是这次运算得到的数据。
T = TypeVar("T")


def add(a: int, b: int = 0) -> int:
    """输入两个整数；返回它们的和；只在内存中计算。"""
    return a + b


def greet(name: str, prefix: str = "你好") -> str:
    """输入名字和招呼用语；返回一段文字；只在内存中计算。"""
    return f"{prefix}，{name}"


def call_function(function: Callable[..., T], *args, **kwargs) -> T:
    """输入函数及其参数；调用后原样返回结果；在终端打印转交过程。"""
    print("收到的函数：", function.__name__)
    print("位置参数 args：", args)
    print("命名参数 kwargs：", kwargs)

    # 定义函数时的 * 和 ** 收集参数；调用时的 * 和 ** 展开参数。
    result = function(*args, **kwargs)
    print("实际结果：", repr(result), "结果类型：", type(result).__name__)
    return result


if __name__ == "__main__":
    print("实验 1A：把 add 函数和它的参数分开交进去")
    number = call_function(add, 3, b=4)
    print("调用者拿到：", number)

    print("\n实验 1B：同一个包装函数也可以返回文字")
    text = call_function(greet, "小明", prefix="早上好")
    print("调用者拿到：", text)

    print("\nT 自己仍然是类型占位符：", T)
    print("把 call_function 的类型标注去掉，函数体仍会产生相同结果。")
```

**运行后观察**：第一次调用应得到 7，类型是 int；第二次得到“早上好，小明”，类型是 str。最后打印的 T 仍是 ~T。注意传进去的是 add，程序进入 call_function 以后才执行 add(3, b=4)。

**动手改一处**：只把 `call_function(add, 3, b=4)` 中的 `b=4` 改为 `b=100`，再运行。观察 kwargs 和结果如何一起变化。

## 2. 把同步工作交给线程，创建 Task 并等待结果

体验程序等待一个耗时计算时，还可以推进其他工作。这对应真实项目中把同步数据库操作交给工作线程的用途。

`async def` 定义可通过 await 等待的函数；调用它会得到待安排的协程对象。`asyncio.to_thread(...)` 也先返回这种对象。`create_task` 把协程交给调度器安排运行，并返回任务对象。`await task` 等待任务的实际结果。线程是干活的执行路线，Task 是 asyncio 跟踪工作状态和结果的对象。

**slow_add(a, b)**

- **功能**：模拟一次耗时 2 秒的同步计算。
- **输入**：a、b 是最终要相加的两个整数。
- **输出**：它们的和，本例为 7。
- **副作用**：占用执行它的线程 2 秒，并打印开始和结束；不访问数据库。

**main()**

- **功能**：准备线程操作、创建任务、打印其他进度，再等待计算结果。
- **输入**：没有参数，使用脚本里给定的 3 和 4。
- **输出**：没有有用返回值，最终把计算结果打印出来。
- **副作用**：安排线程工作、创建异步任务、在终端输出。

运行：

```bash
python3 -m learning.example_02_thread_and_task
```

完整代码：

```python
"""实验 2：同步工作交给线程后，外层任务还能继续打印进度。"""

import asyncio
import time


def slow_add(a: int, b: int) -> int:
    """输入两个整数；等 2 秒后返回和；打印过程，不访问数据库。"""
    print("工作线程：开始计算，模拟耗时 2 秒")
    time.sleep(2)  # 同步等待：占用执行这个函数的线程。
    print("工作线程：计算完成")
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
        await asyncio.sleep(0.3)

    print("外层：现在开始等待 task 的结果")
    result = await task
    print("外层：拿到结果", result)


if __name__ == "__main__":
    asyncio.run(main())
```

**运行后观察**：工作线程打印“开始计算”后，外层仍会打印“正在做别的事”。外层走到 await task 后，要等计算完成才能打印结果 7。`to_thread` 这行仅准备操作，实际安排是在 create_task 之后。

**动手改一处**：只把 `time.sleep(2)` 改成 `time.sleep(5)`。再次运行，观察外层的三次进度仍能出现，最终结果需要更久才能拿到。

## 3. 观察取消如何传播，以及 shield 挡住了什么

比较两种等待方式。A 是等待者，B 是被等待的任务；实验程序只取消 A，观察 B 是否也被取消。

`asyncio.shield(task)` 接收 B 的任务对象，返回一个可等待的对象，阻止 A 的取消沿这次等待传播给 B。它不阻止 A 自己收到 CancelledError。这个实验的 B 是 asyncio.sleep，可以响应取消；后面的第五个实验才是真实的线程数据库写入。

**slow_job()**

- **功能**：提供一个可等待的工作 B。这里用异步计时模拟耗时操作。
- **输入**：没有参数，等待时间固定为 2 秒。
- **输出**：正常时返回文字“B 的结果”；收到取消时抛出 CancelledError。
- **副作用**：等待和打印，不写数据库。

**wait_for_job(task, use_shield)**

- **功能**：作为任务 A 等待 B 的结果。
- **输入**：task 是 B 对应的任务对象；use_shield 决定是否用 shield 隔开取消传播。
- **输出**：正常时返回 B 的文字结果；取消时继续抛出取消异常。
- **副作用**：打印 A 的状态；未使用 shield 时，A 的取消会沿等待关系传播给 B。

**demo(use_shield)**

- **功能**：创建 A/B，取消 A，继续观察 B。
- **输入**：use_shield 为 False 时直接等待，为 True 时使用 shield。
- **输出**：没有有用返回值，终端展示结果。
- **副作用**：创建两个异步任务、取消 A、等待观察结果。

运行：

```bash
python3 -m learning.example_03_cancel_and_shield
```

完整代码：

```python
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
```

**运行后观察**：False 时，B 和 A 都打印收到取消。True 时，A 仍退出等待，但 B 最后正常完成。脚本中最后继续等待 B 的是实验程序，不是已经退出的 A。需要这样做，才能避免整个程序退出时取消还没完成的 B。

**动手改一处**：脚本会自动运行 False 和 True 两组。只在第一组的 `demo(False)` 中把 False 改成 True，确认两组都会保护 B。

## 4. 一行一行观察 finish_inflight

给你贴出的 finish_inflight 加上讲解用的打印，观察收到取消以后为什么会回到 while，以及 raise cancelled 为什么发生在工作结束以后。

`cancelled = None` 表示暂时没有记下取消。`except ... as error` 接住取消并把对象命名为 error。`cancelled = error` 只是记下来，没有结束函数。没有遇到 break，就会再次执行 while 内的等待。这里始终等待同一个 task，不会重新开始工作。`break` 跳出循环，`raise` 把异常传出去，`return` 正常返回值。

**finish_inflight(task)**

- **功能**：保护并等待当前任务；等待期间记下外层取消，内层正常结束后再向上抛出取消。
- **输入**：task 是已经安排执行的 B，对应本次需要等完的操作。
- **输出**：没有取消时返回 B 的结果；外层取消且 B 正常完成时抛出已记录的取消；B 报错时错误继续向上抛出。
- **副作用**：等待、记录内存中的取消对象、打印轨迹；不直接写数据库。

**slow_job()**

- **功能**：产生一个耗时 1 秒的结果，方便观察等待过程。
- **输入**：没有参数。
- **输出**：整数 42。
- **副作用**：异步等待并打印。

**demo(cancel_count)**

- **功能**：分别体验不取消、取消一次、取消两次。
- **输入**：cancel_count 是本次请求取消的次数，本例使用 0、1、2。
- **输出**：没有有用返回值；打印正常结果或最终取消。
- **副作用**：创建 B 和外层任务，按指定次数取消外层，等待结束。

运行：

```bash
python3 -m learning.example_04_finish_inflight
```

完整代码：

```python
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
```

**运行后观察**：取消一次时，输出会出现两次“进入 await”，但 B 只开始一次。取消两次时，进入 await 三次，B 仍只执行一次。最终先看到 B 正常完成，后看到“现在向上抛出记住的取消”。

**动手改一处**：只把最后的 `demo(2)` 改成 `demo(3)`，观察第三次取消仍然只增加一次等待，B 不会重新开始。

## 5. 使用项目真实的 run_sync 写 SQLite

把前面各部分放回真实使用场景：调用 /home/pl/deer_mini/backend/app/runtime/async_io.py 中的 run_sync，把消息写进独立的临时 SQLite 数据库。

这个例子导入项目真实的 run_sync。它用 create_task(to_thread(...)) 跟踪工作线程，再调用真实的 finish_inflight。这里的取消不会强行杀掉已经运行的数据库线程，当前写入仍可能提交。临时文件由 TemporaryDirectory 管理，退出场景以后清理，不使用项目的业务数据库。

**save_message(database_path, content, fail=False)**

- **功能**：执行一笔真实的数据库写入，提交以后返回记录编号。
- **输入**：database_path 是本次实验自己的临时数据库文件；content 是要保存的文字“你好”；fail 是人为触发数据库错误的开关，用来观察错误传播。
- **输出**：提交成功后返回整数编号 1；失败时抛出数据库异常。
- **副作用**：在线程内打开、写入、提交并关闭 SQLite；中间等待 1 秒以便观察取消。

**read_messages(database_path)**

- **功能**：重新从数据库读取实际保存的记录。
- **输入**：database_path 指向刚才那份临时数据库。
- **输出**：已保存记录的列表，例如 [(1, '你好')] 或 []。
- **副作用**：只查询数据库并关闭连接。

**demo(cancel, fail)**

- **功能**：运行一次场景，并用实际查询结果确认发生了什么。
- **输入**：cancel 为 True 时中途取消外层任务；fail 为 True 时让同步数据库函数在提交前报错。
- **输出**：没有有用返回值；打印消息编号、取消或数据库错误，以及数据库中实际存在的记录。
- **副作用**：创建临时数据库、调用真实 run_sync、可选地取消任务；最后清理临时目录。

运行：

```bash
python3 -m learning.example_05_real_run_sync
```

完整代码：

```python
"""实验 5：调用项目真实的 run_sync，写入独立的临时 SQLite 数据库。"""

import asyncio
import sqlite3
import tempfile
import time
from pathlib import Path

from app.runtime.async_io import run_sync


def save_message(database_path: str, content: str, fail: bool = False) -> int:
    """输入临时数据库路径、消息和失败开关；提交后返回编号；真实写 SQLite。"""
    # 在工作线程内创建、使用、关闭连接。
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS messages "
            "(id INTEGER PRIMARY KEY, content TEXT NOT NULL)"
        )
        cursor = connection.execute(
            "INSERT INTO messages(content) VALUES (?)", (content,)
        )
        print("数据库线程：已执行 INSERT，尚未提交")
        time.sleep(1)  # 留出时间，方便观察写入期间发生取消。

        if fail:
            raise sqlite3.OperationalError("实验指定：这次写入失败")

        connection.commit()
        print("数据库线程：提交成功")
        return cursor.lastrowid
    finally:
        # 发生错误而没有提交时，关闭连接会回滚这次未提交的写入。
        connection.close()


def read_messages(database_path: str) -> list:
    """输入临时数据库路径；返回已保存的消息；只查询并关闭连接。"""
    connection = sqlite3.connect(database_path)
    try:
        return connection.execute("SELECT id, content FROM messages ORDER BY id").fetchall()
    finally:
        connection.close()


async def demo(cancel: bool, fail: bool) -> None:
    """输入取消/失败开关；运行一次写入并读取结果；创建后清除临时数据库。"""
    print(f"\n本次实验：取消={cancel}，数据库报错={fail}")

    # 每个场景使用独立临时文件；退出 with 后自动清理。
    with tempfile.TemporaryDirectory(prefix="deer-mini-learning-") as directory:
        database_path = str(Path(directory) / "example.db")

        operation = asyncio.create_task(
            run_sync(save_message, database_path, "你好", fail=fail)
        )

        if cancel:
            await asyncio.sleep(0.3)
            print("外层：请求取消")
            operation.cancel()

        try:
            message_id = await operation
            print("外层：正常拿到消息编号", message_id)
        except asyncio.CancelledError:
            print("外层：收到取消；数据库线程此时已经结束")
        except sqlite3.OperationalError as error:
            print("外层：收到数据库错误：", error)

        rows = await run_sync(read_messages, database_path)
        print("重新读取数据库，实际保存的记录：", rows)


if __name__ == "__main__":
    asyncio.run(demo(cancel=False, fail=False))
    asyncio.run(demo(cancel=True, fail=False))
    asyncio.run(demo(cancel=True, fail=True))
```

**运行后观察**：三个场景分别应为：正常完成并返回编号 1，数据库有一行；中途取消，先提交成功再收到取消，数据库仍有一行；取消期间数据库报错，向上抛出数据库错误，查询结果为空。保存函数返回整数，读取函数返回列表，真实 run_sync 都会原样交还对应结果。

**动手改一处**：只把传给 run_sync 的文字“你好”改成“我正在学习 asyncio”。重新运行，确认前两个场景实际查询到的新文字。

## 对照回你原来的代码

最后一行的嵌套调用可以拆成四步。这里的 function 是要执行的同步函数，args/kwargs 是它需要的参数。正常时，整个函数返回同一份结果；取消和数据库错误的处理由 finish_inflight 完成。

```python
operation = asyncio.to_thread(function, *args, **kwargs)  # 准备线程操作
task = asyncio.create_task(operation)                    # 安排任务
result = await finish_inflight(task)                     # 妥善等待
return result                                            # 正常返回
```

`finish_inflight` 本身没有最长等待时间；同步操作不结束，它就可能继续等待。`shield` 只隔开等待者的取消传播，其他代码直接取消内层任务时仍会进入 task.cancelled() 分支。

查看本次新增文件可以在项目根目录执行 git status --short。它们尚未提交；先理解并运行例子，再根据你的学习进度决定是否提交。
