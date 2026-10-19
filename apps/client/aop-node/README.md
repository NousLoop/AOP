# AOP Node Supervisor (`aop-node`)

Headless edge client that talks to **A2A OS** (Gateway + Orchestrator).

Inspired by patterns in sibling trees:

- **cc-switch** — OS privileges, process lifecycle, self-update
- **tabby** — plugin packages (`plugins/<id>/plugin.toml`)

| Binary | Role |
|--------|------|
| `aopd` | Supervisor daemon: register, heartbeat, **host A2A for `kind=cli` plugins**, spawn manufacturer CLIs per task, whitelist exec, local mgmt HTTP |
| `aop` | Optional CLI against the loopback mgmt API |

## Layout

```
aop-node/
  crates/aop-core         config, manifests, whitelist
  crates/aop-os           process / logs / service / update
  crates/aop-supervisor   OS connect + lifecycle + mgmt HTTP
  crates/aopd             daemon entry
  crates/aop-cli          `aop` CLI
  plugins/
    _harness/             standalone launcher (no aopd; uvicorn harness-agent)
    claude-code/          kind=cli → command=claude   (shared :8020 /agents/claude-code)
    deepseek-harness/     kind=cli → command=dsh
    pi/                   kind=cli → command=pi
    openclaw/             kind=cli → command=openclaw
    hermes/               kind=cli → command=hermes
    echo/                 smoke plugin
  config/aop-node.example.toml
```

## Build

Requires Rust stable (`cargo`).

```bash
cd apps/client/aop-node
cargo build --release
```

Binaries: `target/release/aopd` and `target/release/aop` (`.exe` on Windows).

## Run (offline smoke)

```bash
copy config\aop-node.example.toml aop-node.toml
# edit plugins_dir if needed
cargo run -p aopd -- --config aop-node.toml --offline
```

In another shell:

```bash
cargo run -p aop-cli -- status
cargo run -p aop-cli -- agents
cargo run -p aop-cli -- logs echo
cargo run -p aop-cli -- exec --plugin-id echo
```

Non-allowlisted exec is rejected (HTTP 403).

## Connect to A2A OS

1. Start **Postgres** (and Redis): e.g. `docker start aop-postgres aop-redis` from prior compose.
2. Start Gateway (`:8080`) and Orchestrator (`:8090`).
3. Copy `config/aop-node.example.toml` → `aop-node.toml`. Leave `api_key` empty if `AUTH_REQUIRED=false`.
4. Run `aopd` **without** `--offline`.

On boot the supervisor:

1. Starts mgmt HTTP (`/.well-known/agent-card.json` + `/health`)
2. Autostarts plugins under `plugins/`: for `kind = "cli"`, aopd serves all A2A on one loopback port (`cli_a2a_listen`, default `:8020`) at `/agents/<id>/` (no long-lived Python harness-agent child)
3. `POST {gateway}/v1/agents/register` for each healthy harness endpoint
4. Heartbeats to Orchestrator (falls back to Gateway)
5. On `message/send`, spawns the manufacturer CLI (`claude` / `dsh` / `pi` / `openclaw` / `hermes`) as a short-lived process

Harness plugins (see [`plugins/README.md`](./plugins/README.md)): `plugin.toml` sets `command` to the vendor binary. Optional `*_CLI_PATH` / `AOP_REPO_ROOT`. Standalone/dev without aopd still uses `_harness/launch.py` → `agents/harness-agent`.

## Scripts

若出现「禁止运行脚本 / Execution Policy」，不要改全局策略，用下面任一方式：

```powershell
# 推荐：仅本次绕过
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-service.ps1

# 或双击 / 在管理员 CMD 中：
.\scripts\install-service.cmd
```

```powershell
# Dev: ensure Postgres containers + build/start aopd
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-aop-node.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-aop-node.ps1 -Offline
# 或: .\scripts\start-aop-node.cmd

# Windows service (Administrator)
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-service.ps1
# 或: .\scripts\install-service.cmd
sc start aop-node
```

`aop service install` 在非管理员终端会拒绝访问；请用提升后的 CMD/PowerShell 跑上面的脚本。

## Self-update

Set `update_manifest_url` to a JSON document:

```json
{
  "version": "0.2.0",
  "url": "https://example.com/aopd-0.2.0.exe",
  "sha256": "<hex>",
  "notes": "..."
}
```

```bash
aop update
aop update --apply
```

Empty `update_manifest_url` disables checks.

## Whitelist

Only Supervisor can spawn children. Allowed:

- `whitelist.allowed_plugin_ids` — start by plugin/agent id
- `whitelist.allowed_binaries` — basename of argv[0] (e.g. `python`, `openclaw`)
- `whitelist.allowed_path_prefixes` — absolute path prefixes

Shell metacharacters in argv are rejected.

## Multi-version agents

Pin packages under `versions_dir` (e.g. `~/.aop/node/versions/<id>/<semver>/`) and point `command` / `workdir` at the chosen pin. The client itself is versioned via Cargo `0.1.0` / release artifacts.
