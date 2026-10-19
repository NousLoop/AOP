#!/usr/bin/env python3
"""Register aopd-hosted harness agents to Gateway (docker / host)."""

from __future__ import annotations

import os
import sys
import time

import httpx

GATEWAY = os.getenv("GATEWAY_URL", "http://gateway:8080").rstrip("/")
API_KEY = (
    os.getenv("GATEWAY_API_KEY")
    or os.getenv("AOP_API_KEY")
    or os.getenv("NEXT_PUBLIC_API_KEY")
    or ""
)

# Base for shared CLI A2A (aopd). Override when aopd is reachable under another host.
CLI_A2A_BASE = (
    os.getenv("AOP_CLI_A2A_BASE")
    or os.getenv("AOP_AGENT_AOPD_BASE")
    or "http://host.docker.internal:8020"
).rstrip("/")


def _agents() -> list[tuple[str, str, str]]:
    """(name, env_key, default_endpoint)."""
    try:
        from agent_runtime.harness.profiles import HARNESS_PROFILES

        out: list[tuple[str, str, str]] = []
        for p in HARNESS_PROFILES:
            name = p.plugin_id or p.key
            path = p.path or f"/agents/{name}"
            if not path.startswith("/"):
                path = f"/{path}"
            out.append((name, p.env_url_key, f"{CLI_A2A_BASE}{path}"))
        return out
    except ImportError:  # pragma: no cover
        return [
            ("claude-code", "AOP_AGENT_CLAUDE_CODE_URL", f"{CLI_A2A_BASE}/agents/claude-code"),
            (
                "deepseek-harness",
                "AOP_AGENT_DEEPSEEK_HARNESS_URL",
                f"{CLI_A2A_BASE}/agents/deepseek-harness",
            ),
            ("pi", "AOP_AGENT_PI_URL", f"{CLI_A2A_BASE}/agents/pi"),
            ("openclaw", "AOP_AGENT_OPENCLAW_URL", f"{CLI_A2A_BASE}/agents/openclaw"),
            ("hermes", "AOP_AGENT_HERMES_URL", f"{CLI_A2A_BASE}/agents/hermes"),
        ]


AGENTS = _agents()


def wait(url: str, timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=3.0)
            if r.status_code < 500:
                return
        except Exception:
            pass
        time.sleep(1.5)
    raise RuntimeError(f"timeout waiting for {url}")


def _headers() -> dict[str, str]:
    if not API_KEY:
        return {}
    return {"Authorization": f"Bearer {API_KEY}", "X-API-Key": API_KEY}


def main() -> int:
    print(f"gateway={GATEWAY} auth={'on' if API_KEY else 'off'} base={CLI_A2A_BASE}")
    wait(f"{GATEWAY}/health")
    for name, env_key, default in AGENTS:
        ep = (os.getenv(env_key) or default).rstrip("/")
        wait(f"{ep}/health")
        print(f"register {name} -> {ep}")
        for attempt in range(8):
            try:
                r = httpx.post(
                    f"{GATEWAY}/v1/agents/register",
                    json={"endpoint": ep},
                    headers=_headers(),
                    timeout=30,
                )
                print(f"  status={r.status_code} body={r.text[:200]}")
                if r.status_code < 300:
                    break
            except Exception as exc:  # noqa: BLE001
                print(f"  attempt {attempt+1} error: {exc}")
            time.sleep(2)
        else:
            print(f"FAILED register {name}")
            return 1
    listed = httpx.get(f"{GATEWAY}/v1/agents", headers=_headers(), timeout=15)
    print("agents:", listed.text[:800])
    print("REGISTER_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
