//! Per-plugin A2A HTTP hosted by aopd; tasks spawn manufacturer CLIs.

use std::collections::HashMap;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use axum::extract::{Path as AxumPath, State};
use axum::http::StatusCode;
use axum::routing::{get, post};
use axum::{Json, Router};
use aop_os::{ChildHandle, LogHub};
use serde::Deserialize;
use serde_json::{json, Value};
use tokio::io::AsyncReadExt;
use tokio::process::Command;
use tokio::sync::{oneshot, Mutex, RwLock};
use tower_http::cors::{Any, CorsLayer};
use tracing::{info, warn};
use uuid::Uuid;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CliRunnerKind {
    Claude,
    OpenClaw,
    Hermes,
    Pi,
    Deepseek,
}

impl CliRunnerKind {
    pub fn from_plugin(id: &str, command: &str, env: &HashMap<String, String>) -> Self {
        let runner = env
            .get("HARNESS_RUNNER")
            .map(|s| s.to_ascii_lowercase())
            .unwrap_or_default();
        let id_l = id.to_ascii_lowercase();
        let cmd_l = command.to_ascii_lowercase();
        if runner.contains("openclaw") || id_l.contains("openclaw") || cmd_l.contains("openclaw") {
            return Self::OpenClaw;
        }
        if runner.contains("hermes") || id_l.contains("hermes") || cmd_l.contains("hermes") {
            return Self::Hermes;
        }
        if runner.contains("pi") || id_l == "pi" || id_l.starts_with("pi-") || cmd_l == "pi" {
            return Self::Pi;
        }
        if runner.contains("deepseek")
            || runner == "dsh"
            || id_l.contains("deepseek")
            || cmd_l == "dsh"
        {
            return Self::Deepseek;
        }
        Self::Claude
    }

    fn path_env_keys(self) -> &'static [&'static str] {
        match self {
            Self::Claude => &["CLAUDE_CLI_PATH"],
            Self::OpenClaw => &["OPENCLAW_CLI_PATH"],
            Self::Hermes => &["HERMES_CLI_PATH"],
            Self::Pi => &["PI_CLI_PATH"],
            Self::Deepseek => &["DSH_CLI_PATH"],
        }
    }
}

#[derive(Debug, Clone)]
pub struct CliAgentSpec {
    pub id: String,
    pub command: String,
    pub args: Vec<String>,
    pub env: HashMap<String, String>,
    pub workdir: Option<PathBuf>,
    /// Public A2A base for this agent, e.g. `http://127.0.0.1:8020/agents/claude-code`.
    pub endpoint_base: String,
    pub health_url: String,
    pub agent_id: String,
    pub display_name: String,
    pub runner: CliRunnerKind,
    pub profile: String,
    pub plugin_root: PathBuf,
}

impl CliAgentSpec {
    pub fn try_from_planned(
        id: &str,
        command: &str,
        args: &[String],
        env: &HashMap<String, String>,
        workdir: Option<&Path>,
        cli_a2a_base: &str,
        plugin_root: &Path,
    ) -> Result<Self> {
        let runner = CliRunnerKind::from_plugin(id, command, env);
        let profile = env
            .get("HARNESS_PROFILE")
            .cloned()
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| id.to_string());
        let agent_id = env
            .get("AGENT_ID")
            .cloned()
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| id.to_string());
        let display_name = env
            .get("AGENT_DISPLAY_NAME")
            .cloned()
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| agent_id.clone());
        let endpoint_base = format!(
            "{}/agents/{}",
            cli_a2a_base.trim().trim_end_matches('/'),
            id
        );
        let health_url = format!("{endpoint_base}/health");
        Ok(Self {
            id: id.to_string(),
            command: command.to_string(),
            args: args.to_vec(),
            env: env.clone(),
            workdir: workdir.map(|p| p.to_path_buf()),
            endpoint_base,
            health_url,
            agent_id,
            display_name,
            runner,
            profile,
            plugin_root: plugin_root.to_path_buf(),
        })
    }

    pub fn resolve_binary(&self) -> Option<PathBuf> {
        for key in self.runner.path_env_keys() {
            if let Some(v) = self.env.get(*key).map(|s| s.trim()).filter(|s| !s.is_empty()) {
                let p = PathBuf::from(v);
                if p.is_file() {
                    return Some(prefer_spawnable(p));
                }
            }
            if let Ok(v) = std::env::var(key) {
                let v = v.trim();
                if !v.is_empty() {
                    let p = PathBuf::from(v);
                    if p.is_file() {
                        return Some(prefer_spawnable(p));
                    }
                }
            }
        }
        which_binary(&self.command)
    }

    pub fn runner_ready(&self) -> (bool, String, String) {
        match self.resolve_binary() {
            Some(p) => (true, "cli".into(), p.display().to_string()),
            None => (
                false,
                "missing_cli".into(),
                format!(
                    "manufacturer CLI {:?} not found on PATH (or *_CLI_PATH)",
                    self.command
                ),
            ),
        }
    }
}

fn which_binary(name: &str) -> Option<PathBuf> {
    let name = name.trim();
    if name.is_empty() {
        return None;
    }
    let as_path = PathBuf::from(name);
    if as_path.is_file() {
        return Some(prefer_spawnable(as_path));
    }
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        // Windows npm shims ship an extensionless script plus `.cmd`.
        // CreateProcess cannot run the extensionless file (os error 193).
        #[cfg(windows)]
        {
            for ext in ["exe", "cmd", "bat"] {
                let c = dir.join(format!("{name}.{ext}"));
                if c.is_file() {
                    return Some(c);
                }
            }
        }
        let candidate = dir.join(name);
        if candidate.is_file() {
            return Some(prefer_spawnable(candidate));
        }
    }
    None
}

