import { CircleCheckIcon, CircleDotIcon, CircleIcon } from "lucide-react";
import type { TodoItem, TodoStatus } from "../api/types";
import "./todo-list.css";

const statuses: Record<TodoStatus, { label: string; icon: typeof CircleIcon }> = {
  pending: { label: "待办", icon: CircleIcon },
  in_progress: { label: "进行中", icon: CircleDotIcon },
  completed: { label: "已完成", icon: CircleCheckIcon },
};

// 只认后台已经确认的结果，不把模型提出的调用参数当成已保存的清单。
export function parseTodoResult(content: string | undefined, toolCallId: string): TodoItem[] | null {
  if (!content) return null;
  try {
    const result = JSON.parse(content) as Record<string, unknown> | null;
    if (!result || result.status !== "saved" || result.tool_call_id !== toolCallId
      || typeof result.run_id !== "string" || !result.run_id.trim() || !Array.isArray(result.todos)) return null;
    const items = result.todos as unknown[];
    if (!items.every((item): item is TodoItem => {
      if (!item || typeof item !== "object") return false;
      const todo = item as Record<string, unknown>;
      return typeof todo.content === "string" && !!todo.content.trim()
        && (todo.status === "pending" || todo.status === "in_progress" || todo.status === "completed");
    })) return null;
    return items;
  } catch {
    return null;
  }
}

// 这里只展示某次工具更新后的快照；不会替模型执行工作或自动改变完成状态。
export function TodoList({ todos }: { todos: TodoItem[] }) {
  const completed = todos.filter((todo) => todo.status === "completed").length;
  return (
    <section className="todo-list" aria-label="已保存的任务清单">
      <div className="todo-list-heading">
        <span>任务清单</span><span className="todo-list-count">已完成 {completed}/{todos.length}</span>
      </div>
      {todos.length === 0 ? <p className="todo-list-empty">任务清单已清空</p> : <ol className="todo-list-items">
        {todos.map((todo, index) => {
          const { label, icon: Icon } = statuses[todo.status];
          return <li key={index} className={`todo-list-item todo-${todo.status}`}>
            <Icon size={15} aria-hidden="true" />
            <span className="todo-list-content">{todo.content}</span>
            <span className="todo-list-status">{label}</span>
          </li>;
        })}
      </ol>}
    </section>
  );
}
