//! The person's own folders on a network share (NFS, SMB), used where they are (0.50.0,
//! existing_media.py). Setup mounts each into the library (library/existing/<kind>), and
//! the apps that see it bind that folder. When the share does not answer (the NAS is off
//! when the PC starts, say), the library's folder is the empty one left in its place:
//! Jellyfin would show an empty library, and Radarr could start writing there.
//!
//! So, at start (drive-return) and every minute (the NAS check):
//! - a share that does not answer: the apps that see it are stopped, and kept stopped;
//! - a share that answers again: its folder is mounted into the library again, and the
//!   apps this stopped are started again (only those: never one stopped by hand).
//!
//! What it found goes to `<app SSD>/reports/shared-folders.json`, which the dashboard
//! reads to name the missing share; the apps it stopped are kept there too, so they are
//! started again after a restart as well.
use crate::common;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

/// An app NekoServer knows (the shared catalogue), as the settings may name one to stop.
fn known_app(app: &str) -> bool {
    common::APPS.iter().any(|a| a.0 == app)
}
/// Where the report is, under the app SSD.
const REPORT: &str = "reports/shared-folders.json";

/// One folder of the person's on a share, as the settings name it.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Share {
    pub holds: String,
    pub path: PathBuf,
    pub mount: PathBuf,
    pub share: String,
    pub point: PathBuf,
    pub apps: Vec<String>,
}

/// The shared folders the settings name ("shared_folders", written by setup), each one
/// checked as far as a settings file can be trusted: plain absolute paths, mounted into
/// the library of one of NekoServer's drives, and only apps that may see one. A broken
/// entry is left out (and said), never used.
pub(crate) fn listed(cfg: &Value) -> Vec<Share> {
    cfg["shared_folders"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|entry| {
            let found = share_of(cfg, entry);
            if found.is_none() {
                println!("Shared folders: an entry of the settings is not one NekoServer can use; it is left out");
            }
            found
        })
        .collect()
}

fn share_of(cfg: &Value, entry: &Value) -> Option<Share> {
    let text = |key: &str| entry[key].as_str().unwrap_or("").to_string();
    let path = |key: &str| {
        let value = text(key);
        let p = PathBuf::from(&value);
        (p.is_absolute() && common::safe(&p).is_ok() && !value.contains('\n')).then_some(p)
    };
    let apps: Vec<String> = entry["apps"]
        .as_array()?
        .iter()
        .map(|a| a.as_str().unwrap_or("").to_string())
        .collect();
    let share = text("share");
    let point = path("point")?;
    let fine = !share.is_empty() && apps.iter().all(|a| known_app(a)) && in_a_library(cfg, &point);
    fine.then_some(Share {
        holds: text("holds"),
        path: path("path")?,
        mount: path("mount")?,
        share,
        point,
        apps,
    })
}

/// `point` is <one of NekoServer's drives>/library/existing/<kind>: where setup mounts the
/// person's folders, and nowhere else (this runs `mount` on it).
fn in_a_library(cfg: &Value, point: &Path) -> bool {
    let mut drives: Vec<PathBuf> = ["data_mount", "media_mount"]
        .iter()
        .filter_map(|key| common::mount_path(cfg[*key].as_str().unwrap_or("")).ok())
        .collect();
    drives.extend(
        common::drives_in_use(cfg)
            .iter()
            .filter_map(|d| common::drive_folder(cfg, d)),
    );
    let kind = point.file_name().and_then(|n| n.to_str()).unwrap_or("");
    let plain = !kind.is_empty() && kind.bytes().all(|b| b.is_ascii_lowercase() || b == b'-');
    plain
        && drives
            .iter()
            .any(|d| point.parent() == Some(d.join("library").join("existing").as_path()))
}

/// What findmnt shows at the share's own mount folder ("FSTYPE SOURCE") is the share
/// itself, really mounted: not systemd's automount alone ("autofs systemd-1"), not
/// another share, not nothing.
fn really_mounted(found: Option<&str>, share: &str) -> bool {
    let mut fields = found.unwrap_or("").split_whitespace();
    let (fstype, source) = (fields.next().unwrap_or(""), fields.next().unwrap_or(""));
    !fstype.is_empty() && fstype != "autofs" && source == share
}

/// The share answers: its folder can be listed (which mounts an automount), within a
/// time limit, and it is the share that is mounted there.
fn answers(s: &Share) -> bool {
    let shown = s.path.to_string_lossy();
    let listed = common::command(&["ls", "-1", "--", &shown], 15).is_ok_and(|(code, _)| code == 0);
    let found = common::findmnt("FSTYPE,SOURCE", "--mountpoint", &s.mount, 10)
        .ok()
        .flatten();
    listed && really_mounted(found.as_deref(), &s.share)
}

