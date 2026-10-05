import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { RestoreOperation, RestorePoint, RestorePreview } from "../api/types";
import { RestorePoints } from "./RestorePoints";

const api = vi.hoisted(() => ({ list: vi.fn(), preview: vi.fn(), restore: vi.fn(), status: vi.fn() }));
vi.mock("../api/client", async (original) => ({
  ...await original<typeof import("../api/client")>(),
  listRestorePoints: api.list, previewRestorePoint: api.preview,
  restoreThread: api.restore, getRestoreOperation: api.status,
}));
const point: RestorePoint = { id: "point-1", run_id: "run-1", kind: "turn_start", message: "第一轮问题", created_at: "2026-10-05T10:00:00Z", available: true, unavailable_reason: null };
const preview: RestorePreview = { restore_point_id: point.id, revision: 2, fingerprint: "fingerprint-2", created: ["workspace/original.txt"], modified: ["outputs/report.txt"], deleted: ["uploads/later.pdf"], removed_messages: 4, target_messages: 0, current_messages: 4 };
const operation: RestoreOperation = { operation_id: "operation-1", thread_id: "thread-1", restore_point_id: point.id, recovery_point_id: "recovery-1", status: "committed", error: null, cleaned: true, created_at: point.created_at, updated_at: point.created_at };
const props = () => ({ threadId: "thread-1", userId: "alice", running: false, onCancelRun: vi.fn().mockResolvedValue(undefined), onRestored: vi.fn().mockResolvedValue(undefined), onClose: vi.fn(), onBusyChange: vi.fn() });
async function openPreview() { fireEvent.click(await screen.findByRole("button", { name: /预览恢复：第一轮问题/ })); await screen.findByText("将撤销 4 条消息，恢复后保留 0 条消息。"); }
beforeEach(() => {
  sessionStorage.clear();
  api.list.mockReset().mockResolvedValue([point]); api.preview.mockReset().mockResolvedValue(preview);
  api.restore.mockReset().mockResolvedValue(operation); api.status.mockReset().mockResolvedValue(operation);
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("RestorePoints", () => {
  it("shows the whole scope, first-turn preview, recovery points and unavailable reasons", async () => {
    api.list.mockResolvedValue([point, { ...point, id: "recovery-1", kind: "recovery", message: "恢复前备份" }, { ...point, id: "legacy", available: false, message: "旧轮次", unavailable_reason: "旧轮次没有文件备份" }]);
    render(<RestorePoints {...props()} />);
    expect(screen.getByRole("dialog", { name: "恢复对话与文件" })).toBeTruthy();
    expect(screen.getByText(/uploads、workspace、outputs/)).toBeTruthy();
    expect(screen.getByText(/后续上传的文件可能被删除/)).toBeTruthy();
    expect(screen.getByText(/环境、外部操作和长期记忆不会恢复/)).toBeTruthy();
    await openPreview();
    expect(screen.getByText("workspace/original.txt")).toBeTruthy();
    expect(screen.getByText("outputs/report.txt")).toBeTruthy();
    expect(screen.getByText("uploads/later.pdf")).toBeTruthy();
    expect(screen.getByText("恢复前备份", { selector: "strong" })).toBeTruthy();
    expect(screen.getByText("旧轮次没有文件备份")).toBeTruthy();
    expect((screen.getByRole("button", { name: /预览恢复：旧轮次/ }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("waits for run cancellation before obtaining a new preview", async () => {
    let finish!: () => void;
    const callbacks = props();
    callbacks.onCancelRun.mockImplementation(() => new Promise<void>((resolve) => { finish = resolve; }));
    render(<RestorePoints {...callbacks} running />);
    fireEvent.click(await screen.findByRole("button", { name: /预览恢复：第一轮问题/ }));
    expect(callbacks.onCancelRun).toHaveBeenCalledTimes(1);
    expect(api.preview).not.toHaveBeenCalled();
    await act(async () => { finish(); });
    await screen.findByText("将撤销 4 条消息，恢复后保留 0 条消息。");
    expect(api.preview).toHaveBeenCalledTimes(1);
    expect(api.restore).not.toHaveBeenCalled();
  });

  it("refreshes a stale preview and requires a second confirmation", async () => {
    api.restore.mockRejectedValueOnce(new ApiError("预览过期", 409));
    const callbacks = props(); render(<RestorePoints {...callbacks} />); await openPreview();
    api.preview.mockResolvedValue({ ...preview, revision: 3, fingerprint: "fingerprint-3", removed_messages: 5 });
    fireEvent.click(screen.getByRole("button", { name: "确认恢复消息与文件" }));
    await screen.findByText(/状态已变化，已刷新预览，请重新确认/);
    expect(api.restore).toHaveBeenCalledTimes(1);
    expect(callbacks.onRestored).not.toHaveBeenCalled();
    expect(screen.getByText("将撤销 5 条消息，恢复后保留 0 条消息。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "确认恢复消息与文件" }));
    await waitFor(() => expect(api.restore).toHaveBeenCalledTimes(2));
    expect(api.restore.mock.calls[1][2]).toMatchObject({ revision: 3, fingerprint: "fingerprint-3" });
  });

  it("does not claim the preview was refreshed if the conflict refresh also fails", async () => {
    api.restore.mockRejectedValueOnce(new ApiError("预览过期", 409));
    render(<RestorePoints {...props()} />); await openPreview();
    api.preview.mockRejectedValueOnce(new ApiError("仍在收尾", 409));
    fireEvent.click(screen.getByRole("button", { name: "确认恢复消息与文件" }));
    await screen.findByText("仍在收尾");
    expect(screen.queryByText(/已刷新预览/)).toBeNull();
    expect(screen.queryByRole("button", { name: "确认恢复消息与文件" })).toBeNull();
  });

  it("queries an ambiguous submission and retries only with the same operation id", async () => {
    api.restore.mockRejectedValueOnce(new TypeError("network disconnected"));
    api.status.mockRejectedValueOnce(new ApiError("not found", 404));
    const callbacks = props(); render(<RestorePoints {...callbacks} />); await openPreview();
    fireEvent.click(screen.getByRole("button", { name: "确认恢复消息与文件" }));
    await waitFor(() => expect(callbacks.onRestored).toHaveBeenCalledTimes(1));
    expect(api.restore).toHaveBeenCalledTimes(2);
    expect(api.restore.mock.calls[1][2]).toEqual(api.restore.mock.calls[0][2]);
    expect(api.status).toHaveBeenCalledWith("thread-1", api.restore.mock.calls[0][2].operation_id, "alice");
  });

  it("keeps an ambiguous operation busy when its idempotent retry conflicts before preparation", async () => {
    api.restore.mockRejectedValueOnce(new TypeError("network disconnected"))
      .mockRejectedValueOnce(new ApiError("恢复操作正在准备", 409));
    api.status.mockRejectedValueOnce(new ApiError("not found", 404));
    const callbacks = props(); render(<RestorePoints {...callbacks} />); await openPreview();
    vi.useFakeTimers();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "确认恢复消息与文件" })); });
    const input = api.restore.mock.calls[0][2];
    expect(api.restore.mock.calls[1][2]).toEqual(input);
    expect(api.preview).toHaveBeenCalledTimes(1);
    expect(callbacks.onBusyChange).toHaveBeenLastCalledWith(true);
    expect(callbacks.onRestored).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "确认恢复消息与文件" })).toBeNull();
    expect(JSON.parse(sessionStorage.getItem("deer-mini-restore:alice:thread-1")!)).toEqual(input);
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(api.status).toHaveBeenLastCalledWith("thread-1", input.operation_id, "alice");
    expect(callbacks.onRestored).toHaveBeenCalledTimes(1);
    expect(api.restore).toHaveBeenCalledTimes(2);
  });

  it("releases a rejected storage operation after confirming it was never recorded", async () => {
    api.restore.mockRejectedValue(new ApiError("磁盘空间不足", 507));
    api.status.mockRejectedValue(new ApiError("not found", 404));
    const callbacks = props(); render(<RestorePoints {...callbacks} />); await openPreview();
    vi.useFakeTimers();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "确认恢复消息与文件" })); });
    expect(api.status).toHaveBeenCalledTimes(1);
    expect(api.restore).toHaveBeenCalledTimes(1);
    expect(screen.getByText("磁盘空间不足")).toBeTruthy();
    expect(callbacks.onBusyChange).toHaveBeenLastCalledWith(false);
    expect(sessionStorage.getItem("deer-mini-restore:alice:thread-1")).toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(api.restore).toHaveBeenCalledTimes(1);
  });

  it("remembers a definitive rejection across a failed query and panel remount", async () => {
    api.restore.mockRejectedValueOnce(new ApiError("磁盘空间不足", 507));
    api.status.mockRejectedValueOnce(new TypeError("query disconnected"))
      .mockRejectedValueOnce(new ApiError("not found", 404));
    const first = props(); const firstView = render(<RestorePoints {...first} />); await openPreview();
    fireEvent.click(screen.getByRole("button", { name: "确认恢复消息与文件" }));
    await screen.findByText(/query disconnected/);
    expect(first.onBusyChange).toHaveBeenLastCalledWith(true);
    firstView.unmount();
    const second = props(); render(<RestorePoints {...second} />);
    await screen.findByText("磁盘空间不足");
    expect(api.restore).toHaveBeenCalledTimes(1);
    expect(second.onBusyChange).toHaveBeenLastCalledWith(false);
    expect(sessionStorage.getItem("deer-mini-restore:alice:thread-1:rejected")).toBeNull();
  });

  it("continues an existing committed operation even when its submission returned 507", async () => {
    api.restore.mockRejectedValueOnce(new ApiError("清理失败", 507));
    const callbacks = props(); render(<RestorePoints {...callbacks} />); await openPreview();
    fireEvent.click(screen.getByRole("button", { name: "确认恢复消息与文件" }));
    await waitFor(() => expect(callbacks.onRestored).toHaveBeenCalledTimes(1));
    expect(api.status).toHaveBeenCalledTimes(1);
    expect(api.restore).toHaveBeenCalledTimes(1);
  });

  it.each([400, 422])("queries an authoritative HTTP %s rejection without replaying it", async (status) => {
    api.restore.mockRejectedValueOnce(new ApiError("恢复参数无效", status));
    api.status.mockRejectedValueOnce(new ApiError("not found", 404));
    const callbacks = props(); render(<RestorePoints {...callbacks} />); await openPreview();
    fireEvent.click(screen.getByRole("button", { name: "确认恢复消息与文件" }));
    await screen.findByText("恢复参数无效");
    expect(api.status).toHaveBeenCalledTimes(1);
    expect(api.restore).toHaveBeenCalledTimes(1);
    expect(callbacks.onBusyChange).toHaveBeenLastCalledWith(false);
  });

  it("generates a safe operation id when randomUUID is unavailable on plain HTTP", async () => {
    vi.stubGlobal("crypto", { getRandomValues: (bytes: Uint8Array) => bytes.fill(7) });
    const callbacks = props(); render(<RestorePoints {...callbacks} />); await openPreview();
    fireEvent.click(screen.getByRole("button", { name: "确认恢复消息与文件" }));
    await waitFor(() => expect(callbacks.onRestored).toHaveBeenCalledTimes(1));
    expect(api.restore.mock.calls[0][2].operation_id).toMatch(/^[a-zA-Z0-9-]{32,}$/);
  });

  it("reports unavailable randomness without submitting or locking the conversation", async () => {
    vi.stubGlobal("crypto", {});
    const callbacks = props(); render(<RestorePoints {...callbacks} />); await openPreview();
    fireEvent.click(screen.getByRole("button", { name: "确认恢复消息与文件" }));
    await screen.findByText(/无法生成恢复操作编号/);
    expect(api.restore).not.toHaveBeenCalled();
    expect(callbacks.onBusyChange).toHaveBeenLastCalledWith(false);
  });

  it("waits for committed cleanup instead of declaring success early", async () => {
    const callbacks = props();
    api.restore.mockResolvedValue({ ...operation, cleaned: false });
    render(<RestorePoints {...callbacks} />); await openPreview();
    vi.useFakeTimers();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "确认恢复消息与文件" })); });
    expect(screen.getByText(/正在完成清理/)).toBeTruthy();
    expect(callbacks.onRestored).not.toHaveBeenCalled();
    expect(callbacks.onBusyChange).toHaveBeenLastCalledWith(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(callbacks.onRestored).toHaveBeenCalledTimes(1);
  });

  it.each(["rolled_back", "needs_recovery"] as const)("shows %s as failure without success callbacks", async (status) => {
    const callbacks = props(); api.restore.mockResolvedValue({ ...operation, status });
    render(<RestorePoints {...callbacks} />); await openPreview();
    fireEvent.click(screen.getByRole("button", { name: "确认恢复消息与文件" }));
    await screen.findByText(status === "rolled_back" ? /恢复未完成，已回到恢复前状态/ : /需要人工恢复/);
    expect(callbacks.onRestored).not.toHaveBeenCalled();
    expect(callbacks.onBusyChange).toHaveBeenLastCalledWith(status === "needs_recovery");
  });

  it("keeps the pending operation across closing and reopening the panel", async () => {
    api.restore.mockResolvedValue({ ...operation, status: "applying", cleaned: false });
    const first = props(); const firstView = render(<RestorePoints {...first} />); await openPreview();
    fireEvent.click(screen.getByRole("button", { name: "确认恢复消息与文件" }));
    await screen.findByRole("button", { name: "继续查询恢复结果" });
    const input = api.restore.mock.calls[0][2];
    firstView.unmount();
    const second = props(); render(<RestorePoints {...second} />);
    await waitFor(() => expect(second.onRestored).toHaveBeenCalledTimes(1));
    expect(api.status).toHaveBeenCalledWith("thread-1", input.operation_id, "alice");
    expect(api.restore).toHaveBeenCalledTimes(1);
  });

  it("prevents closing an unresolved restore when browser storage cannot preserve its id", async () => {
    api.restore.mockRejectedValueOnce(new TypeError("network disconnected"));
    api.status.mockRejectedValueOnce(new TypeError("query disconnected"));
    const callbacks = props(); render(<RestorePoints {...callbacks} />); await openPreview();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("storage denied"); });
    fireEvent.click(screen.getByRole("button", { name: "确认恢复消息与文件" }));
    await screen.findByText(/query disconnected/);
    expect((screen.getByRole("button", { name: "关闭恢复面板" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/无法保存操作编号，请保持恢复面板打开/)).toBeTruthy();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(callbacks.onClose).not.toHaveBeenCalled();
    expect(callbacks.onBusyChange).toHaveBeenLastCalledWith(true);
    const input = api.restore.mock.calls[0][2];
    fireEvent.click(screen.getByRole("button", { name: "继续查询恢复结果" }));
    await waitFor(() => expect(callbacks.onRestored).toHaveBeenCalledTimes(1));
    expect(api.status).toHaveBeenLastCalledWith("thread-1", input.operation_id, "alice");
  });

  it("bounds automatic polling and cancels timers on unmount", async () => {
    api.restore.mockResolvedValue({ ...operation, status: "applying", cleaned: false });
    api.status.mockResolvedValue({ ...operation, status: "applying", cleaned: false });
    const callbacks = props(); const { unmount } = render(<RestorePoints {...callbacks} />); await openPreview();
    vi.useFakeTimers();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "确认恢复消息与文件" })); });
    await act(async () => { await vi.advanceTimersByTimeAsync(160000); });
    expect(api.status).toHaveBeenCalledTimes(30);
    expect(screen.getByText(/恢复结果尚未确认，请继续查询/)).toBeTruthy();
    expect(callbacks.onRestored).not.toHaveBeenCalled();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "继续查询恢复结果" })); });
    expect(api.status).toHaveBeenCalledTimes(31);
    unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(api.status).toHaveBeenCalledTimes(31);
  });

  it("does not update the page when a submitted restore finishes after unmount", async () => {
    let finish!: (value: RestoreOperation) => void;
    api.restore.mockImplementation(() => new Promise<RestoreOperation>((resolve) => { finish = resolve; }));
    const callbacks = props(); const { unmount } = render(<RestorePoints {...callbacks} />); await openPreview();
    fireEvent.click(screen.getByRole("button", { name: "确认恢复消息与文件" }));
    unmount(); callbacks.onBusyChange.mockClear();
    await act(async () => { finish(operation); });
    expect(callbacks.onRestored).not.toHaveBeenCalled();
    expect(callbacks.onBusyChange).not.toHaveBeenCalled();
  });

  it("ignores late preview responses after unmount", async () => {
    let finish!: (value: RestorePreview) => void;
    api.preview.mockImplementation(() => new Promise<RestorePreview>((resolve) => { finish = resolve; }));
    const callbacks = props(); const { unmount } = render(<RestorePoints {...callbacks} />);
    fireEvent.click(await screen.findByRole("button", { name: /预览恢复：第一轮问题/ }));
    unmount(); callbacks.onBusyChange.mockClear();
    await act(async () => { finish(preview); });
    expect(callbacks.onBusyChange).not.toHaveBeenCalled();
    expect(callbacks.onRestored).not.toHaveBeenCalled();
  });
});
