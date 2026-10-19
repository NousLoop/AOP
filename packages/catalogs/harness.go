package catalogs

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"sync"
)

//go:embed harness_profiles.json
var harnessProfilesJSON []byte

// HarnessProfileItem is one aopd-hosted harness row.
// Listen address is only in CLIA2AListen — agents do not carry their own port.
type HarnessProfileItem struct {
	Key         string   `json:"key"`
	PluginID    string   `json:"plugin_id,omitempty"`
	Path        string   `json:"path,omitempty"`
	Runner      string   `json:"runner"`
	Name        string   `json:"name"`
	Description string   `json:"description"`
	Tags        []string `json:"tags"`
	EnvURLKey   string   `json:"env_url_key"`
}

// HarnessProfilesFile is the on-disk shape of harness_profiles.json.
type HarnessProfilesFile struct {
	CLIA2AListen string               `json:"cli_a2a_listen"`
	Agents       []HarnessProfileItem `json:"agents"`
}

var (
	harnessOnce sync.Once
	harnessCat  HarnessProfilesFile
	harnessErr  error
)

func loadHarness() {
	harnessOnce.Do(func() {
		if err := json.Unmarshal(harnessProfilesJSON, &harnessCat); err != nil {
			harnessErr = fmt.Errorf("harness_profiles.json: %w", err)
			return
		}
		if harnessCat.CLIA2AListen == "" {
			harnessCat.CLIA2AListen = "127.0.0.1:8020"
		}
		if len(harnessCat.Agents) == 0 {
			harnessErr = fmt.Errorf("harness_profiles.json: empty agents")
		}
	})
}

// HarnessProfiles returns the parsed harness catalog.
func HarnessProfiles() HarnessProfilesFile {
	loadHarness()
	if harnessErr != nil {
		panic(harnessErr)
	}
	return harnessCat
}

// CLIA2AListen is aopd's shared manufacturer-CLI A2A bind (host:port).
func CLIA2AListen() string {
	return HarnessProfiles().CLIA2AListen
}

// CLIA2ABaseURL is http:// + CLIA2AListen.
func CLIA2ABaseURL() string {
	listen := CLIA2AListen()
	if strings.Contains(listen, "://") {
		return strings.TrimRight(listen, "/")
	}
	return "http://" + strings.TrimRight(listen, "/")
}

// CLIA2APort parses the port from cli_a2a_listen.
func CLIA2APort() int {
	listen := CLIA2AListen()
	i := strings.LastIndex(listen, ":")
	if i < 0 {
		return 8020
	}
	p, err := strconv.Atoi(listen[i+1:])
	if err != nil {
		return 8020
	}
	return p
}

// AgentEndpoint returns the public A2A base for a catalog row.
func AgentEndpoint(it HarnessProfileItem) string {
	path := it.Path
	if path == "" && it.PluginID != "" {
		path = "/agents/" + it.PluginID
	}
	if path != "" && !strings.HasPrefix(path, "/") {
		path = "/" + path
	}
	return CLIA2ABaseURL() + strings.TrimRight(path, "/")
}

// HarnessPluginIDs returns aop-node plugin folder ids in catalog order.
func HarnessPluginIDs() []string {
	h := HarnessProfiles()
	out := make([]string, 0, len(h.Agents))
	for _, it := range h.Agents {
		id := it.PluginID
		if id == "" {
			id = it.Key
		}
		out = append(out, id)
	}
	return out
}
