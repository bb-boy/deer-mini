import type { LiveToolEvent, Message } from "../api/types";
import { Fragment, memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "./Icon";
import { MarkdownContent } from "./MarkdownContent";
import { buildAssistantReply, groupMessageTurns } from "./message-groups";
import { AssistantProcess } from "./AssistantProcess";
import { ReasoningBlock } from "./ReasoningBlock";

interface MessageListProps {
  messages: Message[];
  pendingUserMessage?: string;
  liveMessages?: Message[];
  toolEvents?: LiveToolEvent[];
  running?: boolean;
  streamingMessageId?: string | null;
  reasoningMessageId?: string | null;
  onSuggestion?: (prompt: string) => void;
  onCopy?: (content: string) => void | Promise<void>;
}

const NO_MESSAGES: Message[] = [];
const NO_TOOL_EVENTS: LiveToolEvent[] = [];

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

function MessageAvatar() {
  return (
    <span className="message-avatar assistant-avatar" aria-hidden="true">
      <span>DM</span>
    </span>
  );
}

function MessageActions({
  content,
  label = "复制消息",
  onCopy,
}: {
  content: string;
  label?: string;
  onCopy?: (content: string) => void | Promise<void>;
}) {
  if (!onCopy || !content) return null;
  return (
    <div className="message-actions">
      <button
        type="button"
        aria-label={label}
        title={label}
        onClick={() => void onCopy(content)}
      >
        <Icon name="copy" size={13} />
      </button>
    </div>
  );
}

const UserMessage = memo(function UserMessage({
  message,
  onCopy,
}: {
  message: Message;
  onCopy?: (content: string) => void | Promise<void>;
}) {
  return (
    <article className={`message-row message-row-${message.role}`} data-role={message.role}>
      <div className={`message message-${message.role}`}>
        {message.reasoning_content && <ReasoningBlock content={message.reasoning_content} />}
        {message.content ? <div className="message-content">{message.content}</div> : null}
        <MessageActions content={message.content} onCopy={onCopy} />
      </div>
    </article>
  );
});

const AssistantReply = memo(function AssistantReply({
  messages,
  liveMessages = NO_MESSAGES,
  toolEvents = NO_TOOL_EVENTS,
  running = false,
  streamingMessageId,
  reasoningMessageId,
  onCopy,
}: Pick<MessageListProps, "messages" | "liveMessages" | "toolEvents" | "running" | "onCopy" | "streamingMessageId" | "reasoningMessageId">) {
  // 思考从实时过程归入历史回答时，继续使用用户对同一 message_id 的展开选择。
  const [reasoningOpen, setReasoningOpen] = useState<Record<string, boolean>>({});
  const onReasoningOpenChange = useCallback((id: string, open: boolean) => {
    setReasoningOpen((current) => ({ ...current, [id]: open }));
  }, []);
  const { steps, answers, createdAt } = useMemo(
    () => buildAssistantReply(messages, liveMessages, toolEvents, running, streamingMessageId),
    [messages, liveMessages, toolEvents, running, streamingMessageId],
  );
  const tools = steps.filter((step) => step.kind === "tool");
  const activeTool = tools.find((step) => step.phase === "running");
  const content = answers.map((message) => message.content).filter(Boolean).join("\n\n");
  if (!running && steps.length === 0 && answers.length === 0) return null;
  return (
    <article className="message-row message-row-assistant" data-role="assistant">
      <MessageAvatar />
      <div className="message message-assistant">
        <header>
          <span>DeerMini</span>
          {createdAt && <time>{formatMessageTime(createdAt)}</time>}
          {running && <span className="live-label"><span className="live-dot" />{activeTool ? "正在执行工具" : reasoningMessageId ? "正在思考" : streamingMessageId || content ? "正在输出" : "正在处理"}</span>}
        </header>
        {steps.length > 0 && (
          <AssistantProcess steps={steps} running={running} streamingMessageId={streamingMessageId}
            reasoningMessageId={reasoningMessageId} reasoningOpen={reasoningOpen}
            onReasoningOpenChange={onReasoningOpenChange} onCopy={onCopy} />
        )}
        <div className="reply-answer">
          {answers.map((message) => (
            <div className="reply-answer-part" key={message.id}>
              {message.reasoning_content && <ReasoningBlock content={message.reasoning_content}
                isStreaming={running && message.id === reasoningMessageId} open={reasoningOpen[message.id]}
                onOpenChange={(open) => onReasoningOpenChange(message.id, open)} />}
              {message.content && <MarkdownContent content={message.content}
                isLoading={running && (streamingMessageId === undefined || message.id === streamingMessageId)} />}
            </div>
          ))}
          {running && content && <span className="stream-cursor" aria-hidden="true" />}
          {!running && <MessageActions content={content} label="复制回复" onCopy={onCopy} />}
        </div>
      </div>
    </article>
  );
});

function WelcomeState({ onSuggestion }: { onSuggestion?: (prompt: string) => void }) {
  return (
    <div className="empty-state">
      <div className="welcome-mark-wrap">
        <span className="empty-mark" aria-hidden="true">
          <span>DM</span>
        </span>
        <span className="welcome-spark spark-one" />
        <span className="welcome-spark spark-two" />
      </div>
      <p className="welcome-kicker">DeerMini · AGENT WORKSPACE</p>
      <h2>你好，我是 DeerMini</h2>
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

export function MessageList({
  messages,
  pendingUserMessage,
  liveMessages = NO_MESSAGES,
  toolEvents = NO_TOOL_EVENTS,
  running = false,
  streamingMessageId,
  reasoningMessageId,
  onSuggestion,
  onCopy,
}: MessageListProps) {
  const endRef = useRef<HTMLDivElement>(null);
  const followingRef = useRef(true);
  // 历史分组只在保存状态更新时重算，不随每帧文字反复整理整个对话。
  const turns = useMemo(() => groupMessageTurns(messages), [messages]);
  const separateLiveTurn = Boolean(pendingUserMessage) || turns.length === 0;
  const hasLiveReply = running || liveMessages.length > 0 || toolEvents.length > 0;
  const empty = turns.length === 0 && !pendingUserMessage && !hasLiveReply;
  useEffect(() => {
    const end = endRef.current;
    const scrollParent = end?.closest<HTMLElement>(".messages-scroll");
    if (!end || !scrollParent || typeof ResizeObserver === "undefined") return;
    const onScroll = () => {
      followingRef.current = scrollParent.scrollHeight - scrollParent.scrollTop - scrollParent.clientHeight < 180;
    };
    // DeerFlow 组件在内部逐步展示文字；高度变化时也跟随，用户上翻后停止跟随。
    const observer = new ResizeObserver(() => {
      if (followingRef.current && typeof end.scrollIntoView === "function") {
        end.scrollIntoView({ behavior: "auto", block: "end" });
      }
    });
    observer.observe(end.parentElement!);
    scrollParent.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      observer.disconnect();
      scrollParent.removeEventListener("scroll", onScroll);
    };
  }, []);
  useEffect(() => {
    const end = endRef.current;
    const scrollParent = end?.closest<HTMLElement>(".messages-scroll");
    if (!end || !scrollParent) return;
    const distance = scrollParent.scrollHeight - scrollParent.scrollTop - scrollParent.clientHeight;
    if (distance < 180 && typeof end.scrollIntoView === "function") {
      // 输出期间直接跟随，避免反复重启平滑滚动造成视口抖动。
      end.scrollIntoView({ behavior: running || liveMessages.length > 0 ? "auto" : "smooth", block: "end" });
    }
  }, [liveMessages, messages, pendingUserMessage, running, toolEvents]);

  return (
    <section className="message-list" aria-live="polite">
      {empty && <WelcomeState onSuggestion={onSuggestion} />}
      {turns.map((turn, index) => {
        const current = !separateLiveTurn && index === turns.length - 1;
        return (
          <Fragment key={turn.id}>
            {turn.user && <UserMessage message={turn.user} onCopy={onCopy} />}
            <AssistantReply messages={turn.messages} onCopy={onCopy}
              liveMessages={current ? liveMessages : NO_MESSAGES}
              toolEvents={current ? toolEvents : NO_TOOL_EVENTS} running={current && running}
              streamingMessageId={current ? streamingMessageId : null}
              reasoningMessageId={current ? reasoningMessageId : null} />
          </Fragment>
        );
      })}
      {pendingUserMessage && (
        <article className="message-row message-row-user message-row-pending">
          <div className="message message-user message-pending">
            <p className="message-content">{pendingUserMessage}</p>
          </div>
        </article>
      )}
      {separateLiveTurn && hasLiveReply && (
        <AssistantReply messages={NO_MESSAGES} liveMessages={liveMessages} toolEvents={toolEvents}
          running={running} onCopy={onCopy} streamingMessageId={streamingMessageId} reasoningMessageId={reasoningMessageId} />
      )}
      <div ref={endRef} aria-hidden="true" />
    </section>
  );
}
