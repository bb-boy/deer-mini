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
  // 仅前端实时片段使用，不写入后端对话历史。
  generation_interrupted?: boolean;
}

export type TodoStatus = "pending" | "in_progress" | "completed";

export interface TodoItem {
  content: string;
  status: TodoStatus;
}

export interface ThreadState {
  thread_id: string;
  user_id: string;
  messages: Message[];
  workspace_path: string | null;
  // 可选字段兼容升级前返回的旧快照；新后端默认返回空列表和 null。
  todos?: TodoItem[];
  todos_run_id?: string | null;
  todos_tool_call_id?: string | null;
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
  | "model.status"
  | "model.interrupted"
  | "subagent.model.status"
  | "subagent.model.interrupted"
  | "tool.start"
  | "tool.end"
  | "todos.updated"
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

export interface RestorePoint {
  id: string;
  run_id: string | null;
  kind: "turn_start" | "recovery";
  created_at: string;
  message: string;
  available: boolean;
  unavailable_reason: string | null;
}

export interface RestorePreview {
  restore_point_id: string;
  revision: number;
  fingerprint: string;
  created: string[];
  modified: string[];
  deleted: string[];
  removed_messages: number;
  target_messages: number;
  current_messages: number;
}

export interface RestoreInput {
  operation_id: string;
  restore_point_id: string;
  revision: number;
  fingerprint: string;
}

export interface RestoreOperation {
  operation_id: string;
  thread_id: string;
  restore_point_id: string;
  recovery_point_id: string | null;
  status: "prepared" | "applying" | "committed" | "rolled_back" | "needs_recovery";
  error: string | null;
  cleaned: boolean | number;
  created_at: string;
  updated_at: string;
}
