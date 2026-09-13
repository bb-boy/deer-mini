import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { LiveToolEvent, Message } from "../api/types";
import { MessageList } from "./MessageList";

function message(id: string, role: Message["role"], content: string, extra: Partial<Message> = {}): Message {
  return {
    id, role, content, created_at: "2026-01-01T00:00:00+00:00",
    tool_calls: [], tool_call_id: null, reasoning_content: null, ...extra,
  };
}

const user = message("question", "user", "现在是什么时间");
const request = message("request", "assistant", "让我查看一下当前的时间。", {
  tool_calls: [{ id: "call-1", name: "bash", arguments: { command: "date" } }],
});
const result = message("result", "tool", "Sun Sep 13 07:28:32 UTC 2026", { tool_call_id: "call-1" });
const answer = message("answer", "assistant", "当前时间是 07:28:32 UTC。");
const toolEvent: LiveToolEvent = {
  id: "call-1", toolCallId: "call-1", toolName: "bash", phase: "running", arguments: { command: "date" },
};

describe("MessageList", () => {
  it("uses the same Markdown rendering for reasoning, tool commentary and answers", () => {
    const formattedRequest = { ...request, content: "## 工具前说明\n\n- 读取时间", reasoning_content: "## 判断依据\n\n**需要工具**" };
    const formattedAnswer = { ...answer, content: "## 最终回答\n\n| 时间 | 时区 |\n| --- | --- |\n| 18:00 | CST |" };
    const { container } = render(<MessageList messages={[user, formattedRequest, result, formattedAnswer]} />);
    fireEvent.click(screen.getByRole("button", { name: "查看前面的 1 个步骤" }));
    fireEvent.click(screen.getByRole("button", { name: "思考过程" }));
    expect(container.querySelector(".reasoning .markdown-content h2")?.textContent).toBe("判断依据");
    expect(container.querySelector('.reasoning [data-streamdown="strong"]')?.textContent).toBe("需要工具");
    expect(container.querySelector(".reply-process-message ul li")?.textContent).toBe("读取时间");
    expect(container.querySelector(".reply-answer h2")?.textContent).toBe("最终回答");
    expect(container.querySelectorAll(".reply-answer table tr")).toHaveLength(2);
  });

  it("keeps user text identical before and after persistence and copies its original markers", () => {
    const content = "请保留 **hello**、`date` 和换行\n第二行";
    const onCopy = vi.fn();
    const { container, rerender } = render(<MessageList messages={[]} pendingUserMessage={content} />);
    expect(container.querySelector(".message-row-user .message-content")?.textContent).toBe(content);
    rerender(<MessageList messages={[{ ...user, content }]} onCopy={onCopy} />);
    expect(container.querySelector(".message-row-user .message-content")?.textContent).toBe(content);
    expect(container.querySelector(".message-row-user strong, .message-row-user code")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "复制消息" }));
    expect(onCopy).toHaveBeenCalledWith(content);
  });

  it("preserves tool stdout literally while formatting the assistant summary", () => {
    const stdout = "**literal** `literal`\n    indented output\n```";
    const onCopy = vi.fn();
    const { container } = render(<MessageList messages={[user, request, { ...result, content: stdout }, answer]} onCopy={onCopy} />);
    fireEvent.click(screen.getByRole("button", { name: /（bash）详情/ }));
    expect(container.querySelector(".reply-tool-result")?.textContent).toBe(stdout);
    expect(container.querySelector(".reply-tool-result strong, .reply-tool-result code")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "复制工具结果" }));
    expect(onCopy).toHaveBeenCalledWith(stdout);
  });

  it("keeps a table intact across streaming updates and the saved history refresh", async () => {
    const chunks = ["## 工", "具\n\n| 工具 | 作用 |\n| --", "- | --- |\n| **ba", "sh** | 命令 |\n| read_file | 文件 |"];
    const { container, rerender } = render(<MessageList messages={[user]} running />);
    let content = "";
    for (const chunk of chunks) {
      content += chunk;
      rerender(<MessageList messages={[user]} liveMessages={[{ ...answer, content }]} running />);
    }
    await waitFor(() => expect(container.querySelectorAll("table tr")).toHaveLength(3));
    expect(container.querySelector("h2")?.textContent).toBe("工具");
    const onCopy = vi.fn();
    rerender(<MessageList messages={[user, { ...answer, content }]} onCopy={onCopy} />);
    expect(container.querySelectorAll("table tr")).toHaveLength(3);
    expect(container.querySelectorAll(".message-row-assistant")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "复制回复" }));
    expect(onCopy).toHaveBeenCalledWith(content);
  });

  it("hides system instructions without removing them from the supplied state", () => {
    const system = message("system", "system", "[deer_mini runtime context] workspace=/private/path");
    const messages = [system, user, answer];
    const { rerender } = render(<MessageList messages={messages} />);
    expect(screen.queryByText(/deer_mini runtime context/)).toBeNull();
    expect(screen.getByText(answer.content)).toBeTruthy();
    expect(messages[0]).toBe(system);
    rerender(<MessageList messages={[system]} />);
    expect(screen.getByText("你好，我是 DeerMini")).toBeTruthy();
  });

  it("shows the latest tool summary with hidden details and copies only the final answer", () => {
    const onCopy = vi.fn();
    const { container } = render(<MessageList messages={[user, request, result, answer]} onCopy={onCopy} />);
    const replies = container.querySelectorAll<HTMLElement>(".message-row-assistant");
    expect(replies).toHaveLength(1);
    expect(container.querySelectorAll(".message-row-tool")).toHaveLength(0);
    expect(within(replies[0]).getByText(answer.content)).toBeTruthy();
    const process = replies[0].querySelector<HTMLElement>(".reply-process")!;
    const trigger = within(process).getByRole("button", { name: /（bash）详情/ });
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(within(process).queryByText(result.content)).toBeNull();
    fireEvent.click(trigger);
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    expect(within(process).getByText(request.content)).toBeTruthy();
    expect(within(process).getByText(result.content)).toBeTruthy();
    fireEvent.click(within(replies[0]).getByRole("button", { name: "复制回复" }));
    expect(onCopy).toHaveBeenCalledWith(answer.content);
  });

  it("keeps separate questions in separate replies and retains orphan tool results", () => {
    const secondUser = message("question-2", "user", "再总结一下");
    const secondAnswer = message("answer-2", "assistant", "这是第二次回复。");
    const { container } = render(<MessageList messages={[user, result, answer, secondUser, secondAnswer]} />);
    const replies = container.querySelectorAll<HTMLElement>(".message-row-assistant");
    expect(replies).toHaveLength(2);
    fireEvent.click(within(replies[0]).getByRole("button", { name: /详情/ }));
    expect(within(replies[0]).getByText(result.content)).toBeTruthy();
    expect(within(replies[1]).queryByText(result.content)).toBeNull();
    expect(within(replies[1]).getByText(secondAnswer.content)).toBeTruthy();
  });

  it("keeps streaming and saved steps in the same reply without duplicates after refresh", () => {
    const { container, rerender } = render(
      <MessageList messages={[user, request]} liveMessages={[request]} toolEvents={[toolEvent]} running />,
    );
    const reply = container.querySelector<HTMLElement>(".message-row-assistant")!;
    fireEvent.click(within(reply).getByRole("button", { name: /（bash）详情/ }));
    expect(container.querySelectorAll(".message-row-assistant")).toHaveLength(1);
    expect(screen.getAllByText(request.content)).toHaveLength(1);
    expect(reply.querySelectorAll('[data-tool-call-id="call-1"]')).toHaveLength(1);
    const finished: LiveToolEvent = { ...toolEvent, phase: "finished", content: result.content };
    rerender(<MessageList messages={[user, request]} liveMessages={[request, answer]} toolEvents={[finished]} running />);
    expect(container.querySelector(".message-row-assistant")).toBe(reply);
    expect(reply.querySelector(".reply-answer .markdown-content")?.textContent).toBe(answer.content);
    rerender(<MessageList messages={[user, request, result, answer]} liveMessages={[request, answer]} toolEvents={[finished]} />);
    expect(container.querySelector(".message-row-assistant")).toBe(reply);
    expect([...reply.querySelectorAll(".reply-answer .markdown-content")].map((node) => node.textContent)).toEqual([answer.content]);
    expect(screen.getAllByText(result.content)).toHaveLength(1);
    expect(reply.querySelectorAll('[data-tool-call-id="call-1"]')).toHaveLength(1);
    expect(within(reply).getByRole("button", { name: /（bash）详情/ }).getAttribute("aria-expanded")).toBe("true");
  });

  it("respects a manual tool detail toggle while streaming continues", () => {
    const { container, rerender } = render(<MessageList messages={[user, request]} toolEvents={[toolEvent]} running />);
    const process = container.querySelector<HTMLElement>(".reply-process")!;
    const trigger = within(process).getByRole("button", { name: /（bash）详情/ });
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(trigger);
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    rerender(<MessageList messages={[user, request]} toolEvents={[toolEvent]} liveMessages={[answer]} running />);
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
  });

  it("attaches a pending run to its new question instead of the previous reply", () => {
    const { container } = render(
      <MessageList messages={[user, answer]} pendingUserMessage="读取报告"
        liveMessages={[request]} toolEvents={[toolEvent]} running />,
    );
    const replies = container.querySelectorAll<HTMLElement>(".message-row-assistant");
    expect(replies).toHaveLength(2);
    expect(within(replies[0]).queryByText(request.content)).toBeNull();
    expect(within(replies[1]).getByText(request.content)).toBeTruthy();
    expect(screen.getByText("读取报告")).toBeTruthy();
  });

  it("preserves multiple tool rounds with the same tool name inside one process", () => {
    const secondRequest = message("request-2", "assistant", "再检查本地时区。", {
      tool_calls: [{ id: "call-2", name: "bash", arguments: { command: "date +%Z" } }],
    });
    const secondResult = message("result-2", "tool", "UTC", { tool_call_id: "call-2" });
    const { container } = render(<MessageList messages={[user, request, result, secondRequest, secondResult, answer]} />);
    expect(container.querySelectorAll(".message-row-assistant")).toHaveLength(1);
    expect(container.querySelectorAll(".reply-tool")).toHaveLength(2);
    const process = container.querySelector<HTMLElement>(".reply-process")!;
    expect(within(process).getAllByRole("button", { name: /（bash）详情/ })).toHaveLength(1);
    const more = within(process).getByRole("button", { name: "查看前面的 1 个步骤" });
    fireEvent.click(more);
    expect(within(process).getAllByRole("button", { name: /（bash）详情/ })).toHaveLength(2);
    fireEvent.click(within(process.querySelector<HTMLElement>('[data-tool-call-id="call-2"]')!)
      .getByRole("button", { name: /详情/ }));
    expect(within(process).getByText(secondRequest.content)).toBeTruthy();
    expect(within(process).getByText(secondResult.content)).toBeTruthy();
    expect(within(process).queryByText(answer.content)).toBeNull();
  });

  it("does not leave an interrupted tool looking as though it is still running", () => {
    const { container } = render(<MessageList messages={[user, request]} toolEvents={[toolEvent]} running={false} />);
    const tool = container.querySelector<HTMLElement>(".reply-tool")!;
    expect(within(tool).getByText("未收到结果")).toBeTruthy();
    expect(screen.queryByText("正在运行")).toBeNull();
  });

  it("streams real reasoning before text, auto-collapses it and does not duplicate it in history", () => {
    vi.useFakeTimers();
    try {
      const thinking = { ...answer, content: "", reasoning_content: "先确认已有资料。" };
      const { container, rerender } = render(<MessageList messages={[user]} liveMessages={[thinking]}
        running streamingMessageId={answer.id} reasoningMessageId={answer.id} />);
      expect(screen.getByRole("button", { name: "正在思考…" }).getAttribute("aria-expanded")).toBe("true");
      expect(screen.getAllByText(thinking.reasoning_content)).toHaveLength(1);
      const withAnswer = { ...thinking, content: answer.content };
      rerender(<MessageList messages={[user]} liveMessages={[withAnswer]} running
        streamingMessageId={answer.id} reasoningMessageId={null} />);
      act(() => { vi.advanceTimersByTime(1100); });
      expect(screen.getByRole("button", { name: "思考过程" }).getAttribute("aria-expanded")).toBe("false");
      rerender(<MessageList messages={[user, withAnswer]} />);
      expect(container.querySelectorAll(".message-row-assistant")).toHaveLength(1);
      expect(screen.getAllByRole("button", { name: "思考过程" })).toHaveLength(1);
      expect(screen.getAllByText(answer.content)).toHaveLength(1);
      fireEvent.click(screen.getByRole("button", { name: "思考过程" }));
      expect(screen.getAllByText(thinking.reasoning_content)).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("preserves a manual reasoning choice after text starts and the final message is saved", () => {
    vi.useFakeTimers();
    try {
      const thinking = { ...answer, content: "", reasoning_content: "这是真实返回的思考。" };
      const { rerender } = render(<MessageList messages={[user]} liveMessages={[thinking]} running
        streamingMessageId={answer.id} reasoningMessageId={answer.id} />);
      fireEvent.click(screen.getByRole("button", { name: "正在思考…" }));
      fireEvent.click(screen.getByRole("button", { name: "正在思考…" }));
      const complete = { ...thinking, content: answer.content };
      rerender(<MessageList messages={[user]} liveMessages={[complete]} running
        streamingMessageId={answer.id} reasoningMessageId={null} />);
      act(() => { vi.advanceTimersByTime(1100); });
      expect(screen.getByRole("button", { name: "思考过程" }).getAttribute("aria-expanded")).toBe("true");
      rerender(<MessageList messages={[user, complete]} />);
      expect(screen.getByRole("button", { name: "思考过程" }).getAttribute("aria-expanded")).toBe("true");
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps unresolved text in place when a tool call arrives without inventing reasoning", () => {
    const { container, rerender } = render(<MessageList messages={[user]}
      liveMessages={[{ ...request, tool_calls: [] }]} running streamingMessageId={request.id} />);
    const textStep = container.querySelector(".reply-process-message");
    expect(textStep).toBeTruthy();
    expect(container.querySelector(".reasoning")).toBeNull();
    rerender(<MessageList messages={[user]} liveMessages={[request]} toolEvents={[toolEvent]}
      running streamingMessageId={null} />);
    expect(container.querySelector(".reply-process-message")).toBe(textStep);
    expect(container.querySelectorAll(".message-row-assistant")).toHaveLength(1);
    expect(container.querySelector(".reasoning")).toBeNull();
  });
});
