import { request, API_BASE, GATEWAY_PUBLIC_BASE } from "./client";

export type MarketplaceInstallMeta = {
  download?: string;
  install_ps1?: string;
  install_sh?: string;
  windows?: string;
  unix?: string;
  windows_full?: string;
  unix_full?: string;
};

export type MarketplacePackage = {
  package_id: string;
  name: string;
  description?: string;
  publisher?: string;
  version?: string;
  skills?: string[];
  default_endpoint?: string;
  agent_key?: string;
  tags?: string[];
  installed?: boolean;
  /** Virtual agent: harness × role profile */
  harness?: string;
  profile?: string;
  default_port?: number;
  bundle?: boolean;
  download_url?: string;
  install?: MarketplaceInstallMeta;
  agent?: {
    agent_id: string;
    agent_key: string;
    status: string;
    endpoint?: string;
    skills?: string[];
  } | null;
};

/** Prefer Gateway-reachable one-liners (absolute Gateway host, not the BFF). */
export function marketplaceDeployCommands(pkg: MarketplacePackage) {
  const base = GATEWAY_PUBLIC_BASE;
  const id = pkg.package_id;
  const profile = pkg.profile || pkg.agent_key || id.replace(/^pkg-/, "");
  const fromApi = pkg.install;
  return {
    windows:
      fromApi?.windows_full ||
      `irm ${base}/v1/marketplace/agents/${id}/install.ps1 | iex`,
    unix:
      fromApi?.unix_full ||
      `curl -fsSL ${base}/v1/marketplace/agents/${id}/install.sh | bash`,
    windowsShort: fromApi?.windows || `irm ${base}/install/${profile}.ps1 | iex`,
    unixShort: fromApi?.unix || `curl -fsSL ${base}/install/${profile}.sh | bash`,
    download:
      fromApi?.download ||
      pkg.download_url ||
      `${base}/v1/marketplace/agents/${id}/download`,
  };
}

export async function downloadMarketplaceBundle(
  packageId: string,
  filename?: string,
): Promise<void> {
  const res = await fetch(
    `${API_BASE}/v1/marketplace/agents/${encodeURIComponent(packageId)}/download`,
    { cache: "no-store", credentials: "include" },
  );
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${text}`);
  }
  const blob = await res.blob();
  const cd = res.headers.get("Content-Disposition") || "";
  const match = /filename="?([^";]+)"?/i.exec(cd);
  const name = filename || match?.[1] || `${packageId}.zip`;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export function listMarketplace(q?: string) {
  const qs = q ? `?q=${encodeURIComponent(q)}` : "";
  return request<{ packages: MarketplacePackage[] }>(`/v1/marketplace${qs}`);
}

export function installMarketplacePackage(packageId: string, endpoint?: string) {
  return request<{ installed: boolean; agent: Record<string, unknown> }>(
    "/v1/marketplace/install",
    {
      method: "POST",
      body: JSON.stringify({ package_id: packageId, endpoint }),
    },
  );
}
