"""Thread、Run 和 SSE 的 FastAPI 路由。"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse

from app.api.schemas import (
    CheckpointResponse,
    CreateRunRequest,
    CreateThreadRequest,
    ModelProfileResponse,
    ModelsResponse,
    UpdateThreadRequest,
    RunResponse,
    ThreadResponse,
    WorkspaceFileResponse,
)
from app.domain.events import RunEvent
from app.model.config import DEFAULT_MODEL_NAME, MODEL_PROFILES
from app.repositories.checkpoint_repository import CheckpointRepository
from app.repositories.events_repository import EventRepository
from app.repositories.run_repository import RunRepository
from app.repositories.thread_repository import ThreadRepository
from app.runtime.stream_bridge import MemoryStreamBridge, StreamEvent
from app.services.run_coordinator import RunCoordinator
from app.services.thread_service import ThreadService
from app.services.workspace_file_service import (
    UnsafeWorkspacePathError,
    UploadTooLargeError,
    WorkspaceFileService,
)


router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)


@router.get("/models", response_model=ModelsResponse)
def list_models() -> ModelsResponse:
    """前端直接使用后端的配置名和默认模型，避免两边各自写一份列表。"""
    return ModelsResponse(
        default_model=DEFAULT_MODEL_NAME,
        models=[ModelProfileResponse.model_validate(profile) for profile in MODEL_PROFILES.values()],
    )


def _coordinator(request: Request) -> RunCoordinator:
    return request.app.state.run_coordinator


def _stream_bridge(request: Request) -> MemoryStreamBridge:
    return request.app.state.stream_bridge


def _require_owned_run(thread_id: str, run_id: str, user_id: str):
    """确认当前用户同时拥有 Thread 和其中的 Run。"""
    _require_owned_thread(thread_id, user_id)
    run = RunRepository().get(run_id, user_id)
    if run is None or run.thread_id != thread_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Thread 或 Run 不存在",
        )
    return run


def _require_owned_thread(thread_id: str, user_id: str):
    """确认 Thread 存在且属于当前用户。"""
    thread = ThreadRepository().get(thread_id, user_id)
    if thread is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Thread 不存在",
        )
    return thread


@router.post(
    "/threads",
    response_model=ThreadResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_thread(body: CreateThreadRequest) -> ThreadResponse:
    """创建一段对话及其独立文件目录。"""
    try:
        thread = ThreadService().create_thread(body.user_id, body.title)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error
    return ThreadResponse.model_validate(thread)


@router.get(
    "/threads",
    response_model=list[ThreadResponse],
)
def list_threads(
    user_id: str = Query(min_length=1),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[ThreadResponse]:
    """读取一个用户最近更新的对话列表。"""
    threads = ThreadRepository().list_for_user(
        user_id,
        limit=limit,
        offset=offset,
    )
    return [ThreadResponse.model_validate(thread) for thread in threads]


@router.get(
    "/threads/{thread_id}",
    response_model=ThreadResponse,
)
def get_thread(
    thread_id: str,
    user_id: str = Query(min_length=1),
) -> ThreadResponse:
    """读取一个属于当前用户的 Thread。"""
    return ThreadResponse.model_validate(
        _require_owned_thread(thread_id, user_id)
    )


@router.patch(
    "/threads/{thread_id}",
    response_model=ThreadResponse,
)
def update_thread(
    thread_id: str,
    body: UpdateThreadRequest,
    user_id: str = Query(min_length=1),
) -> ThreadResponse:
    """更新当前用户 Thread 的标题。"""
    _require_owned_thread(thread_id, user_id)
    try:
        thread = ThreadService().rename_thread(thread_id, user_id, body.title)
    except RuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error
    return ThreadResponse.model_validate(thread)


@router.delete(
    "/threads/{thread_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_thread(
    thread_id: str,
    request: Request,
    user_id: str = Query(min_length=1),
) -> None:
    """删除当前用户 Thread 及其 Workspace。"""
    _require_owned_thread(thread_id, user_id)
    try:
        await _coordinator(request).delete_thread(thread_id=thread_id, user_id=user_id)
    except RuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error


@router.post(
    "/threads/{thread_id}/runs",
    response_model=RunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_run(
    thread_id: str,
    body: CreateRunRequest,
    request: Request,
) -> RunResponse:
    """创建 Run，并让 Agent 在后台开始执行。"""
    if ThreadRepository().get(thread_id, body.user_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Thread 不存在",
        )

    try:
        run = await _coordinator(request).create_and_start_run(
            user_id=body.user_id,
            thread_id=thread_id,
            message=body.message,
            model_name=body.model_name,
            thinking_enabled=body.thinking_enabled,
            reasoning_effort=body.reasoning_effort,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error
    return RunResponse.model_validate(run)


@router.get(
    "/threads/{thread_id}/runs",
    response_model=list[RunResponse],
)
def list_runs(
    thread_id: str,
    user_id: str = Query(min_length=1),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[RunResponse]:
    """读取一个 Thread 中最近创建的 Run。"""
    _require_owned_thread(thread_id, user_id)
    runs = RunRepository().list_for_thread(
        thread_id,
        user_id,
        limit=limit,
        offset=offset,
    )
    return [RunResponse.model_validate(run) for run in runs]


@router.get(
    "/threads/{thread_id}/state",
    response_model=CheckpointResponse,
)
def get_latest_thread_state(
    thread_id: str,
    user_id: str = Query(min_length=1),
) -> CheckpointResponse:
    """读取 Thread 最新一次关键步骤保存的完整状态。"""
    _require_owned_thread(thread_id, user_id)
    checkpoint = CheckpointRepository().latest(thread_id, user_id)
    if checkpoint is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Thread 还没有 Checkpoint",
        )
    return CheckpointResponse.model_validate(checkpoint)


@router.get(
    "/threads/{thread_id}/runs/{run_id}/checkpoints",
    response_model=list[CheckpointResponse],
)
def list_run_checkpoints(
    thread_id: str,
    run_id: str,
    user_id: str = Query(min_length=1),
) -> list[CheckpointResponse]:
    """读取一次 Run 从用户消息到最终回答的关键状态历史。"""
    _require_owned_run(thread_id, run_id, user_id)
    checkpoints = CheckpointRepository().history(
        thread_id,
        user_id,
        run_id,
    )
    return [
        CheckpointResponse.model_validate(checkpoint)
        for checkpoint in checkpoints
    ]


@router.post(
    "/threads/{thread_id}/files",
    response_model=WorkspaceFileResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_workspace_file(
    thread_id: str,
    file: UploadFile = File(...),
    user_id: str = Query(min_length=1),
) -> WorkspaceFileResponse:
    """把浏览器上传的一个文件保存到当前 Thread Workspace。"""
    thread = _require_owned_thread(thread_id, user_id)

    async def chunks() -> AsyncIterator[bytes]:
        while chunk := await file.read(1024 * 1024):
            yield chunk

    try:
        stored_file = await WorkspaceFileService().save_upload(
            thread.workspace_path,
            file.filename or "",
            chunks(),
        )
    except UploadTooLargeError as error:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=str(error),
        ) from error
    except UnsafeWorkspacePathError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error
    except FileNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    finally:
        await file.close()

    return WorkspaceFileResponse.model_validate(stored_file)


@router.get(
    "/threads/{thread_id}/files",
    response_model=list[WorkspaceFileResponse],
)
def list_workspace_files(
    thread_id: str,
    user_id: str = Query(min_length=1),
) -> list[WorkspaceFileResponse]:
    """列出当前 Thread Workspace 中的普通文件。"""
    thread = _require_owned_thread(thread_id, user_id)
    try:
        files = WorkspaceFileService().list_files(thread.workspace_path)
    except FileNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    return [WorkspaceFileResponse.model_validate(item) for item in files]


@router.get("/threads/{thread_id}/files/{relative_path:path}")
def download_workspace_file(
    thread_id: str,
    relative_path: str,
    user_id: str = Query(min_length=1),
) -> FileResponse:
    """下载当前 Thread Workspace 中一个经过边界检查的文件。"""
    thread = _require_owned_thread(thread_id, user_id)
    try:
        target = WorkspaceFileService().resolve_download(
            thread.workspace_path,
            relative_path,
        )
    except UnsafeWorkspacePathError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error
    except FileNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件不存在",
        ) from error

    return FileResponse(path=target, filename=target.name)


@router.get(
    "/threads/{thread_id}/runs/{run_id}",
    response_model=RunResponse,
)
def get_run(
    thread_id: str,
    run_id: str,
    user_id: str = Query(min_length=1),
) -> RunResponse:
    """从 SQLite 读取 Run 的最新状态。"""
    run = _require_owned_run(thread_id, run_id, user_id)
    return RunResponse.model_validate(run)


@router.post(
    "/threads/{thread_id}/runs/{run_id}/cancel",
    response_model=RunResponse,
)
async def cancel_run(
    thread_id: str,
    run_id: str,
    request: Request,
    user_id: str = Query(min_length=1),
) -> RunResponse:
    """停止正在执行的 Agent Run。"""
    _require_owned_run(thread_id, run_id, user_id)
    try:
        run = await _coordinator(request).cancel_run(
            user_id=user_id,
            thread_id=thread_id,
            run_id=run_id,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except RuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error

    return RunResponse.model_validate(run)


def _encode_sse(event: StreamEvent) -> str:
    """把内存 StreamEvent 编码成浏览器可读取的 SSE 文本。"""
    if event.event == "__heartbeat__":
        return ": heartbeat\n\n"

    data = json.dumps(event.data, ensure_ascii=False, separators=(",", ":"))
    id_line = f"id: {event.id}\n" if event.id else ""
    return f"{id_line}event: {event.event}\ndata: {data}\n\n"


def _state_snapshot(thread_id: str, run_id: str, user_id: str, reason: str) -> StreamEvent:
    """给浏览器一份已保存的状态；这条控制消息不作为数据库日志。"""
    repo = CheckpointRepository()
    checkpoint = repo.latest_for_run(thread_id, run_id, user_id)
    run = _require_owned_run(thread_id, run_id, user_id)
    if checkpoint is None and run.status in {"pending", "running"}:
        checkpoint = repo.latest(thread_id, user_id)
    return StreamEvent("", "stream.reset", {
        "thread_id": thread_id, "run_id": run_id, "reason": reason,
        "checkpoint": CheckpointResponse.model_validate(checkpoint).model_dump()
        if checkpoint is not None else None,
    })


_TERMINAL_EVENTS = {
    "success": "run.end", "error": "run.error",
    "timeout": "run.timeout", "interrupted": "run.interrupted",
}


@router.get("/threads/{thread_id}/runs/{run_id}/events")
def stream_run_events(
    thread_id: str, run_id: str, request: Request,
    user_id: str = Query(min_length=1),
) -> StreamingResponse:
    """优先续传内存流；缓存丢失后用 Checkpoint 和实际日志恢复。"""
    run = _require_owned_run(thread_id, run_id, user_id)
    last_event_id = request.headers.get("last-event-id")
    bridge = _stream_bridge(request)

    async def snapshot(reason: str) -> str:
        event = await asyncio.to_thread(_state_snapshot, thread_id, run_id, user_id, reason)
        return _encode_sse(event)

    async def saved_process_events() -> AsyncIterator[str]:
        # 完整消息由快照恢复；辅助日志只补回确实保存过的工具等过程。
        try:
            persisted = await asyncio.to_thread(
                EventRepository().list_for_run, thread_id, run_id, user_id,
            )
        except Exception:
            logger.warning("读取 Run %s 的辅助历史日志失败", run_id, exc_info=True)
            return
        for event in persisted:
            if event.event_type in {"text.delta", "message.complete", *_TERMINAL_EVENTS.values()}:
                continue
            yield _encode_sse(StreamEvent(f"h:{event.id}", "run_event", event.to_dict()))

    async def event_stream() -> AsyncIterator[str]:
        terminal_sent = False
        # StreamingResponse 真正开始发送时，Run 可能已在后台完成，重新读取一次。
        current = await asyncio.to_thread(_require_owned_run, thread_id, run_id, user_id)
        if not last_event_id:
            # 首次连接也先交代已保存的完整消息，重播片段时可按 message_id 去重。
            yield await snapshot("initial")

        retained = await bridge.stream_exists(run_id)
        if retained or current.status in {"pending", "running"}:
            async for event in bridge.subscribe(run_id, last_event_id=last_event_id):
                if event.event == "__heartbeat__":
                    # 覆盖极短保留时间下的结束/清理竞争，避免等一个已经完成的 Run。
                    latest = await asyncio.to_thread(_require_owned_run, thread_id, run_id, user_id)
                    if latest.status in _TERMINAL_EVENTS:
                        break
                if event.event == "stream.gap":
                    yield await snapshot(str(event.data["reason"]))
                    async for saved_event in saved_process_events():
                        yield saved_event
                    continue
                if event.event == "run_event" and event.data.get("event_type") in _TERMINAL_EVENTS.values():
                    terminal_sent = True
                yield _encode_sse(event)
        else:
            if last_event_id:
                yield await snapshot("stream_lost")
            # 旧日志可能缺失；只恢复实际存在的过程记录，不重复拼接历史文字。
            async for saved_event in saved_process_events():
                yield saved_event

        if not terminal_sent:
            # 即使 run.end 日志丢失，也必须独立通知已经确认的真实终态。
            latest = await asyncio.to_thread(_require_owned_run, thread_id, run_id, user_id)
            if latest.status in _TERMINAL_EVENTS:
                terminal = RunEvent(
                    id=f"terminal:{run_id}:{latest.status}",
                    run_id=run_id, thread_id=thread_id,
                    event_type=_TERMINAL_EVENTS[latest.status],
                    payload={"status": latest.status, "status_confirmed": True,
                             **({"message": latest.error} if latest.error else {})},
                )
                yield _encode_sse(StreamEvent(terminal.id, "run_event", terminal.to_dict()))
            else:
                # 连接关闭只能表示流结束，不能凭空宣告 Run 成功。
                yield _encode_sse(StreamEvent("", "stream.unconfirmed", {
                    "run_id": run_id, "thread_id": thread_id,
                    "message": "输出连接已结束，但运行结果尚未确认，请稍后重新查询",
                }))

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
