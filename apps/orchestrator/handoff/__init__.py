"""Structured agent/node handoff payloads (LangGraph collaboration semantics).

Handoff is a small structured object attached to a completed node's ``output_json``.
Downstream ``_compose_query`` prefers ``artifact_ids`` over dumping full upstream
prose; the collaboration graph can show ``reason`` on edges.

Schema (stable keys):
  from, to, reason, artifact_ids, confidence
Optional: agent_key, agent_id, node_key
"""

from __future__ import annotations

from typing import Any

HANDOFF_SCHEMA_VERSION = 1

# Soft cap per upstream text chunk when composing downstream queries
DEFAULT_UPSTREAM_CHAR_BUDGET = 4000
# Soft cap for the whole upstream section (all deps combined)
DEFAULT_TOTAL_UPSTREAM_BUDGET = 8000
# When a slice must shrink hard, keep at least this many body chars
MIN_SLICE_BODY_CHARS = 240


def build_handoff(
    *,
    from_node: str,
    to_nodes: list[str] | None = None,
    reason: str | None = None,
    artifact_ids: list[str] | None = None,
    confidence: float | None = None,
    agent_key: str | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Build a handoff dict suitable for persistence in ``output_json``."""
    arts = [str(a) for a in (artifact_ids or []) if a]
    conf = 0.5 if confidence is None else max(0.0, min(1.0, float(confidence)))
    to_list = [str(t) for t in (to_nodes or []) if t]
    default_reason = (
        f"Node {from_node} completed"
        + (f" ({agent_key})" if agent_key else "")
        + (f"; hand off to {', '.join(to_list)}" if to_list else "")
    )
    return {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "from": str(from_node),
        "to": to_list,
        "reason": (reason or default_reason).strip(),
        "artifact_ids": arts,
        "confidence": conf,
        "agent_key": agent_key,
        "agent_id": agent_id,
        "node_key": str(from_node),
    }


def parse_handoff(raw: Any) -> dict[str, Any] | None:
    """Normalize a handoff blob from ``output_json``; return None if absent/invalid."""
    if not isinstance(raw, dict):
        return None
    src = raw.get("from") or raw.get("node_key")
    if not src:
        return None
    arts = raw.get("artifact_ids") or raw.get("artifacts") or []
    if not isinstance(arts, list):
        arts = []
    to_raw = raw.get("to") or []
    if isinstance(to_raw, str):
        to_list = [to_raw] if to_raw else []
    elif isinstance(to_raw, list):
        to_list = [str(t) for t in to_raw if t]
    else:
        to_list = []
    conf = raw.get("confidence")
    try:
        conf_f = float(conf) if conf is not None else 0.5
    except (TypeError, ValueError):
        conf_f = 0.5
    return {
        "schema_version": int(raw.get("schema_version") or HANDOFF_SCHEMA_VERSION),
        "from": str(src),
        "to": to_list,
        "reason": str(raw.get("reason") or "").strip(),
        "artifact_ids": [str(a) for a in arts if a],
        "confidence": max(0.0, min(1.0, conf_f)),
        "agent_key": raw.get("agent_key"),
        "agent_id": raw.get("agent_id"),
        "node_key": str(raw.get("node_key") or src),
    }


def artifact_uris_from_refs(refs: list[Any]) -> list[str]:
    """Extract durable artifact URIs from ArtifactRef-like objects/dicts (skip meta)."""
    out: list[str] = []
    for r in refs or []:
        if hasattr(r, "name") and hasattr(r, "uri"):
            name, uri = r.name, r.uri
        elif isinstance(r, dict):
            name, uri = r.get("name"), r.get("uri")
        else:
            continue
        if not uri or name == "meta.json":
            continue
        out.append(str(uri))
    return out


def plan_dependents(plan_json: Any, node_key: str) -> list[str]:
    """Return plan node ids that list ``node_key`` in ``depends_on``."""
    if isinstance(plan_json, str):
        import json

        try:
            plan_json = json.loads(plan_json)
        except Exception:
            return []
    if not isinstance(plan_json, dict):
        return []
    deps: list[str] = []
    for n in plan_json.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id") or "")
        depends = [str(d) for d in (n.get("depends_on") or [])]
        if node_key in depends and nid:
            deps.append(nid)
    return deps


def plan_dependencies(plan_json: Any, node_key: str) -> list[str]:
    """Return ``depends_on`` for ``node_key`` from a plan."""
    if isinstance(plan_json, str):
        import json

        try:
            plan_json = json.loads(plan_json)
        except Exception:
            return []
    if not isinstance(plan_json, dict):
        return []
    for n in plan_json.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        if str(n.get("id") or "") == node_key:
            return [str(d) for d in (n.get("depends_on") or [])]
    return []


def select_upstream_nodes(
    *,
    nodes: list[dict[str, Any]],
    current_node_key: str,
    plan_json: Any,
) -> list[dict[str, Any]]:
    """Pick success upstream nodes for query composition (dependency-aware)."""
    deps = set(plan_dependencies(plan_json, current_node_key))
    selected: list[dict[str, Any]] = []
    for n in nodes or []:
        nid = str(n.get("id") or n.get("node_key") or "")
        if not nid or nid == current_node_key:
            continue
        if n.get("status") != "success":
            continue
        if deps and nid not in deps:
            continue
        # Roots (empty deps): no upstream injection
        if not deps:
            continue
        selected.append(n)
    return selected


def truncate_text(text: str, budget: int = DEFAULT_UPSTREAM_CHAR_BUDGET) -> str:
    if budget <= 0 or len(text) <= budget:
        return text
    return text[: max(0, budget - 1)].rstrip() + "…"


def allocate_slice_budgets(
    n: int,
    *,
    total: int = DEFAULT_TOTAL_UPSTREAM_BUDGET,
    per_cap: int = DEFAULT_UPSTREAM_CHAR_BUDGET,
) -> list[int]:
    """Split a total char budget across *n* upstream slices (fair + capped)."""
    if n <= 0:
        return []
    total = max(0, int(total))
    per_cap = max(1, int(per_cap))
    fair = max(MIN_SLICE_BODY_CHARS, total // n) if total else 0
    each = min(per_cap, fair) if fair else 0
    return [each] * n


def prune_upstream_slices(
    slices: list[dict[str, Any]],
    *,
    total_budget: int = DEFAULT_TOTAL_UPSTREAM_BUDGET,
    per_cap: int = DEFAULT_UPSTREAM_CHAR_BUDGET,
) -> list[dict[str, Any]]:
    """Reducer-style prune: keep dep-scoped slices, shrink bodies to fit budget.

    Each slice dict expects:
      node_key, agent_key, reason, confidence, artifact_ids, body, human (optional)

    Prefer high-confidence handoffs; when body would exceed allocation, fall back
    to a short preview + artifact URI list (overwrite-safe summary, not full transcript).
    """
    if not slices:
        return []
    # Stable order: higher confidence first, then original index
    indexed = list(enumerate(slices))
    indexed.sort(
        key=lambda it: (
            -(float(it[1].get("confidence") or 0.5)),
            it[0],
        )
    )
    budgets = allocate_slice_budgets(len(indexed), total=total_budget, per_cap=per_cap)
    out: list[dict[str, Any] | None] = [None] * len(slices)
    used = 0
    for rank, (orig_i, sl) in enumerate(indexed):
        remaining = max(0, total_budget - used)
        alloc = min(budgets[rank], remaining) if remaining else 0
        body = str(sl.get("body") or "")
        arts = [str(a) for a in (sl.get("artifact_ids") or []) if a]
        human = str(sl.get("human") or "").strip()
        reason = str(sl.get("reason") or "").strip()
        conf = sl.get("confidence")
        pruned = dict(sl)
        pruned["pruned"] = False
        if alloc <= 0:
            # Still emit a stub so the dependent knows the upstream exists
            preview = truncate_text(body, MIN_SLICE_BODY_CHARS) if body else ""
            art_line = (", ".join(arts[:4]) + ("…" if len(arts) > 4 else "")) if arts else ""
            stub_parts = []
            if preview:
                stub_parts.append(preview)
            if art_line:
                stub_parts.append(f"[artifacts: {art_line}]")
            pruned["body"] = "\n".join(stub_parts) or "(upstream omitted; budget exhausted)"
            pruned["human"] = truncate_text(human, 120) if human else ""
            pruned["pruned"] = True
            out[orig_i] = pruned
            continue

        # Header overhead (approx) is paid from alloc
        header_tax = len(reason) + 40
        body_budget = max(MIN_SLICE_BODY_CHARS, alloc - header_tax)
        if len(body) > body_budget:
            if arts:
                art_line = ", ".join(arts[:6]) + ("…" if len(arts) > 6 else "")
                preview = truncate_text(body, max(MIN_SLICE_BODY_CHARS, body_budget // 2))
                pruned["body"] = f"{preview}\n[artifacts: {art_line}]"
            else:
                pruned["body"] = truncate_text(body, body_budget)
            pruned["pruned"] = True
        else:
            pruned["body"] = body

        if human:
            human_budget = max(80, min(400, remaining // 4))
            pruned["human"] = truncate_text(human, human_budget)
        else:
            pruned["human"] = ""

        if isinstance(conf, (int, float)):
            pruned["confidence"] = float(conf)
        out[orig_i] = pruned
        used += len(pruned.get("body") or "") + len(pruned.get("human") or "") + header_tax

    return [s for s in out if s is not None]
