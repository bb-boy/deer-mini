import { type FormEvent, type KeyboardEvent, useState } from "react";


export interface SendMessageInput {
  message: string;
  modelName: string;
  thinkingEnabled: boolean;
  reasoningEffort: string | null;
}

interface ChatComposerProps {
  disabled: boolean;
  running: boolean;
  onSend: (input: SendMessageInput) => Promise<void> | void;
  onCancel: () => Promise<void> | void;
}

export function ChatComposer({ disabled, running, onSend, onCancel }: ChatComposerProps) {
  const [message, setMessage] = useState("");
  const [modelName, setModelName] = useState("ustc-deepseek-flash");
  const [thinkingEnabled, setThinkingEnabled] = useState(false);
  const [reasoningEffort, setReasoningEffort] = useState("medium");
  const [submitting, setSubmitting] = useState(false);

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    const trimmedMessage = message.trim();
    if (!trimmedMessage || disabled || running || submitting) return;

    setSubmitting(true);
    try {
      await onSend({
        message: trimmedMessage,
        modelName,
        thinkingEnabled,
        reasoningEffort: thinkingEnabled ? reasoningEffort : null,
      });
      setMessage("");
    } finally {
      setSubmitting(false);
    }
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  }

  return (
    <form className="composer" onSubmit={submit}>
      <textarea
        aria-label="给 Agent 的消息"
        placeholder={disabled ? "请先新建或选择一个对话" : "描述任务，Shift + Enter 换行"}
        value={message}
        disabled={disabled || running}
        onChange={(event) => setMessage(event.target.value)}
        onKeyDown={handleKeyDown}
        rows={4}
      />
      <div className="composer-toolbar">
        <div className="model-options">
          <label>
            <span>模型</span>
            <select
              aria-label="模型"
              value={modelName}
              disabled={running}
              onChange={(event) => setModelName(event.target.value)}
            >
              <option value="ustc-deepseek-flash">DeepSeek Flash</option>
              <option value="ustc-deepseek-pro">DeepSeek V4 Pro</option>
            </select>
          </label>
          <label className="check-option">
            <input
              type="checkbox"
              aria-label="开启思考"
              checked={thinkingEnabled}
              disabled={running}
              onChange={(event) => setThinkingEnabled(event.target.checked)}
            />
            开启思考
          </label>
          {thinkingEnabled ? (
            <label>
              <span>推理强度</span>
              <select
                aria-label="推理强度"
                value={reasoningEffort}
                disabled={running}
                onChange={(event) => setReasoningEffort(event.target.value)}
              >
                <option value="low">低</option>
                <option value="medium">中</option>
                <option value="high">高</option>
              </select>
            </label>
          ) : null}
        </div>

        {running ? (
          <button type="button" className="danger-button" onClick={() => void onCancel()}>
            停止运行
          </button>
        ) : (
          <button
            type="submit"
            className="primary-button"
            disabled={disabled || submitting || !message.trim()}
          >
            {submitting ? "正在创建…" : "发送任务"}
          </button>
        )}
      </div>
    </form>
  );
}
