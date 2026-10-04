"""仅在 Tavily 网络工具已注册时加入的引用规则。

逐段摘自 DeerFlow backend/packages/harness/deerflow/agents/lead_agent/prompt.py
的 <citations>。保留原文并省略示例；web_fetch 已接入，恢复对应的引用要求。
原模板中的双大括号在格式化后是单大括号，此处直接保存最终文字。
"""

CITATIONS_PROMPT = """
<citations>
**CRITICAL: Always include citations when using web search results**

- **When to Use**: MANDATORY after web_search, web_fetch, or any external information source
- **Format**: Use Markdown link format `[citation:TITLE](URL)` immediately after the claim
- **Placement**: Inline citations should appear right after the sentence or claim they support
- **Sources Section**: Also collect all citations in a "Sources" section at the end of reports

**CRITICAL: Sources section format:**
- Every item in the Sources section MUST be a clickable markdown link with URL
- Use standard markdown link `[Title](URL) - Description` format (NOT `[citation:...]` format)
- The `[citation:Title](URL)` format is ONLY for inline citations within the report body

**WORKFLOW for Research Tasks:**
1. Use web_search to find sources → Extract {title, url, snippet} from results
2. Write content with inline citations: `claim [citation:Title](url)`
3. Collect all citations in a "Sources" section at the end
4. NEVER write claims without citations when sources are available

**CRITICAL RULES:**
- ❌ DO NOT write research content without citations
- ❌ DO NOT forget to extract URLs from search results
- ✅ ALWAYS add `[citation:Title](URL)` after claims from external sources
- ✅ ALWAYS include a "Sources" section listing all references
</citations>
"""
