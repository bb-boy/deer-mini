import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Message, Run, RunEvent, Thread } from "../api/types";


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
  const input = { message: "你好", modelName: "test", thinkingEnabled: false, reasoningEffort: null };
  const delta: RunEvent = {
    id: "delta", run_id: run.id, thread_id: thread.id, event_type: "text.delta",
    payload: { text: "收到的回答", message_id: "answer" }, sequence: null, created_at: run.created_at,
  };

  it("preserves Markdown syntax when SSE splits delimiters across chunks", async () => {
    const { result } = renderHook(() => useAgentRun({ userId: "alice", thread, onSettled: vi.fn() }));
    await act(async () => { await result.current.start(input); });
    const options = openRunStreamMock.mock.calls[0][0];
    const chunks = ["#", "# 工具\n\n| 工具 | 作用 |\n| --", "- | --- |\n| **ba", "sh** | 执行命令 |\n\n`", "``python\nprint('你好')\n`", "``"];
    act(() => chunks.forEach((text, index) => options.onEvent({
      ...delta, id: `markdown-${index}`, payload: { text, message_id: "answer" },
    })));
    await waitFor(() => expect(result.current.liveAssistantText).toBe(chunks.join("")));
    expect(result.current.liveMessages[0].content).toBe(chunks.join(""));
  });

  it("retains all received text while awaiting the final state refresh", async () => {
    let finish!: () => void;
    const onSettled = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
    const { result } = renderHook(() => useAgentRun({ userId: "alice", thread, onSettled }));
    await act(async () => { await result.current.start(input); });
    const options = openRunStreamMock.mock.calls[0][0];
    const text = "完整文字".repeat(1000);
    act(() => options.onEvent({ ...delta, payload: { text } }));
    let terminal!: Promise<void>;
    act(() => { terminal = options.onTerminal({ ...delta, event_type: "run.end", payload: { status: "success", status_confirmed: true } }); });
    expect(result.current.liveAssistantText).toBe(text);
    expect(result.current.liveMessages[0].content).toBe(text);
    expect(result.current.running).toBe(false);
    await act(async () => { finish(); await terminal; });
    expect(result.current.liveAssistantText).toBe("");
    expect(result.current.liveMessages).toEqual([]);
  });

  it("streams reasoning immediately into the same message without mixing it into the answer", async () => {
    const { result } = renderHook(() => useAgentRun({ userId: "alice", thread, onSettled: vi.fn() }));
    await act(async () => { await result.current.start(input); });
    const options = openRunStreamMock.mock.calls[0][0];
    act(() => options.onEvent({ ...delta, event_type: "reasoning.delta", payload: { text: "先看", message_id: "answer" } }));
    expect(result.current.liveMessages).toHaveLength(1);
    expect(result.current.liveMessages[0]).toMatchObject({ content: "", reasoning_content: "先看" });
    expect(result.current.reasoningMessageId).toBe("answer");
    act(() => {
      options.onEvent({ ...delta, event_type: "reasoning.delta", payload: { text: "资料。", message_id: "answer" } });
      options.onEvent(delta);
    });
    expect(result.current.liveMessages).toHaveLength(1);
    expect(result.current.liveMessages[0]).toMatchObject({ content: "收到的回答", reasoning_content: "先看资料。" });
    expect(result.current.liveAssistantText).toBe("收到的回答");
    expect(result.current.reasoningMessageId).toBeNull();
    expect(result.current.streamingMessageId).toBe("answer");
    const complete = result.current.liveMessages[0];
    act(() => {
      options.onEvent({ ...delta, event_type: "message.complete", payload: { message: complete } });
      options.onEvent({ ...delta, event_type: "reasoning.delta", payload: { text: "重复尾部", message_id: "answer" } });
      options.onEvent(delta);
    });
    expect(result.current.liveMessages).toEqual([complete]);
    expect(result.current.streamingMessageId).toBeNull();
  });

  it("ignores saved reasoning when a checkpoint and the realtime tail overlap", async () => {
    const { result } = renderHook(() => useAgentRun({ userId: "alice", thread, onSettled: vi.fn() }));
    await act(async () => { await result.current.start(input); });
    const options = openRunStreamMock.mock.calls[0][0];
    act(() => {
      options.onEvent({ ...delta, event_type: "reasoning.delta" });
      options.onReset({ run_id: run.id, state: { messages: [{ id: "answer" }] } }, "stream_lost");
      options.onEvent({ ...delta, event_type: "reasoning.delta" });
      options.onEvent({ ...delta, event_type: "reasoning.delta", payload: { text: "下一轮思考", message_id: "next" } });
    });
    expect(result.current.liveMessages).toHaveLength(1);
    expect(result.current.liveMessages[0]).toMatchObject({ id: "next", content: "", reasoning_content: "下一轮思考" });
    expect(result.current.reasoningMessageId).toBe("next");
  });

  it("stops the thinking indicator on an unconfirmed close while retaining received reasoning", async () => {
    const { result } = renderHook(() => useAgentRun({ userId: "alice", thread, onSettled: vi.fn() }));
    await act(async () => { await result.current.start(input); });
    const options = openRunStreamMock.mock.calls[0][0];
    act(() => {
      options.onEvent({ ...delta, event_type: "reasoning.delta" });
      options.onUnconfirmed("结果尚未确认");
    });
    expect(result.current.liveMessages[0].reasoning_content).toBe("收到的回答");
    expect(result.current.running).toBe(false);
    expect(result.current.reasoningMessageId).toBeNull();
    expect(result.current.streamingMessageId).toBeNull();
    expect(result.current.currentRun?.status).toBe("pending");
  });

  it("keeps streamed tool commentary and the following answer under their original message ids", async () => {
    const { result } = renderHook(() => useAgentRun({ userId: "alice", thread, onSettled: vi.fn() }));
    await act(async () => { await result.current.start(input); });
    const options = openRunStreamMock.mock.calls[0][0];
    const request: Message = {
      id: "request", role: "assistant", content: "让我查看时间。", created_at: run.created_at,
      tool_calls: [{ id: "call-1", name: "bash", arguments: { command: "date" } }],
      tool_call_id: null, reasoning_content: "需要读取系统时间",
    };
    act(() => {
      options.onEvent({ ...delta, payload: { text: request.content, message_id: request.id } });
      options.onEvent({ ...delta, event_type: "message.complete", payload: { message: request } });
      options.onEvent({ ...delta, event_type: "message.complete", payload: { message: request } });
      options.onEvent(delta);
    });
    await waitFor(() => expect(result.current.liveAssistantText).toBe(`${request.content}\n\n收到的回答`));
    expect(result.current.liveMessages).toHaveLength(2);
    expect(result.current.liveMessages[0]).toMatchObject(request);
    expect(result.current.liveMessages[1]).toMatchObject({ id: "answer", content: "收到的回答", tool_calls: [] });
  });

  it("retains a tool request with no text while showing the next answer without losing characters", async () => {
    const { result } = renderHook(() => useAgentRun({ userId: "alice", thread, onSettled: vi.fn() }));
    await act(async () => { await result.current.start(input); });
    const options = openRunStreamMock.mock.calls[0][0];
    act(() => {
      options.onEvent({ ...delta, event_type: "message.complete", payload: {
        message: { id: "request", content: "", tool_calls: [{ id: "call-1", name: "bash", arguments: {} }] },
      } });
      options.onEvent(delta);
    });
    await waitFor(() => expect(result.current.liveMessages[1]?.content).toBe("收到的回答"));
    expect(result.current.liveMessages[0]).toMatchObject({
      id: "request", content: "", tool_calls: [{ id: "call-1", name: "bash" }],
    });
  });

  it("uses snapshot message ids to avoid replaying saved text", async () => {
    const onSnapshot = vi.fn();
    const { result } = renderHook(() => useAgentRun({ userId: "alice", thread, onSettled: vi.fn(), onSnapshot }));
    await act(async () => { await result.current.start(input); });
    const options = openRunStreamMock.mock.calls[0][0];
    const checkpoint = { run_id: run.id, state: { messages: [{ id: "answer", content: "已保存" }] } };
    act(() => {
      options.onEvent(delta);
      options.onReset(checkpoint, "stream_lost");
      options.onEvent(delta);
      options.onEvent({ ...delta, event_type: "message.complete", payload: { message: { id: "answer", content: "重复的已保存消息" } } });
      options.onEvent({ ...delta, payload: { text: "新片段", message_id: "new" } });
    });
    await waitFor(() => expect(result.current.liveAssistantText).toBe("新片段"));
    expect(onSnapshot).toHaveBeenCalledWith(thread.id, checkpoint);
    expect(result.current.pendingUserMessage).toBe(null);
    expect(result.current.liveMessages).toHaveLength(1);
    expect(result.current.liveMessages[0]).toMatchObject({ id: "new", content: "新片段" });
  });

  it("keeps a newer run intact when an older terminal refresh finishes late", async () => {
    let finish!: () => void;
    const onSettled = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
    const { result } = renderHook(() => useAgentRun({ userId: "alice", thread, onSettled }));
    await act(async () => { await result.current.start(input); });
    const old = openRunStreamMock.mock.calls[0][0];
    let terminal!: Promise<void>;
    act(() => { terminal = old.onTerminal({ ...delta, event_type: "run.end", payload: { status: "success" } }); });
    createRunMock.mockResolvedValue({ ...run, id: "run-2" });
    await act(async () => { await result.current.start(input); });
    const current = openRunStreamMock.mock.calls[1][0];
    act(() => {
      current.onEvent({ ...delta, run_id: "run-2" });
      old.onEvent({ ...delta, payload: { text: "不应该出现" } });
    });
    await act(async () => { finish(); await terminal; });
    await waitFor(() => expect(result.current.liveAssistantText).toBe("收到的回答"));
    expect(result.current.running).toBe(true);
    expect(result.current.currentRun?.id).toBe("run-2");
  });

  it("restores tool progress after a gap and merges repeated live tool events", async () => {
    const { result } = renderHook(() => useAgentRun({ userId: "alice", thread, onSettled: vi.fn() }));
    await act(async () => { await result.current.start(input); });
    const options = openRunStreamMock.mock.calls[0][0];
    const started: RunEvent = { ...delta, id: "start", event_type: "tool.start",
      payload: { tool_call_id: "call-1", tool_name: "read_file", arguments: { path: "report.txt" } } };
    const ended: RunEvent = { ...started, id: "end", event_type: "tool.end",
      payload: { tool_call_id: "call-1", tool_name: "read_file", content: "文件内容" } };
    act(() => {
      options.onEvent(started);
      options.onReset({ run_id: run.id, state: { messages: [] } }, "buffer_expired");
      options.onEvent(started); // 已保存的历史工具日志。
      options.onEvent(ended);
      options.onEvent(started); // 内存尾部可能再次包含相同工具事件。
      options.onEvent(ended);
    });
    expect(result.current.toolEvents).toHaveLength(1);
    expect(result.current.toolEvents[0]).toMatchObject({
      toolCallId: "call-1", phase: "finished", content: "文件内容",
    });
  });

  it("keeps received text if the result refresh fails", async () => {
    const onSettled = vi.fn().mockRejectedValue(new Error("暂时无法读取结果"));
    const { result } = renderHook(() => useAgentRun({ userId: "alice", thread, onSettled }));
    await act(async () => { await result.current.start(input); });
    const options = openRunStreamMock.mock.calls[0][0];
    act(() => options.onEvent(delta));
    await act(async () => { await options.onTerminal({ ...delta, event_type: "run.end", payload: { status: "success" } }); });
    expect(result.current.liveAssistantText).toBe("收到的回答");
    expect(result.current.connectionNotice).toBe("暂时无法读取结果");
  });

  it("does not treat an unconfirmed stream close as success", async () => {
    const onSettled = vi.fn();
    const { result } = renderHook(() => useAgentRun({ userId: "alice", thread, onSettled }));
    await act(async () => { await result.current.start(input); });
    const options = openRunStreamMock.mock.calls[0][0];
    act(() => options.onUnconfirmed("结果尚未确认"));
    expect(result.current.currentRun?.status).toBe("pending");
    expect(result.current.running).toBe(false);
    expect(onSettled).not.toHaveBeenCalled();
  });

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

    await waitFor(() => expect(result.current.liveAssistantText).toBe("你好"));
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
