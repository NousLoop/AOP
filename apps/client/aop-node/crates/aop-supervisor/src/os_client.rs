use anyhow::{Context, Result};
use reqwest::header::{HeaderMap, HeaderValue, AUTHORIZATION};
use serde_json::{json, Value};
use tracing::{info, warn};

use aop_core::Config;

/// Thin HTTP client for A2A OS Gateway / Orchestrator.
#[derive(Clone)]
pub struct OsClient {
    http: reqwest::Client,
    gateway_url: String,
    orchestrator_url: String,
    #[allow(dead_code)]
    api_key: String,
    node_id: String,
    #[allow(dead_code)]
    register_endpoint: String,
    /// Agent id returned by register (or node_id fallback).
    pub agent_id: String,
}

impl OsClient {
    pub fn new(cfg: &Config) -> Result<Self> {
        let mut headers = HeaderMap::new();
        if !cfg.api_key.is_empty() {
            let v = format!("Bearer {}", cfg.api_key);
            headers.insert(AUTHORIZATION, HeaderValue::from_str(&v)?);
            headers.insert("X-API-Key", HeaderValue::from_str(&cfg.api_key)?);
        }
        let http = reqwest::Client::builder()
            .default_headers(headers)
            .timeout(std::time::Duration::from_secs(30))
            .build()?;
        Ok(Self {
            http,
            gateway_url: cfg.gateway_url.clone(),
            orchestrator_url: cfg.orchestrator_url.clone(),
            api_key: cfg.api_key.clone(),
            node_id: cfg.node_id.clone(),
            register_endpoint: cfg.register_endpoint.clone(),
            agent_id: cfg.node_id.clone(),
        })
    }

    /// Connect the supervisor. Nodes are not A2A agents — do not register :7920.
    pub async fn register(&mut self) -> Result<Value> {
        let v = self.heartbeat_node(None).await?;
        info!(node_id = %self.node_id, "edge node heartbeat ok (not registered as agent)");
        Ok(v)
    }

    /// Register any A2A endpoint (harness agent) with Gateway.
    pub async fn register_endpoint_url(&self, endpoint: &str) -> Result<Value> {
        let url = format!("{}/v1/agents/register", self.gateway_url);
        let body = json!({ "endpoint": endpoint });
        let resp = self
            .http
            .post(&url)
            .json(&body)
            .send()
            .await
            .with_context(|| format!("POST {url}"))?;
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        if !status.is_success() {
            warn!(%status, %text, endpoint, "register endpoint failed");
            anyhow::bail!("register failed: {status} {text}");
        }
        info!(endpoint, "registered endpoint with gateway");
        Ok(serde_json::from_str(&text).unwrap_or(json!({"raw": text})))
    }

    pub async fn wait_health(&self, health_url: &str, timeout_s: u64) -> Result<()> {
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(timeout_s);
        let mut last = anyhow::anyhow!("health not checked");
        while std::time::Instant::now() < deadline {
            match self.http.get(health_url).send().await {
                Ok(r) if r.status().is_success() => return Ok(()),
                Ok(r) => last = anyhow::anyhow!("health HTTP {}", r.status()),
                Err(e) => last = e.into(),
            }
            tokio::time::sleep(std::time::Duration::from_millis(400)).await;
        }
        Err(last)
    }

    pub async fn register_with_retry(&mut self, attempts: u32) -> Result<Value> {
        let mut last = anyhow::anyhow!("no attempts");
        for i in 1..=attempts {
            match self.register().await {
                Ok(v) => return Ok(v),
                Err(e) => {
                    warn!(attempt = i, error = %e, "register retry");
                    last = e;
                    tokio::time::sleep(std::time::Duration::from_secs(2 * i as u64)).await;
                }
            }
        }
        Err(last)
    }

    pub async fn heartbeat(&self, extra: Option<Value>) -> Result<Value> {
        self.heartbeat_node(extra).await
    }

    /// Supervisor pulse — not an Agent Card / registry entry.
    pub async fn heartbeat_node(&self, extra: Option<Value>) -> Result<Value> {
        let url = format!("{}/v1/nodes/{}/heartbeat", self.gateway_url, self.node_id);
        let mut body = json!({
            "status": "online",
            "node_id": self.node_id,
            "client_version": aop_core::CLIENT_VERSION,
        });
        if let Some(Value::Object(map)) = extra {
            if let Some(obj) = body.as_object_mut() {
                for (k, v) in map {
                    obj.insert(k, v);
                }
            }
        }
        let resp = self.http.post(&url).json(&body).send().await;
        match resp {
            Ok(r) => {
                let status = r.status();
                let text = r.text().await.unwrap_or_default();
                if !status.is_success() {
                    return self.heartbeat_node_orchestrator(body).await;
                }
                Ok(serde_json::from_str(&text).unwrap_or(json!({"ok": true})))
            }
            Err(_) => self.heartbeat_node_orchestrator(body).await,
        }
    }

    async fn heartbeat_node_orchestrator(&self, body: Value) -> Result<Value> {
        let url = format!(
            "{}/v1/nodes/{}/heartbeat",
            self.orchestrator_url, self.node_id
        );
        let resp = self
            .http
            .post(&url)
            .json(&body)
            .send()
            .await
            .with_context(|| format!("POST {url}"))?;
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        if !status.is_success() {
            warn!(%status, %text, node_id = %self.node_id, "node heartbeat failed");
            anyhow::bail!("node heartbeat failed: {status} {text}");
        }
        Ok(serde_json::from_str(&text).unwrap_or(json!({"ok": true})))
    }

    /// Lifecycle heartbeat for an arbitrary agent id (harness child).
    pub async fn heartbeat_agent(&self, agent_id: &str, extra: Option<Value>) -> Result<Value> {
        // Lifecycle heartbeats go through Gateway → Orchestrator agent-runtime.
        // (Gateway owns /v1/agents/* registry; do not POST heartbeat there.)
        let url = format!(
            "{}/v1/agent-runtime/{}/heartbeat",
            self.gateway_url, agent_id
        );
        let mut body = json!({
            "status": "online",
            "node_id": self.node_id,
            "client_version": aop_core::CLIENT_VERSION,
        });
        if let Some(Value::Object(map)) = extra {
            if let Some(obj) = body.as_object_mut() {
                for (k, v) in map {
                    obj.insert(k, v);
                }
            }
        }
        let resp = self.http.post(&url).json(&body).send().await;
        match resp {
            Ok(r) => {
                let status = r.status();
                let text = r.text().await.unwrap_or_default();
                if !status.is_success() {
                    return self.heartbeat_orchestrator(agent_id, body).await;
                }
                Ok(serde_json::from_str(&text).unwrap_or(json!({"ok": true})))
            }
            Err(_) => self.heartbeat_orchestrator(agent_id, body).await,
        }
    }

    async fn heartbeat_orchestrator(&self, agent_id: &str, body: Value) -> Result<Value> {
        let url = format!(
            "{}/v1/agent-runtime/{}/heartbeat",
            self.orchestrator_url, agent_id
        );
        let resp = self
            .http
            .post(&url)
            .json(&body)
            .send()
            .await
            .with_context(|| format!("POST {url}"))?;
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        if !status.is_success() {
            warn!(%status, %text, agent_id, "heartbeat failed");
            anyhow::bail!("heartbeat failed: {status} {text}");
        }
        Ok(serde_json::from_str(&text).unwrap_or(json!({"ok": true})))
    }
}