/// If `path` is an extensionless or PowerShell npm shim, use a sibling `.cmd`/`.exe`/`.bat`.
fn prefer_spawnable(path: PathBuf) -> PathBuf {
    #[cfg(windows)]
    {
        let ext = path
            .extension()
            .and_then(|e| e.to_str())
            .unwrap_or("")
            .to_ascii_lowercase();
        if matches!(ext.as_str(), "exe" | "cmd" | "bat" | "com") {
            return path;
        }
        let stem = path.file_stem().and_then(|s| s.to_str()).unwrap_or("");
        if let Some(parent) = path.parent() {
            for alt in ["exe", "cmd", "bat"] {
                let c = parent.join(format!("{stem}.{alt}"));
                if c.is_file() {
                    return c;
                }
            }
        }
    }
    path
}

fn find_repo_root(start: &Path) -> Option<PathBuf> {
    if let Ok(env) = std::env::var("AOP_REPO_ROOT") {
        let p = PathBuf::from(env.trim());
        if (p.join("agents/harness-agent/agent.py")).is_file() {
            return Some(p);
        }
    }
    let mut cur = start.to_path_buf();
    for _ in 0..12 {
        if (cur.join("agents/harness-agent/agent.py")).is_file()
            && cur.join("packages/agent-runtime").is_dir()
        {
            return Some(cur);
        }
        if !cur.pop() {
            break;
        }
    }
    None
}

fn load_agent_card(spec: &CliAgentSpec) -> Value {
    let url = format!("{}/", spec.endpoint_base.trim_end_matches('/'));
    let mut card = json!({
        "name": spec.display_name,
        "description": format!("aopd-hosted {} (manufacturer CLI)", spec.id),
        "url": url,
        "version": aop_core::CLIENT_VERSION,
        "protocolVersion": "0.3.0",
        "preferredTransport": "JSONRPC",
        "capabilities": { "streaming": false, "pushNotifications": false },
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text", "application/json"],
        "skills": []
    });
    if let Some(repo) = find_repo_root(&spec.plugin_root) {
        let path = repo
            .join("agents/harness-agent/profiles")
            .join(&spec.profile)
            .join("agent-card.json");
        if let Ok(text) = std::fs::read_to_string(&path) {
            if let Ok(mut loaded) = serde_json::from_str::<Value>(&text) {
                if let Some(obj) = loaded.as_object_mut() {
                    obj.insert("url".into(), json!(url));
                    // Keep a distinct Gateway agent_key (e.g. "aopd Pi" → aopd-pi).
                    // Profile cards reuse the legacy name ("Pi") and would overwrite :8013.
                    obj.insert("name".into(), json!(spec.display_name));
                }
                card = loaded;
            }
        }
    }
    card
}

struct AgentState {
    spec: CliAgentSpec,
    logs: LogHub,
    tasks: RwLock<HashMap<String, Value>>,
    approvals: Mutex<Vec<Value>>,
}

struct ServerCtl {
    shutdown: Option<oneshot::Sender<()>>,
    join: tokio::task::JoinHandle<()>,
}

/// Hub of aopd-hosted CLI A2A agents — **one shared listen**, path `/agents/<id>/…`.
pub struct CliAgentHub {
    agents: RwLock<HashMap<String, Arc<AgentState>>>,
    handles: Mutex<HashMap<String, ChildHandle>>,
    logs: LogHub,
    listen: SocketAddr,
    base_url: String,
    server: Mutex<Option<ServerCtl>>,
}

impl CliAgentHub {
    pub fn new(logs: LogHub, listen: SocketAddr) -> Result<Arc<Self>> {
        if !listen.ip().is_loopback() {
            anyhow::bail!("cli_a2a_listen must be loopback, got {}", listen.ip());
        }
        let base_url = format!("http://{listen}");
        Ok(Arc::new(Self {
            agents: RwLock::new(HashMap::new()),
            handles: Mutex::new(HashMap::new()),
            logs,
            listen,
            base_url,
            server: Mutex::new(None),
        }))
    }

    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    pub async fn list(&self) -> Vec<ChildHandle> {
        let map = self.handles.lock().await;
        map.values().cloned().collect()
    }

    pub async fn is_running(&self, id: &str) -> bool {
        self.agents.read().await.contains_key(id)
    }

    async fn ensure_server(self: &Arc<Self>) -> Result<()> {
        let mut guard = self.server.lock().await;
        if guard.is_some() {
            return Ok(());
        }
        let hub = Arc::clone(self);
        let app = Router::new()
            .route("/health", get(hub_health))
            .route("/agents/:id/health", get(agent_health))
            .route("/agents/:id/.well-known/agent-card.json", get(agent_card))
            .route("/agents/:id/.well-known/agent.json", get(agent_card))
            .route("/agents/:id/", post(jsonrpc))
            .route("/agents/:id", post(jsonrpc))
            .route(
                "/agents/:id/v1/approvals",
                get(list_approvals).post(create_approval),
            )
            .route("/agents/:id/v1/approvals/:approval_id/decide", post(decide_approval))
            .layer(
                CorsLayer::new()
                    .allow_origin(Any)
                    .allow_methods(Any)
                    .allow_headers(Any),
            )
            .with_state(hub);

        let listener = tokio::net::TcpListener::bind(self.listen)
            .await
            .with_context(|| format!("bind shared cli A2A {}", self.listen))?;
        let (shutdown_tx, shutdown_rx) = oneshot::channel::<()>();
        let listen = self.listen;
        let join = tokio::spawn(async move {
            info!(%listen, "shared cli A2A listening");
            let serve = axum::serve(listener, app).with_graceful_shutdown(async {
                let _ = shutdown_rx.await;
            });
            if let Err(e) = serve.await {
                warn!(error = %e, "shared cli A2A server exited");
            }
        });
        *guard = Some(ServerCtl {
            shutdown: Some(shutdown_tx),
            join,
        });
        Ok(())
    }

