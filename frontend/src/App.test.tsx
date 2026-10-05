import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Checkpoint, Thread, WorkspaceFile } from "./api/types";


const listThreadsMock = vi.fn();
const getModelsMock = vi.fn();
const createThreadMock = vi.fn();
const getLatestStateMock = vi.fn();
const listRunsMock = vi.fn();
const listWorkspaceFilesMock = vi.fn();
const uploadWorkspaceFileMock = vi.fn();
const startAgentRunMock = vi.fn();
const renameThreadMock = vi.fn();
const deleteThreadMock = vi.fn();
const clearTransientMock = vi.fn();
const listRestorePointsMock = vi.fn();
const previewRestorePointMock = vi.fn();
const restoreThreadMock = vi.fn();
let running = false;
type AgentRunOptions = Parameters<typeof import("./hooks/useAgentRun").useAgentRun>[0];
let agentRunOptions: AgentRunOptions;

vi.mock("./api/client", async (original) => ({
  ...await original<typeof import("./api/client")>(),
  listRestorePoints: (...args: unknown[]) => listRestorePointsMock(...args),
  previewRestorePoint: (...args: unknown[]) => previewRestorePointMock(...args),
  restoreThread: (...args: unknown[]) => restoreThreadMock(...args),
  getModels: (...args: unknown[]) => getModelsMock(...args),
  listThreads: (...args: unknown[]) => listThreadsMock(...args),
  createThread: (...args: unknown[]) => createThreadMock(...args),
  getLatestState: (...args: unknown[]) => getLatestStateMock(...args),
  listRuns: (...args: unknown[]) => listRunsMock(...args),
  listWorkspaceFiles: (...args: unknown[]) => listWorkspaceFilesMock(...args),
  renameThread: (...args: unknown[]) => renameThreadMock(...args),
  deleteThread: (...args: unknown[]) => deleteThreadMock(...args),
  uploadWorkspaceFile: (...args: unknown[]) => uploadWorkspaceFileMock(...args),
  workspaceFileDownloadUrl: vi.fn(() => "/download"),
}));

vi.mock("./hooks/useAgentRun", () => ({
  useAgentRun: (options: AgentRunOptions) => {
    agentRunOptions = options;
    return {
    currentRun: null,
    pendingUserMessage: null,
    liveAssistantText: "",
    liveMessages: [],
    toolEvents: [],
    running,
    error: null,
    connectionNotice: null,
    modelNotices: [],
    start: (...args: unknown[]) => startAgentRunMock(...args),
    resume: vi.fn(),
    cancel: vi.fn(),
    clearTransient: clearTransientMock,
    };
  },
}));

import App from "./App";


const thread: Thread = {
  id: "thread-1",
  user_id: "demo-user",
  workspace_path: "/tmp/workspace",
  title: "报告分析",
  status: "idle",
  created_at: "2026-01-01T00:00:00+00:00",
  updated_at: "2026-01-01T00:00:00+00:00",
};

