import { useMemo, useState } from "react";
import {
  BookOpenTextIcon, ChevronDownIcon, CopyIcon, FolderOpenIcon,
  MessageSquareTextIcon, NotebookPenIcon, SquareTerminalIcon, WrenchIcon,
} from "lucide-react";
import type { ProcessStep, ToolProcessStep } from "./message-groups";
import { ChainOfThought, ChainOfThoughtContent, ChainOfThoughtStep } from "./ai-elements/chain-of-thought";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "./ui/collapsible";
import { MarkdownContent } from "./MarkdownContent";
import { ReasoningBlock } from "./ReasoningBlock";

interface AssistantProcessProps {
  steps: ProcessStep[];
  running: boolean;
  streamingMessageId?: string | null;
  reasoningMessageId?: string | null;
  reasoningOpen: Record<string, boolean>;
  onReasoningOpenChange: (id: string, open: boolean) => void;
  onCopy?: (content: string) => void | Promise<void>;
}

type DisplayStep = ToolProcessStep | {
  kind: "reasoning" | "text";
  id: string;
  messageId: string;
  content: string;
};

const toolPresentation = {
  bash: { label: "执行命令", icon: SquareTerminalIcon },
  read_file: { label: "读取文件", icon: BookOpenTextIcon },
  write_file: { label: "写入文件", icon: NotebookPenIcon },
  str_replace: { label: "修改文件", icon: NotebookPenIcon },
  ls: { label: "查看目录", icon: FolderOpenIcon },
};

function ToolStep({ step, onCopy }: { step: ToolProcessStep; onCopy?: AssistantProcessProps["onCopy"] }) {
  const presentation = toolPresentation[step.name as keyof typeof toolPresentation]
    ?? { label: `使用 ${step.name}`, icon: WrenchIcon };
  const args = step.arguments ?? {};
  const label = typeof args.description === "string" && args.description.trim()
    ? args.description : presentation.label;
  const rawHint = args.path ?? args.command ?? args.url ?? args.query;
  const hint = typeof rawHint === "string" ? rawHint.replace(/\s+/g, " ") : "";
  const phase = { pending: "等待执行", running: "正在运行", finished: "已返回", unconfirmed: "未收到结果" }[step.phase];
  return (
    <Collapsible className="reply-tool" data-tool-call-id={step.id}>
      <ChainOfThoughtStep className="process-step" icon={presentation.icon}
        status={step.phase === "running" ? "active" : step.phase === "pending" ? "pending" : "complete"}
        label={
          <CollapsibleTrigger className="process-tool-trigger" aria-label={`${label}（${step.name}）详情`}>
            <span className="process-tool-label">{label}</span>
            {hint && <span className="process-tool-hint" title={hint}>{hint}</span>}
            <span className={`tool-phase tool-phase-${step.phase}`}>{phase}</span>
            <ChevronDownIcon className="process-tool-chevron" size={13} />
          </CollapsibleTrigger>
        }>
        <CollapsibleContent className="process-tool-details">
          {Object.keys(args).length > 0 && <>
            <div className="process-detail-label">调用参数</div>
            <pre className="process-tool-output">{JSON.stringify(args, null, 2)}</pre>
          </>}
          <div className="process-detail-heading">
            <span className="process-detail-label">工具结果</span>
            {onCopy && step.content && <button type="button" className="process-copy" aria-label="复制工具结果"
              onClick={() => void onCopy(step.content!)}><CopyIcon size={13} />复制</button>}
          </div>
          {step.content !== undefined
            ? <pre className="process-tool-output reply-tool-result">{step.content || "（工具未返回文字）"}</pre>
            : <p className="process-tool-waiting">{step.phase === "unconfirmed" ? "本次未收到工具结果" : "等待工具返回结果…"}</p>}
        </CollapsibleContent>
      </ChainOfThoughtStep>
    </Collapsible>
  );
}

// 对齐 DeerFlow MessageGroup：保留最新工具，折叠较早步骤；思考总在它产生的文字之前。
export function AssistantProcess({ steps, running, streamingMessageId, reasoningMessageId,
  reasoningOpen, onReasoningOpenChange, onCopy }: AssistantProcessProps) {
  const [showEarlier, setShowEarlier] = useState(false);
  const displaySteps = useMemo(() => steps.flatMap<DisplayStep>((step) => {
    if (step.kind === "tool") return [step];
    const result: DisplayStep[] = [];
    if (step.message.reasoning_content) result.push({
      kind: "reasoning", id: `reasoning-${step.id}`, messageId: step.id, content: step.message.reasoning_content,
    });
    if (step.message.content) result.push({
      kind: "text", id: `text-${step.id}`, messageId: step.id, content: step.message.content,
    });
    return result;
  }), [steps]);
  const lastToolIndex = displaySteps.reduce((last, step, index) => step.kind === "tool" ? index : last, -1);
  const olderIds = new Set(displaySteps.slice(0, Math.max(0, lastToolIndex))
    .filter((step) => step.kind === "reasoning"
      || (step.kind === "tool" && step.phase !== "running" && step.phase !== "pending"))
    .map((step) => step.id));

  return (
    <ChainOfThought className="reply-process" open={true} aria-label="思考与工具步骤">
      {olderIds.size > 0 && <button type="button" className="process-more" aria-expanded={showEarlier}
        onClick={() => setShowEarlier((open) => !open)}>
        <ChevronDownIcon size={14} className={showEarlier ? "is-expanded" : ""} />
        {showEarlier ? "收起前面的步骤" : `查看前面的 ${olderIds.size} 个步骤`}
      </button>}
      <ChainOfThoughtContent className="process-steps">
        {displaySteps.map((step) => (
          <div key={`${step.kind}-${step.id}`} className="process-item" data-step-id={step.id}
            hidden={!showEarlier && olderIds.has(step.id)}>
            {step.kind === "tool" ? <ToolStep step={step} onCopy={onCopy} />
              : step.kind === "reasoning" ? <ReasoningBlock content={step.content}
                isStreaming={running && step.messageId === reasoningMessageId}
                open={reasoningOpen[step.messageId]}
                onOpenChange={(open) => onReasoningOpenChange(step.messageId, open)} />
                : <ChainOfThoughtStep className="process-step reply-process-message" icon={MessageSquareTextIcon}
                  label={<MarkdownContent content={step.content}
                    isLoading={running && step.messageId === streamingMessageId} />} />}
          </div>
        ))}
      </ChainOfThoughtContent>
    </ChainOfThought>
  );
}
