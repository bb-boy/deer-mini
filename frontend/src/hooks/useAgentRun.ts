import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { cancelRun, createRun } from "../api/client";
import { openRunStream } from "../api/run-stream";
import type { Checkpoint, LiveToolEvent, Message, Run, RunEvent, RunStatus, Thread } from "../api/types";

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
  onSnapshot?: (threadId: string, checkpoint: Checkpoint | null) => void;
}

function eventMessage(event: RunEvent): string | null {
  const message = event.payload.message ?? event.payload.error ?? event.payload.reason;
  return typeof message === "string" ? message : null;
}

export function useAgentRun({ userId, thread, onSettled, onSnapshot }: UseAgentRunOptions) {
  const [currentRun, setCurrentRun] = useState<Run | null>(null);
  const [pendingUserMessage, setPendingUserMessage] = useState<string | null>(null);
  // 完整原文立即交给页面；逐步显示由 DeerFlow 的 MarkdownContent 统一处理。
  const [liveMessageDetails, setLiveMessageDetails] = useState<Message[]>([]);
  const [streamingMessageId, setStreamingMessageId] = useState<string | null>(null);
  const [reasoningMessageId, setReasoningMessageId] = useState<string | null>(null);
  const [toolEvents, setToolEvents] = useState<LiveToolEvent[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [connectionNotice, setConnectionNotice] = useState<string | null>(null);
  const closeStreamRef = useRef<(() => void) | null>(null);
  const generationRef = useRef(0);
  const savedMessageIdsRef = useRef(new Set<string>());
  const completedMessageIdsRef = useRef(new Set<string>());
  const settledRef = useRef(onSettled);
  const snapshotRef = useRef(onSnapshot);

  useEffect(() => {
    settledRef.current = onSettled;
    snapshotRef.current = onSnapshot;
  }, [onSettled, onSnapshot]);

  const closeStream = useCallback(() => {
    generationRef.current += 1;
    closeStreamRef.current?.();
    closeStreamRef.current = null;
  }, []);

  const clearText = useCallback(() => {
    savedMessageIdsRef.current.clear();
    completedMessageIdsRef.current.clear();
    setLiveMessageDetails([]);
    setStreamingMessageId(null);
    setReasoningMessageId(null);
  }, []);

  const handleEvent = useCallback((event: RunEvent) => {
    setConnectionNotice(null);
    if (event.event_type === "text.delta" || event.event_type === "reasoning.delta") {
      const text = event.payload.text;
      const id = typeof event.payload.message_id === "string" ? event.payload.message_id : "legacy";
      if (typeof text === "string" && text && !savedMessageIdsRef.current.has(id)
        && !completedMessageIdsRef.current.has(id)) {
        const thinking = event.event_type === "reasoning.delta";
        setStreamingMessageId(id);
        setReasoningMessageId(thinking ? id : null);
        setLiveMessageDetails((current) => {
          const existing = current.find((message) => message.id === id);
          const message: Message = existing ?? {
            id, role: "assistant", content: "", created_at: event.created_at,
            tool_calls: [], tool_call_id: null, reasoning_content: null,
          };
          // 正文和思考按各自的消息编号累积，不能混到同一段显示文字中。
          const updated = thinking
            ? { ...message, reasoning_content: (message.reasoning_content ?? "") + text }
            : { ...message, content: message.content + text };
          return existing ? current.map((item) => item.id === id ? updated : item) : [...current, updated];
        });
      }
      return;
    }
    if (event.event_type === "message.complete") {
      const message = event.payload.message as Partial<Message> | undefined;
      if (message && typeof message.id === "string" && typeof message.content === "string"
        && !savedMessageIdsRef.current.has(message.id)) {
        completedMessageIdsRef.current.add(message.id);
        setStreamingMessageId((id) => id === message.id ? null : id);
        setReasoningMessageId((id) => id === message.id ? null : id);
        const complete: Message = {
          id: message.id, role: message.role ?? "assistant", content: message.content,
          created_at: message.created_at ?? event.created_at,
          tool_calls: message.tool_calls ?? [], tool_call_id: message.tool_call_id ?? null,
          reasoning_content: message.reasoning_content ?? null,
        };
        setLiveMessageDetails((current) => current.some((item) => item.id === complete.id)
          ? current.map((item) => item.id === complete.id ? complete : item)
          : [...current, complete]);
      }
      return;
    }
    if (event.event_type === "tool.start") {
      const toolCallId = String(event.payload.tool_call_id ?? event.id);
      const toolName = String(event.payload.tool_name ?? "unknown_tool");
      const args = event.payload.arguments;
      setToolEvents((current) => current.some((item) => item.toolCallId === toolCallId) ? current : [
        ...current, {
          id: toolCallId, toolCallId, toolName, phase: "running",
          arguments: typeof args === "object" && args !== null
            ? args as Record<string, unknown> : undefined,
        },
      ]);
      return;
    }
    if (event.event_type === "tool.end") {
      const toolCallId = String(event.payload.tool_call_id ?? event.id);
      const toolName = String(event.payload.tool_name ?? "unknown_tool");
      const content = typeof event.payload.content === "string" ? event.payload.content : undefined;
      setToolEvents((current) => {
        if (!current.some((item) => item.toolCallId === toolCallId)) {
          return [...current, { id: toolCallId, toolCallId, toolName, phase: "finished", content }];
        }
        return current.map((item) => item.toolCallId === toolCallId
          ? { ...item, phase: "finished", content } : item);
      });
      return;
    }
    if (["run.error", "run.timeout", "run.interrupted"].includes(event.event_type)) {
      setError(eventMessage(event));
    }
  }, []);

  const connect = useCallback((run: Run) => {
    closeStream();
    const generation = generationRef.current;
    const active = () => generation === generationRef.current;
    setCurrentRun(run);
    setRunning(run.status === "pending" || run.status === "running");
    setConnectionNotice(null);
    closeStreamRef.current = openRunStream({
      threadId: run.thread_id, runId: run.id, userId,
      onEvent: (event) => { if (active()) handleEvent(event); },
      onReset: (checkpoint, reason) => {
        if (!active()) return;
        clearText();
        setToolEvents([]);
        savedMessageIdsRef.current = new Set(checkpoint?.state.messages.map((message) => message.id) ?? []);
        if (checkpoint?.run_id === run.id) setPendingUserMessage(null);
        snapshotRef.current?.(run.thread_id, checkpoint);
        setConnectionNotice(reason === "initial" ? null : "连接已恢复，已重新加载保存的消息");
      },
      onTerminal: async (event) => {
        if (!active()) return;
        closeStreamRef.current = null;
        setRunning(false);
        setStreamingMessageId(null);
        setReasoningMessageId(null);
        if (event.payload.status_confirmed === true) {
          setCurrentRun((current) => current ? { ...current, status: event.payload.status as RunStatus } : current);
        }
        try {
          await settledRef.current(run.thread_id);
          if (!active()) return;
          setPendingUserMessage(null);
          clearText();
        } catch (refreshError) {
          if (active()) setConnectionNotice(refreshError instanceof Error ? refreshError.message : "刷新结果失败，已保留收到的文字");
        }
      },
      onUnconfirmed: (message) => {
        if (!active()) return;
        closeStreamRef.current = null;
        setRunning(false);
        setStreamingMessageId(null);
        setReasoningMessageId(null);
        setConnectionNotice(message);
      },
      onConnectionError: (streamError) => { if (active()) setConnectionNotice(streamError.message); },
    });
  }, [clearText, closeStream, handleEvent, userId]);

  const start = useCallback(async (input: StartAgentRunInput, threadOverride?: Thread) => {
    const targetThread = threadOverride ?? thread;
    if (!targetThread) throw new Error("无法确定这次任务所属的对话");
    closeStream();
    const generation = generationRef.current;
    setError(null);
    setConnectionNotice(null);
    setPendingUserMessage(input.message);
    clearText();
    setToolEvents([]);
    setRunning(true);
    try {
      const run = await createRun(targetThread.id, {
        user_id: userId, message: input.message, model_name: input.modelName,
        thinking_enabled: input.thinkingEnabled, reasoning_effort: input.reasoningEffort,
      });
      if (generation === generationRef.current) connect(run);
      return run;
    } catch (startError) {
      if (generation === generationRef.current) setRunning(false);
      throw startError;
    }
  }, [clearText, closeStream, connect, thread, userId]);

  const resume = useCallback((run: Run) => {
    setError(null);
    setPendingUserMessage(null);
    clearText();
    setToolEvents([]);
    connect(run);
  }, [clearText, connect]);

  const cancel = useCallback(async () => {
    if (!currentRun) return;
    closeStream();
    const generation = generationRef.current;
    try {
      await cancelRun(currentRun.thread_id, currentRun.id, userId);
      if (generation !== generationRef.current) return;
      setRunning(false);
      setStreamingMessageId(null);
      setReasoningMessageId(null);
      await settledRef.current(currentRun.thread_id);
      if (generation !== generationRef.current) return;
      setPendingUserMessage(null);
      clearText();
    } catch (cancelError) {
      if (generation === generationRef.current) {
        setError(cancelError instanceof Error ? cancelError.message : "停止运行失败");
        connect(currentRun);
      }
      throw cancelError;
    }
  }, [clearText, closeStream, connect, currentRun, userId]);

  const clearTransient = useCallback(() => {
    closeStream();
    setCurrentRun(null);
    setRunning(false);
    setPendingUserMessage(null);
    clearText();
    setToolEvents([]);
    setError(null);
    setConnectionNotice(null);
  }, [clearText, closeStream]);

  useEffect(() => () => closeStream(), [closeStream]);
  useEffect(() => {
    if (currentRun && (!thread || currentRun.thread_id !== thread.id)) clearTransient();
  }, [clearTransient, currentRun, thread]);

  const liveMessages = liveMessageDetails;
  const liveAssistantText = useMemo(
    () => liveMessages.map((message) => message.content).filter(Boolean).join("\n\n"),
    [liveMessages],
  );

  return {
    currentRun, pendingUserMessage, liveAssistantText, liveMessages, toolEvents, running, error, connectionNotice,
    streamingMessageId, reasoningMessageId,
    start, resume, cancel, clearTransient,
  };
}
