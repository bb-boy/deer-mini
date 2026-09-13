import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MarkdownContent } from "./MarkdownContent";

describe("MarkdownContent", () => {
  it("renders headings, nested lists, the real tool table, links and emphasis", () => {
    const content = [
      "## 可用工具", "", "- **读取文件**", "  - 子项目", "- 执行命令", "",
      "1. 选择工具", "2. 查看结果", "",
      "| 工具 | 作用 |", "| --- | --- |",
      "| **read_file** | 读取当前工作目录下的文件内容 |",
      "| **bash** | 在当前隔离工作区中执行 Bash 命令 |", "",
      "> 引用说明", "", "---", "", "*斜体* ~~删除~~ [文档](https://example.com/docs)",
    ].join("\n");
    const { container } = render(<MarkdownContent content={content} />);
    expect(screen.getByRole("heading", { name: "可用工具", level: 2 })).toBeTruthy();
    expect(container.querySelector("ul ul li")?.textContent).toBe("子项目");
    expect(container.querySelectorAll("ol li")).toHaveLength(2);
    const table = screen.getByRole("table");
    expect(within(table).getByRole("columnheader", { name: "工具" })).toBeTruthy();
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(within(table).getByRole("cell", { name: "read_file" })).toBeTruthy();
    expect(container.querySelector("blockquote")?.textContent).toContain("引用说明");
    expect(container.querySelector("hr")).toBeTruthy();
    expect(container.querySelector("em")?.textContent).toBe("斜体");
    expect(container.querySelector("del")?.textContent).toBe("删除");
    expect(screen.getByRole("link", { name: "文档" }).getAttribute("href")).toBe("https://example.com/docs");
  });

  it("keeps nested inline code and multiline bold structured", () => {
    const { container } = render(<MarkdownContent content={"**请运行 `date` 命令**\n\n**第一行\n第二行**"} />);
    expect(container.querySelector('[data-streamdown="strong"] code')?.textContent).toBe("date");
    expect([...container.querySelectorAll('[data-streamdown="strong"]')].map((node) => node.textContent)).toEqual(["请运行 date 命令", "第一行\n第二行"]);
  });

  it.each([
    ["```python\nif x:\n    print(x)\n```", "if x:\n    print(x)"],
    ["~~~text\nhello\n~~~", "hello"],
    ["````markdown\n```python\nprint(1)\n```\n````", "```python\nprint(1)\n```"],
    ["```text\nbefore\n```not-a-close\nafter\n```", "before\n```not-a-close\nafter"],
  ])("preserves fenced code contents: %s", async (content, expected) => {
    const { container } = render(<MarkdownContent content={content} />);
    // 配套高亮组件把每行放在独立元素中，浏览器通过 block 样式保留换行。
    await waitFor(() => expect([...container.querySelectorAll('[data-streamdown="code-block-body"] pre > code > span')]
      .map((line) => line.textContent)).toEqual(expected.split("\n")));
    expect(container.querySelectorAll('[data-streamdown="code-block"]')).toHaveLength(1);
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    try {
      fireEvent.click(screen.getByRole("button", { name: "复制代码" }));
      // 围栏前的最后一个换行也是代码原文的一部分，复制时应保留。
      await waitFor(() => expect(writeText).toHaveBeenCalledWith(expected + "\n"));
    } finally {
      writeText.mockRestore();
    }
  });

  it("handles partial bold without showing the opening markers", async () => {
    const { container, rerender } = render(<MarkdownContent content="**重要内容" isLoading />);
    expect(container.querySelector('[data-streamdown="strong"]')?.textContent).toBe("重要内容");
    rerender(<MarkdownContent content="**重要内容**，接下来" isLoading />);
    await waitFor(() => expect(container.textContent).toContain("重要内容，接下来"));
    expect(container.querySelectorAll('[data-streamdown="strong"]')).toHaveLength(1);
  });

  it("reveals a long burst progressively and shows the full answer as soon as streaming ends", () => {
    vi.useFakeTimers();
    try {
      const content = "这是一段需要逐步显示的回答。".repeat(100);
      const { container, rerender } = render(<MarkdownContent content={content} isLoading />);
      expect(container.textContent).toBe("");
      act(() => { vi.advanceTimersByTime(100); });
      expect(container.textContent!.length).toBeGreaterThan(0);
      expect(container.textContent!.length).toBeLessThan(content.length);
      expect(content.startsWith(container.textContent!)).toBe(true);
      rerender(<MarkdownContent content={content} />);
      expect(container.textContent).toBe(content);
    } finally {
      vi.useRealTimers();
    }
  });

  it("hides a trailing empty list marker until its text arrives", async () => {
    const { container, rerender } = render(<MarkdownContent content={"1. 第一项\n\n2."} isLoading />);
    const items = container.querySelectorAll<HTMLLIElement>("li");
    expect(items).toHaveLength(2);
    expect(items[1].hidden).toBe(true);
    rerender(<MarkdownContent content={"1. 第一项\n\n2. 第二项"} isLoading />);
    await waitFor(() => expect(items[1].textContent).toContain("第二项"));
    expect(container.querySelectorAll("li")[0]).toBe(items[0]);
    expect(items[1].hidden).toBe(false);
  });

  it("shows unfinished code while streaming then highlights the complete block", async () => {
    const partial = "## 运行结果\n\n```bash\necho 'hello'";
    const { container, rerender } = render(<MarkdownContent content={partial} isLoading />);
    expect(container.querySelector("pre code")?.textContent).toContain("echo 'hello'");
    rerender(<MarkdownContent content={partial + "\n```\n\n- 完成"} />);
    expect(screen.getAllByRole("heading")).toHaveLength(1);
    expect(screen.getByRole("listitem").textContent).toBe("完成");
    await waitFor(() => expect(container.querySelector('[data-streamdown="code-block-body"]')?.textContent).toContain("echo 'hello'"));
    expect(container.querySelectorAll('[data-streamdown="code-block"]')).toHaveLength(1);
  });

  it("renders model LaTeX delimiters without modifying code literals", () => {
    const content = "公式 \\(x^2\\)\n\n\\[\nx + y\n\\]\n\n`\\(literal\\)`";
    const { container } = render(<MarkdownContent content={content} />);
    expect(container.querySelectorAll(".katex")).toHaveLength(2);
    expect(container.querySelector(".katex-display")).toBeTruthy();
    expect(container.querySelector("code")?.textContent).toBe("\\(literal\\)");
  });

  it("keeps raw HTML inert and filters unsafe link destinations", () => {
    const content = '<script>alert(1)</script>\n\n<img src=x onerror="alert(1)">\n\n[危险链接](javascript:alert(1))\n\n[文档](https://example.com/docs)';
    const { container } = render(<MarkdownContent content={content} />);
    expect(container.querySelector("script, img[onerror], a[href^='javascript:']")).toBeNull();
    expect(screen.getByRole("link", { name: "文档" }).getAttribute("rel")).toContain("noopener");
  });
});