    async fn shutdown_server(&self) {
        let mut guard = self.server.lock().await;
        if let Some(mut ctl) = guard.take() {
            if let Some(tx) = ctl.shutdown.take() {
                let _ = tx.send(());
            }
            let _ = tokio::time::timeout(Duration::from_secs(3), ctl.join).await;
            info!(listen = %self.listen, "shared cli A2A stopped");
        }
    }

    pub async fn start(self: &Arc<Self>, spec: CliAgentSpec) -> Result<ChildHandle> {
        {
            let map = self.handles.lock().await;
            if let Some(h) = map.get(&spec.id) {
                return Ok(h.clone());
            }
        }

        let (ready, mode, reason) = spec.runner_ready();
        info!(
            id = %spec.id,
            binary = %spec.command,
            runner_ready = ready,
            %mode,
            %reason,
            endpoint = %spec.endpoint_base,
            listen = %self.listen,
            "registering cli agent on shared A2A port"
        );

        let state = Arc::new(AgentState {
            spec: spec.clone(),
            logs: self.logs.clone(),
            tasks: RwLock::new(HashMap::new()),
            approvals: Mutex::new(Vec::new()),
        });
        self.agents
            .write()
            .await
            .insert(spec.id.clone(), state);

        self.ensure_server().await?;

        let handle = ChildHandle {
            id: spec.id.clone(),
            pid: 0,
            command: spec.command.clone(),
            args: spec.args.clone(),
            running: true,
        };
        self.handles
            .lock()
            .await
            .insert(spec.id.clone(), handle.clone());
        let _ = self.logs.append(
            &spec.id,
            &format!(
                "[sys] cli A2A {} runner_ready={} ({}) shared_listen={}",
                spec.endpoint_base, ready, reason, self.listen
            ),
        );
        Ok(handle)
    }

    pub async fn stop(&self, id: &str) -> Result<()> {
        self.agents.write().await.remove(id);
        self.handles.lock().await.remove(id);
        let empty = self.agents.read().await.is_empty();
        if empty {
            self.shutdown_server().await;
        }
        let _ = self.logs.append(id, "[sys] cli agent unregistered from shared A2A");
        info!(id, "cli agent stopped");
        Ok(())
    }

    pub async fn stop_all(&self) {
        self.agents.write().await.clear();
        self.handles.lock().await.clear();
        self.shutdown_server().await;
    }

    async fn agent(&self, id: &str) -> Result<Arc<AgentState>, (StatusCode, String)> {
        self.agents
            .read()
            .await
            .get(id)
            .cloned()
            .ok_or_else(|| (StatusCode::NOT_FOUND, format!("unknown cli agent {id}")))
    }
}

async fn hub_health(State(hub): State<Arc<CliAgentHub>>) -> Json<Value> {
    let agents = hub.agents.read().await;
    let mut list = Vec::new();
    for (id, st) in agents.iter() {
        let (ready, mode, reason) = st.spec.runner_ready();
        list.push(json!({
            "id": id,
            "agent": st.spec.agent_id,
            "runner_ready": ready,
            "runner_mode": mode,
            "runner_reason": reason,
            "endpoint": format!("{}/", st.spec.endpoint_base.trim_end_matches('/')),
        }));
    }
    list.sort_by(|a, b| {
        a.get("id")
            .and_then(|v| v.as_str())
            .cmp(&b.get("id").and_then(|v| v.as_str()))
    });
    Json(json!({
        "ok": true,
        "service": "aopd-cli-a2a",
        "listen": hub.listen.to_string(),
        "agents": list,
    }))
}

fn health_body(st: &AgentState) -> Value {
    let (ready, mode, reason) = st.spec.runner_ready();
    // approvals.len needs async — callers that need pending use agent_health
    json!({
        "status": if ready { "ok" } else { "degraded" },
        "agent": st.spec.agent_id,
        "plugin_id": st.spec.id,
        "version": aop_core::CLIENT_VERSION,
        "harness": true,
        "runner": format!("{:?}", st.spec.runner),
        "asyncSpawn": true,
        "runner_ready": ready,
        "runner_mode": mode,
        "runner_reason": reason,
        "peer_approval": true,
        "hosted_by": "aopd",
        "endpoint": format!("{}/", st.spec.endpoint_base.trim_end_matches('/')),
    })
}

async fn agent_health(
    State(hub): State<Arc<CliAgentHub>>,
    AxumPath(id): AxumPath<String>,
) -> Result<Json<Value>, (StatusCode, String)> {
    let st = hub.agent(&id).await?;
    let mut body = health_body(&st);
    let pending = st.approvals.lock().await.len();
    if let Some(obj) = body.as_object_mut() {
        obj.insert("pending_approvals".into(), json!(pending));
    }
    Ok(Json(body))
}

async fn agent_card(
    State(hub): State<Arc<CliAgentHub>>,
    AxumPath(id): AxumPath<String>,
) -> Result<Json<Value>, (StatusCode, String)> {
    let st = hub.agent(&id).await?;
    Ok(Json(load_agent_card(&st.spec)))
}

