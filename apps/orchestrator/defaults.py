"""Shared orchestrator defaults (tenant id + local Postgres DSN).

Import from here instead of re-declaring the same UUID / DSN literals.
"""

from __future__ import annotations

import os

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_DATABASE_URL = "postgresql://aop:aop@127.0.0.1:5432/aop"

# Edge supervisors (aopd). Their HTTP is mgmt, not A2A work.
SUPERVISOR_KEY_PREFIXES = ("aop-node", "node-local")


def is_work_agent(key: str | None) -> bool:
    """True for A2A work agents. Supervisors are nodes, not agents."""
    k = (key or "").strip().lower()
    if not k:
        return False
    return not any(k.startswith(p) for p in SUPERVISOR_KEY_PREFIXES)


def database_url(explicit: str | None = None) -> str:
    """Resolve Postgres URL: explicit arg, else ``DATABASE_URL``, else local default."""
    if explicit:
        return explicit
    return os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
