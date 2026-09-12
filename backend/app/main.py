"""deer_mini FastAPI 应用入口。"""

from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI

from app.api.routes import router
from app.infrastructure.database import initialize_database
from app.runtime.stream_bridge import MemoryStreamBridge
from app.services.run_coordinator import RunCoordinator


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时准备数据库，以及所有请求共享的运行时对象。"""
    # 后续对象会在构造时读取超时、Stream 保留时间和 Bash 开关，
    # 因此必须先把 .env 加载进当前进程。
    load_dotenv()
    initialize_database()
    stream_bridge = MemoryStreamBridge()
    app.state.stream_bridge = stream_bridge
    coordinator = RunCoordinator(stream_bridge)
    app.state.run_coordinator = coordinator
    try:
        await coordinator.start()
        # 当前是单进程 Mini：启动前仍处于 pending/running 的记录，
        # 已经失去负责执行它们的 asyncio Task，需要先统一收尾。
        await coordinator.recover_orphaned_runs()
        yield
    finally:
        try:
            await coordinator.shutdown()
        finally:
            await stream_bridge.close()


app = FastAPI(
    title="deer_mini API",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8005)
