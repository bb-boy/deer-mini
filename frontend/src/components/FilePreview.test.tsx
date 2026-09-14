/// <reference types="node" />
import { Blob as NodeBlob } from "node:buffer";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FilePreview } from "./FilePreview";

vi.mock("./MarkdownContent", () => ({ MarkdownContent: ({ content }: { content: string }) => <article>{content}</article> }));

const file = { name: "report.md", relative_path: "outputs/report.md", size: 30, modified_at: "2026-09-14" };

beforeEach(() => { vi.stubGlobal("Blob", NodeBlob); });
afterEach(() => { vi.unstubAllGlobals(); });

describe("FilePreview", () => {
  it("loads the actual file, switches source view and returns to the list", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response("# 真实报告"));
    vi.stubGlobal("fetch", fetcher);
    const onBack = vi.fn();
    render(<FilePreview file={file} url="/download/report.md" onBack={onBack} />);
    expect(await screen.findByText("# 真实报告")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "源码" }));
    expect(screen.getByText("# 真实报告").tagName).toBe("CODE");
    expect(screen.getByRole("link", { name: "下载 report.md" }).getAttribute("href")).toBe("/download/report.md");
    fireEvent.click(screen.getByRole("button", { name: "返回文件列表" }));
    expect(onBack).toHaveBeenCalledOnce();
  });

  it("previews HTML in an iframe without script or same-origin permissions", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("<h1>报告</h1><script>alert(1)</script>")));
    render(<FilePreview file={{ ...file, name: "report.html" }} url="/report.html" onBack={vi.fn()} />);
    const frame = await screen.findByTitle("report.html 预览");
    expect(frame.getAttribute("sandbox")).toBe("");
    expect(frame.getAttribute("srcdoc")).toContain("default-src 'none'");
  });

  it("keeps downloads available for unsupported or oversized files", async () => {
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    const view = render(<FilePreview file={{ ...file, name: "report.docx" }} url="/report.docx" onBack={vi.fn()} />);
    expect(screen.getByText("此文件类型暂不支持预览。")).toBeTruthy();
    view.unmount();
    render(<FilePreview file={{ ...file, size: 6 * 1024 * 1024 }} url="/large.md" onBack={vi.fn()} />);
    expect((await screen.findByRole("alert")).textContent).toContain("文件超过 5 MB");
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("does not render an old response after closing the preview", async () => {
    let finish!: (response: Response) => void;
    const fetcher = vi.fn().mockReturnValue(new Promise<Response>((resolve) => { finish = resolve; }));
    vi.stubGlobal("fetch", fetcher);
    const view = render(<FilePreview file={file} url="/old.md" onBack={vi.fn()} />);
    const signal = fetcher.mock.calls[0][1].signal as AbortSignal;
    view.unmount();
    expect(signal.aborted).toBe(true);
    finish(new Response("旧对话文件"));
    await waitFor(() => expect(screen.queryByText("旧对话文件")).toBeNull());
  });
});
