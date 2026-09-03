import { afterEach, describe, expect, it, vi } from "vitest";

import { createRun, getLatestState, listThreads } from "./client";


afterEach(() => {
  vi.unstubAllGlobals();
});

describe("API client", () => {
  it("encodes user id and pagination when listing threads", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("[]", { status: 200, headers: { "content-type": "application/json" } }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await listThreads("alice@example.com", { limit: 20, offset: 40 });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/threads?user_id=alice%40example.com&limit=20&offset=40",
      expect.objectContaining({ headers: expect.any(Headers) }),
    );
  });

  it("sends the real run input as JSON", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: "run-1" }), {
        status: 202,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await createRun("thread-1", {
      user_id: "alice",
      message: "读取 report.txt",
      model_name: "ustc-deepseek-flash",
      thinking_enabled: true,
      reasoning_effort: "medium",
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/threads/thread-1/runs",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          user_id: "alice",
          message: "读取 report.txt",
          model_name: "ustc-deepseek-flash",
          thinking_enabled: true,
          reasoning_effort: "medium",
        }),
      }),
    );
  });

  it("treats a missing latest checkpoint as an empty conversation", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "Thread 还没有 Checkpoint" }), {
          status: 404,
          headers: { "content-type": "application/json" },
        }),
      ),
    );

    await expect(getLatestState("thread-1", "alice")).resolves.toBeNull();
  });
});
