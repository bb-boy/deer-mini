import type { Checkpoint, RunEvent } from "./types";

const TERMINAL_EVENTS = new Set<RunEvent["event_type"]>([
  "run.end", "run.error", "run.timeout", "run.interrupted",
]);

interface OpenRunStreamOptions {
  threadId: string;
  runId: string;
  userId: string;
  onEvent: (event: RunEvent) => void;
  onTerminal: (event: RunEvent) => void | Promise<void>;
  onReset?: (checkpoint: Checkpoint | null, reason: string) => void;
  onUnconfirmed?: (message: string) => void;
  onConnectionError: (error: Error) => void;
}

function parseRunEvent(raw: string): RunEvent {
  const value = JSON.parse(raw) as Partial<RunEvent>;
  if (
    typeof value.id !== "string" ||
    typeof value.run_id !== "string" ||
    typeof value.thread_id !== "string" ||
    typeof value.event_type !== "string" ||
    typeof value.payload !== "object" ||
    value.payload === null || Array.isArray(value.payload)
  ) {
    throw new Error("收到无法识别的 RunEvent");
  }
  // 实时事件的 sequence 可以为 null，不能把收到事件等同于日志已入库。
  return value as RunEvent;
}

export function openRunStream(options: OpenRunStreamOptions): () => void {
  const query = new URLSearchParams({ user_id: options.userId });
  const url = `/api/threads/${encodeURIComponent(options.threadId)}/runs/${encodeURIComponent(options.runId)}/events?${query.toString()}`;
  const source = new EventSource(url);
  const seenCursors = new Set<string>();
  let closed = false;
  const close = () => {
    closed = true;
    source.close();
  };
  const fail = (error: unknown) => {
    close();
    options.onConnectionError(error instanceof Error ? error : new Error("SSE 事件解析失败"));
  };

  source.addEventListener("run_event", (raw) => {
    if (closed) return;
    try {
      const message = raw as MessageEvent<string>;
      const event = parseRunEvent(message.data);
      if (event.run_id !== options.runId || event.thread_id !== options.threadId) {
        throw new Error("收到不属于当前任务的事件");
      }
      // 浏览器的 Last-Event-ID 是实时游标，独立于 data 内的日志 id/sequence。
      const cursor = message.lastEventId || event.id;
      if (seenCursors.has(cursor)) return;
      seenCursors.add(cursor);
      if (seenCursors.size > 8192) seenCursors.delete(seenCursors.values().next().value!);
      options.onEvent(event);
      if (TERMINAL_EVENTS.has(event.event_type)) {
        close();
        Promise.resolve(options.onTerminal(event)).catch(options.onConnectionError);
      }
    } catch (error) {
      fail(error);
    }
  });

  source.addEventListener("stream.reset", (raw) => {
    if (closed) return;
    try {
      const snapshot = JSON.parse((raw as MessageEvent<string>).data) as {
        run_id: string; thread_id: string; checkpoint: Checkpoint | null; reason: string;
      };
      if (snapshot.run_id !== options.runId || snapshot.thread_id !== options.threadId
        || (snapshot.checkpoint && snapshot.checkpoint.state.thread_id !== options.threadId)) {
        throw new Error("恢复状态不属于当前任务");
      }
      seenCursors.clear();
      options.onReset?.(snapshot.checkpoint, snapshot.reason);
    } catch (error) {
      fail(error);
    }
  });

  source.addEventListener("stream.unconfirmed", (raw) => {
    if (closed) return;
    try {
      const value = JSON.parse((raw as MessageEvent<string>).data) as { message?: string };
      const message = value.message || "运行结果尚未确认，请稍后重新查询";
      close();
      if (options.onUnconfirmed) options.onUnconfirmed(message);
      else options.onConnectionError(new Error(message));
    } catch (error) {
      fail(error);
    }
  });

  source.onerror = () => {
    if (!closed) options.onConnectionError(new Error("SSE 连接中断，浏览器正在自动重连"));
  };
  return close;
}
