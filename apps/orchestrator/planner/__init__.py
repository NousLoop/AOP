"""Planner: natural language goal → Task DAG (registered agent hops)."""

from __future__ import annotations

from defaults import (
    DEFAULT_TENANT_ID,
    database_url as resolve_database_url,
    is_work_agent,
)

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from db import connect

from .dag import DAGValidationError, PlanNode, TaskPlan, validate_plan
from .decompose import decompose_to_nodes
from .json_plan import extract_json_object, nodes_from_payload
from router import hitl_skills

_NO_SPAWN = "请独立完成本任务，不要再 spawn/委派同伴。"

_MULTI_AGENT_MARKERS = (
    "使用目前所有的agent",
    "用目前所有的agent",
    "使用目前所有的 agent",
    "用目前所有的 agent",
    "使用目前所有",
    "用目前所有",
    "目前所有的agent",
    "目前所有的 agent",
    "目前所有agent",
    "目前所有 agent",
    "所有的agent",
    "所有的 agent",
    "所有agent",
    "所有 agent",
    "全部agent",
    "全部 agent",
    "多智能体",
    "多 agent",
    "多agent",
    "三个agent",
    "三个 agent",
    "all agents",
    "all agent",
    "every agent",
    "use all",
    "所有智能体",
    "全部智能体",
)

@dataclass(frozen=True)
class RegisteredAgent:
    """One schedulable agent from the registry (not a hardcoded catalog)."""

    key: str
    name: str = ""
    priority: int = 100

    @property
    def label(self) -> str:
        return (self.name or self.key).strip() or self.key

def _env_on(name: str, *, default: str = "") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes"}

def _env_off(name: str, *, default: str = "1") -> bool:
    return os.getenv(name, default).lower() in {"0", "false", "no", "off"}

def coerce_registry(
    agents: Sequence[RegisteredAgent] | Iterable[str] | None,
) -> list[RegisteredAgent]:
    """Accept RegisteredAgent rows or bare keys (tests / callers)."""
    if agents is None:
        return []
    out: list[RegisteredAgent] = []
    for a in agents:
        if isinstance(a, RegisteredAgent):
            if is_work_agent(a.key):
                out.append(a)
        else:
            key = str(a).strip()
            if is_work_agent(key):
                out.append(RegisteredAgent(key=key, name=key))
    return out

def _alias_patterns(key: str, name: str = "") -> list[tuple[int, str]]:
    """(specificity, regex) derived from registered key/name — longest first."""
    keyed: list[tuple[int, str]] = []
    seen: set[str] = set()

    def add(pattern: str, weight: int) -> None:
        if pattern in seen:
            return
        seen.add(pattern)
        keyed.append((weight, pattern))

    key = (key or "").strip()
    name = (name or "").strip()
    if not key:
        return []

    def add_token(token: str, weight: int) -> None:
        """Long tokens may freestand; short ones need letter boundaries (pi ⊂ ping)."""
        esc = re.escape(token)
        if len(token) <= 2:
            add(rf"(?:使用|用|调用|让|请让)\s*{esc}(?![a-zA-Z])", weight + 4)
            add(rf"(?<![a-zA-Z]){esc}(?![a-zA-Z])", weight)
        else:
            add(esc, weight)

    add_token(key, len(key) + 10)
    spaced = key.replace("-", " ").replace("_", " ")
    if spaced != key:
        add_token(spaced, len(spaced) + 8)

    for part in re.split(r"[-_\s]+", key):
        part = part.strip()
        if len(part) >= 2 and part.lower() != key.lower():
            add_token(part, len(part))

    if name and name.lower() not in {key.lower(), spaced.lower()}:
        add(re.escape(name), len(name) + 6)
        name_space = re.sub(r"\s+", r"\\s+", re.escape(name))
        add(name_space, len(name) + 5)

    keyed.sort(key=lambda x: (-x[0], -len(x[1])))
    return keyed

