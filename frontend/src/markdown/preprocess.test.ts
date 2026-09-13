import { describe, expect, it } from "vitest";
import { capMarkdownNesting, normalizeStreamdownMathMarkdown, preprocessStreamdownMarkdown, stripLeakedSystemTags } from "./preprocess";

describe("DeerFlow Markdown preprocessing", () => {
  it("preserves ordinary nested lists and code", () => {
    const input = "- a\n  - b\n\n> quote\n\n```python\n    print(1)\n```";
    expect(capMarkdownNesting(input)).toBe(input);
  });
  it("bounds nesting that would overflow the Markdown parser", () => {
    const result = capMarkdownNesting("> ".repeat(3000) + "quote\n" + " ".repeat(1000) + "- item");
    expect((result.split("\n")[0].match(/>/g) ?? [])).toHaveLength(100);
    expect(result.split("\n")[1]).toBe(" ".repeat(200) + "- item");
  });
  it("keeps literal deep indentation inside code blocks", () => {
    const input = "```text\n" + " ".repeat(400) + "literal\n```";
    expect(capMarkdownNesting(input)).toBe(input);
  });
  it("normalizes multiline display math and leaves code literals intact", () => {
    const input = "\\[\nx\n= y\n\\]\n\n`\\(literal\\)`\n\n```tex\n\\[x\\]\n```";
    expect(normalizeStreamdownMathMarkdown(input)).toBe("$$\nx = y\n$$\n\n`\\(literal\\)`\n\n```tex\n\\[x\\]\n```");
  });
  it("keeps TeX comment newlines so the next line is not swallowed", () => {
    const input = "$$\nx % comment\n+ y\n$$";
    expect(normalizeStreamdownMathMarkdown(input)).toBe(input);
  });
  it("keeps nested fence examples literal when stripping leaked tags", () => {
    const input = "<memory>text</memory>\n\n````markdown\n```html\n<memory>literal</memory>\n```\n````";
    expect(stripLeakedSystemTags(input)).toBe(input.replace("<memory>text</memory>", "text"));
  });
  it("normalizes a complete Mermaid dotted arrow without rewriting incomplete code", () => {
    const partial = "```mermaid\ngraph LR\nA -- \"提示\" -.-> B";
    expect(preprocessStreamdownMarkdown(partial)).toBe(partial);
    expect(preprocessStreamdownMarkdown(partial + "\n```")).toBe("```mermaid\ngraph LR\nA -. \"提示\" .-> B\n```");
  });
});
