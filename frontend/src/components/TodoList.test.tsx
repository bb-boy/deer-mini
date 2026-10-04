import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Message, TodoItem } from "../api/types";
import { MessageList } from "./MessageList";
import { parseTodoResult, TodoList } from "./TodoList";

const todos: TodoItem[] = [
  { content: "查找资料", status: "completed" },
  { content: "读取正文", status: "in_progress" },
  { content: "整理回答", status: "pending" },
];
const saved = JSON.stringify({ status: "saved", run_id: "run", tool_call_id: "plan", todos });

function message(id: string, role: Message["role"], content: string, extra: Partial<Message> = {}): Message {
  return { id, role, content, created_at: "2026-09-20T00:00:00Z", tool_calls: [],
    tool_call_id: null, reasoning_content: null, ...extra };
}
const user = message("user", "user", "请列清单处理资料");
const request = message("request", "assistant", "", {
  tool_calls: [{ id: "plan", name: "write_todos", arguments: { todos } }],
});
const result = message("result", "tool", saved, { tool_call_id: "plan" });

describe("任务清单", () => {
  it("显示真实的三种状态和完成数量", () => {
    render(<TodoList todos={todos} />);
    const list = screen.getByRole("region", { name: "已保存的任务清单" });
    expect(within(list).getByText("已完成 1/3")).toBeTruthy();
    expect(within(list).getByText("待办")).toBeTruthy();
    expect(within(list).getByText("进行中")).toBeTruthy();
    expect(within(list).getByText("已完成")).toBeTruthy();
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);
  });

  it("空列表显示已清空", () => {
    render(<TodoList todos={[]} />);
    expect(screen.getByText("任务清单已清空")).toBeTruthy();
  });

  it.each([
    undefined, "执行工具失败", "null", "[]", "{}", JSON.stringify({ todos }),
    JSON.stringify({ status: "pending", run_id: "run", tool_call_id: "plan", todos }),
    JSON.stringify({ status: "saved", run_id: "run", tool_call_id: "other", todos }),
    JSON.stringify({ status: "saved", tool_call_id: "plan", todos }),
    JSON.stringify({ status: "saved", run_id: "run", tool_call_id: "plan", todos: [{ content: "a", status: "broken" }] }),
    JSON.stringify({ status: "saved", run_id: "run", tool_call_id: "plan", todos: [{ content: " ", status: "completed" }] }),
  ])("不把错误、参数或其他调用的结果识别为确认清单：%s", (content) => {
    expect(parseTodoResult(content, "plan")).toBeNull();
  });

  it("等待工具确认，收到结果后显示；刷新后从历史消息继续显示且不重复", () => {
    const { rerender } = render(<MessageList messages={[user, request]} running />);
    expect(screen.queryByRole("region", { name: "已保存的任务清单" })).toBeNull();
    rerender(<MessageList messages={[user, request]} running toolEvents={[
      { id: "event", toolCallId: "plan", toolName: "write_todos", phase: "finished", content: saved },
    ]} />);
    expect(screen.getByText("更新任务清单")).toBeTruthy();
    expect(screen.getByRole("region", { name: "已保存的任务清单" })).toBeTruthy();
    rerender(<MessageList messages={[user, request, result]} />);
    expect(screen.getAllByRole("region", { name: "已保存的任务清单" })).toHaveLength(1);
    expect(screen.getByText("已完成 1/3")).toBeTruthy();
  });

  it("失败或结果丢失时显示未确认，不用调用参数冒充已保存进度", () => {
    const { rerender } = render(<MessageList messages={[user, request]} />);
    expect(screen.getByText("清单尚未确认保存，请查看工具结果")).toBeTruthy();
    expect(screen.queryByRole("region", { name: "已保存的任务清单" })).toBeNull();
    rerender(<MessageList messages={[user, request, { ...result, content: "保存失败" }]} />);
    expect(screen.getByText("清单尚未确认保存，请查看工具结果")).toBeTruthy();
    expect(screen.queryByRole("region", { name: "已保存的任务清单" })).toBeNull();
  });
});
