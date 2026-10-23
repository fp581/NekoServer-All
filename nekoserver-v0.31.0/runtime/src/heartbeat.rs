//! Endpoint secrets stay in a private curl config, never arguments/public status.
use crate::{
    common::{self, Error, Result},
    Args,
};
use serde_json::{json, Value};
use std::{fs, os::unix::fs::MetadataExt, path::Path};
fn endpoint(value: &str) -> bool {
    regex::Regex::new(
        r"^https://[A-Za-z0-9][A-Za-z0-9.-]*(?::([0-9]{1,5}))?/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*$",
    )
    .unwrap()
    .captures(value)
    .is_some_and(|c| {
        value.len() <= 2048
            && !["start", "fail"].contains(&value.rsplit('/').next().unwrap_or(""))
            && c.get(1)
                .is_none_or(|m| m.as_str().parse::<u16>().is_ok_and(|p| p > 0))
    })
}
fn send(cfg: &Value, runtime: &Path) -> Result<()> {
    let url = cfg["endpoint"].as_str().unwrap_or("");
    if !endpoint(url) {
        return Err(Error::new("Invalid heartbeat endpoint"));
    }
    let config = format!("url = {}\nrequest = \"GET\"\n", serde_json::to_string(url)?);
    common::deliver(
        runtime,
        "request.curl",
        &config,
        "Heartbeat delivery failed",
    )
}
pub fn run(a: &Args) -> Result<()> {
    common::require_root()?;
    let private = common::safe(&a.path("config")?)?;
    let m = fs::metadata(&private)?;
    if m.uid() != 0 || m.mode() & 0o077 != 0 {
        return Err(Error::new("Private heartbeat configuration required"));
    }
    let cfg: Value = serde_json::from_slice(&common::bytes(&private, 64000)?)?;
    let runtime = Path::new("/run/nekoserver-heartbeat");
    common::directory(runtime, 0o755)?;
    let _lock = common::lock(&runtime.join("worker.lock"))?;
    let previous = common::json(&runtime.join("status.json"), 64000);
    let result = send(&cfg, runtime);
    let now = common::now();
    common::atomic(
        &runtime.join("status.json"),
        &serde_json::to_vec(
            &json!({"format":"neko-heartbeat-v1","status":if result.is_ok(){"pass"}else{"fail"},"last_attempt":now,"last_success":if result.is_ok(){json!(now)}else{common::num(&previous["last_success"],0.0,now)},"provider_notifications_verified":false}),
        )?,
        0o644,
    )?;
    result
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn urls_are_narrow() {
        assert!(endpoint("https://hc-ping.com/abc-123"));
        assert!(endpoint("https://monitor.example/key/name"));
        for s in [
            "http://host/x",
            "https://host/x?token=y",
            "https://user@host/x",
            "https://host:0/x",
            "https://host/x\nheader=z",
            "https://host/x/fail?create=1",
        ] {
            assert!(!endpoint(s));
        }
    }
}
