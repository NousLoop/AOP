"""Unit tests for harness profile catalog (aopd-only shared listen)."""

from __future__ import annotations

from agent_runtime.harness.profiles import (
    AOPD_HARNESS_PROFILES,
    CLI_A2A_LISTEN,
    HARNESS_PROFILES,
    aopd_plugin_endpoints,
    as_port_tuples,
    host_endpoint_map,
    marketplace_catalog_rows,
    profile_by_key,
)


def test_only_aopd_agents_no_per_agent_port() -> None:
    assert CLI_A2A_LISTEN == "127.0.0.1:8020"
    assert HARNESS_PROFILES is AOPD_HARNESS_PROFILES
    keys = [p.key for p in HARNESS_PROFILES]
    assert keys == [
        "aopd-claude-code",
        "aopd-deepseek-harness",
        "aopd-pi",
        "aopd-openclaw",
        "aopd-hermes",
    ]
    assert all(p.path.startswith("/agents/") for p in HARNESS_PROFILES)
    # Port comes only from cli_a2a_listen, not per-row JSON.
    assert all(p.port == 8020 for p in HARNESS_PROFILES)


def test_profile_by_plugin_id_and_aopd_key() -> None:
    assert profile_by_key("pi").key == "aopd-pi"
    assert profile_by_key("aopd-pi").default_endpoint == "http://127.0.0.1:8020/agents/pi"
    assert profile_by_key("missing") is None


def test_endpoints_not_agent_colon_port() -> None:
    eps = aopd_plugin_endpoints()
    assert eps["claude-code"] == "http://127.0.0.1:8020/agents/claude-code"
    assert as_port_tuples()[0] == ("claude-code", 8020)
    m = host_endpoint_map()
    assert m["aopd-pi"] == "http://127.0.0.1:8020/agents/pi"
    assert m["pi"] == m["aopd-pi"]


def test_marketplace_catalog_rows() -> None:
    rows = marketplace_catalog_rows(endpoint_resolver=lambda _k, fb: fb)
    assert len(rows) == 5
    assert rows[0]["agent_key"] == "aopd-claude-code"
    assert rows[0]["plugin_id"] == "claude-code"
    assert rows[0]["package_id"] == "pkg-aopd-claude-code"
    assert rows[0]["default_endpoint"] == "http://127.0.0.1:8020/agents/claude-code"


def test_aop_node_cli_plugins_match_catalog() -> None:
    from pathlib import Path

    from agent_runtime.harness.profiles import aopd_plugin_ports

    repo = Path(__file__).resolve().parents[3]
    plugins = repo / "apps" / "client" / "aop-node" / "plugins"
    assert plugins.is_dir(), plugins
    for plugin_id in aopd_plugin_ports():
        toml = plugins / plugin_id / "plugin.toml"
        assert toml.is_file(), f"missing plugin.toml for {plugin_id}: {toml}"
        text = toml.read_text(encoding="utf-8")
        assert 'kind = "cli"' in text, f"{toml} should be kind=cli"
        assert "AGENT_DISPLAY_NAME" in text
