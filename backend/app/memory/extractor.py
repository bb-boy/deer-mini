"""从成功轮次抽取有用户原话依据的记忆，后端校验全部建议后保存。"""

import json
import re
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.threads import ThreadState
from app.memory.model_calls import call_memory_model, parse_json_object
from app.memory.prompts import EXTRACTION_PROMPT
from app.memory.store import MemoryStore, MAX_CONTENT_BYTES
from app.memory.operations import PreparedMemoryOperation, record_version
from app.model.base import ChatModel
from app.runtime.async_io import run_sync


_SECRET_PATTERN = re.compile(
    r"(?:api[_ -]?key|password|secret|access[_ -]?token)\s*(?:[:=：]|is\b|是|为)\s*\S+"
    r"|(?:密码|密钥|令牌)\s*(?:[:=：]|是|为)\s*\S+"
    r"|\bsk-[A-Za-z0-9_-]{6,}|\bgh[opusr]_[A-Za-z0-9]{20,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}|\bxox[baprs]-[A-Za-z0-9-]{10,}"
    r"|\bAKIA[A-Z0-9]{16}\b|-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE,
)


class MemoryChange(BaseModel):
    """模型只可提出 add/update，服务端拥有身份、路径和时间的最终控制。"""
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["add", "update"]
    memory_id: str | None = None
    type: Literal["user", "feedback", "reference"]
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=1000)
    content: str = Field(min_length=1, max_length=64_000)
    evidence: str = Field(min_length=1, max_length=4000)


class MemoryExtractor:
    """独立模型调用生成建议；不执行模型工具，不改变 Run/Thread 状态。"""

    def __init__(self, store: MemoryStore, model_factory: Callable[[], ChatModel],
                 *, timeout: float = 30.0) -> None:
        self.store, self.model_factory, self.timeout = store, model_factory, timeout

    async def prepare(self, state: ThreadState, run_id: str) -> list[PreparedMemoryOperation]:
        """从最新用户消息抽取长期信息，校验全部建议并返回可登记保存项。

        普通错误留给后台任务管理器记录，取消和文件提交错误继续传播。
        调用前捕获版本，准备时拒绝过期更新；此阶段不修复索引或写正文。
        """
        users = [message for message in state.messages if message.role == "user"]
        if not users:
            return []
        current_user = users[-1].content[-16_000:]
        if not current_user.strip():
            return []
        records = await run_sync(self.store.snapshot, state.user_id)
        snapshots = {record.id: record for record in records}
        existing, used = [], 2
        for record in records:
            item = {"memory_id": record.id, "type": record.type, "name": record.name,
                    "description": record.description}
            size = len(json.dumps(item, ensure_ascii=False)) + 2
            if used + size > 32_000:
                break
            existing.append(item)
            used += size
        # 仅有限的既有正文用于避免重复与理解更新，不把整库塞进提示词。
        body_used = 0
        for item in existing[:5]:
            record = snapshots[item["memory_id"]]
            if record is not None and body_used + len(record.content) <= 24_000:
                item["content"] = record.content
                body_used += len(record.content)
        recent = [message for message in state.messages
                  if message.role in {"user", "assistant"}][-8:]
        payload = {"current_user": current_user, "existing_memories": existing,
                   "conversation": [{"role": message.role, "content": message.content[-4000:]}
                                    for message in recent]}
        response = await call_memory_model(self.model_factory, prompt=EXTRACTION_PROMPT,
            payload=payload, state=state, run_id=run_id, timeout=self.timeout)
        result = parse_json_object(response.content)
        if set(result) != {"changes"} or not isinstance(result["changes"], list) or len(result["changes"]) > 10:
            raise ValueError("记忆抽取结果格式无效")
        changes = [MemoryChange.model_validate(item) for item in result["changes"]]
        targets = {item["memory_id"]: item for item in existing}
        updated = set()
        for change in changes:
            text_fields = (change.name, change.description, change.content, change.evidence)
            if not all(value.strip() for value in text_fields):
                raise ValueError("记忆字段不能为空白")
            try:
                encoded_fields = [value.encode("utf-8") for value in text_fields]
            except UnicodeError:
                raise ValueError("记忆包含无效字符") from None
            if change.evidence not in current_user:
                raise ValueError("记忆没有本轮用户原话依据")
            if _SECRET_PATTERN.search("\n".join((change.name, change.description, change.content, change.evidence))):
                raise ValueError("记忆内容疑似包含凭据")
            if len(encoded_fields[2]) > MAX_CONTENT_BYTES:
                raise ValueError("记忆正文超限")
            if change.type == "feedback" and not all(
                marker in change.content for marker in ("**Why:**", "**How to apply:**")
            ):
                raise ValueError("feedback 缺少原因或适用方式")
            if change.action == "add":
                if change.memory_id is not None:
                    raise ValueError("新增记忆不能指定永久 ID")
            else:
                target = targets.get(change.memory_id)
                if target is None or target["type"] != change.type or change.memory_id in updated:
                    raise ValueError("更新记忆目标无效或重复")
                updated.add(change.memory_id)
        return await run_sync(
            self.store.prepare, state.user_id,
            [change.model_dump(exclude={"evidence"}) for change in changes],
            source_thread_id=state.thread_id, source_run_id=run_id,
            expected_versions={item["memory_id"]: record_version(snapshots[item["memory_id"]])
                               for item in existing},
        )

    async def extract(self, state: ThreadState, run_id: str) -> None:
        """保留直接抽取保存接口；后台持久恢复使用 prepare 后登记任务。"""
        operations = await self.prepare(state, run_id)
        for operation in operations:
            await run_sync(self.store.apply_operation, state.user_id, operation)
