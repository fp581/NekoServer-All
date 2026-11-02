//! The weekly protected app updates' bookkeeping (MONTHLY-MAINTENANCE.md, "Weekly app
//! updates"). The daily check writes down each new version it finds for an app and when it
//! first saw it; the weekly update installs the newest one that has settled (waited
//! `maintenance.settle_days`, 7 by default, so a release with a bad bug is usually fixed
//! or pulled first), or the one you chose "Install at the next update night" for on the
//! dashboard. Kept on the SSD, root's only: the found version's settings hold the app's
//! passwords, like its compose files.
use crate::common::{self, Error, Result};
use serde_json::{json, Value};
use std::{
    fs,
    path::{Path, PathBuf},
};

/// Everything here lives on the SSD, under this folder.
pub(crate) const FOLDER: &str = "backups/updates";
const PENDING: &str = "pending.json";
const APPROVED: &str = "approved.json";
const PENDING_FORMAT: &str = "neko-pending-updates-v1";
const APPROVED_FORMAT: &str = "neko-approved-updates-v1";
/// How many versions found are kept per app: a stream that publishes a new build every
/// few days (most images do) still has an older one that has settled.
const KEEP: usize = 4;
pub(crate) const SETTLE_DAYS: u64 = 7;
const DAY: f64 = 86_400.0;

