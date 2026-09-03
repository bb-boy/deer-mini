import type { RunEvent } from "./types";


const TERMINAL_EVENTS = new Set<RunEvent["event_type"]>([
  "run.end",
  "run.error",
  "run.timeout",
  "run.interrupted",
]);

interface OpenRunStreamOptions {
  threadId: string;
  runId: string;
  userId: string;
  onEvent: (event: RunEvent) => void;
  onTerminal: (event: RunEvent) => void;
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
    value.payload === null
  ) {
    throw new Error("收到无法识别的 RunEvent");
  }
  return value as RunEvent;
}

export function openRunStream(options: OpenRunStreamOptions): () => void {
  const query = new URLSearchParams({ user_id: options.userId });
  const url = `/api/threads/${encodeURIComponent(options.threadId)}/runs/${encodeURIComponent(options.runId)}/events?${query.toString()}`;
  const source = new EventSource(url);
  const seenEventIds = new Set<string>();

  source.addEventListener("run_event", (message) => {
    try {
      const event = parseRunEvent((message as MessageEvent<string>).data);
      if (seenEventIds.has(event.id)) return;
      seenEventIds.add(event.id);
      options.onEvent(event);
      if (TERMINAL_EVENTS.has(event.event_type)) {
        source.close();
        options.onTerminal(event);
      }
    } catch (error) {
      source.close();
      options.onConnectionError(
        error instanceof Error ? error : new Error("SSE 事件解析失败"),
      );
    }
  });

  source.onerror = () => {
    options.onConnectionError(new Error("SSE 连接中断，浏览器正在自动重连"));
  };

  return () => source.close();
}
