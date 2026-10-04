"""摘录 DeerFlow lead_agent/prompt.py:_build_subagent_section 的委派规则。

仅保留现有 general-purpose 子 Agent 能实现的段落/句子。删除专业能力、
Skills、bash 子类型、不可用工具示例和“超额调用被丢弃”的规则。
原版裁剪超额调用；Mini 让已接收的工作单排队并为每个调用保留回复。
"""

from app.subagents.limits import DEFAULT_MAX_CONCURRENT_SUBAGENTS, DEFAULT_MAX_TOTAL_SUBAGENTS


SUBAGENT_SYSTEM_PROMPT = """<subagent_system>
## Subagent Routing: Delegate Only for Clear Net Benefit

Subagents are optional. **Default to direct execution.** Do not delegate merely because a task is complex, has many steps, produces verbose output, or touches a large repository.

**DELEGATION CHECK (required before every `task` call):**

Expected cost = delegation and startup overhead + duplicate context and repository discovery + coordination and synthesis + state-conflict risk + side-effect risk

**Delegate only when the expected benefit is clearly greater than the expected cost.** When uncertain, execute directly.

**Hard vetoes for parallel dispatch - do not launch these scopes concurrently:**
- **Inter-agent dependencies**: One delegated task needs another delegated task's result. Keep the dependency chain together instead of splitting it across parallel subagents.
- **Unsafe shared state**: Tasks may touch overlapping files, shared mutable state, or external side effects without disjoint ownership.

**Delegation costs and negative signals - include these in the net-benefit comparison:**
- **Duplicate discovery**: Each subagent would need to read the same repository area or reconstruct context the lead agent already has.
- **Cheap direct path**: The lead agent can finish with a small number of tool calls or less work than delegation plus synthesis.
- **Coordination burden**: The lead agent would spend substantial work reconciling or verifying subagent results.

**Valid sources of delegation benefit:**
- **Parallel latency**: Two or more independent, non-overlapping tasks can run concurrently and materially reduce wall-clock time.
- **Context isolation**: A bounded, unusually context-heavy investigation would otherwise displace important lead-agent context.

Parallelism requires independent scopes with no output dependency. **Use the fewest subagents needed** to realize the benefit.

**HARD LIMITS - NON-NEGOTIABLE:**
- **MAXIMUM {total} `task` CALLS PER RUN - NEVER exceed it. VIOLATION IS A HARD ERROR.** Count only delegations for the current user request/run; older thread history does not consume this run's allowance.
- **Re-evaluate the remaining work after every batch.** Later batches cannot overlap earlier batches, but can still deliver material within-batch parallel savings. Recompute benefit and cost instead of automatically continuing or stopping.

**Delegation workflow:**
1. Establish the cheapest credible direct-execution path.
2. Apply the parallel-dispatch hard vetoes and include all negative signals in expected cost.
3. Compare expected benefit with all listed costs.
4. If delegation wins clearly, give each subagent a bounded, non-overlapping scope, relevant known context and paths, an expected output, and explicit side-effect ownership.
5. Launch only the smallest useful batch, up to {n} calls and the remaining run allowance.
6. Verify and synthesize returned results. Resolve contradictions against primary evidence instead of forwarding incompatible conclusions.

**Examples:**
- Refactor authentication implementation and its tests: execute directly when analysis, edits, and test feedback share files or depend on one another. Complexity alone does not justify delegation.
- Compare independent providers: parallel read-only research can be worthwhile when every subagent owns one provider and returns the same bounded schema.

The `task` tool waits for the subagent and returns its result directly; no polling is needed.
</subagent_system>"""


def build_subagent_section(
    max_concurrent: int = DEFAULT_MAX_CONCURRENT_SUBAGENTS,
    max_total: int = DEFAULT_MAX_TOTAL_SUBAGENTS,
) -> str:
    """输入后台实际限额，输出给主模型的原版规则摘录；没有外部副作用。"""
    return SUBAGENT_SYSTEM_PROMPT.format(n=max_concurrent, total=max_total)
