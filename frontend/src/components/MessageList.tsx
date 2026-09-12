import type { Message } from "../api/types";
import { useEffect, useRef } from "react";
import { Icon } from "./Icon";

interface MessageListProps {
  messages: Message[];
  pendingUserMessage?: string;
  liveAssistantText?: string;
  onSuggestion?: (prompt: string) => void;
  onCopy?: (content: string) => void | Promise<void>;
}

const ROLE_LABELS: Record<Message["role"], string> = {
  user: "你",
  assistant: "Deer Mini",
  tool: "工具结果",
  system: "系统",
};

const SUGGESTIONS = [
  { title: "分析一个文件", detail: "读取并总结 Workspace 中的资料" },
  { title: "整理研究思路", detail: "把复杂目标拆成可执行步骤" },
  { title: "运行一个命令", detail: "在隔离环境中检查项目状态" },
];

function formatMessageTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function MessageAvatar({ role }: { role: Message["role"] }) {
  if (role === "user") return <span className="message-avatar user-avatar">你</span>;
  if (role === "tool") {
    return (
      <span className="message-avatar tool-avatar">
        <Icon name="activity" size={15} />
      </span>
    );
  }
  return (
    <span className="message-avatar assistant-avatar" aria-hidden="true">
      <span>DF</span>
    </span>
  );
}

function ReasoningBlock({ content }: { content: string }) {
  return (
    <details className="reasoning">
      <summary>
        <Icon name="sparkles" size={14} />
        <span>查看思考过程</span>
        <Icon name="chevron-down" size={13} className="reasoning-chevron" />
      </summary>
      <p>{content}</p>
    </details>
  );
}

function InlineMessageText({ value }: { value: string }) {
  const parts = value.split(/(`[^`]+`|\*\*[^*]+\*\*)/g);
  return (
    <>
      {parts.map((part, index) => {
        if (part.startsWith("**") && part.endsWith("**")) {
          return <strong key={`${part}-${index}`}>{part.slice(2, -2)}</strong>;
        }
        if (part.startsWith("`") && part.endsWith("`")) {
          return <code key={`${part}-${index}`}>{part.slice(1, -1)}</code>;
        }
        return <span key={`${part}-${index}`}>{part}</span>;
      })}
    </>
  );
}

function MessageBody({ content }: { content: string }) {
  const blocks: Array<{ type: "text" | "code"; value: string }> = [];
  let codeBuffer: string[] | null = null;
  for (const line of content.split("\n")) {
    if (line.trim().startsWith("```")) {
      if (codeBuffer) {
        blocks.push({ type: "code", value: codeBuffer.join("\n") });
        codeBuffer = null;
      } else {
        codeBuffer = [];
      }
      continue;
    }
    if (codeBuffer) codeBuffer.push(line);
    else blocks.push({ type: "text", value: line });
  }
  if (codeBuffer) blocks.push({ type: "code", value: codeBuffer.join("\n") });

  return (
    <div className="message-content">
      {blocks.map((block, index) =>
        block.type === "code" ? (
          <pre className="message-code" key={`code-${index}`}><code>{block.value}</code></pre>
        ) : (
          <span key={`line-${index}`}><InlineMessageText value={block.value} />{index < blocks.length - 1 && <br />}</span>
        ),
      )}
    </div>
  );
}

function MessageActions({
  message,
  onCopy,
}: {
  message: Message;
  onCopy?: (content: string) => void | Promise<void>;
}) {
  if (!onCopy || !message.content) return null;
  return (
    <div className="message-actions">
      <button
        type="button"
        aria-label="复制消息"
        title="复制消息"
        onClick={() => void onCopy(message.content)}
      >
        <Icon name="copy" size={13} />
      </button>
    </div>
  );
}

function MessageCard({
  message,
  onCopy,
}: {
  message: Message;
  onCopy?: (content: string) => void | Promise<void>;
}) {
  if (message.role === "tool") {
    return <ToolMessageCard message={message} onCopy={onCopy} />;
  }
  const toolNames = message.tool_calls.map((call) => call.name).join("、");
  const timestamp = formatMessageTime(message.created_at);
  return (
    <article className={`message-row message-row-${message.role}`} data-role={message.role}>
      <MessageAvatar role={message.role} />
      <div className={`message message-${message.role}`}>
        <header>
          <span>{ROLE_LABELS[message.role]}</span>
          {timestamp && <time>{timestamp}</time>}
        </header>
        {message.reasoning_content && <ReasoningBlock content={message.reasoning_content} />}
        {message.content ? <MessageBody content={message.content} /> : null}
        {toolNames ? (
          <div className="tool-request">
            <Icon name="workspace" size={14} />
            <span>请求工具：{toolNames}</span>
          </div>
        ) : null}
        <MessageActions message={message} onCopy={onCopy} />
      </div>
    </article>
  );
}

