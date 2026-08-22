"""Create a non-overwriting pytest skeleton for any project Python file."""

from __future__ import annotations

import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent
TESTS_DIR = BACKEND_DIR / "tests"
IGNORED_DIRECTORIES = frozenset({".git", ".venv", "__pycache__", "tests"})


def build_test_path(source_path: Path) -> tuple[Path, Path]:
    """Map a project source path to its mirrored path below backend/tests."""
    if source_path.suffix != ".py":
        raise ValueError("只能为 .py 文件创建测试骨架")

    relative_path = source_path.resolve().relative_to(PROJECT_DIR)
    if any(part in IGNORED_DIRECTORIES for part in relative_path.parts):
        raise ValueError("不能为测试、虚拟环境、缓存或 Git 目录中的文件创建测试骨架")

    if relative_path.parts[0] == "backend":
        test_relative_path = Path(*relative_path.parts[1:])
    else:
        test_relative_path = Path("project") / relative_path

    test_path = (
        TESTS_DIR
        / test_relative_path.parent
        / f"test_{test_relative_path.stem}.py"
    )
    return test_path, relative_path


def render_skeleton(source_path: Path) -> str:
    return f'''"""Tests for {source_path}."""

# 在这里编写 pytest 的 test_* 函数。
'''


def main(arguments: list[str]) -> int:
    if len(arguments) != 2:
        print("用法: python3 scripts/new_test.py <项目中的 .py 文件>", file=sys.stderr)
        return 1

    source_path = Path(arguments[1]).resolve()
    if not source_path.is_file():
        print(f"找不到源码文件: {source_path}", file=sys.stderr)
        return 1

    try:
        test_path, relative_path = build_test_path(source_path)
    except ValueError as error:
        print(f"无法创建测试骨架: {error}", file=sys.stderr)
        return 1

    if test_path.exists():
        print(f"测试文件已存在，不覆盖: {test_path}", file=sys.stderr)
        return 1

    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(render_skeleton(relative_path), encoding="utf-8")
    print(f"已创建测试骨架: {test_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
