import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ThreadSidebar } from "./ThreadSidebar";


describe("ThreadSidebar", () => {
  it("switches user, creates a thread, and selects history", () => {
    const onUserIdCommit = vi.fn();
    const onCreate = vi.fn();
    const onSelect = vi.fn();
    render(
      <ThreadSidebar
        userId="alice"
        threads={[
          {
            id: "thread-1",
            user_id: "alice",
            workspace_path: "/tmp/workspace",
            title: "报告分析",
            status: "idle",
            created_at: "2026-01-01T00:00:00+00:00",
            updated_at: "2026-01-01T00:00:00+00:00",
          },
        ]}
        selectedThreadId={null}
        loading={false}
        onUserIdCommit={onUserIdCommit}
        onCreate={onCreate}
        onSelect={onSelect}
      />,
    );

    fireEvent.change(screen.getByLabelText("当前用户 ID"), {
      target: { value: "bob" },
    });
    fireEvent.click(screen.getByRole("button", { name: "切换用户" }));
    fireEvent.click(screen.getByRole("button", { name: "新建对话" }));
    fireEvent.click(screen.getByRole("button", { name: /报告分析/ }));

    expect(onUserIdCommit).toHaveBeenCalledWith("bob");
    expect(onCreate).toHaveBeenCalledOnce();
    expect(onSelect).toHaveBeenCalledWith("thread-1");
  });
});
