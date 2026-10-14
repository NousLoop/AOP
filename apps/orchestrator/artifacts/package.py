"""Shareable run package: artifacts + citations + markdown report."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime
from typing import Any, Callable, Optional

MAX_PACKAGE_BYTES = 40_000_000
MAX_PACKAGE_FILES = 48


def _safe_part(raw: str, fallback: str = "item") -> str:
    s = (raw or "").replace("\\", "/").split("/")[-1].strip()
    out = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in s)
    return (out.strip("-.") or fallback)[:80]


def _task_id(task: dict[str, Any] | None) -> str:
    row = task or {}
    return str(row.get("task_id") or row.get("id") or "task")


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _dumps(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=_json_default).encode("utf-8")


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _goal(task: dict[str, Any] | None) -> str:
    row = task or {}
    inp = row.get("input_json") if isinstance(row.get("input_json"), dict) else {}
    plan = row.get("plan_json") if isinstance(row.get("plan_json"), dict) else {}
    return str(
        inp.get("content")
        or plan.get("goal")
        or row.get("title")
        or ""
    ).strip()


def build_citations(
    task: dict[str, Any] | None,
    *,
    artifacts: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
    evaluation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = task or {}
    nodes = row.get("nodes") if isinstance(row.get("nodes"), list) else []
    agents: list[dict[str, Any]] = []
    seen: set[str] = set()
    for n in nodes:
        if not isinstance(n, dict):
            continue
        key = str(n.get("agent_key") or n.get("agent_key") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        agents.append(
            {
                "agent_key": key,
                "agent_id": n.get("agent_id"),
                "name": n.get("agent_name") or key,
                "node_id": n.get("id") or n.get("node_key"),
            }
        )
    arts = []
    for a in artifacts or []:
        if not isinstance(a, dict):
            continue
        arts.append(
            {
                "name": a.get("name"),
                "uri": a.get("uri"),
                "url": a.get("url"),
                "node_id": a.get("node_id"),
                "mime_type": a.get("mime_type"),
                "type": a.get("type"),
                "size": a.get("size"),
            }
        )
    evs = []
    for e in events or []:
        if not isinstance(e, dict):
            continue
        evs.append(
            {
                "event_type": e.get("event_type") or e.get("type"),
                "message": e.get("message"),
                "ts": e.get("ts") or e.get("timestamp"),
                "node_id": (e.get("payload") or {}).get("node_id")
                if isinstance(e.get("payload"), dict)
                else e.get("node_id"),
            }
        )
    return {
        "task_id": _task_id(row),
        "title": row.get("title"),
        "status": row.get("status"),
        "goal": _goal(row),
        "created_at": _iso(row.get("created_at")),
        "finished_at": _iso(row.get("finished_at")),
        "agents": agents,
        "evaluation": evaluation,
        "artifacts": arts,
        "events": evs,
    }


def build_run_report_markdown(citations: dict[str, Any]) -> str:
    ev = citations.get("evaluation") if isinstance(citations.get("evaluation"), dict) else {}
    score = ev.get("score")
    grade = ev.get("grade")
    lines = [
        f"# AOP run report",
        "",
        f"- **Task:** `{citations.get('task_id')}`",
        f"- **Status:** {citations.get('status') or '—'}",
        f"- **Title:** {citations.get('title') or '—'}",
        f"- **Created:** {citations.get('created_at') or '—'}",
        f"- **Finished:** {citations.get('finished_at') or '—'}",
    ]
    if grade is not None or score is not None:
        lines.append(f"- **Evaluation:** {grade or '—'} ({score if score is not None else '—'})")
    goal = citations.get("goal") or ""
    if goal:
        lines.extend(["", "## Goal", "", goal, ""])
    agents = citations.get("agents") or []
    if agents:
        lines.extend(["## Agents", ""])
        for a in agents:
            lines.append(
                f"- `{a.get('agent_key')}`"
                + (f" · {a.get('name')}" if a.get("name") and a.get("name") != a.get("agent_key") else "")
            )
        lines.append("")
    arts = citations.get("artifacts") or []
    if arts:
        lines.extend(["## Artifacts", ""])
        for a in arts:
            loc = a.get("url") or a.get("uri") or ""
            label = a.get("name") or loc or "artifact"
            if loc:
                lines.append(f"- [{label}]({loc})")
            else:
                lines.append(f"- {label}")
        lines.append("")
    evs = citations.get("events") or []
    if evs:
        lines.extend(["## Timeline", ""])
        for e in evs[:80]:
            ts = e.get("ts") or ""
            kind = e.get("event_type") or "event"
            msg = e.get("message") or ""
            lines.append(f"- `{ts}` **{kind}** {msg}".rstrip())
        lines.append("")
    lines.extend(
        [
            "---",
            "Generated by AOP. Bundle also contains `citations.json` and copied artifact files.",
            "",
        ]
    )
    return "\n".join(lines)


def build_package_zip(
    task: dict[str, Any] | None,
    *,
    artifacts: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
    evaluation: dict[str, Any] | None = None,
    fetch_bytes: Optional[Callable[[str], Optional[bytes]]] = None,
) -> bytes:
    """Zip run-report.md + citations.json + artifact files (best-effort)."""
    citations = build_citations(
        task, artifacts=artifacts, events=events, evaluation=evaluation
    )
    report = build_run_report_markdown(citations)
    buf = io.BytesIO()
    used = 0
    files = 0
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("run-report.md", report.encode("utf-8"))
        zf.writestr(
            "citations.json",
            _dumps(citations),
        )
        files += 2
        for a in artifacts or []:
            if files >= MAX_PACKAGE_FILES:
                break
            if not isinstance(a, dict):
                continue
            uri = str(a.get("uri") or "")
            body: bytes | None = None
            if fetch_bytes and uri:
                try:
                    body = fetch_bytes(uri)
                except Exception:  # noqa: BLE001
                    body = None
            if not body:
                continue
            if used + len(body) > MAX_PACKAGE_BYTES:
                continue
            node = _safe_part(str(a.get("node_id") or "node"), "node")
            name = _safe_part(str(a.get("name") or uri.split("/")[-1] or "artifact"))
            arc = f"artifacts/{node}/{name}"
            zf.writestr(arc, body)
            used += len(body)
            files += 1
        zf.writestr(
            "manifest.json",
            _dumps(
                {
                    "task_id": citations.get("task_id"),
                    "files": files,
                    "artifact_bytes": used,
                }
            ),
        )
    return buf.getvalue()
