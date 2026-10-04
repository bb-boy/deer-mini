# 新版 read_file 与大工具结果落盘

## 已接入的工具

主、子 Agent 使用同一套公共中间件策略。默认工具表注册 Pydantic 版 read_file 和 read_tool_result；新增工具经统一执行入口自动适用大小规则。

read_file 参数：

```json
{"path":"/mnt/user-data/workspace/report.txt","encoding":"utf-8","base":10,"offset":5}
```

读取第 10～14 行。base 从 1 开始，offset 是读取行数，省略则读到末尾。encoding 支持 utf-8、gb18030、utf-16。未知参数、错误类型及无效范围会被 Pydantic 拒绝；工具另行检查路径和文件类型。

## 大小规则

字符数使用 Python len(str)，不是字节数或 token 数；50k=50,000。

- 单次工具返回内容严格超过 50,000 字符：完整内容落盘，消息正文换为最多 2,000 字符的内容预览，加文件路径、原始字符数和读取提示。
- 同一条 assistant 消息发出的工具调用，其返回正文合计严格超过 200,000 字符：按当前结果大小降序选择落盘，直到正文合计不超过 200,000 字符。文件说明也计入预算；必要时缩短预览。
- 不同 assistant 消息分开计算。50,000 字符单结果、200,000 字符合计本身不触发。
- 结果对象的消息 ID、tool_call_id、is_error 不变。已落盘结果不会把预览重新当成全文保存。
- 文件写入采用临时文件、fsync、原子替换，确认保存后才修改消息。写盘失败停止 Run，避免用缺失全文的预览继续运行。
- 取消时先等待任务退出，补齐已知结果或中断说明，再执行本轮收尾检查、保存检查点、发布事件；取消仍向外传播。收尾写盘失败作为 StatePersistenceError 传播。

read_file 返回完整选中行；web_fetch 返回完整提取正文和已脱敏的完整错误。Bash 捕获器保留有界预览，同时保存完整输出到临时文件；BashTool 消费并清理临时文件后交给公共中间件。Bash 非零退出及超时结果也适用。文本输出按 UTF-8 解码，不能解码的字节替换为替代字符；本策略保存工具文本，不是二进制归档。网页搜索数量及供应商提取范围仍由工具或供应商决定。

## 文件在哪里

真实文件位于该对话的 workspace/.tool-results/<随机编号>.txt。模型看到的统一路径为：

```text
/mnt/user-data/workspace/.tool-results/<随机编号>.txt
```

真实路径依据当前 Thread 的 workspace_path，不能用另一个对话的虚拟路径跨对话访问。read_tool_result 只读当前对话 .tool-results 内普通文件，拒绝越界和符号链接。删除对话时随对话目录清理；目前没有按文件定时清理，以保留历史结果读回能力。

全文文件按 UTF-8 保存，保留工具文本中的换行。结果文件创建权限 0600，结果目录新建权限 0700。主、子 Agent 在同一 Thread 共享工作目录。

## 如何读回

消息预览含调用提示。read_tool_result 按字符分页，可处理整份文件只有一行的情况：

```json
{"path":"/mnt/user-data/workspace/.tool-results/<返回的编号>.txt","offset":2000,"limit":2000}
```

这里 offset 从 0 开始，表示跳过的字符数；limit 表示读取字符数（1～50,000，默认 2,000）。读完这页后下一页 offset=4000。超过文件末尾返回空文本。

注意：read_file 的 offset 是“读取多少行”，read_tool_result 的 offset 是“跳过多少字符”，两者的工具 schema 分别注明含义。

## 调用顺序与边界

```text
模型返回工具调用
  → 公共 wrap_tool_call 链
  → ToolExecutor 查表执行具体工具
  → 单结果大于 50k：保存全文、换成预览
  → 结果加入状态、after_tool
  → 全轮结果到齐：检查 200k 合计
  → finalize_tool_results（取消补齐结果也经过）
  → 保存最终状态
  → 发布定稿后的 tool.end
  → 下一轮模型收到预览和读取提示
```

API 的 MessageResponse、Checkpoint 与 tool.end 都提供 tool_result_file、tool_result_chars 字段；旧消息缺省为 None。只有内容预览和说明传给模型；完整文本可通过读取工具取得。

捕获器可以将大输出转磁盘，但当前工具接口仍返回字符串；从 Bash 临时文件读取及公共中间件保存前会暂时持有完整字符串，并非端到端流式落盘。

## 验证

测试覆盖：中文字符阈值、批量最大优先、两条模型消息分别计数、重启恢复、取消、文件保存失败、路径隔离、长单行分页、错误标记、API/SSE 一致、主子 Agent 与 Coordinator 的真实执行链。模型使用模拟响应，数据库和文件使用临时目录；未调用真实模型或网页供应商。
