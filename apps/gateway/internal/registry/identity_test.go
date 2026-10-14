package registry

import "testing"

func TestIsWorkAgentKey(t *testing.T) {
	if !IsWorkAgentKey("hermes") {
		t.Fatal("hermes is a work agent")
	}
	if !IsWorkAgentKey("claude-code") {
		t.Fatal("claude-code is a work agent")
	}
	if IsWorkAgentKey("node-local-1") {
		t.Fatal("node-local-1 is a supervisor")
	}
	if IsWorkAgentKey("aop-node") {
		t.Fatal("aop-node is a supervisor")
	}
	if IsWorkAgentKey("") {
		t.Fatal("empty is not a work agent")
	}
}

func TestNormalizeIdentity(t *testing.T) {
	got, err := NormalizeIdentity("")
	if err != nil || got != DefaultIdentity {
		t.Fatalf("empty -> %q %v", got, err)
	}
	got, err = NormalizeIdentity(" Manager ")
	if err != nil || got != IdentityManager {
		t.Fatalf("manager -> %q %v", got, err)
	}
	for _, code := range []string{
		IdentityAssistant, IdentityManager, IdentityEmployee, IdentityStudent, IdentitySupport,
	} {
		got, err = NormalizeIdentity(code)
		if err != nil || got != code {
			t.Fatalf("%s -> %q %v", code, got, err)
		}
	}
	if _, err := NormalizeIdentity("ceo"); err == nil {
		t.Fatal("ceo should be rejected")
	}
}

func TestIdentityLabel(t *testing.T) {
	if IdentityLabel(IdentityAssistant) != "助理" {
		t.Fatal(IdentityLabel(IdentityAssistant))
	}
	if IdentityLabel(IdentitySupport) != "客服" {
		t.Fatal(IdentityLabel(IdentitySupport))
	}
	if IdentityLabel("unknown") != "unknown" {
		t.Fatal("unknown should passthrough")
	}
}
