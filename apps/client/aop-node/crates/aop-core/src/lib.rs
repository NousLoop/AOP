//! Core types for AOP Node: config, plugin manifests, whitelist.

mod config;
mod expand;
mod manifest;
mod whitelist;

pub use config::{AgentSpec, Config, WhitelistConfig};
pub use expand::expand_path;
pub use manifest::{discover_plugins, PluginKind, PluginManifest};
pub use whitelist::{ExecRequest, Whitelist, WhitelistError};

pub const DEFAULT_MGMT_LISTEN: &str = "127.0.0.1:7920";
/// Shared loopback port for all `kind=cli` A2A agents (path-routed).
pub const DEFAULT_CLI_A2A_LISTEN: &str = "127.0.0.1:8020";
pub const CLIENT_VERSION: &str = env!("CARGO_PKG_VERSION");
