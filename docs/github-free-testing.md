# GitHub 免费运行与测试 deer-mini

本说明用于 GitHub Free 个人账户的 Codespaces 开发和标准 GitHub Actions 自动化测试。请注意 Codespaces 存在免费额度，超过额度时可能暂停使用；无需为测试配置 API Key 或付费服务器。

## 1. 启动 Codespaces

1. 进入 [bb-boy/deer-mini](https://github.com/bb-boy/deer-mini)。
2. 点击 **Code → Codespaces → Create codespace on main**（合并本配置后使用 main；合并前可以在 `github-free-testing` 分支创建）。
3. 选用 2 核机器。首次创建时，`.devcontainer/setup.sh` 会自动安装 Python 和前端依赖。
4. 两个终端分别运行下面的命令。

### 终端 A：后端 API

从仓库根目录开始：

```bash
cd backend
mkdir -p data/codespaces-users
export DEER_MINI_DATABASE_PATH="$PWD/data/codespaces.db"
export DEER_MINI_DATA_ROOT="$PWD/data/codespaces-users"
export DEER_MINI_MEMORY_ENABLED=false
export DEER_MINI_BASH_ENABLED=false
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8005 --reload
```

检查 Codespaces 的 **Ports** 面板中 8005 端口；也可以在 API 的 `/docs` 页面测试接口。

### 终端 B：前端网页

另开一个终端（从仓库根目录开始）：

```bash
cd frontend
pnpm dev
```

在 **Ports** 面板打开 5175 端口，Vite 会把 `/api` 请求代理到本地 8005 端口。

**说明：** 缺少 `USTC_LLM_API_KEY` 等模型服务凭证时，网页和 API 可以进行界面/基础功能调试，但调用真实 AI 对话会失败。不要把真实密钥提交到 GitHub。Bash Docker 沙箱默认关闭；在线免费测试不依赖它。

## 2. 手动运行测试

后端（从仓库根目录开始）：

```bash
cd backend
.venv/bin/python -m pytest tests/app --ignore=tests/app/model/test_factory.py -q
```

前端（另开根目录终端）：

```bash
cd frontend
pnpm typecheck
pnpm test
pnpm build
```

后端回归测试默认使用临时数据库、模拟模型；排除了确实调用模型服务的 `test_factory.py`。需要真实 Docker/模型的 live 测试不在免费 CI 范围。

## 3. GitHub Actions 自动测试

每次向 `main` 提交、发起针对 `main` 的 PR，或在 **Actions → Free tests → Run workflow** 手动触发，都会执行：

- 后端：Python 3.12 安装依赖、运行隔离的 pytest 回归测试。
- 前端：Node.js 22 + pnpm 10.30.3，类型检查、Vitest 测试及 Vite 构建。

工作流不提供 API Key，不部署网站，不接触远程生产数据库，不运行需要 Docker/真实模型的验收测试。绿勾仅表示这些指定检查通过，不代表真实 AI 功能端到端可用。

## 4. 保持免费

- GitHub Free 的公有仓库使用**标准** GitHub 托管运行器执行 Actions 通常不计费；不要改用付费大型运行器。
- Codespaces 有计算及存储免费额度，建议选 2 核环境，使用完在 [Codespaces 管理页面](https://github.com/codespaces) **Stop**，不再需要时删除。
- 建议保持默认的 Codespaces 端口可见性（私有），不要为了调试而公开测试数据库、内部 API 或密钥。