/// The days a new version waits (maintenance.settle_days, 1 to 30; 7 when not set).
pub(crate) fn settle_days(cfg: &Value) -> u64 {
    cfg["maintenance"]["settle_days"]
        .as_u64()
        .filter(|d| (1..=30).contains(d))
        .unwrap_or(SETTLE_DAYS)
}
/// The images a model runs, as one short name: what tells two versions apart.
pub(crate) fn signature(model: &Value) -> String {
    let mut images: Vec<String> = model["services"]
        .as_object()
        .into_iter()
        .flatten()
        .map(|(service, v)| format!("{service}={}", v["image"].as_str().unwrap_or("")))
        .collect();
    images.sort();
    common::sha(images.join("\n").as_bytes())[..16].to_string()
}
fn folder(data: &Path) -> PathBuf {
    data.join(FOLDER)
}
/// Where a found version's settings are kept (its model, an Immich release's files).
pub(crate) fn candidate_folder(data: &Path, app: &str, sig: &str) -> PathBuf {
    folder(data).join("candidates").join(format!("{app}-{sig}"))
}
fn read(path: &Path, format: &str) -> Value {
    let value = common::json(path, 2_000_000);
    if value["format"] == format && value["apps"].is_object() {
        value
    } else {
        json!({"format": format, "apps": {}})
    }
}
/// The versions found and not installed yet (a missing or broken file: none).
pub(crate) fn pending(data: &Path) -> Value {
    read(&folder(data).join(PENDING), PENDING_FORMAT)
}
pub(crate) fn save_pending(data: &Path, pending: &Value) -> Result<()> {
    common::directory(&folder(data), 0o700)?;
    common::atomic_json(&folder(data).join(PENDING), pending)
}
/// The versions you chose to install at the next update night, by app.
pub(crate) fn approved(data: &Path) -> Value {
    read(&folder(data).join(APPROVED), APPROVED_FORMAT)
}
/// "Install at the next update night" for the version `sig` of `app` (the dashboard).
/// Only a version the check found can be chosen.
pub(crate) fn approve(data: &Path, app: &str, sig: &str) -> Result<String> {
    let found = pending(data);
    let Some(version) = found["apps"][app]
        .as_array()
        .into_iter()
        .flatten()
        .find(|c| c["sig"] == sig)
        .map(|c| c["version"].as_str().unwrap_or("").to_string())
    else {
        return Err(Error::new("That version is not waiting any more"));
    };
    let mut chosen = approved(data);
    chosen["apps"][app] = json!(sig);
    common::directory(&folder(data), 0o700)?;
    common::atomic_json(&folder(data).join(APPROVED), &chosen)?;
    Ok(version)
}
/// Note the version `sig` the check found for `app` (`version`: how it is shown), with
/// when it was first seen; a version already noted keeps its first time. Only the newest
/// KEEP are kept; a dropped one's settings folder goes with it.
pub(crate) fn record(
    data: &Path,
    pending: &mut Value,
    app: &str,
    sig: &str,
    version: &str,
    now: f64,
) -> Result<bool> {
    let mut found: Vec<Value> = pending["apps"][app].as_array().cloned().unwrap_or_default();
    let new = !found.iter().any(|c| c["sig"] == sig);
    if new {
        found.push(json!({"sig": sig, "version": version, "first_seen": now}));
    }
    found.sort_by(|a, b| {
        b["first_seen"]
            .as_f64()
            .unwrap_or(0.0)
            .total_cmp(&a["first_seen"].as_f64().unwrap_or(0.0))
    });
    for dropped in found.split_off(KEEP.min(found.len())) {
        remove_candidate(data, app, dropped["sig"].as_str().unwrap_or(""))?;
    }
    pending["apps"][app] = json!(found);
    Ok(new)
}
/// What `app` runs now, as the check last saw it (shown on the Updates page).
pub(crate) fn note_installed(pending: &mut Value, app: &str, version: &str, now: f64) {
    pending["installed"][app] = json!({"version": version, "checked_at": now});
}
/// The app runs what the check found: nothing waits for it.
pub(crate) fn forget(data: &Path, pending: &mut Value, app: &str) -> Result<()> {
    for c in pending["apps"][app].as_array().cloned().unwrap_or_default() {
        remove_candidate(data, app, c["sig"].as_str().unwrap_or(""))?;
    }
    if let Some(apps) = pending["apps"].as_object_mut() {
        apps.remove(app);
    }
    Ok(())
}
/// After `sig` was installed: it and every version found before it are done with.
pub(crate) fn installed(data: &Path, app: &str, sig: &str) -> Result<()> {
    let mut found = pending(data);
    let list: Vec<Value> = found["apps"][app].as_array().cloned().unwrap_or_default();
    let at = list
        .iter()
        .find(|c| c["sig"] == sig)
        .and_then(|c| c["first_seen"].as_f64())
        .unwrap_or(f64::MAX);
    let (done, left): (Vec<Value>, Vec<Value>) = list
        .into_iter()
        .partition(|c| c["first_seen"].as_f64().unwrap_or(0.0) <= at);
    for c in done {
        remove_candidate(data, app, c["sig"].as_str().unwrap_or(""))?;
    }
    found["apps"][app] = json!(left);
    save_pending(data, &found)?;
    let mut chosen = approved(data);
    if chosen["apps"][app] == sig {
        if let Some(apps) = chosen["apps"].as_object_mut() {
            apps.remove(app);
        }
        common::atomic_json(&folder(data).join(APPROVED), &chosen)?;
    }
    Ok(())
}
fn remove_candidate(data: &Path, app: &str, sig: &str) -> Result<()> {
    if sig.is_empty() || !sig.bytes().all(|c| c.is_ascii_hexdigit()) {
        return Ok(());
    }
    let path = candidate_folder(data, app, sig);
    if fs::symlink_metadata(&path).is_ok() {
        fs::remove_dir_all(common::safe(&path)?)?;
    }
    Ok(())
}
/// The version of `app` the weekly update installs now, or None: the one you chose to
/// install, else the newest one that has waited `settle_days`. A version whose settings
/// or images are not here any more (`ready`) is passed over.
pub(crate) fn due(
    pending: &Value,
    approved: &Value,
    app: &str,
    now: f64,
    settle_days: u64,
    ready: &dyn Fn(&str) -> bool,
) -> Option<Value> {
    let found = pending["apps"][app].as_array()?;
    let chosen = approved["apps"][app].as_str();
    if let Some(c) = found.iter().find(|c| c["sig"].as_str() == chosen) {
        if ready(c["sig"].as_str().unwrap_or("")) {
            let mut c = c.clone();
            c["approved"] = json!(true);
            return Some(c);
        }
    }
    found
        .iter()
        .filter(|c| now - c["first_seen"].as_f64().unwrap_or(now) >= settle_days as f64 * DAY)
        .find(|c| ready(c["sig"].as_str().unwrap_or("")))
        .cloned()
}
/// The image IDs of every version found and not installed yet: the clean-up after an
/// update must not remove them (the daily check downloaded them for a later night).
pub(crate) fn waiting_images(data: &Path) -> std::collections::BTreeSet<String> {
    let mut out = std::collections::BTreeSet::new();
    let found = pending(data);
    for (app, list) in found["apps"].as_object().into_iter().flatten() {
        for c in list.as_array().into_iter().flatten() {
            let saved = common::json(
                &candidate_folder(data, app, c["sig"].as_str().unwrap_or(""))
                    .join("candidate.json"),
                64_000,
            );
            for image in saved["images"].as_object().into_iter().flatten() {
                if let Some(image) = image.1.as_str() {
                    out.insert(image.to_string());
                }
            }
        }
    }
    out
}
/// For the dashboard: each app's version now and the versions waiting, with when each
/// installs by itself and whether you chose it. Names and dates only, never a setting.
pub(crate) fn for_dashboard(data: &Path, settle_days: u64, now: f64) -> Value {
    let found = pending(data);
    let chosen = approved(data);
    let mut apps = serde_json::Map::new();
    for (app, seen) in found["installed"].as_object().into_iter().flatten() {
        apps.insert(
            app.clone(),
            json!({"app": app, "installed": common::sanitize(seen["version"].as_str().unwrap_or("")),
                "checked_at": seen["checked_at"].as_f64().map(|t| t.min(now)), "waiting": []}),
        );
    }
    for (app, list) in found["apps"].as_object().into_iter().flatten() {
        let entry = apps.entry(app.clone()).or_insert_with(
            || json!({"app": app, "installed": "", "checked_at": null, "waiting": []}),
        );
        for c in list.as_array().into_iter().flatten() {
            let first = c["first_seen"].as_f64().unwrap_or(now).min(now);
            if let Some(waiting) = entry["waiting"].as_array_mut() {
                waiting.push(json!({
                    "sig": c["sig"],
                    "version": common::sanitize(c["version"].as_str().unwrap_or("")),
                    "first_seen": first,
                    "installs_from": first + settle_days as f64 * DAY,
                    "approved": chosen["apps"][app.as_str()] == c["sig"],
                }));
            }
        }
    }
    json!({"settle_days": settle_days, "apps": apps.into_iter().map(|(_, v)| v).collect::<Vec<_>>()})
}

