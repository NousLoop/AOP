"""Smoke tests for extracted API routers (no Redis required)."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient


class _FakeTasks:
    def list(self, status=None, limit=50, tenant_id=None):
        return [{"id": "t1", "tenant_id": tenant_id or "default", "status": status or "queued"}]

    def create(self, content, title=None, tenant_id=None):
        return {"id": "t-new", "title": title, "tenant_id": tenant_id, "content": content}

    def get(self, task_id, tenant_id=None):
        if task_id == "missing":
            return None
        return {
            "id": task_id,
            "status": "running",
            "tenant_id": tenant_id or "00000000-0000-0000-0000-000000000001",
        }

    def requeue_incomplete(self, task_id):
        return {"task_id": task_id, "jobs": [], "enqueued_nodes": ["run"]}

    def get_with_graph(self, task_id, tenant_id=None):
        return self.get(task_id, tenant_id=tenant_id)

    def billing_usage(self, days=30):
        return {"days": days, "total_usd": 1.5}

    def billing_summary(self):
        return {"balance_usd": 0}

    def invoice_preview(self, days=30):
        return {"days": days, "line_items": []}

    def invoice_preview_markdown(self, days=30):
        return f"# invoice {days}d\n"

    def create_invoice(self, days=30, status="draft"):
        return {"id": "inv-1", "days": days, "status": status}

    def list_invoices(self, limit=50):
        return [{"id": "inv-1"}]

    def get_invoice(self, invoice_id):
        if invoice_id == "missing":
            return None
        return {"id": invoice_id}

    def quota_status(self):
        return {"enabled": True}

    def egress_policy(self):
        return {"mode": "allow", "enabled": True}

    def check_egress(self, url):
        return {"url": url, "allowed": True}

    def overview(self):
        return {"tasks": 1}

    def agent_performance(self, limit=50):
        return []

    def discover(self, **kwargs):
        return {"agents": [{"agent_id": "a1"}], "limit": kwargs.get("limit", 20)}

    def route(self, **kwargs):
        return {"agent_id": "a1", "skill": kwargs.get("skill")}

    def list_tenant_memory(self, limit=50):
        return []

    def list_all_artifacts(self, task_id=None, type_filter=None, limit=100):
        return []

    def list_artifacts(self, task_id):
        return [
            {
                "name": "output.md",
                "uri": "s3://aop-artifacts/tasks/t1/n1/output.md",
                "url": "http://127.0.0.1:9000/aop-artifacts/tasks/t1/n1/output.md",
                "node_id": "n1",
                "mime_type": "text/markdown",
            }
        ]

    def events(self, task_id, limit=80):
        return [
            {
                "event_type": "task.completed",
                "message": "done",
                "ts": "2026-01-01T00:00:00Z",
            }
        ]

    def get_evaluation(self, task_id):
        return {"score": 81.6, "grade": "B", "method": "heuristic"}

    def build_package(self, task_id):
        from artifacts.package import build_package_zip

        return build_package_zip(
            self.get(task_id),
            artifacts=self.list_artifacts(task_id),
            events=self.events(task_id),
            evaluation=self.get_evaluation(task_id),
            fetch_bytes=lambda _uri: b"# hello\n",
        )

    def run_report_markdown(self, task_id):
        from artifacts.package import build_citations, build_run_report_markdown

        return build_run_report_markdown(
            build_citations(
                self.get(task_id),
                artifacts=self.list_artifacts(task_id),
                events=self.events(task_id),
                evaluation=self.get_evaluation(task_id),
            )
        )

    def list_evaluations(self, limit=50, min_score=None):
        return []

    def evaluation_overview(self):
        return {"count": 0}

    def router_preview(self, skill):
        return {"skill": skill, "candidates": []}


class _FakeExecution:
    def get_execution_view(self, task_id):
        return {"execute": {"tenant_id": "00000000-0000-0000-0000-000000000001"}, "delegate": {}}

    def get(self, task_id):
        return SimpleNamespace(state="RUNNING", task_id=task_id)

    def recover(self, task_id):
        return {"action": "wait", "class": "RUNNING", "record": {"task_id": task_id}}

    @property
    def events(self):
        return SimpleNamespace(
            list_for_root=lambda root: [],
            list_for_task=lambda task_id: [],
        )


class _FakeMarketplace:
    def catalog(self, q=None):
        rows = [
            {
                "package_id": "pkg-claude-code",
                "name": "Claude Code Agent",
                "agent_key": "claude-code",
                "skills": [],
            }
        ]
        if q:
            return [r for r in rows if q.lower() in r["name"].lower()]
        return rows

    def get_package(self, package_id):
        for r in self.catalog():
            if r["package_id"] == package_id:
                return r
        return None

    @property
    def stats(self):
        return {"packages": 1}


class _FakeDecision:
    def to_dict(self):
        return {"allowed": True, "code": "OK", "context": {"root_task_id": "r1"}}


class _FakeGovernance:
    def check_and_reserve(self, **kwargs):
        return _FakeDecision()

    def release(self, context):
        return None

    def list_denials(self, root_task_id=None, code=None, limit=100):
        return [{"code": "DEPTH", "root_task_id": root_task_id or "r1"}]


class _FakeScheduling:
    def candidates(self, agents, requirement=None, ctx=None):
        return {"candidates": agents, "requirement": requirement or {}}

    def preview(self, agents, requirement=None, exclude_agent_ids=None, estimated_cost=0.1, ctx=None):
        return {"selected": agents[:1], "estimated_cost": estimated_cost}

    def select(self, agents, requirement=None, exclude_agent_ids=None, estimated_cost=0.1, ctx=None, commit_budget=False):
        return {"agent_id": (agents[0]["agent_id"] if agents else None), "committed": commit_budget}

    def simulate(self, body):
        return {"ok": True, "input": body}

    def recovery_plan(self, **kwargs):
        return {"action": "retry", **{k: v for k, v in kwargs.items() if v is not None}}

    @property
    def cost(self):
        return SimpleNamespace(
            task_cost=lambda task_id: {"task_id": task_id, "total_usd": 0.2, "items": []},
            task_breakdown=lambda task_id: {"task_id": task_id, "items": []},
            tenant_cost=lambda tenant_id: {"tenant_id": tenant_id, "total_usd": 1.0},
        )

    @property
    def resources(self):
        return SimpleNamespace(
            get_quota=lambda tenant_id=None: SimpleNamespace(
                to_dict=lambda: {"tenant_id": tenant_id or "default", "max": 10}
            ),
            check_agent_capacity=lambda health, queue_depth=0, quota=None: (True, "ok"),
        )

    @property
    def budget(self):
        return SimpleNamespace(
            ensure_tenant_budget=lambda tenant_id: {
                "monthly": SimpleNamespace(to_dict=lambda: {"tenant_id": tenant_id, "limit": 100})
            }
        )

    @property
    def policies(self):
        return SimpleNamespace(
            get_tenant_policy=lambda tenant_id: SimpleNamespace(
                to_dict=lambda: {"tenant_id": tenant_id, "mode": "default"}
            ),
            set_tenant_policy=lambda tenant_id, policy: SimpleNamespace(
                to_dict=lambda: {"tenant_id": tenant_id, **policy}
            ),
        )

    @property
    def reliability(self):
        return SimpleNamespace(get=lambda agent_id: None, compute_from_records=lambda records: None)


def test_extracted_api_routers(monkeypatch):
    from app_context import bind_services
    from api import register_api_routes
    import marketplace.bundle as bundle

    monkeypatch.setattr(
        bundle,
        "attach_bundle_meta",
        lambda pkg, base_url="", catalog=None: {**pkg, "install_url": f"{base_url}/x"},
    )
    monkeypatch.setattr(
        "api.preflight.collect_preflight",
        lambda: {"status": "blocked", "can_run": False, "blocking": ["worker"], "agents": []},
    )

    bind_services(
        tasks=_FakeTasks(),
        execution=_FakeExecution(),
        marketplace=_FakeMarketplace(),
        scheduling=_FakeScheduling(),
        governance=_FakeGovernance(),
        workflows=SimpleNamespace(list=lambda status=None: []),
        agent_lifecycle=SimpleNamespace(
            accepts_delegation=lambda agent_id: (True, "ok"),
            health=lambda agent_id: {"agent_id": agent_id, "status": "ready"},
            get=lambda agent_id: None,
            heartbeat=lambda agent_id, **kw: SimpleNamespace(
                to_dict=lambda: {"agent_id": agent_id, "state": "READY"}
            ),
        ),
        capacity_queue=SimpleNamespace(depth=lambda agent_id: 0, cancel=lambda task_id: None),
        exec_cfg=SimpleNamespace(queue_enabled=True),
    )
    app = FastAPI()
    register_api_routes(app)
    client = TestClient(app)
    ten = {"X-Tenant-Id": "00000000-0000-0000-0000-000000000001"}

    r = client.get("/v1/tasks", headers=ten)
    assert r.status_code == 200
    assert r.json()["tasks"][0]["id"] == "t1"

    r = client.post("/v1/tasks", json={"input": {"content": "hello"}, "title": "t"}, headers=ten)
    assert r.status_code == 201
    assert r.json()["id"] == "t-new"

    r = client.get("/v1/marketplace")
    assert r.status_code == 200
    assert r.json()["packages"][0]["package_id"] == "pkg-claude-code"

    r = client.get("/v1/marketplace/stats")
    assert r.status_code == 200
    assert r.json()["stats"]["packages"] == 1

    r = client.get("/v1/billing/usage?days=7")
    assert r.status_code == 200
    assert r.json()["days"] == 7

    r = client.get("/v1/billing/invoice.md?days=7")
    assert r.status_code == 200
    assert "invoice" in r.text

    r = client.get("/v1/quotas")
    assert r.status_code == 200
    assert r.json()["enabled"] is True

    r = client.get("/v1/egress/check", params={"url": "https://example.com"})
    assert r.status_code == 200
    assert r.json()["allowed"] is True

    r = client.post(
        "/v1/governance/check",
        json={
            "root_task_id": "r1",
            "caller_agent_id": "a",
            "target_agent_id": "b",
        },
    )
    assert r.status_code == 200
    assert r.json()["allowed"] is True

    r = client.get("/v1/governance/denials")
    assert r.status_code == 200
    assert r.json()["count"] == 1

    r = client.post("/v1/scheduling/simulate", json={"n": 1})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    r = client.get("/v1/tenants/00000000-0000-0000-0000-000000000001/cost", headers=ten)
    assert r.status_code == 200
    assert r.json()["total_usd"] == 1.0

    r = client.get("/v1/agents/agent-1/capacity")
    assert r.status_code == 200
    assert r.json()["accepts"] is True

    r = client.get("/v1/stats/overview")
    assert r.status_code == 200

    r = client.get("/v1/preflight")
    assert r.status_code == 200
    assert r.json()["status"] == "blocked"

    r = client.post(
        "/v1/discover",
        json={"required_skills": ["code"], "limit": 5},
    )
    assert r.status_code == 200

    r = client.get("/v1/memory")
    assert r.status_code == 200
    assert "memories" in r.json()

    r = client.get("/v1/workflows")
    assert r.status_code == 200
    assert r.json()["workflows"] == []

    r = client.get("/v1/agents/agent-1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"

    r = client.post("/v1/nodes/node-local-1/heartbeat", json={"status": "online"})
    assert r.status_code == 200
    assert r.json()["kind"] == "node"
    assert r.json()["node_id"] == "node-local-1"

    r = client.post("/v1/agents/claude-code/heartbeat", json={})
    assert r.status_code == 200
    assert r.json()["agent_id"] == "claude-code"

    r = client.get("/v1/artifacts")
    assert r.status_code == 200
    assert r.json()["artifacts"] == []

    r = client.get("/v1/tasks/t1/report")
    assert r.status_code == 200
    assert "AOP run report" in r.text
    assert "t1" in r.text

    r = client.get("/v1/tasks/t1/package")
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("application/zip")
    assert r.content[:2] == b"PK"

    r = client.get("/v1/tasks/t1/graph")
    assert r.status_code == 200
    body = r.json()
    assert "nodes" in body or "task" in body

    r = client.get("/v1/tasks/missing/package")
    assert r.status_code == 404

    r = client.post("/v1/tasks/t1/recover", headers=ten)
    assert r.status_code == 200
    assert r.json()["enqueued_nodes"] == ["run"]
    assert r.json()["action"] == "wait"

    r = client.post(
        "/v1/tasks/t1/recover",
        headers={"X-Tenant-Id": "00000000-0000-0000-0000-000000000099"},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "tenant_forbidden"
