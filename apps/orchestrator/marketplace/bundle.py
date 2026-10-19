"""Build downloadable zip bundles for virtual agents (harness × role profile).

Remote hosts unzip → pip install → run → register to A2A OS, without cloning
the full AOP monorepo.
"""

from __future__ import annotations

import io
import json
import logging
import os
import zipfile
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# Skip noise when vendoring packages into the zip.
_VENDOR_SKIP_DIR_NAMES = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".git",
        "egg-info",
        "dist",
        "build",
        "node_modules",
        ".tox",
        ".venv",
        "venv",
    }
)
_VENDOR_SKIP_SUFFIXES = (".pyc", ".pyo", ".pyd")

try:
    from agent_runtime.harness.profiles import default_ports as _default_ports

    DEFAULT_PORTS: dict[str, int] = _default_ports()
except ImportError:  # pragma: no cover
    DEFAULT_PORTS = {}

HARNESS_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("pi", "pi_cli"),
    ("deepseek", "deepseek"),
    ("dsh", "deepseek"),
    ("openclaw", "openclaw"),
    ("hermes", "hermes"),
    ("claude", "claude_cli"),
)


class BundleError(LookupError):
    """Profile or package cannot be bundled."""


def repo_root(explicit: Optional[str | Path] = None) -> Path:
    if explicit is not None:
        return Path(explicit).resolve()
    env = os.getenv("AOP_REPO_ROOT")
    if env:
        return Path(env).resolve()
    # apps/orchestrator/marketplace/bundle.py → repo root
    return Path(__file__).resolve().parents[3]


def harness_for_profile(profile: str) -> str:
    p = (profile or "").lower()
    for prefix, harness in HARNESS_BY_PREFIX:
        if p.startswith(prefix):
            return harness
    return "claude_cli"


def default_port_for_profile(profile: str) -> int:
    """Shared aopd listen port (not a per-agent bind)."""
    if profile in DEFAULT_PORTS:
        return int(DEFAULT_PORTS[profile])
    try:
        from agent_runtime.harness.profiles import CLI_A2A_LISTEN

        return int(str(CLI_A2A_LISTEN).rsplit(":", 1)[-1])
    except Exception:  # noqa: BLE001
        return 8020


