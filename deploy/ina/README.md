# inA 的 /ai 部署

目标：`https://ec-standard-pc-i440fx-piix-1996.tail18699d.ts.net:10000/ai/`。
实际项目目录为 `/home/pl/deer_mini`，使用已有的 Python 3.12 虚拟环境和 pnpm 10.30.3。

请求链路：Tailscale Serve `:10000` → Nginx `:5174` → `/ai/` 静态页面、`/ai/api/` 单进程 Uvicorn `127.0.0.1:8005`。
保留原来的 `/`、`/api/`、`/healthz` 和其他 Tailscale 转发。

## 发布

1. 检查项目 Git 状态、监听端口和服务；只快进同步已经审查通过的 GitHub main。存在本地修改或分叉时停止并保留现场。
2. 首次启动前，将现有数据库（SQLite backup API）、用户目录及本次修改的 Nginx/systemd 配置备份到仓库外、权限 0700 的部署备份目录。`.env` 留在原位置，禁止提交或输出内容。
3. 在 backend 用 `.venv/bin/python -m pip install -r requirements.txt` 安装依赖。在 frontend 用 `pnpm install --frozen-lockfile`，再执行 `pnpm build --base=/ai/`。
4. 将 dist 内容复制到 `/var/www/deer-mini/releases/<commit>/ai/`，保证 Nginx 可读；将 `/var/www/deer-mini/current` 原子切换到该 release。旧 release 留存。
5. 将本目录的 `deer-mini-backend.service` 安装到 `/etc/systemd/system/`，执行 `systemd-analyze verify`、`systemctl daemon-reload`、`systemctl enable --now deer-mini-backend.service`。后续版本更新需要 restart。服务沿用应用的 dotenv 读取和已有数据路径，不配置多 worker。
6. 将 `nginx-ai.conf` 安装为 `/etc/nginx/snippets/deer-mini-ai.conf`，仅在 `/etc/nginx/conf.d/ecrh-data-system-frontend.conf` 的现有 server 内加入一条 `include /etc/nginx/snippets/deer-mini-ai.conf;`。先 `nginx -t`，通过后 reload Nginx。
7. 检查服务 active、`/ai` 相对重定向、`/ai/` 页面、JS/CSS 资源和 `/ai/api/models`。用不存在的随机 thread 验证 SSE/文件地址返回本应用的 404 JSON；不读取用户记录，不调用真实模型。检查已有 `/`、`/healthz` 保持原响应。

API 前缀在 Nginx 中去掉 `/ai`，Uvicorn `--root-path /ai` 保留外部路径信息。前端通过 Vite `BASE_URL` 同时生成 API、SSE 和文件 URL；根路径构建仍使用 `/api`。

## 回退

保留发布前 commit、静态 release、配置备份和数据快照。若启动或验收失败，先停止本次后端服务并恢复本次代理配置/静态链接；如需回退应用，使用旧提交的独立发布副本，保留当前 checkout 的改动。恢复数据库和用户文件是独立操作，不自动覆盖线上数据。