/* ---------- NekoServer's own releases (the NekoServer tab) ---------- */

/// Where NekoServer is published, and what the daily check found there (on the SSD).
pub(crate) const RELEASES: &str =
    "https://api.github.com/repos/fp581/NekoServer-All/releases?per_page=30";
const RELEASE_FILE: &str = "nekoserver.json";
/// "v0.43.0" (stable) or "v0.43.0Beta" (beta), as README.md "Release channels" says: the
/// numbers, and whether it is a beta. Any other tag (a typo) is no release.
pub(crate) fn release_version(tag: &str) -> Option<((u64, u64, u64), bool)> {
    let caps = regex::Regex::new(r"^v([0-9]+)\.([0-9]+)\.([0-9]+)(Beta)?$")
        .ok()?
        .captures(tag)?;
    let n = |i: usize| caps[i].parse::<u64>().ok();
    Some(((n(1)?, n(2)?, n(3)?), caps.get(4).is_some()))
}
/// The release channel (maintenance.nekoserver_channel): "beta" while every release is a
/// beta, "stable" to be offered stable releases only.
pub(crate) fn channel(cfg: &Value) -> &str {
    match cfg["maintenance"]["nekoserver_channel"].as_str() {
        Some("stable") => "stable",
        _ => "beta",
    }
}
/// The newest release newer than `current` on `channel`, from GitHub's list: never a
/// draft, never a tag outside the channels, and on the stable channel never a beta (nor
/// a release GitHub marks as a pre-release). A stable release counts as newer than a
/// beta of the same numbers.
pub(crate) fn newest_release(releases: &Value, channel: &str, current: &str) -> Option<Value> {
    let rank = |numbers: (u64, u64, u64), beta: bool| (numbers, !beta);
    let now = {
        let numbers = current
            .split('.')
            .map(|p| p.parse::<u64>().ok())
            .collect::<Option<Vec<_>>>()?;
        rank(
            (
                numbers.first().copied()?,
                *numbers.get(1)?,
                *numbers.get(2)?,
            ),
            true,
        )
    };
    releases
        .as_array()?
        .iter()
        .filter(|r| r["draft"] != true)
        .filter_map(|r| {
            let (numbers, beta) = release_version(r["tag_name"].as_str()?)?;
            let allowed = channel == "beta" || (!beta && r["prerelease"] != true);
            (allowed && rank(numbers, beta) > now).then(|| (rank(numbers, beta), r))
        })
        .max_by(|a, b| a.0.cmp(&b.0))
        .map(|(_, r)| {
            json!({
                "tag": r["tag_name"],
                "name": common::sanitize(r["name"].as_str().unwrap_or("")),
                "published_at": r["published_at"],
                "notes": r["html_url"],
            })
        })
}
/// Note what the check found: this release, and the newest one offered (or none).
pub(crate) fn save_release(
    data: &Path,
    installed: &str,
    channel: &str,
    available: Option<Value>,
    now: f64,
) -> Result<()> {
    common::directory(&folder(data), 0o700)?;
    common::atomic_json(
        &folder(data).join(RELEASE_FILE),
        &json!({"format": "neko-release-check-v1", "installed": installed, "channel": channel, "available": available, "checked_at": now}),
    )
}
/// What the NekoServer tab shows: names, dates and a link only.
pub(crate) fn release_for_dashboard(data: &Path) -> Value {
    let saved = common::json(&folder(data).join(RELEASE_FILE), 64_000);
    if saved["format"] != "neko-release-check-v1" {
        return json!({});
    }
    let notes = saved["available"]["notes"]
        .as_str()
        .filter(|u| u.starts_with("https://github.com/fp581/NekoServer-All/releases/"));
    json!({
        "installed": saved["installed"],
        "channel": saved["channel"],
        "checked_at": saved["checked_at"],
        "available": saved["available"]["tag"].as_str().filter(|t| release_version(t).is_some()).map(|tag| json!({
            "tag": tag,
            "name": saved["available"]["name"],
            "published_at": saved["available"]["published_at"],
            "notes": notes,
        })),
    })
}
/// The release the check offered, when `tag` is it: what the dashboard may start.
pub(crate) fn offered(data: &Path, tag: &str) -> bool {
    release_for_dashboard(data)["available"]["tag"] == tag
}

