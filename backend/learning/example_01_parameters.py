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
