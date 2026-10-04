"""选用 DeerFlow 原文中已有功能对应的文件规则。

来源：backend/packages/harness/deerflow/agents/lead_agent/prompt.py 中的 <working_directory>。
只保留现有工作目录、输出目录和 read_file 能遵循的原文条目。
尚未实现的上传标签、历史文件查询、文档转换、文件展示和 ACP 条目不加入。
"""

# 仅删去不适用的整条规则，保留条目的英文措辞不改写。
WORKING_DIRECTORY_PROMPT = """
<working_directory existed="true">
- User workspace: `/mnt/user-data/workspace` - Working directory for temporary files
- Output files: `/mnt/user-data/outputs` - Final deliverables must be saved here

**File Management:**
- Use `read_file` tool to read uploaded files using their paths from the list
- All temporary work happens in `/mnt/user-data/workspace`
- Treat `/mnt/user-data/workspace` as your default current working directory for coding and file-editing tasks
- When writing scripts or commands that create/read files from the workspace, prefer relative paths such as `hello.txt`, `../uploads/data.csv`, and `../outputs/report.md`
- Avoid hardcoding `/mnt/user-data/...` inside generated scripts when a relative path from the workspace is enough
</working_directory>
"""
