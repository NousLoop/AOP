from __future__ import annotations

from agent_runtime.harness.platform_role import (
    enrich_system_prompt,
    enrich_system_prompt_for_agent,
    format_platform_role_block,
    identity_label,
    resolve_platform_role,
    role_from_message_metadata,
)


def test_identity_label_zh():
    assert identity_label("assistant") == "助理"
    assert identity_label("manager") == "主管"
    assert identity_label("support") == "客服"


def test_format_block_includes_owner_and_identity():
    block = format_platform_role_block(
        identity="manager",
        owner_display_name="AOP Admin",
        owner_email="admin@aop.local",
    )
    assert "主管" in block
    assert "`manager`" in block
    assert "AOP Admin (admin@aop.local)" in block
    assert "平台身份与所有者" in block


def test_enrich_appends_once():
    base = "You are a helper."
    once = enrich_system_prompt(
        base,
        identity="employee",
        owner_email="u@aop.local",
        owner_display_name="User",
    )
    assert once.startswith("You are a helper.")
    assert once.count("Platform assignment") == 1
    twice = enrich_system_prompt(
        once,
        identity="student",
        owner_email="other@aop.local",
    )
    assert twice.count("Platform assignment") == 1
    assert "employee" in twice  # second enrich skipped


def test_role_from_message_metadata():
    meta = {
        "agentIdentity": "support",
        "agentOwnerEmail": "cs@aop.local",
        "agentOwnerName": "CS Bot",
    }
    role = role_from_message_metadata(meta)
    assert role is not None
    assert role["identity"] == "support"
    assert role["owner_email"] == "cs@aop.local"
    assert role_from_message_metadata({}) is None


def test_resolve_prefers_message_metadata(monkeypatch):
    monkeypatch.setattr(
        "agent_runtime.harness.platform_role.fetch_agent_platform_role",
        lambda *_a, **_k: {"identity": "assistant", "owner_email": "admin@aop.local",
                           "owner_display_name": "Admin", "owner_user_id": "1"},
    )
    role = resolve_platform_role(
        "claude-code",
        message={"metadata": {"agentIdentity": "manager", "owner_email": "boss@aop.local"}},
    )
    assert role["identity"] == "manager"
    assert role["owner_email"] == "boss@aop.local"


def test_enrich_for_agent_uses_gateway_lookup(monkeypatch):
    monkeypatch.setattr(
        "agent_runtime.harness.platform_role.fetch_agent_platform_role",
        lambda ref: {
            "identity": "student",
            "owner_email": "admin@aop.local",
            "owner_display_name": "AOP Admin",
            "owner_user_id": "u1",
        }
        if ref == "pi"
        else None,
    )
    out = enrich_system_prompt_for_agent("Base.", "pi")
    assert "学生" in out
    assert "AOP Admin (admin@aop.local)" in out
    assert "Base." in out
