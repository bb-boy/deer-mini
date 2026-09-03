import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { WorkspaceFiles } from "./WorkspaceFiles";


describe("WorkspaceFiles", () => {
  it("renders download links and forwards a selected upload", () => {
    const onUpload = vi.fn();
    render(
      <WorkspaceFiles
        files={[
          {
            relative_path: "outputs/report.txt",
            name: "report.txt",
            size: 2048,
            modified_at: "2026-01-01T00:00:00+00:00",
          },
        ]}
        disabled={false}
        downloadUrl={(path) => `/download/${path}`}
        onUpload={onUpload}
      />,
    );

    const link = screen.getByRole("link", { name: /report.txt/ });
    expect(link.getAttribute("href")).toBe("/download/outputs/report.txt");

    const file = new File(["hello"], "hello.txt", { type: "text/plain" });
    fireEvent.change(screen.getByLabelText("上传文件"), {
      target: { files: [file] },
    });
    expect(onUpload).toHaveBeenCalledWith(file);
  });
});