fn bound(point: &Path) -> bool {
    matches!(
        common::findmnt("TARGET", "--mountpoint", point, 10),
        Ok(Some(_))
    )
}

/// What to do now: the apps to stop (they see a share that does not answer) and the ones
/// to start (stopped by this before, and every share they see answers again).
/// `ready`: for each share, whether it answers and is mounted into the library.
fn decide(shares: &[Share], ready: &[bool], held_before: &[String]) -> (Vec<String>, Vec<String>) {
    let mut stop: Vec<String> = vec![];
    for (s, ok) in shares.iter().zip(ready) {
        for app in s.apps.iter().filter(|_| !ok) {
            if !stop.contains(app) {
                stop.push(app.clone());
            }
        }
    }
    let start = held_before
        .iter()
        .filter(|app| !stop.contains(app))
        .cloned()
        .collect();
    (stop, start)
}

/// The check, at start and every minute. Never fails its caller: what goes wrong is said.
pub(crate) fn check(cfg: &Value) {
    if common::setup_running() {
        return; // setup mounts the folders and starts the apps itself
    }
    let Ok(data) = common::mount_path(cfg["data_mount"].as_str().unwrap_or("")) else {
        return;
    };
    let report = data.join(REPORT);
    let before = common::json(&report, 64000);
    let held_before: Vec<String> = before["held"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|a| a.as_str())
        .filter(|a| known_app(a))
        .map(str::to_string)
        .collect();
    let shares = listed(cfg);
    if shares.is_empty() && held_before.is_empty() {
        return;
    }
    let ready: Vec<bool> = shares.iter().map(bring_back).collect();
    let (stop, start) = decide(&shares, &ready, &held_before);
    for app in &stop {
        stop_app(app);
    }
    for app in &start {
        start_app(app);
    }
    let rows: Vec<Value> = shares
        .iter()
        .zip(&ready)
        .map(|(s, ok)| {
            json!({"holds": s.holds, "path": s.path, "share": s.share, "ready": ok, "apps": s.apps})
        })
        .collect();
    let saved = json!({"at": common::now(), "shares": rows, "held": stop});
    if let Err(e) = common::atomic_json(&report, &saved) {
        println!("Shared folders: the report could not be saved ({e})");
    }
}

/// A share that answers is mounted into the library again when it is not. True when it
/// answers and is mounted there.
fn bring_back(s: &Share) -> bool {
    if !answers(s) {
        println!(
            "Shared folders: {} ({}) does not answer; the apps that see it stay stopped, and nothing is written in its place",
            s.share,
            s.path.display()
        );
        return false;
    }
    if !bound(&s.point) {
        let shown = s.point.to_string_lossy();
        match common::command(&["mount", &shown], 60) {
            Ok((0, _)) => println!(
                "Shared folders: {} answers again; mounted into {shown}",
                s.share
            ),
            _ => println!(
                "Shared folders: {} answers, but {shown} could not be mounted yet",
                s.share
            ),
        }
    }
    bound(&s.point)
}

/// The containers of one app (its Compose project neko-<app>), all of them (`-a`) or
/// only the running ones.
fn containers(app: &str, all: bool) -> Vec<String> {
    let label = format!("label=com.docker.compose.project=neko-{app}");
    let mut args = vec!["docker", "ps", "-q", "--filter", label.as_str()];
    if all {
        args.push("-a");
    }
    common::command(&args, 30)
        .map(|(_, out)| out.split_whitespace().map(str::to_string).collect())
        .unwrap_or_default()
}

fn stop_app(app: &str) {
    let running = containers(app, false);
    if running.is_empty() {
        return;
    }
    let mut args = vec!["docker", "stop"];
    args.extend(running.iter().map(String::as_str));
    match common::command(&args, 180) {
        Ok((0, _)) => {
            println!("Shared folders: {app} stopped until the share it sees answers again")
        }
        _ => println!("Shared folders: {app} could not be stopped; tried again in a minute"),
    }
}