def default_agent(available: set[str]) -> str | None:
    """Prefer DEFAULT_AGENT (claude-code) when ready; else the hosted kernel."""
    work = {k for k in available if is_work_agent(k)}
    if not work:
        return None
    preferred = (os.getenv("DEFAULT_AGENT") or "claude-code").strip()
    if preferred and preferred in work:
        return preferred
    return sorted(work)[0]

def mentioned_agents(
    goal: str,
    registry: Sequence[RegisteredAgent] | Iterable[str] | None,
) -> list[str]:
    """Registered agent keys named in ``goal`` (specificity order)."""
    agents = coerce_registry(registry)
    text = goal or ""
    # Build global pattern list: longer aliases win when overlapping.
    ranked: list[tuple[int, str, str]] = []  # weight, pattern, key
    for ag in agents:
        for weight, pattern in _alias_patterns(ag.key, ag.name):
            ranked.append((weight, pattern, ag.key))
    ranked.sort(key=lambda x: (-x[0], -len(x[1])))

    hits: list[str] = []
    seen: set[str] = set()
    for _w, pattern, key in ranked:
        if key in seen:
            continue
        if re.search(pattern, text, flags=re.IGNORECASE):
            seen.add(key)
            hits.append(key)
    return hits

def wants_all_agents(
    goal: str,
    registry: Sequence[RegisteredAgent] | Iterable[str] | None = None,
) -> bool:
    """True when the goal asks for every agent, or names ≥2 registered ones."""
    low = (goal or "").lower()
    if any(m.lower() in low for m in _MULTI_AGENT_MARKERS):
        return True
    if registry is None:
        return False
    return len(mentioned_agents(goal, registry)) >= 2

def requested_agent(
    goal: str,
    available: set[str] | None = None,
    registry: Sequence[RegisteredAgent] | Iterable[str] | None = None,
) -> str | None:
    """If the goal names exactly one registered agent, return its key."""
    text = (goal or "").strip()
    reg = coerce_registry(registry if registry is not None else available)
    if not text or wants_all_agents(text, reg):
        return None
    hits = mentioned_agents(text, reg)
    if len(hits) != 1:
        return None
    key = hits[0]
    if available is not None and key not in available:
        return None
    return key

def extract_work_goal(
    goal: str,
    registry: Sequence[RegisteredAgent] | Iterable[str] | None = None,
) -> str:
    """Strip orchestration / agent-name routing; keep the work request."""
    text = (goal or "").strip()
    if not text:
        return text

    cleaned = text
    for marker in sorted(_MULTI_AGENT_MARKERS, key=len, reverse=True):
        cleaned = re.sub(re.escape(marker), " ", cleaned, flags=re.IGNORECASE)

    cleaned = re.sub(
        r"(的)?(目前)?(全部|所有)?\s*agents?\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"(的)?(目前)?(全部|所有)?\s*智能体", " ", cleaned)

    for ag in coerce_registry(registry):
        for _w, pattern in _alias_patterns(ag.key, ag.name):
            cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)

    cleaned = re.sub(r"[，,、；;]\s*[，,、；;]+", "，", cleaned)
    cleaned = re.sub(r"^(请)?(使用|用|调用|让|请让)\s*", "", cleaned.strip())
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = cleaned.strip(" ，,、；;：:")
    return cleaned or text

def agent_instruction(
    agent_key: str,
    work_goal: str,
    *,
    parallel: bool = False,
    label: str | None = None,
) -> str:
    """Generic brief — no hardcoded domain angles."""
    work = (work_goal or "").strip()
    who = (label or agent_key).strip() or agent_key
    if parallel:
        head = f"你是 {who}。平台已并行调度其他 agent，{_NO_SPAWN}"
    else:
        head = f"你是 {who}。{_NO_SPAWN}"
    return f"{head}\n\n任务：\n{work}"

def _node_id_for(key: str, used: set[str]) -> str:
    base = re.sub(r"[^a-zA-Z0-9]+", "_", key).strip("_")[:24] or "agent"
    nid = base
    n = 2
    while nid in used:
        nid = f"{base}{n}"
        n += 1
    used.add(nid)
    return nid

