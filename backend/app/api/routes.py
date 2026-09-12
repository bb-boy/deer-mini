"""Thread、Run 和 SSE 的 FastAPI 路由。"""

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse

from app.api.schemas import (
    CheckpointResponse,
    CreateRunRequest,
    CreateThreadRequest,
    UpdateThreadRequest,
    RunResponse,
    ThreadResponse,
    WorkspaceFileResponse,
)
from app.domain.events import RunEvent
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


def _persisted_stream_event(event: RunEvent) -> StreamEvent:
    """把 SQLite 中的 RunEvent 包装成 SSE 传输事件。"""
    return StreamEvent(
        id=event.id,
        event="run_event",
        data=event.to_dict(),
    )


def _replay_start_index(
    events: list[RunEvent],
    last_event_id: str | None,
) -> int:
    """找到 Last-Event-ID 后的第一条数据库事件；未知 ID 时从头回放。"""
    if not last_event_id:
        return 0

    for index, event in enumerate(events):
        if event.id == last_event_id:
            return index + 1
    return 0


@router.get("/threads/{thread_id}/runs/{run_id}/events")
def stream_run_events(
    thread_id: str,
    run_id: str,
    request: Request,
    user_id: str = Query(min_length=1),
) -> StreamingResponse:
    """订阅一次 Run 的文字、工具和结束事件。"""
    run = _require_owned_run(thread_id, run_id, user_id)
    last_event_id = request.headers.get("last-event-id")
    bridge = _stream_bridge(request)
    persisted_events = EventRepository().list_for_run(
        thread_id,
        run_id,
        user_id,
    )
    replay_start = _replay_start_index(
        persisted_events,
        last_event_id,
    )
    latest_persisted_sequence = max(
        (event.sequence or 0 for event in persisted_events),
        default=0,
    )

    async def event_stream() -> AsyncIterator[str]:
        # 先补发断线期间已经写入 SQLite 的可靠事件。
        for event in persisted_events[replay_start:]:
            yield _encode_sse(_persisted_stream_event(event))

        # 终态 Run 不会再产生事件，历史回放完成后立即关闭连接。
        if run.status not in {"pending", "running"}:
            return

        # 活跃 Run 再接内存实时流。快照中已有的 sequence 会被过滤，
        # 避免同一事件从 SQLite 和 MemoryStreamBridge 各发送一次。
        async for event in bridge.subscribe(run_id):
            if event.event == "run_event":
                sequence = event.data.get("sequence")
                if (
                    isinstance(sequence, int)
                    and sequence <= latest_persisted_sequence
                ):
                    continue
            yield _encode_sse(event)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
