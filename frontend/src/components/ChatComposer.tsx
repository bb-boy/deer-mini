import {
  type ChangeEvent,
  type DragEvent,
  type FormEvent,
  type KeyboardEvent,
  type MouseEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { Icon } from "./Icon";

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
  suggestion?: string | null;
  onSuggestionConsumed?: () => void;
  attachments?: readonly File[];
  onAttach?: (file: File) => void | Promise<void>;
  onRemoveAttachment?: (index: number) => void;
  history?: readonly string[];
  draftKey?: string;
  onClear?: () => void;
  onNewThread?: () => void;
  onHelp?: () => void;
}

const COMMANDS = [
  { name: "/clear", description: "清空当前显示，不删除历史记录" },
  { name: "/new", description: "开始一段新的对话" },
  { name: "/help", description: "查看 Deer Mini 的使用提示" },
];

function readDraft(key?: string): string {
  if (!key || typeof window === "undefined") return "";
  return window.sessionStorage.getItem(key) ?? "";
}

function saveDraft(key: string | undefined, value: string): void {
  if (!key || typeof window === "undefined") return;
  if (value) {
    window.sessionStorage.setItem(key, value);
  } else {
    window.sessionStorage.removeItem(key);
  }
}

function readPreference(
  key: string,
  fallback: string,
  allowed?: readonly string[],
): string {
  if (typeof window === "undefined") return fallback;
  const value = window.localStorage.getItem(key);
  return value && (!allowed || allowed.includes(value)) ? value : fallback;
}

const MODEL_NAMES = ["ustc-deepseek-flash", "ustc-deepseek-pro"] as const;
const REASONING_EFFORTS = ["low", "medium", "high"] as const;

function isComposing(event: KeyboardEvent<HTMLTextAreaElement>): boolean {
  return event.nativeEvent.isComposing || event.keyCode === 229;
}

