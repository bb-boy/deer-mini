import { type FormEvent, useEffect, useState } from "react";

import type { Thread } from "../api/types";
import { Icon } from "./Icon";

interface ThreadSidebarProps {
  userId: string;
  threads: Thread[];
  selectedThreadId: string | null;
  loading: boolean;
  onUserIdCommit: (userId: string) => void;
  onCreate: () => void;
  onSelect: (threadId: string) => void;
  onNavigate?: (section: "chat" | "activity" | "files") => void;
  activeSection?: "chat" | "activity" | "files";
  onRename?: (threadId: string, title: string) => void | Promise<void>;
  onDelete?: (threadId: string) => void | Promise<void>;
  onShare?: (thread: Thread) => void | Promise<void>;
  onExport?: (thread: Thread, format: "json" | "markdown") => void | Promise<void>;
  darkMode?: boolean;
  onToggleDarkMode?: () => void;
  collapsed?: boolean;
  onToggle?: () => void;
}

function statusLabel(status: Thread["status"]): string {
  return status === "running" ? "运行中" : "空闲";
}

function SidebarNavItem({
  icon,
  label,
  selected = false,
  collapsed,
  onClick,
}: {
  icon: "message" | "activity" | "workspace";
  label: string;
  selected?: boolean;
  collapsed?: boolean;
  onClick?: () => void;
}) {
  return (
    <button
      type="button"
      className={`sidebar-nav-item ${selected ? "selected" : ""}`}
      title={collapsed ? label : undefined}
      aria-current={selected ? "page" : undefined}
      onClick={onClick}
    >
      <Icon name={icon} size={17} />
      <span>{label}</span>
    </button>
  );
}