async fn list_approvals(
    State(hub): State<Arc<CliAgentHub>>,
    AxumPath(id): AxumPath<String>,
) -> Result<Json<Value>, (StatusCode, String)> {
    let st = hub.agent(&id).await?;
    let items = st.approvals.lock().await.clone();
    Ok(Json(json!({ "approvals": items })))
}

#[derive(Deserialize)]
struct ApprovalBody {
    #[serde(default)]
    task_id: Option<String>,
    #[serde(default)]
    reason: Option<String>,
    #[serde(flatten)]
    extra: HashMap<String, Value>,
}

async fn create_approval(
    State(hub): State<Arc<CliAgentHub>>,
    AxumPath(agent_id): AxumPath<String>,
    Json(body): Json<ApprovalBody>,
) -> Result<Json<Value>, (StatusCode, String)> {
    let st = hub.agent(&agent_id).await?;
    let id = Uuid::new_v4().to_string();
    let rec = json!({
        "id": id,
        "status": "pending",
        "task_id": body.task_id,
        "reason": body.reason,
        "extra": body.extra,
    });
    st.approvals.lock().await.push(rec.clone());
    Ok(Json(rec))
}

#[derive(Deserialize)]
struct DecideBody {
    #[serde(default)]
    approved: bool,
    #[serde(default)]
    reason: Option<String>,
}

async fn decide_approval(
    State(hub): State<Arc<CliAgentHub>>,
    AxumPath((agent_id, approval_id)): AxumPath<(String, String)>,
    Json(body): Json<DecideBody>,
) -> Result<Json<Value>, (StatusCode, String)> {
    let st = hub.agent(&agent_id).await?;
    let mut items = st.approvals.lock().await;
    let Some(rec) = items
        .iter_mut()
        .find(|r| r.get("id").and_then(|v| v.as_str()) == Some(approval_id.as_str()))
    else {
        return Err((
            StatusCode::NOT_FOUND,
            format!("approval {approval_id} not found"),
        ));
    };
    rec["status"] = json!(if body.approved { "approved" } else { "rejected" });
    if let Some(r) = body.reason {
        rec["decision_reason"] = json!(r);
    }
    Ok(Json(rec.clone()))
}

#[derive(Deserialize)]
struct JsonRpcReq {
    #[serde(default)]
    jsonrpc: String,
    #[serde(default)]
    id: Value,
    #[serde(default)]
    method: String,
    #[serde(default)]
    params: Value,
}

async fn jsonrpc(
    State(hub): State<Arc<CliAgentHub>>,
    AxumPath(agent_id): AxumPath<String>,
    Json(req): Json<JsonRpcReq>,
) -> Result<Json<Value>, (StatusCode, String)> {
    let st = hub.agent(&agent_id).await?;
    let req_id = req.id;
    let body = match req.method.as_str() {
        "message/send" => handle_message_send(st, req_id, req.params).await,
        "tasks/get" => {
            let task_id = req
                .params
                .get("id")
                .or_else(|| req.params.get("taskId"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let tasks = st.tasks.read().await;
            if let Some(t) = tasks.get(task_id) {
                json!({"jsonrpc": "2.0", "id": req_id, "result": t})
            } else {
                json!({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32001, "message": format!("task not found: {task_id}")}
                })
            }
        }
        "tasks/cancel" => {
            let task_id = req
                .params
                .get("id")
                .or_else(|| req.params.get("taskId"))
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let mut tasks = st.tasks.write().await;
            if let Some(t) = tasks.get_mut(&task_id) {
                t["status"] = json!({"state": "canceled"});
                json!({"jsonrpc": "2.0", "id": req_id, "result": t})
            } else {
                json!({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32001, "message": format!("task not found: {task_id}")}
                })
            }
        }
        other => json!({
            "jsonrpc": req.jsonrpc.if_empty("2.0"),
            "id": req_id,
            "error": {"code": -32601, "message": format!("Method not found: {other}")}
        }),
    };
    Ok(Json(body))
}

trait IfEmpty {
    fn if_empty(self, default: &str) -> String;
}
impl IfEmpty for String {
    fn if_empty(self, default: &str) -> String {
        if self.is_empty() {
            default.into()
        } else {
            self
        }
    }
}