def parallel_harness_nodes(
    available: set[str] | Sequence[RegisteredAgent] | Sequence[str],
    *,
    hitl: set[str],
    work_goal: str = "",
    labels: dict[str, str] | None = None,
) -> list[PlanNode]:
    """One independent DAG node per registered work agent (fan-out)."""
    items = list(available) if available is not None else []
    if items and isinstance(items[0], RegisteredAgent):
        agents = coerce_registry(items)  # type: ignore[arg-type]
        keys = [a.key for a in agents]
        label_map = {a.key: a.label for a in agents}
    else:
        keys = sorted(
            {str(k).strip() for k in items if is_work_agent(str(k))}
        )
        label_map = {k: k for k in keys}
    if labels:
        label_map.update(labels)

    if not keys:
        return []

    work = (work_goal or "").strip()
    used: set[str] = set()
    nodes: list[PlanNode] = []
    for key in keys:
        nodes.append(
            PlanNode(
                id=_node_id_for(key, used),
                agent_key=key,
                depends_on=[],
                requires_approval=key in hitl,
                instruction=(
                    agent_instruction(
                        key, work, parallel=True, label=label_map.get(key)
                    )
                    if work
                    else None
                ),
            )
        )
    return nodes

def probe_agent_ready(endpoint: str, *, timeout_s: float = 0.4) -> bool:
    """GET ``/health`` — treat missing flag as ready (older agents)."""
    if _env_on("PLANNER_SKIP_READY_PROBE"):
        return True
    url = (endpoint or "").rstrip("/") + "/health"
    try:
        import json
        import urllib.request

        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            raw = resp.read()
        if not raw:
            return True
        data = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(data, dict):
            return True
        if "runner_ready" in data:
            return bool(data.get("runner_ready"))
        return True
    except Exception:  # noqa: BLE001
        return False


def probe_keys_ready(
    endpoints: dict[str, str],
    keys: Iterable[str],
    *,
    timeout_s: float = 0.8,
) -> list[str]:
    """Probe ``/health`` in parallel so plan() does not serial-wait 10 agents."""
    wanted = [k for k in sorted({str(k).strip() for k in keys if k}) if endpoints.get(k)]
    if not wanted:
        return []
    if len(wanted) == 1:
        k = wanted[0]
        return [k] if probe_agent_ready(endpoints[k], timeout_s=timeout_s) else []

    from concurrent.futures import ThreadPoolExecutor, as_completed

    ready: list[str] = []
    with ThreadPoolExecutor(max_workers=min(16, len(wanted))) as pool:
        futs = {
            pool.submit(probe_agent_ready, endpoints[k], timeout_s=timeout_s): k
            for k in wanted
        }
        for fut in as_completed(futs):
            key = futs[fut]
            try:
                if fut.result():
                    ready.append(key)
            except Exception:  # noqa: BLE001
                continue
    return sorted(ready)

@dataclass
class PlannerResult:
    plan: TaskPlan
    method: str
    available_agent_keys: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "available_agent_keys": self.available_agent_keys,
            "plan": self.plan.to_dict(),
        }

