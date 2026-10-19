"""Official A2A wire compliance fixtures (Phase 0/4)."""

from __future__ import annotations

import json
from pathlib import Path

from a2a_sdk import (
    PROTOCOL_VERSION,
    SPEC_COMMIT,
    AgentCard,
    Message,
    Part,
    Task,
    TaskStatus,
)
from a2a_sdk.protocol import METHODS_MUST, TASK_STATES

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_spec_pin_documented():
    assert PROTOCOL_VERSION == "0.3.0"
    assert len(SPEC_COMMIT) >= 40
    assert "message/send" in METHODS_MUST
    assert "rejected" in TASK_STATES
    assert "auth-required" in TASK_STATES


def test_official_agent_card_fixture_roundtrip():
    raw = json.loads((FIXTURES / "official_agent_card.json").read_text(encoding="utf-8"))
    card = AgentCard.from_dict(raw)
    assert card.protocol_version == PROTOCOL_VERSION
    assert card.preferred_transport == "JSONRPC"
    assert card.supports_streaming() is True
    assert card.skill_ids() == ["echo"]
    out = card.to_dict()
    assert out["protocolVersion"] == PROTOCOL_VERSION
    assert out["preferredTransport"] == "JSONRPC"
    # Empty security schemes may be omitted; field is preserved on the DTO.
    assert card.security_schemes == {}
    assert card.supports_authenticated_extended_card is False


def test_message_send_request_fixture_uses_kind():
    raw = json.loads((FIXTURES / "message_send_request.json").read_text(encoding="utf-8"))
    assert raw["method"] == "message/send"
    part = raw["params"]["message"]["parts"][0]
    assert part["kind"] == "text"
    msg = Message.from_dict(raw["params"]["message"])
    assert msg.parts[0].kind == "text"
    assert msg.correlation_id == "corr-1"
    wire = msg.to_dict()
    assert wire["parts"][0]["kind"] == "text"


def test_message_send_response_fixture_parses_task():
    raw = json.loads((FIXTURES / "message_send_response.json").read_text(encoding="utf-8"))
    task = Task.from_dict(raw["result"])
    assert task.id == "task-1"
    assert task.status == TaskStatus.COMPLETED
    assert task.artifacts[0].parts[0].kind == "text"
    assert "hello a2a" in (task.artifacts[0].text() or "")


def test_part_reads_kind_or_type_writes_kind_only():
    p = Part.from_dict({"kind": "text", "text": "a"})
    assert p.type == "text"
    q = Part.from_dict({"type": "data", "data": {"x": 1}})
    assert q.type == "data"
    wire = p.to_dict()
    assert wire["kind"] == "text"
    assert "type" not in wire


def test_task_status_rejected_and_auth_required():
    assert TaskStatus.parse("rejected") == TaskStatus.REJECTED
    assert TaskStatus.parse({"state": "auth-required"}) == TaskStatus.AUTH_REQUIRED
    assert TaskStatus.parse("cancelled") == TaskStatus.CANCELED


def test_task_failed_string_status_message_is_error():
    task = Task.from_dict(
        {
            "id": "t-fail",
            "status": {"state": "failed", "message": "dsh exited 1: missing API key"},
            "artifacts": [],
            "metadata": {"error": "dsh exited 1: missing API key"},
        }
    )
    assert task.status == TaskStatus.FAILED
    assert task.error == "dsh exited 1: missing API key"
