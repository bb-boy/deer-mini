import {
  type CSSProperties,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  createThread,
  getLatestState,
  listRuns,
  listThreads,
  listWorkspaceFiles,
  deleteThread as deleteThreadRequest,
  renameThread as renameThreadRequest,
  uploadWorkspaceFile,
  workspaceFileDownloadUrl,
} from "./api/client";
import type { Message, Run, Thread, WorkspaceFile } from "./api/types";
import { ChatComposer, type SendMessageInput } from "./components/ChatComposer";
import { ContextPanel, type ContextTab } from "./components/ContextPanel";
import { Icon } from "./components/Icon";
import { MessageList } from "./components/MessageList";
import { ThreadSidebar } from "./components/ThreadSidebar";
import { useAgentRun } from "./hooks/useAgentRun";
const DEFAULT_USER_ID = "demo-user";
const DEFAULT_CONTEXT_WIDTH = 304;
const MIN_CONTEXT_WIDTH = 260;
const MAX_CONTEXT_WIDTH = 460;
function readInitialUserId(): string {
  return window.localStorage.getItem("deer-mini-user-id") || DEFAULT_USER_ID;
}
function readStoredBoolean(key: string, fallback: boolean): boolean {
  const value = window.localStorage.getItem(key);
  return value === null ? fallback : value === "true";
}
function readStoredNumber(key: string, fallback: number): number {
  const value = Number(window.localStorage.getItem(key));
  return Number.isFinite(value) && value >= MIN_CONTEXT_WIDTH && value <= MAX_CONTEXT_WIDTH
    ? value
    : fallback;
}
function readInitialSidebarCollapsed(): boolean {
  return window.innerWidth <= 720 || readStoredBoolean("deer-mini-sidebar-collapsed", false);
}
function readInitialContextOpen(): boolean {
  if (window.innerWidth <= 1080) return false;
  return readStoredBoolean("deer-mini-context-open", true);
}
function readHashThreadId(): string | null {
  const hash = window.location.hash.replace(/^#/, "");
  const value = new URLSearchParams(hash).get("thread");
  return value || null;
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
  const [sidebarCollapsed, setSidebarCollapsed] = useState(readInitialSidebarCollapsed);
  const [contextPanelOpen, setContextPanelOpen] = useState(() =>
    readInitialContextOpen(),
  );
  const [contextWidth, setContextWidth] = useState(() =>
    readStoredNumber("deer-mini-context-width", DEFAULT_CONTEXT_WIDTH),
  );
  const [resizingContext, setResizingContext] = useState(false);
  const [suggestedPrompt, setSuggestedPrompt] = useState<string | null>(null);
  const [pendingAttachments, setPendingAttachments] = useState<File[]>([]);
  const [contextTab, setContextTab] = useState<ContextTab>("activity");
  const [sidebarSection, setSidebarSection] = useState<"chat" | "activity" | "files">("chat");
  const [copyNotice, setCopyNotice] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [darkMode, setDarkMode] = useState(() =>
    readStoredBoolean("deer-mini-dark-mode", false),
  );
  const resumedRunIds = useRef(new Set<string>());
  const newChatRequestedRef = useRef(false);
  const userRequestGenerationRef = useRef(0);
  const threadRequestGenerationRef = useRef(0);
  useEffect(() => {
    window.localStorage.setItem("deer-mini-sidebar-collapsed", String(sidebarCollapsed));
  }, [sidebarCollapsed]);
  useEffect(() => {
    window.localStorage.setItem("deer-mini-context-open", String(contextPanelOpen));
  }, [contextPanelOpen]);
  useEffect(() => {
    window.localStorage.setItem("deer-mini-context-width", String(contextWidth));
  }, [contextWidth]);
  useEffect(() => {
    if (!resizingContext) return;
    const handlePointerMove = (event: PointerEvent) => {
      const nextWidth = window.innerWidth - event.clientX;
      setContextWidth(Math.min(MAX_CONTEXT_WIDTH, Math.max(MIN_CONTEXT_WIDTH, nextWidth)));
    };
    const stopResizing = () => setResizingContext(false);
    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", stopResizing, { once: true });
    window.addEventListener("pointercancel", stopResizing, { once: true });
    return () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", stopResizing);
      window.removeEventListener("pointercancel", stopResizing);
    };
  }, [resizingContext]);
  useEffect(() => {
    window.localStorage.setItem("deer-mini-dark-mode", String(darkMode));
  }, [darkMode]);

  useEffect(() => {
    const handleViewportChange = () => {
      if (window.innerWidth <= 720) setSidebarCollapsed(true);
      if (window.innerWidth <= 1080) setContextPanelOpen(false);
    };
    window.addEventListener("resize", handleViewportChange);
    return () => window.removeEventListener("resize", handleViewportChange);
  }, []);
  useEffect(() => {
    const handleHashChange = () => {
      const threadId = readHashThreadId();
      if (threadId && threads.some((thread) => thread.id === threadId)) {
        newChatRequestedRef.current = false;
        setSelectedThreadId(threadId);
        return;
      }
      if (!threadId) {
        newChatRequestedRef.current = true;
        setSelectedThreadId(null);
      }
    };
    window.addEventListener("hashchange", handleHashChange);
    return () => window.removeEventListener("hashchange", handleHashChange);
  }, [threads]);
  const selectedThread = useMemo(
    () => threads.find((thread) => thread.id === selectedThreadId) ?? null,
    [selectedThreadId, threads],
  );
  const refreshThreads = useCallback(async () => {
    const generation = userRequestGenerationRef.current;
    const nextThreads = await listThreads(userId);
    if (generation !== userRequestGenerationRef.current) return nextThreads;
    setThreads(nextThreads);
    setSelectedThreadId((current) => {
      if (newChatRequestedRef.current) return null;
      const hashThreadId = readHashThreadId();
      if (hashThreadId && nextThreads.some((thread) => thread.id === hashThreadId)) {
        return hashThreadId;
      }
      if (current && nextThreads.some((thread) => thread.id === current)) return current;
      return nextThreads[0]?.id ?? null;
    });
    return nextThreads;
  }, [userId]);
  const loadThreadData = useCallback(
    async (threadId: string) => {
      const generation = userRequestGenerationRef.current;
      const threadGeneration = ++threadRequestGenerationRef.current;
      const [checkpoint, nextRuns, nextFiles] = await Promise.all([
        getLatestState(threadId, userId),
        listRuns(threadId, userId),
        listWorkspaceFiles(threadId, userId),
      ]);
      if (
        generation !== userRequestGenerationRef.current ||
        threadGeneration !== threadRequestGenerationRef.current
      ) {
        return [];
      }
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
  useEffect(() => {
    if (agentRun.running && agentRun.toolEvents.length > 0) {
      setContextTab("activity");
      setContextPanelOpen(true);
    }
  }, [agentRun.running, agentRun.toolEvents.length]);
  useEffect(() => {
    const handleShortcut = (event: KeyboardEvent) => {
      if (event.key === "Escape" && agentRun.running) {
        event.preventDefault();
        void agentRun.cancel();
        return;
      }
      if (event.key === "Escape" && helpOpen) {
        event.preventDefault();
        setHelpOpen(false);
        return;
      }
      if (event.key === "Escape" && contextPanelOpen && window.innerWidth <= 1080) {
        event.preventDefault();
        setContextPanelOpen(false);
        return;
      }
      if (event.key === "Escape" && !sidebarCollapsed && window.innerWidth <= 720) {
        event.preventDefault();
        setSidebarCollapsed(true);
      }
    };
    window.addEventListener("keydown", handleShortcut);
    return () => window.removeEventListener("keydown", handleShortcut);
  }, [agentRun.cancel, agentRun.running, contextPanelOpen, helpOpen, sidebarCollapsed]);
  function switchUser(nextUserId: string) {
    if (agentRun.running) {
      setPageError("请先停止当前运行，再切换用户");
      return;
    }
    userRequestGenerationRef.current += 1;
    threadRequestGenerationRef.current += 1;
    window.localStorage.setItem("deer-mini-user-id", nextUserId);
    resumedRunIds.current.clear();
    agentRun.clearTransient();
    setSelectedThreadId(null);
    setThreads([]);
    setMessages([]);
    setRuns([]);
    setFiles([]);
    setSuggestedPrompt(null);
    setPendingAttachments([]);
    setContextTab("activity");
    setSidebarSection("chat");
    setHelpOpen(false);
    newChatRequestedRef.current = false;
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
    setUserId(nextUserId);
  }
  function handleCreateThread() {
    if (agentRun.running) {
      setPageError("请先停止当前运行，再开始新对话");
      return;
    }
    setPageError(null);
    resumedRunIds.current.clear();
    agentRun.clearTransient();
    setSelectedThreadId(null);
    setMessages([]);
    setRuns([]);
    setFiles([]);
    setSuggestedPrompt(null);
    setPendingAttachments([]);
    setContextTab("activity");
    setSidebarSection("chat");
    setHelpOpen(false);
    newChatRequestedRef.current = true;
    threadRequestGenerationRef.current += 1;
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
    if (window.innerWidth <= 720) setSidebarCollapsed(true);
  }
  function handleSelectThread(threadId: string) {
    if (threadId === selectedThreadId) return;
    setPendingAttachments([]);
    setSuggestedPrompt(null);
    setContextTab("activity");
    setSidebarSection("chat");
    setHelpOpen(false);
    newChatRequestedRef.current = false;
    threadRequestGenerationRef.current += 1;
    setSelectedThreadId(threadId);
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#thread=${encodeURIComponent(threadId)}`);
    if (window.innerWidth <= 720) setSidebarCollapsed(true);
  }
  function handleAttach(file: File) {
    setPendingAttachments((current) => [...current, file]);
  }
  function removeAttachment(index: number) {
    setPendingAttachments((current) => current.filter((_, itemIndex) => itemIndex !== index));
  }
  function clearDisplayedMessages() {
    setMessages([]);
  }
  async function handleRenameThread(threadId: string, title: string) {
    setPageError(null);
    const updatedThread = await renameThreadRequest(threadId, userId, title);
    setThreads((current) => current.map((thread) =>
      thread.id === threadId ? updatedThread : thread,
    ));
    try {
      await refreshThreads();
    } catch (error) {
      setPageError(
        `标题已更新，但重新加载对话列表失败：${
          error instanceof Error ? error.message : "未知错误"
        }`,
      );
    }
  }
  async function handleDeleteThread(threadId: string) {
    setPageError(null);
    const deletingSelected = selectedThreadId === threadId;
    await deleteThreadRequest(threadId, userId);
    setThreads((current) => current.filter((thread) => thread.id !== threadId));
    if (deletingSelected) {
      agentRun.clearTransient();
      threadRequestGenerationRef.current += 1;
      setSelectedThreadId(null);
      setMessages([]);
      setRuns([]);
      setFiles([]);
    }
    if (deletingSelected) newChatRequestedRef.current = true;
    try {
      await refreshThreads();
    } catch (error) {
      setPageError(
        `对话已删除，但重新加载对话列表失败：${
          error instanceof Error ? error.message : "未知错误"
        }`,
      );
    }
    if (deletingSelected) {
      setSelectedThreadId(null);
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
  }
  async function handleShareThread(thread: Thread) {
    const shareUrl = `${window.location.origin}${window.location.pathname}#thread=${encodeURIComponent(thread.id)}`;
    await copyMessage(shareUrl);
  }
  async function handleExportThread(
    thread: Thread,
    format: "json" | "markdown",
  ) {
    let checkpoint: Awaited<ReturnType<typeof getLatestState>>;
    try {
      checkpoint = await getLatestState(thread.id, userId);
    } catch (error) {
      setPageError(error instanceof Error ? error.message : "导出对话失败");
      throw error;
    }
    if (!checkpoint) {
      setPageError("这段对话还没有可导出的消息");
      return;
    }
    const payload = format === "json"
      ? JSON.stringify({ thread, checkpoint }, null, 2)
      : [
          `# ${thread.title || "Deer Mini 对话"}`,
          "",
          ...checkpoint.state.messages.flatMap((message) => [
            `## ${message.role === "user" ? "你" : message.role === "assistant" ? "Deer Mini" : message.role}`,
            "",
            message.content,
            "",
          ]),
        ].join("\n");
    const blobUrl = URL.createObjectURL(
      new Blob([payload], {
        type: format === "json" ? "application/json" : "text/markdown",
      }),
    );
    const link = document.createElement("a");
    link.href = blobUrl;
    const safeTitle = (thread.title || "deer-mini-thread")
      .replace(/[\\/:*?"<>|]/g, "_")
      .slice(0, 80);
    link.download = `${safeTitle}.${format === "json" ? "json" : "md"}`;
    link.click();
    URL.revokeObjectURL(blobUrl);
  }
  async function copyMessage(content: string) {
    let copied = false;
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(content);
        copied = true;
      } catch (error) {
        setPageError(error instanceof Error ? error.message : "复制消息失败");
        return;
      }
    } else if (document.execCommand) {
      try {
        const helper = document.createElement("textarea");
        helper.value = content;
        helper.setAttribute("readonly", "true");
        helper.style.position = "fixed";
        helper.style.opacity = "0";
        try {
          document.body.appendChild(helper);
          helper.select();
          copied = document.execCommand("copy");
        } finally {
          helper.remove();
        }
      } catch (error) {
        setPageError(error instanceof Error ? error.message : "复制消息失败");
        return;
      }
    }
    if (!copied) {
      setPageError("当前浏览器不支持复制消息");
      return;
    }
    setPageError(null);
    setCopyNotice(true);
    window.setTimeout(() => setCopyNotice(false), 1800);
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
        window.history.replaceState(
          null,
          "",
          `${window.location.pathname}${window.location.search}#thread=${encodeURIComponent(targetThread.id)}`,
        );
      }
      newChatRequestedRef.current = false;
      const attachments = pendingAttachments;
      if (attachments.length > 0) {
        setUploading(true);
        try {
          await Promise.all(
            attachments.map((file) =>
              uploadWorkspaceFile(targetThread!.id, userId, file),
            ),
          );
          setPendingAttachments([]);
          setFiles(await listWorkspaceFiles(targetThread.id, userId));
        } finally {
          setUploading(false);
        }
      }
      if (window.innerWidth > 1080) {
        setContextPanelOpen(true);
        setContextTab("activity");
      }
      await agentRun.start(input, targetThread);
      setThreads((current) =>
        current.map((thread) =>
          thread.id === targetThread?.id ? { ...thread, status: "running" } : thread,
        ),
      );
      await refreshThreads();
    } catch (error) {
      setPageError(error instanceof Error ? error.message : "任务发送失败");
      throw error;
    }
  }
  async function handleUpload(file: File) {
    if (!selectedThread) return;
    setContextTab("files");
    setContextPanelOpen(true);
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
    <div
      className={`app-shell ${sidebarCollapsed ? "sidebar-is-collapsed" : ""} ${
        contextPanelOpen ? "context-is-open" : "context-is-closed"
      } ${darkMode ? "theme-dark" : ""} ${resizingContext ? "is-resizing" : ""}`}
      style={{ "--context-width": `${contextWidth}px` } as CSSProperties}
    >
      <ThreadSidebar
        userId={userId}
        threads={threads}
        selectedThreadId={selectedThreadId}
        loading={loadingThreads}
        onUserIdCommit={switchUser}
        onCreate={() => void handleCreateThread()}
        onSelect={handleSelectThread}
        onRename={handleRenameThread}
        onDelete={handleDeleteThread}
        onShare={handleShareThread}
        onExport={handleExportThread}
        darkMode={darkMode}
        onToggleDarkMode={() => setDarkMode((current) => !current)}
        onNavigate={(section) => {
          setSidebarSection(section);
          if (section === "chat") {
            setContextPanelOpen(false);
            return;
          }
          setContextTab(section);
          setContextPanelOpen(true);
        }}
        activeSection={sidebarSection}
        collapsed={sidebarCollapsed}
        onToggle={() => setSidebarCollapsed((current) => !current)}
      />
      {!sidebarCollapsed ? (
        <button
          type="button"
          className="sidebar-backdrop"
          aria-label="关闭导航"
          onClick={() => setSidebarCollapsed(true)}
        />
      ) : null}
      <main className="chat-column">
        <header className="chat-header">
          <div className="header-leading">
            <button
              type="button"
              className="icon-button mobile-menu-button"
              aria-label="切换导航"
              onClick={() => setSidebarCollapsed((current) => !current)}
            >
              <Icon name="menu" size={18} />
            </button>
            <div className="header-title">
              <span className="eyebrow">CONVERSATION</span>
              <h1>{selectedThread?.title || "新的对话"}</h1>
            </div>
          </div>
          <div className="header-actions">
            {selectedThread ? (
              <span className={`thread-state ${agentRun.running ? "running" : "idle"}`}>
                <span className="state-dot" />
                {agentRun.running ? "Agent 运行中" : "已就绪"}
              </span>
            ) : null}
            <button
              type="button"
              className={`header-panel-button ${contextPanelOpen ? "active" : ""}`}
              aria-label={contextPanelOpen ? "隐藏工作区面板" : "显示工作区面板"}
              onClick={() => setContextPanelOpen((current) => !current)}
            >
              <Icon name="panel" size={16} />
              <span>工作区</span>
            </button>
          </div>
        </header>
        {visibleError ? (
          <div className="notice error-notice" role="alert">
            <Icon name="close" size={15} />
            <span>{visibleError}</span>
          </div>
        ) : null}
        {agentRun.connectionNotice ? (
          <div className="notice connection-notice" role="status">
            <span className="notice-pulse" />
            <span>{agentRun.connectionNotice}</span>
          </div>
        ) : null}
        {copyNotice ? <div className="copy-toast" role="status">已复制到剪贴板</div> : null}
        <div className="messages-scroll">
          <div className="messages-frame">
            {loadingThread ? (
              <p className="loading-copy" role="status">
                正在恢复 Checkpoint…
              </p>
            ) : null}
            <MessageList
              messages={messages}
              pendingUserMessage={agentRun.pendingUserMessage ?? undefined}
              liveAssistantText={agentRun.liveAssistantText}
              onSuggestion={setSuggestedPrompt}
              onCopy={copyMessage}
            />
          </div>
        </div>
        <ChatComposer
          disabled={loadingThreads || uploading}
          running={agentRun.running}
          onSend={handleSend}
          onCancel={agentRun.cancel}
          suggestion={suggestedPrompt}
          onSuggestionConsumed={() => setSuggestedPrompt(null)}
          attachments={pendingAttachments}
          onAttach={handleAttach}
          onRemoveAttachment={removeAttachment}
          history={messages.filter((message) => message.role === "user").map((message) => message.content)}
          draftKey={`deer-mini-draft:${userId}:${selectedThreadId ?? "new"}`}
          onClear={clearDisplayedMessages}
          onNewThread={handleCreateThread}
          onHelp={() => setHelpOpen(true)}
        />
        {helpOpen ? (
          <section className="help-panel" role="dialog" aria-label="使用提示">
            <div className="help-panel-heading">
              <div>
                <span className="eyebrow">QUICK GUIDE</span>
                <h2>使用提示</h2>
              </div>
              <button type="button" className="icon-button" aria-label="关闭使用提示" onClick={() => setHelpOpen(false)}>
                <Icon name="close" size={15} />
              </button>
            </div>
            <ul>
              <li><strong>Enter</strong><span>发送任务</span></li>
              <li><strong>Shift + Enter</strong><span>换行</span></li>
              <li><strong>/clear</strong><span>清空当前显示</span></li>
              <li><strong>Esc</strong><span>停止运行中的 Agent</span></li>
            </ul>
          </section>
        ) : null}
      </main>
      {contextPanelOpen ? (
        <button
          type="button"
          className="context-backdrop"
          aria-label="关闭工作区面板"
          onClick={() => setContextPanelOpen(false)}
        />
      ) : null}
      <ContextPanel
        open={contextPanelOpen}
        resizing={resizingContext}
        tab={contextTab}
        runs={runs}
        toolEvents={agentRun.toolEvents}
        files={files}
        selectedThread={selectedThread}
        uploading={uploading}
        onClose={() => setContextPanelOpen(false)}
        onResizeStart={() => setResizingContext(true)}
        onTabChange={setContextTab}
        downloadUrl={(relativePath) =>
          selectedThread
            ? workspaceFileDownloadUrl(selectedThread.id, userId, relativePath)
            : "#"
        }
        onUpload={handleUpload}
      />
    </div>
  );
}
