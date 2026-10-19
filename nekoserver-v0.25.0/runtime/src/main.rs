mod app_snapshot;
mod backup;
mod collect;
mod common;
mod heartbeat;
mod host_snapshot;
mod ledger;
mod maintenance;
mod media;
mod music_backup;
mod notify;
mod os_policy;
mod serve;
mod startup;
use common::{Error, Result};
use std::{collections::BTreeMap, env, path::PathBuf};
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
#[derive(Default)]
pub struct Args(pub BTreeMap<String, String>);
impl Args {
    pub fn get(&self, key: &str) -> Result<&str> {
        self.0
            .get(key)
            .map(String::as_str)
            .ok_or_else(|| Error::new("Missing required option"))
    }
    pub fn path(&self, key: &str) -> Result<PathBuf> {
        Ok(PathBuf::from(self.get(key)?))
    }
    pub fn value(&self, key: &str, default: &str) -> String {
        self.0
            .get(key)
            .cloned()
            .unwrap_or_else(|| default.to_string())
    }
    pub fn flag(&self, key: &str) -> bool {
        self.0.contains_key(key)
    }
    pub fn number(&self, key: &str, default: u64) -> Result<u64> {
        match self.0.get(key) {
            Some(v) => v.parse().map_err(|_| Error::new("Invalid numeric option")),
            None => Ok(default),
        }
    }
}
fn main() {
    if let Err(e) = entry() {
        let stage = common::current_stage();
        eprintln!(
            "NekoServer runtime failed{}: {}. No success recorded.",
            if stage.is_empty() {
                String::new()
            } else {
                format!(" while {stage}")
            },
            common::sanitize(&e.to_string())
        );
        std::process::exit(1)
    }
}
fn entry() -> Result<()> {
    let mut argv = env::args().skip(1);
    let mode = argv.next().unwrap_or_default();
    if mode == "--version" {
        println!(
            "nekoserver-runtime {VERSION}{}",
            if cfg!(feature = "test-fixtures") {
                " test-fixtures"
            } else {
                ""
            }
        );
        return Ok(());
    }
    if mode == "--help" || mode.is_empty() {
        println!("nekoserver-runtime {VERSION}\nCommands: serve, collect, archive, copy, preservation, weekly, database, media-health, music-backup, app-snapshot, app-resume, notify, hdd-policy, host-snapshot, host-resume, heartbeat, storage-guard, gateway-guard, maintenance, maintenance-resume\nUse COMMAND --help for options.");
        return Ok(());
    }
    let allowed = match mode.as_str() {
        "storage-guard" | "gateway-guard" | "heartbeat" | "hdd-policy" | "host-snapshot"
        | "host-resume" => vec!["config"],
        "music-backup" | "app-snapshot" | "app-resume" | "maintenance" | "maintenance-resume" => {
            vec!["config"]
        }
        "notify" => vec!["config", "snapshot", "state", "runtime", "test"],
        "media-health" => vec!["config", "output"],
        "serve" => vec!["assets", "status", "port", "public-host"],
        "collect" => vec!["config", "output", "state"],
        "archive" => vec!["source", "archive", "min-age", "apply"],
        "copy" => vec!["source", "dest", "uuid", "apply"],
        "preservation" => vec![
            "config",
            "data",
            "media",
            "min-age",
            "db-backup",
            "archive-dir",
            "db-keep",
        ],
        "weekly" => vec![
            "config",
            "uuid",
            "fstype",
            "source",
            "dest",
            "media-root",
            "data",
            "cloud",
        ],
        "database" => vec!["compose", "env", "dest", "apply"],
        _ => return Err(Error::new("Unknown command")),
    };
    let mut a = Args::default();
    while let Some(arg) = argv.next() {
        if arg == "--help" {
            println!(
                "{mode}: {}",
                allowed
                    .iter()
                    .map(|k| format!("--{k}"))
                    .collect::<Vec<_>>()
                    .join(" ")
            );
            return Ok(());
        }
        let key = arg
            .strip_prefix("--")
            .ok_or_else(|| Error::new("Invalid option"))?;
        if !allowed.contains(&key) || a.0.contains_key(key) {
            return Err(Error::new("Unknown or repeated option"));
        }
        let value = if key == "apply" || key == "db-backup" || key == "test" {
            "true".into()
        } else {
            let v = argv
                .next()
                .ok_or_else(|| Error::new("Missing option value"))?;
            if v.starts_with("--") {
                return Err(Error::new("Missing option value"));
            }
            v
        };
        a.0.insert(key.into(), value);
    }
    match mode.as_str() {
        "maintenance" => maintenance::run(&a, false),
        "maintenance-resume" => maintenance::run(&a, true),
        "hdd-policy" => os_policy::run(&a),
        "music-backup" => music_backup::run(&a),
        "notify" => notify::run(&a),
        "storage-guard" => startup::run(&a, false),
        "gateway-guard" => startup::run(&a, true),
        "heartbeat" => heartbeat::run(&a),
        "host-snapshot" => host_snapshot::run(&a),
        "host-resume" => host_snapshot::resume_command(&a),
        "app-snapshot" => app_snapshot::run(&a),
        "app-resume" => app_snapshot::resume_command(&a),
        "media-health" => media::run(&a),
        "serve" => serve::run(&a),
        "collect" => collect::run(&a),
        "archive" => {
            backup::archive(
                &a.path("source")?,
                &a.path("archive")?,
                a.flag("apply"),
                a.number("min-age", 600)?,
                // Manual runs are rare: compare every existing copy by checksum.
                backup::Check::Full,
            )?;
            Ok(())
        }
        "copy" => {
            if a.flag("apply") {
                backup::validate_external(&a.path("dest")?, a.get("uuid")?)?;
            }
            backup::copy_tree(
                &a.path("source")?,
                &a.path("dest")?,
                a.flag("apply"),
                backup::Check::Full,
            )?;
            Ok(())
        }
        "preservation" => backup::preservation(&a),
        "weekly" => backup::weekly(&a),
        "database" => {
            backup::database(
                &a.path("compose")?,
                &a.path("env")?,
                &a.path("dest")?,
                a.flag("apply"),
            )?;
            Ok(())
        }
        _ => unreachable!(),
    }
}
