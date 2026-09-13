import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ChatComposer } from "./ChatComposer";
import type { ModelProfile } from "../api/types";

const models: ModelProfile[] = [
  { name: "ustc-deepseek-flash", display_name: "USTC Flash", supports_thinking: true, supports_reasoning_effort: true },
  { name: "siliconflow-deepseek-flash", display_name: "SiliconFlow Flash", supports_thinking: true, supports_reasoning_effort: true },
];
const modelProps = { models, defaultModelName: "siliconflow-deepseek-flash" };

beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });

describe("ChatComposer", () => {
  it("submits message and selected model options", () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    render(
      <ChatComposer {...modelProps} disabled={false} running={false} onSend={onSend} onCancel={vi.fn()} />,
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
      modelName: "siliconflow-deepseek-flash",
      thinkingEnabled: true,
      reasoningEffort: "medium",
    });
  });

  it("shows a stop action while a run is active", () => {
    const onCancel = vi.fn();
    render(
      <ChatComposer {...modelProps} disabled={false} running={true} onSend={vi.fn()} onCancel={onCancel} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "停止运行" }));
    expect(onCancel).toHaveBeenCalledOnce();
  });

  it("uses the backend default instead of the legacy hardcoded model preference", () => {
    localStorage.setItem("deer-mini-model", "ustc-deepseek-flash");
    const onSend = vi.fn().mockResolvedValue(undefined);
    render(<ChatComposer {...modelProps} disabled={false} running={false} onSend={onSend} onCancel={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("给 Agent 的消息"), { target: { value: "你好" } });
    fireEvent.click(screen.getByRole("button", { name: "发送任务" }));
    expect(onSend).toHaveBeenCalledWith(expect.objectContaining({ modelName: "siliconflow-deepseek-flash" }));
  });

  it("follows updated defaults while preserving an explicit model selection", () => {
    const props = { ...modelProps, disabled: false, running: false, onSend: vi.fn(), onCancel: vi.fn() };
    const { rerender } = render(<ChatComposer {...props} />);
    const select = screen.getByLabelText("模型") as HTMLSelectElement;
    expect(Array.from(select.options, (option) => option.textContent)).toEqual(["默认 · SiliconFlow Flash", "USTC Flash"]);
    expect(select.selectedOptions[0].textContent).toBe("默认 · SiliconFlow Flash");
    rerender(<ChatComposer {...props} defaultModelName="ustc-deepseek-flash" />);
    expect(select.selectedOptions[0].textContent).toBe("默认 · USTC Flash");
    fireEvent.change(select, { target: { value: "siliconflow-deepseek-flash" } });
    rerender(<ChatComposer {...props} defaultModelName="ustc-deepseek-flash" />);
    expect(select.value).toBe("siliconflow-deepseek-flash");
    expect(localStorage.getItem("deer-mini-model-choice")).toBe("siliconflow-deepseek-flash");
    // 手动选择的模型成为默认项时仍正确显示，再切换默认也不会丢失原选择。
    rerender(<ChatComposer {...props} />);
    expect(select.selectedOptions[0].textContent).toBe("默认 · SiliconFlow Flash");
    expect(select.options).toHaveLength(2);
    rerender(<ChatComposer {...props} defaultModelName="ustc-deepseek-flash" />);
    expect(select.value).toBe("siliconflow-deepseek-flash");
  });

  it("does not switch provider when the user selects a thinking mode", () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    render(<ChatComposer {...modelProps} disabled={false} running={false} onSend={onSend} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByText("专业", { selector: "strong" }));
    fireEvent.change(screen.getByLabelText("给 Agent 的消息"), { target: { value: "帮我分析" } });
    fireEvent.click(screen.getByRole("button", { name: "发送任务" }));
    expect(onSend).toHaveBeenCalledWith(expect.objectContaining({
      modelName: "siliconflow-deepseek-flash", thinkingEnabled: true, reasoningEffort: "medium",
    }));
  });

  it("selects by profile name even when two models have the same display name", () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    render(<ChatComposer models={models.map((model) => ({ ...model, display_name: "DeepSeek V4 Flash" }))}
      defaultModelName="ustc-deepseek-flash" disabled={false} running={false} onSend={onSend} onCancel={vi.fn()} />);
    expect(screen.getAllByRole("option", { name: "DeepSeek V4 Flash" })).toHaveLength(1);
    expect(screen.getByRole("option", { name: "默认 · DeepSeek V4 Flash" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("模型"), { target: { value: "siliconflow-deepseek-flash" } });
    fireEvent.change(screen.getByLabelText("给 Agent 的消息"), { target: { value: "使用硅基流动" } });
    fireEvent.click(screen.getByRole("button", { name: "发送任务" }));
    expect(onSend).toHaveBeenCalledWith(expect.objectContaining({ modelName: "siliconflow-deepseek-flash" }));
  });

  it("waits for model configuration and obeys the selected model capabilities", () => {
    localStorage.setItem("deer-mini-thinking", "true");
    const onSend = vi.fn().mockResolvedValue(undefined);
    const props = { disabled: false, running: false, onSend, onCancel: vi.fn(), defaultModelName: "plain-chat" };
    const { rerender } = render(<ChatComposer {...props} models={[]} />);
    fireEvent.change(screen.getByLabelText("给 Agent 的消息"), { target: { value: "你好" } });
    expect((screen.getByRole("button", { name: "发送任务" }) as HTMLButtonElement).disabled).toBe(true);
    rerender(<ChatComposer {...props} models={[{
      name: "plain-chat", display_name: "普通聊天", supports_thinking: false, supports_reasoning_effort: false,
    }]} />);
    expect((screen.getByLabelText("开启思考") as HTMLInputElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "发送任务" }));
    expect(onSend).toHaveBeenCalledWith(expect.objectContaining({ modelName: "plain-chat", thinkingEnabled: false, reasoningEffort: null }));
  });
});
