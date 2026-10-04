"""实验 1B：相同的运行结果，带 T 的版本能提前检查出错误。"""

from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def get_number() -> int:
    """没有参数；返回整数 7；只做内存计算。"""
    return 7


def call_without_type(function):
    """输入一个无参数函数；调用并返回结果；没有返回类型说明。"""
    return function()


def call_with_type(function: Callable[..., T]) -> T:
    """输入一个无参数函数；返回它的结果；用 T 关联两边的结果类型。"""
    return function()


if __name__ == "__main__":
    without_type = call_without_type(get_number)
    with_type = call_with_type(get_number)

    print("没有 T，实际结果：", without_type, type(without_type).__name__)
    print("有 T，实际结果：", with_type, type(with_type).__name__)

    # upper() 是字符串的方法，整数不能使用。这里故意写错来做实验。
    # 捕获错误是为了让实验继续打印；类型检查器仍然可以指出错误。
    try:
        without_type.upper()
    except AttributeError:
        print("没有 T：运行到这里才发现，整数没有 upper()。")

    try:
        with_type.upper()
    except AttributeError:
        print("有 T：运行到这里同样会报错，但检查器能提前指出这一行。")
