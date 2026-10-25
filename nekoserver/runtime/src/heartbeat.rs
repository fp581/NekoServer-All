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
    common::atomic(
        &runtime.join("status.json"),
        &serde_json::to_vec(&status(result.is_ok(), common::now(), &previous))?,
        0o644,
    )?;
    result
}
/// The status after one check-in. A failed one keeps the last success as it was saved,
/// also one from a clock that was ahead (the dashboard shows that one as now).
fn status(sent: bool, now: f64, previous: &Value) -> Value {
    json!({"format":"neko-heartbeat-v1","status":if sent{"pass"}else{"fail"},"last_attempt":now,"last_success":if sent{json!(now)}else{common::kept_time(&previous["last_success"])},"provider_notifications_verified":false})
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn a_clock_put_back_never_erases_the_last_check_in() {
        // Saved while the clock was a day ahead; now the clock is right and a check-in fails.
        let now = 1_800_000_000.0;
        let previous = json!({"last_success": now + 86400.0});
        assert_eq!(status(false, now, &previous)["last_success"], now + 86400.0);
        assert_eq!(status(true, now, &previous)["last_success"], now);
        assert!(status(false, now, &json!({"last_success": "x"}))["last_success"].is_null());
        // Shown, it reads as now rather than in the future or as never.
        assert_eq!(
            common::shown_time(&previous["last_success"], now),
            json!(now)
        );
    }
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