fn start_app(app: &str) {
    let all = containers(app, true);
    if all.is_empty() {
        return;
    }
    let mut args = vec!["docker", "start"];
    args.extend(all.iter().map(String::as_str));
    match common::command(&args, 180) {
        Ok((0, _)) => println!("Shared folders: {app} started again"),
        _ => println!("Shared folders: {app} could not be started; tried again in a minute"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn settings(point: &str, apps: Value) -> Value {
        json!({
            "data_mount": "/srv/nekoserver",
            "media_mount": "/mnt/nekoserver-media",
            "storage": {"d3": {"uuid": "d3-1", "fstype": "ext4"}},
            "locations": {"music": "d3"},
            "shared_folders": [{
                "holds": "movies", "path": "/mnt/tank/films", "mount": "/mnt/tank",
                "share": "nas:/tank", "point": point, "apps": apps,
            }],
        })
    }

    #[test]
    fn only_folders_mounted_into_a_library_and_apps_that_see_them_are_used() {
        let apps = json!(["jellyfin", "radarr"]);
        let good = listed(&settings(
            "/mnt/nekoserver-media/library/existing/movies",
            apps.clone(),
        ));
        assert_eq!(good.len(), 1);
        assert_eq!(
            good[0].point,
            Path::new("/mnt/nekoserver-media/library/existing/movies")
        );
        // On a drive something is placed on (the music's d3), too.
        assert_eq!(
            listed(&settings(
                "/mnt/nekoserver-drives/d3/library/existing/music",
                apps.clone()
            ))
            .len(),
            1
        );
        // Anything else is never mounted or stopped for.
        for point in [
            "/etc/fstab",
            "/mnt/nekoserver-media/library/movies",
            "/mnt/nekoserver-media/library/existing/../../x",
            "/mnt/nekoserver-drives/d4/library/existing/movies",
            "library/existing/movies",
        ] {
            assert!(listed(&settings(point, apps.clone())).is_empty(), "{point}");
        }
        let docker = settings(
            "/mnt/nekoserver-media/library/existing/movies",
            json!(["portainer"]),
        );
        assert!(listed(&docker).is_empty());
        assert!(listed(&json!({})).is_empty());
    }

    #[test]
    fn an_automount_alone_or_another_share_is_not_the_share() {
        assert!(really_mounted(Some("nfs4 nas:/tank"), "nas:/tank"));
        assert!(!really_mounted(Some("autofs systemd-1"), "nas:/tank"));
        assert!(!really_mounted(Some("nfs4 other:/tank"), "nas:/tank"));
        assert!(!really_mounted(None, "nas:/tank"));
        assert!(!really_mounted(Some(""), "nas:/tank"));
    }

    /// Gate run C8: with the share away, the apps that see it stop; once it answers and
    /// is mounted into the library again, exactly the ones stopped for it start again.
    #[test]
    fn the_apps_that_see_a_share_that_is_away_stop_and_start_with_it() {
        let share = |holds: &str, apps: &[&str]| Share {
            holds: holds.into(),
            path: PathBuf::from("/mnt/tank/films"),
            mount: PathBuf::from("/mnt/tank"),
            share: "nas:/tank".into(),
            point: PathBuf::from("/mnt/nekoserver-media/library/existing").join(holds),
            apps: apps.iter().map(|a| a.to_string()).collect(),
        };
        let both = [
            share("movies", &["jellyfin", "radarr", "sonarr", "qbittorrent"]),
            share("music", &["navidrome", "lidarr"]),
        ];
        let names = |list: &[&str]| list.iter().map(|a| a.to_string()).collect::<Vec<_>>();
        // The films' share away at start: its apps stop, the music's run.
        let (stop, start) = decide(&both, &[false, true], &[]);
        assert_eq!(
            stop,
            names(&["jellyfin", "radarr", "sonarr", "qbittorrent"])
        );
        assert!(start.is_empty());
        // A minute later, still away: still stopped, nothing started.
        let (stop, start) = decide(&both, &[false, true], &stop);
        assert_eq!(stop.len(), 4);
        assert!(start.is_empty());
        // Back and mounted: the four start again.
        let (stop, start) = decide(
            &both,
            &[true, true],
            &names(&["jellyfin", "radarr", "sonarr", "qbittorrent"]),
        );
        assert!(stop.is_empty());
        assert_eq!(
            start,
            names(&["jellyfin", "radarr", "sonarr", "qbittorrent"])
        );
        // An app that sees two shares starts only once both answer.
        let two = [share("movies", &["jellyfin"]), share("tv", &["jellyfin"])];
        let (stop, start) = decide(&two, &[true, false], &names(&["jellyfin"]));
        assert_eq!((stop, start), (names(&["jellyfin"]), vec![]));
        // Nothing stopped before, all there: nothing to do.
        assert_eq!(decide(&both, &[true, true], &[]), (vec![], vec![]));
    }
}
