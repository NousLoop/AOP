package registry

import (
	"fmt"
	"sort"
	"strings"

	"github.com/aop/a2a-platform/packages/catalogs"
)

// Business identity codes stored on agents.identity (platform governance, not A2A card).
// Codes must match packages/catalogs/identities.json.
const (
	IdentityAssistant = "assistant"
	IdentityManager   = "manager"
	IdentityEmployee  = "employee"
	IdentityStudent   = "student"
	IdentitySupport   = "support"

	DevAdminEmail = "admin@aop.local"
)

// DefaultIdentity and ValidIdentities come from packages/catalogs/identities.json.
var (
	DefaultIdentity = catalogs.DefaultIdentity()
	ValidIdentities = catalogs.IdentityLabels()
)

func NormalizeIdentity(raw string) (string, error) {
	id := strings.ToLower(strings.TrimSpace(raw))
	if id == "" {
		return DefaultIdentity, nil
	}
	if _, ok := ValidIdentities[id]; !ok {
		allowed := make([]string, 0, len(ValidIdentities))
		for code := range ValidIdentities {
			allowed = append(allowed, code)
		}
		sort.Strings(allowed)
		return "", fmt.Errorf("invalid identity %q; allowed: %s", raw, strings.Join(allowed, ", "))
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
