"""Goal decomposition into ordered hops. Each hop is an agent_key."""

from __future__ import annotations

import re

from .dag import PlanNode

_SPLIT_RE = re.compile(
    r"(?:"
    r"\d+[\.\)、]\s*"  # 1. / 1) / 1、
    r"|首先|然后|接着|其次|最后|再者"
    r"|先\s*|再\s*|然后\s*"
    r"|\bthen\b|\bafter that\b|\band then\b"
    r"|；|;|，|,|\n+"
    r")",
    re.IGNORECASE,
)


def split_goal_fragments(goal: str) -> list[str]:
    text = (goal or "").strip()
    if not text:
        return []
    parts = [p.strip(" \t\r\n-—·•") for p in _SPLIT_RE.split(text)]
    return [p for p in parts if len(p) >= 2]


def agent_for_fragment(fragment: str, available: set[str]) -> str | None:
    """Match a registered agent_key named in the fragment."""
    low = fragment.lower()
    hits = [k for k in available if k and k.lower() in low]
    if not hits:
        return None
    hits.sort(key=lambda k: (-len(k), k))
    return hits[0]


def decompose_to_nodes(
    goal: str,
    available: set[str],
    *,
    hitl: set[str],
    max_nodes: int = 6,
) -> list[PlanNode]:
    """Linear DAG when ≥2 fragments name distinct registered agents."""
    frags = split_goal_fragments(goal)
    if len(frags) < 2:
        return []

    steps: list[tuple[str, str]] = []
    used: list[str] = []
    for frag in frags:
        agent_key = agent_for_fragment(frag, available)
        if not agent_key:
            continue
        if used and used[-1] == agent_key:
            continue
        node_id = agent_key
        if node_id in {s[0] for s in steps}:
            node_id = f"{agent_key}-{len(steps) + 1}"
        steps.append((node_id, agent_key))
        used.append(agent_key)
        if len(steps) >= max_nodes:
            break

    if len(steps) < 2:
        return []

    nodes: list[PlanNode] = []
    prev: str | None = None
    for node_id, agent_key in steps:
        deps = [prev] if prev else []
        nodes.append(
            PlanNode(
                id=node_id,
                agent_key=agent_key,
                depends_on=deps,
                requires_approval=agent_key in hitl,
            )
        )
        prev = node_id
    return nodes
