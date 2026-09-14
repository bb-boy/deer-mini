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
type AgentRunOptions = Parameters<typeof import("./hooks/useAgentRun").useAgentRun>[0];
let agentRunOptions: AgentRunOptions;

vi.mock("./api/client", () => ({
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
    running: false,
    error: null,
    connectionNotice: null,
    start: (...args: unknown[]) => startAgentRunMock(...args),
    resume: vi.fn(),
    cancel: vi.fn(),
    clearTransient: vi.fn(),
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