const checkpoint: Checkpoint = {
  id: 1,
  thread_id: thread.id,
  run_id: "run-1",
  step: 2,
  created_at: "2026-01-01T00:00:00+00:00",
  state: {
    thread_id: thread.id,
    user_id: "demo-user",
    workspace_path: thread.workspace_path,
    messages: [
      {
        id: "message-1",
        role: "assistant",
        content: "历史回答",
        created_at: "2026-01-01T00:00:00+00:00",
        tool_calls: [],
        tool_call_id: null,
        reasoning_content: null,
      },
    ],
  },
};

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  running = false;
  clearTransientMock.mockReset();
  listRestorePointsMock.mockReset().mockResolvedValue([{ id: "point-1", run_id: "run-1", kind: "turn_start", message: "第一轮问题", created_at: "2026-10-05T00:00:00Z", available: true, unavailable_reason: null }]);
  previewRestorePointMock.mockReset().mockResolvedValue({ restore_point_id: "point-1", revision: 2, fingerprint: "abc", created: [], modified: [], deleted: [], removed_messages: 1, target_messages: 0, current_messages: 1 });
  restoreThreadMock.mockReset().mockResolvedValue({ operation_id: "operation-1", status: "committed", cleaned: true });
  window.history.replaceState(null, "", "/");
  getModelsMock.mockReset().mockResolvedValue({
    default_model: "ustc-deepseek-flash",
    models: [
      { name: "ustc-deepseek-flash", display_name: "USTC Flash", supports_thinking: true, supports_reasoning_effort: true },
      { name: "siliconflow-deepseek-flash", display_name: "SiliconFlow Flash", supports_thinking: true, supports_reasoning_effort: true },
    ],
  });
  listThreadsMock.mockReset().mockResolvedValue([thread]);
  createThreadMock.mockReset().mockResolvedValue(thread);
  getLatestStateMock.mockReset().mockResolvedValue(checkpoint);
  listRunsMock.mockReset().mockResolvedValue([]);
  listWorkspaceFilesMock.mockReset().mockResolvedValue([]);
  uploadWorkspaceFileMock.mockReset().mockResolvedValue(undefined);
  startAgentRunMock.mockReset().mockResolvedValue(undefined);
  renameThreadMock.mockReset();
  deleteThreadMock.mockReset();
});