fn extract_text(message: &Value) -> String {
    let parts = message.get("parts").and_then(|p| p.as_array());
    let Some(parts) = parts else {
        return String::new();
    };
    parts
        .iter()
        .filter_map(|p| {
            if p.get("kind").and_then(|t| t.as_str()) == Some("text") {
                p.get("text").and_then(|t| t.as_str())
            } else {
                None
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
        .trim()
        .to_string()
}

fn wants_async(params: &Value) -> bool {
    let meta = params.get("metadata");
    if meta.and_then(|m| m.get("async")).and_then(|v| v.as_bool()) == Some(true) {
        return true;
    }
    if params.get("async").and_then(|v| v.as_bool()) == Some(true) {
        return true;
    }
    meta.and_then(|m| m.get("callbackUrl"))
        .and_then(|v| v.as_str())
        .map(|s| !s.is_empty())
        .unwrap_or(false)
}

async fn handle_message_send(st: Arc<AgentState>, req_id: Value, params: Value) -> Value {
    let (ready, _mode, reason) = st.spec.runner_ready();
    if !ready {
        return json!({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32003, "message": format!("runner not ready: {reason}")}
        });
    }

    let message = params.get("message").cloned().unwrap_or(json!({}));
    let prompt = extract_text(&message);
    if prompt.is_empty() {
        return json!({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32602, "message": "message has no text parts"}
        });
    }

    let client_id = params
        .get("id")
        .or_else(|| params.get("taskId"))
        .or_else(|| params.get("task_id"))
        .cloned();
    let task_id = client_id
        .and_then(|v| v.as_str().map(|s| s.to_string()))
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| Uuid::new_v4().to_string());

    let system = message
        .get("metadata")
        .and_then(|m| m.get("systemPrompt"))
        .and_then(|s| s.as_str())
        .unwrap_or("")
        .to_string();

    let mut task = json!({
        "id": task_id,
        "status": {"state": "working"},
        "artifacts": [],
        "metadata": {
            "agentId": st.spec.agent_id,
            "hostedBy": "aopd",
            "runner": format!("{:?}", st.spec.runner),
        }
    });
    st.tasks
        .write()
        .await
        .insert(task_id.clone(), task.clone());

    if wants_async(&params) {
        task["status"] = json!({"state": "submitted"});
        st.tasks
            .write()
            .await
            .insert(task_id.clone(), task.clone());
        let st2 = Arc::clone(&st);
        let tid = task_id.clone();
        tokio::spawn(async move {
            let result = run_manufacturer_cli(&st2, &tid, &prompt, &system).await;
            let mut tasks = st2.tasks.write().await;
            tasks.insert(tid, result);
        });
        return json!({"jsonrpc": "2.0", "id": req_id, "result": task});
    }

    let result = run_manufacturer_cli(&st, &task_id, &prompt, &system).await;
    st.tasks
        .write()
        .await
        .insert(task_id.clone(), result.clone());
    // Sync: surface CLI/HTTP failures as JSON-RPC errors (task still in `data`).
    if result
        .pointer("/status/state")
        .and_then(|v| v.as_str())
        == Some("failed")
    {
        let msg = result
            .pointer("/metadata/error")
            .and_then(|v| v.as_str())
            .or_else(|| result.get("text").and_then(|v| v.as_str()))
            .unwrap_or("cli task failed");
        let code = result
            .pointer("/metadata/httpStatus")
            .and_then(|v| v.as_u64())
            .map(|h| h as i64)
            .or_else(|| {
                result
                    .pointer("/error/code")
                    .and_then(|v| v.as_i64())
            })
            .unwrap_or(-32000);
        return json!({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": code,
                "message": msg,
                "data": result,
            }
        });
    }
    json!({"jsonrpc": "2.0", "id": req_id, "result": result})
}

fn harness_workdir(spec: &CliAgentSpec, task_id: &str) -> PathBuf {
    let root = spec
        .env
        .get("HARNESS_WORKDIR")
        .cloned()
        .filter(|s| !s.trim().is_empty())
        .or_else(|| std::env::var("HARNESS_WORKDIR").ok())
        .filter(|s| !s.trim().is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            dirs::home_dir()
                .unwrap_or_else(|| PathBuf::from("."))
                .join(".aop")
                .join("workspaces")
        });
    let dir = root.join(task_id).join(&spec.agent_id);
    let _ = std::fs::create_dir_all(&dir);
    dir
}

