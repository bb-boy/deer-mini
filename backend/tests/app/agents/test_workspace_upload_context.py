import asyncio
from app.agents.workspace_context_middleware import WorkspaceContextMiddleware
from app.domain.threads import ThreadState
from app.runtime.context import RuntimeContext
from app.tools.registry import ToolRegistry


def test_upload_context_is_current_thread_only_and_replaced_each_run(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "资料.txt").write_text("do not inject contents")
    (uploads / ".upload-hidden.part").write_text("partial")
    (uploads / "linked.txt").symlink_to(uploads / "资料.txt")
    state = ThreadState(thread_id="t1", user_id="u1", workspace_path=str(workspace), messages=[])
    context = RuntimeContext(user_id="u1", thread_id="t1", run_id="r1", workspace_path=str(workspace), record_event=None, save_checkpoint=None)
    middleware = WorkspaceContextMiddleware(ToolRegistry())
    asyncio.run(middleware.before_agent(state, context))
    asyncio.run(middleware.before_agent(state, context))
    assert len(state.messages) == 1
    assert "uploads/\\u8d44\\u6599.txt" in state.messages[0].content
    assert "do not inject contents" not in state.messages[0].content
    assert "linked.txt" not in state.messages[0].content
    assert ".upload-hidden.part" not in state.messages[0].content
