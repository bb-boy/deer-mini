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

  emit(name: string, event: unknown, cursor?: string) {
    const message = new MessageEvent(name, {
      data: JSON.stringify(event),
      lastEventId: cursor ?? (event as { id?: string }).id ?? "",
    });
    for (const listener of this.listeners.get(name) ?? []) listener(message);
  }
}


afterEach(() => {
  FakeEventSource.latest = null;
  vi.unstubAllGlobals();
});

describe("openRunStream", () => {
  it("deduplicates by live cursor and accepts unsaved event payloads", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const onEvent = vi.fn();
    const onReset = vi.fn();
    openRunStream({ threadId: "t", runId: "r", userId: "alice", onEvent, onReset,
      onTerminal: vi.fn(), onConnectionError: vi.fn() });
    const source = FakeEventSource.latest!;
    const event = { id: "payload-id", run_id: "r", thread_id: "t", event_type: "text.delta", sequence: null, payload: { text: "字" } };
    source.emit("run_event", event, "s:epoch:1");
    source.emit("run_event", event, "s:epoch:1");
    source.emit("run_event", { ...event, id: "next" }, "s:epoch:2");
    expect(onEvent).toHaveBeenCalledTimes(2);
    source.emit("stream.reset", { run_id: "r", thread_id: "t", checkpoint: null, reason: "stream_lost" });
    expect(onReset).toHaveBeenCalledWith(null, "stream_lost");
  });

  it("reports an unconfirmed result without firing the terminal callback", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const onTerminal = vi.fn();
    const onUnconfirmed = vi.fn();
    openRunStream({ threadId: "t", runId: "r", userId: "alice", onEvent: vi.fn(),
      onTerminal, onUnconfirmed, onConnectionError: vi.fn() });
    FakeEventSource.latest!.emit("stream.unconfirmed", { message: "结果尚未确认" });
    expect(onUnconfirmed).toHaveBeenCalledWith("结果尚未确认");
    expect(onTerminal).not.toHaveBeenCalled();
    expect(FakeEventSource.latest!.closed).toBe(true);
  });

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