async fn run_manufacturer_cli(
    st: &AgentState,
    task_id: &str,
    prompt: &str,
    system: &str,
) -> Value {
    let binary = match st.spec.resolve_binary() {
        Some(b) => b,
        None => {
            return failed_task_simple(task_id, &st.spec, "manufacturer CLI not found");
        }
    };
    let cwd = harness_workdir(&st.spec, task_id);
    let timeout_s: u64 = st
        .spec
        .env
        .get("HARNESS_CLI_TIMEOUT_S")
        .and_then(|s| s.parse().ok())
        .or_else(|| std::env::var("HARNESS_CLI_TIMEOUT_S").ok().and_then(|s| s.parse().ok()))
        .unwrap_or(600);

    let (argv, cleanup) = match build_argv(&st.spec, &binary, prompt, system, &cwd) {
        Ok(v) => v,
        Err(e) => return failed_task_simple(task_id, &st.spec, &e.to_string()),
    };

    let _ = st.logs.append(
        &st.spec.id,
        &format!("[sys] spawn task={task_id} argv={argv:?} cwd={}", cwd.display()),
    );

    let mut cmd = Command::new(&argv[0]);
    cmd.args(&argv[1..])
        .current_dir(&cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    for (k, v) in &st.spec.env {
        if !v.is_empty() {
            cmd.env(k, v);
        }
    }
    if st.spec.runner == CliRunnerKind::Claude {
        cmd.env(
            "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT",
            "1",
        );
    }
    if st.spec.runner == CliRunnerKind::Deepseek {
        let dsh_home = st
            .spec
            .env
            .get("DSH_HOME")
            .cloned()
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| {
                dirs::home_dir()
                    .unwrap_or_else(|| PathBuf::from("."))
                    .join(".aop")
                    .join("dsh-home")
                    .display()
                    .to_string()
            });
        let _ = std::fs::create_dir_all(&dsh_home);
        cmd.env("DSH_HOME", &dsh_home);
        if !system.is_empty() {
            cmd.env("DSH_SYSTEM_PROMPT", system);
        }
    }

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            cleanup_temp(cleanup);
            return failed_task_simple(task_id, &st.spec, &format!("spawn failed: {e}"));
        }
    };
    let pid = child.id().unwrap_or(0);
    let _ = st
        .logs
        .append(&st.spec.id, &format!("[sys] task={task_id} pid={pid}"));

    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    // Read concurrently so a full pipe cannot deadlock the child. After the
    // process exits, do not wait forever for EOF — some CLIs leave the pipe open.
    let out_buf = Arc::new(Mutex::new(Vec::<u8>::new()));
    let err_buf = Arc::new(Mutex::new(Vec::<u8>::new()));
    let out_task = tokio::spawn(drain_pipe(stdout, Arc::clone(&out_buf)));
    let err_task = tokio::spawn(drain_pipe(stderr, Arc::clone(&err_buf)));

    let waited = tokio::time::timeout(Duration::from_secs(timeout_s), child.wait()).await;
    let timed_out = waited.is_err();
    if timed_out {
        let _ = child.start_kill();
        let _ = child.wait().await;
    }
    let _ = tokio::time::timeout(Duration::from_secs(2), out_task).await;
    let _ = tokio::time::timeout(Duration::from_secs(2), err_task).await;
    let stdout_text = String::from_utf8_lossy(&out_buf.lock().await).into_owned();
    let stderr_text = String::from_utf8_lossy(&err_buf.lock().await).into_owned();

    let outcome = match waited {
        Ok(Ok(s)) => (stdout_text, stderr_text, s.code(), s.success(), false),
        Ok(Err(e)) => (
            stdout_text,
            format!("wait failed: {e}\n{stderr_text}"),
            None,
            false,
            false,
        ),
        Err(_) => (
            stdout_text,
            if stderr_text.is_empty() {
                format!("timeout after {timeout_s}s")
            } else {
                stderr_text
            },
            None,
            false,
            true,
        ),
    };
    cleanup_temp(cleanup);
    let (stdout, stderr, exit_code, exit_ok, timed_out) = outcome;
    if !stdout.is_empty() {
        let _ = st
            .logs
            .append(&st.spec.id, &format!("[stdout] {}", truncate(&stdout, 4000)));
    }
    if !stderr.is_empty() {
        let _ = st
            .logs
            .append(&st.spec.id, &format!("[stderr] {}", truncate(&stderr, 2000)));
    }

    let text = extract_cli_text(st.spec.runner, &stdout);
    let http_status = detect_http_status(&stdout, &stderr);

    if timed_out {
        return failed_task(
            task_id,
            &st.spec,
            &format!("timeout after {timeout_s}s"),
            exit_code,
            http_status,
            &stdout,
            &stderr,
            binary.display().to_string(),
        );
    }

    // Soft failures: CLI may exit 0 while printing API Error / HTTP 4xx/5xx.
    if let Some(soft) = detect_cli_failure_message(&stdout, &stderr, http_status) {
        return failed_task(
            task_id,
            &st.spec,
            &soft,
            exit_code,
            http_status,
            &stdout,
            &stderr,
            binary.display().to_string(),
        );
    }

    // Any non-success exit code is a failure (compatible with all OS exit codes).
    if !exit_ok {
        let code_label = exit_code
            .map(|c| c.to_string())
            .unwrap_or_else(|| "unknown".into());
        let detail = if !stderr.trim().is_empty() {
            truncate(&stderr, 800)
        } else if !stdout.trim().is_empty() {
            truncate(&stdout, 800)
        } else {
            "no output".into()
        };
        return failed_task(
            task_id,
            &st.spec,
            &format!("cli exited {code_label}: {detail}"),
            exit_code,
            http_status,
            &stdout,
            &stderr,
            binary.display().to_string(),
        );
    }

    if text.trim().is_empty() {
        return failed_task(
            task_id,
            &st.spec,
            "cli exited 0 but produced no output",
            exit_code,
            http_status,
            &stdout,
            &stderr,
            binary.display().to_string(),
        );
    }

    json!({
        "id": task_id,
        "status": {"state": "completed"},
        "text": text,
        "artifacts": [{
            "name": "output",
            "parts": [{"type": "text", "text": text}]
        }],
        "metadata": {
            "agentId": st.spec.agent_id,
            "hostedBy": "aopd",
            "runner": format!("{:?}", st.spec.runner),
            "exitCode": exit_code.unwrap_or(0),
            "binary": binary.display().to_string(),
        }
    })
}

fn failed_task_simple(task_id: &str, spec: &CliAgentSpec, err: &str) -> Value {
    failed_task(
        task_id,
        spec,
        err,
        None,
        None,
        "",
        "",
        spec.command.clone(),
    )
}

fn failed_task(
    task_id: &str,
    spec: &CliAgentSpec,
    err: &str,
    exit_code: Option<i32>,
    http_status: Option<u16>,
    stdout: &str,
    stderr: &str,
    binary: String,
) -> Value {
    let mut meta = json!({
        "agentId": spec.agent_id,
        "hostedBy": "aopd",
        "runner": format!("{:?}", spec.runner),
        "error": err,
        "binary": binary,
    });
    if let Some(c) = exit_code {
        meta["exitCode"] = json!(c);
    }
    if let Some(h) = http_status {
        meta["httpStatus"] = json!(h);
    }
    if !stdout.is_empty() {
        meta["stdoutPreview"] = json!(truncate(stdout, 1000));
    }
    if !stderr.is_empty() {
        meta["stderrPreview"] = json!(truncate(stderr, 500));
    }
    json!({
        "id": task_id,
        "status": {"state": "failed", "message": err},
        "text": err,
        "artifacts": [],
        "metadata": meta,
        "error": {
            "code": http_status.map(|h| h as i64).unwrap_or(-32000),
            "message": err,
        }
    })
}

