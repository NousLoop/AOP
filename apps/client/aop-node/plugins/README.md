# aop-node plugins

Supervisor discovers `plugins/<id>/plugin.toml` and may autostart them.

## Ownership

| Concern | Owner when using aopd |
|---------|------------------------|
| Start / stop A2A endpoints | aop-node (`kind = "cli"` → in-process; other kinds → child process) |
| Task execution | Short-lived **manufacturer CLI** (`claude` / `openclaw` / `hermes` / `pi` / `dsh`) |
| `POST /v1/agents/register` | aop-node (node + each healthy child endpoint) |
| Lifecycle heartbeat | aop-node (`/v1/agent-runtime/{id}/heartbeat` for node + each harness) |
| A2A `message/send` | aopd-hosted A2A for `kind = "cli"` |

Without aop-node, use `scripts/start_and_register_agents.py` (dev-only; each agent heartbeats itself via `agents/harness-agent`).

## Layout

| Path | Role |
|------|------|
| `_harness/launch.py` | Standalone launcher (no aopd): find AOP repo → uvicorn harness-agent |
| `_harness/run_profile.py` | Standalone plugin entry (marks node-managed) |
| `claude-code/` … `hermes/` | `plugin.toml` only — `kind = "cli"`, `command` = manufacturer binary |
| `echo/` | Smoke-test plugin (not an A2A agent) |

```text
plugins/<id>/
  plugin.toml   # kind=cli, command=<vendor-cli>  (A2A is /agents/<id>/ on cli_a2a_listen)
```

**Agent A2A code is not the plugin child.** For `kind = "cli"`, aopd binds one shared port (`cli_a2a_listen`, default `127.0.0.1:8020`) and routes `/agents/<id>/`. Tasks spawn the vendor CLI. Profiles/cards still live in monorepo `agents/harness-agent/profiles/<id>/`. Peer approval: `/agents/<id>/v1/approvals`.

## Prerequisites

Manufacturer CLIs on `PATH` (or `*_CLI_PATH`):

| Plugin | Binary | Optional path env |
|--------|--------|-------------------|
| claude-code | `claude` | `CLAUDE_CLI_PATH` |
| deepseek-harness | `dsh` | `DSH_CLI_PATH` |
| pi | `pi` | `PI_CLI_PATH` |
| openclaw | `openclaw` | `OPENCLAW_CLI_PATH` |
| hermes | `hermes` | `HERMES_CLI_PATH` |

Missing CLI → A2A `/health` reports `runner_ready=false` (endpoint still up).

Optional: set `AOP_REPO_ROOT` so aopd can load Agent Cards from `agents/harness-agent/profiles/`.

## Manual smoke (standalone harness-agent, no aopd)

```powershell
cd apps\client\aop-node\plugins\claude-code
$env:HARNESS_PROFILE="claude-code"; $env:PORT="8011"; $env:AGENT_ID="claude-code"
$env:AOP_PLUGIN_DIR=(Resolve-Path .)
python -u ..\_harness\launch.py
# → http://127.0.0.1:8011/health
```

## Via aopd

```powershell
cd apps\client\aop-node
cargo run -p aopd -- --config aop-node.toml
# or Windows service: sc start aop-node
curl http://127.0.0.1:7920/v1/agents
curl http://127.0.0.1:7920/v1/agents/claude-code/start
curl http://127.0.0.1:8020/health
curl http://127.0.0.1:8020/agents/claude-code/health
```