class Planner:
    def __init__(
        self,
        database_url: str | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ):
        self.database_url = resolve_database_url(database_url)
        self.tenant_id = tenant_id

    def plan(self, goal: str, *, title: str | None = None) -> PlannerResult:
        goal = (goal or "").strip()
        if not goal:
            raise ValueError("goal is required")

        registry = self.list_registered_agents()
        available = {a.key for a in registry}
        labels = {a.key: a.label for a in registry}
        hitl = set(hitl_skills())
        method = "heuristic"
        nodes: list[PlanNode] = []

        # 1) Fan-out to ready registered agents.
        if wants_all_agents(goal, registry):
            ready_keys = set(self.list_ready_agents(available))
            pool = [a for a in registry if a.key in (ready_keys or available)]
            multi = parallel_harness_nodes(
                pool or registry,
                hitl=hitl,
                work_goal=extract_work_goal(goal, registry),
                labels=labels,
            )
            if multi:
                nodes = multi
                method = "heuristic_multi"

        # 2) Opt-in multi-step: fragments that name distinct agent_keys.
        if not nodes and not _env_off("PLANNER_V2") and _env_on("PLANNER_MULTI_STEP"):
            stepped = decompose_to_nodes(goal, available, hitl=hitl)
            if stepped:
                nodes = stepped
                method = "heuristic_steps"

        # 3) Single-hop: named registered agent, else default among registry.
        if not nodes:
            nodes = self._heuristic_nodes(goal, available, registry=registry, labels=labels)
            method = "heuristic"

        # 4) Optional LLM — skip forced fan-out.
        if method != "heuristic_multi" and _env_on("PLANNER_LLM"):
            llm_nodes, repaired = self._try_llm_nodes(goal, available, hitl=hitl)
            if llm_nodes:
                try:
                    trial = TaskPlan(
                        title=title or _short_title(goal),
                        goal=goal,
                        nodes=llm_nodes,
                    )
                    validate_plan(trial, available_agent_keys=available)
                    nodes = llm_nodes
                    method = "llm_repaired" if repaired else "llm"
                except DAGValidationError:
                    pass

        # 5) Last-resort fallback.
        if not nodes:
            agent = default_agent(available)
            if agent:
                nodes = [PlanNode(id="run", agent_key=agent)]
            elif available:
                nodes = [PlanNode(id="step1", agent_key=sorted(available)[0])]
            else:
                raise ValueError("no online agents registered; cannot plan")
            method = "fallback"

        plan = TaskPlan(title=title or _short_title(goal), goal=goal, nodes=nodes)
        validate_plan(plan, available_agent_keys=available)
        return PlannerResult(plan=plan, method=method, available_agent_keys=sorted(available))

    def list_registered_agents(self) -> list[RegisteredAgent]:
        """Online work agents from the registry (excludes aop-node supervisor)."""
        sql = """
            SELECT a.agent_key, a.name, a.priority
            FROM agents a
            WHERE a.tenant_id = %s::uuid
              AND a.status IN ('online', 'running')
              AND EXISTS (
                SELECT 1 FROM agent_endpoints e
                WHERE e.agent_id = a.id AND e.is_primary = true
              )
            ORDER BY a.priority DESC, a.agent_key
        """
        with connect(self.database_url) as conn:
            rows = conn.execute(sql, (self.tenant_id,)).fetchall()
        out: list[RegisteredAgent] = []
        for r in rows:
            key = str(r.get("agent_key") or "").strip()
            if not is_work_agent(key):
                continue
            out.append(
                RegisteredAgent(
                    key=key,
                    name=str(r.get("name") or key),
                    priority=int(r.get("priority") or 100),
                )
            )
        return out

    def list_available_agents(self) -> list[str]:
        """Online work agent_keys."""
        return [a.key for a in self.list_registered_agents()]

    def list_agent_endpoints(self) -> dict[str, str]:
        """Map online agent_key → primary endpoint URL."""
        sql = """
            SELECT a.agent_key, e.url
            FROM agents a
            JOIN agent_endpoints e ON e.agent_id = a.id AND e.is_primary = true
            WHERE a.tenant_id = %s::uuid
              AND a.status IN ('online', 'running')
            ORDER BY a.agent_key
        """
        with connect(self.database_url) as conn:
            rows = conn.execute(sql, (self.tenant_id,)).fetchall()
        out: dict[str, str] = {}
        for r in rows:
            key = r.get("agent_key")
            url = r.get("url")
            if key and url and is_work_agent(str(key)):
                out[str(key)] = str(url)
        return out

    def list_ready_agents(self, available: set[str] | None = None) -> list[str]:
        """Online work agents. Health is preflight's job — do not block plan()."""
        keys = set(available) if available is not None else set(self.list_available_agents())
        return sorted(k for k in keys if is_work_agent(k))

    def _heuristic_nodes(
        self,
        goal: str,
        available: set[str],
        *,
        registry: Sequence[RegisteredAgent] | None = None,
        labels: dict[str, str] | None = None,
    ) -> list[PlanNode]:
        hitl = set(hitl_skills())
        reg = list(registry) if registry is not None else coerce_registry(available)
        label_map = labels or {a.key: a.label for a in reg}
        return self._single_hop(goal, available, hitl=hitl, registry=reg, labels=label_map)

    def _single_hop(
        self,
        goal: str,
        available: set[str],
        *,
        hitl: set[str],
        registry: Sequence[RegisteredAgent],
        labels: dict[str, str],
    ) -> list[PlanNode]:
        named = requested_agent(goal, available, registry)
        pool = available
        if not named:
            ready = {k for k in self.list_ready_agents(available) if is_work_agent(k)}
            pool = ready or available
        agent = named or default_agent(pool)
        if not agent:
            return []
        work = extract_work_goal(goal, registry) if named else goal
        instruction = (
            agent_instruction(
                agent, work, parallel=False, label=labels.get(agent)
            )
            if named and work.strip()
            else None
        )
        return [
            PlanNode(
                id="run",
                agent_key=agent,
                depends_on=[],
                requires_approval=agent in hitl,
                instruction=instruction,
            )
        ]

    def _try_llm_nodes(
        self,
        goal: str,
        available: set[str],
        *,
        hitl: set[str],
    ) -> tuple[list[PlanNode] | None, bool]:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            return None, False
        try:
            from openai import OpenAI
        except ImportError:
            return None, False

        client = OpenAI(api_key=api_key, base_url=os.getenv("OPENAI_BASE_URL") or None)
        model = os.getenv("PLANNER_MODEL", "gpt-4o-mini")
        prompt = {
            "goal": goal,
            "available_agent_keys": sorted(available),
            "schema": {
                "nodes": [
                    {
                        "id": "string",
                        "agent_key": "must be in available_agent_keys",
                        "depends_on": ["node_id"],
                    }
                ]
            },
        }
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a task planner for a multi-agent platform. "
                            "Return JSON with key nodes. Only use available_agent_keys "
                            "(each hop is an agent_key). Prefer parallel independent "
                            "nodes. Keep 2-5 nodes. When the goal asks to use all agents "
                            "/ 所有agent / 全部智能体, create one parallel node per "
                            "available agent_key with empty depends_on."
                        ),
                    },
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
            )
            content = resp.choices[0].message.content or "{}"
        except Exception:  # noqa: BLE001
            return None, False

        data = extract_json_object(content)
        if not data:
            return None, False

        repaired = False
        try:
            raw_nodes = data.get("nodes") or []
            if not isinstance(raw_nodes, list) or not raw_nodes:
                return None, False
            nodes: list[PlanNode] = []
            for n in raw_nodes:
                if not isinstance(n, dict):
                    repaired = True
                    continue
                agent_key = str(n.get("agent_key") or "")
                nodes.append(
                    PlanNode(
                        id=str(n.get("id") or "node"),
                        agent_key=agent_key,
                        depends_on=[str(d) for d in (n.get("depends_on") or [])],
                        requires_approval=bool(n.get("requires_approval"))
                        or agent_key in hitl,
                    )
                )
            trial = TaskPlan(title="t", goal=goal, nodes=nodes)
            validate_plan(trial, available_agent_keys=available)
            return nodes, repaired
        except (DAGValidationError, KeyError, TypeError, ValueError):
            repaired = True
            fixed = nodes_from_payload(data, available_agent_keys=available, hitl=hitl)
            if not fixed:
                return None, False
            try:
                validate_plan(
                    TaskPlan(title="t", goal=goal, nodes=fixed),
                    available_agent_keys=available,
                )
            except DAGValidationError:
                return None, False
            return fixed, True

def _short_title(goal: str, limit: int = 48) -> str:
    cleaned = re.sub(r"\s+", " ", goal).strip()
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "..."
