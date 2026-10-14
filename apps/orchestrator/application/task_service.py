"""TaskService: application facade for HTTP / API consumers."""

from __future__ import annotations

from typing import Any

from artifacts import ArtifactStore
from artifacts.manager import ArtifactManager
from billing import BillingService
from billing.invoice import InvoiceService
from evaluation import EvaluationService
from egress import EgressService
from memory import MemoryService
from observability import MetricsService
from planner.engine import PlanningEngine
from quota import QuotaService
from router import AgentRouter
from runtime_graph import RuntimeGraphService
from scheduler.engine import SchedulingEngine
from streams import StreamClient
from workflows import WorkflowService
from aggregator import Aggregator
from execution.dag_bridge import try_sync_from_ctx

from .task_manager import TaskManager


class TaskService:
    """Facade over domain engines — method signatures match legacy service.TaskService."""

    def __init__(self) -> None:
        self.planner_engine = PlanningEngine()
        self.planner = self.planner_engine.planner
        self.router = AgentRouter()
        self.runtime_graph = RuntimeGraphService()
        self.streams = StreamClient()
        self.artifact_store = ArtifactStore()
        self.artifacts = self.artifact_store  # legacy attribute name
        self.workflows = WorkflowService()
        self.evaluations = EvaluationService()
        self.aggregator = Aggregator()
        self.memory = MemoryService()
        self.metrics = MetricsService()
        self.billing = BillingService()
        self.invoices = InvoiceService(billing=self.billing)
        self.quotas = QuotaService(billing=self.billing)
        self.egress = EgressService()

        self.scheduling = SchedulingEngine()
        self.scheduler = self.scheduling.scheduler
        self.artifact_manager = ArtifactManager(
            store=self.artifact_store,
            scheduler=self.scheduler,
        )
        self.tasks = TaskManager(
            planning=self.planner_engine,
            scheduling=self.scheduling,
            workflows=self.workflows,
            quotas=self.quotas,
            memory=self.memory,
            artifacts=self.artifact_manager,
        )

    # ── lifecycle (TaskManager) ──────────────────────────────────────────

    def create(
        self,
        content: str,
        *,
        title: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        return self.tasks.create(content, title=title, tenant_id=tenant_id)

    def run_workflow(
        self,
        workflow_id: str,
        *,
        content: str,
        title: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        return self.tasks.run_workflow(
            workflow_id, content=content, title=title, tenant_id=tenant_id
        )

    def get(self, task_id: str, *, tenant_id: str | None = None) -> dict[str, Any] | None:
        row = self.tasks.get(task_id, tenant_id=tenant_id)
        return self._with_os_state(row) if row else None

    def list(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        tasks = self.tasks.list(status=status, limit=limit, tenant_id=tenant_id)
        return self._decorate_os_states(tasks)

    def _decorate_os_states(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ids = [str(t.get("task_id") or t.get("id") or "") for t in tasks]
        ids = [i for i in ids if i]
        states = self._os_states(ids)
        out: list[dict[str, Any]] = []
        for t in tasks:
            row = dict(t)
            tid = str(row.get("task_id") or row.get("id") or "")
            os_state = states.get(tid)
            if os_state:
                row["os_state"] = os_state
            out.append(row)
        return out

    def _with_os_state(self, row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        tid = str(out.get("task_id") or out.get("id") or "")
        if not tid:
            return out
        states = self._os_states([tid])
        os_state = states.get(tid)
        if os_state:
            out["os_state"] = os_state
        return out

    def _os_states(self, task_ids: list[str]) -> dict[str, str]:
        exec_svc = getattr(self, "execution", None)
        if exec_svc is None:
            try:
                from app_context import ctx

                exec_svc = ctx.execution
            except Exception:  # noqa: BLE001
                exec_svc = None
        fn = getattr(exec_svc, "states_for_tasks", None) if exec_svc else None
        if not callable(fn):
            return {}
        try:
            return fn(task_ids) or {}
        except Exception:  # noqa: BLE001
            return {}

    def cancel(self, task_id: str) -> dict[str, Any] | None:
        return self.tasks.cancel(task_id)

    def requeue_incomplete(self, task_id: str) -> dict[str, Any]:
        out = self.scheduling.requeue_incomplete(task_id)
        try_sync_from_ctx(task_id, "running")
        return out

    def approve(
        self,
        task_id: str,
        *,
        node_key: str | None = None,
        human_input: str | None = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        return self.tasks.approve(
            task_id, node_key=node_key, human_input=human_input, actor=actor
        )

    def reject(
        self,
        task_id: str,
        *,
        node_key: str | None = None,
        reason: str = "rejected by user",
    ) -> dict[str, Any]:
        return self.tasks.reject(task_id, node_key=node_key, reason=reason)

    def list_checkpoints(
        self,
        task_id: str,
        *,
        node_key: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return self.tasks.list_checkpoints(
            task_id, node_key=node_key, limit=limit
        )

    def replay_from_node(
        self,
        task_id: str,
        node_key: str,
        *,
        clear_downstream: bool = True,
    ) -> dict[str, Any]:
        return self.tasks.replay_from_node(
            task_id, node_key, clear_downstream=clear_downstream
        )

    def overview(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        return self.tasks.overview(tenant_id=tenant_id)

    def events(self, task_id: str, limit: int = 80) -> list[dict[str, Any]]:
        return self.tasks.events(task_id, limit=limit)

    def list_artifacts(self, task_id: str) -> list[dict[str, Any]]:
        return self.tasks.list_artifacts(task_id)

    def build_package(self, task_id: str) -> bytes:
        from artifacts.package import build_package_zip

        row = self.get(task_id)
        if not row:
            raise KeyError(task_id)
        store = self.artifacts
        if hasattr(store, "store"):
            store = store.store

        def _fetch(uri: str) -> bytes | None:
            getter = getattr(store, "get_bytes", None)
            if not callable(getter) or not uri:
                return None
            try:
                return getter(uri)
            except Exception:  # noqa: BLE001
                return None

        return build_package_zip(
            row,
            artifacts=self.list_artifacts(task_id),
            events=self.events(task_id),
            evaluation=self.get_evaluation(task_id),
            fetch_bytes=_fetch,
        )

    def run_report_markdown(self, task_id: str) -> str:
        from artifacts.package import build_citations, build_run_report_markdown

        row = self.get(task_id)
        if not row:
            raise KeyError(task_id)
        return build_run_report_markdown(
            build_citations(
                row,
                artifacts=self.list_artifacts(task_id),
                events=self.events(task_id),
                evaluation=self.get_evaluation(task_id),
            )
        )

    def list_all_artifacts(
        self,
        *,
        task_id: str | None = None,
        type_filter: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.tasks.list_all_artifacts(
            task_id=task_id, type_filter=type_filter, limit=limit
        )

    # ── router ───────────────────────────────────────────────────────────

    def router_preview(self, skill: str) -> dict[str, Any]:
        return self.router.preview(skill)

    def discover(self, **kwargs: Any) -> dict[str, Any]:
        """A2A OS: capability-aware Agent discovery (callable by any Agent)."""
        return self.router.discover(**kwargs)

    def route(self, **kwargs: Any) -> dict[str, Any]:
        """A2A OS: select the best Agent for a request (callable by any Agent)."""
        return self.router.route(**kwargs)

    # ── runtime execution graph ──────────────────────────────────────────

    def record_runtime_edge(self, **kwargs: Any) -> dict[str, Any]:
        """A2A OS: record an Agent-to-Agent call edge in the runtime graph."""
        return self.runtime_graph.record_edge(**kwargs)

    def runtime_graph_view(self, root_task_id: str) -> dict[str, Any]:
        """A2A OS: reconstruct the runtime execution graph for a root task."""
        return self.runtime_graph.get_graph(root_task_id)

    def collaboration_graph_view(self, root_task_id: str) -> dict[str, Any]:
        """A2A OS: frontend-friendly collaboration graph (nodes + links).

        Prefers recorded Agent→Agent runtime edges. When none exist (typical for
        orchestrator-driven plan DAGs), synthesizes caller→task→target links from
        the Task plan + assigned agents so the console still shows who ran what.

        Always enriches with per-node handoff (中间传递) and MinIO artifacts
        (最终产物) for the console collaboration view.
        """
        graph = self.runtime_graph.collaboration_graph(root_task_id)
        row = self.get(root_task_id)
        if not graph.get("links") and row:
            synthesized = self.runtime_graph.collaboration_from_plan(row)
            if synthesized.get("links"):
                graph = synthesized
        return self._enrich_collaboration_graph(graph, root_task_id, row)

    def _enrich_collaboration_graph(
        self,
        graph: dict[str, Any],
        root_task_id: str,
        row: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Attach handoff summaries + artifact file lists for the UI."""
        out = dict(graph or {})
        nodes = [dict(n) for n in (out.get("nodes") or []) if isinstance(n, dict)]
        links = [dict(l) for l in (out.get("links") or []) if isinstance(l, dict)]

        by_key: dict[str, dict[str, Any]] = {}
        if row:
            for n in row.get("nodes") or []:
                if not isinstance(n, dict):
                    continue
                key = str(n.get("id") or n.get("node_key") or "")
                if key:
                    by_key[key] = n

        arts: list[dict[str, Any]] = []
        try:
            arts = list(self.list_artifacts(root_task_id) or [])
        except Exception:  # noqa: BLE001
            arts = []

        arts_by_node: dict[str, list[dict[str, Any]]] = {}
        for a in arts:
            if not isinstance(a, dict):
                continue
            nid = str(a.get("node_id") or "").strip()
            if nid:
                arts_by_node.setdefault(nid, []).append(a)

        # Plan deps → find leaf node keys (最终结果所属节点)
        deps_map: dict[str, list[str]] = {}
        if row:
            plan = row.get("plan_json") or {}
            if isinstance(plan, str):
                import json as _json

                try:
                    plan = _json.loads(plan)
                except Exception:
                    plan = {}
            for pn in plan.get("nodes") or []:
                if not isinstance(pn, dict):
                    continue
                nid = str(pn.get("id") or "")
                if nid:
                    deps_map[nid] = [str(d) for d in (pn.get("depends_on") or [])]
        referenced: set[str] = set()
        for deps in deps_map.values():
            referenced.update(deps)
        leaf_keys = {k for k in deps_map if k not in referenced} or set(deps_map.keys())

        handoffs_summary: list[dict[str, Any]] = []
        for node in nodes:
            if node.get("type") != "task":
                continue
            plan_nid = str(node.get("plan_node_id") or "")
            tid = str(node.get("task_id") or "")
            key = plan_nid or (tid.split(":")[-1] if ":" in tid else tid)
            src = by_key.get(key) or {}
            ho = node.get("handoff") if isinstance(node.get("handoff"), dict) else None
            if not ho and isinstance(src.get("handoff"), dict):
                ho = src["handoff"]
            if isinstance(ho, dict):
                node["handoff"] = ho
                if not node.get("handoff_reason") and ho.get("reason"):
                    node["handoff_reason"] = str(ho["reason"])
                if not node.get("artifact_ids") and isinstance(ho.get("artifact_ids"), list):
                    node["artifact_ids"] = [str(a) for a in ho["artifact_ids"] if a]
                handoffs_summary.append(
                    {
                        "node_key": key,
                        "task_id": tid,
                        "agent_key": node.get("agent_key") or src.get("agent_key"),
                        "status": node.get("status") or src.get("status"),
                        "reason": node.get("handoff_reason") or ho.get("reason"),
                        "from": ho.get("from") or key,
                        "to": ho.get("to") or [],
                        "artifact_ids": node.get("artifact_ids") or [],
                        "confidence": ho.get("confidence"),
                    }
                )

            node_arts = list(arts_by_node.get(key) or [])
            if node_arts:
                node["artifacts"] = node_arts
                if not node.get("artifact_ids"):
                    node["artifact_ids"] = [
                        str(a.get("uri") or a.get("url"))
                        for a in node_arts
                        if a.get("uri") or a.get("url")
                    ]
            node["is_leaf"] = key in leaf_keys if leaf_keys else False

        # Do NOT copy mid-task outputs onto links — that made orchestrator
        # dispatch edges look like they "passed" the node's own result files.
        # Inbound handoff files already live on links from plan synthesis.

        final_arts: list[dict[str, Any]] = []
        for key in sorted(leaf_keys):
            final_arts.extend(arts_by_node.get(key) or [])
        if not final_arts and arts:
            # Parallel fan-out: every node is a leaf → show all non-meta files
            final_arts = [
                a
                for a in arts
                if isinstance(a, dict)
                and str(a.get("name") or "").lower() not in {"meta.json"}
            ]

        out["nodes"] = nodes
        out["links"] = links
        out["artifacts"] = arts
        out["handoffs"] = handoffs_summary
        out["final_artifacts"] = final_arts
        return out

    def get_with_graph(self, task_id: str, *, tenant_id: str | None = None) -> dict[str, Any] | None:
        """Central Task plus associated runtime collaboration graph (Phase 2)."""
        row = self.tasks.get(task_id, tenant_id=tenant_id)
        if not row:
            return None
        try:
            graph = self.collaboration_graph_view(task_id)
        except Exception:  # noqa: BLE001 - graph is best-effort enrichment
            graph = {
                "root_task_id": task_id,
                "kind": "collaboration_graph",
                "nodes": [],
                "links": [],
                "tree": [],
                "edges": [],
                "handoffs": [],
                "final_artifacts": [],
                "artifacts": [],
            }
        out = dict(row)
        out["collaboration_graph"] = graph
        out["runtime_graph"] = self.runtime_graph.get_graph(task_id)
        return self._with_os_state(out)

    def agent_performance(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self.router.agent_performance(limit=limit)

    # ── evaluation ───────────────────────────────────────────────────────

    def evaluate(self, task_id: str, *, method: str = "heuristic") -> dict[str, Any]:
        return self.evaluations.evaluate(task_id, method=method)

    def get_evaluation(self, task_id: str) -> dict[str, Any] | None:
        return self.evaluations.get_for_task(task_id)

    def list_evaluations(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 50,
        min_score: float | None = None,
    ) -> list[dict[str, Any]]:
        return self.evaluations.list(tenant_id=tenant_id, limit=limit, min_score=min_score)

    def evaluation_overview(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        return self.evaluations.overview(tenant_id=tenant_id)

    # ── memory ───────────────────────────────────────────────────────────

    def list_memory(self, task_id: str) -> list[dict[str, Any]]:
        return self.memory.list_for_task(task_id)

    def put_memory(
        self,
        task_id: str,
        memory_key: str,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.memory.put(task_id, memory_key, content, metadata=metadata)

    def delete_memory(self, task_id: str, memory_key: str) -> bool:
        return self.memory.delete(task_id, memory_key)

    def list_tenant_memory(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self.memory.list_tenant(limit=limit)

    def put_tenant_memory(
        self,
        memory_key: str,
        content: str,
        *,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.memory.put_tenant(
            memory_key,
            content,
            title=title,
            metadata=metadata,
        )

    def search_tenant_memory(self, query: str, *, top_k: int = 5) -> list[dict[str, Any]]:
        return self.memory.search_tenant(query, top_k=top_k)

    def delete_tenant_memory(self, memory_key: str) -> bool:
        return self.memory.delete_tenant(memory_key)

    def promote_task_memory(self, task_id: str) -> list[dict[str, Any]]:
        return self.memory.promote_task(task_id)

    # ── metrics / billing / quota / egress ───────────────────────────────

    def metrics_snapshot(self, *, hours: float = 24) -> dict[str, Any]:
        return self.metrics.snapshot(hours=hours)

    def metrics_prometheus(self, *, hours: float = 24) -> str:
        return self.metrics.prometheus_text(hours=hours)

    def billing_usage(self, *, days: int = 30, tenant_id: str | None = None) -> dict[str, Any]:
        return self.billing.usage(days=days, tenant_id=tenant_id)

    def billing_summary(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        return self.billing.summary(tenant_id=tenant_id)

    def invoice_preview(self, *, days: int = 30, tenant_id: str | None = None) -> dict[str, Any]:
        return self.invoices.preview(days=days, tenant_id=tenant_id)

    def invoice_preview_markdown(self, *, days: int = 30, tenant_id: str | None = None) -> str:
        return self.invoices.preview_markdown(days=days, tenant_id=tenant_id)

    def create_invoice(
        self, *, days: int = 30, tenant_id: str | None = None, status: str = "draft"
    ) -> dict[str, Any]:
        return self.invoices.create(days=days, tenant_id=tenant_id, status=status)

    def list_invoices(self, *, tenant_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self.invoices.list(tenant_id=tenant_id, limit=limit)

    def get_invoice(self, invoice_id: str, *, tenant_id: str | None = None) -> dict[str, Any] | None:
        return self.invoices.get(invoice_id, tenant_id=tenant_id)

    def create_checkout(
        self,
        invoice_id: str,
        *,
        tenant_id: str | None = None,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        return self.invoices.create_checkout(
            invoice_id, tenant_id=tenant_id, dry_run=dry_run
        )

    def list_checkouts(
        self, *, tenant_id: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        return self.invoices.list_checkouts(tenant_id=tenant_id, limit=limit)

    def list_quota_grants(
        self, *, tenant_id: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        return self.quotas.list_grants(tenant_id=tenant_id, limit=limit)

    def handle_stripe_webhook(
        self, payload: bytes, sig_header: str | None
    ) -> dict[str, Any]:
        return self.invoices.handle_stripe_webhook(payload, sig_header)

    def simulate_checkout_paid(self, stripe_session_id: str) -> dict[str, Any]:
        return self.invoices.simulate_checkout_paid(stripe_session_id)

    def quota_status(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        return self.quotas.status(tenant_id=tenant_id)

    def get_quota(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        return self.quotas.get(tenant_id=tenant_id)

    def update_quota(self, *, tenant_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.quotas.upsert(tenant_id=tenant_id, **kwargs)

    def egress_policy(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        return self.egress.get(tenant_id=tenant_id)

    def update_egress(self, *, tenant_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.egress.upsert(tenant_id=tenant_id, **kwargs)

    def check_egress(self, url: str, *, tenant_id: str | None = None) -> dict[str, Any]:
        return self.egress.check_url(url, tenant_id=tenant_id)
