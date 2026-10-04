# 亲眼看见 T 帮类型检查器做了什么

实际实验文件在 inA：/home/pl/deer_mini/backend/learning/example_01b_type_check.py。

这个实验只讲一件事：两个包装函数的函数体完全一样，都执行传入的函数并返回结果；其中一个通过 T 说明结果类型的关系，让检查器在运行之前发现错误。

“类型检查器”是一种只阅读代码、不执行业务逻辑的工具。这个实验使用 mypy。Python 正常执行文件时，不会因为写了 T 就自动检查或转换结果。

## 代码中的三个函数

**get_number()**

- 功能：提供一个确定会返回整数的函数，作为实验输入。
- 输入：没有参数。
- 输出：整数 7。
- 副作用：只返回内存中的值。

**call_without_type(function)**

- 功能：执行传入函数，交还结果，作为没有返回类型说明的对照组。
- 输入：function 是一个不需要参数的函数，本例传入 get_number。
- 输出：get_number 返回的整数 7，但这个包装函数没有提供可供检查器关联的结果类型说明。
- 副作用：执行传入函数；本例只做内存计算。

**call_with_type(function)**

- 功能：执行同样的操作，并用 T 关联输入函数的返回类型和包装函数的返回类型。
- 输入：function 同样是 get_number；Callable[..., T] 说明它的正常返回类型是 T。
- 输出：仍然是整数 7；-> T 让检查器把这个返回结果认定为整数。
- 副作用：与对照组相同。

`upper()` 是字符串的方法，用于把英文字母变成大写。整数没有这个方法，所以实验中两次调用 upper 都是故意写错的。

`try/except AttributeError` 接住这两次运行错误，让你能看到两组输出。接住运行错误，并不妨碍类型检查器提前指出代码中的错误。

## 完整代码

```python
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
```

## 先运行，再检查

在已经连接到 inA 的终端里，先执行：

```bash
python3 /home/pl/deer_mini/backend/learning/example_01b_type_check.py
```

两组运行结果都是 7，实际类型都是 int，而且错误调用 upper 时都会被 Python 拒绝。这说明 T 本身不改变程序执行。

再执行类型检查：

```bash
uv tool run --from mypy mypy --cache-dir=/dev/null /home/pl/deer_mini/backend/learning/example_01b_type_check.py
```

工具应该指出 `with_type.upper()` 这一行：整数没有 upper 属性。没有类型标注的包装函数让结果类型变得不明确，因此在这个例子和默认检查设置下，另一行没有同样的提前提示。

检查器报告错误时退出码为 1，这是本实验故意放入的错误，不是工具运行失败。

## 只改一处，再看结果

把：

```python
        with_type.upper()
```

改成：

```python
        print(with_type + 1)
```

再运行同一条检查命令，确认刚才的类型错误消失。运行 Python 文件时，这一处也会打印整数 8。

第一处没有类型标注的错误仍然存在。这个对照说明：工具只能根据它获得的类型信息检查代码，静态检查通过不意味着程序里所有问题都消失。

你项目里的 T 也是这个用途：persist 返回 Checkpoint，所以 run_sync(persist) 正常返回的结果也被识别为 Checkpoint。真正的数据传递由函数体中的 return 完成。
