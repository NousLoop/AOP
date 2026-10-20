# A2A Agent Orchestration Platform (AOP)

基于 [A2A 协议](https://github.com/a2aproject/A2A) 的多 Agent **注册 · 发现 · 调度 · 编排 · 执行** 平台。

**当前形态：** 平台能力 Phase 1–37 + A2A OS Phase 3–6；Agent 统一为 Harness 虚拟 Agent；本地默认由 `aop-node` 拉起。

## 文档

完整索引见 **[docs/README.md](./docs/README.md)**。

| 文档 | 说明 |
|------|------|
| [启动指南](./docs/guides/getting-started.md) | 本地启动 · 端口 · 排障 |
| [架构总览](./docs/architecture/overview.md) | 落地栈与分层 |
| [A2A 协议](./docs/architecture/a2a-protocol.md) | 官方协议兼容约定 |
| [Agent 开发](./docs/architecture/agent-dev-guide.md) | Card / 接入规范 |
| [Harness](./docs/architecture/harness-migration.md) | 虚拟 Agent · CLI 环境 |
| [API](./docs/reference/api.md) · [Database](./docs/reference/database.md) · [Redis](./docs/reference/redis.md) | 契约 |
| [部署](./deployments/README.md) | 单机云部署 · GHCR 镜像 |
| [CI/CD](./deployments/README.md#cicd-github-actions--ghcr) | GitHub Actions 测试与镜像推送 |
| [aop-node](./apps/client/aop-node/README.md) | 边缘 Supervisor（本地默认拉起 Harness） |

## 仓库结构

```text
apps/
  gateway/          Go · 注册中心 / 鉴权 / 反代 (:8080)
  orchestrator/     Python · 编排 / Worker / Outbox (:8090)
  web/              Next.js Console (:3000)
  client/aop-node/  Rust · 边缘 Supervisor (:7920)
agents/
  harness-agent/    唯一 Agent 部署（profiles: claude-code / deepseek / pi / openclaw / hermes）
packages/
  a2a-sdk/          A2A JSON-RPC 客户端
  agent-runtime/    Harness 适配 + 协作
  llm-provider/
infrastructure/     postgres · redis · minio
deployments/        docker-compose · deploy.sh · 可观测
docs/               文档中心
scripts/            dev_up.py · golden_demo.py · start_and_register_agents.py
```

## 快速开始（本地）

一键拉起（基础设施 + Orchestrator + Worker + Outbox + Gateway + Web + **aop-node**）：

```bash
python scripts/dev_up.py
```

打开 http://127.0.0.1:3000 — 账号 `admin@aop.local` / `aop_admin_dev`。  
Harness 由 **aopd**（`:7920`）统一拉起并注册，不再默认开 5 个 uvicorn。`--skip-agents` 跳过；`--legacy-agents` 或 `AOP_LEGACY_AGENTS=1` 回退到旧的 5 进程。  
黄金路径自检：`python scripts/golden_demo.py --check`；建一条 Command Center 同款任务：`python scripts/golden_demo.py`。

下面是等价的分步启动（排障时用）。

### 1. 基础设施

```bash
docker compose -f deployments/docker-compose.yml up -d postgres redis minio
python infrastructure/postgres/migrate.py
```

### 2. Orchestrator + Worker + Outbox

```bash
cd apps/orchestrator
pip install -r requirements.txt
pip install -e ../../packages/a2a-sdk
uvicorn main:app --host 0.0.0.0 --port 8090
# 另开终端
python worker.py
python outbox_processor_service.py
```

> 缺少 Outbox 时，首批节点会卡在 `outbox_events(pending)`。详见 [启动指南](./docs/guides/getting-started.md)。

### 3. Gateway

```bash
cd apps/gateway
# PowerShell: $env:ORCHESTRATOR_URL="http://127.0.0.1:8090"
# 本地默认 AUTH_REQUIRED=false
go run ./cmd/
```

### 4. Harness Agents 并注册

`dev_up.py` 默认走 aop-node。单独启动：

```bash
cd apps/client/aop-node
copy config\aop-node.example.toml aop-node.toml
cargo run -p aopd -- --config aop-node.toml
```

```bash
pip install -e "packages/agent-runtime[harness]"
pip install -e packages/a2a-sdk
python scripts/start_and_register_agents.py
```

| Profile | Port |
|---------|------|
| claude-code | 8011 |
| deepseek-harness | 8012 |
| pi | 8013 |
| openclaw | 8014 |
| hermes | 8015 |

默认规划目标：`claude-code`（`DEFAULT_AGENT`）。详见 [harness-migration.md](./docs/architecture/harness-migration.md)。

### 5. Web Console

```bash
cd apps/web
npm install
npm run dev
```

打开 http://127.0.0.1:3000 — 账号 `admin@aop.local` / `aop_admin_dev`。

### 6. Edge Node

本机默认 Supervisor 即 aop-node（`dev_up.py` 会拉起）。装 Windows 服务：

```bash
cd apps/client/aop-node
copy config\aop-node.example.toml aop-node.toml
# 管理员: scripts\install-service.cmd  →  sc start aop-node
```

见 [aop-node README](./apps/client/aop-node/README.md)。管理口：`http://127.0.0.1:7920`。

## 云服务器单机部署

```bash
cd deployments
cp .env.example .env   # 编辑 AOP_PUBLIC_HOST
./deploy.sh up
```

详见 [deployments/README.md](./deployments/README.md)。部署默认 `AUTH_REQUIRED=true`。

## 进度速览

- **平台 Phase 1–34**：Registry · Planner/DAG · Worker · Console · Auth · Billing · Quotas · Sandbox · Stripe · Egress  
- **平台 Phase 35–37**：见 [docs/phases](./docs/phases/) 摘要  
- **A2A OS Phase 3–6**：Execution · Production；Scheduling / Marketplace **API 仍在、Console 停放**  
- **协议**：Agent 线对齐 [a2aproject/A2A](https://github.com/a2aproject/A2A)；OS 控制面仍为 `/v1/*`

验收表：[mvp-plan.md](./docs/architecture/mvp-plan.md)。冒烟：`apps/orchestrator/scripts/phase*.py`。

### 常用运维

| 现象 | 处理 |
|------|------|
| `429` + `quota_concurrent` | Settings「配额」调高，或取消卡住的 `running` 任务；Console 会显示中文说明 |
| Gateway `proxy error …:8090` | 确认 Orchestrator 在听；必要时重启 Gateway |
| 任务 `ready` 不推进 | 确认 Outbox Processor 在跑；Console 预检横幅会标红 |
| 鉴权 | Bearer 会话或 `X-API-Key`；本地可 `AUTH_REQUIRED=false` |
