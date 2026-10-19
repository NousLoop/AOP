use std::collections::HashMap;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{anyhow, Context, Result};
use serde::Serialize;
use tokio::sync::RwLock;
use tracing::{info, warn};

use aop_core::{
    discover_plugins, AgentSpec, Config, ExecRequest, PluginKind, PluginManifest, Whitelist,
};
use aop_os::{read_log_tail, ChildHandle, LogHub, ProcessManager, SpawnSpec};

use crate::cli_a2a::{CliAgentHub, CliAgentSpec};
use crate::os_client::OsClient;

#[derive(Debug, Clone, Serialize)]
pub struct StatusSnapshot {
    pub node_id: String,
    pub agent_id: String,
    pub client_version: String,
    pub gateway_url: String,
    pub mgmt_listen: String,
    pub children: Vec<ChildHandle>,
    pub registered: bool,
}

#[derive(Clone)]
struct Planned {
    id: String,
    kind: PluginKind,
    command: String,
    args: Vec<String>,
    env: HashMap<String, String>,
    workdir: Option<PathBuf>,
    health_url: Option<String>,
    plugin_root: Option<PathBuf>,
}

pub struct Supervisor {
    pub cfg: Config,
    pub whitelist: Whitelist,
    pub processes: Arc<ProcessManager>,
    pub logs: LogHub,
    pub cli_hub: Arc<CliAgentHub>,
    pub os: RwLock<OsClient>,
    pub registered: RwLock<bool>,
    planned: RwLock<HashMap<String, Planned>>,
    /// Externally running agents adopted via health check (not spawned by us).
    adopted: RwLock<HashMap<String, ChildHandle>>,
}

impl Supervisor {
    pub fn new(cfg: Config) -> Result<Arc<Self>> {
        let logs = LogHub::new(cfg.log_path())?;
        let processes = ProcessManager::new(logs.clone());
        let cli_listen: SocketAddr = cfg
            .cli_a2a_listen
            .parse()
            .with_context(|| format!("invalid cli_a2a_listen {}", cfg.cli_a2a_listen))?;
        let cli_hub = CliAgentHub::new(logs.clone(), cli_listen)?;
        let whitelist = Whitelist::from_config(&cfg.whitelist);
        let os = OsClient::new(&cfg)?;
        Ok(Arc::new(Self {
            cfg,
            whitelist,
            processes,
            logs,
            cli_hub,
            os: RwLock::new(os),
            registered: RwLock::new(false),
            planned: RwLock::new(HashMap::new()),
            adopted: RwLock::new(HashMap::new()),
        }))
    }

    /// Load manifests, autostart children, optionally register node + harnesses.
    /// Call only after mgmt HTTP is listening when `connect_os` (Gateway fetches agent card).
    pub async fn bootstrap(self: &Arc<Self>, connect_os: bool) -> Result<()> {
        self.reload_planned().await?;
        if self.cfg.autostart {
            self.start_all_planned().await?;
        }
        if connect_os {
            {
                let mut os = self.os.write().await;
                match os.register_with_retry(8).await {
                    Ok(_) => *self.registered.write().await = true,
                    Err(e) => {
                        warn!(error = %e, "continuing without successful node heartbeat");
                    }
                }
            }
            self.register_ready_children().await;
            let this = Arc::clone(self);
            tokio::spawn(async move { this.heartbeat_loop().await });
        }
        let this = Arc::clone(self);
        tokio::spawn(async move { this.reaper_loop().await });
        Ok(())
    }

    /// Wait for each planned health_url then register that A2A endpoint to Gateway.
    async fn register_ready_children(&self) {
        let planned: Vec<Planned> = self.planned.read().await.values().cloned().collect();
        let os = self.os.read().await;
        for p in planned {
            let Some(health) = p.health_url.clone() else {
                continue;
            };
            // Derive agent base URL from /health
            let endpoint = health
                .trim_end_matches('/')
                .trim_end_matches("/health")
                .to_string()
                + "/";
            match os.wait_health(&health, 25).await {
                Ok(()) => {
                    if let Err(e) = os.register_endpoint_url(&endpoint).await {
                        warn!(id = %p.id, endpoint, error = %e, "child register failed");
                    } else {
                        info!(id = %p.id, endpoint, "child registered");
                    }
                }
                Err(e) => warn!(id = %p.id, health, error = %e, "child health timeout"),
            }
        }
    }

