//! Supervisor: OS connect + heartbeat + child lifecycle + whitelist exec + mgmt HTTP.

mod api;
mod cli_a2a;
mod lifecycle;
mod os_client;

pub use api::serve_mgmt;
pub use cli_a2a::{CliAgentHub, CliAgentSpec};
pub use lifecycle::Supervisor;
pub use os_client::OsClient;
