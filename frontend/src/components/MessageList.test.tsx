import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MessageList } from "./MessageList";


describe("MessageList", () => {
  it("renders user, assistant, tool history and a live draft", () => {
    render(
      <MessageList
        messages={[
          {
            id: "m1",
            role: "user",
            content: "读取报告",
            created_at: "2026-01-01T00:00:00+00:00",
            tool_calls: [],
            tool_call_id: null,
            reasoning_content: null,
          },
          {
            id: "m2",
            role: "tool",
            content: "报告内容",
            created_at: "2026-01-01T00:00:01+00:00",
            tool_calls: [],
            tool_call_id: "call-1",
            reasoning_content: null,
          },
        ]}
        pendingUserMessage="继续总结"
        liveAssistantText="总结中…"
      />,
    );

    expect(screen.getByText("读取报告")).toBeTruthy();
    expect(screen.getByText("报告内容")).toBeTruthy();
    expect(screen.getByText("继续总结")).toBeTruthy();
    expect(screen.getByText("总结中…")).toBeTruthy();
  });
});
