"""
模型能力配置。

这里只保存非秘密信息，API Key 仍然放在 .env 中。
"""

from dataclasses import dataclass
from typing import Literal


ModelProvider = Literal[
    "openai_compatible",
]


# 不同模型可能使用不同的思考协议。
ThinkingFormat = Literal[
    "none",
    "deepseek",
]


@dataclass(frozen=True)
class ModelProfile:
    # Run.model_name 保存的内部配置名。
    name: str

    # 前端展示的名称。
    display_name: str

    # 使用哪一种模型适配器。
    provider: ModelProvider

    # 发送给 USTC 的真实模型 ID。
    model_id: str

    # 从哪个环境变量读取连接信息。
    api_key_env: str
    base_url_env: str

    # 模型能力。
    supports_thinking: bool = False
    supports_reasoning_effort: bool = False

    # 模型采用哪一种思考请求格式。
    thinking_format: ThinkingFormat = "none"

    # 工具执行完成后，下一轮是否要把
    # assistant.reasoning_content 原样传回模型。
    replay_reasoning_content: bool = False


DEFAULT_MODEL_NAME = "siliconflow-deepseek-flash"


MODEL_PROFILES: dict[str, ModelProfile] = {
    "ustc-deepseek-flash": ModelProfile(
        name="ustc-deepseek-flash",
        display_name="USTC DeepSeek V4 Flash",
        provider="openai_compatible",
        model_id="deepseek-v4-flash-ascend",
        api_key_env="USTC_LLM_API_KEY",
        base_url_env="USTC_LLM_BASE_URL",
        supports_thinking=True,
        supports_reasoning_effort=True,
        thinking_format="deepseek",
        replay_reasoning_content=True,
    ),

    "ustc-deepseek-pro": ModelProfile(
        name="ustc-deepseek-pro",
        display_name="USTCDeepSeek V4 Pro",
        provider="openai_compatible",
        model_id="deepseek-v4-pro",
        api_key_env="USTC_LLM_API_KEY",
        base_url_env="USTC_LLM_BASE_URL",
        supports_thinking=True,
        supports_reasoning_effort=True,
        thinking_format="deepseek",
        replay_reasoning_content=True,
    ),

    "siliconflow-deepseek-flash": ModelProfile(
            name="siliconflow-deepseek-flash",
            display_name="SILICONFLOW DeepSeek V4 Flash",
            provider="openai_compatible",
            model_id="deepseek-ai/DeepSeek-V4-Flash",
            api_key_env="SILICONFLOW_LLM_API_KEY",
            base_url_env="SILICONFLOW_LLM_BASE_URL",
            supports_thinking=True,
            supports_reasoning_effort=True,
            thinking_format="deepseek",
            replay_reasoning_content=True,
        ),
}


def get_model_profile(name: str) -> ModelProfile:
    profile = MODEL_PROFILES.get(name)

    if profile is None:
        available = ", ".join(sorted(MODEL_PROFILES))

        raise ValueError(
            f"未知模型配置：{name}。"
            f"可用模型：{available}"
        )

    return profile