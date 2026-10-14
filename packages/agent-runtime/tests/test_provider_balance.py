from __future__ import annotations

from agent_runtime.harness.protocol import HarnessResult, HarnessStatus
from agent_runtime.harness.runners._cli_common import (
    apply_provider_balance_failure,
    harness_output_error,
    provider_balance_error,
)


def test_detects_claude_cli_402():
    msg = provider_balance_error(
        "API Error: 402 Insufficient Balance (request_id: 670a32dc-9fa9-4c26-a062-d51e47f65db4)"
    )
    assert msg == "余额不足"


def test_detects_duplicate_402_line():
    blob = (
        "API Error: 402 Insufficient Balance (request_id: 670a32dc-9fa9-4c26-a062-d51e47f65db4)"
        "API Error: 402 Insufficient Balance (request_id: 670a32dc-9fa9-4c26-a062-d51e47f65db4)"
    )
    assert provider_balance_error(blob) == "余额不足"


def test_ignores_normal_output():
    assert provider_balance_error("Wrote 402 lines of notes about balance sheets.") is None
    assert provider_balance_error("HTTP 200 OK") is None


def test_coerce_ok_result_to_failed():
    result = HarnessResult(
        text="API Error: 402 Insufficient Balance (request_id: abc)",
        status=HarnessStatus.OK,
    )
    out = apply_provider_balance_failure(result)
    assert out.status == HarnessStatus.FAILED
    assert out.error == "余额不足"


def test_harness_output_error_timeout_and_json():
    assert "API call failed" in (harness_output_error("API call failed after 3 retries: Request timed out.") or "")
    assert harness_output_error('{"ok": false, "status": "error"}')
    assert harness_output_error("# AOP hosted result") == "未执行真实推理（hosted stub）"