/// Pull an HTTP-like status (e.g. 429) from CLI stdout/stderr when present.
fn detect_http_status(stdout: &str, stderr: &str) -> Option<u16> {
    capture_http_status(&format!("{stdout}\n{stderr}"))
}

fn capture_http_status(text: &str) -> Option<u16> {
    // Lightweight scan without regex crate: find 3-digit codes near error keywords.
    let lower = text.to_ascii_lowercase();
    let keywords = [
        "api error",
        "request rejected",
        "http ",
        "status",
        "error",
        "rate limit",
        "unauthorized",
        "forbidden",
        "not found",
    ];
    let mut best: Option<u16> = None;
    let bytes = lower.as_bytes();
    let mut i = 0;
    while i + 2 < bytes.len() {
        if bytes[i].is_ascii_digit()
            && bytes[i + 1].is_ascii_digit()
            && bytes[i + 2].is_ascii_digit()
            && (i == 0 || !bytes[i - 1].is_ascii_digit())
            && (i + 3 >= bytes.len() || !bytes[i + 3].is_ascii_digit())
        {
            let code: u16 = lower[i..i + 3].parse().unwrap_or(0);
            if (400..600).contains(&code) {
                let start = i.saturating_sub(80);
                let window = &lower[start..i];
                let paren = i > 0 && bytes[i - 1] == b'(';
                if paren || keywords.iter().any(|k| window.contains(k)) {
                    best = Some(code);
                    if code == 429 || (500..600).contains(&code) {
                        return Some(code);
                    }
                }
            }
            i += 3;
            continue;
        }
        i += 1;
    }
    best
}

fn detect_cli_failure_message(
    stdout: &str,
    stderr: &str,
    http_status: Option<u16>,
) -> Option<String> {
    let combined = format!("{stdout}\n{stderr}");
    let lower = combined.to_ascii_lowercase();

    if let Some(code) = http_status {
        if code >= 400 {
            // Keep the most informative line containing the status.
            let line = combined
                .lines()
                .find(|l| l.contains(&code.to_string()))
                .unwrap_or(combined.lines().next().unwrap_or(combined.trim()));
            return Some(format!("HTTP {code}: {}", truncate(line.trim(), 500)));
        }
    }

    const MARKERS: &[&str] = &[
        "api error:",
        "request rejected",
        "rate limit",
        "authentication_error",
        "permission denied",
        "insufficient_quota",
        "model service has been paused",
        "usage limit",
    ];
    for marker in MARKERS {
        if lower.contains(marker) {
            let line = combined
                .lines()
                .find(|l| l.to_ascii_lowercase().contains(marker))
                .unwrap_or(combined.trim());
            return Some(truncate(line.trim(), 500));
        }
    }
    None
}

fn truncate(s: &str, n: usize) -> String {
    if s.len() <= n {
        s.to_string()
    } else {
        format!("{}…", &s[..n])
    }
}

struct TempCleanup(Vec<PathBuf>);

async fn drain_pipe<R>(pipe: Option<R>, buf: Arc<Mutex<Vec<u8>>>)
where
    R: tokio::io::AsyncRead + Unpin,
{
    let Some(mut pipe) = pipe else {
        return;
    };
    let mut tmp = [0u8; 8192];
    loop {
        match pipe.read(&mut tmp).await {
            Ok(0) | Err(_) => break,
            Ok(n) => buf.lock().await.extend_from_slice(&tmp[..n]),
        }
    }
}

fn cleanup_temp(c: TempCleanup) {
    for p in c.0 {
        let _ = std::fs::remove_file(p);
    }
}

