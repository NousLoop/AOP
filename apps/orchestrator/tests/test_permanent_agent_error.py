"""Hermetic tests for scheduler failure helpers."""

from __future__ import annotations

from scheduler import is_cancel_error, is_permanent_agent_error


def test_permanent_deepseek_unavailable():
    assert is_permanent_agent_error(
        "DeepSeek harness unavailable: install deepseek-harness-sdk or dsh CLI, "
        "or set DEEPSEEK_API_KEY for tool_loop fallback."
    )


def test_permanent_pi_missing():
    assert is_permanent_agent_error(
        "Pi CLI not found (pi). Install @earendil-works/pi-coding-agent "
        "or set PI_CLI_PATH / PI_API_KEY (or OPENAI_API_KEY) for tool_loop fallback."
    )


def test_permanent_balance_shortage():
    assert is_permanent_agent_error("余额不足")
    assert is_permanent_agent_error(
        "API Error: 402 Insufficient Balance (request_id: abc)"
    )


def test_transient_a2a_not_permanent():
    assert not is_permanent_agent_error("A2A error: A2A execution failed with status failed")
    assert not is_permanent_agent_error("timeout waiting for agent")


def test_cancel_error_detected():
    assert is_cancel_error("a2a status=canceled")
    assert is_cancel_error("A2A execution failed with status cancelled: canceled by user")
    assert is_cancel_error("preview canceled or timed out")
    assert not is_cancel_error("A2A execution failed with status failed")
    assert not is_cancel_error("timeout waiting for agent")
