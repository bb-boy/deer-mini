# 公共存储与记忆恢复合并验收

日期：2026-10-06。用户授权：合并后做真实测试。

## 代码与审查

- 远端主仓库：`tx:/home/pl/deer_mini`，分支 `main`。
- 模块提交：`8bc643c`；主分支原基线：`f391879`；合并提交：`d5fdab8afe9c52c4ff2b6c8165bdd01720fba237`。
- 集成工作树：`/home/pl/deer_mini/.worktree/storage-integration`，分支 `storage-integration`。原有工作树和本地未提交副本保留。
- 数据库初始化同时保留 checkpoint 表及记忆任务表。修复 StorageError 继承 RuntimeError 导致 HTTP 409 误分类，补齐 checkpoint 持久化异常包装，去除重复测试隔离 fixture。
- 未参与实现的 reviewer 对照已确认设计、完整模块差异及 checkpoint 集成完成独立审查和复审：Ready to merge: Yes；无未解决 Critical、Important、Minor。

## 自动回归

Python 3.12.13，使用原 storage-foundations 隔离虚拟环境，在集成工作树 backend 执行：

```sh
python -m pytest tests/app --ignore=tests/app/model/test_factory.py -q --tb=short
```

结果：853 passed、13 skipped，159.59 秒。随后补充其余接口分类修正和3个测试，对最终代码执行：

```sh
python -m pytest tests/app/api tests/app/services/test_file_checkpoint_service.py -q --tb=short
```

结果：70 passed，25.56 秒。两次均只有既有 LangSmith deprecation warning。计数不相加；首次失败版本为851通过、1失败、13跳过，上传故障误报409已修复。暂存区 whitespace 检查通过。

## 合并后的真实验收

使用真实 SiliconFlow 模型，配置名 `siliconflow-deepseek-flash`、模型 ID `deepseek-ai/DeepSeek-V4-Flash`。使用合成对话数据，不读取生产用户对话。通过真实 RunCoordinator、模型客户端、SQLite 和 Markdown 文件完成测试；没有启动 HTTP 服务。

独立数据目录：`tx:/home/pl/deer-mini-test-data/storage-acceptance-d5fdab8/`。

- 数据库：`acceptance.db`，独立文件，使用生产表结构。
- 用户文件：`users/`，与生产目录隔离。
- 脚本：`storage_real_acceptance.py`（显式调用；不加入常规自动测试）。
- 调用记录：`calls.json`；阶段结果：`real-result.log`；重放比对：`before-replay.json`。
- 凭证仅在进程内从既有配置加载；未复制到上述目录或提交 Git。

| 场景 | 实测结果 |
| --- | --- |
| 明确长期偏好→成功 Run→抽取→登记→保存 | 通过，生成 feedback 正文和索引，任务 success |
| 同用户新对话询问 SQLite 事务 | 通过，真实选择模型召回，主回答按“结论→原因”组织 |
| 更新为“原因→结论” | 通过，保持原记忆 ID，正文已反映新偏好 |
| 不同用户简短问候 | 通过，无记忆注入，无新增记忆 |
| 持久化的对话 checkpoint | 未包含 selected_memories 注入块；5 个恢复点正常建立 |
| 真模型抽取后模拟 ENOSPC | 主 Run 成功，保存项未报成功，SQLite 保留待恢复 payload |
| 退出后独立 Python 进程恢复 | 通过；禁止创建模型客户端，模型调用0；相同 ID 完成保存 |
| 成功清理与重复重启 | payload 清除，再次恢复后全部 Markdown 字节及时间戳不变 |
| SQLite integrity_check / foreign_key_check | ok / 无违规 |

模型调用总计12次：5次主对话、5次抽取、2次选择。最终5个 Run success、3个记忆任务 success、5个 checkpoint 恢复点。恢复测试两次独立进程，均无模型调用。

## 验证边界

这是选定合成场景的一次真实模型验收，不能等同于所有输入的模型质量评估。存储故障由受控异常注入，不是物理断电；真实 checkpoint 创建已覆盖，文件恢复 Docker 场景未在本次重复验证。模型回答中出现“已记住”式表述，不能据此判断持久化成功，本次断言使用 SQLite 和文件实际结果。

已合并远端 main；没有重启服务、部署前端、修改 Caddy/systemd 或生产数据库。本次未向 GitHub 推送。
