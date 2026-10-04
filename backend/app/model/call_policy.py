"""模型请求策略；环境变量只包含数字，不读取密钥。"""

import math
import os
from dataclasses import dataclass, fields


@dataclass(frozen=True)
class ModelCallPolicy:
    max_attempts: int = 3  # 含第一次请求
    base_delay_seconds: float = 1.0
    delay_cap_seconds: float = 8.0
    burst_delay_seconds: float = 5.0
    max_wait_seconds: float = 60.0
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 120.0
    write_timeout_seconds: float = 30.0
    pool_timeout_seconds: float = 10.0
    attempt_timeout_seconds: float = 300.0
    call_timeout_seconds: float = 420.0

    def __post_init__(self):
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 10:
            raise ValueError("max_attempts 必须是 1～10 的整数")
        for item in fields(self):
            if item.name == "max_attempts":
                continue
            value = getattr(self, item.name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{item.name} 必须是大于 0 的有限数字")

    @classmethod
    def from_env(cls):
        values = {}
        for item in fields(cls):
            key = "DEER_MINI_MODEL_" + item.name.upper()
            raw = os.getenv(key)
            if raw is not None and raw.strip():
                try:
                    values[item.name] = int(raw) if item.name == "max_attempts" else float(raw)
                except ValueError:
                    raise ValueError(f"{key} 必须是有效数字") from None
        return cls(**values)
