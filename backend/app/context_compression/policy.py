"""压缩策略的非秘密配置；容量/余量使用 token，空闲间隔使用秒。"""

import os
from dataclasses import dataclass, fields


@dataclass(frozen=True)
class CompressionPolicy:
    enabled: bool = True
    snip_enabled: bool = True
    context_window: int = 1_000_000
    compact_remaining: int = 33_000
    reminder_tokens: int = 10_000
    idle_seconds: int = 3600
    keep_tool_results: int = 5
    summary_tokens: int = 8000

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name in {"enabled", "snip_enabled"}:
                if type(value) is not bool:
                    raise ValueError(f"{item.name} 必须是布尔值")
            elif type(value) is not int or value <= 0:
                raise ValueError(f"{item.name} 必须是正整数")
        if not self.summary_tokens < self.compact_remaining < self.context_window:
            raise ValueError("必须满足 summary_tokens < compact_remaining < context_window")

    @classmethod
    def from_env(cls) -> "CompressionPolicy":
        values = {}
        for item in fields(cls):
            name = "DEER_MINI_CONTEXT_" + item.name.upper()
            raw = os.getenv(name)
            if raw is None:
                continue
            if item.name in {"enabled", "snip_enabled"}:
                if raw.lower().strip() not in {"true", "false", "1", "0"}:
                    raise ValueError(f"{name} 必须是 true 或 false")
                values[item.name] = raw.lower().strip() in {"true", "1"}
            else:
                try:
                    values[item.name] = int(raw)
                except ValueError:
                    raise ValueError(f"{name} 必须是整数") from None
        return cls(**values)
