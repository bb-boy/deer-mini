import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("streamdown", async (importOriginal) => {
  const original = await importOriginal<typeof import("streamdown")>();
  return {
    ...original,
    Streamdown: (props: import("streamdown").StreamdownProps) => {
      if (props.children === "触发解析异常") throw new Error("diagnostic parser error");
      return <original.Streamdown {...props} />;
    },
  };
});
import { MarkdownContent } from "./MarkdownContent";

describe("Markdown fallback", () => {
  it("keeps the failed message readable and retries on the next content update", () => {
    const logged = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      const { container, rerender } = render(<><MarkdownContent content="触发解析异常" /><span>其他消息</span></>);
      expect(container.querySelector(".markdown-fallback")?.textContent).toBe("触发解析异常");
      expect(screen.getByText("其他消息")).toBeTruthy();
      rerender(<><MarkdownContent content="## 已恢复" /><span>其他消息</span></>);
      expect(screen.getByRole("heading", { name: "已恢复" })).toBeTruthy();
      expect(container.querySelector(".markdown-fallback")).toBeNull();
    } finally {
      logged.mockRestore();
    }
  });
});