#[cfg(test)]
mod tests {
    use super::*;
    fn scratch(name: &str) -> PathBuf {
        let p = std::env::temp_dir().join(format!("neko-updates-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&p);
        fs::create_dir_all(&p).unwrap();
        p
    }
    const NOW: f64 = 1_800_000_000.0;
    #[test]
    fn a_new_version_waits_its_settling_days_unless_you_choose_it() {
        let data = scratch("settle");
        let mut found = pending(&data);
        assert!(record(
            &data,
            &mut found,
            "jellyfin",
            "aaaa",
            "10.11.1",
            NOW - 8.0 * DAY
        )
        .unwrap());
        assert!(record(
            &data,
            &mut found,
            "jellyfin",
            "bbbb",
            "10.11.2",
            NOW - 2.0 * DAY
        )
        .unwrap());
        // Seen again: it keeps the day it was first seen.
        assert!(!record(&data, &mut found, "jellyfin", "bbbb", "10.11.2", NOW).unwrap());
        save_pending(&data, &found).unwrap();
        let ready = |_: &str| true;
        let none = json!({"apps": {}});
        // The newest that has settled: 10.11.1 (8 days), not 10.11.2 (2 days).
        let due_now = due(&found, &none, "jellyfin", NOW, 7, &ready).unwrap();
        assert_eq!(due_now["sig"], "aaaa");
        // With 1 settling day, 10.11.2 (2 days) has settled too, and is the newest.
        assert_eq!(
            due(&found, &none, "jellyfin", NOW, 1, &ready).unwrap()["sig"],
            "bbbb"
        );
        // Chosen on the dashboard: installed at the next night, settled or not.
        assert_eq!(approve(&data, "jellyfin", "bbbb").unwrap(), "10.11.2");
        let chosen = approved(&data);
        let picked = due(&found, &chosen, "jellyfin", NOW, 7, &ready).unwrap();
        assert_eq!(
            (picked["sig"].as_str(), picked["approved"].as_bool()),
            (Some("bbbb"), Some(true))
        );
        // Only a version the check found can be chosen.
        assert!(approve(&data, "jellyfin", "cccc").is_err());
        // Settings or images gone: passed over.
        assert!(due(&found, &none, "jellyfin", NOW, 7, &|s: &str| s != "aaaa").is_none());
        // Nothing waits for an app with nothing found.
        assert!(due(&found, &none, "immich", NOW, 7, &ready).is_none());
        let _ = fs::remove_dir_all(data);
    }
    #[test]
    fn an_installed_version_and_those_before_it_are_done_with() {
        let data = scratch("installed");
        let mut found = pending(&data);
        for (sig, at) in [("aaaa", 30.0), ("bbbb", 20.0), ("cccc", 10.0)] {
            record(&data, &mut found, "immich", sig, sig, NOW - at * DAY).unwrap();
            fs::create_dir_all(candidate_folder(&data, "immich", sig)).unwrap();
        }
        save_pending(&data, &found).unwrap();
        approve(&data, "immich", "bbbb").unwrap();
        installed(&data, "immich", "bbbb").unwrap();
        let left = pending(&data);
        let sigs: Vec<&str> = left["apps"]["immich"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| c["sig"].as_str().unwrap())
            .collect();
        assert_eq!(sigs, ["cccc"]);
        assert!(!candidate_folder(&data, "immich", "aaaa").exists());
        assert!(candidate_folder(&data, "immich", "cccc").exists());
        assert!(approved(&data)["apps"]["immich"].is_null());
        // Only the newest KEEP versions are kept.
        let mut found = pending(&data);
        for n in 0..6 {
            record(
                &data,
                &mut found,
                "sonarr",
                &format!("{n:04}"),
                "x",
                NOW + n as f64,
            )
            .unwrap();
        }
        assert_eq!(found["apps"]["sonarr"].as_array().unwrap().len(), KEEP);
        forget(&data, &mut found, "sonarr").unwrap();
        assert!(found["apps"]["sonarr"].is_null());
        let _ = fs::remove_dir_all(data);
    }
    #[test]
    fn only_a_release_on_your_channel_and_newer_than_this_one_is_offered() {
        let list = json!([
            {"tag_name": "v0.44.0Beta", "draft": true, "prerelease": true},
            {"tag_name": "v0.43.1Beta", "prerelease": true, "html_url": "https://github.com/fp581/NekoServer-All/releases/tag/v0.43.1Beta"},
            {"tag_name": "v0.43.0", "prerelease": false},
            {"tag_name": "v0.42.0Bata", "prerelease": false},
            {"tag_name": "V0.50.0Beta", "prerelease": false},
            {"tag_name": "v0.41.0Beta", "prerelease": true},
        ]);
        // Beta: the newest beta or stable, never a draft or a typo.
        assert_eq!(
            newest_release(&list, "beta", "0.42.1").unwrap()["tag"],
            "v0.43.1Beta"
        );
        // Stable: betas are never offered.
        assert_eq!(
            newest_release(&list, "stable", "0.42.1").unwrap()["tag"],
            "v0.43.0"
        );
        // Nothing newer: nothing offered; a stable release beats its own beta.
        assert!(newest_release(&list, "beta", "0.43.1").is_none());
        let same = json!([{"tag_name": "v0.43.1"}, {"tag_name": "v0.43.1Beta"}]);
        assert_eq!(
            newest_release(&same, "beta", "0.43.0").unwrap()["tag"],
            "v0.43.1"
        );
        // A release GitHub marks as a pre-release is not stable, whatever its tag.
        let marked = json!([{"tag_name": "v0.43.0", "prerelease": true}]);
        assert!(newest_release(&marked, "stable", "0.42.1").is_none());
        assert_eq!(
            channel(&json!({"maintenance": {"nekoserver_channel": "stable"}})),
            "stable"
        );
        assert_eq!(channel(&json!({})), "beta");
        // What the dashboard sees, and the only tag it may start an upgrade to.
        let data = scratch("release");
        save_release(
            &data,
            "0.42.1",
            "beta",
            newest_release(&list, "beta", "0.42.1"),
            NOW,
        )
        .unwrap();
        let shown = release_for_dashboard(&data);
        assert_eq!(shown["available"]["tag"], "v0.43.1Beta");
        assert!(shown["available"]["notes"]
            .as_str()
            .unwrap()
            .ends_with("/v0.43.1Beta"));
        assert!(offered(&data, "v0.43.1Beta"));
        assert!(!offered(&data, "v0.43.0"));
        let _ = fs::remove_dir_all(data);
    }
    #[test]
    fn the_dashboard_sees_names_and_dates_only() {
        let data = scratch("dashboard");
        let mut found = pending(&data);
        record(&data, &mut found, "nextcloud", "abcd", "32.0.1", NOW - DAY).unwrap();
        save_pending(&data, &found).unwrap();
        note_installed(&mut found, "nextcloud", "32.0.0", NOW);
        save_pending(&data, &found).unwrap();
        let shown = for_dashboard(&data, 7, NOW);
        let app = &shown["apps"][0];
        assert_eq!(
            (app["app"].as_str(), app["installed"].as_str()),
            (Some("nextcloud"), Some("32.0.0"))
        );
        assert_eq!(app["waiting"][0]["installs_from"], json!(NOW + 6.0 * DAY));
        assert_eq!(app["waiting"][0]["approved"], false);
        assert_eq!(shown["settle_days"], 7);
        assert_eq!(settle_days(&json!({"maintenance": {"settle_days": 3}})), 3);
        assert_eq!(
            settle_days(&json!({"maintenance": {"settle_days": 99}})),
            SETTLE_DAYS
        );
        assert_eq!(settle_days(&json!({})), SETTLE_DAYS);
        // Two models differing in one image are two versions.
        let a = json!({"services": {"web": {"image": "sha256:1"}, "db": {"image": "sha256:2"}}});
        let b = json!({"services": {"db": {"image": "sha256:2"}, "web": {"image": "sha256:3"}}});
        assert_ne!(signature(&a), signature(&b));
        assert_eq!(signature(&a), signature(&a.clone()));
        let _ = fs::remove_dir_all(data);
    }
}
