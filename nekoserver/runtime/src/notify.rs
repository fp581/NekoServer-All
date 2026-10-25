//! ntfy delivery from sanitized local monitoring, with durable deduplication.
use crate::{
    common::{self, Error, Result},
    Args,
};
use regex::Regex;
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::Path,
};
fn label(id: &str) -> Option<String> {
    match id {
        "monitoring-stale" => return Some("Monitoring data unavailable".into()),
        "memory-pressure" => return Some("Available RAM is low".into()),
        "temperature" => return Some("CPU temperature is high".into()),
        "media-stale" => return Some("Media connection checks are stale".into()),
        "media-failure" => return Some("Media connection checks failed".into()),
        "backup-key" => return Some("Monthly backups keep the app passwords unencrypted".into()),
        "tailscale:login" => return Some("Phone access is signed out of Tailscale".into()),
        "tailscale:expiry" => {
            return Some("Phone access stops soon: Tailscale sign-in runs out".into())
        }
        "heartbeat:delivery" => {
            return Some("Outage alerts: the outside monitor is not reached".into())
        }
        "notifications:delivery" => {
            return Some("Phone alerts could not be delivered for a while".into())
        }
        _ => {}
    }
    let (kind, key) = id.split_once(':')?;
    if kind == "startup" {
        // startup:<unit> (won't start after a restart) or startup:<unit>:failed.
        let (unit, failed) = match key.strip_suffix(":failed") {
            Some(unit) => (unit, true),
            None => (key, false),
        };
        let name = crate::collect::STARTUP.iter().find(|u| u.0 == unit)?.1;
        return Some(format!(
            "{name}: {}",
            if failed {
                "stopped with an error"
            } else {
                "won't start after a restart"
            }
        ));
    }
    let name = match kind {
        "mount" | "space" | "smart" => match key {
            "data_ssd" => "Application SSD",
            "media_hdd" => "Media HDD",
            "os" => "OS disk",
            _ => return None,
        },
        "backup" => match key {
            "archive" => "Photo archive",
            "database" => "Immich database backup",
            "external" => "External photo backup",
            "applications" => "Media application backup",
            "host" => "Host and user backup",
            "music" => "Music library backup",
            "maintenance" => "Monthly backup and updates",
            _ => return None,
        },
        "app" => match key {
            "jellyfin" => "Jellyfin",
            "navidrome" => "Navidrome",
            "sonarr" => "Sonarr",
            "radarr" => "Radarr",
            "lidarr" => "Lidarr",
            "prowlarr" => "Prowlarr",
            "seerr" => "Seerr",
            "qbittorrent" => "qBittorrent",
            "immich" => "Immich",
            "nextcloud" => "Nextcloud",
            _ => return None,
        },
        "integrity" => crate::collect::CHECKS
            .iter()
            .find(|(id, _)| *id == key)
            .map(|(_, name)| *name)?,
        "major-update" => match key {
            "immich" => "Immich",
            "nextcloud" => "Nextcloud",
            _ => return None,
        },
        _ => return None,
    };
    Some(format!(
        "{name}: {}",
        match kind {
            "mount" => "drive unavailable",
            "space" => "low disk space",
            "smart" => "disk health warning",
            "backup" => "backup needs attention",
            "major-update" => "new major version available",
            "integrity" => "damaged backup copies found",
            _ => "app needs attention",
        }
    ))
}
fn endpoint(s: &str) -> bool {
    let re =
        Regex::new(r"^https://[A-Za-z0-9][A-Za-z0-9.-]*(?::([0-9]{1,5}))?/[A-Za-z0-9_-]{1,128}$")
            .unwrap();
    re.captures(s).is_some_and(|c| {
        c.get(1)
            .is_none_or(|v| v.as_str().parse::<u16>().is_ok_and(|p| p > 0))
    })
}
fn settings(cfg: &Value, key: &str, default: u64, low: u64, high: u64) -> Result<u64> {
    let n = if cfg.get(key).is_none() {
        default
    } else {
        cfg[key]
            .as_u64()
            .ok_or_else(|| Error::new("Invalid notification setting"))?
    };
    if !(low..=high).contains(&n) {
        return Err(Error::new("Notification setting out of range"));
    }
    Ok(n)
}
/// How old the collector's snapshot may be: two collection intervals plus 30 s, the
/// page's and acceptance's rule (a 5-minute interval is allowed, so a fixed 3 minutes
/// would call every slower reading "monitoring unavailable" and ignore its alerts).
/// Never less than the 3 minutes used before.
fn max_age(s: &Value) -> f64 {
    let interval = s["collect_interval"]
        .as_u64()
        .filter(|v| (30..=300).contains(v))
        .unwrap_or(60) as f64;
    2.0 * interval + 30.0
}
fn observations(s: &Value, now: f64) -> (BTreeMap<String, String>, BTreeSet<String>, bool) {
    let oldest = now - max_age(s).max(180.0);
    let fresh = s["format"] == "neko-dashboard-v1"
        && s["generated_at"]
            .as_f64()
            .is_some_and(|t| t.is_finite() && t >= oldest && t <= now + 30.0)
        && s["alerts"].is_array()
        && s["applications"].is_array()
        && s["storage"].is_array()
        && s["backups"].is_array();
    let mut active = BTreeMap::new();
    let mut unknown = BTreeSet::new();
    // Everything that can alert starts as unknown and is cleared only by explicit
    // evidence below (a running, not unhealthy app; a passed job; a mounted, checked
    // drive), so "RECOVERED" is never sent for a job that is merely running again or an
    // app whose state cannot be read. The ids come from the same lists the dashboard
    // uses, so a newly monitored app or job is covered automatically.
    let apps = crate::collect::APPS
        .iter()
        .map(|(id, ..)| format!("app:{id}"));
    let jobs = crate::collect::JOBS
        .iter()
        .map(|(id, ..)| format!("backup:{id}"));
    let drives = ["data_ssd", "media_hdd", "os"]
        .iter()
        .flat_map(|d| ["mount", "space", "smart"].map(|kind| format!("{kind}:{d}")));
    let checks = crate::collect::CHECKS
        .iter()
        .map(|(id, _)| format!("integrity:{id}"));
    let services = crate::collect::STARTUP
        .iter()
        .flat_map(|(unit, _)| [format!("startup:{unit}"), format!("startup:{unit}:failed")]);
    for id in apps.chain(jobs).chain(drives).chain(checks).chain(services) {
        if label(&id).is_some() {
            unknown.insert(id);
        }
    }
    if !fresh {
        active.insert("monitoring-stale".into(), "warning".into());
        return (active, unknown, false);
    }
    for alert in s["alerts"].as_array().unwrap().iter().take(100) {
        let id = alert["id"].as_str().unwrap_or("");
        let level = alert["level"].as_str().unwrap_or("");
        if label(id).is_some() && ["critical", "warning"].contains(&level) {
            active.insert(id.into(), level.into());
        }
    }
    for row in s["applications"].as_array().unwrap() {
        // Running, healthy and answering (as the dashboard shows it): only then recovered.
        if row["state"] == "running"
            && !["unhealthy", "starting"].contains(&row["health"].as_str().unwrap_or(""))
            && row["responding"] != false
        {
            unknown.remove(&format!("app:{}", row["id"].as_str().unwrap_or("")));
        }
    }
    for row in s["backups"].as_array().unwrap() {
        if row["status"] == "pass" {
            unknown.remove(&format!("backup:{}", row["id"].as_str().unwrap_or("")));
        }
    }
    // A service is good again only when it is listed (the check ran) and is enabled at
    // boot, or no longer failed: a missing list never counts as recovered.
    for row in s["startup"].as_array().cloned().unwrap_or_default() {
        let unit = row["unit"].as_str().unwrap_or("");
        if row["enabled"] == true {
            unknown.remove(&format!("startup:{unit}"));
        }
        let running = if crate::collect::stays_running(unit) {
            row["active"] == "active"
        } else {
            row["active"].is_string() && row["active"] != "failed"
        };
        if running {
            unknown.remove(&format!("startup:{unit}:failed"));
        }
    }
    // A check is good again only when its latest results list no damaged files, and a
    // full check (every copy read) has run: a quick run alone proves nothing.
    for row in s["integrity"].as_array().cloned().unwrap_or_default() {
        if row["full_at"].as_f64().is_some_and(|t| t > 0.0)
            && ["damaged", "both_damaged", "disagree", "missing"]
                .iter()
                .all(|k| row[k].as_u64() == Some(0))
        {
            unknown.remove(&format!("integrity:{}", row["id"].as_str().unwrap_or("")));
        }
    }
    for row in s["storage"].as_array().unwrap() {
        let id = row["id"].as_str().unwrap_or("");
        if row["status"] == "mounted" {
            unknown.remove(&format!("mount:{id}"));
            if row["used_percent"].is_number() {
                unknown.remove(&format!("space:{id}"));
            }
        }
        if row["status"] == "mounted" && row["smart"]["status"] == "pass" {
            unknown.remove(&format!("smart:{id}"));
        }
    }
    if s["media"]["status"] != "current" {
        unknown.insert("media-failure".into());
    }
    if !s["metrics"]["memory_percent"].is_number() {
        unknown.insert("memory-pressure".into());
    }
    if !s["metrics"]["temperature"].is_number() {
        unknown.insert("temperature".into());
    }
    (active, unknown, true)
}
fn send(cfg: &Value, runtime: &Path, message: &str, critical: bool) -> Result<()> {
    let url = cfg["endpoint"].as_str().unwrap_or("");
    let token = if cfg.get("token").is_none() {
        ""
    } else {
        cfg["token"]
            .as_str()
            .ok_or_else(|| Error::new("Invalid token"))?
    };
    if !endpoint(url)
        || token.len() > 512
        || !token
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
    {
        return Err(Error::new("Invalid private notification destination"));
    }
    // JSON quoted strings also satisfy curl's double-quoted configuration syntax.
    let mut config=format!("url = {}\nrequest = \"POST\"\nheader = \"Title: NekoServer health\"\nheader = \"Content-Type: text/plain; charset=utf-8\"\nheader = \"Priority: {}\"\ndata-binary = {}\n",serde_json::to_string(url)?,if critical{5}else{3},serde_json::to_string(message)?);
    if !token.is_empty() {
        config.push_str(&format!(
            "header = {}\n",
            serde_json::to_string(&format!("Authorization: Bearer {token}"))?
        ));
    }
    common::deliver(
        runtime,
        "delivery.curl",
        &config,
        "Notification delivery failed",
    )
}
/// What is no longer looked after, so an alert sent about it is dropped without a
/// "RECOVERED" (nothing recovered): a backup switched off with its app, an app taken out
/// of the selection (the dashboard lists every app set up, running or not).
fn retired(s: &Value) -> BTreeSet<String> {
    let listed: BTreeSet<String> = s["applications"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|r| r["id"].as_str().map(|id| format!("app:{id}")))
        .collect();
    let mut out: BTreeSet<String> = crate::collect::APPS
        .iter()
        .map(|(id, ..)| format!("app:{id}"))
        .filter(|id| !listed.contains(id))
        .collect();
    out.extend(
        s["backups"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|r| r["status"] == "disabled")
            .filter_map(|r| r["id"].as_str().map(|id| format!("backup:{id}"))),
    );
    out
}
/// Pure transition logic; only successfully delivered events advance deduplication.
fn plan(
    s: &Value,
    previous: &Value,
    now: f64,
    debounce: u64,
    repeat: u64,
) -> (Value, Vec<(String, bool, String)>) {
    let (active, unknown, fresh) = observations(s, now);
    let mut state = if previous["format"] == "neko-notifications-v1" {
        previous.clone()
    } else {
        json!({"format":"neko-notifications-v1","entries":{},"attempts":0})
    };
    if !state["entries"].is_object() {
        state["entries"] = json!({});
    }
    let mut events = vec![];
    for (id, level) in &active {
        if !state["entries"][id].is_object() {
            state["entries"][id] = json!({"first_seen":now,"last_sent":null,"sent_level":null});
        }
        let row = &state["entries"][id];
        let first = row["first_seen"]
            .as_f64()
            .filter(|t| t.is_finite() && *t <= now)
            .unwrap_or(now);
        let sent = row["last_sent"]
            .as_f64()
            .filter(|t| t.is_finite() && *t <= now);
        let due = sent.is_none_or(|t| now - t >= repeat as f64)
            || (level == "critical" && row["sent_level"] == "warning");
        if due && (now - first >= debounce as f64 || id.starts_with("mount:")) {
            events.push((id.clone(), false, level.clone()));
        }
    }
    let keys = state["entries"]
        .as_object()
        .unwrap()
        .keys()
        .cloned()
        .collect::<Vec<_>>();
    let gone = if fresh { retired(s) } else { BTreeSet::new() };
    for id in keys {
        if label(&id).is_none() || (gone.contains(&id) && !active.contains_key(&id)) {
            state["entries"].as_object_mut().unwrap().remove(&id);
            continue;
        }
        if !active.contains_key(&id) && fresh && !unknown.contains(&id) {
            if state["entries"][&id]["last_sent"].is_number() {
                events.push((id, true, "recovered".into()));
            } else {
                state["entries"].as_object_mut().unwrap().remove(&id);
            }
        }
    }
    (state, events)
}
fn report(runtime: &Path, state: &Value, now: f64, pending: usize) -> Result<()> {
    common::atomic(
        &runtime.join("delivery.json"),
        &serde_json::to_vec(
            &json!({"format":"neko-delivery-v1","generated_at":now,"status":state["delivery_status"].as_str().unwrap_or("unknown"),"last_success":state["last_success"],"last_attempt":state["last_attempt"],"next_attempt":state["next_attempt"],"pending":pending}),
        )?,
        0o644,
    )
}
/// Is a message due now? A retry is never set more than an hour ahead, so one further
/// ahead was set while the clock was ahead: it is ignored, or alerts would wait until the
/// clock reached it again (days, after a clock put back by days).
fn delivery_due(next_attempt: &Value, now: f64) -> bool {
    next_attempt
        .as_f64()
        .is_none_or(|t| t <= now || t > now + 3600.0)
}
pub fn run(a: &Args) -> Result<()> {
    common::require_root()?;
    let config_path = a.path("config")?;
    use std::os::unix::fs::MetadataExt;
    let metadata = fs::metadata(common::safe(&config_path)?)?;
    if metadata.uid() != 0 || metadata.mode() & 0o077 != 0 {
        return Err(Error::new("Private root-owned delivery config required"));
    }
    let cfg: Value = serde_json::from_slice(&common::bytes(&config_path, 64000)?)?;
    if !endpoint(cfg["endpoint"].as_str().unwrap_or("")) {
        return Err(Error::new("Notification destination requires setup"));
    }
    let debounce = settings(&cfg, "debounce_seconds", 180, 0, 3600)?;
    let repeat = settings(&cfg, "repeat_seconds", 43200, 3600, 604800)?;
    let minimum = settings(&cfg, "minimum_interval_seconds", 300, 60, 3600)?;
    let root = common::safe(&a.path("state")?)?;
    let runtime = common::safe(&a.path("runtime")?)?;
    for p in [&root, &runtime] {
        common::directory(p, 0o700)?;
        use std::os::unix::fs::MetadataExt;
        if fs::metadata(p)?.mode() & 0o077 != 0 || fs::metadata(p)?.uid() != 0 {
            return Err(Error::new(
                "Private root-owned notification directory required",
            ));
        }
    }
    let _lock = common::lock(&root.join("worker.lock"))?;
    let path = root.join("state.json");
    let old = if path.exists() {
        serde_json::from_slice(&common::bytes(&path, 256000)?)?
    } else {
        json!({})
    };
    let destination = common::sha(format!("{}:{}", cfg["endpoint"], cfg["token"]).as_bytes());
    let old = if old["destination"] == destination {
        old
    } else {
        json!({})
    };
    if a.flag("test") {
        let now = common::now();
        let result = send(
            &cfg,
            &runtime,
            "NekoServer test: notification delivery is configured.",
            false,
        );
        let mut state = if old.is_object() { old } else { json!({}) };
        state["format"] = json!("neko-notifications-v1");
        state["destination"] = json!(destination);
        state["last_attempt"] = json!(now);
        state["delivery_status"] = json!(if result.is_ok() { "pass" } else { "fail" });
        if result.is_ok() {
            state["last_success"] = json!(now);
        }
        common::atomic_json(&path, &state)?;
        report(&runtime, &state, now, 0)?;
        return result;
    }
    let snapshot = common::json(&a.path("snapshot")?, 2_000_000);
    let now = common::now();
    let (mut state, events) = plan(&snapshot, &old, now, debounce, repeat);
    state["destination"] = json!(destination);
    let mut pending = events.len();
    let due = delivery_due(&state["next_attempt"], now);
    let mut failed = false;
    if !events.is_empty() && due {
        let mut message = String::from("NekoServer health update\n");
        for (id, recovered, level) in &events {
            message.push_str(&format!(
                "{}: {}\n",
                if *recovered {
                    "RECOVERED"
                } else {
                    level.as_str()
                },
                label(id).unwrap()
            ));
        }
        message.push_str("Review the local dashboard and service logs for details.");
        state["last_attempt"] = json!(now);
        if send(
            &cfg,
            &runtime,
            &message,
            events.iter().any(|(_, r, l)| !*r && l == "critical"),
        )
        .is_ok()
        {
            for (id, recovered, level) in &events {
                if *recovered {
                    state["entries"].as_object_mut().unwrap().remove(id);
                } else {
                    state["entries"][id]["last_sent"] = json!(now);
                    state["entries"][id]["sent_level"] = json!(level);
                }
            }
            pending = 0;
            state["attempts"] = json!(0);
            state["last_success"] = json!(now);
            state["delivery_status"] = json!("pass");
            state["next_attempt"] = json!(now + minimum as f64);
        } else {
            failed = true;
            let attempts = state["attempts"]
                .as_u64()
                .unwrap_or(0)
                .saturating_add(1)
                .min(10);
            state["attempts"] = json!(attempts);
            state["delivery_status"] = json!("fail");
            state["next_attempt"] =
                json!(now + (60u64 * 2u64.pow((attempts - 1).min(6) as u32)).min(3600) as f64);
        }
    }
    common::atomic_json(&path, &state)?;
    report(&runtime, &state, now, pending)?;
    if failed {
        return Err(Error::new("Notification delivery failed; retry scheduled"));
    }
    Ok(())
}
#[cfg(test)]
mod tests {
    #[test]
    fn a_retry_time_from_a_clock_that_was_ahead_never_holds_alerts_back() {
        let now = 1_800_000_000.0;
        assert!(delivery_due(&serde_json::json!(null), now));
        assert!(delivery_due(&serde_json::json!(now - 1.0), now));
        // A retry or the minimum interval still waits (both are at most an hour).
        assert!(!delivery_due(&serde_json::json!(now + 300.0), now));
        assert!(!delivery_due(&serde_json::json!(now + 3600.0), now));
        // Set a day ahead, before the clock was put back: due now.
        assert!(delivery_due(&serde_json::json!(now + 86400.0), now));
    }
    #[test]
    fn every_monitored_app_and_job_can_notify() {
        // Alerts without a label are dropped, so every id the dashboard can raise needs one.
        for (job, ..) in crate::collect::JOBS {
            assert!(label(&format!("backup:{job}")).is_some(), "backup:{job}");
        }
        for (app, ..) in crate::collect::APPS {
            assert!(label(&format!("app:{app}")).is_some(), "app:{app}");
        }
        for app in ["immich", "nextcloud"] {
            assert!(
                label(&format!("major-update:{app}")).is_some(),
                "major-update:{app}"
            );
        }
        assert_eq!(
            label("backup:maintenance").unwrap(),
            "Monthly backup and updates: backup needs attention"
        );
    }
    use super::*;
    fn snapshot(now: f64, alerts: Value) -> Value {
        json!({"format":"neko-dashboard-v1","generated_at":now,"alerts":alerts,"applications":[{"id":"sonarr","state":"running","health":"healthy"}],"storage":[],"backups":[],"media":{"status":"current"},"metrics":{"temperature":40}})
    }
    #[test]
    fn rejects_destination_injection() {
        for s in [
            "http://host/topic",
            "https://user:secret@host/topic",
            "https://host/topic?x=y",
            "https://host/topic\nheader=x",
            "https://host:0/topic",
        ] {
            assert!(!endpoint(s));
        }
        assert!(endpoint("https://ntfy.example:8443/private_topic"));
    }
    #[test]
    fn transient_failure_is_debounced() {
        let s = snapshot(1000., json!([{"id":"app:sonarr","level":"warning"}]));
        let (state, e) = plan(&s, &json!({}), 1000., 180, 43200);
        assert!(e.is_empty());
        let (_, e) = plan(
            &snapshot(1200., s["alerts"].clone()),
            &state,
            1200.,
            180,
            43200,
        );
        assert_eq!(e.len(), 1);
    }
    #[test]
    fn mount_alert_is_immediate() {
        let (_, e) = plan(
            &snapshot(1000., json!([{"id":"mount:data_ssd","level":"critical"}])),
            &json!({}),
            1000.,
            180,
            43200,
        );
        assert_eq!(e.len(), 1);
    }
    #[test]
    fn unsent_recovery_is_silent() {
        let (state, _) = plan(
            &snapshot(1000., json!([{"id":"app:sonarr","level":"warning"}])),
            &json!({}),
            1000.,
            180,
            43200,
        );
        let (state, e) = plan(&snapshot(1100., json!([])), &state, 1100., 180, 43200);
        assert!(e.is_empty());
        assert!(state["entries"].as_object().unwrap().is_empty());
    }
    #[test]
    fn stale_input_never_recovers_reported_problem() {
        let previous = json!({"format":"neko-notifications-v1","entries":{"app:sonarr":{"last_sent":1000,"first_seen":900,"sent_level":"warning"}}});
        let (_, e) = plan(&json!({}), &previous, 2000., 0, 43200);
        assert!(!e.iter().any(|(_, r, _)| *r));
    }
    #[test]
    fn reported_problem_recovers_on_fresh_input() {
        let previous = json!({"format":"neko-notifications-v1","entries":{"app:sonarr":{"last_sent":1000,"first_seen":900,"sent_level":"warning"}}});
        let (_, e) = plan(&snapshot(2000., json!([])), &previous, 2000., 0, 43200);
        assert_eq!(e, vec![("app:sonarr".into(), true, "recovered".into())]);
    }
    #[test]
    fn service_alerts_notify_and_recover_only_with_evidence() {
        // Every service the collector watches can raise both of its alerts.
        for (unit, _) in crate::collect::STARTUP {
            assert!(
                label(&format!("startup:{unit}")).is_some(),
                "startup:{unit}"
            );
            assert!(
                label(&format!("startup:{unit}:failed")).is_some(),
                "startup:{unit}:failed"
            );
        }
        assert_eq!(
            label("startup:smb.service:failed").unwrap(),
            "Shared folders (Samba): stopped with an error"
        );
        let alert = json!([{"id":"startup:smb.service:failed","level":"critical"}]);
        let (_, e) = plan(&snapshot(1000., alert), &json!({}), 1000., 0, 43200);
        assert_eq!(
            e,
            vec![(
                "startup:smb.service:failed".into(),
                false,
                "critical".into()
            )]
        );
        let previous = json!({"format":"neko-notifications-v1","entries":{"startup:smb.service:failed":{"last_sent":1000}}});
        // The alert is gone but the service list is missing (systemctl failed): no recovery.
        let (_, e) = plan(&snapshot(2000., json!([])), &previous, 2000., 0, 43200);
        assert!(e.is_empty(), "{e:?}");
        // Listed and running again: recovered.
        let mut s = snapshot(2000., json!([]));
        s["startup"] = json!([{"unit":"smb.service","enabled":true,"active":"active"}]);
        let (_, e) = plan(&s, &previous, 2000., 0, 43200);
        assert_eq!(
            e,
            vec![(
                "startup:smb.service:failed".into(),
                true,
                "recovered".into()
            )]
        );
    }
    #[test]
    fn an_alert_about_something_no_longer_looked_after_is_dropped_quietly() {
        let previous = json!({"format":"neko-notifications-v1","entries":{
            "backup:archive":{"last_sent":1000},"app:qbittorrent":{"last_sent":1000}}});
        let mut s = snapshot(2000., json!([]));
        s["backups"] = json!([{"id":"archive","status":"disabled"}]);
        // qBittorrent is not in the list any more: taken out of the selection.
        let (state, e) = plan(&s, &previous, 2000., 0, 43200);
        assert!(
            e.is_empty(),
            "no RECOVERED for what was switched off: {e:?}"
        );
        assert!(state["entries"].as_object().unwrap().is_empty(), "{state}");
    }
    #[test]
    fn an_app_that_runs_but_does_not_answer_has_not_recovered() {
        let previous =
            json!({"format":"neko-notifications-v1","entries":{"app:sonarr":{"last_sent":1000}}});
        let mut s = snapshot(2000., json!([]));
        s["applications"] =
            json!([{"id":"sonarr","state":"running","health":"unknown","responding":false}]);
        let (_, e) = plan(&s, &previous, 2000., 0, 43200);
        assert!(e.is_empty(), "{e:?}");
        let shown = crate::collect::alerts(&s);
        assert!(shown
            .as_array()
            .unwrap()
            .iter()
            .any(|a| a["id"] == "app:sonarr" && a["level"] == "critical"));
    }
    #[test]
    fn unknown_app_is_not_recovered() {
        let previous =
            json!({"format":"neko-notifications-v1","entries":{"app:sonarr":{"last_sent":1000}}});
        let mut s = snapshot(2000., json!([]));
        s["applications"] = json!([{"id":"sonarr","state":"unknown"}]);
        let (_, e) = plan(&s, &previous, 2000., 0, 43200);
        assert!(e.is_empty());
    }
    #[test]
    fn recovery_needs_evidence_for_every_monitored_app_and_job() {
        // Alerts were open; now Nextcloud's state is unknown and the failed jobs are only
        // running again. None of that proves recovery.
        let previous = json!({"format":"neko-notifications-v1","entries":{
            "app:nextcloud":{"last_sent":1000},
            "backup:maintenance":{"last_sent":1000},
            "backup:music":{"last_sent":1000}}});
        let mut s = snapshot(2000., json!([]));
        s["applications"] = json!([{"id":"nextcloud","state":"unknown"}]);
        s["backups"] =
            json!([{"id":"maintenance","status":"running"},{"id":"music","status":"running"}]);
        let (_, e) = plan(&s, &previous, 2000., 0, 43200);
        assert!(e.is_empty(), "{e:?}");
        // Real evidence: Nextcloud running and both jobs passed.
        s["applications"] = json!([{"id":"nextcloud","state":"running","health":"healthy"}]);
        s["backups"] = json!([{"id":"maintenance","status":"pass"},{"id":"music","status":"pass"}]);
        let (_, e) = plan(&s, &previous, 2000., 0, 43200);
        assert_eq!(e.len(), 3, "{e:?}");
    }
    #[test]
    fn every_app_and_job_needs_evidence_before_recovery() {
        let mut s = snapshot(2000., json!([]));
        s["applications"] = json!([]);
        let (_, unknown, _) = observations(&s, 2000.);
        for (job, ..) in crate::collect::JOBS {
            assert!(unknown.contains(&format!("backup:{job}")), "{job}");
        }
        for (app, ..) in crate::collect::APPS {
            assert!(unknown.contains(&format!("app:{app}")), "{app}");
        }
        for (check, _) in crate::collect::CHECKS {
            assert!(unknown.contains(&format!("integrity:{check}")), "{check}");
        }
    }
    #[test]
    fn damaged_backup_copies_alert_and_recover_only_when_the_checks_are_clean() {
        let previous = json!({"format":"neko-notifications-v1","entries":{"integrity:usb_photos":{"last_sent":1000}}});
        let mut s = snapshot(2000., json!([]));
        // No results yet: not a recovery.
        let (_, e) = plan(&s, &previous, 2000., 0, 43200);
        assert!(e.is_empty(), "{e:?}");
        // Clean lists from quick runs only (no full check yet): still not a recovery.
        s["integrity"] = json!([{"id":"usb_photos","damaged":0,"both_damaged":0,"disagree":0,"missing":0,"full_at":null}]);
        let (_, e) = plan(&s, &previous, 2000., 0, 43200);
        assert!(e.is_empty(), "{e:?}");
        s["integrity"] = json!([{"id":"usb_photos","damaged":0,"both_damaged":0,"disagree":0,"missing":0,"full_at":1500.0}]);
        let (_, e) = plan(&s, &previous, 2000., 0, 43200);
        assert_eq!(e.len(), 1);
        assert!(label("integrity:usb_photos")
            .unwrap()
            .contains("damaged backup copies"));
    }
    #[test]
    fn untrusted_titles_and_ids_are_not_sent() {
        let (_, e) = plan(
            &snapshot(
                2000.,
                json!([{"id":"SECRET","level":"critical","title":"PRIVATE"},{"id":"app:sonarr","level":"warning","title":"PRIVATE"}]),
            ),
            &json!({}),
            2000.,
            0,
            43200,
        );
        assert_eq!(e.len(), 1);
        assert!(!label(&e[0].0).unwrap().contains("PRIVATE"));
    }
}