fn build_argv(
    spec: &CliAgentSpec,
    binary: &Path,
    prompt: &str,
    system: &str,
    cwd: &Path,
) -> Result<(Vec<String>, TempCleanup)> {
    let mut cleanup = TempCleanup(Vec::new());
    let bin = binary.display().to_string();
    let composed = if system.is_empty() {
        prompt.to_string()
    } else {
        format!("{system}\n\n{prompt}")
    };

    let argv = match spec.runner {
        CliRunnerKind::Claude => {
            let permission = spec
                .env
                .get("CLAUDE_CLI_PERMISSION_MODE")
                .cloned()
                .unwrap_or_else(|| "bypassPermissions".into());
            let allowed = spec
                .env
                .get("CLAUDE_CLI_ALLOWED_TOOLS")
                .cloned()
                .unwrap_or_else(|| {
                    "WebSearch,WebFetch,Bash,Read,Edit,Write,Glob,Grep,Agent,Skill,TodoWrite"
                        .into()
                });
            let mut a = vec![
                bin,
                "-p".into(),
                composed,
                "--output-format".into(),
                "text".into(),
                "--permission-mode".into(),
                permission.clone(),
            ];
            // bypassPermissions still prompts unless this flag is set; otherwise
            // print mode can sit until the process is killed.
            if permission.eq_ignore_ascii_case("bypassPermissions") {
                a.push("--dangerously-skip-permissions".into());
            }
            if !allowed.is_empty() && !matches!(allowed.to_ascii_lowercase().as_str(), "-" | "none" | "off")
            {
                a.extend(["--allowed-tools".into(), allowed]);
            }
            a.extend(spec.args.iter().cloned());
            a
        }
        CliRunnerKind::OpenClaw => {
            let path = std::env::temp_dir().join(format!("aop-openclaw-{}.txt", Uuid::new_v4()));
            std::fs::write(&path, &composed)?;
            cleanup.0.push(path.clone());
            let mut a = vec![
                bin,
                "agent".into(),
                "exec".into(),
                "--cwd".into(),
                cwd.display().to_string(),
                "--message-file".into(),
                path.display().to_string(),
                "--json".into(),
            ];
            if let Some(model) = spec.env.get("OPENCLAW_MODEL").filter(|s| !s.is_empty()) {
                a.extend(["--model".into(), model.clone()]);
            }
            a.extend(spec.args.iter().cloned());
            a
        }
        CliRunnerKind::Hermes => {
            let mut a = vec![
                bin,
                "--yolo".into(),
                "--accept-hooks".into(),
                "-z".into(),
                composed,
            ];
            a.extend(spec.args.iter().cloned());
            a
        }
        CliRunnerKind::Pi => {
            let mut a = vec![
                bin,
                "--mode".into(),
                "json".into(),
                "--print".into(),
                "--no-session".into(),
            ];
            if !system.is_empty() {
                let path = std::env::temp_dir().join(format!("aop-pi-sys-{}.md", Uuid::new_v4()));
                std::fs::write(&path, system)?;
                cleanup.0.push(path.clone());
                a.extend([
                    "--system-prompt".into(),
                    "You are Pi Agent. Follow the user goal carefully.".into(),
                    "--append-system-prompt".into(),
                    path.display().to_string(),
                ]);
            }
            a.extend(spec.args.iter().cloned());
            // Pass prompt via @file: Windows CreateProcess rejects .cmd args that
            // contain `"`, and orchestrator prompts often include quotes/newlines.
            let prompt_path = std::env::temp_dir().join(format!("aop-pi-prompt-{}.txt", Uuid::new_v4()));
            std::fs::write(&prompt_path, prompt)?;
            cleanup.0.push(prompt_path.clone());
            a.extend([
                "--".into(),
                format!("@{}", prompt_path.display()),
            ]);
            a
        }
        CliRunnerKind::Deepseek => {
            // `sdk-minimal` is a stdio JSON-RPC server (0 positional args).
            // Oneshot tasks use the shipped `headless` profile.
            let profile = spec
                .env
                .get("DSH_CLI_PROFILE")
                .cloned()
                .filter(|s| !s.trim().is_empty())
                .unwrap_or_else(|| "headless".into());
            let mut a = vec![bin, "--profile".into(), profile.clone()];
            if !profile.eq_ignore_ascii_case("sdk-minimal") {
                a.push(composed);
            }
            a.extend(spec.args.iter().cloned());
            a
        }
    };
    Ok((argv, cleanup))
}

fn extract_cli_text(runner: CliRunnerKind, stdout: &str) -> String {
    let text = stdout.trim();
    if text.is_empty() {
        return String::new();
    }
    match runner {
        CliRunnerKind::OpenClaw | CliRunnerKind::Pi | CliRunnerKind::Deepseek => {
            // Prefer last JSON object on stdout.
            if let Some(obj) = last_json_object(text) {
                if let Some(s) = obj.get("final").and_then(|v| v.as_str()) {
                    return s.to_string();
                }
                if let Some(s) = obj.get("text").and_then(|v| v.as_str()) {
                    return s.to_string();
                }
                if let Some(s) = obj
                    .pointer("/result/text")
                    .and_then(|v| v.as_str())
                {
                    return s.to_string();
                }
                if let Some(arr) = obj.get("payloads").and_then(|v| v.as_array()) {
                    let joined: Vec<&str> = arr
                        .iter()
                        .filter_map(|p| p.get("text").and_then(|t| t.as_str()))
                        .collect();
                    if !joined.is_empty() {
                        return joined.join("\n");
                    }
                }
            }
            text.to_string()
        }
        CliRunnerKind::Claude | CliRunnerKind::Hermes => text.to_string(),
    }
}

fn last_json_object(text: &str) -> Option<Value> {
    // Try whole text, then last {...} slice.
    if let Ok(v) = serde_json::from_str::<Value>(text) {
        return Some(v);
    }
    let start = text.rfind('{')?;
    serde_json::from_str(&text[start..]).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_429_from_claude_api_error() {
        let out = "API Error: Request rejected (429) — Your account has reached the set usage limit";
        assert_eq!(capture_http_status(out), Some(429));
        let msg = detect_cli_failure_message(out, "", Some(429)).unwrap();
        assert!(msg.contains("429"));
    }

    #[test]
    fn non_error_numbers_ignored() {
        assert_eq!(capture_http_status("completed in 200 ms, tokens 1500"), None);
    }

    #[test]
    fn extract_text_requires_kind_text() {
        let kind = json!({"parts": [{"kind": "text", "text": "hello kind"}]});
        assert_eq!(extract_text(&kind), "hello kind");
        let typ_only = json!({"parts": [{"type": "text", "text": "legacy"}]});
        assert_eq!(extract_text(&typ_only), "");
        let bare = json!({"parts": [{"text": "no kind"}]});
        assert_eq!(extract_text(&bare), "");
    }

    #[test]
    fn prefers_cmd_shim_over_extensionless_npm_script() {
        let dir = std::env::temp_dir().join(format!("aop-shim-{}", Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let bare = dir.join("pi");
        let cmd = dir.join("pi.cmd");
        std::fs::write(&bare, "#!/bin/sh\necho hi\n").unwrap();
        std::fs::write(&cmd, "@echo off\r\n").unwrap();
        let chosen = prefer_spawnable(bare);
        let _ = std::fs::remove_dir_all(&dir);
        #[cfg(windows)]
        assert!(chosen.ends_with("pi.cmd"), "got {}", chosen.display());
        #[cfg(not(windows))]
        assert!(chosen.ends_with("pi"));
    }
}
