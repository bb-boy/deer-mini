import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Checkpoint, Thread } from "./api/types";


const listThreadsMock = vi.fn();
const createThreadMock = vi.fn();
const getLatestStateMock = vi.fn();
const listRunsMock = vi.fn();
const listWorkspaceFilesMock = vi.fn();
const startAgentRunMock = vi.fn();

vi.mock("./api/client", () => ({
  listThreads: (...args: unknown[]) => listThreadsMock(...args),
  createThread: (...args: unknown[]) => createThreadMock(...args),
  getLatestState: (...args: unknown[]) => getLatestStateMock(...args),
  listRuns: (...args: unknown[]) => listRunsMock(...args),
  listWorkspaceFiles: (...args: unknown[]) => listWorkspaceFilesMock(...args),
  uploadWorkspaceFile: vi.fn(),
  workspaceFileDownloadUrl: vi.fn(() => "/download"),
}));

vi.mock("./hooks/useAgentRun", () => ({
  useAgentRun: () => ({
    currentRun: null,
    pendingUserMessage: null,
    liveAssistantText: "",
    toolEvents: [],
    running: false,
    error: null,
    connectionNotice: null,
    start: (...args: unknown[]) => startAgentRunMock(...args),
    resume: vi.fn(),
    cancel: vi.fn(),
    clearTransient: vi.fn(),
  }),
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
  listThreadsMock.mockReset().mockResolvedValue([thread]);
  createThreadMock.mockReset().mockResolvedValue(thread);
  getLatestStateMock.mockReset().mockResolvedValue(checkpoint);
  listRunsMock.mockReset().mockResolvedValue([]);
  listWorkspaceFilesMock.mockReset().mockResolvedValue([]);
  startAgentRunMock.mockReset().mockResolvedValue(undefined);
});

describe("App", () => {
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
});
