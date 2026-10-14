"""A2A OS: runtime execution graph.

Records the Agent-to-Agent call edges that emerge at runtime when Agents
autonomously discover and invoke one another, and reconstructs the call graph
for a given ``root_task_id``.

IMPORTANT: this is a *runtime execution graph*, not a predefined workflow DAG.
Edges are appended as calls actually happen; nothing here declares in advance
that Agent A must call Agent B.
"""

from __future__ import annotations

from defaults import DEFAULT_TENANT_ID, database_url as resolve_database_url

import os
from datetime import datetime, timezone
from typing import Any

from db import connect

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _looks_like_uuid(value: str) -> bool:
    parts = value.split("-")
    return len(parts) == 5 and all(parts)

class RuntimeGraphService:
    """Persist and query the runtime Agent-to-Agent execution graph."""

    def __init__(
        self,
        database_url: str | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ):
        self.database_url = resolve_database_url(database_url)
        self.tenant_id = tenant_id

    def record_edge(
        self,
        *,
        caller_agent_id: str,
        target_agent_id: str,
        root_task_id: str | None = None,
        parent_task_id: str | None = None,
        task_id: str | None = None,
        correlation_id: str | None = None,
        skill: str | None = None,
        depth: int = 0,
        status: str = "submitted",
        tenant_id: str | None = None,
        delegation_id: str | None = None,
        attempt: int = 1,
        error: str | None = None,
    ) -> dict[str, Any]:
        sql = """
            INSERT INTO a2a_runtime_edges (
              root_task_id, parent_task_id, task_id, correlation_id,
              caller_agent_id, target_agent_id, skill, depth, status, tenant_id,
              delegation_id, attempt, started_at, error, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s, now())
            RETURNING id, root_task_id, parent_task_id, task_id, correlation_id,
                      caller_agent_id, target_agent_id, skill, depth, status,
                      tenant_id, delegation_id, attempt, started_at, finished_at,
                      duration_ms, error, created_at
        """
        params = (
            root_task_id,
            parent_task_id,
            task_id,
            correlation_id,
            caller_agent_id,
            target_agent_id,
            skill,
            int(depth or 0),
            status,
            tenant_id or self.tenant_id,
            delegation_id,
            int(attempt or 1),
            error,
        )
        with connect(self.database_url) as conn:
            row = conn.execute(sql, params).fetchone()
        return self._serialize(row)

    def update_edge_status(
        self,
        *,
        task_id: str | None = None,
        delegation_id: str | None = None,
        status: str,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        """Mark a runtime edge finished (Phase 3 live collaboration graph)."""
        if not task_id and not delegation_id:
            return None
        clauses = []
        params: list[Any] = []
        if task_id:
            clauses.append("task_id = %s")
            params.append(task_id)
        if delegation_id:
            clauses.append("delegation_id = %s")
            params.append(delegation_id)
        where = " AND ".join(clauses)
        sql = f"""
            UPDATE a2a_runtime_edges
               SET status = %s,
                   error = COALESCE(%s, error),
                   finished_at = now(),
                   duration_ms = CASE
                     WHEN started_at IS NOT NULL
                     THEN EXTRACT(EPOCH FROM (now() - started_at)) * 1000
                     ELSE duration_ms END,
                   updated_at = now()
             WHERE {where}
         RETURNING id, root_task_id, parent_task_id, task_id, correlation_id,
                   caller_agent_id, target_agent_id, skill, depth, status,
                   tenant_id, delegation_id, attempt, started_at, finished_at,
                   duration_ms, error, created_at
        """
        with connect(self.database_url) as conn:
            row = conn.execute(sql, [status, error, *params]).fetchone()
        return self._serialize(row) if row else None

    def list_edges(self, root_task_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        sql = """
            SELECT id, root_task_id, parent_task_id, task_id, correlation_id,
                   caller_agent_id, target_agent_id, skill, depth, status,
                   tenant_id, created_at
            FROM a2a_runtime_edges
            WHERE root_task_id = %s
            ORDER BY id ASC
            LIMIT %s
        """
        with connect(self.database_url) as conn:
            rows = conn.execute(sql, (root_task_id, max(1, min(limit, 2000)))).fetchall()
        return [self._serialize(r) for r in rows]

    def branch_lineage(self, root_task_id: str, task_id: str | None) -> dict[str, Any]:
        """Authoritative lineage for one branch, rebuilt from recorded edges.

        Walks ``parent_task_id`` links from ``task_id`` up to the root and returns
        the ordered chain of agents that handled this branch (matching
        ``CallContext.visited_agents``), the real depth, and per-agent visit
        counts. Limited revisits are countable (A→B→A → visits[A]=2); the OS
        uses this instead of trusting caller-supplied ``visited_agents``
        (anti-tamper, Phase 2).
        """
        edges = self.list_edges(root_task_id)
        by_task: dict[str, dict[str, Any]] = {}
        for e in edges:
            tid = e.get("task_id")
            if tid:
                by_task.setdefault(tid, e)

        # Walk leaf → root collecting handlers (edge targets). Reverse to get
        # root → leaf, then prepend the topmost caller so the chain mirrors
        # CallContext: [A, B, A] for a legal reverse call.
        handlers_rev: list[str] = []
        seen_tasks: set[str] = set()
        cursor = task_id
        depth = 0
        top_caller: str | None = None
        while cursor and cursor in by_task and cursor not in seen_tasks:
            seen_tasks.add(cursor)
            e = by_task[cursor]
            target = e.get("target_agent_id")
            if target:
                handlers_rev.append(str(target))
            depth = max(depth, int(e.get("depth") or 0))
            top_caller = str(e.get("caller_agent_id") or "") or top_caller
            cursor = e.get("parent_task_id")
        handlers_rev.reverse()
        if top_caller and (not handlers_rev or handlers_rev[0] != top_caller):
            chain = [top_caller] + handlers_rev
        else:
            chain = handlers_rev

        visits: dict[str, int] = {}
        for a in chain:
            visits[a] = visits.get(a, 0) + 1
        return {
            "root_task_id": root_task_id,
            "task_id": task_id,
            "agent_chain": chain,
            "depth": depth,
            "agent_visits": visits,
        }

    def _agent_directory(self) -> dict[str, dict[str, str]]:
        """Map agent_key and agent UUID → {id, key, name} for identity merge.

        Runtime edges historically mix ``agent_key`` (callers) with UUID
        (targets). Without a directory the collaboration graph shows the same
        logical agent as two disconnected nodes.
        """
        out: dict[str, dict[str, str]] = {}
        try:
            with connect(self.database_url) as conn:
                rows = conn.execute(
                    "SELECT id::text AS id, agent_key, name FROM agents"
                ).fetchall()
        except Exception:
            return out
        for row in rows or []:
            if isinstance(row, dict):
                aid = str(row.get("id") or "")
                key = str(row.get("agent_key") or "")
                name = str(row.get("name") or key or aid)
            else:
                aid = str(row[0] or "")
                key = str(row[1] or "")
                name = str(row[2] or key or aid)
            meta = {"id": aid, "key": key, "name": name}
            if aid:
                out[aid] = meta
            if key:
                out[key] = meta
        return out

    def _canonical_agent(
        self,
        raw: str | None,
        directory: dict[str, dict[str, str]],
    ) -> tuple[str, str, str]:
        """Return (canonical_id, agent_key, display_name)."""
        if not raw:
            return "", "", ""
        s = str(raw)
        meta = directory.get(s)
        if meta:
            return meta["id"] or s, meta.get("key") or "", meta.get("name") or s
        return s, s if not _looks_like_uuid(s) else "", s

    def collaboration_graph(self, root_task_id: str) -> dict[str, Any]:
        """Frontend-friendly collaboration graph (nodes + links + tree).

        Stabilizes the runtime execution graph into a schema suitable for
        React Flow / D3 without changing the underlying edge store.

        Agent identities are canonicalized (key ↔ UUID) so call chains such as
        ``search → analysis → rag`` render as one path through mid task nodes.
        """
        base = self.get_graph(root_task_id)
        edges = base.get("edges") or []
        directory = self._agent_directory()
        nodes: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []
        seen_agents: set[str] = set()
        seen_tasks: set[str] = set()
        for e in edges:
            caller_raw = e.get("caller_agent_id")
            target_raw = e.get("target_agent_id")
            caller_id, caller_key, caller_name = self._canonical_agent(caller_raw, directory)
            target_id, target_key, target_name = self._canonical_agent(target_raw, directory)

            for aid, key, name, role in (
                (caller_id, caller_key, caller_name, "caller"),
                (target_id, target_key, target_name, "target"),
            ):
                if not aid or aid in seen_agents:
                    continue
                seen_agents.add(aid)
                nodes.append({
                    "id": aid,
                    "type": "agent",
                    "label": name or key or aid,
                    "agent_key": key or None,
                    "role": role,
                })

            tid = e.get("task_id")
            if tid and str(tid) not in seen_tasks:
                seen_tasks.add(str(tid))
                nodes.append({
                    "id": f"task:{tid}",
                    "type": "task",
                    "label": e.get("skill") or str(tid),
                    "task_id": tid,
                    "status": e.get("status"),
                    "skill": e.get("skill"),
                    "depth": e.get("depth"),
                    "agent_id": target_id or e.get("target_agent_id"),
                })
            links.append({
                "id": f"edge:{e.get('id')}",
                "source": caller_id,
                "target": target_id,
                "task_id": tid,
                "parent_task_id": e.get("parent_task_id"),
                "skill": e.get("skill"),
                "depth": e.get("depth"),
                "status": e.get("status"),
                "correlation_id": e.get("correlation_id"),
                "created_at": e.get("created_at"),
            })
        return {
            "root_task_id": root_task_id,
            "kind": "collaboration_graph",
            "node_count": len(nodes),
            "link_count": len(links),
            "max_depth": base.get("max_depth", 0),
            "agents": base.get("agents") or [],
            "nodes": nodes,
            "links": links,
            "tree": base.get("tree") or [],
            "edges": edges,
        }

    def collaboration_from_plan(self, task_row: dict[str, Any]) -> dict[str, Any]:
        """Synthesize caller→task→target links from a plan DAG + assignments.

        Used when ``a2a_runtime_edges`` is empty (orchestrator scheduled the
        nodes itself). Upstream agents (or ``orchestrator``) call through a mid
        task node into the assigned target agent — matching the React Flow layout.
        """
        task_id = str(task_row.get("id") or task_row.get("task_id") or "")
        plan = task_row.get("plan_json") or {}
        if isinstance(plan, str):
            import json as _json
            try:
                plan = _json.loads(plan)
            except Exception:
                plan = {}
        plan_nodes = plan.get("nodes") or []
        exec_nodes = task_row.get("nodes") or []
        if isinstance(exec_nodes, str):
            import json as _json
            try:
                exec_nodes = _json.loads(exec_nodes)
            except Exception:
                exec_nodes = []

        assigned: dict[str, dict[str, Any]] = {}
        for n in exec_nodes:
            if not isinstance(n, dict):
                continue
            key = str(n.get("id") or n.get("node_key") or "")
            if not key:
                continue
            agent = n.get("agent_id") or n.get("assigned_agent_id")
            assigned[key] = {
                "agent_id": str(agent) if agent else "",
                "agent_key": n.get("agent_key"),
                "status": n.get("status"),
                "handoff": n.get("handoff"),
            }

        # Topological depth from depends_on
        deps_map: dict[str, list[str]] = {}
        skills: dict[str, str] = {}
        for pn in plan_nodes:
            if not isinstance(pn, dict):
                continue
            nid = str(pn.get("id") or "")
            if not nid:
                continue
            deps_map[nid] = [str(d) for d in (pn.get("depends_on") or [])]
            skills[nid] = str(
                pn.get("agent_key")
                or assigned.get(nid, {}).get("agent_key")
                or nid
            )

        depth_of: dict[str, int] = {}

        def _depth(nid: str, stack: set[str]) -> int:
            if nid in depth_of:
                return depth_of[nid]
            if nid in stack:
                return 1
            stack.add(nid)
            deps = deps_map.get(nid) or []
            d = 1 if not deps else 1 + max((_depth(x, stack) for x in deps), default=0)
            stack.discard(nid)
            depth_of[nid] = d
            return d

        for nid in deps_map:
            _depth(nid, set())

        directory = self._agent_directory()
        orch_id, _, orch_name = self._canonical_agent("orchestrator", directory)
        if not orch_id:
            orch_id, orch_name = "orchestrator", "Orchestrator"

        nodes: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []
        seen_agents: set[str] = set()
        seen_tasks: set[str] = set()

        def _add_agent(raw: str, role: str, label_hint: str | None = None) -> str:
            aid, key, name = self._canonical_agent(raw, directory)
            if not aid:
                return ""
            if aid not in seen_agents:
                seen_agents.add(aid)
                nodes.append({
                    "id": aid,
                    "type": "agent",
                    "label": label_hint or name or key or aid,
                    "agent_key": key or None,
                    "role": role,
                })
            return aid

        _add_agent(orch_id, "caller", orch_name)

        link_i = 0
        for nid, deps in deps_map.items():
            info = assigned.get(nid) or {}
            target_raw = info.get("agent_id") or ""
            if not target_raw:
                continue
            target_id = _add_agent(target_raw, "target")
            if not target_id:
                continue
            tid = f"{task_id}:{nid}" if task_id else nid
            task_node_id = f"task:{tid}"
            if tid not in seen_tasks:
                seen_tasks.add(tid)
                ho = info.get("handoff") if isinstance(info.get("handoff"), dict) else None
                art_ids: list[str] = []
                if ho and isinstance(ho.get("artifact_ids"), list):
                    art_ids = [str(a) for a in ho["artifact_ids"] if a]
                nodes.append({
                    "id": task_node_id,
                    "type": "task",
                    "label": skills.get(nid) or nid,
                    "task_id": tid,
                    "status": info.get("status"),
                    "agent_key": skills.get(nid) or info.get("agent_key"),
                    "depth": depth_of.get(nid, 1),
                    "agent_id": target_id,
                    "plan_node_id": nid,
                    "handoff": ho,
                    "artifact_ids": art_ids,
                    "handoff_reason": (str(ho["reason"]) if ho and ho.get("reason") else None),
                })

            callers: list[str] = []
            if not deps:
                callers = [orch_id]
            else:
                for dep in deps:
                    dep_agent = (assigned.get(dep) or {}).get("agent_id")
                    if dep_agent:
                        callers.append(_add_agent(str(dep_agent), "caller"))
                    else:
                        callers.append(orch_id)
            if not callers:
                callers = [orch_id]

            for caller in callers:
                if not caller:
                    continue
                link_i += 1
                # Only upstream handoff counts as "传递文件". Mid-task outputs stay on
                # the task node — never paint them onto orchestrator dispatch edges.
                reason = None
                pass_arts: list[str] = []
                pass_ho: dict[str, Any] | None = None
                is_dispatch = not deps or caller == orch_id
                if deps and not is_dispatch:
                    for dep in deps:
                        dep_info = assigned.get(dep) or {}
                        dep_agent = dep_info.get("agent_id")
                        if dep_agent and self._canonical_agent(str(dep_agent), directory)[0] == caller:
                            ho = dep_info.get("handoff")
                            if isinstance(ho, dict):
                                pass_ho = ho
                                if ho.get("reason"):
                                    reason = str(ho["reason"])
                                if isinstance(ho.get("artifact_ids"), list):
                                    pass_arts = [str(a) for a in ho["artifact_ids"] if a]
                                break
                links.append({
                    "id": f"plan-edge:{nid}:{link_i}",
                    "source": caller,
                    "target": target_id,
                    "task_id": tid,
                    "parent_task_id": None,
                    "skill": skills.get(nid) or info.get("agent_key"),
                    "reason": reason,
                    "artifact_ids": pass_arts,
                    "handoff": pass_ho,
                    "depth": depth_of.get(nid, 1),
                    "status": info.get("status") or "completed",
                    "correlation_id": task_id or None,
                    "created_at": task_row.get("created_at"),
                    "source_kind": "plan",
                    "kind": "handoff" if pass_arts or (reason and deps) else "dispatch",
                })

        max_depth = max(depth_of.values(), default=0)
        return {
            "root_task_id": task_id,
            "kind": "collaboration_graph",
            "source": "plan_synthesis",
            "node_count": len(nodes),
            "link_count": len(links),
            "max_depth": max_depth,
            "agents": sorted(seen_agents),
            "nodes": nodes,
            "links": links,
            "tree": [],
            "edges": [],
        }

    def root_stats(self, root_task_id: str) -> dict[str, Any]:
        """Authoritative aggregate call stats for a root task (DB-backed)."""
        edges = self.list_edges(root_task_id)
        visits: dict[str, int] = {}
        for e in edges:
            for a in (e.get("caller_agent_id"), e.get("target_agent_id")):
                if a:
                    visits[str(a)] = visits.get(str(a), 0) + 1
        return {
            "root_task_id": root_task_id,
            "call_count": len(edges),
            "max_depth": max((int(e.get("depth") or 0) for e in edges), default=0),
            "agent_visits": visits,
        }

    def get_graph(self, root_task_id: str) -> dict[str, Any]:
        """Reconstruct the runtime execution tree for a root task."""
        edges = self.list_edges(root_task_id)
        nodes: list[dict[str, Any]] = []
        by_task: dict[str, dict[str, Any]] = {}
        for e in edges:
            node = {
                "task_id": e.get("task_id"),
                "caller_agent_id": e.get("caller_agent_id"),
                "target_agent_id": e.get("target_agent_id"),
                "skill": e.get("skill"),
                "depth": e.get("depth"),
                "status": e.get("status"),
                "correlation_id": e.get("correlation_id"),
                "created_at": e.get("created_at"),
                "children": [],
            }
            nodes.append(node)
            tid = e.get("task_id")
            if tid:
                by_task.setdefault(tid, node)

        roots: list[dict[str, Any]] = []
        for e, node in zip(edges, nodes):
            parent = e.get("parent_task_id")
            parent_node = by_task.get(parent) if parent else None
            if parent_node is not None and parent_node is not node:
                parent_node["children"].append(node)
            else:
                roots.append(node)

        agents = sorted(
            {
                a
                for e in edges
                for a in (e.get("caller_agent_id"), e.get("target_agent_id"))
                if a
            }
        )
        max_depth = max((int(e.get("depth") or 0) for e in edges), default=0)
        return {
            "root_task_id": root_task_id,
            "kind": "runtime_execution_graph",
            "edge_count": len(edges),
            "max_depth": max_depth,
            "agents": agents,
            "edges": edges,
            "tree": roots,
        }

    @staticmethod
    def _serialize(row: Any) -> dict[str, Any]:
        d = dict(row)
        created = d.get("created_at")
        if isinstance(created, datetime):
            d["created_at"] = created.isoformat()
        return d

__all__ = ["RuntimeGraphService"]
