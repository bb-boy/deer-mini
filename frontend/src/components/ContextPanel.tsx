import type { LiveToolEvent, Run, Thread, WorkspaceFile } from "../api/types";
import { Icon } from "./Icon";
import { RunTimeline } from "./RunTimeline";
import { WorkspaceFiles } from "./WorkspaceFiles";

export type ContextTab = "activity" | "files" | "history";

interface ContextPanelProps {
  open: boolean;
  resizing: boolean;
  tab: ContextTab;
  runs: Run[];
  toolEvents: LiveToolEvent[];
  files: WorkspaceFile[];
  selectedThread: Thread | null;
  uploading: boolean;
  onClose: () => void;
  onResizeStart: () => void;
  onTabChange: (tab: ContextTab) => void;
  downloadUrl: (relativePath: string) => string;
  onUpload: (file: File) => void | Promise<void>;
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

function formatRunTime(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function TabButton({
  active,
  icon,
  label,
  count,
  onClick,
}: {
  active: boolean;
  icon: "activity" | "folder" | "clock";
  label: string;
  count?: number;
  onClick: () => void;
}) {
  return (
    <button type="button" role="tab" aria-selected={active} className={active ? "active" : ""} onClick={onClick}>
      <Icon name={icon} size={14} />
      <span>{label}</span>
      {count ? <b>{count}</b> : null}
    </button>
  );
}

function HistoryPanel({ runs }: { runs: Run[] }) {
  return (
    <section className="side-section recent-runs">
      <div className="section-heading">
        <div className="panel-title-wrap">
          <span className="panel-icon clock-icon"><Icon name="clock" size={16} /></span>
          <div><span className="eyebrow">HISTORY</span><h2>最近运行</h2></div>
        </div>
        <span className="count-badge">{runs.length}</span>
      </div>
      {runs.length === 0 ? (
        <p className="muted">还没有 Run。完成一次任务后，运行记录会出现在这里。</p>
      ) : (
        <ul>
          {runs.slice(0, 5).map((run) => (
            <li key={run.id}>
              <span className="run-copy"><strong>{run.model_name || "默认模型"}</strong><small>{formatRunTime(run.created_at)}</small></span>
              <span className={`run-status ${run.status}`}>{formatStatus(run.status)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function ContextPanel({
  open,
  resizing,
  tab,
  runs,
  toolEvents,
  files,
  selectedThread,
  uploading,
  onClose,
  onResizeStart,
  onTabChange,
  downloadUrl,
  onUpload,
}: ContextPanelProps) {
  return (
    <aside
      className={`context-column ${open ? "is-open" : "is-closed"}`}
      aria-hidden={!open}
      inert={!open}
    >
      <button type="button" className={`context-resizer ${resizing ? "is-resizing" : ""}`} aria-label="调整工作区宽度" onPointerDown={(event) => { event.preventDefault(); onResizeStart(); }} />
      <div className="context-header">
        <div><span className="eyebrow">WORKSPACE</span><h2>工作区</h2></div>
        <button type="button" className="icon-button context-close" aria-label="关闭工作区面板" onClick={onClose}><Icon name="close" size={16} /></button>
      </div>
      <div className="context-content">
        <div className="context-tabs" role="tablist" aria-label="工作区面板">
          <TabButton active={tab === "activity"} icon="activity" label="运行" count={toolEvents.length} onClick={() => onTabChange("activity")} />
          <TabButton active={tab === "files"} icon="folder" label="文件" count={files.length} onClick={() => onTabChange("files")} />
          <TabButton active={tab === "history"} icon="clock" label="历史" onClick={() => onTabChange("history")} />
        </div>
        {tab === "activity" && <RunTimeline events={toolEvents} />}
        {tab === "files" && <WorkspaceFiles files={files} disabled={!selectedThread || uploading} downloadUrl={downloadUrl} onUpload={onUpload} />}
        {tab === "history" && <HistoryPanel runs={runs} />}
        {!selectedThread && tab === "files" ? <p className="panel-footnote">选择一个对话后，才能管理它的 Workspace。</p> : null}
      </div>
    </aside>
  );
}