export function ChatComposer({
  disabled,
  running,
  onSend,
  onCancel,
  suggestion,
  onSuggestionConsumed,
  attachments = [],
  onAttach,
  onRemoveAttachment,
  history = [],
  draftKey,
  onClear,
  onNewThread,
  onHelp,
}: ChatComposerProps) {
  const [message, setMessage] = useState("");
  const [modelName, setModelName] = useState(() =>
    readPreference("deer-mini-model", "ustc-deepseek-flash", MODEL_NAMES),
  );
  const [thinkingEnabled, setThinkingEnabled] = useState(
    () => readPreference("deer-mini-thinking", "false") === "true",
  );
  const [reasoningEffort, setReasoningEffort] = useState(() =>
    readPreference("deer-mini-effort", "medium", REASONING_EFFORTS),
  );
  const [submitting, setSubmitting] = useState(false);
  const [focused, setFocused] = useState(false);
  const [commandIndex, setCommandIndex] = useState(0);
  const [historyIndex, setHistoryIndex] = useState<number | null>(null);
  const [historyDraft, setHistoryDraft] = useState("");
  const [dragging, setDragging] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const latestDraftRef = useRef({ key: draftKey, value: "" });
  const hydratedDraftKeyRef = useRef<string | undefined>(undefined);

  function writeMessage(value: string) {
    latestDraftRef.current = { key: draftKey, value };
    setMessage(value);
  }

  useEffect(() => {
    const previous = latestDraftRef.current;
    if (
      hydratedDraftKeyRef.current !== undefined &&
      previous.key !== draftKey
    ) {
      saveDraft(previous.key, previous.value);
    }
    const saved = readDraft(draftKey);
    hydratedDraftKeyRef.current = draftKey;
    latestDraftRef.current = { key: draftKey, value: saved };
    setMessage(saved);
    setHistoryIndex(null);
    setHistoryDraft("");
  }, [draftKey]);

  useEffect(() => {
    if (suggestion === undefined || suggestion === null) return;
    writeMessage(suggestion);
    onSuggestionConsumed?.();
    requestAnimationFrame(() => textareaRef.current?.focus());
  }, [onSuggestionConsumed, suggestion]);

  useEffect(() => {
    if (hydratedDraftKeyRef.current !== draftKey) return;
    const timer = window.setTimeout(() => saveDraft(draftKey, message), 280);
    return () => window.clearTimeout(timer);
  }, [draftKey, message]);

  useEffect(() => {
    latestDraftRef.current = { key: draftKey, value: message };
  }, [draftKey, message]);

  useEffect(() => {
    const flushDraft = () => {
      saveDraft(latestDraftRef.current.key, latestDraftRef.current.value);
    };
    window.addEventListener("pagehide", flushDraft);
    return () => window.removeEventListener("pagehide", flushDraft);
  }, []);

  useEffect(() => {
    window.localStorage.setItem("deer-mini-model", modelName);
    window.localStorage.setItem("deer-mini-thinking", String(thinkingEnabled));
    window.localStorage.setItem("deer-mini-effort", reasoningEffort);
  }, [modelName, reasoningEffort, thinkingEnabled]);

  useEffect(() => {
    const element = textareaRef.current;
    if (!element) return;
    element.style.height = "auto";
    element.style.height = `${Math.min(Math.max(element.scrollHeight, 70), 180)}px`;
  }, [message]);

  const normalizedHistory = useMemo(() => {
    const values: string[] = [];
    for (const item of history) {
      const value = item.trim();
      if (value && values.at(-1) !== value) values.push(value);
    }
    return values;
  }, [history]);

  const commandQuery = message.startsWith("/") && !message.includes(" ")
    ? message.toLowerCase()
    : null;
  const commandOptions = commandQuery
    ? COMMANDS.filter((command) => command.name.startsWith(commandQuery))
    : [];
  const showCommandMenu = focused && commandOptions.length > 0;
  const activeMode = !thinkingEnabled
    ? "快速"
    : modelName.includes("pro")
      ? "专业"
      : "思考";

  function chooseCommand(name: string) {
    if (name === "/clear") {
      writeMessage(name);
    } else if (name === "/new") {
      writeMessage(name);
    } else {
      writeMessage("/help ");
    }
    setCommandIndex(0);
    requestAnimationFrame(() => textareaRef.current?.focus());
  }

  function selectMode(mode: "flash" | "thinking" | "pro" | "ultra", event: MouseEvent<HTMLButtonElement>) {
    if (running) return;
    if (mode === "flash") {
      setThinkingEnabled(false);
      setModelName("ustc-deepseek-flash");
    } else if (mode === "thinking") {
      setThinkingEnabled(true);
      setModelName("ustc-deepseek-flash");
    } else {
      setThinkingEnabled(true);
      setModelName("ustc-deepseek-pro");
      setReasoningEffort(mode === "ultra" ? "high" : "medium");
    }
    event.currentTarget.closest("details")?.removeAttribute("open");
  }

  function browseHistory(direction: "up" | "down"): boolean {
    if (normalizedHistory.length === 0) return false;
    const canBrowse = message.trim() === "" || historyIndex !== null;
    if (!canBrowse) return false;
    if (historyIndex === null) setHistoryDraft(message);

    if (direction === "up") {
      const next = historyIndex === null
        ? normalizedHistory.length - 1
        : Math.max(0, historyIndex - 1);
      setHistoryIndex(next);
      writeMessage(normalizedHistory[next] ?? "");
      return true;
    }

    if (historyIndex === null) return false;
    if (historyIndex >= normalizedHistory.length - 1) {
      setHistoryIndex(null);
      writeMessage(historyDraft);
      return true;
    }
    const next = historyIndex + 1;
    setHistoryIndex(next);
    writeMessage(normalizedHistory[next] ?? "");
    return true;
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (isComposing(event)) return;
    if (showCommandMenu && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
      event.preventDefault();
      const delta = event.key === "ArrowDown" ? 1 : -1;
      setCommandIndex((current) => (current + delta + commandOptions.length) % commandOptions.length);
      return;
    }
    if (showCommandMenu && (event.key === "Tab" || event.key === "Enter")) {
      if (event.shiftKey) return;
      event.preventDefault();
      const command = commandOptions[commandIndex];
      if (command) chooseCommand(command.name);
      return;
    }
    if (event.key === "Escape" && showCommandMenu) {
      event.preventDefault();
      setFocused(false);
      return;
    }
    if (event.key === "ArrowUp" && browseHistory("up")) {
      event.preventDefault();
      return;
    }
    if (event.key === "ArrowDown" && browseHistory("down")) {
      event.preventDefault();
      return;
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  }

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    const trimmedMessage = message.trim();
    if (!trimmedMessage || disabled || running || submitting) return;
    if (trimmedMessage === "/clear" && onClear) {
      onClear();
      writeMessage("");
      saveDraft(draftKey, "");
      return;
    }
    if (trimmedMessage === "/new" && onNewThread) {
      onNewThread();
      writeMessage("");
      saveDraft(draftKey, "");
      return;
    }
    if (trimmedMessage === "/help" && onHelp) {
      onHelp();
      writeMessage("");
      saveDraft(draftKey, "");
      return;
    }

    setSubmitting(true);
    try {
      await onSend({
        message: trimmedMessage,
        modelName,
        thinkingEnabled,
        reasoningEffort: thinkingEnabled ? reasoningEffort : null,
      });
      writeMessage("");
      setHistoryIndex(null);
      setHistoryDraft("");
      saveDraft(draftKey, "");
    } finally {
      setSubmitting(false);
    }
  }

  function handleFiles(files: FileList | File[]) {
    for (const file of Array.from(files)) {
      if (onAttach) void onAttach(file);
    }
  }

  function handleFileInput(event: ChangeEvent<HTMLInputElement>) {
    if (event.currentTarget.files) handleFiles(event.currentTarget.files);
    event.currentTarget.value = "";
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    if (!disabled && event.dataTransfer.files.length > 0) {
      handleFiles(event.dataTransfer.files);
    }
  }

  const sendDisabled = disabled || submitting || running || !message.trim();
  return (
    <form className={`composer ${dragging ? "is-dragging" : ""}`} onSubmit={submit}>
      <div
        className="composer-shell"
        onDragEnter={(event) => {
          event.preventDefault();
          if (!disabled) setDragging(true);
        }}
        onDragOver={(event) => event.preventDefault()}
        onDragLeave={(event) => {
          if (event.currentTarget === event.target) setDragging(false);
        }}
        onDrop={handleDrop}
      >
        {dragging && <div className="drop-hint"><Icon name="upload" size={14} />释放以上传文件</div>}
        {attachments.length > 0 && (
          <div className="attachment-list" aria-label="待上传附件">
            {attachments.map((file, index) => (
              <span className="attachment-chip" key={`${file.name}-${index}`}>
                <Icon name="paperclip" size={13} />
                <span>{file.name}</span>
                {onRemoveAttachment && (
                  <button
                    type="button"
                    aria-label={`移除附件 ${file.name}`}
                    onClick={() => onRemoveAttachment(index)}
                  >
                    <Icon name="close" size={12} />
                  </button>
                )}
              </span>
            ))}
          </div>
        )}
        <div className="composer-topline">
          <span className="composer-context">
            <span className="composer-context-icon"><Icon name="sparkles" size={14} /></span>
            Deer Mini
          </span>
          <span className="composer-hint">Enter 发送 · Shift + Enter 换行</span>
        </div>
        <textarea
          ref={textareaRef}
          aria-label="给 Agent 的消息"
          aria-describedby="composer-hint"
          placeholder={disabled ? "请先新建或选择一个对话" : "描述一个任务，让 Deer Mini 帮你完成…"}
          value={message}
          disabled={disabled || running}
          onChange={(event) => {
            writeMessage(event.target.value);
            setHistoryIndex(null);
            setHistoryDraft("");
            setCommandIndex(0);
          }}
          onKeyDown={handleKeyDown}
          onPaste={(event) => {
            if (event.clipboardData.files.length > 0) {
              event.preventDefault();
              handleFiles(event.clipboardData.files);
            }
          }}
          onFocus={() => setFocused(true)}
          onBlur={() => window.setTimeout(() => setFocused(false), 120)}
          rows={3}
        />
        {showCommandMenu && (
          <div className="command-menu" role="listbox" aria-label="命令建议">
            <p className="command-menu-label">快捷命令</p>
            {commandOptions.map((command, index) => (
              <button
                key={command.name}
                type="button"
                role="option"
                aria-selected={index === commandIndex}
                className={index === commandIndex ? "selected" : ""}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => chooseCommand(command.name)}
              >
                <code>{command.name}</code>
                <span>{command.description}</span>
              </button>
            ))}
          </div>
        )}
        <div className="composer-toolbar">
          <div className="composer-tools">
            <label className="composer-attach" title="添加附件">
              <Icon name="paperclip" size={14} />
              <span>附件</span>
              <input
                type="file"
                multiple
                aria-label="添加附件"
                disabled={disabled || !onAttach}
                onChange={handleFileInput}
              />
            </label>
            <span className="composer-tool-chip">
              <Icon name="workspace" size={14} />
              Workspace
            </span>
            <label className="composer-select">
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
              <Icon name="chevron-down" size={13} />
            </label>
            <details className="mode-picker">
              <summary>
                <Icon name="sparkles" size={13} />
                <span>{activeMode}模式</span>
                <Icon name="chevron-down" size={12} />
              </summary>
              <div className="mode-picker-menu" role="listbox" aria-label="选择运行模式">
                <button
                  type="button"
                  role="option"
                  aria-selected={!thinkingEnabled}
                  onClick={(event) => selectMode("flash", event)}
                >
                  <strong>快速</strong><small>快速回答，适合简单任务</small>
                </button>
                <button
                  type="button"
                  role="option"
                  aria-selected={thinkingEnabled && !modelName.includes("pro")}
                  onClick={(event) => selectMode("thinking", event)}
                >
                  <strong>思考</strong><small>逐步推理，平衡速度与深度</small>
                </button>
                <button
                  type="button"
                  role="option"
                  aria-selected={thinkingEnabled && modelName.includes("pro") && reasoningEffort !== "high"}
                  onClick={(event) => selectMode("pro", event)}
                >
                  <strong>专业</strong><small>更强模型，适合复杂工作流</small>
                </button>
                <button
                  type="button"
                  role="option"
                  aria-selected={thinkingEnabled && modelName.includes("pro") && reasoningEffort === "high"}
                  onClick={(event) => selectMode("ultra", event)}
                >
                  <strong className="gold-text">极致</strong><small>最高推理强度，适合长任务</small>
                </button>
              </div>
            </details>
            <label className="thinking-sync sr-only">
              <input
                type="checkbox"
                aria-label="开启思考"
                checked={thinkingEnabled}
                disabled={running}
                onChange={(event) => setThinkingEnabled(event.target.checked)}
              />
              开启思考
            </label>
            {thinkingEnabled && (
              <label className="composer-select effort-select">
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
                <Icon name="chevron-down" size={13} />
              </label>
            )}
          </div>
          {running ? (
            <button type="button" className="composer-stop" onClick={() => void onCancel()}>
              <Icon name="stop" size={14} />
              <span>停止运行</span>
            </button>
          ) : (
            <button
              type="submit"
              className="composer-submit"
              aria-label="发送任务"
              disabled={sendDisabled}
            >
              <span>{submitting ? "正在创建…" : "发送任务"}</span>
              <Icon name="arrow-up" size={15} />
            </button>
          )}
        </div>
      </div>
      <p id="composer-hint" className="composer-footnote">
        Deer Mini 会在独立 Workspace 中读取文件并执行必要的工具步骤。
      </p>
    </form>
  );
}
