import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  createThread,
  getLatestState,
  listRuns,
  listThreads,
  listWorkspaceFiles,
  uploadWorkspaceFile,
  workspaceFileDownloadUrl,
} from "./api/client";
import type { Message, Run, Thread, WorkspaceFile } from "./api/types";
import { ChatComposer, type SendMessageInput } from "./components/ChatComposer";
import { MessageList } from "./components/MessageList";
import { RunTimeline } from "./components/RunTimeline";
import { ThreadSidebar } from "./components/ThreadSidebar";
import { WorkspaceFiles } from "./components/WorkspaceFiles";
import { useAgentRun } from "./hooks/useAgentRun";


const DEFAULT_USER_ID = "demo-user";

function readInitialUserId(): string {
  return window.localStorage.getItem("deer-mini-user-id") || DEFAULT_USER_ID;
}

function formatStatus(status: Run["status"]): string {
  const labels: Record<Run["status"], string> = {
    pending: "等待中",
    running: "运行中",
    success: "成功",
    error: "失败",
    interrupted: "已停止",
    timeout: "超时",
  };
  return labels[status];
}

export default function App() {
  const [userId, setUserId] = useState(readInitialUserId);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [files, setFiles] = useState<WorkspaceFile[]>([]);
  const [loadingThreads, setLoadingThreads] = useState(true);
  const [loadingThread, setLoadingThread] = useState(false);
  const [pageError, setPageError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const resumedRunIds = useRef(new Set<string>());

  const selectedThread = useMemo(
    () => threads.find((thread) => thread.id === selectedThreadId) ?? null,
    [selectedThreadId, threads],
  );

  const refreshThreads = useCallback(async () => {
    const nextThreads = await listThreads(userId);
    setThreads(nextThreads);
    setSelectedThreadId((current) => {
      if (current && nextThreads.some((thread) => thread.id === current)) return current;
      return nextThreads[0]?.id ?? null;
    });
    return nextThreads;
  }, [userId]);

  const loadThreadData = useCallback(
    async (threadId: string) => {
      const [checkpoint, nextRuns, nextFiles] = await Promise.all([
        getLatestState(threadId, userId),
        listRuns(threadId, userId),
        listWorkspaceFiles(threadId, userId),
      ]);
      setMessages(checkpoint?.state.messages ?? []);
      setRuns(nextRuns);
      setFiles(nextFiles);
      return nextRuns;
    },
    [userId],
  );

  const handleSettled = useCallback(
    async (threadId: string) => {
      try {
        await Promise.all([refreshThreads(), loadThreadData(threadId)]);
      } catch (error) {
        setPageError(error instanceof Error ? error.message : "刷新运行结果失败");
      }
    },
    [loadThreadData, refreshThreads],
  );

  const agentRun = useAgentRun({ userId, thread: selectedThread, onSettled: handleSettled });

  useEffect(() => {
    let cancelled = false;
    setLoadingThreads(true);
    setPageError(null);
    refreshThreads()
      .catch((error) => {
        if (!cancelled) setPageError(error instanceof Error ? error.message : "加载对话失败");
      })
      .finally(() => {
        if (!cancelled) setLoadingThreads(false);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshThreads]);

  useEffect(() => {
    if (!selectedThreadId) {
      setMessages([]);
      setRuns([]);
      setFiles([]);
      return;
    }

    let cancelled = false;
    setLoadingThread(true);
    setPageError(null);
    loadThreadData(selectedThreadId)
      .catch((error) => {
        if (!cancelled) setPageError(error instanceof Error ? error.message : "加载对话详情失败");
      })
      .finally(() => {
        if (!cancelled) setLoadingThread(false);
      });
    return () => {
      cancelled = true;
    };
  }, [loadThreadData, selectedThreadId]);

  useEffect(() => {
    const activeRun = runs.find((run) => run.status === "pending" || run.status === "running");
    if (
      activeRun &&
      activeRun.thread_id === selectedThreadId &&
      activeRun.id !== agentRun.currentRun?.id &&
      !resumedRunIds.current.has(activeRun.id)
    ) {
      resumedRunIds.current.add(activeRun.id);
      agentRun.resume(activeRun);
    }
  }, [agentRun, runs, selectedThreadId]);

  function switchUser(nextUserId: string) {
    window.localStorage.setItem("deer-mini-user-id", nextUserId);
    resumedRunIds.current.clear();
    agentRun.clearTransient();
    setSelectedThreadId(null);
    setThreads([]);
    setMessages([]);
    setRuns([]);
    setFiles([]);
    setUserId(nextUserId);
  }

  async function handleCreateThread() {
    setPageError(null);
    try {
      const thread = await createThread(userId, "新对话");
      setThreads((current) => [thread, ...current.filter((item) => item.id !== thread.id)]);
      setSelectedThreadId(thread.id);
    } catch (error) {
      setPageError(error instanceof Error ? error.message : "创建对话失败");
    }
  }

  async function handleSend(input: SendMessageInput) {
    setPageError(null);
    try {
      let targetThread = selectedThread;
      if (!targetThread) {
        targetThread = await createThread(userId, input.message.slice(0, 30));
        setThreads((current) => [
          targetThread as Thread,
          ...current.filter((item) => item.id !== targetThread?.id),
        ]);
        setSelectedThreadId(targetThread.id);
      }
      await agentRun.start(input, targetThread);
      await refreshThreads();
    } catch (error) {
      setPageError(error instanceof Error ? error.message : "创建 Run 失败");
      throw error;
    }
  }

  async function handleUpload(file: File) {
    if (!selectedThread) return;
    setUploading(true);
    setPageError(null);
    try {
      await uploadWorkspaceFile(selectedThread.id, userId, file);
      setFiles(await listWorkspaceFiles(selectedThread.id, userId));
    } catch (error) {
      setPageError(error instanceof Error ? error.message : "上传文件失败");
    } finally {
      setUploading(false);
    }
  }

  const visibleError = pageError || agentRun.error;

  return (
    <div className="app-shell">
      <ThreadSidebar
        userId={userId}
        threads={threads}
        selectedThreadId={selectedThreadId}
        loading={loadingThreads}
        onUserIdCommit={switchUser}
        onCreate={() => void handleCreateThread()}
        onSelect={(threadId) => setSelectedThreadId(threadId)}
      />

      <main className="chat-column">
        <header className="chat-header">
          <div>
            <span className="eyebrow">Thread</span>
            <h1>{selectedThread?.title || "选择一个对话"}</h1>
          </div>
          {selectedThread ? (
            <span className={`thread-state ${agentRun.running ? "running" : "idle"}`}>
              {agentRun.running ? "Agent 运行中" : "已就绪"}
            </span>
          ) : null}
        </header>

        {visibleError ? <div className="notice error-notice">{visibleError}</div> : null}
        {agentRun.connectionNotice ? (
          <div className="notice connection-notice">{agentRun.connectionNotice}</div>
        ) : null}

        <div className="messages-scroll">
          {loadingThread ? <p className="loading-copy">正在恢复 Checkpoint…</p> : null}
          <MessageList
            messages={messages}
            pendingUserMessage={agentRun.pendingUserMessage ?? undefined}
            liveAssistantText={agentRun.liveAssistantText}
          />
        </div>

        <ChatComposer
          disabled={loadingThreads}
          running={agentRun.running}
          onSend={handleSend}
          onCancel={agentRun.cancel}
        />
      </main>

      <aside className="context-column">
        <RunTimeline events={agentRun.toolEvents} />
        <WorkspaceFiles
          files={files}
          disabled={!selectedThread || uploading}
          downloadUrl={(relativePath) =>
            selectedThread
              ? workspaceFileDownloadUrl(selectedThread.id, userId, relativePath)
              : "#"
          }
          onUpload={handleUpload}
        />
        <section className="side-section recent-runs">
          <div className="section-heading">
            <div>
              <span className="eyebrow">History</span>
              <h2>最近运行</h2>
            </div>
          </div>
          {runs.length === 0 ? (
            <p className="muted">还没有 Run。</p>
          ) : (
            <ul>
              {runs.slice(0, 5).map((run) => (
                <li key={run.id}>
                  <span>{run.model_name || "默认模型"}</span>
                  <span className={`run-status ${run.status}`}>{formatStatus(run.status)}</span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </aside>
    </div>
  );
}
