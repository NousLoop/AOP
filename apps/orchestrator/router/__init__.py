"""Router v3: Skill Match + Online + configurable scored selection (latency / success / priority)."""

from __future__ import annotations

from defaults import DEFAULT_TENANT_ID, database_url as resolve_database_url, is_work_agent

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row
from db import connect

try:
    import redis
except ImportError:  # pragma: no cover
    redis = None  # type: ignore

from .scoring import ScoringEngine, ScoringPolicy

ONLINE_STATUSES = frozenset({"online", "running"})

# Optional: Import CandidateFilter for advanced filtering
try:
    from .filter import CandidateFilter, TaskRequirements, RoutingPolicy
    HAS_FILTER = True
except ImportError:  # pragma: no cover
    HAS_FILTER = False

@dataclass
class RoutedAgent:
    agent_id: str
    agent_key: str
    name: str
    endpoint: str
    status: str
    skills: list[str]
    priority: int
    source: str  # redis+pg | pg
    score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)

class RouterError(LookupError):
    pass

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

class AgentRouter:
    """Select an online agent that provides the requested skill (scored)."""

    def __init__(
        self,
        database_url: str | None = None,
        redis_url: str | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
        *,
        metrics_window_hours: float | None = None,
        scoring_policy: ScoringPolicy | None = None,
        lifecycle_checker=None,
        capacity_queue_depth=None,
    ):
        self.database_url = resolve_database_url(database_url)
        self.redis_url = redis_url or os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
        self.tenant_id = tenant_id
        self.metrics_window_hours = float(
            metrics_window_hours
            if metrics_window_hours is not None
            else os.getenv("ROUTER_METRICS_HOURS", "24")
        )
        self.smart = os.getenv("ROUTER_SMART", "true").lower() not in {"0", "false", "no"}
        self.lifecycle_checker = lifecycle_checker
        self.capacity_queue_depth = capacity_queue_depth
        self._redis = None
        if redis is not None:
            try:
                self._redis = redis.Redis.from_url(self.redis_url, decode_responses=True)
                self._redis.ping()
            except Exception:  # noqa: BLE001
                self._redis = None
        
        # Initialize scoring engine with configurable policy
        self._scoring_policy = scoring_policy or ScoringPolicy.from_env()
        self._scoring_engine = ScoringEngine(self._scoring_policy)

    def set_lifecycle_hooks(
        self,
        *,
        lifecycle_checker=None,
        capacity_queue_depth=None,
    ) -> None:
        """P4.15: wire AgentLifecycle / capacity queue into CandidateFilter."""
        if lifecycle_checker is not None:
            self.lifecycle_checker = lifecycle_checker
        if capacity_queue_depth is not None:
            self.capacity_queue_depth = capacity_queue_depth

    @property
    def scoring_policy(self) -> ScoringPolicy:
        """Get the current scoring policy."""
        return self._scoring_policy

    def select(
        self,
        skill: str,
        *,
        exclude_agent_ids: set[str] | list[str] | None = None,
    ) -> RoutedAgent:
        ranked = self.rank(skill, exclude_agent_ids=exclude_agent_ids)
        if not ranked and exclude_agent_ids:
            # No alternate online agent — fall back to the excluded pool rather
            # than failing with "no online agent after excluding …".
            ranked = self.rank(skill, exclude_agent_ids=None)
        if not ranked:
            excluded = {str(x) for x in (exclude_agent_ids or []) if x}
            raise RouterError(
                f"no online agent for skill={skill!r}"
                + (f" after excluding {sorted(excluded)}" if excluded else "")
            )
        return ranked[0]

    def rank(
        self,
        skill: str,
        *,
        exclude_agent_ids: set[str] | list[str] | None = None,
    ) -> list[RoutedAgent]:
        skill = (skill or "").strip()
        if not skill:
            raise RouterError("skill is required")

        excluded = {str(x) for x in (exclude_agent_ids or []) if x}
        keyed = self._get_agent(skill)
        keyed_key = str((keyed or {}).get("agent_key") or "")
        exact_key = keyed is not None and is_work_agent(keyed_key) and (
            keyed_key == skill or str(keyed.get("agent_id") or "") == skill
        )
        if exact_key:
            candidates = [keyed]
            source = "agent_key"
        else:
            candidates = self._candidates_from_redis(skill)
            source = "redis+pg"
            if not candidates:
                candidates = self._candidates_from_pg(skill)
                source = "pg"

        online = [
            c
            for c in candidates
            if c["status"] in ONLINE_STATUSES
            and c.get("endpoint")
            and c["agent_id"] not in excluded
            and is_work_agent(str(c.get("agent_key") or ""))
        ]
        # Plan skill is agent_key: never score-mix a different agent into that slot.
        named = [c for c in online if str(c.get("agent_key") or "") == skill]
        if keyed_key == skill or named:
            online = named
            source = "agent_key"
        if not online:
            return []

        metrics = self._load_metrics([c["agent_id"] for c in online])
        scored: list[RoutedAgent] = []
        for c in online:
            breakdown = self._score(c, metrics.get(c["agent_id"]) or {})
            total = float(breakdown["total"])
            scored.append(
                RoutedAgent(
                    agent_id=c["agent_id"],
                    agent_key=c["agent_key"],
                    name=c["name"],
                    endpoint=c["endpoint"],
                    status=c["status"],
                    skills=list(c.get("skills") or []),
                    priority=int(c["priority"]),
                    source=source,
                    score=round(total, 4),
                    score_breakdown={k: round(v, 4) for k, v in breakdown.items()},
                )
            )

        if self.smart:
            scored.sort(key=lambda a: (-a.score, a.priority, a.name))
        else:
            scored.sort(key=lambda a: (a.priority, a.name))
        return scored

    def preview(
        self,
        skill: str,
        *,
        exclude_agent_ids: set[str] | list[str] | None = None,
    ) -> dict[str, Any]:
        ranked = self.rank(skill, exclude_agent_ids=exclude_agent_ids)
        return {
            "skill": skill,
            "smart": self.smart,
            "metrics_window_hours": self.metrics_window_hours,
            "scoring_policy": self._scoring_policy.to_dict(),
            "candidates": [
                {
                    "agent_id": a.agent_id,
                    "agent_key": a.agent_key,
                    "name": a.name,
                    "status": a.status,
                    "endpoint": a.endpoint,
                    "priority": a.priority,
                    "score": a.score,
                    "score_breakdown": a.score_breakdown,
                    "source": a.source,
                }
                for a in ranked
            ],
            "selected": ranked[0].agent_id if ranked else None,
        }

    # ── A2A OS: open discovery & routing (callable by any Agent) ──────────

    def _all_candidates_with_cards(self, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
        """Fetch every agent for the tenant with card_json + skills + endpoint.

        Used by :meth:`discover` so the capability/IO/resource filters in
        ``router.filter`` can read the full Agent Card, not just the skill index.
        """
        tid = tenant_id or self.tenant_id
        sql = """
            SELECT a.id::text AS agent_id,
                   a.agent_key,
                   a.name,
                   a.status,
                   a.priority,
                   a.tenant_id::text AS tenant_id,
                   a.card_json,
                   COALESCE((
                     SELECT e.url FROM agent_endpoints e
                     WHERE e.agent_id = a.id AND e.is_primary = true
                     ORDER BY e.updated_at DESC LIMIT 1
                   ), '') AS endpoint,
                   COALESCE((
                     SELECT array_agg(s.skill_id ORDER BY s.skill_id)
                     FROM agent_skills s WHERE s.agent_id = a.id
                   ), '{}') AS skills
            FROM agents a
            WHERE a.tenant_id = %s::uuid
        """
        with connect(self.database_url) as conn:
            rows = conn.execute(sql, (tid,)).fetchall()
        return [dict(r) for r in rows]

    def discover(
        self,
        *,
        required_skills: list[str] | None = None,
        capabilities: dict[str, Any] | None = None,
        input: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        tenant_id: str | None = None,
        match_all_skills: bool = False,
        exclude_agent_ids: set[str] | list[str] | None = None,
        include_offline: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Capability-aware discovery over the Agent Registry.

        This is the infrastructure entry point any Agent can call to find peers
        (``POST /v1/discover``). It runs the full Router v3 filter pipeline
        (skill → capability → IO compatibility → health → resource → tenant →
        policy) and scores the survivors. The caller — not a central
        orchestrator — decides which candidate to invoke.
        """
        skills = [str(s).strip() for s in (required_skills or []) if str(s).strip()]
        caps = capabilities or {}
        inp = input or {}
        out = output or {}
        tid = tenant_id or self.tenant_id

        candidates = self._all_candidates_with_cards(tenant_id=tid)
        candidates = [
            c for c in candidates if is_work_agent(str(c.get("agent_key") or ""))
        ]

        # Stage 1: skill match (union by default, intersection when match_all_skills).
        # Plan nodes use agent_key; treat key as an implicit skill.
        if skills:
            wanted = set(skills)

            def _skill_set(c: dict[str, Any]) -> set[str]:
                have = {str(s) for s in (c.get("skills") or []) if s}
                key = str(c.get("agent_key") or "").strip()
                if key:
                    have.add(key)
                return have

            if match_all_skills:
                candidates = [c for c in candidates if wanted.issubset(_skill_set(c))]
            else:
                candidates = [c for c in candidates if wanted & _skill_set(c)]

        if not include_offline:
            candidates = [c for c in candidates if c.get("status") in ONLINE_STATUSES and c.get("endpoint")]

        resources = caps.get("resources") or {}
        excluded = {str(x) for x in (exclude_agent_ids or []) if x}
        if HAS_FILTER:
            reqs = TaskRequirements(
                skill=skills[0] if skills else "",
                input_modes=(inp.get("modes") or ([inp["type"]] if inp.get("type") else ["text"])),
                output_modes=(out.get("modes") or ([out["type"]] if out.get("type") else ["text"])),
                requires_streaming=bool(caps.get("streaming")),
                requires_web_browsing=bool(caps.get("web_browsing") or caps.get("webBrowsing")),
                requires_code_execution=bool(caps.get("code_execution") or caps.get("codeExecution")),
                required_cpu=caps.get("cpu_cores") or resources.get("cpuCores"),
                required_memory_mb=caps.get("memory_mb") or resources.get("memoryMb"),
                requires_gpu=bool(caps.get("gpu") or resources.get("gpuRequired")),
                gpu_type=caps.get("gpu_type") or resources.get("gpuType"),
                tenant_id=tid,
            )
            candidate_filter = CandidateFilter(
                lifecycle_checker=self.lifecycle_checker,
                capacity_queue_depth=self.capacity_queue_depth,
            )
            filtered, stats = candidate_filter.filter_candidates(
                candidates, reqs, exclude_agent_ids=excluded or None
            )
            filter_stats = stats.to_dict()
        else:  # pragma: no cover - filter module always present
            filtered = [c for c in candidates if c["agent_id"] not in excluded]
            filter_stats = {}

        metrics = self._load_metrics([c["agent_id"] for c in filtered])
        scored: list[RoutedAgent] = []
        for c in filtered:
            breakdown = self._score(c, metrics.get(c["agent_id"]) or {})
            scored.append(
                RoutedAgent(
                    agent_id=c["agent_id"],
                    agent_key=c["agent_key"],
                    name=c["name"],
                    endpoint=normalize_endpoint(c.get("endpoint") or ""),
                    status=c["status"],
                    skills=list(c.get("skills") or []),
                    priority=int(c["priority"]),
                    source="registry",
                    score=round(float(breakdown["total"]), 4),
                    score_breakdown={k: round(v, 4) for k, v in breakdown.items()},
                )
            )

        if self.smart:
            scored.sort(key=lambda a: (-a.score, a.priority, a.name))
        else:
            scored.sort(key=lambda a: (a.priority, a.name))
        scored = scored[: max(1, min(limit, 100))]

        return {
            "required_skills": skills,
            "match_all_skills": match_all_skills,
            "capabilities": caps,
            "input": inp,
            "output": out,
            "tenant_id": tid,
            "candidates": [
                {
                    "agent_id": a.agent_id,
                    "agent_key": a.agent_key,
                    "name": a.name,
                    "status": a.status,
                    "endpoint": a.endpoint,
                    "skills": a.skills,
                    "priority": a.priority,
                    "score": a.score,
                    "score_breakdown": a.score_breakdown,
                    "source": a.source,
                }
                for a in scored
            ],
            "selected": scored[0].agent_id if scored else None,
            "filter_stats": filter_stats,
        }

    def route(
        self,
        *,
        skill: str | None = None,
        required_skills: list[str] | None = None,
        capabilities: dict[str, Any] | None = None,
        input: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        tenant_id: str | None = None,
        exclude_agent_ids: set[str] | list[str] | None = None,
    ) -> dict[str, Any]:
        """Pick the single best Agent for a request (``POST /v1/route``).

        Runs the same capability-aware pipeline as :meth:`discover` and returns
        the selected Agent plus the ranked candidate list. The calling Agent
        still owns the decision of whether to actually invoke the result.
        """
        skills = [str(s).strip() for s in (required_skills or []) if str(s).strip()]
        if skill and str(skill).strip():
            skills.insert(0, str(skill).strip())
        result = self.discover(
            required_skills=skills,
            capabilities=capabilities,
            input=input,
            output=output,
            tenant_id=tenant_id,
            exclude_agent_ids=exclude_agent_ids,
        )
        candidates = result["candidates"]
        result["selected_agent"] = candidates[0] if candidates else None
        return result

    def agent_performance(self, *, limit: int = 50) -> list[dict[str, Any]]:
        since = _utc_now() - timedelta(hours=self.metrics_window_hours)
        sql = """
            SELECT a.id::text AS agent_id,
                   a.agent_key,
                   a.name,
                   a.status,
                   a.priority,
                   COUNT(r.id)::int AS request_count,
                   COUNT(r.id) FILTER (WHERE r.status = 'success')::int AS success_count,
                   COUNT(r.id) FILTER (WHERE r.status = 'failed')::int AS failure_count,
                   COALESCE(AVG(r.latency_ms) FILTER (WHERE r.latency_ms IS NOT NULL), 0)::float
                     AS avg_latency_ms
            FROM agents a
            LEFT JOIN agent_runs r
              ON r.agent_id = a.id AND r.created_at >= %s
            WHERE a.tenant_id = %s::uuid
            GROUP BY a.id
            ORDER BY request_count DESC, a.name ASC
            LIMIT %s
        """
        with connect(self.database_url) as conn:
            rows = conn.execute(sql, (since, self.tenant_id, max(1, min(limit, 200)))).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            req = int(r["request_count"] or 0)
            ok = int(r["success_count"] or 0)
            out.append(
                {
                    **dict(r),
                    "success_rate": round((ok / req) if req else 0.0, 4),
                }
            )
        return out

    def _score(self, candidate: dict[str, Any], metrics: dict[str, Any]) -> dict[str, float]:
        """Score a candidate using the configurable scoring engine.

        Args:
            candidate: Agent candidate dictionary
            metrics: Agent metrics from agent_runs

        Returns:
            Dictionary with individual scores and total score
        """
        # Use new configurable scoring engine
        scores = self._scoring_engine.score_candidate(candidate, metrics)
        
        # Add sample count and latency for backward compatibility
        req = int(metrics.get("request_count") or 0)
        avg_lat = float(metrics.get("avg_latency_ms") or 0.0)
        scores["samples"] = float(req)
        scores["avg_latency_ms"] = avg_lat
        
        # For backward compatibility with smart=false mode
        if not self.smart:
            priority_score = scores["priority"]
            scores["total"] = priority_score
        
        return scores

    def _load_metrics(self, agent_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not agent_ids:
            return {}
        since = _utc_now() - timedelta(hours=self.metrics_window_hours)
        sql = """
            SELECT agent_id::text AS agent_id,
                   COUNT(*)::int AS request_count,
                   COUNT(*) FILTER (WHERE status = 'success')::int AS success_count,
                   COUNT(*) FILTER (WHERE status = 'failed')::int AS failure_count,
                   COALESCE(AVG(latency_ms) FILTER (WHERE latency_ms IS NOT NULL), 0)::float
                     AS avg_latency_ms
            FROM agent_runs
            WHERE agent_id = ANY(%s::uuid[])
              AND created_at >= %s
            GROUP BY agent_id
        """
        with connect(self.database_url) as conn:
            rows = conn.execute(sql, (agent_ids, since)).fetchall()
        return {r["agent_id"]: dict(r) for r in rows}

    def _candidates_from_redis(self, skill: str) -> list[dict[str, Any]]:
        if self._redis is None:
            return []
        agent_ids = list(self._redis.smembers(f"agent:skill:{skill}") or [])
        if not agent_ids:
            return []
        by_id = self._get_agents([str(a) for a in agent_ids])
        out: list[dict[str, Any]] = []
        for agent_id in agent_ids:
            row = by_id.get(str(agent_id))
            if row and skill in (row.get("skills") or []):
                out.append(row)
        return out

    def _candidates_from_pg(self, skill: str) -> list[dict[str, Any]]:
        sql = """
            SELECT a.id::text AS agent_id,
                   a.agent_key,
                   a.name,
                   a.status,
                   a.priority,
                   COALESCE((
                     SELECT e.url FROM agent_endpoints e
                     WHERE e.agent_id = a.id AND e.is_primary = true
                     ORDER BY e.updated_at DESC LIMIT 1
                   ), '') AS endpoint,
                   COALESCE((
                     SELECT array_agg(s.skill_id ORDER BY s.skill_id)
                     FROM agent_skills s WHERE s.agent_id = a.id
                   ), '{}') AS skills
            FROM agents a
            WHERE a.tenant_id = %s::uuid
              AND EXISTS (
                SELECT 1 FROM agent_skills s
                WHERE s.agent_id = a.id AND s.skill_id = %s
              )
            ORDER BY a.priority ASC, a.name ASC
        """
        with connect(self.database_url) as conn:
            rows = conn.execute(sql, (self.tenant_id, skill)).fetchall()
        return [dict(r) for r in rows]

    def _get_agents(self, agent_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Batch-load agents by id or agent_key (one PG round-trip)."""
        ids = [str(a).strip() for a in agent_ids if str(a).strip()]
        if not ids:
            return {}
        sql = """
            SELECT a.id::text AS agent_id,
                   a.agent_key,
                   a.name,
                   a.status,
                   a.priority,
                   COALESCE((
                     SELECT e.url FROM agent_endpoints e
                     WHERE e.agent_id = a.id AND e.is_primary = true
                     ORDER BY e.updated_at DESC LIMIT 1
                   ), '') AS endpoint,
                   COALESCE((
                     SELECT array_agg(s.skill_id ORDER BY s.skill_id)
                     FROM agent_skills s WHERE s.agent_id = a.id
                   ), '{}') AS skills
            FROM agents a
            WHERE a.tenant_id = %s::uuid
              AND (a.id::text = ANY(%s) OR a.agent_key = ANY(%s))
        """
        with connect(self.database_url) as conn:
            rows = conn.execute(sql, (self.tenant_id, ids, ids)).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            out[item["agent_id"]] = item
            key = item.get("agent_key")
            if key:
                out[str(key)] = item
        return out

    def _get_agent(self, agent_id: str) -> dict[str, Any] | None:
        found = self._get_agents([agent_id])
        return found.get(str(agent_id))

def normalize_endpoint(url: str) -> str:
    """Map docker service hostnames to localhost when running agents on the host.

    Inside Compose set AOP_RUNTIME=docker so service DNS (claude-coder, …) is kept.
    """
    runtime = os.getenv("AOP_RUNTIME", "").lower()
    if runtime in {"docker", "compose", "container"}:
        return url

    parsed = urlparse(url)
    host = parsed.hostname or ""
    try:
        from agent_runtime.harness.profiles import host_port_map

        mapped = host_port_map()
    except ImportError:  # pragma: no cover
        mapped = {
            "claude-code": 8011,
            "deepseek-harness": 8012,
            "pi": 8013,
            "openclaw": 8014,
            "hermes": 8015,
            "harness-agent": 8011,
        }
    if host in mapped:
        port = parsed.port or mapped[host]
        return f"http://127.0.0.1:{port}/"
    return url

def hitl_skills() -> set[str]:
    """Skills that pause for human approval after success (comma-separated env)."""
    raw = os.getenv("HITL_SKILLS", "off")
    if not raw or raw.strip().lower() in {"0", "false", "off", "none"}:
        return set()
    return {s.strip() for s in raw.split(",") if s.strip()}
