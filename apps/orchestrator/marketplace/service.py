"""Phase 6 Marketplace Service — publish / install / activate on existing Registry."""
from __future__ import annotations

from defaults import DEFAULT_TENANT_ID, database_url as resolve_database_url

import json
import logging
import os
import threading
import time
from typing import Any

import httpx

from db import connect
from marketplace.deps import DependencyResolver, parse_dependencies, satisfies
from marketplace.manifest import AgentManifest, ManifestValidationError, validate_manifest
from marketplace.permissions import PermissionService
from marketplace.sandbox_provider import SandboxSpec, get_sandbox_provider
from marketplace.skills import SkillRegistry, SkillSearchQuery

logger = logging.getLogger(__name__)

# Version statuses for agent manifests
VERSION_ACTIVE = "ACTIVE"
VERSION_INACTIVE = "INACTIVE"
VERSION_DEPRECATED = "DEPRECATED"
VERSION_REVOKED = "REVOKED"

# Marketplace package statuses
PKG_DRAFT = "DRAFT"
PKG_PUBLISHED = "PUBLISHED"
PKG_DEPRECATED = "DEPRECATED"
PKG_REVOKED = "REVOKED"

def _agent_endpoint(env_key: str, fallback: str) -> str:
    return (os.getenv(env_key) or fallback).rstrip("/")

# Curated catalog for local/dev marketplace — built from harness profile SoT.
try:
    from agent_runtime.harness.profiles import marketplace_catalog_rows

    CATALOG: list[dict[str, Any]] = marketplace_catalog_rows(
        endpoint_resolver=_agent_endpoint
    )
except ImportError:  # pragma: no cover — orchestrator without editable agent-runtime
    CATALOG = [
        {
            "package_id": "pkg-aopd-claude-code",
            "name": "aopd Claude Code",
            "description": "Claude Code via aopd shared CLI A2A",
            "publisher": "AOP Official",
            "version": "0.1.0",
            "skills": [],
            "default_endpoint": _agent_endpoint(
                "AOP_AGENT_CLAUDE_CODE_URL",
                "http://127.0.0.1:8020/agents/claude-code",
            ),
            "agent_key": "aopd-claude-code",
            "plugin_id": "claude-code",
            "tags": ["harness", "claude", "aopd"],
        },
    ]

