import type { Message } from "../api/types";


interface MessageListProps {
  messages: Message[];
  pendingUserMessage?: string;
  liveAssistantText?: string;
}

const ROLE_LABELS: Record<Message["role"], string> = {
  user: "你",
  assistant: "Agent",
  tool: "工具结果",
  system: "系统",
};

function MessageCard({ message }: { message: Message }) {
  const toolNames = message.tool_calls.map((call) => call.name).join("、");
  return (
    <article className={`message message-${message.role}`} data-role={message.role}>
      <header>{ROLE_LABELS[message.role]}</header>
      {message.reasoning_content && (
        <details className="reasoning">
          <summary>查看思考过程</summary>
          <p>{message.reasoning_content}</p>
        </details>
      )}
      {message.content ? <p>{message.content}</p> : null}
      {toolNames ? <p className="tool-request">请求工具：{toolNames}</p> : null}
    </article>
  );
}

export function MessageList({
  messages,
  pendingUserMessage,
  liveAssistantText,
}: MessageListProps) {
  const empty = messages.length === 0 && !pendingUserMessage && !liveAssistantText;
  return (
    <section className="message-list" aria-live="polite">
      {empty && (
        <div className="empty-state">
          <span className="empty-mark">dm</span>
          <h2>从一个真实任务开始</h2>
          <p>上传文件，或者让 Agent 读取 Workspace 并完成工作。</p>
        </div>
      )}
      {messages.map((message) => (
        <MessageCard key={message.id} message={message} />
      ))}
      {pendingUserMessage && (
        <article className="message message-user message-pending">
          <header>你</header>
          <p>{pendingUserMessage}</p>
        </article>
      )}
      {liveAssistantText && (
        <article className="message message-assistant message-streaming">
          <header>Agent · 正在输出</header>
          <p>{liveAssistantText}</p>
          <span className="stream-cursor" aria-hidden="true" />
        </article>
      )}
    </section>
  );
}
