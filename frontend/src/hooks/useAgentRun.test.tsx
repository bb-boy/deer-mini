import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Run, RunEvent, Thread } from "../api/types";


const createRunMock = vi.fn();
const cancelRunMock = vi.fn();
const openRunStreamMock = vi.fn();

vi.mock("../api/client", () => ({
  createRun: (...args: unknown[]) => createRunMock(...args),
  cancelRun: (...args: unknown[]) => cancelRunMock(...args),
}));

vi.mock("../api/run-stream", () => ({
  openRunStream: (...args: unknown[]) => openRunStreamMock(...args),
}));

import { useAgentRun } from "./useAgentRun";


const thread: Thread = {
  id: "thread-1",
  user_id: "alice",
  workspace_path: "/tmp/workspace",
  title: "Test",
  status: "idle",
  created_at: "2026-01-01T00:00:00+00:00",
  updated_at: "2026-01-01T00:00:00+00:00",
};

const run: Run = {
  id: "run-1",
  thread_id: "thread-1",
  user_id: "alice",
  status: "pending",
  model_name: "ustc-deepseek-flash",
  thinking_enabled: false,
  reasoning_effort: null,
  error: null,
  started_at: null,
  finished_at: null,
  created_at: "2026-01-01T00:00:00+00:00",
  updated_at: "2026-01-01T00:00:00+00:00",
};

beforeEach(() => {
  createRunMock.mockReset().mockResolvedValue(run);
  cancelRunMock.mockReset().mockResolvedValue({ ...run, status: "interrupted" });
  openRunStreamMock.mockReset().mockReturnValue(vi.fn());
});

describe("useAgentRun", () => {
  it("accumulates text and tool events, then settles terminal state", async () => {
    const onSettled = vi.fn().mockResolvedValue(undefined);
    const { result } = renderHook(() =>
      useAgentRun({ userId: "alice", thread, onSettled }),
    );

    await act(async () => {
      await result.current.start({
        message: "读取文件",
        modelName: "ustc-deepseek-flash",
        thinkingEnabled: false,
        reasoningEffort: null,
      });
    });

    const streamOptions = openRunStreamMock.mock.calls[0][0];
    const baseEvent: RunEvent = {
      id: "event-1",
      run_id: "run-1",
      thread_id: "thread-1",
      event_type: "text.delta",
      payload: { text: "你好" },
      sequence: 1,
      created_at: "2026-01-01T00:00:00+00:00",
    };
    act(() => {
      streamOptions.onEvent(baseEvent);
      streamOptions.onEvent({
        ...baseEvent,
        id: "event-2",
        sequence: 2,
        event_type: "tool.start",
        payload: { tool_call_id: "call-1", tool_name: "read_file", arguments: {} },
      });
    });

    expect(result.current.liveAssistantText).toBe("你好");
    expect(result.current.toolEvents[0]?.toolName).toBe("read_file");

    await act(async () => {
      streamOptions.onTerminal({
        ...baseEvent,
        id: "event-3",
        sequence: 3,
        event_type: "run.end",
        payload: { status: "success" },
      });
    });

    await waitFor(() => expect(onSettled).toHaveBeenCalledWith("thread-1"));
    expect(result.current.running).toBe(false);
  });
});