class MarketplaceService:
    """Agent capability directory + install path into Gateway Registry."""

    def __init__(
        self,
        database_url: str | None = None,
        gateway_url: str | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
        *,
        event_bus=None,
        lifecycle=None,
        pool=None,
    ):
        self.database_url = resolve_database_url(database_url)
        self.gateway_url = (gateway_url or os.getenv("GATEWAY_URL", "http://127.0.0.1:8080")).rstrip(
            "/"
        )
        self.tenant_id = tenant_id
        self._events = event_bus
        self._lifecycle = lifecycle
        self._lock = threading.RLock()
        self._packages: dict[str, dict[str, Any]] = {}  # package_id -> package
        self._manifests: dict[str, dict[str, AgentManifest]] = {}  # agent_key -> version -> manifest
        self._installations: dict[str, dict[str, Any]] = {}  # install_key -> record
        self._active_versions: dict[str, str] = {}  # agent_key -> active version
        self.skills = SkillRegistry(pool=pool, default_tenant=tenant_id)
        self.permissions = PermissionService()
        self.resolver = DependencyResolver()
        self._seed_catalog_packages()
        # metrics counters (also mirrored to prometheus when wired)
        self.stats = {
            "agent_registration_count": 0,
            "agent_install_count": 0,
            "agent_activation_count": 0,
            "skill_discovery_count": 0,
            "skill_invocation_count": 0,
            "installation_failure_count": 0,
            "discovery_latency_ms_sum": 0.0,
            "discovery_latency_ms_count": 0,
        }

    def _seed_catalog_packages(self) -> None:
        for pkg in CATALOG:
            self._packages[pkg["package_id"]] = {
                **pkg,
                "status": PKG_PUBLISHED,
                "kind": "agent",
                "tenant_id": self.tenant_id,
            }
            for sk in pkg.get("skills") or []:
                self.skills.attach_provider(sk, pkg["agent_key"])

    def _emit(self, event_type: str, *, agent_id: str | None = None, payload: dict | None = None) -> None:
        if self._events is None:
            return
        try:
            from execution.events import ExecutionEvent

            self._events.emit(
                ExecutionEvent(
                    event_type=event_type,
                    agent_id=agent_id,
                    payload=payload or {},
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("marketplace event emit skipped: %s", exc)

    # ── Catalog (backward compatible) ─────────────────────────────────────

    def catalog(self, *, q: str | None = None, tenant_id: str | None = None) -> list[dict[str, Any]]:
        tenant_id = tenant_id or self.tenant_id
        installed = self._installed_by_key(tenant_id=tenant_id)
        query = (q or "").strip().lower()
        out: list[dict[str, Any]] = []
        for pkg in self._packages.values():
            if pkg.get("tenant_id") and pkg["tenant_id"] != tenant_id and pkg.get("status") != PKG_PUBLISHED:
                # published packages are visible cross-tenant as catalog; install is tenant-scoped
                if pkg["tenant_id"] != tenant_id:
                    continue
            if query:
                hay = " ".join(
                    [
                        str(pkg.get("name") or ""),
                        str(pkg.get("description") or ""),
                        str(pkg.get("agent_key") or pkg.get("package_id") or ""),
                        " ".join(pkg.get("skills") or []),
                        " ".join(pkg.get("tags") or []),
                    ]
                ).lower()
                if query not in hay:
                    continue
            agent_key = pkg.get("agent_key") or pkg.get("package_id")
            agent = installed.get(agent_key)
            item = {**pkg, "installed": agent is not None, "agent": agent}
            out.append(item)
        return out

    def get_package(self, package_id: str) -> dict[str, Any] | None:
        pkg = self._packages.get(package_id)
        if not pkg:
            return None
        installed = self._installed_by_key()
        agent_key = pkg.get("agent_key") or package_id
        return {**pkg, "installed": agent_key in installed, "agent": installed.get(agent_key)}

    # ── Manifest registration ─────────────────────────────────────────────

    def register_manifest(
        self,
        data: dict[str, Any],
        *,
        tenant_id: str | None = None,
        activate: bool = True,
        skip_gateway: bool = False,
    ) -> dict[str, Any]:
        tenant_id = tenant_id or self.tenant_id
        manifest = validate_manifest(data)

        # Permission check
        decl = self.permissions.validate_declared(manifest.permissions)
        if not decl.allowed:
            raise ManifestValidationError(decl.reason)
        ten = self.permissions.tenant_allows(tenant_id, manifest.permissions)
        if not ten.allowed:
            raise PermissionError(f"tenant permission denied: {ten.denied}")

        # Dependency check (soft if catalog empty for missing optional kinds)
        deps = parse_dependencies(manifest.dependencies)
        if deps:
            available = self._available_versions()
            result = self.resolver.resolve(deps, available)
            if not result.ok and result.conflicts:
                raise ValueError(f"dependency conflict: {result.conflicts}")
            # missing skills that exist in skill registry versions are OK if we can auto-attach
            for miss in list(result.missing):
                kind, _, name = miss.partition(":")
                if kind == "skill" and self.skills.get(name):
                    result.missing.remove(miss)
            if result.missing:
                raise ValueError(f"missing dependencies: {result.missing}")

        with self._lock:
            versions = self._manifests.setdefault(manifest.name, {})
            versions[manifest.version] = manifest
            if activate:
                self._active_versions[manifest.name] = manifest.version

        self.permissions.grant(tenant_id, manifest.name, manifest.permissions)
        for sk in manifest.skills:
            self.skills.attach_provider(sk, manifest.name)
            if not self.skills.get(sk):
                self.skills.register(
                    {"name": sk, "version": "1.0.0", "description": f"Skill from {manifest.name}"},
                    persist=False,
                )
                self.skills.attach_provider(sk, manifest.name)

        agent_payload: dict[str, Any] = {}
        if not skip_gateway:
            agent_payload = self._gateway_register(manifest.endpoint)

        agent_id = (
            agent_payload.get("id")
            or agent_payload.get("agent_id")
            or agent_payload.get("agent", {}).get("id")
            or manifest.name
        )
        if self._lifecycle is not None:
            try:
                self._lifecycle.register(
                    str(agent_id),
                    version=manifest.version,
                    capabilities={"skills": manifest.skills, "capabilities": manifest.capabilities},
                )
                self._lifecycle.mark_ready(str(agent_id))
                # Soft marketplace overlay in metadata (get() returns dict)
                with getattr(self._lifecycle, "_lock", threading.RLock()):
                    internal = getattr(self._lifecycle, "_agents", {}).get(str(agent_id))
                    if internal is not None:
                        meta = dict(getattr(internal, "metadata", None) or {})
                        meta["marketplace_status"] = "READY"
                        meta["agent_key"] = manifest.name
                        meta["manifest_version"] = manifest.version
                        internal.metadata = meta
            except Exception as exc:  # noqa: BLE001
                logger.warning("lifecycle register skipped: %s", exc)

        self._persist_manifest(manifest, tenant_id=tenant_id, status=VERSION_ACTIVE if activate else VERSION_INACTIVE)
        self.stats["agent_registration_count"] += 1
        self._emit(
            "AGENT_REGISTERED",
            agent_id=str(agent_id),
            payload={"agent_key": manifest.name, "version": manifest.version, "tenant_id": tenant_id},
        )
        return {
            "registered": True,
            "agent_key": manifest.name,
            "version": manifest.version,
            "status": VERSION_ACTIVE if activate else VERSION_INACTIVE,
            "checksum": manifest.checksum(),
            "agent": agent_payload,
            "agent_id": str(agent_id),
            "skills": manifest.skills,
            "permissions": manifest.permissions,
        }

    def heartbeat(self, agent_id: str, **kwargs) -> dict[str, Any]:
        if self._lifecycle is not None:
            rec = self._lifecycle.heartbeat(agent_id, **kwargs)
            return rec.to_dict() if hasattr(rec, "to_dict") else dict(rec)
        return {"agent_id": agent_id, "status": "ok"}

    def unregister(self, agent_id: str, *, agent_key: str | None = None) -> dict[str, Any]:
        if self._lifecycle is not None:
            try:
                from execution.state_machine import AgentLifecycleState

                self._lifecycle.transition(agent_id, AgentLifecycleState.DRAINING)
                self._lifecycle.transition(agent_id, AgentLifecycleState.OFFLINE)
            except Exception as exc:  # noqa: BLE001
                logger.warning("lifecycle unregister: %s", exc)
        # Soft-revoke active version if known
        if agent_key and agent_key in self._active_versions:
            ver = self._active_versions.pop(agent_key, None)
            if ver and agent_key in self._manifests and ver in self._manifests[agent_key]:
                pass  # keep history; mark inactive via set_version_status
            if ver:
                self.set_version_status(agent_key, ver, VERSION_INACTIVE)
        self._emit("AGENT_DEACTIVATED", agent_id=agent_id, payload={"agent_key": agent_key})
        return {"unregistered": True, "agent_id": agent_id}

    def set_version_status(
        self,
        agent_key: str,
        version: str,
        status: str,
        *,
        force: bool = False,
        active_task_versions: set[str] | None = None,
    ) -> dict[str, Any]:
        status = status.upper()
        if status not in {VERSION_ACTIVE, VERSION_INACTIVE, VERSION_DEPRECATED, VERSION_REVOKED}:
            raise ValueError(f"invalid version status: {status}")
        if status == VERSION_REVOKED and not force:
            busy = active_task_versions or set()
            if version in busy:
                raise ValueError("cannot revoke version with active task dependencies")
        with self._lock:
            if agent_key not in self._manifests or version not in self._manifests[agent_key]:
                raise ValueError(f"manifest not found: {agent_key}@{version}")
            if status == VERSION_ACTIVE:
                self._active_versions[agent_key] = version
            elif self._active_versions.get(agent_key) == version and status != VERSION_ACTIVE:
                # demote active
                self._active_versions.pop(agent_key, None)
        self._emit(
            "AGENT_VERSION_PUBLISHED" if status == VERSION_ACTIVE else "AGENT_UPDATED",
            payload={"agent_key": agent_key, "version": version, "status": status},
        )
        return {"agent_key": agent_key, "version": version, "status": status}

    def list_versions(self, agent_key: str) -> list[dict[str, Any]]:
        versions = self._manifests.get(agent_key) or {}
        active = self._active_versions.get(agent_key)
        return [
            {
                "agent_key": agent_key,
                "version": v,
                "status": VERSION_ACTIVE if v == active else VERSION_INACTIVE,
                "checksum": m.checksum(),
                "skills": m.skills,
            }
            for v, m in sorted(versions.items())
        ]

    def get_manifest(self, agent_key: str, version: str | None = None) -> AgentManifest | None:
        versions = self._manifests.get(agent_key) or {}
        if version:
            return versions.get(version)
        active = self._active_versions.get(agent_key)
        if active and active in versions:
            return versions[active]
        if versions:
            return versions[sorted(versions.keys())[-1]]
        return None

    # ── Publish ───────────────────────────────────────────────────────────

    def publish(
        self,
        data: dict[str, Any],
        *,
        tenant_id: str | None = None,
        status: str = PKG_PUBLISHED,
    ) -> dict[str, Any]:
        tenant_id = tenant_id or self.tenant_id
        manifest = validate_manifest(data)
        # Security scan (checksum + permission + no shell without privileged)
        checksum = manifest.checksum()
        package_id = data.get("package_id") or f"pkg-{manifest.name}"
        pkg = {
            "package_id": package_id,
            "name": manifest.name,
            "description": manifest.description,
            "publisher": manifest.author or "unknown",
            "version": manifest.version,
            "skills": list(manifest.skills),
            "capabilities": list(manifest.capabilities),
            "default_endpoint": manifest.endpoint,
            "agent_key": manifest.name,
            "tags": list(manifest.metadata.get("tags") or []),
            "status": status,
            "kind": "agent",
            "tenant_id": tenant_id,
            "checksum": checksum,
            "manifest": manifest.to_dict(),
            "permissions": list(manifest.permissions),
            "requirements": dict(manifest.requirements),
            "dependencies": dict(manifest.dependencies),
        }
        with self._lock:
            self._packages[package_id] = pkg
            self._manifests.setdefault(manifest.name, {})[manifest.version] = manifest
        for sk in manifest.skills:
            self.skills.attach_provider(sk, manifest.name)
        self._persist_package(pkg)
        self._emit(
            "AGENT_VERSION_PUBLISHED",
            payload={"package_id": package_id, "version": manifest.version, "tenant_id": tenant_id},
        )
        return {"published": True, "package": pkg}

    def publish_status(self, package_id: str, status: str) -> dict[str, Any]:
        status = status.upper()
        if status not in {PKG_DRAFT, PKG_PUBLISHED, PKG_DEPRECATED, PKG_REVOKED}:
            raise ValueError(f"invalid package status: {status}")
        pkg = self._packages.get(package_id)
        if not pkg:
            raise ValueError(f"package not found: {package_id}")
        pkg["status"] = status
        self._emit("AGENT_UPDATED", payload={"package_id": package_id, "status": status})
        return {"package_id": package_id, "status": status}

    # ── Install / Activate ────────────────────────────────────────────────

    def install(
        self,
        *,
        package_id: str | None = None,
        endpoint: str | None = None,
        tenant_id: str | None = None,
        sandbox: str = "local_process",
        version: str | None = None,
    ) -> dict[str, Any]:
        tenant_id = tenant_id or self.tenant_id
        ep = (endpoint or "").strip()
        pkg = None
        if package_id:
            pkg = self._packages.get(package_id) or next(
                (p for p in CATALOG if p["package_id"] == package_id), None
            )
            if not pkg:
                raise ValueError(f"package not found: {package_id}")
            if pkg.get("status") == PKG_REVOKED:
                raise ValueError("cannot install REVOKED package")
            if not ep:
                ep = pkg.get("default_endpoint") or ""
            version = version or pkg.get("version") or "0.0.0"
        if not ep:
            raise ValueError("package_id or endpoint is required")

        agent_key = (pkg or {}).get("agent_key") or package_id or "unknown"
        install_key = f"{tenant_id}:{agent_key}:{version or 'latest'}"
        record = {
            "tenant_id": tenant_id,
            "package_id": package_id,
            "version": version or "latest",
            "agent_key": agent_key,
            "status": "INSTALLING",
            "endpoint": ep,
            "sandbox": sandbox,
            "error": None,
        }
        self._installations[install_key] = record

        try:
            # Verify manifest if published with one
            manifest_data = (pkg or {}).get("manifest")
            permissions = list((pkg or {}).get("permissions") or ["network", "other_agents"])
            requirements = dict((pkg or {}).get("requirements") or {})
            checksum = (pkg or {}).get("checksum")
            if manifest_data:
                m = validate_manifest(manifest_data)
                permissions = m.permissions
                requirements = m.requirements
                checksum = m.checksum()
                # dependency check
                deps = parse_dependencies(m.dependencies)
                if deps:
                    result = self.resolver.resolve(deps, self._available_versions())
                    if result.conflicts:
                        raise ValueError(f"dependency conflict: {result.conflicts}")

            # Sandbox — never execute remote source
            provider = get_sandbox_provider(sandbox)
            sb = provider.provision(
                SandboxSpec(
                    agent_key=agent_key,
                    version=str(version or "latest"),
                    endpoint=ep,
                    permissions=permissions,
                    requirements=requirements,
                    checksum=checksum,
                    source_url=(pkg or {}).get("source_url"),
                )
            )
            if not sb.ok:
                raise RuntimeError(sb.error or "sandbox provision failed")
            if not provider.health_check(sb.sandbox_id):
                raise RuntimeError("sandbox health check failed")

            record["status"] = "ACTIVATING"
            resp = httpx.post(
                f"{self.gateway_url}/v1/agents/register",
                json={"endpoint": ep},
                timeout=30.0,
            )
            if resp.status_code >= 300:
                raise RuntimeError(f"install failed: {resp.status_code} {resp.text}")
            data = resp.json()
            agent_id = data.get("id") or data.get("agent_id") or (data.get("agent") or {}).get("id")

            if self._lifecycle is not None and agent_id:
                try:
                    self._lifecycle.register(str(agent_id), version=str(version or ""))
                    self._lifecycle.mark_ready(str(agent_id))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("lifecycle on install: %s", exc)

            self.permissions.grant(tenant_id, agent_key, permissions)
            for sk in (pkg or {}).get("skills") or []:
                self.skills.attach_provider(sk, agent_key)

            record["status"] = "ACTIVE"
            record["agent_id"] = str(agent_id) if agent_id else None
            record["sandbox_id"] = sb.sandbox_id
            self.stats["agent_install_count"] += 1
            self.stats["agent_activation_count"] += 1
            self._emit(
                "AGENT_INSTALLED",
                agent_id=str(agent_id) if agent_id else None,
                payload={"package_id": package_id, "tenant_id": tenant_id, "agent_key": agent_key},
            )
            self._emit(
                "AGENT_ACTIVATED",
                agent_id=str(agent_id) if agent_id else None,
                payload={"package_id": package_id, "tenant_id": tenant_id},
            )
            return {
                "installed": True,
                "activated": True,
                "endpoint": ep,
                "package_id": package_id,
                "agent": data,
                "sandbox": sb.__dict__,
                "installation": record,
            }
        except Exception as exc:
            record["status"] = "FAILED"
            record["error"] = str(exc)
            self.stats["installation_failure_count"] += 1
            raise

    def activate(self, package_id: str, *, tenant_id: str | None = None) -> dict[str, Any]:
        tenant_id = tenant_id or self.tenant_id
        pkg = self._packages.get(package_id)
        if not pkg:
            raise ValueError(f"package not found: {package_id}")
        agent_key = pkg.get("agent_key") or package_id
        # find installation
        for key, inst in self._installations.items():
            if inst.get("package_id") == package_id and inst.get("tenant_id") == tenant_id:
                inst["status"] = "ACTIVE"
                self.stats["agent_activation_count"] += 1
                self._emit("AGENT_ACTIVATED", payload={"package_id": package_id, "agent_key": agent_key})
                return {"activated": True, "installation": inst}
        # not installed — install then activate
        return self.install(package_id=package_id, tenant_id=tenant_id)

    def deactivate(self, package_id: str, *, tenant_id: str | None = None) -> dict[str, Any]:
        tenant_id = tenant_id or self.tenant_id
        for inst in self._installations.values():
            if inst.get("package_id") == package_id and inst.get("tenant_id") == tenant_id:
                inst["status"] = "INACTIVE"
                agent_id = inst.get("agent_id")
                if agent_id and self._lifecycle is not None:
                    try:
                        from execution.state_machine import AgentLifecycleState

                        self._lifecycle.transition(str(agent_id), AgentLifecycleState.DRAINING)
                    except Exception:  # noqa: BLE001
                        pass
                self._emit("AGENT_DEACTIVATED", payload={"package_id": package_id})
                return {"deactivated": True, "installation": inst}
        raise ValueError(f"installation not found for {package_id}")

    # ── Skills ────────────────────────────────────────────────────────────

    def list_skills(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.skills.list()]

    def get_skill(self, name: str) -> dict[str, Any] | None:
        s = self.skills.get(name)
        return s.to_dict() if s else None

    def register_skill(self, data: dict[str, Any]) -> dict[str, Any]:
        skill = self.skills.register(data)
        self._emit("SKILL_PUBLISHED", payload={"name": skill.name, "version": skill.version})
        return skill.to_dict()

    def search_skills(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        t0 = time.perf_counter()
        results = self.skills.search(SkillSearchQuery(
            skill=query.get("skill") or query.get("name") or query.get("q"),
            capability=query.get("capability"),
            input=query.get("input"),
            output=query.get("output"),
            modality=query.get("modality"),
            resource=query.get("resource"),
            version=query.get("version"),
            tenant=query.get("tenant"),
            tags=query.get("tags"),
        ))
        latency = (time.perf_counter() - t0) * 1000
        self.stats["skill_discovery_count"] += 1
        self.stats["discovery_latency_ms_sum"] += latency
        self.stats["discovery_latency_ms_count"] += 1
        self.skills.discovery_count = self.stats["skill_discovery_count"]
        return [s.to_dict() for s in results]

    # ── Dynamic discovery ─────────────────────────────────────────────────

    def discover_by_skill(
        self,
        skill: str,
        *,
        version_constraint: str | None = None,
        agents: list[dict[str, Any]] | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Skill Discovery → Agent Discovery (candidates for Router/Scheduler)."""
        t0 = time.perf_counter()
        skill_hits = self.search_skills({"skill": skill, "version": version_constraint})
        providers: list[str] = []
        for s in skill_hits:
            providers.extend(s.get("providers") or [])
        # Also check skill registry attach
        providers.extend(self.skills.providers_for(skill))
        providers = list(dict.fromkeys(providers))

        candidates: list[dict[str, Any]] = []
        agent_list = agents if agents is not None else list(self._installed_by_key(tenant_id=tenant_id or self.tenant_id).values())
        for a in agent_list:
            key = a.get("agent_key") or a.get("name") or ""
            skills = set(a.get("skills") or [])
            if skill in skills or key in providers:
                # version constraint against manifest / agent version
                ver = str(a.get("version") or a.get("current_version") or "0.0.0")
                m = self.get_manifest(key)
                if m:
                    ver = m.version
                    if version_constraint and not satisfies(ver, version_constraint):
                        continue
                    if skill not in m.skills and skill not in skills:
                        continue
                elif version_constraint and not satisfies(ver, version_constraint):
                    continue
                candidates.append({**a, "matched_skill": skill, "matched_version": ver})

        latency = (time.perf_counter() - t0) * 1000
        self.stats["discovery_latency_ms_sum"] += latency
        self.stats["discovery_latency_ms_count"] += 1
        return {
            "skill": skill,
            "version_constraint": version_constraint,
            "skills": skill_hits,
            "providers": providers,
            "candidates": candidates,
            "latency_ms": round(latency, 3),
        }

    def resolve_invocation(
        self,
        skill: str,
        *,
        version_constraint: str | None = None,
        agents: list[dict[str, Any]] | None = None,
        scheduling: Any = None,
        tenant_id: str | None = None,
        exclude_agent_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Skill → Discovery → Capability → Scheduler selection (no hardcoded URL)."""
        disc = self.discover_by_skill(
            skill,
            version_constraint=version_constraint,
            agents=agents,
            tenant_id=tenant_id,
        )
        cands = disc["candidates"]
        selected = None
        decision = None
        if scheduling is not None and cands:
            req = {"skill": skill}
            if version_constraint:
                req["version_constraint"] = version_constraint
            decision = scheduling.select(
                cands,
                requirement=req,
                exclude_agent_ids=exclude_agent_ids,
            )
            selected = decision.get("selected") if isinstance(decision, dict) else getattr(decision, "selected", None)
        elif cands:
            selected = cands[0]
        self.stats["skill_invocation_count"] += 1
        return {
            "skill": skill,
            "discovery": disc,
            "selected": selected,
            "decision": decision if isinstance(decision, dict) else (
                decision.to_dict() if decision is not None and hasattr(decision, "to_dict") else None
            ),
        }

    # ── Internals ─────────────────────────────────────────────────────────

    def _gateway_register(self, endpoint: str) -> dict[str, Any]:
        resp = httpx.post(
            f"{self.gateway_url}/v1/agents/register",
            json={"endpoint": endpoint},
            timeout=30.0,
        )
        if resp.status_code >= 300:
            raise RuntimeError(f"registry register failed: {resp.status_code} {resp.text}")
        return resp.json()

    def _available_versions(self) -> dict[str, dict[str, list[str]]]:
        skills: dict[str, list[str]] = {}
        for s in self.skills.list():
            skills.setdefault(s.name, []).append(s.version)
        agents: dict[str, list[str]] = {}
        for key, vers in self._manifests.items():
            agents[key] = list(vers.keys())
        for pkg in self._packages.values():
            ak = pkg.get("agent_key")
            if ak:
                agents.setdefault(ak, []).append(str(pkg.get("version") or "0.0.0"))
        return {"skill": skills, "agent": agents, "model": {}, "tool": {}, "runtime": {"a2a": ["1.0.0"]}}

    def _persist_manifest(self, manifest: AgentManifest, *, tenant_id: str, status: str) -> None:
        try:
            with connect(self.database_url) as conn:
                conn.execute(
                    """
                    INSERT INTO a2a_agent_manifests
                      (agent_key, tenant_id, version, status, manifest, checksum, publisher, updated_at)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, now())
                    ON CONFLICT (tenant_id, agent_key, version) DO UPDATE SET
                      status = EXCLUDED.status,
                      manifest = EXCLUDED.manifest,
                      checksum = EXCLUDED.checksum,
                      updated_at = now()
                    """,
                    (
                        manifest.name,
                        tenant_id,
                        manifest.version,
                        status,
                        json.dumps(manifest.to_dict()),
                        manifest.checksum(),
                        manifest.author,
                    ),
                )
                for perm in manifest.permissions:
                    conn.execute(
                        """
                        INSERT INTO a2a_agent_permissions (tenant_id, agent_key, version, permission, granted)
                        VALUES (%s, %s, %s, %s, true)
                        ON CONFLICT (tenant_id, agent_key, version, permission) DO UPDATE SET granted = true
                        """,
                        (tenant_id, manifest.name, manifest.version, perm),
                    )
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("manifest persist skipped: %s", exc)

    def _persist_package(self, pkg: dict[str, Any]) -> None:
        try:
            with connect(self.database_url) as conn:
                row = conn.execute(
                    """
                    INSERT INTO a2a_marketplace_packages
                      (package_id, tenant_id, kind, name, description, publisher, status, latest_version, tags, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                    ON CONFLICT (tenant_id, package_id) DO UPDATE SET
                      status = EXCLUDED.status,
                      latest_version = EXCLUDED.latest_version,
                      description = EXCLUDED.description,
                      updated_at = now()
                    RETURNING id
                    """,
                    (
                        pkg["package_id"],
                        pkg.get("tenant_id") or self.tenant_id,
                        pkg.get("kind") or "agent",
                        pkg.get("name"),
                        pkg.get("description"),
                        pkg.get("publisher"),
                        pkg.get("status") or PKG_PUBLISHED,
                        pkg.get("version"),
                        json.dumps(pkg.get("tags") or []),
                        json.dumps({"agent_key": pkg.get("agent_key"), "checksum": pkg.get("checksum")}),
                    ),
                ).fetchone()
                if row and pkg.get("manifest"):
                    pkg_id = row["id"] if isinstance(row, dict) else row[0]
                    conn.execute(
                        """
                        INSERT INTO a2a_marketplace_versions
                          (package_ref, version, status, manifest, checksum, source_url)
                        VALUES (%s, %s, %s, %s::jsonb, %s, %s)
                        ON CONFLICT (package_ref, version) DO UPDATE SET
                          status = EXCLUDED.status,
                          manifest = EXCLUDED.manifest,
                          checksum = EXCLUDED.checksum
                        """,
                        (
                            pkg_id,
                            pkg.get("version"),
                            pkg.get("status") or PKG_PUBLISHED,
                            json.dumps(pkg.get("manifest")),
                            pkg.get("checksum"),
                            pkg.get("source_url"),
                        ),
                    )
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("package persist skipped: %s", exc)

    def _installed_by_key(self, *, tenant_id: str | None = None) -> dict[str, dict[str, Any]]:
        tenant_id = tenant_id or self.tenant_id
        sql = """
            SELECT a.id::text AS agent_id, a.agent_key, a.name, a.status,
                   a.current_version AS version,
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
        try:
            with connect(self.database_url) as conn:
                rows = conn.execute(sql, (tenant_id,)).fetchall()
            return {r["agent_key"]: dict(r) for r in rows}
        except Exception as exc:  # noqa: BLE001
            logger.debug("installed_by_key fallback: %s", exc)
            # fallback from installations memory
            out: dict[str, dict[str, Any]] = {}
            for inst in self._installations.values():
                if inst.get("tenant_id") == tenant_id and inst.get("status") == "ACTIVE":
                    out[inst["agent_key"]] = {
                        "agent_id": inst.get("agent_id"),
                        "agent_key": inst["agent_key"],
                        "status": "online",
                        "version": inst.get("version"),
                        "endpoint": inst.get("endpoint"),
                        "skills": (self._packages.get(inst.get("package_id") or "") or {}).get("skills") or [],
                    }
            return out

    def list_agents(self) -> list[dict[str, Any]]:
        return list(self._installed_by_key().values())