#[cfg(test)]
mod review_tests {
    use super::*;
    /// Every alert the collector can raise, all at once: each one must have a label, or
    /// the phone never hears about it (Tailscale signed out was dropped this way).
    #[test]
    fn every_collector_alert_can_reach_the_phone() {
        let mut both = vec![];
        for days_left in [Value::Null, json!(3.0)] {
            let drives: Vec<Value> = ["os", "data_ssd", "media_hdd"]
                .iter()
                .flat_map(|id| {
                    [
                        json!({"id": id, "name": id, "status": "missing", "smart": {"status": "fail"}}),
                        json!({"id": id, "name": id, "status": "mounted", "used_percent": 99.0, "smart": {"status": "warning"}}),
                    ]
                })
                .collect();
            let s = json!({
                "notifications": {"status": "fail"},
                "heartbeat": {"status": "stale"},
                "tailscale": {"state": if days_left.is_null() { "NeedsLogin" } else { "Running" }, "days_left": days_left},
                "startup": crate::collect::STARTUP.iter().map(|(u, n)| json!({"unit": u, "name": n, "active": "failed", "enabled": false})).collect::<Vec<_>>(),
                "storage": drives,
                "metrics": {"memory_percent": 99.0, "temperature": 99.0},
                "applications": crate::collect::APPS.iter().map(|(id, name, ..)| json!({"id": id, "name": name, "state": "exited"})).collect::<Vec<_>>(),
                "backups": crate::collect::JOBS.iter().map(|(id, name, ..)| json!({"id": id, "name": name, "status": "fail", "timer": {"installed": true}})).collect::<Vec<_>>(),
                "backup_key": false,
                "integrity": crate::collect::CHECKS.iter().map(|(id, name)| json!({"id": id, "name": name, "damaged": 1})).collect::<Vec<_>>(),
                "updates": [{"app": "immich", "name": "Immich"}, {"app": "nextcloud", "name": "Nextcloud"}],
                "media": {"status": "stale", "counts": {"fail": 1}},
            });
            both.extend(crate::collect::alerts(&s).as_array().unwrap().clone());
        }
        let ids: BTreeSet<&str> = both.iter().filter_map(|a| a["id"].as_str()).collect();
        for id in [
            "tailscale:login",
            "tailscale:expiry",
            "backup-key",
            "heartbeat:delivery",
        ] {
            assert!(ids.contains(id), "{id} not raised: {ids:?}");
        }
        for id in &ids {
            assert!(label(id).is_some(), "no label for {id}");
        }
    }
    /// A 5-minute collection interval is allowed: a reading 200 s old is current, and its
    /// alerts are acted on.
    #[test]
    fn a_slower_collector_is_not_monitoring_unavailable() {
        let now = 10_000.0;
        let s = json!({"format":"neko-dashboard-v1","generated_at":now-200.0,"collect_interval":300,
            "alerts":[{"id":"mount:media_hdd","level":"critical"}],"applications":[],"storage":[],"backups":[]});
        let (_, events) = plan(&s, &json!({}), now, 0, 43200);
        assert!(
            events.iter().any(|e| e.0 == "mount:media_hdd"),
            "{events:?}"
        );
        assert!(
            !events.iter().any(|e| e.0 == "monitoring-stale"),
            "{events:?}"
        );
        // At the default interval a reading 200 s old is late (never stricter than 3 minutes).
        let mut late = s.clone();
        late["collect_interval"] = json!(60);
        let (_, events) = plan(&late, &json!({}), now, 0, 43200);
        assert!(
            events.iter().any(|e| e.0 == "monitoring-stale"),
            "{events:?}"
        );
    }
}
