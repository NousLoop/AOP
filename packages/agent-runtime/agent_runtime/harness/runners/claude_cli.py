"""Claude Code CLI harness runner (``claude -p --output-format stream-json``)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from typing import Any, Optional

from ..cost import estimate_cost
from ..events import utc_now
from ..process import ProcessSupervisor
from ..protocol import (
    EventCallback,
    HarnessEvent,
    HarnessEventType,
    HarnessResult,
    HarnessStatus,
    TokenUsage,
)
from ._cli_common import (
    apply_provider_balance_failure,
    bump_stream_limit,
    compose_cli_prompt,
    readline_unlimited,
    resolve_harness_workdir,
    workdir_from_message,
)
from ..preview import (
    build_preview_user_prompt,
    claude_tui_argv,
    collect_preview_artifacts,
    ensure_claude_workspace_trusted,
    harness_preview_mode,
    prepare_preview_workspace,
    run_preview_and_collect,
    should_use_live_preview,
)

logger = logging.getLogger(__name__)


async def _ok_or_balance(result: HarnessResult, emit) -> HarnessResult:
    result = apply_provider_balance_failure(result)
    if result.status != HarnessStatus.OK:
        await emit(HarnessEventType.ERROR, {"error": result.error})
        return result
    await emit(HarnessEventType.FINAL, {"state": "completed"})
    return result


def _message_text(message: dict[str, Any]) -> str:
    parts = message.get("parts") or []
    texts = [str(p.get("text") or "") for p in parts if p.get("type") == "text"]
    return "\n".join(t for t in texts if t).strip()


def _parse_usage(obj: dict[str, Any]) -> Optional[TokenUsage]:
    usage = obj.get("usage") or obj.get("token_usage") or {}
    if not isinstance(usage, dict):
        return None
    inp = int(usage.get("input_tokens") or usage.get("inputTokens") or usage.get("prompt_tokens") or 0)
    out = int(usage.get("output_tokens") or usage.get("outputTokens") or usage.get("completion_tokens") or 0)
    model = obj.get("model") or usage.get("model")
    if not inp and not out and not model:
        return None
    return TokenUsage(
        input_tokens=inp,
        output_tokens=out,
        model=str(model) if model else None,
        estimated_cost=estimate_cost(input_tokens=inp, output_tokens=out),
    )


def parse_stream_json_line(line: str) -> dict[str, Any] | None:
    line = (line or "").strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return {"type": "text_delta", "text": line}
    if isinstance(obj, dict):
        return obj
    return {"type": "text_delta", "text": str(obj)}


class ClaudeCliRunner:
    """Invoke Claude Code CLI in print/stream-json mode."""

    def __init__(
        self,
        *,
        binary: Optional[str] = None,
        timeout_s: float = 1800.0,
        extra_args: Optional[list[str]] = None,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        supervisor: Optional[ProcessSupervisor] = None,
    ):
        self.binary = binary or os.getenv("CLAUDE_CLI_PATH") or "claude"
        self.timeout_s = float(os.getenv("CLAUDE_CLI_TIMEOUT_S") or timeout_s)
        self.extra_args = list(extra_args or [])
        self.base_workdir = resolve_harness_workdir(cwd, legacy_env=("CLAUDE_WORKDIR",))
        self.env = env
        self.supervisor = supervisor or ProcessSupervisor()

    @property
    def cwd(self) -> str:
        """Parent workdir root (per-run cwd is nested under task × agent)."""
        return self.base_workdir

    def resolve_binary(self) -> Optional[str]:
        path = shutil.which(self.binary) if self.binary else None
        if path:
            return path
        # Allow absolute path without which()
        if self.binary and os.path.isfile(self.binary):
            return self.binary
        return None

    def readiness(self) -> dict[str, Any]:
        if self.resolve_binary():
            return {"ready": True, "mode": "cli"}
        return {
            "ready": False,
            "mode": None,
            "reason": f"Claude CLI not found ({self.binary}). Install Claude Code or set CLAUDE_CLI_PATH.",
        }

    async def cancel(self, task_id: str) -> bool:
        return await self.supervisor.cancel(task_id)

    async def run(
        self,
        *,
        task_id: str,
        message: dict[str, Any],
        skill_id: str,
        on_event: EventCallback,
    ) -> HarnessResult:
        started = time.monotonic()
        prompt = _message_text(message)
        meta = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        system = str((meta or {}).get("systemPrompt") or "").strip()
        if system:
            prompt = compose_cli_prompt(prompt, system=system, skill_id=skill_id)

        async def _emit(etype: HarnessEventType, payload: dict[str, Any]) -> None:
            ev = HarnessEvent(type=etype, task_id=task_id, timestamp=utc_now(), payload=payload)
            maybe = on_event(ev)
            if asyncio.iscoroutine(maybe):
                await maybe

        binary = self.resolve_binary()
        if not binary:
            err = f"Claude CLI not found ({self.binary}). Set CLAUDE_CLI_PATH or install Claude Code."
            await _emit(HarnessEventType.ERROR, {"error": err})
            return HarnessResult(
                text="",
                status=HarnessStatus.FAILED,
                error=err,
                skill_id=skill_id,
                data={"runner": "ClaudeCliRunner", "available": False},
            )

        await _emit(HarnessEventType.STATUS, {"state": "working", "message": "starting claude cli"})

        # Headless harness: grant tools by default (WebSearch/WebFetch etc.).
        # Override via CLAUDE_CLI_PERMISSION_MODE / CLAUDE_CLI_ALLOWED_TOOLS.
        permission_mode = (
            os.getenv("CLAUDE_CLI_PERMISSION_MODE") or "bypassPermissions"
        ).strip()
        allowed_tools = (
            os.getenv("CLAUDE_CLI_ALLOWED_TOOLS")
            or "WebSearch,WebFetch,Bash,Read,Edit,Write,Glob,Grep,Agent,Skill,TodoWrite"
        ).strip()

        argv = [
            binary,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            permission_mode,
        ]
        if allowed_tools and allowed_tools.lower() not in {"-", "none", "off"}:
            argv.extend(["--allowed-tools", allowed_tools])
        argv.extend(self.extra_args)
        env = dict(self.env or os.environ.copy())

        usage = TokenUsage()
        chunks: list[str] = []
        stderr_bits: list[str] = []
        canceled = False
        run_cwd = workdir_from_message(self.base_workdir, task_id=task_id, message=message)

        if should_use_live_preview():
            use_tui = harness_preview_mode() == "tui"
            user_goal = _message_text(message)
            preview_prompt = (
                build_preview_user_prompt(user_goal, system=system, skill_id=skill_id)
                if use_tui
                else prompt
            )
            if use_tui:
                prepare_preview_workspace(run_cwd, user_goal=user_goal, prompt=preview_prompt)
                ensure_claude_workspace_trusted(run_cwd)
                argv = claude_tui_argv(
                    preview_prompt,
                    binary=binary,
                    permission_mode=permission_mode,
                    allowed_tools=allowed_tools,
                    extra_args=list(self.extra_args),
                )
                # Unknown custom model ids (e.g. deepseek-v4-pro) trip a catalog
                # warning; don't block the TUI on assumed 200k windows.
                env.setdefault("CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT", "1")
            await _emit(
                HarnessEventType.STATUS,
                {
                    "state": "working",
                    "message": f"live preview: claude ({'tui' if use_tui else 'exec'} console)",
                },
            )

            async def _on_line(line: str) -> None:
                obj = parse_stream_json_line(line)
                if obj:
                    await self._handle_obj(obj, chunks, usage, _emit)
                elif line.strip():
                    chunks.append(line)
                    await _emit(HarnessEventType.DELTA, {"text": line})

            try:
                rc, raw, killed = await asyncio.wait_for(
                    run_preview_and_collect(
                        self.supervisor,
                        task_id,
                        cwd=run_cwd,
                        argv=argv,
                        env=env,
                        timeout_s=self.timeout_s,
                        on_line=None if use_tui else _on_line,
                        interactive=use_tui,
                    ),
                    timeout=self.timeout_s + 5,
                )
            except asyncio.TimeoutError:
                await self.supervisor.cancel(task_id)
                usage.wall_time_ms = int((time.monotonic() - started) * 1000)
                await _emit(HarnessEventType.ERROR, {"error": "claude cli timeout"})
                return HarnessResult(
                    text="".join(chunks) or collect_preview_artifacts(run_cwd),
                    status=HarnessStatus.TIMEOUT,
                    error=f"timeout after {self.timeout_s}s",
                    skill_id=skill_id,
                    usage=usage,
                    data={"runner": "ClaudeCliRunner", "livePreview": True, "tui": use_tui},
                )
            usage.wall_time_ms = int((time.monotonic() - started) * 1000)
            if usage.estimated_cost <= 0 and (usage.input_tokens or usage.output_tokens):
                usage.estimated_cost = estimate_cost(
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                )
            await _emit(HarnessEventType.USAGE, usage.to_dict())
            text = (
                collect_preview_artifacts(run_cwd, fallback=raw)
                if use_tui
                else "".join(chunks).strip()
            )
            if killed or (rc not in (0, None) and not text):
                err = f"claude exited with code {rc}"
                status = HarnessStatus.CANCELED if killed else HarnessStatus.FAILED
                await _emit(
                    HarnessEventType.ERROR
                    if status == HarnessStatus.FAILED
                    else HarnessEventType.STATUS,
                    {"error": err}
                    if status == HarnessStatus.FAILED
                    else {"state": "canceled"},
                )
                return HarnessResult(
                    text=text,
                    status=status,
                    error=err,
                    skill_id=skill_id,
                    usage=usage,
                    data={"runner": "ClaudeCliRunner", "exitCode": rc, "livePreview": True},
                )
            return await _ok_or_balance(
                HarnessResult(
                    text=text,
                    status=HarnessStatus.OK,
                    skill_id=skill_id,
                    usage=usage,
                    data={"runner": "ClaudeCliRunner", "exitCode": rc, "livePreview": True},
                ),
                _emit,
            )

        try:
            mp = await self.supervisor.spawn(task_id, *argv, env=env, cwd=run_cwd)
        except FileNotFoundError:
            err = f"Failed to spawn Claude CLI: {binary}"
            await _emit(HarnessEventType.ERROR, {"error": err})
            return HarnessResult(text="", status=HarnessStatus.FAILED, error=err, skill_id=skill_id)

        bump_stream_limit(mp.proc.stdout)
        bump_stream_limit(mp.proc.stderr)

        async def _read_stdout() -> None:
            assert mp.proc.stdout is not None
            while True:
                line_b = await readline_unlimited(mp.proc.stdout)
                if not line_b:
                    break
                line = line_b.decode("utf-8", errors="replace")
                obj = parse_stream_json_line(line)
                if not obj:
                    continue
                await self._handle_obj(obj, chunks, usage, _emit)

        async def _read_stderr() -> None:
            assert mp.proc.stderr is not None
            while True:
                line_b = await readline_unlimited(mp.proc.stderr)
                if not line_b:
                    break
                stderr_bits.append(line_b.decode("utf-8", errors="replace"))

        try:
            await asyncio.wait_for(
                asyncio.gather(_read_stdout(), _read_stderr(), mp.proc.wait()),
                timeout=self.timeout_s,
            )
        except asyncio.TimeoutError:
            await self.supervisor.cancel(task_id)
            usage.wall_time_ms = int((time.monotonic() - started) * 1000)
            await _emit(HarnessEventType.ERROR, {"error": "claude cli timeout"})
            return HarnessResult(
                text="".join(chunks),
                status=HarnessStatus.TIMEOUT,
                error=f"timeout after {self.timeout_s}s",
                skill_id=skill_id,
                usage=usage,
                data={"runner": "ClaudeCliRunner"},
            )
        except Exception as exc:  # noqa: BLE001
            await self.supervisor.cancel(task_id)
            usage.wall_time_ms = int((time.monotonic() - started) * 1000)
            err = f"claude cli stream error: {exc}"
            await _emit(HarnessEventType.ERROR, {"error": err})
            return HarnessResult(
                text="".join(chunks),
                status=HarnessStatus.FAILED,
                error=err,
                skill_id=skill_id,
                usage=usage,
                data={"runner": "ClaudeCliRunner", "streamError": True},
            )
        finally:
            # If cancel raced us, supervisor already removed the entry.
            await self.supervisor.unregister(task_id)

        usage.wall_time_ms = int((time.monotonic() - started) * 1000)
        if usage.estimated_cost <= 0 and (usage.input_tokens or usage.output_tokens):
            usage.estimated_cost = estimate_cost(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )
        await _emit(HarnessEventType.USAGE, usage.to_dict())

        # Detect cancel: killed process typically non-zero; check task cancel via empty stdout + kill
        rc = mp.returncode
        text = "".join(chunks).strip()
        if rc not in (0, None) and not text:
            err = "".join(stderr_bits).strip() or f"claude exited with code {rc}"
            # taskkill / cancel often yields abrupt exit
            if mp._killed:  # noqa: SLF001
                canceled = True
            status = HarnessStatus.CANCELED if canceled else HarnessStatus.FAILED
            await _emit(
                HarnessEventType.ERROR if status == HarnessStatus.FAILED else HarnessEventType.STATUS,
                {"error": err} if status == HarnessStatus.FAILED else {"state": "canceled"},
            )
            return HarnessResult(
                text=text,
                status=status,
                error=err,
                skill_id=skill_id,
                usage=usage,
                data={"runner": "ClaudeCliRunner", "exitCode": rc, "stderr": err[:1000]},
            )

        return await _ok_or_balance(
            HarnessResult(
                text=text,
                status=HarnessStatus.OK,
                skill_id=skill_id,
                usage=usage,
                data={
                    "runner": "ClaudeCliRunner",
                    "exitCode": rc,
                    "stderr": "".join(stderr_bits)[:500] if stderr_bits else "",
                },
            ),
            _emit,
        )

    async def _handle_obj(
        self,
        obj: dict[str, Any],
        chunks: list[str],
        usage: TokenUsage,
        emit,
    ) -> None:
        otype = str(obj.get("type") or obj.get("event") or "")

        u = _parse_usage(obj)
        if u:
            usage.input_tokens += u.input_tokens
            usage.output_tokens += u.output_tokens
            if u.model:
                usage.model = u.model

        # Common Claude stream-json shapes (best-effort; CLI formats evolve).
        text = ""
        if otype in {"assistant", "message", "result", "content"}:
            if isinstance(obj.get("message"), dict):
                content = obj["message"].get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text += str(block.get("text") or "")
                elif isinstance(content, str):
                    text = content
            text = text or str(obj.get("text") or obj.get("result") or obj.get("content") or "")
        elif otype in {"text_delta", "delta", "content_block_delta"}:
            delta = obj.get("delta") or obj
            if isinstance(delta, dict):
                text = str(delta.get("text") or delta.get("partial_json") or "")
            else:
                text = str(obj.get("text") or "")
        elif otype == "error":
            await emit(HarnessEventType.ERROR, {"error": str(obj.get("error") or obj)})
            return
        else:
            # Fallback: any free-form text field
            text = str(obj.get("text") or "")

        if text:
            chunks.append(text)
            await emit(HarnessEventType.DELTA, {"text": text})


__all__ = ["ClaudeCliRunner", "parse_stream_json_line"]