describe("App", () => {
  it("restores messages and file cards while invalidating an older refresh and file preview", async () => {
    const file = { name: "report.pdf", relative_path: "outputs/report.pdf", size: 8, modified_at: thread.updated_at };
    listWorkspaceFilesMock.mockResolvedValue([file]);
    render(<App />);
    await screen.findByText("历史回答");
    fireEvent.click(screen.getByRole("tab", { name: /文件/, hidden: true }));
    fireEvent.click(screen.getByRole("button", { name: "预览 report.pdf", hidden: true }));
    expect(screen.getByRole("region", { name: "文件预览 report.pdf", hidden: true })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("添加附件"), { target: { files: [new File(["queued"], "queued.txt")] } });
    expect(screen.getByLabelText("待上传附件")).toBeTruthy();

    let releaseFiles!: (files: WorkspaceFile[]) => void;
    listWorkspaceFilesMock.mockImplementationOnce(() => new Promise<WorkspaceFile[]>((resolve) => { releaseFiles = resolve; }));
    let oldRefresh!: Promise<void> | void;
    act(() => { oldRefresh = agentRunOptions.onSettled(thread.id); });
    fireEvent.click(screen.getByRole("button", { name: "恢复对话与文件" }));
    fireEvent.click(await screen.findByRole("button", { name: /预览恢复：第一轮问题/ }));
    await screen.findByText("将撤销 1 条消息，恢复后保留 0 条消息。");
    getLatestStateMock.mockResolvedValue({ ...checkpoint, state: { ...checkpoint.state, messages: [] } });
    listWorkspaceFilesMock.mockResolvedValue([file]);
    fireEvent.click(screen.getByRole("button", { name: "确认恢复消息与文件" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "恢复对话与文件" })).toBeNull());
    expect(clearTransientMock).toHaveBeenCalled();
    expect(screen.queryByText("历史回答")).toBeNull();
    expect(screen.queryByLabelText("待上传附件")).toBeNull();
    expect(screen.queryByRole("region", { name: "文件预览 report.pdf", hidden: true })).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: /文件/, hidden: true }));
    expect(screen.getByRole("button", { name: "预览 report.pdf", hidden: true })).toBeTruthy();
    await act(async () => { releaseFiles([{ ...file, name: "stale.pdf", relative_path: "outputs/stale.pdf" }]); await oldRefresh; });
    expect(screen.queryByText("历史回答")).toBeNull();
    expect(screen.queryByText("stale.pdf")).toBeNull();
  });

  it("does not apply a restore response after switching to another thread", async () => {
    const other = { ...thread, id: "thread-2", title: "另一段对话" };
    listThreadsMock.mockResolvedValue([thread, other]);
    getLatestStateMock.mockImplementation((id: string) => Promise.resolve({ ...checkpoint, state: { ...checkpoint.state, messages: [{ ...checkpoint.state.messages[0], content: id === thread.id ? "历史回答" : "另一段回答" }] } }));
    let finish!: (value: unknown) => void;
    restoreThreadMock.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    render(<App />); await screen.findByText("历史回答");
    fireEvent.click(screen.getByRole("button", { name: "恢复对话与文件" }));
    fireEvent.click(await screen.findByRole("button", { name: /预览恢复：第一轮问题/ }));
    await screen.findByRole("button", { name: "确认恢复消息与文件" });
    fireEvent.click(screen.getByRole("button", { name: "确认恢复消息与文件" }));
    await waitFor(() => expect(restoreThreadMock).toHaveBeenCalledTimes(1));
    act(() => { window.location.hash = "thread=thread-2"; window.dispatchEvent(new HashChangeEvent("hashchange")); });
    await screen.findByText("另一段回答");
    clearTransientMock.mockClear();
    await act(async () => { finish({ status: "committed", cleaned: true }); });
    expect(screen.getByText("另一段回答")).toBeTruthy();
    expect(clearTransientMock).not.toHaveBeenCalled();
    expect((screen.getByLabelText("给 Agent 的消息") as HTMLTextAreaElement).disabled).toBe(false);
  });

  it("preserves an unflushed draft when switching threads", async () => {
    const other = { ...thread, id: "thread-2", title: "另一段对话" };
    listThreadsMock.mockResolvedValue([thread, other]);
    render(<App />); await screen.findByText("历史回答");
    fireEvent.change(screen.getByLabelText("给 Agent 的消息"), { target: { value: "未发送草稿" } });
    fireEvent.click(screen.getByRole("button", { name: /另一段对话/ }));
    expect(sessionStorage.getItem(`deer-mini-draft:${thread.user_id}:${thread.id}`)).toBe("未发送草稿");
  });

  it("ignores an older refresh failure after restoring the conversation", async () => {
    render(<App />); await screen.findByText("历史回答");
    let rejectFiles!: (error: Error) => void;
    listWorkspaceFilesMock.mockImplementationOnce(() => new Promise<WorkspaceFile[]>((_resolve, reject) => { rejectFiles = reject; }));
    let olderRefresh!: Promise<void> | void;
    act(() => { olderRefresh = agentRunOptions.onSettled(thread.id); });
    fireEvent.click(screen.getByRole("button", { name: "恢复对话与文件" }));
    fireEvent.click(await screen.findByRole("button", { name: /预览恢复：第一轮问题/ }));
    await screen.findByRole("button", { name: "确认恢复消息与文件" });
    fireEvent.click(screen.getByRole("button", { name: "确认恢复消息与文件" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "恢复对话与文件" })).toBeNull());
    await act(async () => {
      rejectFiles(new Error("旧文件刷新失败"));
      await Promise.resolve(olderRefresh).catch(() => undefined);
    });
    expect(screen.queryByText("旧文件刷新失败")).toBeNull();
  });

  it("prevents uploads while a run is active and explains why", async () => {
    running = true;
    render(<App />);
    await screen.findByText("历史回答");
    fireEvent.click(screen.getByRole("tab", { name: /文件/, hidden: true }));
    expect((screen.getByLabelText("上传文件") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText("添加附件") as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByText(/运行期间不能上传文件，请等待运行结束/)).toBeTruthy();
  });
  it("clears the previous thread's file cards while the next thread loads", async () => {
    const otherThread = { ...thread, id: "thread-2", title: "另一段对话" };
    const oldFile = { name: "previous.txt", relative_path: "uploads/previous.txt", size: 8, modified_at: thread.updated_at };
    let releaseFiles!: (files: WorkspaceFile[]) => void;
    listThreadsMock.mockResolvedValue([thread, otherThread]);
    listWorkspaceFilesMock.mockImplementation((threadId: string) => threadId === thread.id
      ? Promise.resolve([oldFile])
      : new Promise<WorkspaceFile[]>((resolve) => { releaseFiles = resolve; }));
    render(<App />);
    await screen.findByText("历史回答");
    fireEvent.click(screen.getByRole("tab", { name: /文件/, hidden: true }));
    expect(screen.getByRole("button", { name: "预览 previous.txt", hidden: true })).toBeTruthy();

    // B 的文件请求尚未返回时，不能把 A 的卡片配上 B 的下载地址。
    fireEvent.click(screen.getByRole("button", { name: /另一段对话/ }));
    fireEvent.click(screen.getByRole("tab", { name: /文件/, hidden: true }));
    const staleCard = screen.queryByRole("button", { name: "预览 previous.txt", hidden: true });
    await act(async () => { releaseFiles([]); });
    expect(staleCard).toBeNull();
  });

  it("keeps the current thread's files when a previous thread's upload finishes late", async () => {
    const otherThread = { ...thread, id: "thread-2", title: "另一段对话" };
    const uploaded = { name: "old-upload.txt", relative_path: "uploads/old-upload.txt", size: 8, modified_at: thread.updated_at };
    const currentFile = { name: "current.txt", relative_path: "outputs/current.txt", size: 8, modified_at: thread.updated_at };
    let releaseUpload!: (file: WorkspaceFile) => void;
    listThreadsMock.mockResolvedValue([thread, otherThread]);
    listWorkspaceFilesMock.mockImplementation((threadId: string) => Promise.resolve(threadId === thread.id ? [] : [currentFile]));
    uploadWorkspaceFileMock.mockImplementation(() => new Promise<WorkspaceFile>((resolve) => { releaseUpload = resolve; }));
    render(<App />);
    await screen.findByText("历史回答");
    fireEvent.click(screen.getByRole("tab", { name: /文件/, hidden: true }));
    fireEvent.change(screen.getByLabelText("上传文件"), { target: { files: [new File(["original"], uploaded.name)] } });
    expect(uploadWorkspaceFileMock).toHaveBeenCalledWith(thread.id, thread.user_id, expect.any(File));

    // 上传 A 时切到 B；即使 A 后来成功，也只能更新 A 的文件。
    fireEvent.click(screen.getByRole("button", { name: /另一段对话/ }));
    await waitFor(() => expect(listWorkspaceFilesMock).toHaveBeenCalledWith(otherThread.id, thread.user_id));
    fireEvent.click(screen.getByRole("tab", { name: /文件/, hidden: true }));
    await screen.findByRole("button", { name: "预览 current.txt", hidden: true });
    listWorkspaceFilesMock.mockImplementation((threadId: string) => Promise.resolve(threadId === thread.id ? [uploaded] : [currentFile]));
    await act(async () => { releaseUpload(uploaded); });

    expect(screen.queryByRole("button", { name: "预览 old-upload.txt", hidden: true })).toBeNull();
    expect(screen.getByRole("button", { name: "预览 current.txt", hidden: true })).toBeTruthy();
  });

  it("refreshes backend model defaults when returning to the page", async () => {
    render(<App />);
    await screen.findByRole("option", { name: "默认 · USTC Flash" });
    getModelsMock.mockResolvedValue({
      default_model: "siliconflow-deepseek-flash",
      models: [
        { name: "siliconflow-deepseek-flash", display_name: "SiliconFlow Flash", supports_thinking: true, supports_reasoning_effort: true },
      ],
    });
    fireEvent.focus(window);
    const option = await screen.findByRole("option", { name: "默认 · SiliconFlow Flash" });
    expect((option as HTMLOptionElement).selected).toBe(true);
    fireEvent.change(screen.getByLabelText("给 Agent 的消息"), { target: { value: "使用新的默认模型" } });
    fireEvent.click(screen.getByRole("button", { name: "发送任务" }));
    await waitFor(() => expect(startAgentRunMock).toHaveBeenCalledWith(
      expect.objectContaining({ modelName: "siliconflow-deepseek-flash" }),
      expect.anything(),
    ));
  });

  it("keeps a newer stream snapshot when an older refresh finishes late", async () => {
    render(<App />);
    await screen.findByText("历史回答");

    let releaseFiles!: (files: WorkspaceFile[]) => void;
    listWorkspaceFilesMock.mockImplementationOnce(() => new Promise<WorkspaceFile[]>((resolve) => {
      releaseFiles = resolve;
    }));
    let olderRefresh!: Promise<void> | void;
    act(() => { olderRefresh = agentRunOptions.onSettled(thread.id); });

    const newerCheckpoint: Checkpoint = {
      ...checkpoint, id: 2, run_id: "run-2", step: 1,
      state: {
        ...checkpoint.state,
        messages: [...checkpoint.state.messages, {
          ...checkpoint.state.messages[0], id: "new-user", role: "user", content: "下一轮问题",
        }],
      },
    };
    act(() => agentRunOptions.onSnapshot?.(thread.id, newerCheckpoint));
    expect(screen.getByText("下一轮问题")).toBeTruthy();

    await act(async () => {
      releaseFiles([{ name: "report.txt", relative_path: "report.txt", size: 8, modified_at: thread.updated_at }]);
      await olderRefresh;
    });
    expect(screen.getByText("下一轮问题")).toBeTruthy();
    // 只淘汰过时的消息响应；同次刷新的文件仍能显示。
    fireEvent.click(screen.getByRole("tab", { name: /文件/, hidden: true }));
    expect(screen.getAllByText("report.txt").length).toBeGreaterThan(0);
  });

  it("loads the first thread and restores messages from the latest checkpoint", async () => {
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: /报告分析/ }));

    await waitFor(() => expect(getLatestStateMock).toHaveBeenCalledWith("thread-1", "demo-user"));
    expect(await screen.findByText("历史回答")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "报告分析" })).toBeTruthy();
  });

  it("creates a thread when the first message is sent", async () => {
    listThreadsMock.mockResolvedValue([]);
    render(<App />);

    const composer = await screen.findByLabelText("给 Agent 的消息");
    await waitFor(() => expect((composer as HTMLTextAreaElement).disabled).toBe(false));
    fireEvent.change(composer, { target: { value: "分析 report.txt" } });
    fireEvent.click(screen.getByRole("button", { name: "发送任务" }));

    await waitFor(() => expect(createThreadMock).toHaveBeenCalledWith("demo-user", "分析 report.txt"));
    expect(startAgentRunMock).toHaveBeenCalledWith(
      expect.objectContaining({ message: "分析 report.txt" }),
      thread,
    );
  });

  it("keeps a successful rename when refreshing the list fails", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "报告分析" });
    renameThreadMock.mockResolvedValue({ ...thread, title: "新的标题" });
    listThreadsMock.mockRejectedValueOnce(new Error("refresh failed"));

    fireEvent.click(screen.getByTitle("更多操作：报告分析"));
    fireEvent.click(screen.getByRole("menuitem", { name: "重命名" }));
    fireEvent.change(screen.getByLabelText("对话名称"), {
      target: { value: "新的标题" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    expect(await screen.findByRole("heading", { name: "新的标题" })).toBeTruthy();
    expect((await screen.findByRole("alert")).textContent).toContain(
      "标题已更新，但重新加载对话列表失败",
    );
    expect(screen.queryByRole("dialog", { name: "重命名对话" })).toBeNull();
  });

  it("removes a successfully deleted thread when refreshing the list fails", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "报告分析" });
    deleteThreadMock.mockResolvedValue(undefined);
    listThreadsMock.mockRejectedValueOnce(new Error("refresh failed"));

    fireEvent.click(screen.getByTitle("更多操作：报告分析"));
    fireEvent.click(screen.getByRole("menuitem", { name: "删除对话" }));
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));

    expect(await screen.findByRole("heading", { name: "新的对话" })).toBeTruthy();
    expect((await screen.findByRole("alert")).textContent).toContain(
      "对话已删除，但重新加载对话列表失败",
    );
    expect(screen.queryByTitle("更多操作：报告分析")).toBeNull();
  });
});
