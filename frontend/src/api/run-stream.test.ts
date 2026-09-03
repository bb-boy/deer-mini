import { afterEach, describe, expect, it, vi } from "vitest";

import type { RunEvent } from "./types";
import { openRunStream } from "./run-stream";


class FakeEventSource {
  static latest: FakeEventSource | null = null;
  readonly listeners = new Map<string, Array<(event: MessageEvent) => void>>();
  onerror: ((event: Event) => void) | null = null;
  closed = false;

  constructor(readonly url: string) {
    FakeEventSource.latest = this;
  }

  addEventListener(name: string, listener: EventListener) {
    const callbacks = this.listeners.get(name) ?? [];
    callbacks.push(listener as (event: MessageEvent) => void);
    this.listeners.set(name, callbacks);
  }

  close() {
    this.closed = true;
  }

  emit(name: string, event: RunEvent) {
    const message = new MessageEvent(name, {
      data: JSON.stringify(event),
      lastEventId: event.id,
    });
    for (const listener of this.listeners.get(name) ?? []) listener(message);
  }
}


afterEach(() => {
  FakeEventSource.latest = null;
  vi.unstubAllGlobals();
});

describe("openRunStream", () => {
  it("encodes ids, deduplicates replay, and closes on a terminal event", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const onEvent = vi.fn();
    const onTerminal = vi.fn();

    openRunStream({
      threadId: "thread 1",
      runId: "run/1",
      userId: "alice@example.com",
      onEvent,
      onTerminal,
      onConnectionError: vi.fn(),
    });
    const source = FakeEventSource.latest!;
    const delta: RunEvent = {
      id: "event-1",
      run_id: "run/1",
      thread_id: "thread 1",
      sequence: 1,
      event_type: "text.delta",
      payload: { text: "你好" },
      created_at: "2026-01-01T00:00:00+00:00",
    };
    const ended: RunEvent = {
      ...delta,
      id: "event-2",
      sequence: 2,
      event_type: "run.end",
      payload: { status: "success" },
    };

    source.emit("run_event", delta);
    source.emit("run_event", delta);
    source.emit("run_event", ended);

    expect(source.url).toBe(
      "/api/threads/thread%201/runs/run%2F1/events?user_id=alice%40example.com",
    );
    expect(onEvent).toHaveBeenCalledTimes(2);
    expect(onTerminal).toHaveBeenCalledWith(ended);
    expect(source.closed).toBe(true);
  });
});
