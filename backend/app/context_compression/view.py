"""按稳定消息 ID 生成活动上下文，工具交互不可拆开；不改动输入消息。"""

from dataclasses import replace

from app.context_compression.state import CompressionState
from app.domain.messages import Message


RECOVERABLE_TOOL_KEYS = frozenset({
    "read", "readfile", "bash", "grep", "glob", "websearch", "edit", "editfile", "write", "writefile",
})


def is_recoverable_tool(name: str) -> bool:
    """兼容 snake_case 工具名与用户列举的 Read/Bash 等大小写别名。"""
    return name.casefold().replace("_", "") in RECOVERABLE_TOOL_KEYS


SNIP_INSTRUCTIONS = """可用 snip 工具按 <message_id> 删除已无用的旧上下文。
不需要等待提醒。只选择已经不影响当前任务的旧信息，保留用户目标、约束、纠正和关键决定。
工具交互必须同时指定调用消息和其所有结果的 message_id；不能只删其中一条。
没有 message_id 标记的消息受到保护。原始聊天仍保留，Snip 只影响以后给模型的上下文。"""
SNIP_REMINDER = "新增上下文已达到 Snip 提醒间隔。请考虑是否有无用旧消息可用 snip 清理；若仍有用，直接继续任务。提醒不要求必须裁剪。"


def groups(messages: list[Message]) -> list[list[Message]]:
    result: list[list[Message]] = []
    for message in messages:
        if message.role == "tool" and result and result[-1][0].tool_calls:
            result[-1].append(message)
        else:
            result.append([message])
    return result


def eligible_ids(messages: list[Message]) -> set[str]:
    """只开放当前用户轮以前的完整交互；未知工具、失败和 Task/清单结果保护。"""
    current = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].role == "user"), 0)
    old_ids = {message.id for message in messages[:current]}
    eligible: set[str] = set()
    for group in groups(messages):
        if not all(message.id in old_ids for message in group):
            continue
        first = group[0]
        if any(message.role == "system" or message.is_error for message in group):
            continue
        if first.tool_calls:
            if any(not is_recoverable_tool(call.name) and call.name != "snip" for call in first.tool_calls):
                continue
            call_ids = [call.id for call in first.tool_calls]
            if len(call_ids) != len(set(call_ids)) or [m.tool_call_id for m in group[1:]] != call_ids:
                continue
        elif first.role == "tool":
            continue
        eligible.update(message.id for message in group)
    return eligible


def validate_snip(messages: list[Message], selected: set[str], state: CompressionState) -> None:
    if not selected <= {message.id for message in messages}:
        raise ValueError("Snip 包含未知或不属于当前上下文的消息 ID")
    pending = selected - set(state.snipped_ids)
    if pending & set(state.summarized_ids):
        raise ValueError("消息已纳入摘要，不能单独 Snip")
    if not pending <= eligible_ids(messages):
        raise ValueError("Snip 包含当前对话、不可重复结果或其他受保护消息")
    effective = selected | set(state.snipped_ids)
    for group in groups(messages):
        ids = {message.id for message in group}
        if ids & effective and not ids <= effective:
            raise ValueError("Snip 必须包含完整工具交互组的全部消息 ID")


def active_view(messages: list[Message], state: CompressionState) -> list[Message]:
    """连续 Snip 区段分别生成边界，摘要替代其覆盖消息；引用只替换工具正文。"""
    snipped, summarized = set(state.snipped_ids), set(state.summarized_ids)
    output = []
    in_snip = False
    summary_added = False
    for message in messages:
        if message.id in summarized:
            if not summary_added:
                output.append(Message(role="assistant", id=state.summary_id,
                    content="以下是此前对话的交接摘要；后续用户要求优先。\n" + state.summary))
                summary_added = True
            in_snip = False
            continue
        if message.id in snipped:
            if not in_snip:
                output.append(Message(role="assistant", id="snip-boundary-" + message.id,
                    content="[这之前的内容已被清理：此处一段旧消息已由 Snip 移出模型上下文。原始聊天仍保留。]"))
            in_snip = True
            continue
        in_snip = False
        path = state.cleared_results.get(message.id)
        if path is not None:
            message = replace(message, content=(
                "[旧工具结果正文已清理；原结果保存在 " + path
                + "，可用 read_tool_result 读取。不要重新执行有副作用的操作。]"
            ))
        output.append(message)
    return output


def decorate_snip(messages: list[Message], original: list[Message], *, reminder: bool) -> list[Message]:
    ids = eligible_ids(original)
    prepared = [replace(m, content=f"<message_id>{m.id}</message_id>\n{m.content}")
                if m.id in ids else m for m in messages]
    instructions = SNIP_INSTRUCTIONS + ("\n\n" + SNIP_REMINDER if reminder else "")
    for i, message in enumerate(prepared):
        if message.role == "system":
            prepared[i] = replace(message, content=message.content + "\n\n" + instructions)
            break
    else:
        prepared.insert(0, Message(role="system", content=instructions))
    return prepared
