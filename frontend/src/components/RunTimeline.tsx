import type { LiveToolEvent } from "../api/types";
import { Icon } from "./Icon";

interface RunTimelineProps {
  events: LiveToolEvent[];
}

function renderArguments(value?: Record<string, unknown>) {
  if (!value || Object.keys(value).length === 0) return null;
  return JSON.stringify(value, null, 2);
}

function phaseLabel(phase: LiveToolEvent["phase"]): string {
  return phase === "running" ? "运行中" : "已完成";
}

export function RunTimeline({ events }: RunTimelineProps) {
  return (
    <section className="run-timeline side-section" aria-label="工具执行过程">
      <div className="panel-heading">
        <div className="panel-title-wrap">
          <span className="panel-icon activity-icon"><Icon name="activity" size={16} /></span>
          <div>
            <span className="eyebrow">AGENT LOOP</span>
            <h2>运行步骤</h2>
          </div>
        </div>
        <span className="count-badge">{events.length}</span>
      </div>

      {events.length === 0 ? (
        <div className="panel-empty">
          <span className="empty-line" />
          <p>模型调用工具时，每一步都会显示在这里。</p>
        </div>
      ) : (
        <ol className="timeline-list">
          {events.map((event) => {
            const args = renderArguments(event.arguments);
            return (
              <li key={event.id} className={`timeline-item ${event.phase}`}>
                <span className="timeline-node" aria-hidden="true" />
                <div className="timeline-title">
                  <div className="timeline-tool-name">
                    <Icon name="workspace" size={13} />
                    <code>{event.toolName}</code>
                  </div>
                  <span className={`phase ${event.phase}`}>
                    {event.phase === "running" && <span className="phase-dot" />}
                    {phaseLabel(event.phase)}
                  </span>
                </div>
                {args ? (
                  <details className="timeline-details">
                    <summary>查看参数</summary>
                    <pre>{args}</pre>
                  </details>
                ) : null}
                {event.content ? <p>{event.content}</p> : null}
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
