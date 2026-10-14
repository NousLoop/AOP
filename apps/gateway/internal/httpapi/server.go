package httpapi

import (
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/prometheus/client_golang/prometheus/promhttp"

	"github.com/aop/a2a-platform/apps/gateway/internal/auth"
)

type Server struct {
	Agents            *AgentHandlers
	APIKeys           *APIKeyHandlers
	RBAC              *RBACHandlers
	Audit             *AuditHandlers
	AuthAPI           *AuthHandlers
	AuthStore         *auth.Store
	AuthRequired      bool
	DefaultTenantID   string
	OrchestratorProxy http.Handler
}

func (s *Server) Handler() http.Handler {
	r := chi.NewRouter()
	r.Use(middleware.RequestID)
	r.Use(middleware.RealIP)
	r.Use(middleware.Logger)
	r.Use(middleware.Recoverer)
	r.Use(withCORS)
	r.Use(withAuth(s.AuthStore, s.AuthRequired, s.DefaultTenantID))

	r.Get("/health", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{
			"status":        "ok",
			"service":       "gateway",
			"phase":         "25-auth",
			"auth_required": s.AuthRequired,
		})
	})

	r.Handle("/metrics", promhttp.Handler())

	r.Get("/", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{
			"name":          "AOP Gateway",
			"phase":         "25-auth",
			"auth_required": s.AuthRequired,
			"roles":         auth.RoleNames(),
			"scopes": []string{
				"*", "admin", "api_key.admin",
				"agent.read", "agent.write",
				"task.read", "task.write",
				"memory.read", "memory.write",
				"role:viewer", "role:operator", "role:admin",
			},
			"endpoints": []string{
				"GET /health",
				"GET /v1/agents",
				"POST /v1/agents/register",
				"PATCH /v1/agents/{id}",
				"GET /v1/agents/{id}/capacity",
				"GET /v1/agents/{id}/reliability",
				"GET /v1/agents/{id}/health",
				"POST /v1/agents/{id}/heartbeat",
				"POST /v1/agent-runtime/{id}/heartbeat",
				"GET /v1/nodes",
				"POST /v1/nodes/{id}/heartbeat",
				"GET /v1/api-keys",
				"POST /v1/api-keys",
				"DELETE /v1/api-keys/{id}",
				"GET /v1/rbac/roles",
				"GET /v1/rbac/me",
				"GET /v1/audit-logs",
				"POST /v1/auth/login",
				"POST /v1/auth/logout",
				"GET /v1/auth/me",
				"GET /v1/auth/users",
				"POST /v1/auth/invite",
				"GET /v1/tasks",
				"POST /v1/tasks",
				"WS /v1/tasks/{id}/events/ws",
				"WS /v1/events/ws",
				"GET /v1/preflight",
				"GET /v1/stats/overview",
				"GET /v1/stats/agents",
				"GET /v1/billing/usage",
				"GET /v1/billing/summary",
				"GET /v1/billing/invoice",
				"GET /v1/billing/invoice.md",
				"GET /v1/billing/invoices",
				"POST /v1/billing/invoices",
				"POST /v1/billing/invoices/{id}/checkout",
				"GET /v1/billing/checkouts",
				"POST /v1/billing/webhooks/stripe",
				"POST /v1/billing/checkouts/simulate-paid",
				"GET /v1/quotas",
				"PUT /v1/quotas",
				"GET /v1/quotas/grants",
				"GET /v1/egress",
				"PUT /v1/egress",
				"GET /v1/egress/check",
				"GET /v1/router/preview",
				"GET /v1/metrics",
				"GET /v1/artifacts",
				"GET /v1/workflows",
				"GET /v1/marketplace",
				"GET /v1/evaluations",
				"GET /v1/evaluations/overview",
				"GET /v1/memory",
				"GET /v1/memory/search",
			},
		})
	})

	r.Route("/v1/agents", func(r chi.Router) {
		// Phase 5/6 OS endpoints: register before registry handlers so these
		// paths forward to Orchestrator. Registry keeps POST /{id}/health probe.
		// Heartbeat/drain live only under /v1/agent-runtime/* (proxied below).
		if s.OrchestratorProxy != nil {
			r.Get("/{agentID}/capacity", s.OrchestratorProxy.ServeHTTP)
			r.Get("/{agentID}/reliability", s.OrchestratorProxy.ServeHTTP)
			r.Get("/{agentID}/health", s.OrchestratorProxy.ServeHTTP)
			// Compat: old aopd posted here; Orchestrator aliases to agent-runtime.
			r.Post("/{agentID}/heartbeat", s.OrchestratorProxy.ServeHTTP)
		}
		s.Agents.Routes(r)
	})

	r.Route("/v1/api-keys", func(r chi.Router) {
		s.APIKeys.Routes(r)
	})

	if s.RBAC != nil {
		r.Route("/v1/rbac", func(r chi.Router) {
			s.RBAC.Routes(r)
		})
	}

	if s.Audit != nil {
		r.Route("/v1/audit-logs", func(r chi.Router) {
			s.Audit.Routes(r)
		})
	}

	if s.AuthAPI != nil {
		r.Route("/v1/auth", func(r chi.Router) {
			s.AuthAPI.Routes(r)
		})
	}

	if s.OrchestratorProxy != nil {
		taskProxy := requireMutatingScope(s.AuthStore, "task.write")(s.OrchestratorProxy)
		r.Handle("/v1/tasks", taskProxy)
		r.Handle("/v1/tasks/*", taskProxy)
		// Visual Runtime preferred WS path (snapshot + incremental events)
		r.Handle("/ws", s.OrchestratorProxy)
		r.Handle("/ws/*", s.OrchestratorProxy)
		r.Handle("/v1/events", s.OrchestratorProxy)
		r.Handle("/v1/events/*", s.OrchestratorProxy)
		r.Handle("/v1/workflows", taskProxy)
		r.Handle("/v1/workflows/*", taskProxy)
		r.Handle("/v1/marketplace", s.OrchestratorProxy)
		r.Handle("/v1/marketplace/*", s.OrchestratorProxy)
		// Claude-style short install aliases: /install/claude-coder.ps1
		r.Handle("/install", s.OrchestratorProxy)
		r.Handle("/install/*", s.OrchestratorProxy)
		r.Handle("/v1/skills", s.OrchestratorProxy)
		r.Handle("/v1/skills/*", s.OrchestratorProxy)
		r.Handle("/v1/discover/skill", s.OrchestratorProxy)
		r.Handle("/v1/invoke/skill", s.OrchestratorProxy)
		r.Handle("/v1/health/probe", requireMutatingScope(s.AuthStore, "agent.write")(s.OrchestratorProxy))
		r.Handle("/v1/preflight", s.OrchestratorProxy)
		r.Handle("/v1/stats", s.OrchestratorProxy)
		r.Handle("/v1/stats/*", s.OrchestratorProxy)
		r.Handle("/v1/billing", s.OrchestratorProxy)
		r.Handle("/v1/billing/*", s.OrchestratorProxy)
		r.Handle("/v1/quotas", s.OrchestratorProxy)
		r.Handle("/v1/quotas/*", s.OrchestratorProxy)
		r.Handle("/v1/egress", s.OrchestratorProxy)
		r.Handle("/v1/egress/*", s.OrchestratorProxy)
		r.Handle("/v1/artifacts", s.OrchestratorProxy)
		r.Handle("/v1/artifacts/*", s.OrchestratorProxy)
		r.Handle("/v1/evaluations", taskProxy)
		r.Handle("/v1/evaluations/*", taskProxy)
		r.Handle("/v1/router", s.OrchestratorProxy)
		r.Handle("/v1/router/*", s.OrchestratorProxy)
		// A2A OS: open discovery/routing/runtime-graph infrastructure. Any
		// authenticated Agent may call these directly (no central orchestrator).
		r.Handle("/v1/route", s.OrchestratorProxy)
		r.Handle("/v1/discover", s.OrchestratorProxy)
		r.Handle("/v1/runtime", s.OrchestratorProxy)
		r.Handle("/v1/runtime/*", s.OrchestratorProxy)
		// Phase 2: collaboration graph API for frontend visualization.
		r.Handle("/v1/collaboration", s.OrchestratorProxy)
		r.Handle("/v1/collaboration/*", s.OrchestratorProxy)
		// Phase 2.2: OS-level governance authority. Agents query the trusted OS
		// for an authoritative allow/deny decision before delegating to a peer.
		r.Handle("/v1/governance", s.OrchestratorProxy)
		r.Handle("/v1/governance/*", s.OrchestratorProxy)
		// Phase 5: intelligent scheduling + cost + tenant policy
		r.Handle("/v1/scheduling", s.OrchestratorProxy)
		r.Handle("/v1/scheduling/*", s.OrchestratorProxy)
		r.Handle("/v1/tenants", s.OrchestratorProxy)
		r.Handle("/v1/tenants/*", s.OrchestratorProxy)
		// Phase 3: agent lifecycle (gateway-safe path; /v1/agents/* is registry).
		r.Handle("/v1/agent-runtime", s.OrchestratorProxy)
		r.Handle("/v1/agent-runtime/*", s.OrchestratorProxy)
		// Edge supervisors are not A2A agents.
		r.Handle("/v1/nodes", s.OrchestratorProxy)
		r.Handle("/v1/nodes/*", s.OrchestratorProxy)
		r.Handle("/v1/metrics", s.OrchestratorProxy)
		r.Handle("/v1/metrics/*", s.OrchestratorProxy)
		r.Handle("/v1/memory", taskProxy)
		r.Handle("/v1/memory/*", taskProxy)
	}

	return r
}
