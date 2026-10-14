package registry

import (
	"fmt"
	"strings"
)

// Business identity codes stored on agents.identity (platform governance, not A2A card).
const (
	IdentityAssistant = "assistant"
	IdentityManager   = "manager"
	IdentityEmployee  = "employee"
	IdentityStudent   = "student"
	IdentitySupport   = "support"

	DefaultIdentity = IdentityAssistant
	DevAdminEmail   = "admin@aop.local"
)

var ValidIdentities = map[string]string{
	IdentityAssistant: "助理",
	IdentityManager:   "主管",
	IdentityEmployee:  "员工",
	IdentityStudent:   "学生",
	IdentitySupport:   "客服",
}

func NormalizeIdentity(raw string) (string, error) {
	id := strings.ToLower(strings.TrimSpace(raw))
	if id == "" {
		return DefaultIdentity, nil
	}
	if _, ok := ValidIdentities[id]; !ok {
		return "", fmt.Errorf("invalid identity %q; allowed: assistant, manager, employee, student, support", raw)
	}
	return id, nil
}

func IdentityLabel(code string) string {
	if label, ok := ValidIdentities[code]; ok {
		return label
	}
	return code
}

var (
	ErrInvalidIdentity = fmt.Errorf("invalid identity")
	ErrInvalidOwner    = fmt.Errorf("invalid owner")
)
