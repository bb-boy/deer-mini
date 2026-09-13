import type { Checkpoint, ModelsResponse, Run, Thread, WorkspaceFile } from "./types";


export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData)) {
    headers.set("content-type", "application/json");
  }

  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    let detail = `请求失败（HTTP ${response.status}）`;
    try {
      const payload = (await response.json()) as { detail?: unknown };
      if (typeof payload.detail === "string") detail = payload.detail;
    } catch {
      // 非 JSON 错误保持通用提示。
    }
    throw new ApiError(detail, response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function userQuery(userId: string): URLSearchParams {
  return new URLSearchParams({ user_id: userId });
}

export function getModels(): Promise<ModelsResponse> {
  return request<ModelsResponse>("/api/models");
}

export function listThreads(
  userId: string,
  options: { limit?: number; offset?: number } = {},
): Promise<Thread[]> {
  const query = userQuery(userId);
  query.set("limit", String(options.limit ?? 50));
  query.set("offset", String(options.offset ?? 0));
  return request<Thread[]>(`/api/threads?${query.toString()}`);
}

export function createThread(userId: string, title?: string): Promise<Thread> {
  return request<Thread>("/api/threads", {
    method: "POST",
    body: JSON.stringify({ user_id: userId, title: title || null }),
  });
}

export function getThread(threadId: string, userId: string): Promise<Thread> {
  return request<Thread>(
    `/api/threads/${encodeURIComponent(threadId)}?${userQuery(userId)}`,
  );
}

export function renameThread(
  threadId: string,
  userId: string,
  title: string,
): Promise<Thread> {
  return request<Thread>(
    `/api/threads/${encodeURIComponent(threadId)}?${userQuery(userId).toString()}`,
    {
      method: "PATCH",
      body: JSON.stringify({ title }),
    },
  );
}

export function deleteThread(threadId: string, userId: string): Promise<void> {
  return request<void>(
    `/api/threads/${encodeURIComponent(threadId)}?${userQuery(userId).toString()}`,
    { method: "DELETE" },
  );
}

export function listRuns(threadId: string, userId: string): Promise<Run[]> {
  const query = userQuery(userId);
  query.set("limit", "50");
  query.set("offset", "0");
  return request<Run[]>(
    `/api/threads/${encodeURIComponent(threadId)}/runs?${query.toString()}`,
  );
}

export function getRun(
  threadId: string,
  runId: string,
  userId: string,
): Promise<Run> {
  return request<Run>(
    `/api/threads/${encodeURIComponent(threadId)}/runs/${encodeURIComponent(runId)}?${userQuery(userId)}`,
  );
}

export async function getLatestState(
  threadId: string,
  userId: string,
): Promise<Checkpoint | null> {
  try {
    return await request<Checkpoint>(
      `/api/threads/${encodeURIComponent(threadId)}/state?${userQuery(userId)}`,
    );
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

export interface CreateRunInput {
  user_id: string;
  message: string;
  model_name: string;
  thinking_enabled: boolean;
  reasoning_effort: string | null;
}

export function createRun(
  threadId: string,
  input: CreateRunInput,
): Promise<Run> {
  return request<Run>(`/api/threads/${encodeURIComponent(threadId)}/runs`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function cancelRun(
  threadId: string,
  runId: string,
  userId: string,
): Promise<Run> {
  return request<Run>(
    `/api/threads/${encodeURIComponent(threadId)}/runs/${encodeURIComponent(runId)}/cancel?${userQuery(userId)}`,
    { method: "POST" },
  );
}

export function listWorkspaceFiles(
  threadId: string,
  userId: string,
): Promise<WorkspaceFile[]> {
  return request<WorkspaceFile[]>(
    `/api/threads/${encodeURIComponent(threadId)}/files?${userQuery(userId)}`,
  );
}

export function uploadWorkspaceFile(
  threadId: string,
  userId: string,
  file: File,
): Promise<WorkspaceFile> {
  const body = new FormData();
  body.append("file", file);
  return request<WorkspaceFile>(
    `/api/threads/${encodeURIComponent(threadId)}/files?${userQuery(userId)}`,
    { method: "POST", body },
  );
}

export function workspaceFileDownloadUrl(
  threadId: string,
  userId: string,
  relativePath: string,
): string {
  const encodedPath = relativePath
    .split("/")
    .map((segment) => encodeURIComponent(segment))
    .join("/");
  return `/api/threads/${encodeURIComponent(threadId)}/files/${encodedPath}?${userQuery(userId)}`;
}
