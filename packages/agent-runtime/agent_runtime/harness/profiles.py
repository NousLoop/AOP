"""Canonical harness virtual-agent profile table (single source of truth).

Ports / keys / default runners for:
- ``scripts/start_and_register_agents.py``
- ``marketplace`` catalog + bundle ports
- router host→localhost remapping
- aop-node ``plugins/*/plugin.toml`` (keep in sync manually; values live here)

Specialty coder/researcher splits are gone — one row per product.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from typing import Iterable


@dataclass(frozen=True)
class HarnessProfile:
    key: str
    port: int
    runner: str
    name: str
    description: str
    tags: tuple[str, ...] = ()
    env_url_key: str = ""
    skills: tuple[str, ...] = ()
    plugin_id: str = ""

    @property
    def package_id(self) -> str:
        return f"pkg-{self.key}"

    @property
    def default_endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"


HARNESS_PROFILES: tuple[HarnessProfile, ...] = (
    HarnessProfile(
        key="claude-code",
        port=8011,
        runner="claude_cli",
        name="Claude Code Agent",
        description="Unified Claude Code harness agent",
        tags=("harness", "claude"),
        env_url_key="AOP_AGENT_CLAUDE_CODE_URL",
    ),
    HarnessProfile(
        key="deepseek-harness",
        port=8012,
        runner="deepseek",
        name="DeepSeek Harness Agent",
        description="Unified DeepSeek Harness agent",
        tags=("harness", "deepseek"),
        env_url_key="AOP_AGENT_DEEPSEEK_HARNESS_URL",
    ),
    HarnessProfile(
        key="pi",
        port=8013,
        runner="pi_cli",
        name="Pi Agent",
        description="Unified Pi harness agent",
        tags=("harness", "pi"),
        env_url_key="AOP_AGENT_PI_URL",
    ),
    HarnessProfile(
        key="openclaw",
        port=8014,
        runner="openclaw",
        name="OpenClaw Agent",
        description="Unified OpenClaw harness agent (agent exec)",
        tags=("harness", "openclaw"),
        env_url_key="AOP_AGENT_OPENCLAW_URL",
    ),
    HarnessProfile(
        key="hermes",
        port=8015,
        runner="hermes",
        name="Hermes Agent",
        description="Unified Hermes Agent harness (hermes chat)",
        tags=("harness", "hermes"),
        env_url_key="AOP_AGENT_HERMES_URL",
    ),
)

# Same five products under aopd, distinct ports + card names → distinct agent_key.
AOPD_HARNESS_PROFILES: tuple[HarnessProfile, ...] = (
    HarnessProfile(
        key="aopd-claude-code",
        port=8021,
        runner="claude_cli",
        name="aopd Claude Code",
        description="Claude Code harness supervised by aopd",
        tags=("harness", "claude", "aopd"),
        env_url_key="AOP_AGENT_AOPD_CLAUDE_CODE_URL",
        plugin_id="claude-code",
    ),
    HarnessProfile(
        key="aopd-deepseek-harness",
        port=8022,
        runner="deepseek",
        name="aopd DeepSeek Harness",
        description="DeepSeek Harness supervised by aopd",
        tags=("harness", "deepseek", "aopd"),
        env_url_key="AOP_AGENT_AOPD_DEEPSEEK_HARNESS_URL",
        plugin_id="deepseek-harness",
    ),
    HarnessProfile(
        key="aopd-pi",
        port=8023,
        runner="pi_cli",
        name="aopd Pi",
        description="Pi harness supervised by aopd",
        tags=("harness", "pi", "aopd"),
        env_url_key="AOP_AGENT_AOPD_PI_URL",
        plugin_id="pi",
    ),
    HarnessProfile(
        key="aopd-openclaw",
        port=8024,
        runner="openclaw",
        name="aopd OpenClaw",
        description="OpenClaw harness supervised by aopd",
        tags=("harness", "openclaw", "aopd"),
        env_url_key="AOP_AGENT_AOPD_OPENCLAW_URL",
        plugin_id="openclaw",
    ),
    HarnessProfile(
        key="aopd-hermes",
        port=8025,
        runner="hermes",
        name="aopd Hermes",
        description="Hermes harness supervised by aopd",
        tags=("harness", "hermes", "aopd"),
        env_url_key="AOP_AGENT_AOPD_HERMES_URL",
        plugin_id="hermes",
    ),
)


def all_profiles() -> tuple[HarnessProfile, ...]:
    return HARNESS_PROFILES + AOPD_HARNESS_PROFILES


def profile_by_key(key: str) -> HarnessProfile | None:
    k = (key or "").strip().lower()
    for p in all_profiles():
        if p.key == k:
            return p
    return None


def default_ports() -> dict[str, int]:
    return {p.key: p.port for p in HARNESS_PROFILES}


def aopd_plugin_ports() -> dict[str, int]:
    """aop-node plugin folder id → listen port."""
    return {(p.plugin_id or p.key): p.port for p in AOPD_HARNESS_PROFILES}


def host_port_map() -> dict[str, int]:
    """Docker service host names → local ports (plus harness-agent alias)."""
    m = {p.key: p.port for p in all_profiles()}
    m["harness-agent"] = 8011
    return m


def as_port_tuples() -> list[tuple[str, int]]:
    return [(p.key, p.port) for p in HARNESS_PROFILES]


def as_aopd_plugin_port_tuples() -> list[tuple[str, int]]:
    return [((p.plugin_id or p.key), p.port) for p in AOPD_HARNESS_PROFILES]


def marketplace_catalog_rows(
    *,
    endpoint_resolver: Callable[[str, str], str] | None = None,
) -> list[dict]:
    """Build marketplace CATALOG dicts from the profile table."""

    def _ep(env_key: str, fallback: str) -> str:
        if endpoint_resolver is not None:
            return str(endpoint_resolver(env_key, fallback)).rstrip("/")
        import os

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
                "tags": list(p.tags),
            }
        )
    return rows


__all__ = [
    "HarnessProfile",
    "HARNESS_PROFILES",
    "AOPD_HARNESS_PROFILES",
    "all_profiles",
    "profile_by_key",
    "default_ports",
    "aopd_plugin_ports",
    "host_port_map",
    "as_port_tuples",
    "as_aopd_plugin_port_tuples",
    "marketplace_catalog_rows",
]
