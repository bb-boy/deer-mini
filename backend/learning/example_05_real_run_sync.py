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
