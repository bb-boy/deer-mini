# 上下文压缩与原生工具集成、inA 部署

确认依据：2026-10-08，用户要求同步到 inA，同时合并并启动到 https://ec-standard-pc-i440fx-piix-1996.tail18699d.ts.net:10000/ai。本次提交、合并、推送和该站点部署已获授权；密钥继续仅保留在机器本地。

## 基线与目录

- 唯一集成工作树：/Users/jk/Documents/ChatGPT/de/.worktree/deer-mini-source/.worktree/context-compression，分支 context-compression。续接原模块，不另开模块开发工作树。
- 当前分支起点：d92420730a46be47e563afc3d22b6eed29ff88eb；原生工具提交：24badd8950d4952a0b4bfbfde46ca04d8060e71e；GitHub main：e25a655f330e5783e63f1025182529294ce8e40b。
- inA 实际项目：/home/pl/deer_mini。用户写的 /home/pl/deer-mini 不存在；实际 main 干净，HEAD 为 e25a655。另一个 backup 目录有历史改动，不修改它。
- inA 已有 Tailscale Serve 与 Nginx。先核对端口 10000 的路由，不覆盖其他站点。

## 分工与步骤

1. 主 agent 发起合并；原 module-developer 负责 backend/** 的冲突、组合行为和开发报告；主 agent 负责 frontend/**、部署配置及本文。
2. 后端完成组合回归，前端验证 /ai 路径、API/SSE/文件 URL，测试使用隔离数据。
3. 原独立 reviewer 审查稳定合并结果及新增部署适配；必要时由原开发者修复并复审。
4. 推送已验证的合并版本，inA fetch 后快进同步。保留本地 .env、数据库、用户目录和现有备份。
5. 先备份本次需要修改的服务/代理配置，再配置单进程后端、静态前端和 /ai 路由，校验后加载。
6. 检查系统服务、页面及静态资源、健康/模型列表接口、API 路由和指定外部地址。不自动调用真实模型，不输出凭据。

## 验收

- 原生文件工具和 Snip 同时可用，压缩预算、原始记录保留及主子 Agent 协议维持正确。
- 后端常规回归（排除 test_factory.py 与 learning）、前端 typecheck/test/build 和独立合并审查通过。
- /ai 页面及 API/SSE/文件请求路由到本项目，其他现有代理路径保持。
- inA 运行提交与 GitHub 已验证版本一致；密钥、.env、用户数据和日志不进入 Git。

## 集成验证（2026-10-08，待独立审查）

- 后端三处工具列表冲突已解决，新增真实原生工具 → idle → 引用恢复 → Snip 组合用例及持久化失败组合验证。完整回归 1012 passed、16 skipped；详见 developer-report.md 的合并集成交回记录。
- 前端合并后基线 126 passed；新增 `/ai` 测试先复现 2 failed（普通 API 与 SSE 使用根路径），修复后 128 passed。日志：`/tmp/deer-ina-subpath-before.log`、`/tmp/deer-ina-frontend-final.log`。
- `pnpm typecheck` 和 `pnpm build --base=/ai/` 通过，使用 pnpm 10.30.3。构建仅有大 bundle 提示，日志 `/tmp/deer-ina-frontend-build.log`。
- `deploy/ina/` 保存专用 systemd unit、Nginx server 内 include 与发布/回退说明。在 inA 使用独立临时 Nginx 配置运行 `nginx -t`，并运行 `systemd-analyze verify`，均退出 0；无服务或站点变更。后者另报告系统已有 snapd unit 的未知字段，与本 unit 无关。
- 已确认后端无独立 health 路由，以只读 `/api/models` 验证 API；inA 原 `/` 和 `/healthz` 基线均为 200。现有 `.env`、用户目录和数据库存在，服务首次启动前备份。

独立审查：原 reviewer 对合并稳定树给出 Spec Compliance PASS、Code Quality PASS、APPROVE，无未解决问题。核对 40 个任务文件及完整后端树摘要；自行执行组合定向回归 210 passed、隔离 ASGI `/ai` API 与未知 Thread SSE/文件路由检查、18 个构建资源引用存在性检查。复用并核对上述完整回归与配置预检。结论仅覆盖实现及部署方案，实际站点验收另行记录。

inA 发布前快照已保存至仓库外 `/home/pl/deer-mini-deploy-backups/20261008-context-integration-p9ig5mr0`（0700），含 SQLite backup API 快照（quick_check ok）、现有用户文件、`.env` 与原代理配置。凭据未输出或进入 Git。

状态：实现与独立审查完成，进入已授权的 GitHub main 发布、inA 快进同步及部署验收。
