import { useState } from "react";
import { SafeReasoningContent } from "../markdown/components";
import { Reasoning, ReasoningTrigger } from "./ai-elements/reasoning";
import { Shimmer } from "./ai-elements/shimmer";

interface ReasoningBlockProps {
  content: string;
  isStreaming?: boolean;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}

// 展示模型真正返回的思考；历史记录没有计时数据，所以不编造“思考了几秒”。
export function ReasoningBlock({ content, isStreaming = false, open, onOpenChange }: ReasoningBlockProps) {
  const [initiallyStreaming] = useState(isStreaming);
  const [localOpen, setLocalOpen] = useState(isStreaming);
  return (
    <Reasoning className="reasoning" isStreaming={isStreaming} defaultOpen={initiallyStreaming}
      open={open ?? localOpen} autoClose={open === undefined}
      onOpenChange={(next) => { setLocalOpen(next); onOpenChange?.(next); }}>
      <ReasoningTrigger className="reasoning-trigger" getThinkingMessage={(thinking) => thinking
        ? <Shimmer as="span" duration={1.5}>正在思考…</Shimmer>
        : <span>思考过程</span>} />
      <SafeReasoningContent className="markdown-content reasoning-content">{content}</SafeReasoningContent>
    </Reasoning>
  );
}
