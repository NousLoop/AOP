"""Harness profiles — loaded from packages/catalogs/harness_profiles.json (aopd only).

All work agents are hosted by aopd on a shared CLI A2A listen
(``cli_a2a_listen``, default ``127.0.0.1:8020``) with path ``/agents/<plugin_id>/``.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HarnessProfile:
    key: str
    runner: str
    name: str
    description: str
    tags: tuple[str, ...] = ()
    env_url_key: str = ""
    skills: tuple[str, ...] = ()
    plugin_id: str = ""
    path: str = ""

    @property
    def package_id(self) -> str:
        return f"pkg-{self.key}"

    @property
    def port(self) -> int:
        """Shared aopd listen port. Not a per-agent bind."""
        hostport = CLI_A2A_LISTEN.rsplit(":", 1)
        try:
            return int(hostport[-1])
        except ValueError:
            return 8020

    @property
    def default_endpoint(self) -> str:
        host = CLI_A2A_LISTEN
        if "://" not in host:
            host = f"http://{host}"
        p = self.path if self.path.startswith("/") else f"/{self.path}" if self.path else ""
        return f"{host.rstrip('/')}{p.rstrip('/')}"


def _catalog_path() -> Path | None:
    env = (os.getenv("AOP_REPO_ROOT") or "").strip()
    if env:
        p = Path(env) / "packages" / "catalogs" / "harness_profiles.json"
        if p.is_file():
            return p
    here = Path(__file__).resolve()
    for parent in here.parents:
        p = parent / "packages" / "catalogs" / "harness_profiles.json"
        if p.is_file():
            return p
    return None


def _row_to_profile(row: dict[str, Any]) -> HarnessProfile:
    return HarnessProfile(
        key=str(row["key"]),
        runner=str(row.get("runner") or "claude_cli"),
        name=str(row.get("name") or row["key"]),
        description=str(row.get("description") or ""),
        tags=tuple(str(t) for t in (row.get("tags") or ())),
        env_url_key=str(row.get("env_url_key") or ""),
        skills=tuple(str(s) for s in (row.get("skills") or ())),
        plugin_id=str(row.get("plugin_id") or ""),
        path=str(row.get("path") or ""),
    )


def _fallback_catalog() -> dict[str, Any]:
    return {
        "cli_a2a_listen": "127.0.0.1:8020",
        "agents": [
            {
                "key": "aopd-claude-code",
                "plugin_id": "claude-code",
                "path": "/agents/claude-code",
                "runner": "claude_cli",
                "name": "aopd Claude Code",
                "description": "Claude Code via aopd shared CLI A2A",
                "tags": ["harness", "claude", "aopd"],
                "env_url_key": "AOP_AGENT_CLAUDE_CODE_URL",
            },
            {
                "key": "aopd-deepseek-harness",
                "plugin_id": "deepseek-harness",
                "path": "/agents/deepseek-harness",
                "runner": "deepseek",
                "name": "aopd DeepSeek Harness",
                "description": "DeepSeek via aopd shared CLI A2A",
                "tags": ["harness", "deepseek", "aopd"],
                "env_url_key": "AOP_AGENT_DEEPSEEK_HARNESS_URL",
            },
            {
                "key": "aopd-pi",
                "plugin_id": "pi",
                "path": "/agents/pi",
                "runner": "pi_cli",
                "name": "aopd Pi",
                "description": "Pi via aopd shared CLI A2A",
                "tags": ["harness", "pi", "aopd"],
                "env_url_key": "AOP_AGENT_PI_URL",
            },
            {
                "key": "aopd-openclaw",
                "plugin_id": "openclaw",
                "path": "/agents/openclaw",
                "runner": "openclaw",
                "name": "aopd OpenClaw",
                "description": "OpenClaw via aopd shared CLI A2A",
                "tags": ["harness", "openclaw", "aopd"],
                "env_url_key": "AOP_AGENT_OPENCLAW_URL",
            },
            {
                "key": "aopd-hermes",
                "plugin_id": "hermes",
                "path": "/agents/hermes",
                "runner": "hermes",
                "name": "aopd Hermes",
                "description": "Hermes via aopd shared CLI A2A",
                "tags": ["harness", "hermes", "aopd"],
                "env_url_key": "AOP_AGENT_HERMES_URL",
            },
        ],
    }


def _load_catalog() -> dict[str, Any]:
    path = _catalog_path()
    if path is None:
        logger.warning(
            "packages/catalogs/harness_profiles.json not found; using built-in fallback"
        )
        return _fallback_catalog()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("failed to load %s: %s", path, exc)
        return _fallback_catalog()
    # Migrate older {legacy,aopd} shape if someone still has a checkout copy.
    if "agents" not in data and "aopd" in data:
        data = {
            "cli_a2a_listen": data.get("cli_a2a_listen") or "127.0.0.1:8020",
            "agents": list(data.get("aopd") or []),
        }
    return data


_CATALOG = _load_catalog()
CLI_A2A_LISTEN: str = str(_CATALOG.get("cli_a2a_listen") or "127.0.0.1:8020")

# Single product table: aopd-hosted agents only.
HARNESS_PROFILES: tuple[HarnessProfile, ...] = tuple(
    _row_to_profile(r) for r in (_CATALOG.get("agents") or [])
)
# Back-compat alias (same rows).
AOPD_HARNESS_PROFILES: tuple[HarnessProfile, ...] = HARNESS_PROFILES


def all_profiles() -> tuple[HarnessProfile, ...]:
    return HARNESS_PROFILES


def profile_by_key(key: str) -> HarnessProfile | None:
    k = (key or "").strip().lower()
    for p in HARNESS_PROFILES:
        if p.key == k or (p.plugin_id and p.plugin_id == k):
            return p
    return None


def default_ports() -> dict[str, int]:
    """plugin_id / agent_key → listen port (all share 8020)."""
    m: dict[str, int] = {}
    for p in HARNESS_PROFILES:
        m[p.key] = p.port
        if p.plugin_id:
            m[p.plugin_id] = p.port
    return m


def aopd_plugin_ports() -> dict[str, int]:
    return {(p.plugin_id or p.key): p.port for p in HARNESS_PROFILES}


def aopd_plugin_endpoints() -> dict[str, str]:
    return {(p.plugin_id or p.key): p.default_endpoint for p in HARNESS_PROFILES}


def host_endpoint_map() -> dict[str, str]:
    """Service/agent name → aopd path URL. Listen address is not per-agent."""
    m: dict[str, str] = {}
    for p in HARNESS_PROFILES:
        m[p.key] = p.default_endpoint
        if p.plugin_id:
            m[p.plugin_id] = p.default_endpoint
    return m


def host_port_map() -> dict[str, int]:
    """Deprecated: every known harness name shares the single aopd listen port."""
    port = HARNESS_PROFILES[0].port if HARNESS_PROFILES else 8020
    m = {name: port for name in host_endpoint_map()}
    m["harness-agent"] = port
    return m


def as_port_tuples() -> list[tuple[str, int]]:
    """(plugin_id, port) for each agent — preferred for health / register loops."""
    return [((p.plugin_id or p.key), p.port) for p in HARNESS_PROFILES]


def as_aopd_plugin_port_tuples() -> list[tuple[str, int]]:
    return as_port_tuples()


def marketplace_catalog_rows(
    *,
    endpoint_resolver: Callable[[str, str], str] | None = None,
) -> list[dict]:
    def _ep(env_key: str, fallback: str) -> str:
        if endpoint_resolver is not None:
            return str(endpoint_resolver(env_key, fallback)).rstrip("/")
        return (os.getenv(env_key) or fallback).rstrip("/")

    rows: list[dict] = []
    for p in HARNESS_PROFILES:
        rows.append(
            {
                "package_id": p.package_id,
                "name": p.name,
                "description": p.description,
                "publisher": "AOP Official",
                "version": "0.1.0",
                "skills": list(p.skills),
                "default_endpoint": _ep(p.env_url_key, p.default_endpoint),
                "agent_key": p.key,
                "plugin_id": p.plugin_id,
                "tags": list(p.tags),
            }
        )
    return rows


__all__ = [
    "CLI_A2A_LISTEN",
    "HarnessProfile",
    "HARNESS_PROFILES",
    "AOPD_HARNESS_PROFILES",
    "all_profiles",
    "profile_by_key",
    "default_ports",
    "aopd_plugin_ports",
    "aopd_plugin_endpoints",
    "host_endpoint_map",
    "host_port_map",
    "as_port_tuples",
    "as_aopd_plugin_port_tuples",
    "marketplace_catalog_rows",
]
