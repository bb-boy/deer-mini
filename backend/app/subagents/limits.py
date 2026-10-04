"""单个父 Run 的委派限额；提示词和后台调度使用同一份数字。"""

# 与 DeerFlow 默认值一致；这是每个 Run 的额度，不是全服务的全局额度。
DEFAULT_MAX_CONCURRENT_SUBAGENTS = 3
DEFAULT_MAX_TOTAL_SUBAGENTS = 6
