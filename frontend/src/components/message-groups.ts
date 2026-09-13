import type { LiveToolEvent, Message, ToolCall } from "../api/types";

export interface MessageTurn {
  id: string;
  user?: Message;
  messages: Message[];
}

export interface ToolProcessStep {
  kind: "tool";
  id: string;
  name: string;
  arguments?: Record<string, unknown>;
  content?: string;
  phase: "pending" | "running" | "finished" | "unconfirmed";
}

export type ProcessStep = { kind: "message"; id: string; message: Message } | ToolProcessStep;

// 一次用户提问后的模型消息和工具结果属于同一个回复；不修改原始状态。
export function groupMessageTurns(messages: Message[]): MessageTurn[] {
  const turns: MessageTurn[] = [];
  for (const message of messages) {
    // 系统说明仍保存在 Checkpoint 中、仍交给模型，只是不出现在聊天界面。
    if (message.role === "system") continue;
    if (message.role === "user") {
      turns.push({ id: message.id, user: message, messages: [] });
      continue;
    }
    if (turns.length === 0) turns.push({ id: message.id, messages: [] });
    turns[turns.length - 1].messages.push(message);
  }
  return turns;
}

// 保存的消息优先；实时消息和工具进度按原编号合并，刷新时不会再显示一份。
export function buildAssistantReply(
  savedMessages: Message[],
  liveMessages: Message[],
  toolEvents: LiveToolEvent[],
  running: boolean,
  streamingMessageId?: string | null,
) {
  const byId = new Map(savedMessages.map((message) => [message.id, message]));
  for (const message of liveMessages) {
    if (!byId.has(message.id)) byId.set(message.id, message);
  }
  const messages = [...byId.values()].filter((message) => message.role === "assistant" || message.role === "tool");
  const results = new Map<string, Message>();
  const calls = new Map<string, ToolCall>();
  const events = new Map(toolEvents.map((event) => [event.toolCallId, event]));
  let lastRequestIndex = -1;
  messages.forEach((message, index) => {
    if (message.role === "tool") results.set(message.tool_call_id ?? message.id, message);
    if (message.role === "assistant" && message.tool_calls.length > 0) {
      lastRequestIndex = index;
      for (const call of message.tool_calls) calls.set(call.id, call);
    }
  });

  const steps: ProcessStep[] = [];
  const answers: Message[] = [];
  const shownTools = new Set<string>();
  const addTool = (id: string) => {
    if (shownTools.has(id)) return;
    shownTools.add(id);
    const call = calls.get(id);
    const result = results.get(id);
    const event = events.get(id);
    steps.push({
      kind: "tool", id,
      name: call?.name ?? event?.toolName ?? "工具",
      arguments: call?.arguments ?? event?.arguments,
      content: result?.content ?? event?.content,
      // “已返回”仅表示收到工具结果；不把工具结束误报为整次任务成功。
      phase: result || event?.phase === "finished" ? "finished"
        : !running ? "unconfirmed" : event?.phase === "running" ? "running" : "pending",
    });
  };

  messages.forEach((message, index) => {
    if (message.role === "tool") {
      addTool(message.tool_call_id ?? message.id);
    } else if (index <= lastRequestIndex || (running && message.id === streamingMessageId)) {
      // 对齐 DeerFlow：尚未完成的文字可能接着发起工具调用，先放在过程内。
      if (message.content || message.reasoning_content) {
        steps.push({ kind: "message", id: message.id, message });
      }
      for (const call of message.tool_calls) addTool(call.id);
    } else {
      answers.push(message);
    }
  });
  // 重连时可能先看到工具事件，也保留这些尚未匹配到模型消息的步骤。
  for (const event of toolEvents) addTool(event.toolCallId);
  return { steps, answers, createdAt: messages[0]?.created_at };
}
