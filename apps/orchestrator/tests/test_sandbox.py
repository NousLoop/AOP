"""Phase 22 sandbox policy unit tests."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from router import normalize_endpoint
from sandbox import (
    SandboxPolicy,
    SandboxViolation,
    check_endpoint,
    check_skill,
    clamp_input,
    clamp_output,
    guard_call,
    timeout_for_skill,
)


def test_allowlisted_agent_host_ok():
    with patch.dict(os.environ, {"AGENT_SANDBOX": "1"}, clear=False):
        check_endpoint("http://claude-code:8011/")


def test_unknown_host_denied():
    with patch.dict(os.environ, {"AGENT_SANDBOX": "1"}, clear=False):
        with pytest.raises(SandboxViolation, match="not allowlisted"):
            check_endpoint("http://evil.example:8080/")


def test_metadata_blocked_when_strict():
    with patch.dict(
        os.environ,
        {
            "AGENT_SANDBOX": "1",
            "AGENT_SANDBOX_STRICT": "1",
            "AGENT_ENDPOINT_ALLOWLIST": "169.254.169.254,127.0.0.1",
        },
        clear=False,
    ):
        with pytest.raises(SandboxViolation, match="blocked"):
            check_endpoint("http://169.254.169.254/latest/meta-data/")


def test_skill_denylist():
    with patch.dict(
        os.environ,
        {"AGENT_SANDBOX": "1", "AGENT_SKILL_DENYLIST": "text-to-video"},
        clear=False,
    ):
        with pytest.raises(SandboxViolation, match="denied"):
            check_skill("text-to-video")
            check_skill("claude-code")


def test_input_clamp_and_timeout():
    with patch.dict(
        os.environ,
        {
            "AGENT_SANDBOX": "1",
            "AGENT_MAX_INPUT_CHARS": "10",
            "AGENT_SKILL_TIMEOUTS": "claude-code:12",
            "A2A_TIMEOUT": "60",
        },
        clear=False,
    ):
        pol = SandboxPolicy.from_env()
        assert clamp_input("abcdefghijklmnop", pol) == "abcdefghij"
        assert timeout_for_skill("claude-code", pol) == 12.0
        q, t = guard_call(
            endpoint="http://127.0.0.1:8012/",
            skill="claude-code",
            query="x" * 100,
            policy=pol,
        )
        assert len(q) == 10
        assert t == 12.0


def test_output_clamp():
    with patch.dict(
        os.environ,
        {"AGENT_SANDBOX": "1", "AGENT_MAX_OUTPUT_CHARS": "5"},
        clear=False,
    ):
        out = clamp_output("hello world")
        assert out is not None
        assert out.startswith("hello")
        assert "truncated" in out


def test_normalize_keeps_docker_dns_when_runtime_docker():
    with patch.dict(os.environ, {"AOP_RUNTIME": "docker"}, clear=False):
        assert normalize_endpoint("http://claude-code:8011/") == "http://claude-code:8011/"


def test_normalize_maps_to_localhost_on_host():
    with patch.dict(os.environ, {"AOP_RUNTIME": "host"}, clear=False):
        assert normalize_endpoint("http://claude-code:8011/") == "http://127.0.0.1:8020/agents/claude-code/"
        assert normalize_endpoint("http://rag-agent:8002/") == "http://rag-agent:8002/"
