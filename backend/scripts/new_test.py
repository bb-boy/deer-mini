"""Create a non-overwriting pytest skeleton for an app module."""

from __future__ import annotations

import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR / "app"
TESTS_DIR = BACKEND_DIR / "tests"


def build_test_path(source_path: Path) -> tuple[Path, str]:
    """Map app/domain/messages.py to tests/domain/test_messages.py."""
    if source_path.suffix != ".py":
        raise ValueError("只能为 .py 文件创建测试骨架")

    relative_path = source_path.resolve().relative_to(APP_DIR)
    module_name = "app." + ".".join(relative_path.with_suffix("").parts)
    test_path = TESTS_DIR / relative_path.parent / f"test_{relative_path.stem}.py"
    return test_path, module_name


def render_skeleton(module_name: str, source_path: Path) -> str:
    return f'''"""Tests for {module_name}.

Source: {source_path}
"""

# 在这里编写 pytest 的 test_* 函数。
'''


def main(arguments: list[str]) -> int:
    if len(arguments) != 2:
        print("用法: python3 scripts/new_test.py app/模块路径.py", file=sys.stderr)
        return 1

    source_path = Path(arguments[1]).resolve()
    if not source_path.is_file():
        print(f"找不到源码文件: {source_path}", file=sys.stderr)
        return 1

    try:
        test_path, module_name = build_test_path(source_path)
    except ValueError as error:
        print(f"无法创建测试骨架: {error}", file=sys.stderr)
        return 1

    if test_path.exists():
        print(f"测试文件已存在，不覆盖: {test_path}", file=sys.stderr)
        return 1

    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(render_skeleton(module_name, source_path), encoding="utf-8")
    print(f"已创建测试骨架: {test_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
