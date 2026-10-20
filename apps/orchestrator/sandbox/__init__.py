"""Agent execution sandbox policy (Phase 22).

Control-plane guardrails before A2A calls:
- skill allow/deny lists
- endpoint host allowlist + optional SSRF blocklist
- input/output size caps
- per-skill timeout overrides
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from urllib.parse import urlparse


class SandboxViolation(PermissionError):
    """Raised when an agent call violates sandbox policy."""


_DEFAULT_ALLOWED_HOSTS = (
    "127.0.0.1",
    "localhost",
    "claude-code",
    "deepseek-harness",
    "pi",
    "openclaw",
    "hermes",
    "harness-agent",
    "host.docker.internal",
)

# Cloud metadata / link-local — blocked when AGENT_SANDBOX_STRICT=1
_BLOCKED_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fd00:ec2::/32"),  # AWS IMDS v2 IPv6 (best-effort)
)


def sandbox_enabled() -> bool:
    return os.getenv("AGENT_SANDBOX", "1").lower() not in {"0", "false", "no", "off"}


def sandbox_strict() -> bool:
    return os.getenv("AGENT_SANDBOX_STRICT", "0").lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SandboxPolicy:
    enabled: bool
    strict: bool
    allowed_hosts: frozenset[str]
    denied_skills: frozenset[str]
    allowed_skills: frozenset[str] | None  # None = all except denied
    max_input_chars: int
    max_output_chars: int
    default_timeout: float
    skill_timeouts: dict[str, float]

    @classmethod
    def from_env(cls) -> SandboxPolicy:
        hosts_raw = os.getenv("AGENT_ENDPOINT_ALLOWLIST", "")
        hosts = {h.strip().lower() for h in hosts_raw.split(",") if h.strip()}
        if not hosts:
            hosts = {h.lower() for h in _DEFAULT_ALLOWED_HOSTS}

        deny = {
            s.strip()
            for s in os.getenv("AGENT_SKILL_DENYLIST", "").split(",")
            if s.strip()
        }
        allow_raw = os.getenv("AGENT_SKILL_ALLOWLIST", "").strip()
        allow: frozenset[str] | None
        if allow_raw:
            allow = frozenset(s.strip() for s in allow_raw.split(",") if s.strip())
        else:
            allow = None

        timeouts: dict[str, float] = {
            "claude-code": 480.0,
            "deepseek-harness": 480.0,
            "pi": 480.0,
            "openclaw": 480.0,
            "hermes": 480.0,
        }
        for part in os.getenv("AGENT_SKILL_TIMEOUTS", "").split(","):
            part = part.strip()
            if not part or ":" not in part:
                continue
            skill, val = part.split(":", 1)
            try:
                timeouts[skill.strip()] = float(val.strip())
            except ValueError:
                continue

        return cls(
            enabled=sandbox_enabled(),
            strict=sandbox_strict(),
            allowed_hosts=frozenset(hosts),
            denied_skills=frozenset(deny),
            allowed_skills=allow,
            max_input_chars=int(os.getenv("AGENT_MAX_INPUT_CHARS", "50000")),
            max_output_chars=int(os.getenv("AGENT_MAX_OUTPUT_CHARS", "200000")),
            default_timeout=float(os.getenv("A2A_TIMEOUT", "120")),
            skill_timeouts=timeouts,
        )


def _host_allowed(hostname: str, policy: SandboxPolicy) -> bool:
    host = (hostname or "").lower().strip("[]")
    if not host:
        return False
    if host in policy.allowed_hosts:
        return True
    # Allow *.local / explicit suffix patterns ending with *
    for pattern in policy.allowed_hosts:
        if pattern.startswith("*.") and host.endswith(pattern[1:]):
            return True
    return False


def _is_blocked_ip(hostname: str) -> bool:
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    if policy_block_private() and (ip.is_private or ip.is_loopback or ip.is_link_local):
        # loopback allowed via allowlist normally; strict private block is opt-in
        if ip.is_loopback:
            return False
        return True
    for net in _BLOCKED_NETWORKS:
        if ip in net:
            return True
    return False


def policy_block_private() -> bool:
    """When strict, also refuse non-allowlisted private IPs (SSRF hardening)."""
    return sandbox_strict()


def check_skill(skill: str | None, policy: SandboxPolicy | None = None) -> None:
    pol = policy or SandboxPolicy.from_env()
    if not pol.enabled:
        return
    skill = (skill or "").strip()
    if not skill:
        raise SandboxViolation("skill is required under sandbox")
    if skill in pol.denied_skills:
        raise SandboxViolation(f"skill denied by sandbox: {skill}")
    if pol.allowed_skills is not None and skill not in pol.allowed_skills:
        raise SandboxViolation(f"skill not in allowlist: {skill}")


def check_endpoint(url: str, policy: SandboxPolicy | None = None) -> None:
    pol = policy or SandboxPolicy.from_env()
    if not pol.enabled:
        return
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise SandboxViolation(f"endpoint scheme not allowed: {scheme or '(empty)'}")
    host = (parsed.hostname or "").lower()
    if not _host_allowed(host, pol):
        raise SandboxViolation(f"endpoint host not allowlisted: {host}")
    if pol.strict and _is_blocked_ip(host):
        raise SandboxViolation(f"endpoint IP blocked by strict sandbox: {host}")
    # Block obvious metadata hostnames
    if pol.strict and host in {"metadata.google.internal", "metadata"}:
        raise SandboxViolation(f"endpoint host blocked: {host}")


def clamp_input(text: str, policy: SandboxPolicy | None = None) -> str:
    pol = policy or SandboxPolicy.from_env()
    raw = text or ""
    if not pol.enabled:
        return raw
    if len(raw) > pol.max_input_chars:
        return raw[: pol.max_input_chars]
    return raw


def clamp_output(text: str | None, policy: SandboxPolicy | None = None) -> str | None:
    if text is None:
        return None
    pol = policy or SandboxPolicy.from_env()
    if not pol.enabled:
        return text
    if len(text) > pol.max_output_chars:
        return text[: pol.max_output_chars] + "\n…[truncated by sandbox]"
    return text


def timeout_for_skill(skill: str | None, policy: SandboxPolicy | None = None) -> float:
    pol = policy or SandboxPolicy.from_env()
    skill = (skill or "").strip()
    if skill and skill in pol.skill_timeouts:
        return pol.skill_timeouts[skill]
    return pol.default_timeout


def guard_call(
    *,
    endpoint: str,
    skill: str | None,
    query: str,
    policy: SandboxPolicy | None = None,
) -> tuple[str, float]:
    """Validate and return (clamped_query, timeout_seconds)."""
    from sandbox.profiles import check_high_risk_endpoint
    from egress import EgressDenied, EgressService

    pol = policy or SandboxPolicy.from_env()
    check_skill(skill, pol)
    check_endpoint(endpoint, pol)
    check_high_risk_endpoint(skill, endpoint)
    try:
        EgressService().assert_query_allowed(query, skill=skill)
    except EgressDenied as exc:
        raise SandboxViolation(f"egress: {exc}") from exc
    return clamp_input(query, pol), timeout_for_skill(skill, pol)
