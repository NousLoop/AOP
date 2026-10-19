"""Tests for virtual-agent zip bundles + Claude-style install scripts."""

from __future__ import annotations

import io
import zipfile

import pytest

from marketplace.bundle import (
    BundleError,
    attach_bundle_meta,
    build_agent_bundle,
    harness_for_profile,
    install_urls,
    render_install_ps1,
    render_install_sh,
    resolve_package_meta,
)
from marketplace.service import CATALOG


def test_harness_for_profile():
    assert harness_for_profile("claude-code") == "claude_cli"
    assert harness_for_profile("deepseek-harness") == "deepseek"
    assert harness_for_profile("pi") == "pi_cli"


def test_resolve_by_agent_key():
    meta = resolve_package_meta("aopd-claude-code", CATALOG)
    assert meta["package_id"] == "pkg-aopd-claude-code"
    assert meta["agent_key"] == "aopd-claude-code"
    assert meta["plugin_id"] == "claude-code"
    assert meta["harness"] == "claude_cli"
    assert str(meta["default_port"]) == "8020"
    # plugin_id still resolves to the same aopd package
    by_plugin = resolve_package_meta("claude-code", CATALOG)
    assert by_plugin["agent_key"] == "aopd-claude-code"


def test_install_urls_claude_style():
    urls = install_urls(
        "pkg-aopd-claude-code", base_url="http://os:8000", profile="aopd-claude-code"
    )
    assert urls["windows"] == "irm http://os:8000/install/aopd-claude-code.ps1 | iex"
    assert urls["unix"] == "curl -fsSL http://os:8000/install/aopd-claude-code.sh | bash"
    assert "pkg-aopd-claude-code/install.ps1" in urls["windows_full"]


def test_attach_bundle_meta():
    pkg = attach_bundle_meta(CATALOG[0], base_url="http://127.0.0.1:8000", catalog=CATALOG)
    assert pkg["bundle"] is True
    assert pkg["install"]["windows"].startswith("irm ")
    assert "/download" in pkg["download_url"]


def test_render_install_ps1_embeds_download():
    script, meta = render_install_ps1(
        "pkg-aopd-claude-code",
        base_url="http://aop.example:8000",
        catalog=CATALOG,
    )
    assert meta["profile"] == "claude-code"
    assert meta["agent_key"] == "aopd-claude-code"
    assert "Invoke-WebRequest" in script
    assert "$PackageId = 'pkg-aopd-claude-code'" in script
    assert "/v1/marketplace/agents/$PackageId/download" in script


def test_render_install_sh_embeds_download():
    script, meta = render_install_sh(
        "aopd-claude-code",
        base_url="http://aop.example:8000",
        catalog=CATALOG,
    )
    assert meta["package_id"] == "pkg-aopd-claude-code"
    assert script.startswith("#!/usr/bin/env bash")
    assert "curl -fsSL" in script


def test_build_agent_bundle_zip():
    data, meta = build_agent_bundle("pkg-aopd-claude-code", catalog=CATALOG)
    assert meta["filename"].endswith(".zip")
    assert meta["size_bytes"] == len(data)
    assert len(data) > 1000
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
    # Zip layout still uses harness profile folder (plugin_id).
    assert "claude-code/agent.py" in names
    assert "claude-code/agent-card.json" in names
    assert "claude-code/system.md" in names
    assert "claude-code/requirements.txt" in names
    assert "claude-code/scripts/register.py" in names
    assert "claude-code/bundle.json" in names
    assert any(n.startswith("claude-code/vendor/agent-runtime/") for n in names)


def test_build_unknown_package():
    with pytest.raises(BundleError):
        build_agent_bundle("pkg-does-not-exist", catalog=CATALOG)
