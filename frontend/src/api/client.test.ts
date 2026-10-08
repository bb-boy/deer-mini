import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createRun,
  listRestorePoints,
  previewRestorePoint,
  restoreThread,
  getRestoreOperation,
  deleteThread,
  getLatestState,
  getModels,
  listThreads,
  renameThread,
  workspaceFileDownloadUrl,
} from "./client";


afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("API client", () => {
  it("keeps API and encoded file URLs under the deployment base", async () => {
    vi.stubEnv("BASE_URL", "/ai/");
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("{}", { status: 200, headers: { "content-type": "application/json" } }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await getModels();
    expect(fetchMock).toHaveBeenCalledWith("/ai/api/models", expect.any(Object));
    expect(workspaceFileDownloadUrl("thread/1", "alice@example.com", "outputs/a b.txt"))
      .toBe("/ai/api/threads/thread%2F1/files/outputs/a%20b.txt?user_id=alice%40example.com");
  });

  it("uses owned restore endpoints and preserves the idempotent operation input", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(
      new Response("{}", { status: 200, headers: { "content-type": "application/json" } }),
    ));
    vi.stubGlobal("fetch", fetchMock);
    const input = { operation_id: "operation-1", restore_point_id: "point/1", revision: 7, fingerprint: "abc" };
    await listRestorePoints("thread/1", "alice@example.com");
    await previewRestorePoint("thread/1", "point/1", "alice@example.com");
    await restoreThread("thread/1", "alice@example.com", input);
    await getRestoreOperation("thread/1", "operation-1", "alice@example.com");
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/threads/thread%2F1/restore-points?user_id=alice%40example.com",
      "/api/threads/thread%2F1/restore-points/point%2F1/preview?user_id=alice%40example.com",
      "/api/threads/thread%2F1/restore?user_id=alice%40example.com",
      "/api/threads/thread%2F1/restore-operations/operation-1?user_id=alice%40example.com",
    ]);
    expect(fetchMock.mock.calls[1][1].method).toBe("POST");
    expect(JSON.parse(fetchMock.mock.calls[2][1].body)).toEqual(input);
  });
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

  it("uses the thread update and delete endpoints", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ id: "thread-1", title: "新标题" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await renameThread("thread-1", "alice", "新标题");
    await deleteThread("thread-1", "alice");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/threads/thread-1?user_id=alice",
      expect.objectContaining({ method: "PATCH", body: JSON.stringify({ title: "新标题" }) }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/threads/thread-1?user_id=alice",
      expect.objectContaining({ method: "DELETE" }),
    );
  });
});
