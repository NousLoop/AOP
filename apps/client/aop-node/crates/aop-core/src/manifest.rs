use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

use crate::expand::expand_path;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PluginKind {
    Agent,
    Plugin,
    /// Manufacturer CLI: aopd hosts A2A; short-lived spawn of `command` per task.
    Cli,
}

impl Default for PluginKind {
    fn default() -> Self {
        Self::Plugin
    }
}

impl PluginKind {
    pub fn is_cli(self) -> bool {
        matches!(self, Self::Cli)
    }
}

/// Tabby-style plugin/agent package manifest (`plugin.toml`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PluginManifest {
    pub id: String,
    pub version: String,
    #[serde(default)]
    pub kind: PluginKind,
    pub command: String,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub env: HashMap<String, String>,
    #[serde(default)]
    pub workdir: Option<String>,
    #[serde(default)]
    pub health_url: Option<String>,
    #[serde(default)]
    pub description: String,
}

impl PluginManifest {
    pub fn load(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref();
        let text = fs::read_to_string(path)
            .with_context(|| format!("read plugin manifest {}", path.display()))?;
        let m: Self = toml::from_str(&text)
            .with_context(|| format!("parse plugin manifest {}", path.display()))?;
        Ok(m)
    }

    pub fn workdir_path(&self, plugin_root: &Path) -> PathBuf {
        match &self.workdir {
            Some(w) => {
                let p = expand_path(w);
                if p.is_absolute() {
                    p
                } else {
                    plugin_root.join(p)
                }
            }
            None => plugin_root.to_path_buf(),
        }
    }
}

/// Discover `plugins/<id>/plugin.toml` under `plugins_dir`.
pub fn discover_plugins(plugins_dir: &Path) -> Result<Vec<(PathBuf, PluginManifest)>> {
    let mut out = Vec::new();
    if !plugins_dir.is_dir() {
        return Ok(out);
    }
    for entry in fs::read_dir(plugins_dir)? {
        let entry = entry?;
        if !entry.file_type()?.is_dir() {
            continue;
        }
        let manifest_path = entry.path().join("plugin.toml");
        if !manifest_path.is_file() {
            continue;
        }
        let m = PluginManifest::load(&manifest_path)?;
        out.push((entry.path(), m));
    }
    out.sort_by(|a, b| a.1.id.cmp(&b.1.id));
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use tempfile::tempdir;

    #[test]
    fn load_and_discover() {
        let dir = tempdir().unwrap();
        let plug = dir.path().join("echo");
        fs::create_dir_all(&plug).unwrap();
        let mut f = fs::File::create(plug.join("plugin.toml")).unwrap();
        write!(
            f,
            r#"
id = "echo"
version = "0.1.0"
kind = "plugin"
command = "echo"
args = ["hello"]
"#
        )
        .unwrap();
        let found = discover_plugins(dir.path()).unwrap();
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].1.id, "echo");
        assert_eq!(found[0].1.kind, PluginKind::Plugin);
    }

    #[test]
    fn parse_cli_kind() {
        let m: PluginManifest = toml::from_str(
            r#"
id = "claude-code"
version = "0.1.0"
kind = "cli"
command = "claude"
health_url = "http://127.0.0.1:8021/health"
"#,
        )
        .unwrap();
        assert!(m.kind.is_cli());
        assert_eq!(m.command, "claude");
    }
}
