import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RunTimeline } from "./RunTimeline";


describe("RunTimeline", () => {
  it("shows running and completed tool steps", () => {
    render(
      <RunTimeline
        events={[
          {
            id: "call-1",
            toolCallId: "call-1",
            toolName: "read_file",
            phase: "running",
            arguments: { path: "report.txt" },
          },
          {
            id: "call-2",
            toolCallId: "call-2",
            toolName: "bash",
            phase: "finished",
            content: "ok",
          },
        ]}
      />,
    );

    expect(screen.getByText("read_file")).toBeTruthy();
    expect(screen.getByText("运行中")).toBeTruthy();
    expect(screen.getByText("bash")).toBeTruthy();
    expect(screen.getByText("已完成")).toBeTruthy();
  });
});
