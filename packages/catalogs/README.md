# Shared catalogs

Machine-readable enums and tables shared across Gateway (Go), Web (TS), and `agent-runtime` (Python).

| File | Purpose |
|------|---------|
| `identities.json` | Agent business identity codes + zh labels |
| `harness_profiles.json` | aopd work agents: `key` + `plugin_id` + `path` only |

## Harness rules

- **One listen:** `cli_a2a_listen` (default `127.0.0.1:8020`). Agents do **not** declare their own port.
- **Identity:** registry `agent_key` is `aopd-<plugin>` (e.g. `aopd-claude-code`).
- **Path:** A2A URL is `{cli_a2a_listen}{path}` → `http://127.0.0.1:8020/agents/claude-code`.
- **Do not** write `agent:8011` / per-agent `:8020` in catalogs or call sites; remap old hostnames via `host_endpoint_map()`.

Legacy per-port uvicorn (`8011`–`8015`) is opt-in only: `python scripts/dev_up.py --legacy-agents`.

Consumers:

- **Go** — `go:embed` (`identities.go`, `harness.go`)
- **Web** — `apps/web/lib/identities.ts`, `apps/web/lib/harness.ts`
- **Python** — `agent_runtime.harness.platform_role` / `profiles`
