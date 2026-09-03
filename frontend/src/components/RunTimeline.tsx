import type { LiveToolEvent } from "../api/types";


interface RunTimelineProps {
  events: LiveToolEvent[];
}

function renderArguments(value?: Record<string, unknown>) {
  if (!value || Object.keys(value).length === 0) return null;
  return JSON.stringify(value, null, 2);
}

export function RunTimeline({ events }: RunTimelineProps) {
  return (
    <section className="run-timeline" aria-label="工具执行过程">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">Agent Loop</span>
          <h2>工具执行</h2>
        </div>
        <span className="count-badge">{events.length}</span>
      </div>

      {events.length === 0 ? (
        <p className="empty-copy">模型调用工具时，会在这里显示每一步。</p>
      ) : (
        <ol className="timeline-list">
          {events.map((event) => (
            <li key={event.id} className="timeline-item">
              <div className="timeline-title">
                <code>{event.toolName}</code>
                <span className={`phase ${event.phase}`}>
                  {event.phase === "running" ? "运行中" : "已完成"}
                </span>
              </div>
              {renderArguments(event.arguments) ? (
                <pre>{renderArguments(event.arguments)}</pre>
              ) : null}
              {event.content ? <p>{event.content}</p> : null}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
