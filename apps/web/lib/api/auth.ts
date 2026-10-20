import { request, clearLegacySessionToken } from "./client";

export type ApiKeyRecord = {
  id: string;
  name: string;
  key_prefix: string;
  scopes: string[];
  status: string;
  created_at?: string;
  expires_at?: string | null;
  last_used_at?: string | null;
  api_key?: string;
};

export function listApiKeys() {
  return request<{ api_keys: ApiKeyRecord[] }>("/v1/api-keys");
}

export function createApiKey(
  name: string,
  opts?: { role?: string; scopes?: string[] },
) {
  const body: Record<string, unknown> = { name };
  if (opts?.role) body.role = opts.role;
  else body.scopes = opts?.scopes || ["role:operator"];
  return request<ApiKeyRecord>("/v1/api-keys", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function revokeApiKey(keyId: string) {
  return request<{ revoked: boolean; id: string }>(`/v1/api-keys/${keyId}`, {
    method: "DELETE",
  });
}

export type RbacRole = { role: string; scopes: string[] };

export function listRbacRoles() {
  return request<{ roles: RbacRole[]; docs?: string }>("/v1/rbac/roles");
}

export function getRbacMe() {
  return request<{
    authenticated: boolean;
    auth_required?: boolean;
    name?: string;
    scopes?: string[];
    roles?: string[];
    expanded?: string[];
  }>("/v1/rbac/me");
}

export type AuditLogEntry = {
  id: number;
  tenant_id?: string | null;
  action: string;
  resource_type?: string;
  resource_id?: string;
  ip?: string;
  payload?: Record<string, unknown>;
  created_at?: string;
  summary?: string;
};

export function listAuditLogs(limit = 50) {
  return request<{ audit_logs: AuditLogEntry[] }>(`/v1/audit-logs?limit=${limit}`);
}

export type AuthUser = {
  id: string;
  email: string;
  display_name?: string;
  role: string;
  tenant_id?: string;
  status?: string;
};

export async function login(email: string, password: string) {
  clearLegacySessionToken();
  const res = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ email, password }),
    cache: "no-store",
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${text}`);
  }
  return res.json() as Promise<{
    token_type?: string;
    expires_at: string;
    user: AuthUser;
    scopes: string[];
  }>;
}

export function listTeammates() {
  return request<{ users: AuthUser[] }>("/v1/auth/users");
}

export function inviteTeammate(body: {
  email: string;
  display_name?: string;
  role?: string;
  with_api_key?: boolean;
}) {
  return request<{
    user: AuthUser;
    temporary_password: string;
    login_hint?: string;
    api_key?: ApiKeyRecord;
  }>("/v1/auth/invite", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function logout() {
  clearLegacySessionToken();
  const res = await fetch("/api/auth/logout", {
    method: "POST",
    credentials: "include",
    cache: "no-store",
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${text}`);
  }
  return res.json() as Promise<{ logged_out: boolean }>;
}

export function getAuthMe() {
  return request<{
    authenticated: boolean;
    auth_required?: boolean;
    kind?: string;
    name?: string;
    email?: string;
    user_id?: string;
    user_role?: string;
    scopes?: string[];
    roles?: string[];
    expanded?: string[];
    user?: AuthUser;
  }>("/v1/auth/me");
}

export function getGatewayHealth() {
  return request<{
    status: string;
    service?: string;
    phase?: string;
    auth_required?: boolean;
  }>("/health");
}
