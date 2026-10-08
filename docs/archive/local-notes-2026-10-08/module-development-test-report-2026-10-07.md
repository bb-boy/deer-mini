# module-development skill 测试报告

日期：2026-10-07（Asia/Shanghai）。对象：本仓库的 `.agents/skills/module-development/` 及 `.codex/agents/` 对应角色配置。

## 结论

修正后，21 项独立情景测试通过；隔离 Git 项目完成新工作树、开发、自测、故障注入、独立审查、原开发者修复、原 reviewer 复审和主 agent 最终验证。最终 5 个行为测试全部通过，原仓库暂存、未暂存和未跟踪哨兵保持。实际运行记录确认原生与通用回退开发者使用 `gpt-6-luna`，reviewer 使用 `gpt-6.1-sol`。

此结论限于下述测试范围，不把情景回答等同于所有分支的实际执行。

## 发现及修正

1. **模型回退缺口**：developer TOML 已设 Luna，但旧技能只要求通用 agent 接收 `developer_instructions`，且写“默认继承模型”。仅复制提示词不能应用 TOML 顶层模型。现明确读取并传递角色模型；通用开发者显式传 `model`，使用 `collaboration.spawn_agent` 时设 `fork_turns="none"`；未配置模型的 reviewer 省略覆盖并继承当前模型。工具不支持选择时报告限制，不虚称生效。
2. **非 Git 位置缺口**：旧文案只规定文件快照，后续仍一律要求工作树、分支和 SHA。现明确固定 `MODULE_DIR`、文件快照比较，分支与 `BASE_SHA` 不适用，并同步两份开发者和两份 reviewer 指令。
3. **指定 GrillMe 的措辞歧义**：旧文案区分“明确要求”和“明确要求必须”，可能不尊重普通指定。现明确用户指定使用即可；不可用时说明并暂停依赖部分，未指定时才允许等价梳理。

新模块必须新建专用工作树和模块分支、同模块续接复用原位置的规则保持。全局模型、provider、凭据配置未修改。

## 独立情景测试

两个独立 agent 在 `fork_turns="none"` 的上下文中读取实际 skill 与角色文件，覆盖全部六节；发现问题后原测试者复测。情景仅作只读判断，不实际进行部署、采访或线上操作。

| 编号 | 覆盖的决定 | 最终结果 |
| --- | --- | --- |
| A1 | 已确认需求直接沿用，不重复访谈；书面依据和基线 | PASS |
| A2 | 目标含糊、工具不可用，等价澄清且继续独立调查 | PASS |
| A3 | 用户指定 GrillMe 但不可用，尊重指定和暂停边界 | PASS |
| A4 | 定位源码而非文档仓库、需求文档位置、保留脏基线 | PASS |
| A5 | 新模块必须新建专用工作树及分支 | PASS |
| A6 | 换开发者、跨会话、旧树未提交成果和迁移边界 | PASS |
| A7 | 共享接口未定时的依赖顺序、并行和文件所有权 | PASS |
| A8 | 已确认局部提示文案的最小流程 | PASS |
| A9 | 非 Git 目录、快照、分支与 SHA 替代语义 | PASS |
| B1 | 同一工作树稳定版本审查、暂停相关写入 | PASS |
| B2 | 开发者 DONE 不替代组合验证和真实行为覆盖 | PASS |
| B3 | 全部跳过且退出 0 不能称行为通过 | PASS |
| B4 | 已修复后补测试仍需原始缺陷失败证据 | PASS |
| B5 | 连续修复无效后重查根因，不继续猜测补丁 | PASS |
| B6 | 核实 review 争议、Important 修复复审、Minor 范围 | PASS |
| B7 | 原生和通用角色参数、Luna 与 reviewer 模型继承 | PASS |
| B8 | 独立 reviewer 不可用时如实报告审查缺失 | PASS |
| B9 | 全部任务提交、暂存/未暂存、未跟踪及目录外文档 | PASS |
| B10 | 续接旧 DONE 后新增改动的补测、补审与证据版本 | PASS |
| B11 | 本地通过不自动授予提交、推送、部署和清理权限 | PASS |
| B12 | 按项目真实存在的命令验证，不虚构 lint 或 Ruff | PASS |

A9 和 B7 初测存在明确缺口，修正后复测通过；A3 的措辞边界单独复测通过。模拟“经理催促”不作为新的真人授权。

## 隔离项目实际演练

- 测试根目录：`/var/folders/tp/kh3s7gk95rs62w0jqt4dchnc0000gn/T/module-skill-full-test-6r49a3wo`。
- 主仓库：`/var/folders/tp/kh3s7gk95rs62w0jqt4dchnc0000gn/T/module-skill-full-test-6r49a3wo/source`；主分支 `main`。
- 新模块工作树：`/var/folders/tp/kh3s7gk95rs62w0jqt4dchnc0000gn/T/module-skill-full-test-6r49a3wo/source/.worktree/text-summary`。
- 模块分支：`text-summary`。
- BASE_SHA：`e94c9c5f5191f5f9d89732b9305f4be65046b778`。
- 权威需求：[requirements.md](/var/folders/tp/kh3s7gk95rs62w0jqt4dchnc0000gn/T/module-skill-full-test-6r49a3wo/source/docs/text-summary/requirements.md)，5 项验收；先保存需求再派发实现。
- 开发者拥有 `src/text_summary.py`、`tests/test_text_summary.py`；reviewer 只读审查同一工作树；修复期间与审查期间顺序执行。
- 开发者和 reviewer 均未提交、推送或部署；主仓库仅新增夹具需求文档，保留三份模拟用户哨兵。

