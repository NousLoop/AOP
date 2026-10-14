"""Shared helpers for CLI harness runners."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Optional

from ..events import utc_now
from ..protocol import EventCallback, HarnessEvent, HarnessEventType

# Parent root when HARNESS_WORKDIR (and legacy product envs) are unset.
# Per-run cwd is nested as <root>/<task>/<agent>/ via resolve_run_workdir.
_DEFAULT_WORKDIR = Path.home() / ".aop" / "workspaces"


def resolve_harness_workdir(
    explicit: Optional[str] = None,
    *,
    legacy_env: tuple[str, ...] = (),
    create: bool = True,
) -> str:
    """Resolve parent workdir root for harness runners (not the per-run cwd).

    Priority:
    1. Constructor ``explicit`` path
    2. ``HARNESS_WORKDIR`` (unified)
    3. Product legacy envs (``CLAUDE_WORKDIR`` / ``DSH_WORKSPACE`` / ``PI_WORKDIR``)
    4. ``~/.aop/workspaces``

    Callers should pass this root into :func:`resolve_run_workdir` so each
    ``task × agent`` gets an isolated subdirectory.
    """
    candidates: list[str] = []
    if explicit is not None and str(explicit).strip():
        candidates.append(str(explicit).strip())
    shared = (os.getenv("HARNESS_WORKDIR") or "").strip()
    if shared:
        candidates.append(shared)
    for name in legacy_env:
        val = (os.getenv(name) or "").strip()
        if val:
            candidates.append(val)
    if not candidates:
        candidates.append(str(_DEFAULT_WORKDIR))

    path = Path(candidates[0]).expanduser().resolve()
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _sanitize_path_segment(value: str, *, fallback: str = "unknown") -> str:
    """Keep a single path segment filesystem-safe (no separators / traversal)."""
    raw = (value or "").strip()
    if not raw:
        return fallback
    # Drop path separators and windows drive markers; collapse other junk.
    cleaned = []
    for ch in raw:
        if ch.isalnum() or ch in {"-", "_", ".", "="}:
            cleaned.append(ch)
        elif ch in {"/", "\\", ":"}:
            cleaned.append("_")
        else:
            cleaned.append("_")
    out = "".join(cleaned).strip("._")
    if not out or out in {".", ".."}:
        return fallback
    # Avoid absurdly long directory names (UUID+uuid still fine).
    return out[:120]


def resolve_run_workdir(
    base: Optional[str] = None,
    *,
    task_id: str,
    agent_id: str,
    root_task_id: Optional[str] = None,
    create: bool = True,
) -> str:
    """Resolve per-run cwd: ``<base>/<root_or_task>/<agent>/``.

    ``base`` is typically from :func:`resolve_harness_workdir`. Prefer
    ``root_task_id`` (platform / OS task) over the A2A invocation ``task_id``.
    """
    root = Path(base or resolve_harness_workdir()).expanduser().resolve()
    task_seg = _sanitize_path_segment(
        str(root_task_id or task_id or ""),
        fallback="no-task",
    )
    agent_seg = _sanitize_path_segment(str(agent_id or ""), fallback="agent")
    path = root / task_seg / agent_seg
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return str(path)


def workdir_from_message(
    base: Optional[str],
    *,
    task_id: str,
    message: Optional[dict[str, Any]] = None,
    default_agent_id: Optional[str] = None,
) -> str:
    """Convenience: pull rootTaskId / agentId from message metadata then nest."""
    meta = message.get("metadata") if isinstance(message, dict) else None
    if not isinstance(meta, dict):
        meta = {}
    root = (
        meta.get("rootTaskId")
        or meta.get("root_task_id")
        or meta.get("correlationId")
        or meta.get("correlation_id")
    )
    agent = (
        meta.get("agentId")
        or meta.get("agent_id")
        or default_agent_id
        or os.getenv("HARNESS_PROFILE")
        or os.getenv("AGENT_ID")
        or "agent"
    )
    return resolve_run_workdir(
        base,
        task_id=task_id,
        agent_id=str(agent),
        root_task_id=str(root) if root else None,
    )


def message_text(message: dict[str, Any]) -> str:
    """Extract user text from an A2A message (tolerant of type/kind variants)."""
    if not isinstance(message, dict):
        return str(message or "").strip()

    # Top-level shortcuts some clients use
    for key in ("text", "content", "prompt"):
        val = message.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    parts = message.get("parts") or []
    texts: list[str] = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        kind = str(p.get("type") or p.get("kind") or "").lower()
        if kind and kind not in {"text", "input_text", "plain_text"}:
            continue
        chunk = p.get("text")
        if chunk is None and isinstance(p.get("content"), str):
            chunk = p.get("content")
        if isinstance(chunk, str) and chunk.strip():
            texts.append(chunk.strip())
        elif kind == "" and isinstance(p.get("text"), str) and p["text"].strip():
            # Missing type but has text — still accept
            texts.append(p["text"].strip())
    return "\n".join(texts).strip()


def system_prompt_from_message(message: dict[str, Any]) -> str:
    meta = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    return str((meta or {}).get("systemPrompt") or "").strip()


def resolve_binary(name_or_path: str) -> Optional[str]:
    if not name_or_path:
        return None
    found = shutil.which(name_or_path)
    if found:
        return found
    if os.path.isfile(name_or_path):
        return name_or_path
    return None


def openai_compatible_api_key(*extra_env: str) -> str:
    """First non-empty API key from common OpenAI-compatible env vars."""
    keys = list(extra_env) + [
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "PI_API_KEY",
    ]
    for name in keys:
        val = (os.getenv(name) or "").strip()
        if val:
            return val
    return ""


def skill_label(skill_id: str | None) -> str:
    """Return a real skill id for prompt framing, or empty when skill-less."""
    s = (skill_id or "").strip()
    if not s or s.lower() in {"default", "none", "-"}:
        return ""
    return s


def compose_cli_prompt(
    prompt: str,
    *,
    system: str = "",
    skill_id: str | None = None,
) -> str:
    """Build CLI user prompt; omit ``(skill=…)`` when the card has no skills."""
    label = skill_label(skill_id)
    task = f"User task (skill={label}):\n{prompt}" if label else f"User task:\n{prompt}"
    sys = (system or "").strip()
    if sys:
        return f"{sys}\n\n---\n\n{task}"
    if label:
        return task
    return prompt


def system_with_skill(system: str, skill_id: str | None = None) -> str:
    """Append Active skill only when a real skill id is present."""
    sys = (system or "").strip()
    label = skill_label(skill_id)
    if not label:
        return sys
    if sys:
        return f"{sys}\n\nActive skill: {label}"
    return f"Active skill: {label}"


def run_openai_compatible_sync(
    *,
    prompt: str,
    system: str,
    skill_id: str,
    api_key: str,
    base_url: str,
    model: str,
) -> tuple[str, Any]:
    """Blocking chat completion via llm_provider OpenAIProvider."""
    from llm_provider import LLMMessage, LLMProviderConfig, LLMRequest, OpenAIProvider

    from ..cost import estimate_cost
    from ..protocol import TokenUsage

    cfg = LLMProviderConfig(api_key=api_key, base_url=base_url, model=model)
    provider = OpenAIProvider(cfg)
    sys_content = system_with_skill(system, skill_id) or "You are a helpful assistant."
    request = LLMRequest(
        messages=[
            LLMMessage(role="system", content=sys_content),
            LLMMessage(role="user", content=prompt),
        ],
        model=model,
    )
    response = provider.chat_completion(request)
    u = response.usage or {}
    usage = TokenUsage(
        input_tokens=int(u.get("prompt_tokens") or 0),
        output_tokens=int(u.get("completion_tokens") or 0),
        model=model,
    )
    if usage.input_tokens or usage.output_tokens:
        usage.estimated_cost = estimate_cost(
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens
        )
    text = str(response.content or "")
    if not text:
        raise RuntimeError("empty LLM response")
    return text, usage


async def emit_event(
    on_event: EventCallback,
    *,
    task_id: str,
    etype: HarnessEventType,
    payload: dict[str, Any],
) -> None:
    ev = HarnessEvent(type=etype, task_id=task_id, timestamp=utc_now(), payload=payload)
    maybe = on_event(ev)
    if asyncio.iscoroutine(maybe):
        await maybe


_BALANCE_MSG = "余额不足"


def provider_balance_error(*parts: object) -> str | None:
    """Detect provider 402 / insufficient-balance in CLI or A2A text.

    Claude Code prints ``API Error: 402 Insufficient Balance (request_id: …)``
    on stdout and often still exits 0; that must not be treated as success.
    """
    blob = "\n".join(str(p) for p in parts if p is not None and str(p).strip())
    if not blob:
        return None
    if _BALANCE_MSG in blob:
        return _BALANCE_MSG
    lower = blob.lower()
    if "insufficient balance" in lower or "insufficient_quota" in lower:
        return _BALANCE_MSG
    if "payment required" in lower:
        return _BALANCE_MSG
    if re.search(r"\b402\b", blob) and (
        "insufficient" in lower
        or "api error" in lower
        or "payment required" in lower
        or "insufficient_quota" in lower
    ):
        return _BALANCE_MSG
    return None


def harness_output_error(*parts: object) -> str | None:
    """Treat CLI/A2A payloads that are clearly failed as failed, even if exit 0."""
    balance = provider_balance_error(*parts)
    if balance:
        return balance
    blob = "\n".join(str(p) for p in parts if p is not None and str(p).strip())
    if not blob:
        return None
    stripped = blob.strip()
    lower = stripped.lower()
    if stripped.startswith("{") and ("ok" in lower or "status" in lower):
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            if obj.get("ok") is False:
                return str(obj.get("error") or obj.get("final") or obj.get("status") or "agent error")[:400]
            st = str(obj.get("status") or "").lower()
            if st in {"error", "failed"}:
                return str(obj.get("error") or obj.get("final") or st)[:400]
        elif '"ok": false' in lower or '"ok":false' in lower:
            return "agent error"
    if "api call failed" in lower:
        return stripped.split("\n", 1)[0][:400]
    if "returned empty output" in lower:
        return stripped.split("\n", 1)[0][:400]
    if (
        "# aop hosted result" in lower
        or "platform-hosted runner" in lower
        or "no external cli" in lower
        or "hosted stub" in lower
        or "未执行真实推理" in stripped
    ):
        return "未执行真实推理（hosted stub）"
    return None


def apply_provider_balance_failure(result: Any) -> Any:
    """If ``result`` carries a 402/balance or other obvious CLI failure, coerce failed."""
    from ..protocol import HarnessStatus

    extra = None
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        extra = " ".join(
            str(data.get(k) or "")
            for k in ("error", "stderr", "message", "status", "ok")
        )
        if data.get("ok") is False:
            extra = extra + " ok=false"
        if data.get("stub") is True or (
            data.get("hosted") is True and not data.get("model_invoked")
        ):
            extra = extra + " hosted stub"
    msg = harness_output_error(
        getattr(result, "error", None),
        getattr(result, "text", None),
        extra,
    )
    if not msg:
        return result
    result.status = HarnessStatus.FAILED
    result.error = msg
    return result


# Default asyncio.StreamReader limit is 64KiB; Pi/Claude JSONL tool payloads often exceed it.
_CLI_STREAM_LIMIT = int(os.getenv("AOP_CLI_STREAM_LIMIT", str(16 * 1024 * 1024)))
_CLI_MAX_LINE = int(os.getenv("AOP_CLI_MAX_LINE", str(32 * 1024 * 1024)))


def bump_stream_limit(stream: Optional[asyncio.StreamReader], *, limit: int | None = None) -> None:
    """Raise StreamReader buffer limit so readline/readuntil survive large JSONL lines."""
    if stream is None:
        return
    target = int(limit if limit is not None else _CLI_STREAM_LIMIT)
    try:
        current = int(getattr(stream, "_limit", 0) or 0)
        if current < target:
            stream._limit = target  # noqa: SLF001 — public API has no setter
    except Exception:  # noqa: BLE001
        pass


async def readline_unlimited(stream: asyncio.StreamReader) -> bytes:
    """Read one line; recover from LimitOverrunError on huge tool/JSON payloads."""
    parts: list[bytes] = []
    while True:
        try:
            bump_stream_limit(stream)
            data = await stream.readuntil(b"\n")
            parts.append(data)
            return b"".join(parts)
        except asyncio.LimitOverrunError as exc:
            # Data without separator is still buffered — drain ``consumed`` and continue.
            piece = await stream.readexactly(exc.consumed)
            parts.append(piece)
            if sum(len(p) for p in parts) > _CLI_MAX_LINE:
                raise ValueError(
                    f"CLI stdout line exceeds {_CLI_MAX_LINE} bytes"
                ) from exc
        except asyncio.IncompleteReadError as exc:
            parts.append(exc.partial or b"")
            return b"".join(parts)
        except ValueError as exc:
            # Older asyncio wraps LimitOverrunError as ValueError from readline().
            msg = str(exc).lower()
            if "chunk exceed the limit" in msg or "separator is not found" in msg:
                # Fall back: read whatever is buffered in small chunks until newline/EOF.
                while True:
                    chunk = await stream.read(65536)
                    if not chunk:
                        return b"".join(parts)
                    parts.append(chunk)
                    if b"\n" in chunk or sum(len(p) for p in parts) > _CLI_MAX_LINE:
                        return b"".join(parts)
            raise
