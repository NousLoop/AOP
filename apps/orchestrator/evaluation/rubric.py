"""Report-aware task score.

A finished task is not an A by default. When a report node exists, most of the
grade comes from the document: structure, prose, citations, and layout.
Execution only confirms the pipeline actually finished.
"""

from __future__ import annotations

import json
import re
from typing import Any


def score_task(task: dict[str, Any], nodes: list[dict[str, Any]]) -> dict[str, Any]:
    status = (task.get("status") or "").lower()
    total = len(nodes) or 1
    success = sum(1 for node in nodes if node.get("status") == "success")
    failed = sum(1 for node in nodes if node.get("status") == "failed")
    execution = _execution_score(status, success, total, failed)

    report = _report_payload(nodes)
    if report is None:
        score = execution * 0.7 + (20 if status == "completed" else 0)
        if status == "completed":
            score = min(score, 89.0)
        score = round(max(0.0, min(100.0, score)), 1)
        dims = {"execution": round(execution, 1)}
        return _pack(score, dims, status, success, total, failed, note="no report node")

    html = report["html"]
    goal = report["goal"]
    dims = {
        "execution": round(execution, 1),
        "structure": round(_structure_score(html), 1),
        "prose": round(_prose_score(html, goal), 1),
        "citations": round(_citation_score(html), 1),
        "layout": round(_layout_score(html, goal), 1),
    }
    score = round(
        dims["execution"] * 0.15
        + dims["structure"] * 0.20
        + dims["prose"] * 0.30
        + dims["citations"] * 0.15
        + dims["layout"] * 0.20,
        1,
    )
    return _pack(score, dims, status, success, total, failed, note="report rubric")


def _pack(
    score: float,
    dims: dict[str, float],
    status: str,
    success: int,
    total: int,
    failed: int,
    *,
    note: str,
) -> dict[str, Any]:
    grade = _grade(score)
    weakest = min(dims, key=dims.get) if dims else "execution"
    weakest_label = _LABELS.get(weakest, weakest)
    return {
        "score": score,
        "grade": grade,
        "dimensions": dims,
        "summary": (
            f"Grade {grade} ({score}). 最弱项 {weakest_label}={dims.get(weakest, 0)}. "
            f"status={status}, nodes={success}/{total} ok."
        ),
        "details": {
            "status": status,
            "nodes_total": total,
            "nodes_success": success,
            "nodes_failed": failed,
            "rubric": note,
            "weakest": weakest,
        },
    }


_LABELS = {
    "execution": "执行",
    "structure": "结构",
    "prose": "行文",
    "citations": "引用",
    "layout": "版式",
}


def _grade(score: float) -> str:
    if score >= 90:
        return "A"
    if score >= 75:
        return "B"
    if score >= 60:
        return "C"
    if score >= 40:
        return "D"
    return "F"


def _execution_score(status: str, success: int, total: int, failed: int) -> float:
    rate = success / total
    if status == "completed":
        base = 70.0
    elif status == "failed":
        base = 30.0
    elif status == "cancelled":
        base = 15.0
    else:
        base = 40.0
    return max(0.0, min(100.0, base * 0.4 + rate * 60.0 - failed * 8.0))


def _report_payload(nodes: list[dict[str, Any]]) -> dict[str, str] | None:
    for node in nodes:
        output = node.get("output_json") or {}
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except json.JSONDecodeError:
                output = {}
        if not isinstance(output, dict):
            continue
        data = output.get("data") if isinstance(output.get("data"), dict) else {}
        files = data.get("files") if isinstance(data.get("files"), dict) else {}
        html = files.get("report.html")
        if not isinstance(html, str) or not html.strip():
            continue
        goal = str(data.get("query") or "")
        return {"html": html, "goal": goal}
    return None


def _text(html: str) -> str:
    stripped = re.sub(r"<style[\s\S]*?</style>", " ", html or "", flags=re.I)
    stripped = re.sub(r"<[^>]+>", " ", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


def _structure_score(html: str) -> float:
    text = _text(html)
    needed = ("摘要", "发现", "分析", "结论")
    hits = sum(1 for token in needed if token in text)
    score = hits / len(needed) * 70
    if len(text) >= 220:
        score += 30
    elif len(text) >= 120:
        score += 15
    return min(100.0, score)


def _prose_score(html: str, goal: str) -> float:
    text = _text(html)
    score = 75.0
    goal_key = re.sub(r"\s+", "", goal or "")
    body = re.sub(r"\s+", "", text)
    if goal_key and goal_key in body and len(goal_key) > 12:
        score -= 30
    if "上游没有" in text or "没有可绘制" in text:
        score -= 25
    headings = re.findall(r"<h2>([^<]+)</h2>", html or "")
    if headings and len(headings) != len(set(headings)):
        score -= 15
    paragraphs = re.findall(r"<p[^>]*>([^<]{12,})</p>", html or "")
    if len(paragraphs) < 4:
        score -= 15
    else:
        score += 15
    return max(0.0, min(100.0, score))


def _citation_score(html: str) -> float:
    text = _text(html)
    urls = re.findall(r"https?://", html or "")
    named = len(re.findall(r"<ol class='refs'>.*?</ol>", html or "", flags=re.S))
    items = len(re.findall(r"<ol class='refs'>[\s\S]*?<li>", html or ""))
    if urls:
        return min(100.0, 55 + len(urls) * 10)
    if items:
        return min(80.0, 40 + items * 8)
    if named or "References" in text or "参考文献" in text:
        return 35.0
    return 10.0


def _layout_score(html: str, goal: str) -> float:
    score = 20.0
    if "<style" in (html or ""):
        score += 35
    if "class='sheet'" in (html or "") or 'class="sheet"' in (html or ""):
        score += 15
    if "class='toc'" in (html or "") or 'class="toc"' in (html or ""):
        score += 10
    if "没有可绘制" in (html or ""):
        score -= 25
    h1 = re.search(r"<h1>([^<]*)</h1>", html or "")
    title = h1.group(1).strip() if h1 else ""
    if title and title != (goal or "").strip():
        score += 15
    elif title:
        score += 5
    return max(0.0, min(100.0, score))
