"""Inject platform identity / owner into harness system prompts."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

IDENTITY_LABELS: dict[str, str] = {
    "assistant": "助理",
    "manager": "主管",
    "employee": "员工",
    "student": "学生",
    "support": "客服",
}

_DEFAULT_GATEWAY = "http://127.0.0.1:8080"
_CACHE_TTL_S = 30.0
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def identity_label(code: str | None) -> str:
    c = (code or "").strip().lower() or "assistant"
    return IDENTITY_LABELS.get(c, c or "助理")


def format_platform_role_block(
    *,
    identity: str | None = None,
    owner_display_name: str | None = None,
    owner_email: str | None = None,
    owner_user_id: str | None = None,
) -> str:
    """Render the bilingual role footer appended to system prompts."""
    code = (identity or "").strip().lower() or "assistant"
    label = identity_label(code)
    owner = (owner_display_name or "").strip()
    email = (owner_email or "").strip()
    if owner and email:
        owner_line = f"{owner} ({email})"
    elif owner:
        owner_line = owner
    elif email:
        owner_line = email
    elif (owner_user_id or "").strip():
        owner_line = owner_user_id.strip()
    else:
        owner_line = "未绑定"

    return (
        "## Platform assignment（平台身份与所有者）\n"
        f"- Identity（身份）: {label} / `{code}`\n"
        f"- Owner（所有者）: {owner_line}\n"
        "Act according to this business identity when collaborating with other agents; "
        "the owner is the human accountable for this agent."
    )


def enrich_system_prompt(
    base: str,
    *,
    identity: str | None = None,
    owner_display_name: str | None = None,
    owner_email: str | None = None,
    owner_user_id: str | None = None,
) -> str:
    block = format_platform_role_block(
        identity=identity,
        owner_display_name=owner_display_name,
        owner_email=owner_email,
        owner_user_id=owner_user_id,
    )
    base = (base or "").rstrip()
    # Avoid duplicating if already injected.
    if "Platform assignment（平台身份与所有者）" in base:
        return base
    if not base:
        return block
    return f"{base}\n\n---\n\n{block}"


def _gateway_base() -> str:
    return (
        os.getenv("A2A_OS_URL")
        or os.getenv("GATEWAY_URL")
        or _DEFAULT_GATEWAY
    ).rstrip("/")


def _http_get_json(url: str, *, timeout: float = 2.5) -> dict[str, Any] | None:
    req = urllib.request.Request(url, method="GET")
    key = (
        os.getenv("A2A_OS_API_KEY")
        or os.getenv("GATEWAY_API_KEY")
        or os.getenv("AOP_API_KEY")
        or ""
    ).strip()
    if key:
        req.add_header("Authorization", f"Bearer {key}")
        req.add_header("X-API-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        logger.debug("platform role fetch failed %s: %s", url, exc)
        return None


def fetch_agent_platform_role(agent_ref: str) -> dict[str, Any] | None:
    """Lookup identity/owner from Gateway registry (short TTL cache)."""
    ref = (agent_ref or "").strip()
    if not ref:
        return None
    now = time.time()
    hit = _cache.get(ref.lower())
    if hit and now - hit[0] < _CACHE_TTL_S:
        return dict(hit[1])

    base = _gateway_base()
    data = _http_get_json(f"{base}/v1/agents/{urllib.parse.quote(ref, safe='')}")
    if data is None:
        # Fallback: list and match key/id/name.
        listing = _http_get_json(f"{base}/v1/agents")
        agents = (listing or {}).get("agents") if isinstance(listing, dict) else None
        if isinstance(agents, list):
            needle = ref.lower()
            for a in agents:
                if not isinstance(a, dict):
                    continue
                for field in ("agent_id", "agent_key", "name"):
                    if str(a.get(field) or "").strip().lower() == needle:
                        data = a
                        break
                if data is not None:
                    break

    if not isinstance(data, dict):
        return None

    role = {
        "identity": str(data.get("identity") or "assistant"),
        "owner_user_id": str(data.get("owner_user_id") or ""),
        "owner_email": str(data.get("owner_email") or ""),
        "owner_display_name": str(data.get("owner_display_name") or ""),
    }
    _cache[ref.lower()] = (now, role)
    return dict(role)


def role_from_message_metadata(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """Optional overrides passed by orchestrator / callers."""
    if not isinstance(meta, dict):
        return None
    identity = meta.get("agentIdentity") or meta.get("agent_identity") or meta.get("identity")
    owner_email = meta.get("agentOwnerEmail") or meta.get("owner_email")
    owner_name = meta.get("agentOwnerName") or meta.get("owner_display_name")
    owner_id = meta.get("agentOwnerUserId") or meta.get("owner_user_id")
    if not any([identity, owner_email, owner_name, owner_id]):
        return None
    return {
        "identity": str(identity or "assistant"),
        "owner_email": str(owner_email or ""),
        "owner_display_name": str(owner_name or ""),
        "owner_user_id": str(owner_id or ""),
    }


def resolve_platform_role(
    agent_id: str,
    *,
    message: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve role: message metadata wins, else Gateway registry, else defaults."""
    meta: dict[str, Any] = {}
    if isinstance(params, dict) and isinstance(params.get("metadata"), dict):
        meta.update(params["metadata"])
    if isinstance(message, dict) and isinstance(message.get("metadata"), dict):
        meta.update(message["metadata"])

    from_meta = role_from_message_metadata(meta)
    if from_meta:
        return from_meta

    refs: list[str] = []
    for candidate in (
        agent_id,
        os.getenv("AGENT_ID"),
        os.getenv("HARNESS_PROFILE"),
        os.getenv("AGENT_DISPLAY_NAME"),
    ):
        c = (candidate or "").strip()
        if c and c not in refs:
            refs.append(c)

    for ref in refs:
        found = fetch_agent_platform_role(ref)
        if found:
            return found

    return {
        "identity": "assistant",
        "owner_user_id": "",
        "owner_email": "",
        "owner_display_name": "",
    }


def enrich_system_prompt_for_agent(
    base: str,
    agent_id: str,
    *,
    message: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> str:
    role = resolve_platform_role(agent_id, message=message, params=params)
    return enrich_system_prompt(base, **role)


__all__ = [
    "IDENTITY_LABELS",
    "enrich_system_prompt",
    "enrich_system_prompt_for_agent",
    "fetch_agent_platform_role",
    "format_platform_role_block",
    "identity_label",
    "resolve_platform_role",
    "role_from_message_metadata",
]
