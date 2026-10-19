# Harness Migration

## Split of concerns

| Layer | Owns | Code |
|-------|------|------|
| **A2A OS** | How agents collaborate (routing, scoring, lineage, cost, idempotency) | `apps/orchestrator`, `agent_runtime.collaboration` / `a2a_server` |
| **Harness** | How an agent finishes a task | `agent_runtime.harness` + runners |
| **Virtual agent** | One profile per harness product (agent-card + system prompt) | `agents/harness-agent/profiles/*` |

**Specialty agents and coder/researcher role splits are gone.** One agent per harness
product: `claude-code`, `deepseek-harness`, `pi`, `openclaw`, `hermes`.

Virtual agent cards have **empty `skills`**. The planner writes `agent_key` into the plan node
`skill` field; the router resolves by agent_key.

## Architecture

```
A2A OS  --message/send-->  HarnessA2AAdapter  --run-->  Runner
                              | heartbeat
                              v
                     /v1/agent-runtime/{id}/heartbeat

Runners:
  ClaudeCliRunner       https://github.com/anthropics/claude-code
  DeepSeekHarnessRunner https://github.com/deepseek-ai/deepseek-harness
  PiCliRunner           https://github.com/earendil-works/pi
  OpenClawRunner / HermesRunner  (CLI exec; see profiles/)
```

## Virtual cards

Ports / marketplace catalog keys live in
`packages/agent-runtime/agent_runtime/harness/profiles.py` (single source of truth).
Keep `plugins/*/plugin.toml` ports in sync with that table.

| Profile | Port | Notes |
|---------|------|--------|
| `claude-code` | 8011 | Claude Code Agent |
| `deepseek-harness` | 8012 | DeepSeek Harness Agent |
| `pi` | 8013 | Pi Agent |
| `openclaw` | 8014 | OpenClaw (`agent exec --json`) |
| `hermes` | 8015 | Hermes Agent (`hermes -z`) |

Default planner: single node → `claude-code` (`DEFAULT_AGENT`). Override to route to another product.

When the goal asks for **all / every / 所有 / 全部 agents**, the planner fans out to
**one parallel node per ready online harness agent** (`method=heuristic_multi`).
Agents whose `/health` reports `runner_ready=false` (missing CLI / API key, or
only an LLM chat stub without product CLI/SDK) are **skipped** so the task can
still complete on agents that work (usually Claude).

Fan-out **dispatches** a per-node `instruction` (routing preamble stripped), e.g.
「使用目前所有的agent，调研ui2v…」→ each agent receives a role brief +
「调研ui2v…」, not the raw orchestration sentence. Worker sends `instruction`
to the CLI; Claude’s system prompt tells it **not** to re-spawn peers when the
platform already parallelized.

| Agent | Needs to be ready |
|-------|-------------------|
| `claude-code` | `claude` CLI on PATH (or `CLAUDE_CLI_PATH`) |
| `deepseek-harness` | `DEEPSEEK_API_KEY`（或 `OPENAI_API_KEY`）必填；可选 `DSH_HOME`（默认 `~/.aop/dsh-home`） |
| `pi` | `pi` CLI, **or** `PI_API_KEY` / `OPENAI_API_KEY` (tool_loop) |
| `openclaw` | `openclaw` CLI on PATH (or `OPENCLAW_CLI_PATH`) |
| `hermes` | `hermes` CLI on PATH (or `HERMES_CLI_PATH`) |

All harness runners use **`HARNESS_WORKDIR`** as the **parent root** for CLI/SDK
cwd (Read/Write/Bash). Each run nests under that root as:

```text
<HARNESS_WORKDIR>/<rootTaskId>/<agentId>/
```

- `rootTaskId`：平台任务 ID（入站 `rootTaskId` / `correlationId`）；没有则用本次 A2A `task_id`
- `agentId`：`HARNESS_PROFILE` / `AGENT_ID`（如 `claude-code`、`pi`）

Default parent root when unset: `~/.aop/workspaces`（不再共用单一的 `default` 目录）。
Legacy: `CLAUDE_WORKDIR` / `DSH_WORKSPACE` / `PI_WORKDIR` / `OPENCLAW_WORKDIR` / `HERMES_WORKDIR` only apply if `HARNESS_WORKDIR` is empty（同样作为父根再嵌套）。

