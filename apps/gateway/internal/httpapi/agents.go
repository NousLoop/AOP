package httpapi

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	"github.com/go-chi/chi/v5"

	"github.com/aop/a2a-platform/apps/gateway/internal/auth"
	"github.com/aop/a2a-platform/apps/gateway/internal/registry"
)

type AgentHandlers struct {
	Store   *registry.Store
	Fetcher *registry.CardFetcher
	Tenant  string
	Auth    *auth.Store
	Audit   *auth.AuditStore
}

type registerRequest struct {
	Endpoint string `json:"endpoint"`
}

type updateMetaRequest struct {
	Identity    *string `json:"identity"`
	OwnerUserID *string `json:"owner_user_id"`
}

func (h *AgentHandlers) Routes(r chi.Router) {
	r.Get("/", h.list)

	r.Group(func(r chi.Router) {
		if h.Auth != nil {
			r.Use(requireScopeIfAuthed(h.Auth, "agent.write"))
		}
		r.Post("/register", h.register)
		r.Patch("/{agentID}", h.updateMeta)
		r.Delete("/{agentID}", h.delete)
		r.Post("/{agentID}/disable", h.disable)
		r.Post("/{agentID}/enable", h.enable)
		r.Post("/{agentID}/health", h.health)
	})

	r.Get("/{agentID}", h.get)
}

func (h *AgentHandlers) register(w http.ResponseWriter, r *http.Request) {
	var req registerRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "validation_error", "invalid json body", nil)
		return
	}
	req.Endpoint = strings.TrimSpace(req.Endpoint)
	if req.Endpoint == "" {
		writeError(w, http.StatusBadRequest, "validation_error", "endpoint is required", nil)
		return
	}
	if err := registry.ValidateAgentEndpoint(req.Endpoint); err != nil {
		writeError(w, http.StatusBadRequest, "validation_error", "endpoint not allowed", err.Error())
		return
	}

	card, raw, err := h.Fetcher.Fetch(r.Context(), req.Endpoint)
	if err != nil {
		writeError(w, http.StatusBadGateway, "agent_unavailable", "failed to fetch agent card", err.Error())
		return
	}
	if err := h.Fetcher.Health(r.Context(), req.Endpoint); err != nil {
		writeError(w, http.StatusBadGateway, "agent_unavailable", "agent health check failed", err.Error())
		return
	}

	result, err := h.Store.Register(r.Context(), tenantOrDefault(r, h.Tenant), req.Endpoint, card, raw)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", "failed to register agent", err.Error())
		return
	}
	tenant := tenantOrDefault(r, h.Tenant)
	recordAudit(h.Audit, r, tenant, "agent.register", "agent", result.AgentID, map[string]any{
		"endpoint": req.Endpoint,
		"status":   result.Status,
	})
	writeJSON(w, http.StatusCreated, result)
}

func (h *AgentHandlers) list(w http.ResponseWriter, r *http.Request) {
	skill := r.URL.Query().Get("skill")
	status := r.URL.Query().Get("status")
	items, err := h.Store.List(r.Context(), tenantOrDefault(r, h.Tenant), skill, status)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", "failed to list agents", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"agents": items})
}

func (h *AgentHandlers) get(w http.ResponseWriter, r *http.Request) {
	agentID := chi.URLParam(r, "agentID")
	rec, err := h.Store.Get(r.Context(), tenantOrDefault(r, h.Tenant), agentID)
	if errors.Is(err, registry.ErrNotFound) {
		writeError(w, http.StatusNotFound, "not_found", "agent not found", nil)
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", "failed to get agent", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, rec)
}

func (h *AgentHandlers) updateMeta(w http.ResponseWriter, r *http.Request) {
	agentID := chi.URLParam(r, "agentID")
	var req updateMetaRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "validation_error", "invalid json body", nil)
		return
	}
	if req.Identity == nil && req.OwnerUserID == nil {
		writeError(w, http.StatusBadRequest, "validation_error", "identity or owner_user_id required", nil)
		return
	}
	tenant := tenantOrDefault(r, h.Tenant)
	rec, err := h.Store.UpdateMeta(r.Context(), tenant, agentID, req.Identity, req.OwnerUserID)
	if errors.Is(err, registry.ErrNotFound) {
		writeError(w, http.StatusNotFound, "not_found", "agent not found", nil)
		return
	}
	if errors.Is(err, registry.ErrInvalidIdentity) || errors.Is(err, registry.ErrInvalidOwner) {
		writeError(w, http.StatusBadRequest, "validation_error", err.Error(), nil)
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", "failed to update agent meta", err.Error())
		return
	}
	recordAudit(h.Audit, r, tenant, "agent.update_meta", "agent", agentID, map[string]any{
		"identity":      rec.Identity,
		"owner_user_id": rec.OwnerUserID,
	})
	writeJSON(w, http.StatusOK, rec)
}

func (h *AgentHandlers) delete(w http.ResponseWriter, r *http.Request) {
	agentID := chi.URLParam(r, "agentID")
	tenant := tenantOrDefault(r, h.Tenant)
	err := h.Store.Delete(r.Context(), tenant, agentID)
	if errors.Is(err, registry.ErrNotFound) {
		writeError(w, http.StatusNotFound, "not_found", "agent not found", nil)
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", "failed to delete agent", err.Error())
		return
	}
	recordAudit(h.Audit, r, tenant, "agent.delete", "agent", agentID, nil)
	writeJSON(w, http.StatusOK, map[string]any{"deleted": true})
}

func (h *AgentHandlers) disable(w http.ResponseWriter, r *http.Request) {
	h.setStatus(w, r, "disabled")
}

func (h *AgentHandlers) enable(w http.ResponseWriter, r *http.Request) {
	h.setStatus(w, r, "online")
}

func (h *AgentHandlers) setStatus(w http.ResponseWriter, r *http.Request, status string) {
	agentID := chi.URLParam(r, "agentID")
	tenant := tenantOrDefault(r, h.Tenant)
	rec, err := h.Store.SetStatus(r.Context(), tenant, agentID, status)
	if errors.Is(err, registry.ErrNotFound) {
		writeError(w, http.StatusNotFound, "not_found", "agent not found", nil)
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", "failed to update status", err.Error())
		return
	}
	action := "agent.enable"
	if status == "disabled" {
		action = "agent.disable"
	}
	recordAudit(h.Audit, r, tenant, action, "agent", agentID, map[string]any{"status": status})
	writeJSON(w, http.StatusOK, rec)
}

func (h *AgentHandlers) health(w http.ResponseWriter, r *http.Request) {
	agentID := chi.URLParam(r, "agentID")
	rec, err := h.Store.Get(r.Context(), tenantOrDefault(r, h.Tenant), agentID)
	if errors.Is(err, registry.ErrNotFound) {
		writeError(w, http.StatusNotFound, "not_found", "agent not found", nil)
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", "failed to get agent", err.Error())
		return
	}
	if rec.Endpoint == "" {
		writeError(w, http.StatusBadRequest, "validation_error", "agent has no endpoint", nil)
		return
	}
	if err := h.Fetcher.Health(r.Context(), rec.Endpoint); err != nil {
		_, _ = h.Store.SetStatus(r.Context(), tenantOrDefault(r, h.Tenant), rec.ID, "offline")
		writeError(w, http.StatusBadGateway, "agent_unavailable", "health check failed", err.Error())
		return
	}
	updated, err := h.Store.SetStatus(r.Context(), tenantOrDefault(r, h.Tenant), rec.ID, "online")
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", "failed to update status", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"agent_id": updated.ID,
		"status":   updated.Status,
		"healthy":  true,
	})
}
