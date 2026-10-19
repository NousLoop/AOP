// Package catalogs loads shared machine-readable enums used by Gateway / Web / Python.
package catalogs

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"sync"
)

//go:embed identities.json
var identitiesJSON []byte

// IdentityItem is one business-identity option (code stored in DB; label for UI).
type IdentityItem struct {
	Code  string `json:"code"`
	Label string `json:"label"`
}

// IdentitiesFile is the on-disk shape of identities.json.
type IdentitiesFile struct {
	Default string         `json:"default"`
	Items   []IdentityItem `json:"items"`
}

var (
	identitiesOnce sync.Once
	identitiesCat  IdentitiesFile
	identitiesErr  error
	identitiesMap  map[string]string
)

func loadIdentities() {
	identitiesOnce.Do(func() {
		if err := json.Unmarshal(identitiesJSON, &identitiesCat); err != nil {
			identitiesErr = fmt.Errorf("identities.json: %w", err)
			return
		}
		if identitiesCat.Default == "" {
			identitiesCat.Default = "assistant"
		}
		identitiesMap = make(map[string]string, len(identitiesCat.Items))
		for _, it := range identitiesCat.Items {
			if it.Code == "" {
				continue
			}
			identitiesMap[it.Code] = it.Label
		}
		if len(identitiesMap) == 0 {
			identitiesErr = fmt.Errorf("identities.json: empty items")
		}
	})
}

// Identities returns the parsed catalog (panics only if embed is corrupt — fail fast at boot).
func Identities() IdentitiesFile {
	loadIdentities()
	if identitiesErr != nil {
		panic(identitiesErr)
	}
	return identitiesCat
}

// IdentityLabels maps code → zh label.
func IdentityLabels() map[string]string {
	loadIdentities()
	if identitiesErr != nil {
		panic(identitiesErr)
	}
	out := make(map[string]string, len(identitiesMap))
	for k, v := range identitiesMap {
		out[k] = v
	}
	return out
}

// DefaultIdentity is the catalog default code.
func DefaultIdentity() string {
	return Identities().Default
}
