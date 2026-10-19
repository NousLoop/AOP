use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

use crate::expand::{expand_path, resolve_relative};
use crate::{DEFAULT_CLI_A2A_LISTEN, DEFAULT_MGMT_LISTEN};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    pub gateway_url: String,
    #[serde(default)]
    pub api_key: String,
    #[serde(default = "default_node_id")]
    pub node_id: String,
    /// Orchestrator base for heartbeat (falls back to gateway_url if empty).
    #[serde(default)]
    pub orchestrator_url: String,
    #[serde(default = "default_plugins_dir")]
    pub plugins_dir: String,
    #[serde(default = "default_versions_dir")]
    pub versions_dir: String,
    #[serde(default = "default_log_dir")]
    pub log_dir: String,
    #[serde(default = "default_heartbeat")]
    pub heartbeat_interval_secs: u64,
    #[serde(default = "default_mgmt")]
    pub mgmt_listen: String,
    /// Shared A2A listen for all `kind=cli` plugins (`/agents/<id>/…`).
    #[serde(default = "default_cli_a2a")]
    pub cli_a2a_listen: String,
    /// Empty disables self-update checks.
    #[serde(default)]
    pub update_manifest_url: String,
    /// Public endpoint registered to Gateway for this node (mgmt or proxy).
    #[serde(default)]
    pub register_endpoint: String,
    #[serde(default)]
    pub agents: Vec<AgentSpec>,
    #[serde(default)]
    pub whitelist: WhitelistConfig,
    /// Auto-start configured agents + discovered plugins on boot.
    #[serde(default = "default_true")]
    pub autostart: bool,
}

fn default_node_id() -> String {
    "node-local-1".into()
}
fn default_plugins_dir() -> String {
    "./plugins".into()
}
fn default_versions_dir() -> String {
    "~/.aop/node/versions".into()
}
fn default_log_dir() -> String {
    "~/.aop/node/logs".into()
}
fn default_heartbeat() -> u64 {
    30
}
fn default_mgmt() -> String {
    DEFAULT_MGMT_LISTEN.into()
}
fn default_cli_a2a() -> String {
    DEFAULT_CLI_A2A_LISTEN.into()
}
fn default_true() -> bool {
    true
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentSpec {
    pub id: String,
    #[serde(default = "default_local_version")]
    pub version: String,
    pub command: String,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub env: HashMap<String, String>,
    #[serde(default)]
    pub workdir: Option<String>,
    #[serde(default)]
    pub health_url: Option<String>,
    /// If false, listed but not started on boot.
    #[serde(default = "default_true")]
    pub enabled: bool,
}

fn default_local_version() -> String {
    "local".into()
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct WhitelistConfig {
    #[serde(default)]
    pub allowed_binaries: Vec<String>,
    #[serde(default)]
    pub allowed_plugin_ids: Vec<String>,
    #[serde(default)]
    pub allowed_path_prefixes: Vec<String>,
}

impl Config {
    pub fn load(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref();
        let text =
            fs::read_to_string(path).with_context(|| format!("read config {}", path.display()))?;
        let mut cfg: Self = toml::from_str(&text)
            .with_context(|| format!("parse config {}", path.display()))?;
        let base = path
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .to_path_buf();
        cfg.normalize(&base);
        Ok(cfg)
    }

    pub fn example() -> Self {
        let mut cfg = Self {
            gateway_url: "http://127.0.0.1:8080".into(),
            api_key: String::new(),
            node_id: default_node_id(),
            orchestrator_url: "http://127.0.0.1:8090".into(),
            plugins_dir: default_plugins_dir(),
            versions_dir: default_versions_dir(),
            log_dir: default_log_dir(),
            heartbeat_interval_secs: default_heartbeat(),
            mgmt_listen: default_mgmt(),
            cli_a2a_listen: default_cli_a2a(),
            update_manifest_url: String::new(),
            register_endpoint: "http://127.0.0.1:7920/".into(),
            agents: vec![],
            whitelist: WhitelistConfig {
                allowed_binaries: vec![
                    "python".into(),
                    "python3".into(),
                    "openclaw".into(),
                    "hermes".into(),
                    "pi".into(),
                    "claude".into(),
                    "dsh".into(),
                    "echo".into(),
                    "cmd".into(),
                ],
                allowed_plugin_ids: vec![
                    "claude-code".into(),
                    "openclaw".into(),
                    "hermes".into(),
                    "echo".into(),
                ],
                allowed_path_prefixes: vec![],
            },
            autostart: true,
        };
        cfg.normalize(Path::new("."));
        cfg
    }

    fn normalize(&mut self, base: &Path) {
        self.gateway_url = self.gateway_url.trim_end_matches('/').to_string();
        if self.orchestrator_url.is_empty() {
            self.orchestrator_url = self.gateway_url.clone();
        } else {
            self.orchestrator_url = self.orchestrator_url.trim_end_matches('/').to_string();
        }
        self.plugins_dir = resolve_relative(base, &self.plugins_dir)
            .to_string_lossy()
            .into_owned();
        self.versions_dir = expand_path(&self.versions_dir)
            .to_string_lossy()
            .into_owned();
        self.log_dir = expand_path(&self.log_dir).to_string_lossy().into_owned();
        if self.register_endpoint.is_empty() {
            self.register_endpoint = format!("http://{}/", self.mgmt_listen);
        }
        if self.mgmt_listen.is_empty() {
            self.mgmt_listen = DEFAULT_MGMT_LISTEN.into();
        }
        if self.cli_a2a_listen.is_empty() {
            self.cli_a2a_listen = DEFAULT_CLI_A2A_LISTEN.into();
        }
    }

    pub fn plugins_path(&self) -> PathBuf {
        PathBuf::from(&self.plugins_dir)
    }

    /// Base URL for shared CLI A2A (no trailing slash), e.g. `http://127.0.0.1:8020`.
    pub fn cli_a2a_base_url(&self) -> String {
        format!("http://{}", self.cli_a2a_listen.trim().trim_end_matches('/'))
    }

    pub fn cli_agent_endpoint(&self, plugin_id: &str) -> String {
        format!("{}/agents/{}/", self.cli_a2a_base_url(), plugin_id)
    }

    pub fn cli_agent_health_url(&self, plugin_id: &str) -> String {
        format!("{}health", self.cli_agent_endpoint(plugin_id))
    }

    pub fn log_path(&self) -> PathBuf {
        PathBuf::from(&self.log_dir)
    }

    pub fn versions_path(&self) -> PathBuf {
        PathBuf::from(&self.versions_dir)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use tempfile::tempdir;

    #[test]
    fn load_minimal_toml() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("aop.toml");
        let mut f = fs::File::create(&path).unwrap();
        write!(
            f,
            r#"
gateway_url = "http://127.0.0.1:8080"
api_key = "test"
node_id = "n1"
plugins_dir = "./plugins"

[whitelist]
allowed_binaries = ["python"]
allowed_plugin_ids = ["echo"]
"#
        )
        .unwrap();
        let cfg = Config::load(&path).unwrap();
        assert_eq!(cfg.node_id, "n1");
        assert!(cfg.plugins_dir.contains("plugins"));
        assert_eq!(cfg.heartbeat_interval_secs, 30);
        assert_eq!(cfg.mgmt_listen, DEFAULT_MGMT_LISTEN);
    }
}
