from typing import Literal, List
from dataclasses import dataclass, field
from app.domain.common import new_id, utc_now


RunStatus = Literal[
    "pending",
      "running",
      "success",
      "error",
      "interrupted",
      "timeout",]



@dataclass
class Run:
    id: str
    thread_id: str
    user_id: str
    status: RunStatus = "pending"
    model_name: str | None = None
    thinking_enabled: bool = True
    reasoning_effort: str | None = None
    error:str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)