## Environment

| Variable | Meaning |
|----------|---------|
| `HARNESS_PROFILE` | Profile dir name (default `claude-code`) |
| `HARNESS_RUNNER` | Override: `claude_cli` / `pi_cli` / `deepseek` / `openclaw` / `hermes` |
| `HARNESS_ENABLED` | Default on in start script; `0` to skip |
| `HARNESS_PROFILES` | Comma filter, e.g. `claude-code,pi,openclaw,hermes` |
| `HARNESS_WORKDIR` | Parent workdir root; per-run cwd is `<root>/<task>/<agent>/` (default `~/.aop/workspaces`) |
| `HARNESS_LIVE_PREVIEW` | `1`/`true`/`yes`/`on` → 弹本机新控制台实时预览（需桌面会话）；默认关闭 |
| `CODEWHALE_CLI_PATH` | deepseek 预览用的 CodeWhale 二进制（默认 PATH 上的 `codewhale`） |
| `DEFAULT_AGENT` | Planner target agent_key (default `claude-code`) |
| `CLAUDE_CLI_PATH` / `PI_CLI_PATH` / `DSH_CLI_PATH` / `OPENCLAW_CLI_PATH` / `HERMES_CLI_PATH` | Binaries |
| `CLAUDE_WORKDIR` / `DSH_WORKSPACE` / `PI_WORKDIR` / `OPENCLAW_WORKDIR` / `HERMES_WORKDIR` | Legacy per-runner parent root (ignored when `HARNESS_WORKDIR` is set) |
| `CLAUDE_CLI_PERMISSION_MODE` | Claude `-p` permission mode (default `bypassPermissions`) |

## Live preview（本机终端）

设 `HARNESS_LIVE_PREVIEW=1` 并**重启 harness agents** 后，每次任务会在
`~/.aop/workspaces/<task>/<agent>/` 下用 Windows `CREATE_NEW_CONSOLE` 弹出各 Agent 的**正式 TUI**
（不 tee stdout，避免把界面打成工具文本流）。结果优先读 `output.md`；
`preview_prompt.md` / `USER_GOAL.md` 会落盘；exec 模式仍写 `preview.log`。

| Profile | 预览命令（可见终端） |
|---------|----------------------|
| `deepseek-harness` | **TUI**：`codewhale --fresh --approval-policy auto -p "<prompt>"` |
| `claude-code` | **TUI**：`claude --permission-mode … "<prompt>"`（不用 `-p/--print`） |
| `pi` | **TUI**：`pi -- "<prompt>"`（不用 `--print` / `--mode json`） |
| `openclaw` | 默认：`openclaw agent exec --cwd … --message-file …`（弹窗 tee；`OPENCLAW_PREVIEW_UI=tui` 才走 chat TUI） |
| `hermes` | 默认：`hermes -z … --yolo`（弹窗 tee；避开 Windows 上 `hermes chat` → `cli` ImportError。`HERMES_PREVIEW_UI=tui` 才走现代 TUI） |