function ToolMessageCard({
  message,
  onCopy,
}: {
  message: Message;
  onCopy?: (content: string) => void | Promise<void>;
}) {
  const timestamp = formatMessageTime(message.created_at);
  return (
    <article className="message-row message-row-tool" data-role="tool">
      <MessageAvatar role="tool" />
      <div className="message message-tool">
        <details open>
          <summary>
            <span><Icon name="activity" size={13} />工具结果</span>
            {timestamp && <time>{timestamp}</time>}
            <Icon name="chevron-down" size={13} />
          </summary>
          {message.content ? <MessageBody content={message.content} /> : null}
          <MessageActions message={message} onCopy={onCopy} />
        </details>
      </div>
    </article>
  );
}

function WelcomeState({ onSuggestion }: { onSuggestion?: (prompt: string) => void }) {
  return (
    <div className="empty-state">
      <div className="welcome-mark-wrap">
        <span className="empty-mark" aria-hidden="true">
          <span>DF</span>
        </span>
        <span className="welcome-spark spark-one" />
        <span className="welcome-spark spark-two" />
      </div>
      <p className="welcome-kicker">DEER MINI · AGENT WORKSPACE</p>
      <h2>你好，我是 Deer Mini</h2>
      <p className="welcome-description">
        一个能阅读文件、调用工具并持续完成任务的智能工作伙伴。
      </p>
      <div className="suggestion-grid">
        {SUGGESTIONS.map((suggestion) => (
          <button
            key={suggestion.title}
            type="button"
            className="suggestion-card"
            onClick={() => onSuggestion?.(`${suggestion.title}：${suggestion.detail}`)}
            disabled={!onSuggestion}
          >
            <span className="suggestion-icon">
              <Icon name="sparkles" size={15} />
            </span>
            <span className="suggestion-copy">
              <strong>{suggestion.title}</strong>
              <small>{suggestion.detail}</small>
            </span>
            <Icon name="chevron-right" size={15} className="suggestion-arrow" />
          </button>
        ))}
      </div>
    </div>
  );
}

function LiveMessage({ content }: { content: string }) {
  return (
    <article className="message-row message-row-assistant message-row-live">
      <MessageAvatar role="assistant" />
      <div className="message message-assistant message-streaming">
        <header>
          <span>Deer Mini</span>
          <span className="live-label"><span className="live-dot" />正在输出</span>
        </header>
        <MessageBody content={content} />
        <span className="stream-cursor" aria-hidden="true" />
      </div>
    </article>
  );
}

export function MessageList({
  messages,
  pendingUserMessage,
  liveAssistantText,
  onSuggestion,
  onCopy,
}: MessageListProps) {
  const endRef = useRef<HTMLDivElement>(null);
  const empty = messages.length === 0 && !pendingUserMessage && !liveAssistantText;
  useEffect(() => {
    const end = endRef.current;
    const scrollParent = end?.closest<HTMLElement>(".messages-scroll");
    if (!end || !scrollParent) return;
    const distance = scrollParent.scrollHeight - scrollParent.scrollTop - scrollParent.clientHeight;
    if (distance < 180 && typeof end.scrollIntoView === "function") {
      end.scrollIntoView({ behavior: "smooth", block: "end" });
    }
  }, [liveAssistantText, messages.length, pendingUserMessage]);

  return (
    <section className="message-list" aria-live="polite">
      {empty && <WelcomeState onSuggestion={onSuggestion} />}
      {messages.map((message) => (
        <MessageCard key={message.id} message={message} onCopy={onCopy} />
      ))}
      {pendingUserMessage && (
        <article className="message-row message-row-user message-row-pending">
          <MessageAvatar role="user" />
          <div className="message message-user message-pending">
            <header><span>你</span><span className="pending-label">刚刚</span></header>
            <p className="message-content">{pendingUserMessage}</p>
          </div>
        </article>
      )}
      {liveAssistantText && <LiveMessage content={liveAssistantText} />}
      <div ref={endRef} aria-hidden="true" />
    </section>
  );
}
