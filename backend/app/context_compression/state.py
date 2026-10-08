"""可随主/子 Checkpoint 恢复的压缩元数据，以及先保存后发布的提交入口。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.runtime.async_io import finish_inflight
from app.runtime.errors import StatePersistenceError


if TYPE_CHECKING:
    from app.domain.threads import ThreadState
    from app.runtime.context import RuntimeContext


class CompressionState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)

    snipped_ids: list[str] = Field(default_factory=list)
    cleared_results: dict[str, str] = Field(default_factory=dict)
    summarized_ids: list[str] = Field(default_factory=list)
    summary: str | None = None
    summary_id: str | None = None
    growth_anchor_id: str | None = None
    growth_tokens: int = Field(default=0, ge=0)
    last_api_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    usage_model: str | None = None
    last_request_estimate: int = Field(default=0, ge=0)
    last_prompt_tokens: int | None = Field(default=None, gt=0)
    calibration_model: str | None = None
    calibration_ratio: float = Field(default=1.0, ge=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_summary(self) -> CompressionState:
        present = (bool(self.summary), bool(self.summary_id), bool(self.summarized_ids))
        if any(present) and not all(present):
            raise ValueError("摘要、摘要编号和覆盖消息必须同时存在")
        for values in (self.snipped_ids, self.summarized_ids, list(self.cleared_results)):
            if len(values) != len(set(values)) or any(not value.strip() for value in values):
                raise ValueError("压缩消息编号必须非空且唯一")
        return self


async def commit_compression(state: ThreadState, context: RuntimeContext, candidate: CompressionState) -> None:
    """保存当前状态的副本，确认后只替换压缩元数据；取消等待在途保存收尾。

    调用方必须在当前 Agent 的顺序阶段执行。子 Agent 的保存入口由执行器锁保护。
    输入 state/context 是当前作用域，candidate 不得与现有元数据共用可变容器。
    """
    candidate = CompressionState.model_validate(candidate.model_dump())

    async def persist() -> None:
        snapshot = deepcopy(state)
        snapshot.compression = candidate.model_copy(deep=True)
        try:
            await context.save_checkpoint(snapshot)
        except StatePersistenceError:
            raise
        except Exception as error:
            raise StatePersistenceError("上下文压缩状态未能确认保存") from error
        state.compression = candidate.model_copy(deep=True)

    await finish_inflight(asyncio.create_task(persist()))