function ThreadRow({
  thread,
  selected,
  onSelect,
  onMenu,
  menuOpen,
  onRename,
  onDelete,
  onShare,
  onExport,
}: {
  thread: Thread;
  selected: boolean;
  onSelect: (threadId: string) => void;
  onMenu?: (threadId: string) => void;
  menuOpen?: boolean;
  onRename?: () => void;
  onDelete?: () => void;
  onShare?: () => void;
  onExport?: (format: "json" | "markdown") => void;
}) {
  return (
    <div className={`thread-item-wrap ${menuOpen ? "menu-open" : ""}`}>
      <button
        type="button"
        className={`thread-item ${selected ? "selected" : ""}`}
        onClick={() => onSelect(thread.id)}
      >
        <span className="thread-item-icon" aria-hidden="true">
          <Icon name="message" size={14} />
        </span>
        <span className="thread-item-copy">
          <span className="thread-title">{thread.title || "未命名对话"}</span>
          <span className="thread-item-meta">
            <span className={`status-dot ${thread.status}`} />
            {statusLabel(thread.status)}
          </span>
        </span>
        <Icon name="chevron-right" size={14} className="thread-item-arrow" />
      </button>
      {onMenu && (
        <>
          <button
            type="button"
            className="thread-item-menu-button"
            aria-label="更多操作"
            title={`更多操作：${thread.title || "未命名对话"}`}
            aria-expanded={menuOpen}
            onClick={(event) => {
              event.stopPropagation();
              onMenu(thread.id);
            }}
          >
            <span />
            <span />
            <span />
          </button>
          {menuOpen && (
            <div className="thread-menu" role="menu">
              {onRename && (
                <button type="button" role="menuitem" onClick={onRename}>
                  <Icon name="file" size={14} />
                  重命名
                </button>
              )}
              {onShare && (
                <button type="button" role="menuitem" onClick={onShare}>
                  <Icon name="copy" size={14} />
                  复制链接
                </button>
              )}
              {onExport && (
                <button type="button" role="menuitem" onClick={() => onExport("json")}>
                  <Icon name="download" size={14} />
                  导出 JSON
                </button>
              )}
              {onExport && (
                <button type="button" role="menuitem" onClick={() => onExport("markdown")}>
                  <Icon name="file" size={14} />
                  导出 Markdown
                </button>
              )}
              {onDelete && (
                <button type="button" role="menuitem" className="delete-action" onClick={onDelete}>
                  <Icon name="close" size={14} />
                  删除对话
                </button>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

export function ThreadSidebar({
  userId,
  threads,
  selectedThreadId,
  loading,
  onUserIdCommit,
  onCreate,
  onSelect,
  onNavigate,
  activeSection = "chat",
  onRename,
  onDelete,
  onShare,
  onExport,
  darkMode = false,
  onToggleDarkMode,
  collapsed = false,
  onToggle,
}: ThreadSidebarProps) {
  const [draftUserId, setDraftUserId] = useState(userId);
  const [searchQuery, setSearchQuery] = useState("");
  const [menuThreadId, setMenuThreadId] = useState<string | null>(null);
  const [renameTarget, setRenameTarget] = useState<Thread | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<Thread | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);

  useEffect(() => {
    setDraftUserId(userId);
    setSearchQuery("");
    setMenuThreadId(null);
  }, [userId]);

  const visibleThreads = threads.filter((thread) => {
    const query = searchQuery.trim().toLowerCase();
    return !query || (thread.title || "未命名对话").toLowerCase().includes(query);
  });

  function commitUser(event: FormEvent) {
    event.preventDefault();
    const nextUserId = draftUserId.trim();
    if (nextUserId) onUserIdCommit(nextUserId);
  }

  function openRename(thread: Thread) {
    setMenuThreadId(null);
    setActionError(null);
    setRenameTarget(thread);
    setRenameValue(thread.title || "");
  }

  async function commitRename() {
    const title = renameValue.trim();
    if (!renameTarget || !title || !onRename) return;
    setActionBusy(true);
    try {
      await onRename(renameTarget.id, title);
      setRenameTarget(null);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "重命名失败");
    } finally {
      setActionBusy(false);
    }
  }

  async function commitDelete() {
    if (!deleteTarget || !onDelete) return;
    setActionBusy(true);
    try {
      await onDelete(deleteTarget.id);
      setDeleteTarget(null);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "删除失败");
    } finally {
      setActionBusy(false);
    }
  }

  return (
    <aside
      className={`thread-sidebar ${collapsed ? "is-collapsed" : ""}`}
      onClick={(event) => {
        const target = event.target as HTMLElement;
        if (
          target.closest(".thread-item") &&
          !target.closest(".thread-item-menu-button")
        ) {
          setMenuThreadId(null);
        } else if (!target.closest(".thread-item-wrap")) {
          setMenuThreadId(null);
        }
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape") setMenuThreadId(null);
      }}
    >
      <div className="sidebar-header">
        <div className="brand-block">
          <span className="brand-mark" aria-hidden="true">
            <span>DF</span>
          </span>
          {!collapsed && (
            <div className="brand-copy">
              <strong>DeerFlow</strong>
              <span>Agent workspace</span>
            </div>
          )}
        </div>
        {onToggle && (
          <button
            type="button"
            className="icon-button sidebar-toggle"
            aria-label={collapsed ? "展开导航" : "折叠导航"}
            onClick={onToggle}
          >
            <Icon name="panel" size={17} />
          </button>
        )}
      </div>

      <button
        type="button"
        className="new-chat-button"
        onClick={onCreate}
        title={collapsed ? "新建对话" : undefined}
      >
        <Icon name="plus" size={17} />
        {!collapsed && <span>新建对话</span>}
      </button>

      <nav className="sidebar-nav" aria-label="主导航">
        {!collapsed && <p className="sidebar-label">工作区</p>}
        <SidebarNavItem
          icon="message"
          label="对话"
          selected={activeSection === "chat"}
          collapsed={collapsed}
          onClick={() => onNavigate?.("chat")}
        />
        <SidebarNavItem
          icon="activity"
          label="运行记录"
          selected={activeSection === "activity"}
          collapsed={collapsed}
          onClick={() => onNavigate?.("activity")}
        />
        <SidebarNavItem
          icon="workspace"
          label="工作区文件"
          selected={activeSection === "files"}
          collapsed={collapsed}
          onClick={() => onNavigate?.("files")}
        />
      </nav>

      {!collapsed && (
        <>
          <form className="user-switcher" onSubmit={commitUser}>
            <label htmlFor="user-id">当前用户 ID</label>
            <div className="user-input-wrap">
              <Icon name="user" size={14} />
              <input
                id="user-id"
                value={draftUserId}
                onChange={(event) => setDraftUserId(event.target.value)}
              />
              <button type="submit" className="user-submit" aria-label="切换用户">
                <Icon name="chevron-right" size={14} />
              </button>
            </div>
          </form>

          <div className="thread-heading">
            <div>
              <span className="sidebar-label">最近对话</span>
              <span className="thread-count">{threads.length}</span>
            </div>
            <button
              type="button"
              className="icon-button thread-add"
              aria-label="添加对话"
              onClick={onCreate}
            >
              <Icon name="plus" size={15} />
            </button>
          </div>

          <label className="thread-search">
            <Icon name="search" size={14} />
            <span className="sr-only">搜索对话</span>
            <input
              aria-label="搜索对话"
              placeholder="搜索对话"
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
            />
          </label>

          <nav className="thread-list" aria-label="历史对话">
            {loading && visibleThreads.length === 0 ? (
              <p className="sidebar-muted">正在加载…</p>
            ) : null}
            {!loading && visibleThreads.length === 0 ? (
              <p className="sidebar-empty">
                {threads.length === 0 ? "还没有对话，先创建一个。" : "没有匹配的对话。"}
              </p>
            ) : null}
            {visibleThreads.map((thread) => (
              <ThreadRow
                key={thread.id}
                thread={thread}
                selected={selectedThreadId === thread.id}
                onSelect={onSelect}
                onMenu={onRename || onDelete || onShare || onExport ? (threadId) => setMenuThreadId((current) => current === threadId ? null : threadId) : undefined}
                menuOpen={menuThreadId === thread.id}
                onRename={onRename ? () => openRename(thread) : undefined}
                onShare={onShare ? () => {
                  setMenuThreadId(null);
                  void Promise.resolve(onShare(thread)).catch((error) => {
                    setActionError(error instanceof Error ? error.message : "复制链接失败");
                  });
                } : undefined}
                onExport={onExport ? (format) => {
                  setMenuThreadId(null);
                  void Promise.resolve(onExport(thread, format)).catch((error) => {
                    setActionError(error instanceof Error ? error.message : "导出对话失败");
                  });
                } : undefined}
                onDelete={onDelete ? () => {
                  setMenuThreadId(null);
                  setActionError(null);
                  setDeleteTarget(thread);
                } : undefined}
              />
            ))}
          </nav>
        </>
      )}

      {collapsed && (
        <nav className="collapsed-thread-list" aria-label="历史对话">
          {threads.slice(0, 6).map((thread) => (
            <button
              key={thread.id}
              type="button"
              className={`collapsed-thread-dot ${
                selectedThreadId === thread.id ? "selected" : ""
              }`}
              aria-label={thread.title || "未命名对话"}
              title={thread.title || "未命名对话"}
              onClick={() => onSelect(thread.id)}
            >
              <Icon name="message" size={15} />
            </button>
          ))}
        </nav>
      )}

      <div className="sidebar-spacer" />
      <footer className="sidebar-footer">
        <div className="account-card" title={collapsed ? userId : undefined}>
          <span className="account-avatar">{userId.slice(0, 1).toUpperCase()}</span>
          {!collapsed && (
            <span className="account-copy">
              <strong>{userId}</strong>
              <small>本地工作区</small>
            </span>
          )}
          <button
            type="button"
            className="icon-button account-settings"
            aria-label="设置"
            aria-expanded={settingsOpen}
            onClick={() => setSettingsOpen((current) => !current)}
          >
            <Icon name="settings" size={16} />
          </button>
        </div>
        {settingsOpen && (
          <section className="settings-popover" aria-label="设置">
            <div className="settings-popover-heading">
              <span>设置</span>
              <button
                type="button"
                className="icon-button"
                aria-label="关闭设置"
                onClick={() => setSettingsOpen(false)}
              >
                <Icon name="close" size={13} />
              </button>
            </div>
            <button
              type="button"
              className="settings-option"
              onClick={onToggleDarkMode}
              disabled={!onToggleDarkMode}
            >
              <span>
                <strong>深色模式</strong>
                <small>{darkMode ? "已开启" : "跟随浅色主题"}</small>
              </span>
              <span className={`settings-switch ${darkMode ? "is-on" : ""}`}><span /></span>
            </button>
          </section>
        )}
      </footer>

      {renameTarget && (
        <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget && !actionBusy) setRenameTarget(null);
        }}>
          <section className="action-dialog" role="dialog" aria-modal="true" aria-labelledby="rename-dialog-title">
            <span className="dialog-kicker">THREAD SETTINGS</span>
            <h2 id="rename-dialog-title">重命名对话</h2>
            <label htmlFor="rename-thread-input">对话名称</label>
            <input
              id="rename-thread-input"
              value={renameValue}
              autoFocus
              onChange={(event) => setRenameValue(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") void commitRename();
                if (event.key === "Escape" && !actionBusy) setRenameTarget(null);
              }}
            />
            {actionError && <p className="dialog-error" role="alert">{actionError}</p>}
            <div className="dialog-actions">
              <button type="button" className="dialog-secondary" onClick={() => setRenameTarget(null)} disabled={actionBusy}>取消</button>
              <button type="button" className="dialog-primary" onClick={() => void commitRename()} disabled={actionBusy || !renameValue.trim()}>
                {actionBusy ? "保存中…" : "保存"}
              </button>
            </div>
          </section>
        </div>
      )}

      {deleteTarget && (
        <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget && !actionBusy) setDeleteTarget(null);
        }}>
          <section className="action-dialog danger-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-dialog-title">
            <span className="dialog-kicker danger-kicker">DELETE THREAD</span>
            <h2 id="delete-dialog-title">删除这段对话？</h2>
            <p>删除后会同时移除该对话的运行记录与 Workspace 文件，此操作无法撤销。</p>
            {actionError && <p className="dialog-error" role="alert">{actionError}</p>}
            <div className="dialog-actions">
              <button type="button" className="dialog-secondary" onClick={() => setDeleteTarget(null)} disabled={actionBusy}>取消</button>
              <button type="button" className="dialog-danger" onClick={() => void commitDelete()} disabled={actionBusy}>
                {actionBusy ? "删除中…" : "确认删除"}
              </button>
            </div>
          </section>
        </div>
      )}
    </aside>
  );
}
