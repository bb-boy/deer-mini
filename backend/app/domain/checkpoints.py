""""
Checkpoint
├── 属于哪个 Thread
├── 属于哪次 Run
├── 第几个步骤
├── 当时完整的 ThreadState
└── 创建时间


Run A：用户问“读取 report.txt”
  step = 1：保存用户消息
  step = 2：保存模型的工具调用请求
  step = 3：保存工具读取结果
  step = 4：保存最终回答

Run B：用户继续问“总结成三点”
  step = 1：保存新的用户消息
  step = 2：保存模型回答
同一个 Thread 可以有多个 Run
每个 Run 的 step 都从 1 重新开始


写入数据库前：None
写入数据库后：整数 ID，例如 41
  
"""



from dataclasses import dataclass, field
from typing import Any, Literal, List

from app.domain.common import utc_now
from app.domain.threads import ThreadState


@dataclass   #@dataclass 中，必填字段永远放在可选字段前面，
class Checkpoint:
    
    thread_id: str
    run_id: str
    step: int   #表示某次run的第几次存档
    state : ThreadState
    id: int | None = None  #在python中创建的checkpoint对象，id为None，写入数据库后，id为整数
    
    created_at: str = field(default_factory=utc_now)



