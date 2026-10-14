"""Built-in harness runners."""

from .claude_cli import ClaudeCliRunner, parse_stream_json_line
from .deepseek import DeepSeekHarnessRunner
from .hermes import HermesCliRunner
from .openclaw import OpenClawCliRunner
from .pi_cli import PiCliRunner, parse_pi_jsonl_line

__all__ = [
    "ClaudeCliRunner",
    "parse_stream_json_line",
    "PiCliRunner",
    "parse_pi_jsonl_line",
    "DeepSeekHarnessRunner",
    "OpenClawCliRunner",
    "HermesCliRunner",
]


def create_runner(name: str | None = None):
    """Factory: ``claude_cli`` | ``pi_cli`` | ``deepseek`` | ``openclaw`` | ``hermes``."""
    key = (name or "claude_cli").lower().replace("-", "_")
    if key in {"claude", "claude_cli", "claude_code"}:
        return ClaudeCliRunner()
    if key in {"pi", "pi_cli", "pi_coding_agent"}:
        return PiCliRunner()
    if key in {"deepseek", "dsh", "deepseek_harness", "deepseek_cli"}:
        return DeepSeekHarnessRunner()
    if key in {"openclaw", "openclaw_cli", "open_claw"}:
        return OpenClawCliRunner()
    if key in {"hermes", "hermes_cli", "hermes_agent"}:
        return HermesCliRunner()
    raise ValueError(f"Unsupported harness runner: {name!r}")