模块实现 `summarize_text(text: str) -> dict[str, int]`，覆盖非空白行、空白分词、空输入、精确返回字段及非字符串错误，回归原有 greetings。无外部服务、数据库或真实用户文件参与业务测试。

| 阶段 | 实际结果 |
| --- | --- |
| 修改前基线 | 1 个原有用例通过 |
| 初次开发自测和主 agent 检查 | 5 个用例通过 |
| 主 agent 故障注入 | 把 `split()` 改为 `split(" ")`；同一测试集出现 3 个目标行为失败 |
| 独立 reviewer 初审 | Spec Compliance FAIL、Code Quality FAIL、REQUEST_CHANGES；直接定位字面空格分词错误，指出旧报告不对应当前代码 |
| 原开发者修复 | 仅修实现，原测试文件哈希未改变；5 个用例通过 |
| 原 reviewer 复审 | Spec Compliance PASS、Code Quality PASS、APPROVE；自己重跑同一测试集及核对哨兵 |
| 主 agent 最终验证 | 5 个用例全部通过，退出 0；工作树、分支、原始 HEAD 及三类哨兵核对通过 |

故障为测试主 agent 有意注入，用于验证 reviewer 能发现错误并阻止交付，不是开发者初次实现缺陷。

实际测试命令（在模块工作树执行）：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

最终输出：

```text
Ran 5 tests in 0.000s
OK
```

最终实现 SHA-256：`4b37ed01a6783c934b562ba7dcc6d3fee254f2339affaee761939998202d84db`。
测试文件 SHA-256（故障前后不变）：`07aca4a328ddeb7d82dfbde58f75dade1eff90e314c0fc10a6a5a326678dcc88`。
需求 SHA-256：`66508585f326677b381c71a259d5cee9ecd411e981e43b08e71f1b1f60122a32`。

证据：[开发及修复报告](/var/folders/tp/kh3s7gk95rs62w0jqt4dchnc0000gn/T/module-skill-full-test-6r49a3wo/developer-report.md)、[故障失败输出](/var/folders/tp/kh3s7gk95rs62w0jqt4dchnc0000gn/T/module-skill-full-test-6r49a3wo/fault-tests.txt)、[最终测试输出](/var/folders/tp/kh3s7gk95rs62w0jqt4dchnc0000gn/T/module-skill-full-test-6r49a3wo/final-tests.txt)、[原始夹具与差异](/var/folders/tp/kh3s7gk95rs62w0jqt4dchnc0000gn/T/module-skill-full-test-6r49a3wo/fixture.json)、[哨兵核对](/var/folders/tp/kh3s7gk95rs62w0jqt4dchnc0000gn/T/module-skill-full-test-6r49a3wo/sentinel-verification.json)。夹具保留在临时目录，未加入当前仓库。

## 实际模型验证

只核对本次测试线程的运行记录 `turn_context.model`，不以 agent 自述或角色名称作证明，不记录凭据或完整环境。

| 角色及派发方式 | 线程 ID | 运行记录模型 |
| --- | --- | --- |
| 原生 module-developer，实际实现及修复 | 01a11526-1ac1-7e30-97f1-473755373779 | gpt-6-luna |
| 原生 module-reviewer，实际初审及复审，省略模型覆盖 | 01a11528-6687-7be3-a992-f6a7cdb40905 | gpt-6.1-sol |
| 通用 default 开发者回退探针，显式 model + fork_turns=none | 01a1152a-9bb3-7121-bf24-0556b27f4461 | gpt-6-luna |

通用探针只测试派发模型与指令读取，未重复完整模块实现。已比较全局配置及旧独立 runner 的原始哈希，确认未修改；旧独立 runner 不属于本 skill 当前派发路径，未实际启动。

## 静态验证与范围

- skill-creator 的 `quick_validate.py`：`Skill is valid!`。
- 两份 developer TOML 及两份 reviewer TOML 解析成功，角色副本逐字一致。
- developer 固定 Luna；reviewer 未设置模型覆盖，保持 `read-only`。
- `agents/openai.yaml` 解析及必需 UI 字段检查通过。
- GrillMe 路由与当前安装入口核对一致。
- `git diff --check` 通过；对未跟踪交付文件额外检查实际内容，未把空 diff 当作完整审查。

未实际执行真实 GrillMe 采访、多个开发者同时写入的竞态、非 Git 完整模块演练或迁移/部署；这些分支通过只读情景验证。主项目 backend/frontend 没有业务代码改动，因此未运行其业务回归或启动服务。测试期间 provider 网络调用仅为用户授权的 Codex 子 agent 执行，不调用夹具中的真实业务模型服务。
