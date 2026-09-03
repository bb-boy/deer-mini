import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ChatComposer } from "./ChatComposer";


describe("ChatComposer", () => {
  it("submits message and selected model options", () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    render(
      <ChatComposer disabled={false} running={false} onSend={onSend} onCancel={vi.fn()} />,
    );

    fireEvent.change(screen.getByLabelText("给 Agent 的消息"), {
      target: { value: "读取 report.txt" },
    });
    fireEvent.click(screen.getByLabelText("开启思考"));
    fireEvent.change(screen.getByLabelText("推理强度"), {
      target: { value: "medium" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送任务" }));

    expect(onSend).toHaveBeenCalledWith({
      message: "读取 report.txt",
      modelName: "ustc-deepseek-flash",
      thinkingEnabled: true,
      reasoningEffort: "medium",
    });
  });

  it("shows a stop action while a run is active", () => {
    const onCancel = vi.fn();
    render(
      <ChatComposer disabled={false} running={true} onSend={vi.fn()} onCancel={onCancel} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "停止运行" }));
    expect(onCancel).toHaveBeenCalledOnce();
  });
});
