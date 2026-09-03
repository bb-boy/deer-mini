import { type FormEvent, useEffect, useState } from "react";

import type { Thread } from "../api/types";


interface ThreadSidebarProps {
  userId: string;
  threads: Thread[];
  selectedThreadId: string | null;
  loading: boolean;
  onUserIdCommit: (userId: string) => void;
  onCreate: () => void;
  onSelect: (threadId: string) => void;
}

export function ThreadSidebar({
  userId,
  threads,
  selectedThreadId,
  loading,
  onUserIdCommit,
  onCreate,
  onSelect,
}: ThreadSidebarProps) {
  const [draftUserId, setDraftUserId] = useState(userId);

  useEffect(() => setDraftUserId(userId), [userId]);

  function commitUser(event: FormEvent) {
    event.preventDefault();
    const nextUserId = draftUserId.trim();
    if (nextUserId) onUserIdCommit(nextUserId);
  }

  return (
    <aside className="thread-sidebar">
      <div className="brand-block">
        <span className="brand-mark" aria-hidden="true">D</span>
        <div>
          <strong>Deer Mini</strong>
          <span>Agent 工作台</span>
        </div>
      </div>

      <form className="user-switcher" onSubmit={commitUser}>
        <label htmlFor="user-id">当前用户 ID</label>
        <div className="inline-form">
          <input
            id="user-id"
            value={draftUserId}
            onChange={(event) => setDraftUserId(event.target.value)}
          />
          <button type="submit" className="quiet-button">切换用户</button>
        </div>
      </form>

      <div className="thread-heading">
        <span>对话</span>
        <button type="button" className="primary-button compact" onClick={onCreate}>
          新建对话
        </button>
      </div>

      <nav className="thread-list" aria-label="历史对话">
        {loading && threads.length === 0 ? <p className="muted">正在加载…</p> : null}
        {!loading && threads.length === 0 ? (
          <p className="empty-copy">还没有对话，先创建一个。</p>
        ) : null}
        {threads.map((thread) => (
          <button
            key={thread.id}
            type="button"
            className={`thread-item ${selectedThreadId === thread.id ? "selected" : ""}`}
            onClick={() => onSelect(thread.id)}
          >
            <span className="thread-title">{thread.title || "未命名对话"}</span>
            <span className={`status-dot ${thread.status}`}>
              {thread.status === "running" ? "运行中" : "空闲"}
            </span>
          </button>
        ))}
      </nav>
    </aside>
  );
}
