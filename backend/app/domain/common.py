

from datetime import datetime, timezone
from uuid import uuid4



#生成唯一的id
def new_id() -> str: 
    return str(uuid4())


#生成统一的时间戳
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()