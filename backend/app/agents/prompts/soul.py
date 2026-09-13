import html
from pathlib import Path


def load_agent_soul(soul_path: Path) -> str | None:
    """读取职责文件；文件不存在或为空时，表示没有自定义职责。"""
    try:
        content = soul_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None

    return content or None


def get_agent_soul(soul_path: Path) -> str:
    """生成 soul 段落。"""
    soul = load_agent_soul(soul_path)
    if not soul:
        return ""

    # 沿用原版转义方式，避免文件中的标签打断外层标签结构。
    escaped_soul = html.escape(soul, quote=False)
    return f"<soul>\n{escaped_soul}\n</soul>\n"