    async fn reload_planned(&self) -> Result<()> {
        let mut map = HashMap::new();
        for a in &self.cfg.agents {
            if !a.enabled {
                continue;
            }
            map.insert(a.id.clone(), planned_from_agent(a));
        }
        for (root, m) in discover_plugins(&self.cfg.plugins_path())? {
            let mut planned = planned_from_manifest(&m, &root);
            // kind=cli shares one port; health/register URL is path-routed.
            if planned.kind.is_cli() {
                planned.health_url = Some(self.cfg.cli_agent_health_url(&planned.id));
            }
            map.insert(m.id.clone(), planned);
        }
        *self.planned.write().await = map;
        Ok(())
    }

    async fn start_all_planned(&self) -> Result<()> {
        let ids: Vec<String> = self.planned.read().await.keys().cloned().collect();
        for id in ids {
            if let Err(e) = self.start_child(&id).await {
                warn!(id, error = %e, "autostart failed");
            }
        }
        Ok(())
    }

    pub async fn start_child(&self, id: &str) -> Result<ChildHandle> {
        let planned = self
            .planned
            .read()
            .await
            .get(id)
            .cloned()
            .ok_or_else(|| anyhow!("unknown agent/plugin {id}"))?;

        if planned.kind.is_cli() {
            return self.start_cli_child(&planned).await;
        }

        // Already up (e.g. started outside aopd) — adopt without re-binding the port.
        if let Some(health) = &planned.health_url {
            let os = self.os.read().await;
            if os.wait_health(health, 2).await.is_ok() {
                info!(id, health, "already healthy; skip spawn");
                let handle = ChildHandle {
                    id: planned.id.clone(),
                    pid: 0,
                    command: planned.command.clone(),
                    args: planned.args.clone(),
                    running: true,
                };
                self.adopted
                    .write()
                    .await
                    .insert(id.to_string(), handle.clone());
                return Ok(handle);
            }
        }

        let argv: Vec<String> = std::iter::once(planned.command.clone())
            .chain(planned.args.clone())
            .collect();
        let req = ExecRequest {
            plugin_id: if self.whitelist.allows_plugin(id) {
                Some(id.to_string())
            } else {
                None
            },
            argv,
            workdir: planned.workdir.as_ref().map(|p| p.display().to_string()),
        };
        self.whitelist.check(&req).map_err(|e| anyhow!(e))?;

        let mut env = planned.env;
        // Edge SoT: supervisor owns OS heartbeats for children.
        env.insert("AOP_NODE_MANAGED".into(), "1".into());
        env.insert("HARNESS_HEARTBEAT".into(), "0".into());

        let spec = SpawnSpec {
            id: planned.id,
            command: planned.command,
            args: planned.args,
            env,
            workdir: planned.workdir,
        };
        self.processes.spawn(spec).await
    }

    async fn start_cli_child(&self, planned: &Planned) -> Result<ChildHandle> {
        if self.cli_hub.is_running(&planned.id).await {
            let list = self.cli_hub.list().await;
            if let Some(h) = list.into_iter().find(|h| h.id == planned.id) {
                return Ok(h);
            }
        }

        let plugin_root = planned
            .plugin_root
            .clone()
            .unwrap_or_else(|| PathBuf::from("."));

        // Whitelist manufacturer binary basename.
        let req = ExecRequest {
            plugin_id: if self.whitelist.allows_plugin(&planned.id) {
                Some(planned.id.clone())
            } else {
                None
            },
            argv: vec![planned.command.clone()],
            workdir: planned.workdir.as_ref().map(|p| p.display().to_string()),
        };
        self.whitelist.check(&req).map_err(|e| anyhow!(e))?;

        let spec = CliAgentSpec::try_from_planned(
            &planned.id,
            &planned.command,
            &planned.args,
            &planned.env,
            planned.workdir.as_deref(),
            self.cli_hub.base_url(),
            &plugin_root,
        )?;
        self.cli_hub.start(spec).await
    }

    pub async fn stop_child(&self, id: &str) -> Result<()> {
        self.adopted.write().await.remove(id);
        if self.cli_hub.is_running(id).await {
            return self.cli_hub.stop(id).await;
        }
        self.processes.kill(id).await
    }

    pub async fn restart_child(&self, id: &str) -> Result<ChildHandle> {
        let _ = self.stop_child(id).await;
        self.start_child(id).await
    }

    pub async fn exec_whitelisted(&self, req: ExecRequest) -> Result<ChildHandle> {
        self.whitelist.check(&req).map_err(|e| anyhow!(e))?;

        if let Some(id) = req.plugin_id.clone() {
            // Prefer planned manifest
            if self.planned.read().await.contains_key(&id) {
                return self.start_child(&id).await;
            }
        }

        let id = req
            .plugin_id
            .clone()
            .unwrap_or_else(|| format!("exec-{}", uuid::Uuid::new_v4()));
        let mut argv = req.argv;
        if argv.is_empty() {
            return Err(anyhow!("exec requires argv or known plugin_id"));
        }
        let command = argv.remove(0);
        let workdir = req.workdir.map(PathBuf::from);
        let spec = SpawnSpec {
            id,
            command,
            args: argv,
            env: HashMap::new(),
            workdir,
        };
        // Ensure id is allowlisted for one-shot: use binary check already done
        self.processes.spawn(spec).await
    }

