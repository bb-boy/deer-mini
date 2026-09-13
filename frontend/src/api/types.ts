export type ThreadStatus = "idle" | "running";
export type RunStatus =
  | "pending"
  | "running"
  | "success"
  | "error"
  | "interrupted"
  | "timeout";

export type MessageRole = "user" | "assistant" | "system" | "tool";

export interface ModelProfile {
  name: string;
  display_name: string;
  supports_thinking: boolean;
  supports_reasoning_effort: boolean;
}

export interface ModelsResponse {
  default_model: string;
  models: ModelProfile[];
}

export interface Thread {
  id: string;
  user_id: string;
  workspace_path: string;
  title: string | null;
  status: ThreadStatus;
  created_at: string;
  updated_at: string;
}

export interface Run {
  id: string;
  thread_id: string;
  user_id: string;
  status: RunStatus;
  model_name: string | null;
  thinking_enabled: boolean;
  reasoning_effort: string | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export interface Message {
  role: MessageRole;
  content: string;
  id: string;
  created_at: string;
  tool_calls: ToolCall[];
  tool_call_id: string | null;
  reasoning_content: string | null;
}

export interface ThreadState {
  thread_id: string;
  user_id: string;
  messages: Message[];
  workspace_path: string | null;
}

export interface Checkpoint {
  id: number | null;
  thread_id: string;
  run_id: string;
  step: number;
  state: ThreadState;
  created_at: string;
}

export interface WorkspaceFile {
  relative_path: string;
  name: string;
  size: number;
  modified_at: string;
}

export type RunEventType =
  | "run.start"
  | "text.delta"
  | "reasoning.delta"
  | "message.complete"
  | "tool.start"
  | "tool.end"
  | "run.end"
  | "run.error"
  | "run.interrupted"
  | "run.timeout";

export interface RunEvent {
  id: string;
  run_id: string;
  thread_id: string;
  event_type: RunEventType;
  payload: Record<string, unknown>;
  sequence: number | null;
  created_at: string;
}

export interface LiveToolEvent {
  id: string;
  toolCallId: string;
  toolName: string;
  phase: "running" | "finished";
  arguments?: Record<string, unknown>;
  content?: string;
}
