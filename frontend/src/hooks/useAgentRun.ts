import { useCallback, useEffect, useRef, useState } from "react";

import { cancelRun, createRun } from "../api/client";
import { openRunStream } from "../api/run-stream";
import type { LiveToolEvent, Run, RunEvent, Thread } from "../api/types";


export interface StartAgentRunInput {
  message: string;
  modelName: string;
  thinkingEnabled: boolean;
  reasoningEffort: string | null;
}

interface UseAgentRunOptions {
  userId: string;
  thread: Thread | null;
  onSettled: (threadId: string) => Promise<void> | void;
}

function eventMessage(event: RunEvent): string | null {
  const message = event.payload.message ?? event.payload.error ?? event.payload.reason;
  return typeof message === "string" ? message : null;
}

export function useAgentRun({ userId, thread, onSettled }: UseAgentRunOptions) {
  const [currentRun, setCurrentRun] = useState<Run | null>(null);
  const [pendingUserMessage, setPendingUserMessage] = useState<string | null>(null);
  const [liveAssistantText, setLiveAssistantText] = useState("");
  const [toolEvents, setToolEvents] = useState<LiveToolEvent[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [connectionNotice, setConnectionNotice] = useState<string | null>(null);
  const closeStreamRef = useRef<(() => void) | null>(null);
  const settledRef = useRef(onSettled);

  useEffect(() => {
    settledRef.current = onSettled;
  }, [onSettled]);

  const closeStream = useCallback(() => {
    closeStreamRef.current?.();
    closeStreamRef.current = null;
  }, []);

  const handleEvent = useCallback((event: RunEvent) => {
    setConnectionNotice(null);
    if (event.event_type === "text.delta") {
      const text = event.payload.text;
      if (typeof text === "string") setLiveAssistantText((current) => current + text);
      return;
    }

    if (event.event_type === "tool.start") {
      const toolCallId = String(event.payload.tool_call_id ?? event.id);
      const toolName = String(event.payload.tool_name ?? "unknown_tool");
      const args = event.payload.arguments;
      setToolEvents((current) => {
        if (current.some((item) => item.toolCallId === toolCallId)) return current;
        return [
          ...current,
          {
            id: toolCallId,
            toolCallId,
            toolName,
            phase: "running",
            arguments:
              typeof args === "object" && args !== null
                ? (args as Record<string, unknown>)
                : undefined,
          },
        ];
      });
      return;
    }

    if (event.event_type === "tool.end") {
      const toolCallId = String(event.payload.tool_call_id ?? event.id);
      const toolName = String(event.payload.tool_name ?? "unknown_tool");
      const content = typeof event.payload.content === "string" ? event.payload.content : undefined;
      setToolEvents((current) => {
        const index = current.findIndex((item) => item.toolCallId === toolCallId);
        if (index === -1) {
          return [...current, { id: toolCallId, toolCallId, toolName, phase: "finished", content }];
        }
        return current.map((item, itemIndex) =>
          itemIndex === index ? { ...item, phase: "finished", content } : item,
        );
      });
      return;
    }

    if (["run.error", "run.timeout", "run.interrupted"].includes(event.event_type)) {
      setError(eventMessage(event));
    }
  }, []);

  const connect = useCallback(
    (run: Run) => {
      closeStream();
      setCurrentRun(run);
      setRunning(run.status === "pending" || run.status === "running");
      setConnectionNotice(null);
      closeStreamRef.current = openRunStream({
        threadId: run.thread_id,
        runId: run.id,
        userId,
        onEvent: handleEvent,
        onTerminal: async () => {
          closeStreamRef.current = null;
          setRunning(false);
          await settledRef.current(run.thread_id);
          setPendingUserMessage(null);
          setLiveAssistantText("");
        },
        onConnectionError: (streamError) => setConnectionNotice(streamError.message),
      });
    },
    [closeStream, handleEvent, userId],
  );

  const start = useCallback(
    async (input: StartAgentRunInput, threadOverride?: Thread) => {
      const targetThread = threadOverride ?? thread;
      if (!targetThread) throw new Error("无法确定这次任务所属的对话");
      setError(null);
      setConnectionNotice(null);
      setPendingUserMessage(input.message);
      setLiveAssistantText("");
      setToolEvents([]);
      const run = await createRun(targetThread.id, {
        user_id: userId,
        message: input.message,
        model_name: input.modelName,
        thinking_enabled: input.thinkingEnabled,
        reasoning_effort: input.reasoningEffort,
      });
      connect(run);
      return run;
    },
    [connect, thread, userId],
  );

  const resume = useCallback(
    (run: Run) => {
      setError(null);
      setPendingUserMessage(null);
      setLiveAssistantText("");
      setToolEvents([]);
      connect(run);
    },
    [connect],
  );

  const cancel = useCallback(async () => {
    if (!currentRun) return;
    closeStream();
    try {
      await cancelRun(currentRun.thread_id, currentRun.id, userId);
      setRunning(false);
      await settledRef.current(currentRun.thread_id);
      setPendingUserMessage(null);
      setLiveAssistantText("");
    } catch (cancelError) {
      setError(cancelError instanceof Error ? cancelError.message : "停止运行失败");
      throw cancelError;
    }
  }, [closeStream, currentRun, userId]);

  const clearTransient = useCallback(() => {
    setPendingUserMessage(null);
    setLiveAssistantText("");
    setToolEvents([]);
    setError(null);
    setConnectionNotice(null);
  }, []);

  useEffect(() => () => closeStream(), [closeStream]);

  useEffect(() => {
    if (currentRun && thread && currentRun.thread_id !== thread.id) {
      closeStream();
      setCurrentRun(null);
      setRunning(false);
      clearTransient();
    }
  }, [clearTransient, closeStream, currentRun, thread]);

  return {
    currentRun,
    pendingUserMessage,
    liveAssistantText,
    toolEvents,
    running,
    error,
    connectionNotice,
    start,
    resume,
    cancel,
    clearTransient,
  };
}