def resolve_package_meta(
    package_id: str,
    catalog: Iterable[dict[str, Any]],
    packages: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Resolve package metadata from live packages or static catalog."""
    pkg: dict[str, Any] | None = None
    if packages and package_id in packages:
        pkg = dict(packages[package_id])
    if pkg is None:
        for item in catalog:
            if (
                item.get("package_id") == package_id
                or item.get("agent_key") == package_id
                or item.get("plugin_id") == package_id
            ):
                pkg = dict(item)
                break
    if not pkg:
        raise BundleError(f"package not found: {package_id}")

    agent_key = str(pkg.get("agent_key") or pkg.get("profile") or package_id)
    if agent_key.startswith("pkg-"):
        agent_key = agent_key[4:]
    plugin_id = str(pkg.get("plugin_id") or "").strip()
    # Filesystem profile under agents/harness-agent/profiles/ is the plugin id
    # (claude-code), not the registry key (aopd-claude-code).
    profile = str(pkg.get("profile") or plugin_id or agent_key)
    if profile.startswith("aopd-") and not plugin_id:
        profile = profile[len("aopd-") :]
    harness = str(pkg.get("harness") or harness_for_profile(profile))
    version = str(pkg.get("version") or "0.1.0")
    port = int(pkg.get("default_port") or default_port_for_profile(agent_key))
    skills = list(pkg.get("skills") or [])
    name = str(pkg.get("name") or agent_key)
    description = str(pkg.get("description") or f"Virtual agent: {agent_key}")
    return {
        **pkg,
        "package_id": pkg.get("package_id") or f"pkg-{agent_key}",
        "agent_key": agent_key,
        "plugin_id": plugin_id or profile,
        "profile": profile,
        "harness": harness,
        "version": version,
        "default_port": port,
        "skills": skills,
        "name": name,
        "description": description,
        "bundle": True,
    }


def _should_skip(path: Path) -> bool:
    name = path.name
    if name in _VENDOR_SKIP_DIR_NAMES:
        return True
    if name.endswith(".egg-info") or name.endswith(".dist-info"):
        return True
    if path.suffix in _VENDOR_SKIP_SUFFIXES:
        return True
    return False


def _iter_vendor_files(src: Path) -> Iterable[tuple[Path, Path]]:
    """Yield (absolute_file, relative_to_src) for vendoring."""
    if not src.is_dir():
        return
    for root, dirs, files in os.walk(src):
        root_path = Path(root)
        # prune in-place
        dirs[:] = [d for d in dirs if not _should_skip(root_path / d)]
        for fname in files:
            fp = root_path / fname
            if _should_skip(fp):
                continue
            rel = fp.relative_to(src)
            yield fp, rel


def _agent_py(meta: dict[str, Any]) -> str:
    profile = meta["profile"]
    harness = meta["harness"]
    agent_id = meta["agent_key"]
    version = meta["version"]
    port = meta["default_port"]
    return f'''"""Standalone virtual agent: {profile} (harness × role).

Generated by AOP Marketplace bundle download. Card + system prompt live
alongside this file; runtime is vendored under ./vendor/.
"""

from __future__ import annotations

import os
from pathlib import Path

from agent_runtime.harness.adapter import create_harness_app, load_agent_card
from agent_runtime.harness.runners import create_runner

ROOT = Path(__file__).resolve().parent
CARD_PATH = ROOT / "agent-card.json"
SYSTEM_PATH = ROOT / "system.md"
SYSTEM_PROMPT = SYSTEM_PATH.read_text(encoding="utf-8") if SYSTEM_PATH.exists() else ""

PROFILE = {profile!r}
HARNESS_RUNNER = os.getenv("HARNESS_RUNNER") or {harness!r}
AGENT_ID = os.getenv("AGENT_ID") or {agent_id!r}

_card = load_agent_card(CARD_PATH)
_runner = create_runner(HARNESS_RUNNER)

app = create_harness_app(
    agent_id=AGENT_ID,
    runner=_runner,
    card=_card,
    system_prompt=SYSTEM_PROMPT,
    enable_heartbeat=os.getenv("HARNESS_HEARTBEAT", "1") not in {{"0", "false", "off"}},
    title="AOP " + str(_card.get("name") or AGENT_ID),
    version=str(_card.get("version") or {version!r}),
)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", str({port})))
    uvicorn.run("agent:app", host="0.0.0.0", port=port, reload=False)
'''


def _requirements_txt() -> str:
    return """# Install vendored AOP packages then HTTP stack.
# From the unzipped agent directory:
#   pip install -r requirements.txt

-e ./vendor/llm-provider
-e ./vendor/agent-runtime[harness]
fastapi>=0.110
uvicorn>=0.27
httpx>=0.27
openai>=1.40
"""


def _env_example(meta: dict[str, Any]) -> str:
    harness = meta["harness"]
    port = meta["default_port"]
    lines = [
        f"# {meta['name']} — remote host env",
        f"PORT={port}",
        f"AGENT_ID={meta['agent_key']}",
        f"AGENT_URL=http://127.0.0.1:{port}/",
        "A2A_OS_URL=http://127.0.0.1:8000",
        "GATEWAY_URL=http://127.0.0.1:8080",
        "# Optional API key for OS register / heartbeat",
        "# AOP_API_KEY=",
        "HARNESS_HEARTBEAT=1",
        f"HARNESS_RUNNER={harness}",
        "",
    ]
    if harness == "claude_cli":
        lines += [
            "# Claude Code CLI (install separately)",
            "# CLAUDE_CLI_PATH=claude",
            "",
        ]
    elif harness == "pi_cli":
        lines += [
            "# Pi CLI (install separately): https://github.com/earendil-works/pi",
            "# PI_CLI_PATH=pi",
            "",
        ]
    else:
        lines += [
            "# DeepSeek harness — SDK / dsh / API tool_loop",
            "# DEEPSEEK_API_KEY=",
            "# DEEPSEEK_BASE_URL=",
            "# DEEPSEEK_MODEL=",
            "# DSH_CLI_PATH=dsh",
            "# DSH_RUNNER_MODE=auto",
            "",
        ]
    return "\n".join(lines)


def _readme(meta: dict[str, Any]) -> str:
    profile = meta["profile"]
    harness = meta["harness"]
    port = meta["default_port"]
    skills = ", ".join(meta.get("skills") or []) or "(see agent-card.json)"
    binary_note = {
        "claude_cli": "Install [Claude Code](https://github.com/anthropics/claude-code) and ensure `claude` is on PATH (or set `CLAUDE_CLI_PATH`).",
        "pi_cli": "Install [Pi](https://github.com/earendil-works/pi) and ensure `pi` is on PATH (or set `PI_CLI_PATH`).",
        "deepseek": "Install [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) and/or set `DEEPSEEK_API_KEY` for the tool_loop fallback.",
    }.get(harness, "Install the harness CLI required by this profile.")

    return f"""# {meta['name']}

Virtual agent bundle: **harness × role** = `{harness}` × `{profile}`.

Skills: {skills}

## Requirements

- Python 3.11+
- {binary_note}
- Network reachability to your A2A OS / Gateway

## Install

```bash
cd {profile}
python -m venv .venv
# Windows: .venv\\Scripts\\activate
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edit AGENT_URL / A2A_OS_URL / GATEWAY_URL
```

## Run

```bash
# load .env in your shell, or export vars manually
uvicorn agent:app --host 0.0.0.0 --port {port}
```

Health: `GET http://127.0.0.1:{port}/health`  
Card: `GET http://127.0.0.1:{port}/.well-known/agent-card.json`

## Register with A2A OS

Point `AGENT_URL` at an address the OS can reach (not only loopback if OS is remote):

```bash
python scripts/register.py
```

This POSTs to `$GATEWAY_URL/v1/agents/register` (fallback `$A2A_OS_URL/v1/marketplace/register`).

## Docker (optional)

```bash
docker build -t aop-{profile} .
docker run --rm -p {port}:{port} --env-file .env aop-{profile}
```

## Cancel / heartbeat

- OS cancel fans out as A2A `tasks/cancel` → harness process kill
- Heartbeat: `POST {{A2A_OS_URL}}/v1/agent-runtime/{{AGENT_ID}}/heartbeat`
"""


def _register_py(meta: dict[str, Any]) -> str:
    skills = meta.get("skills") or []
    skills_lit = json.dumps(skills)
    return f'''"""Register this virtual agent endpoint with A2A OS / Gateway."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    import httpx
except ImportError:
    print("httpx required: pip install httpx", file=sys.stderr)
    raise SystemExit(1)

ROOT = Path(__file__).resolve().parents[1]
CARD_PATH = ROOT / "agent-card.json"


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def main() -> int:
    _load_dotenv()
    endpoint = (os.getenv("AGENT_URL") or "").rstrip("/")
    if not endpoint:
        print("AGENT_URL is required", file=sys.stderr)
        return 1

    gateway = (os.getenv("GATEWAY_URL") or "").rstrip("/")
    os_url = (os.getenv("A2A_OS_URL") or "").rstrip("/")
    api_key = os.getenv("AOP_API_KEY") or os.getenv("API_KEY") or ""

    headers = {{"Content-Type": "application/json"}}
    if api_key:
        headers["Authorization"] = f"Bearer {{api_key}}"
        headers["X-API-Key"] = api_key

    card: dict = {{}}
    if CARD_PATH.is_file():
        card = json.loads(CARD_PATH.read_text(encoding="utf-8"))

    agent_id = os.getenv("AGENT_ID") or {meta["agent_key"]!r}
    skills = {skills_lit}
    if not skills and isinstance(card.get("skills"), list):
        skills = [s.get("id") for s in card["skills"] if isinstance(s, dict) and s.get("id")]

    # Prefer Gateway agent register (endpoint discovery via agent-card).
    errors: list[str] = []
    if gateway:
        url = f"{{gateway}}/v1/agents/register"
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(url, json={{"endpoint": endpoint}}, headers=headers)
            print(f"gateway register {{resp.status_code}}: {{resp.text[:500]}}")
            if resp.is_success:
                return 0
            errors.append(f"{{url}} -> {{resp.status_code}}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{{url}} -> {{exc}}")

    if os_url:
        url = f"{{os_url}}/v1/marketplace/register"
        body = {{
            "name": agent_id,
            "version": str(card.get("version") or {meta["version"]!r}),
            "description": card.get("description") or {meta["description"]!r},
            "endpoint": endpoint if endpoint.endswith("/") else endpoint + "/",
            "protocol": "a2a",
            "skills": skills,
            "capabilities": list((card.get("capabilities") or {{}}).keys())
            if isinstance(card.get("capabilities"), dict)
            else list(card.get("capabilities") or []),
            "metadata": {{"harness": {meta["harness"]!r}, "profile": {meta["profile"]!r}, "bundle": True}},
            "activate": True,
        }}
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(url, json=body, headers=headers)
            print(f"marketplace register {{resp.status_code}}: {{resp.text[:500]}}")
            if resp.is_success:
                return 0
            errors.append(f"{{url}} -> {{resp.status_code}}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{{url}} -> {{exc}}")

    print("register failed:", "; ".join(errors) or "no GATEWAY_URL / A2A_OS_URL", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _dockerfile(meta: dict[str, Any]) -> str:
    port = meta["default_port"]
    return f"""FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
COPY vendor ./vendor
RUN pip install --no-cache-dir -r requirements.txt
COPY agent.py agent-card.json system.md ./
COPY scripts ./scripts
ENV PORT={port}
ENV HARNESS_RUNNER={meta["harness"]}
ENV AGENT_ID={meta["agent_key"]}
EXPOSE {port}
CMD ["sh", "-c", "uvicorn agent:app --host 0.0.0.0 --port ${{PORT}}"]
"""


def build_agent_bundle(
    package_id: str,
    *,
    catalog: Iterable[dict[str, Any]],
    packages: Optional[dict[str, dict[str, Any]]] = None,
    root: Optional[str | Path] = None,
) -> tuple[bytes, dict[str, Any]]:
    """Build a zip for ``package_id``. Returns (zip_bytes, meta)."""
    meta = resolve_package_meta(package_id, catalog, packages)
    base = repo_root(root)
    profile = meta["profile"]
    profile_dir = base / "agents" / "harness-agent" / "profiles" / profile
    if not profile_dir.is_dir():
        raise BundleError(f"profile directory not found: {profile_dir}")

    card_path = profile_dir / "agent-card.json"
    system_path = profile_dir / "system.md"
    if not card_path.is_file():
        raise BundleError(f"missing agent-card.json in {profile_dir}")

    runtime_src = base / "packages" / "agent-runtime"
    llm_src = base / "packages" / "llm-provider"
    if not runtime_src.is_dir():
        raise BundleError(f"missing agent-runtime package: {runtime_src}")
    if not llm_src.is_dir():
        raise BundleError(f"missing llm-provider package: {llm_src}")

    prefix = f"{profile}/"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(prefix + "agent.py", _agent_py(meta))
        zf.writestr(prefix + "agent-card.json", card_path.read_text(encoding="utf-8"))
        if system_path.is_file():
            zf.writestr(prefix + "system.md", system_path.read_text(encoding="utf-8"))
        else:
            zf.writestr(prefix + "system.md", "")
        zf.writestr(prefix + "requirements.txt", _requirements_txt())
        zf.writestr(prefix + ".env.example", _env_example(meta))
        zf.writestr(prefix + "README.md", _readme(meta))
        zf.writestr(prefix + "Dockerfile", _dockerfile(meta))
        zf.writestr(prefix + "scripts/register.py", _register_py(meta))
        # manifest for tooling
        zf.writestr(
            prefix + "bundle.json",
            json.dumps(
                {
                    "package_id": meta["package_id"],
                    "agent_key": meta["agent_key"],
                    "profile": profile,
                    "harness": meta["harness"],
                    "version": meta["version"],
                    "default_port": meta["default_port"],
                    "skills": meta.get("skills") or [],
                },
                indent=2,
            )
            + "\n",
        )

        for src, vendor_name in ((runtime_src, "agent-runtime"), (llm_src, "llm-provider")):
            for abs_path, rel in _iter_vendor_files(src):
                arc = f"{prefix}vendor/{vendor_name}/{rel.as_posix()}"
                zf.write(abs_path, arcname=arc)

    data = buf.getvalue()
    meta = {
        **meta,
        "download_path": f"/v1/marketplace/agents/{meta['package_id']}/download",
        "bundle_version": meta["version"],
        "filename": f"{profile}-{meta['version']}.zip",
        "size_bytes": len(data),
    }
    logger.info(
        "built agent bundle package=%s profile=%s bytes=%s",
        meta["package_id"],
        profile,
        len(data),
    )
    return data, meta


def _install_ps1(meta: dict[str, Any], *, base_url: str) -> str:
    """Claude Code–style one-liner installer for Windows PowerShell.

    Usage::

        irm {base}/v1/marketplace/agents/{package_id}/install.ps1 | iex
    """
    package_id = meta["package_id"]
    profile = meta["profile"]
    harness = meta["harness"]
    port = meta["default_port"]
    version = meta["version"]
    name = meta["name"]
    base = base_url.rstrip("/")
    return f"""# AOP virtual agent installer — {name} ({profile})
# Mirrors Claude Code UX: irm <url>/install.ps1 | iex
# Generated by AOP Marketplace. Do not edit by hand.
param(
    [string]$BaseUrl = '{base}',
    [string]$InstallDir = '',
    [string]$OsUrl = '',
    [string]$GatewayUrl = '',
    [switch]$SkipPip,
    [switch]$Register,
    [switch]$Start
)

$ErrorActionPreference = 'Stop'
$PackageId = '{package_id}'
$Profile = '{profile}'
$Harness = '{harness}'
$DefaultPort = {port}
$Version = '{version}'

function Write-AopInfo([string]$Msg) {{ Write-Host "[aop] $Msg" -ForegroundColor Cyan }}
function Write-AopOk([string]$Msg) {{ Write-Host "[aop] $Msg" -ForegroundColor Green }}
function Write-AopWarn([string]$Msg) {{ Write-Host "[aop] $Msg" -ForegroundColor Yellow }}

if (-not $BaseUrl) {{
    throw 'BaseUrl is empty. Pass -BaseUrl http://your-a2a-os:8000'
}}
$BaseUrl = $BaseUrl.TrimEnd('/')

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {{ $py = Get-Command python3 -ErrorAction SilentlyContinue }}
if (-not $py) {{ throw 'Python 3.11+ is required on PATH (python or python3).' }}

if (-not $InstallDir) {{
    $InstallDir = Join-Path $HOME ".aop\\agents\\$Profile"
}}
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$zipPath = Join-Path $env:TEMP "aop-$Profile-$Version.zip"

$downloadUrl = "$BaseUrl/v1/marketplace/agents/$PackageId/download"
Write-AopInfo "Downloading $PackageId from $downloadUrl"
Invoke-WebRequest -Uri $downloadUrl -OutFile $zipPath -UseBasicParsing

Write-AopInfo "Extracting to $InstallDir"
if (Test-Path (Join-Path $InstallDir 'agent.py')) {{
    Write-AopWarn "Existing install found — overlaying files"
}}
Expand-Archive -Path $zipPath -DestinationPath $InstallDir -Force
# Zip root is profile/ — flatten if nested
$nested = Join-Path $InstallDir $Profile
if ((Test-Path (Join-Path $nested 'agent.py')) -and -not (Test-Path (Join-Path $InstallDir 'agent.py'))) {{
    Get-ChildItem -Force $nested | ForEach-Object {{
        Move-Item -Force $_.FullName (Join-Path $InstallDir $_.Name)
    }}
    Remove-Item -Recurse -Force $nested -ErrorAction SilentlyContinue
}}
Remove-Item -Force $zipPath -ErrorAction SilentlyContinue

Push-Location $InstallDir
try {{
    if (-not (Test-Path '.env') -and (Test-Path '.env.example')) {{
        Copy-Item '.env.example' '.env'
    }}
    if ($OsUrl) {{
        (Get-Content '.env') -replace '(?m)^A2A_OS_URL=.*', "A2A_OS_URL=$OsUrl" | Set-Content '.env'
    }} else {{
        (Get-Content '.env') -replace '(?m)^A2A_OS_URL=.*', "A2A_OS_URL=$BaseUrl" | Set-Content '.env'
    }}
    if ($GatewayUrl) {{
        (Get-Content '.env') -replace '(?m)^GATEWAY_URL=.*', "GATEWAY_URL=$GatewayUrl" | Set-Content '.env'
    }}

    if (-not $SkipPip) {{
        Write-AopInfo "Creating venv + pip install (vendored agent-runtime)"
        & $py.Source -m venv .venv
        $pip = Join-Path $InstallDir '.venv\\Scripts\\pip.exe'
        if (-not (Test-Path $pip)) {{ throw "venv pip missing: $pip" }}
        & $pip install -U pip
        & $pip install -r requirements.txt
    }}

    $binDir = Join-Path $HOME '.aop\\bin'
    New-Item -ItemType Directory -Force -Path $binDir | Out-Null
    $launcher = Join-Path $binDir "aop-$Profile.cmd"
    @"
@echo off
cd /d "$InstallDir"
if exist ".venv\\Scripts\\activate.bat" call ".venv\\Scripts\\activate.bat"
if exist ".env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
    if not "%%A"=="" if not "%%A"=="#" set "%%A=%%B"
  )
)
if "%PORT%"=="" set PORT=$DefaultPort
python -m uvicorn agent:app --host 0.0.0.0 --port %PORT%
"@ | Set-Content -Encoding ASCII $launcher

    if ($Register) {{
        Write-AopInfo "Registering with A2A OS"
        & (Join-Path $InstallDir '.venv\\Scripts\\python.exe') (Join-Path $InstallDir 'scripts\\register.py')
    }}

    Write-AopOk "Installed $Profile $Version -> $InstallDir"
    Write-Host ""
    Write-Host "Next steps (same idea as Claude Code after install):" -ForegroundColor White
    Write-Host "  1. Ensure harness binary is available ($Harness)"
    Write-Host "  2. Edit $InstallDir\\.env  (AGENT_URL must be reachable from OS)"
    Write-Host "  3. Start:  $launcher"
    Write-Host "     or:     cd $InstallDir; .\\.venv\\Scripts\\Activate.ps1; uvicorn agent:app --host 0.0.0.0 --port $DefaultPort"
    Write-Host "  4. Register: python scripts\\register.py"
    Write-Host ""
    Write-Host "Reinstall / update:" -ForegroundColor White
    Write-Host "  irm $BaseUrl/v1/marketplace/agents/$PackageId/install.ps1 | iex"

    if ($Start) {{
        Write-AopInfo "Starting agent on port $DefaultPort"
        & $launcher
    }}
}} finally {{
    Pop-Location
}}
"""


def _install_sh(meta: dict[str, Any], *, base_url: str) -> str:
    """Unix one-liner installer (curl | bash), Claude Code style."""
    package_id = meta["package_id"]
    profile = meta["profile"]
    harness = meta["harness"]
    port = meta["default_port"]
    version = meta["version"]
    name = meta["name"]
    base = base_url.rstrip("/")
    return f"""#!/usr/bin/env bash
# AOP virtual agent installer — {name} ({profile})
# Usage: curl -fsSL {base}/v1/marketplace/agents/{package_id}/install.sh | bash
set -euo pipefail

BASE_URL="${{AOP_BASE_URL:-{base}}}"
BASE_URL="${{BASE_URL%/}}"
PACKAGE_ID="{package_id}"
PROFILE="{profile}"
HARNESS="{harness}"
DEFAULT_PORT="{port}"
VERSION="{version}"
INSTALL_DIR="${{AOP_INSTALL_DIR:-$HOME/.aop/agents/$PROFILE}}"
SKIP_PIP="${{AOP_SKIP_PIP:-0}}"
REGISTER="${{AOP_REGISTER:-0}}"

info() {{ printf '\\033[36m[aop]\\033[0m %s\\n' "$*"; }}
ok() {{ printf '\\033[32m[aop]\\033[0m %s\\n' "$*"; }}

if ! command -v python3 >/dev/null 2>&1 && ! command -v python >/dev/null 2>&1; then
  echo "Python 3.11+ is required" >&2
  exit 1
fi
PY="$(command -v python3 || command -v python)"

mkdir -p "$INSTALL_DIR"
ZIP="$(mktemp -t aop-$PROFILE.XXXXXX.zip)"
DOWNLOAD_URL="$BASE_URL/v1/marketplace/agents/$PACKAGE_ID/download"
info "Downloading $PACKAGE_ID from $DOWNLOAD_URL"
curl -fsSL "$DOWNLOAD_URL" -o "$ZIP"

info "Extracting to $INSTALL_DIR"
TMP_EXTRACT="$(mktemp -d)"
unzip -qo "$ZIP" -d "$TMP_EXTRACT"
if [ -f "$TMP_EXTRACT/$PROFILE/agent.py" ]; then
  cp -a "$TMP_EXTRACT/$PROFILE"/. "$INSTALL_DIR"/
else
  cp -a "$TMP_EXTRACT"/. "$INSTALL_DIR"/
fi
rm -rf "$TMP_EXTRACT" "$ZIP"

cd "$INSTALL_DIR"
[ -f .env ] || cp .env.example .env
if grep -q '^A2A_OS_URL=' .env 2>/dev/null; then
  sed -i.bak "s|^A2A_OS_URL=.*|A2A_OS_URL=$BASE_URL|" .env && rm -f .env.bak
fi

if [ "$SKIP_PIP" != "1" ]; then
  info "Creating venv + pip install"
  "$PY" -m venv .venv
  .venv/bin/pip install -U pip
  .venv/bin/pip install -r requirements.txt
fi

mkdir -p "$HOME/.aop/bin"
LAUNCHER="$HOME/.aop/bin/aop-$PROFILE"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
cd "$INSTALL_DIR"
set -a
[ -f .env ] && . ./.env
set +a
export PORT="${{PORT:-$DEFAULT_PORT}}"
exec "$INSTALL_DIR/.venv/bin/python" -m uvicorn agent:app --host 0.0.0.0 --port "$PORT"
EOF
chmod +x "$LAUNCHER"

if [ "$REGISTER" = "1" ]; then
  info "Registering with A2A OS"
  .venv/bin/python scripts/register.py || true
fi

ok "Installed $PROFILE $VERSION -> $INSTALL_DIR"
echo
echo "Next steps:"
echo "  1. Ensure harness binary is available ($HARNESS)"
echo "  2. Edit $INSTALL_DIR/.env (AGENT_URL must be reachable from OS)"
echo "  3. Start:  $LAUNCHER"
echo "  4. Register: (cd $INSTALL_DIR && .venv/bin/python scripts/register.py)"
echo
echo "Reinstall:"
echo "  curl -fsSL $BASE_URL/v1/marketplace/agents/$PACKAGE_ID/install.sh | bash"
"""


def install_urls(package_id: str, *, base_url: str, profile: str | None = None) -> dict[str, str]:
    base = base_url.rstrip("/")
    pid = package_id
    # Prefer short Claude-like aliases when profile is known.
    short = profile or (pid[4:] if pid.startswith("pkg-") else pid)
    return {
        "download": f"{base}/v1/marketplace/agents/{pid}/download",
        "install_ps1": f"{base}/v1/marketplace/agents/{pid}/install.ps1",
        "install_sh": f"{base}/v1/marketplace/agents/{pid}/install.sh",
        "windows": f"irm {base}/install/{short}.ps1 | iex",
        "unix": f"curl -fsSL {base}/install/{short}.sh | bash",
        "windows_full": f"irm {base}/v1/marketplace/agents/{pid}/install.ps1 | iex",
        "unix_full": f"curl -fsSL {base}/v1/marketplace/agents/{pid}/install.sh | bash",
    }


def attach_bundle_meta(
    pkg: dict[str, Any],
    *,
    base_url: str | None = None,
    catalog: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Enrich a catalog/package dict with harness + one-liner install commands."""
    out = dict(pkg)
    try:
        packages = None
        if pkg.get("package_id"):
            packages = {str(pkg["package_id"]): pkg}
        meta = resolve_package_meta(
            str(pkg.get("package_id") or pkg.get("agent_key") or ""),
            catalog or [pkg],
            packages=packages,
        )
    except BundleError:
        return out
    out["harness"] = meta["harness"]
    out["profile"] = meta["profile"]
    out["default_port"] = meta["default_port"]
    out["bundle"] = True
    if base_url:
        urls = install_urls(meta["package_id"], base_url=base_url, profile=meta["profile"])
        out["download_url"] = urls["download"]
        out["install"] = urls
    return out


def render_install_ps1(
    package_id: str,
    *,
    base_url: str,
    catalog: Iterable[dict[str, Any]],
    packages: Optional[dict[str, dict[str, Any]]] = None,
) -> tuple[str, dict[str, Any]]:
    meta = resolve_package_meta(package_id, catalog, packages)
    return _install_ps1(meta, base_url=base_url), meta


def render_install_sh(
    package_id: str,
    *,
    base_url: str,
    catalog: Iterable[dict[str, Any]],
    packages: Optional[dict[str, dict[str, Any]]] = None,
) -> tuple[str, dict[str, Any]]:
    meta = resolve_package_meta(package_id, catalog, packages)
    return _install_sh(meta, base_url=base_url), meta


__all__ = [
    "BundleError",
    "DEFAULT_PORTS",
    "attach_bundle_meta",
    "build_agent_bundle",
    "default_port_for_profile",
    "harness_for_profile",
    "install_urls",
    "render_install_ps1",
    "render_install_sh",
    "repo_root",
    "resolve_package_meta",
]