    pub async fn status(&self) -> StatusSnapshot {
        let os = self.os.read().await;
        let mut children = self.processes.list().await;
        let managed: std::collections::HashSet<String> =
            children.iter().map(|c| c.id.clone()).collect();
        for h in self.cli_hub.list().await {
            if !managed.contains(&h.id) {
                children.push(h);
            }
        }
        let managed: std::collections::HashSet<String> =
            children.iter().map(|c| c.id.clone()).collect();
        for (id, h) in self.adopted.read().await.iter() {
            if !managed.contains(id) {
                children.push(h.clone());
            }
        }
        children.sort_by(|a, b| a.id.cmp(&b.id));
        StatusSnapshot {
            node_id: self.cfg.node_id.clone(),
            agent_id: os.agent_id.clone(),
            client_version: aop_core::CLIENT_VERSION.into(),
            gateway_url: self.cfg.gateway_url.clone(),
            mgmt_listen: self.cfg.mgmt_listen.clone(),
            children,
            registered: *self.registered.read().await,
        }
    }

    pub fn read_logs(&self, id: &str, max_bytes: u64) -> Result<String> {
        read_log_tail(self.logs.path_for(id), max_bytes)
    }

    async fn heartbeat_loop(self: Arc<Self>) {
        let interval = Duration::from_secs(self.cfg.heartbeat_interval_secs.max(5));
        loop {
            tokio::time::sleep(interval).await;
            let children = {
                let mut c = self.processes.list().await;
                for h in self.cli_hub.list().await {
                    if !c.iter().any(|x| x.id == h.id) {
                        c.push(h);
                    }
                }
                c
            };
            let planned_ids: Vec<String> = self.planned.read().await.keys().cloned().collect();
            let extra = serde_json::json!({
                "children": children.iter().map(|c| {
                    serde_json::json!({"id": c.id, "pid": c.pid, "running": c.running})
                }).collect::<Vec<_>>(),
            });
            let os = self.os.read().await;
            // Node supervisor heartbeat.
            if let Err(e) = os.heartbeat(Some(extra.clone())).await {
                warn!(error = %e, "node heartbeat error");
            }
            // Child harness lifecycle (AOP_NODE_MANAGED / cli-hosted do not self-heartbeat).
            // Skip plugins without health_url (e.g. sample echo) — they are not OS agents.
            let planned_snapshot: HashMap<String, Option<String>> = {
                let planned = self.planned.read().await;
                planned
                    .iter()
                    .map(|(k, v)| (k.clone(), v.health_url.clone()))
                    .collect()
            };
            for id in planned_ids {
                if planned_snapshot.get(&id).and_then(|u| u.as_ref()).is_none() {
                    continue;
                }
                let child_extra = serde_json::json!({
                    "managed_by": self.cfg.node_id,
                    "plugin_id": id,
                });
                if let Err(e) = os.heartbeat_agent(&id, Some(child_extra)).await {
                    warn!(agent_id = %id, error = %e, "child heartbeat error");
                }
            }
        }
    }

    async fn reaper_loop(self: Arc<Self>) {
        loop {
            tokio::time::sleep(Duration::from_secs(10)).await;
            let _ = self.processes.list().await; // drops exited entries
        }
    }

    pub async fn shutdown(&self) {
        info!("supervisor shutting down; stopping cli A2A + children");
        self.cli_hub.stop_all().await;
        self.processes.kill_all().await;
    }
}

fn planned_from_agent(a: &AgentSpec) -> Planned {
    Planned {
        id: a.id.clone(),
        kind: PluginKind::Agent,
        command: a.command.clone(),
        args: a.args.clone(),
        env: a.env.clone(),
        workdir: a.workdir.as_ref().map(PathBuf::from),
        health_url: a.health_url.clone(),
        plugin_root: None,
    }
}

fn planned_from_manifest(m: &PluginManifest, root: &std::path::Path) -> Planned {
    Planned {
        id: m.id.clone(),
        kind: m.kind,
        command: m.command.clone(),
        args: m.args.clone(),
        env: m.env.clone(),
        workdir: Some(m.workdir_path(root)),
        health_url: m.health_url.clone(),
        plugin_root: Some(root.to_path_buf()),
    }
}