无桌面会话（如纯 SSH / Windows Service）时会打警告并**回退无头**，避免挂死。
CodeWhale 安装见 [Hmbown/CodeWhale](https://github.com/Hmbown/CodeWhale)。

若仍要旧的无头文字预览：`$env:HARNESS_PREVIEW_MODE="exec"`（或 `CODEWHALE_PREVIEW_MODE=exec`）。

deepseek 预览路径会把**用户目标放在 prompt 最前**（避免把 system 标题当成任务）。

```powershell
$env:HARNESS_LIVE_PREVIEW="1"
# 重启 agents 后，Console 建任务即弹窗
```

## Edge Node（推荐本机路径）

本机用 [`apps/client/aop-node`](../../apps/client/aop-node) 的 `aopd` Supervisor：

- `plugins/*/plugin.toml` 使用 **`kind = "cli"`**，`command` 为厂家二进制（`claude` / `dsh` / `pi` / `openclaw` / `hermes`）
- **A2A HTTP 由 aopd 在单一端口托管**（`cli_a2a_listen`，默认 `127.0.0.1:8020`，路径 `/agents/<plugin-id>/`；不再长期 spawn `python … harness-agent`）
- 任务执行时短生命周期 spawn 厂家 CLI；`/health` 的 `runner_ready` 表示本机能否解析该 CLI
- 向 Gateway 注册各 endpoint，并向 Orchestrator 发节点 + agent 生命周期心跳

无 aopd 的 standalone/dev 仍可用 `agents/harness-agent` + `_harness/launch.py`。

| 变量 | 含义 |
|------|------|
| `CLAUDE_CLI_ALLOWED_TOOLS` | Comma list for `--allowed-tools` (default includes WebSearch/WebFetch); `none` to omit |
| `A2A_OS_URL` / `GATEWAY_URL` | Heartbeat + optional cost POST |
| `AOP_NODE_MANAGED` | standalone harness-agent 下：`1` → 不自心跳（aopd 代发）；`kind=cli` 时 A2A 已在 aopd 内，无需此变量 |
| `HARNESS_HEARTBEAT` | 无 aop-node 时默认 `1` |
| `*_CLI_PATH` | 覆盖厂家 CLI 路径（`CLAUDE_CLI_PATH` / `OPENCLAW_CLI_PATH` / …） |

## Remote install (Claude Code–style)

```powershell
irm http://<a2a-os>:8000/install/claude-code.ps1 | iex
```

```bash
curl -fsSL http://<a2a-os>:8000/install/claude-code.sh | bash
```

## Local run

**默认：** `python scripts/dev_up.py` 会启动 **aopd**（`:7920`），由它 autostart `plugins/*` 并注册 Gateway。不要再并行跑 5 个 uvicorn。

单独跑 Supervisor：

```powershell
cd apps\client\aop-node
copy config\aop-node.example.toml aop-node.toml
cargo run -p aopd -- --config aop-node.toml
```

**无 Supervisor 回退**（`dev_up.py --legacy-agents`，或 `start_and_register_agents.py`）。`:7920` 已健康**且** 8011–8015 都在听才会跳过；节点空转时会自动拉起 harness。

```powershell
pip install -e "packages/agent-runtime[harness]"
pip install -e packages/a2a-sdk
python scripts/start_and_register_agents.py
# 即使端口已 occupied 仍再起一份：FORCE_START_SCRIPT=1
```

```powershell
$env:HARNESS_PROFILE="claude-code"; $env:PORT="8011"
uvicorn agent:app --app-dir agents/harness-agent --port 8011
```

## 审批接入

Harness 虚拟 Agent **共用** `create_harness_app` 上的 `/v1/approvals`。四种模式（`none` / `system` / `agent` / `both`）、计划字段、`hitl` 信号、回调 `actor=agent` 见 **[agent-approval.md](./agent-approval.md)**。

加新 profile 时不要复制审批实现；对齐 `profiles.py` 端口与 `agent_key` 即可。`A2A_OS_URL`（或 `GATEWAY_URL`）必须能打到 Gateway，否则 Agent 审完无法解锁任务。

## Async spawn（Agent 内委派）

父 Harness 可把耗时子目标交给另一个已注册 Agent，自己继续执行，稍后 join：

1. 子 Agent 的 `message/send` 在 `metadata.async=true` 或带 `callbackUrl` 时立即返回 `submitted`，后台跑 runner。
2. 父侧：`AgentCollaborator.spawn` / `join`，或 HTTP `POST /v1/collab/spawn` 与 `/v1/collab/join`。
3. Lineage 与 runtime edges 仍走现有 A2A OS 路径。

## Platform async（编排 Worker）

默认开启（`A2A_EXEC_ASYNC=1`）：Worker 对 Agent 使用 `async_mode` 提交后**立刻 ACK Redis 消息**，在线程池中 `tasks/get` join，再 `mark_success` 解锁下游。这样长跑节点不再占满单个 consumer。

| 变量 | 默认 | 含义 |
|------|------|------|
| `A2A_EXEC_ASYNC` | `1` | `0` 恢复同步阻塞执行 |
| `A2A_ASYNC_WORKERS` | `8` | join 线程池大小 |
| `A2A_ASYNC_JOIN_TIMEOUT` | `A2A_TIMEOUT` 或 `900` | join 最长等待（秒） |
| `A2A_ASYNC_POLL_INTERVAL` | `2` | poll 间隔（秒） |

不支持 async 的旧 Agent 会忽略 flag 并同步跑完；Worker 见 `completed` 后仍走原 finalize 路径。
