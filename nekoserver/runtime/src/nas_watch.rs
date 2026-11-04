//! Drives on a network storage box (a NAS, over iSCSI): what happens when the NAS stops
//! answering. Tried in the lab: a break of up to ten minutes only pauses the writes
//! (setup raises the waits, network_storage.py); a longer one fails them, ext4 aborts its
//! journal and the drive stays read-only until it is checked. Linux still shows it
//! mounted "rw" then, so only a write tells.
//!
//! A timer runs this every minute. Each NekoServer drive that setup chose on a NAS gets a
//! test write (a small file written, synced and removed, in a folder of NekoServer's own):
//! - It works: the drive is fine. If this stopped the apps earlier, they are started
//!   again, now that the right drive is mounted at its folder and takes writes.
//! - It fails (read-only, an I/O error, the drive not mounted there): the apps that write
//!   there (Docker, the shared folders, the home gateway) are stopped, once, so nothing
//!   carries on half-written. The dashboard says what to do. Nothing is unmounted by
//!   force and the filesystem is never repaired here: that is the recovery menu's
//!   "Check a drive that turned read-only", with you there.
//! - No answer within 20 seconds: the NAS is still away and the writes wait for it.
//!   Nothing is done (stopping the apps would wait as well); it is looked at again in a
//!   minute.
use crate::common::{self, Error, Result};
use crate::drive_return;
use serde_json::{json, Value};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::Duration;

/// What the last check found for each drive ("writes", "fails", "waits", for the
/// dashboard) and whether this stopped the apps (gone after a restart: setup and the
/// drive-return service start them then).
pub(crate) const STATE: &str = "/run/nekoserver-nas-watch.json";
/// The folder at each drive's top that holds the test file.
pub(crate) const PROBE_DIR: &str = ".nekoserver-check";
/// A test write that takes longer than this is "no answer".
const PROBE_WAIT: Duration = Duration::from_secs(20);

#[derive(Debug, PartialEq, Clone, Copy)]
pub(crate) enum Health {
    /// The test write worked.
    Writes,
    /// The test write failed, or the drive is not mounted at its folder.
    Fails,
    /// No answer yet: the NAS is away and the write waits.
    Waits,
}

/// One test write at `top` (a drive's folder): written, synced and removed.
pub(crate) fn write_test(top: &Path) -> std::io::Result<()> {
    use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
    let dir = top.join(PROBE_DIR);
    match fs::DirBuilder::new().mode(0o700).create(&dir) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
        Err(e) => return Err(e),
    }
    if fs::symlink_metadata(&dir)?.file_type().is_symlink() {
        return Err(std::io::Error::other("the check folder is a link"));
    }
    let file = dir.join(format!("probe-{}", std::process::id()));
    let mut out = fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .custom_flags(libc::O_NOFOLLOW)
        .mode(0o600)
        .open(&file)?;
    out.write_all(b"NekoServer checks that this drive takes writes\n")?;
    out.sync_all()?;
    drop(out);
    fs::remove_file(&file)?;
    fs::File::open(&dir)?.sync_all()
}

/// How the drive at `top` is: mounted there as the right filesystem (`uuid`), and taking
/// a test write within PROBE_WAIT.
pub(crate) fn health(top: &Path, uuid: &str) -> Health {
    let mounted = common::findmnt("UUID", "--mountpoint", top, 8)
        .ok()
        .flatten()
        .is_some_and(|u| u.trim() == uuid);
    if !mounted {
        return Health::Fails;
    }
    let owned = top.to_path_buf();
    let handle = std::thread::spawn(move || write_test(&owned).is_ok());
    match common::joined(handle, PROBE_WAIT) {
        Some(true) => Health::Writes,
        Some(false) => Health::Fails,
        None => Health::Waits,
    }
}

/// What to do now, from the drives' health and whether this stopped the apps already.
#[derive(Debug, PartialEq)]
enum Act {
    Nothing,
    Hold,
    Resume,
}
fn decide(states: &[Health], held: bool) -> Act {
    if states.contains(&Health::Fails) {
        if held {
            Act::Nothing
        } else {
            Act::Hold
        }
    } else if states.contains(&Health::Waits) {
        Act::Nothing
    } else if held {
        Act::Resume
    } else {
        Act::Nothing
    }
}

/// The NekoServer drives setup chose on a NAS: (folder, filesystem UUID, name).
pub(crate) fn nas_drives(cfg: &Value) -> Result<Vec<(PathBuf, String, &'static str)>> {
    let mut found = vec![];
    for (key, folder, name) in [
        ("data_ssd", "data_mount", "the application SSD"),
        ("media_hdd", "media_mount", "the media drive"),
    ] {
        if !cfg["storage"][key]["network"].is_object() {
            continue;
        }
        let uuid = cfg["storage"][key]["uuid"].as_str().unwrap_or("");
        if uuid.is_empty() || !uuid.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-') {
            return Err(Error::new("Drive identity missing from the settings"));
        }
        found.push((
            common::mount_path(cfg[folder].as_str().unwrap_or(""))?,
            uuid.to_string(),
            name,
        ));
    }
    Ok(found)
}

/// The services this stops: what writes to the drives (the apps in Docker, the shared
/// folders, the home gateway). Docker's socket too, so a command can't wake it.
const WRITERS: [&str; 5] = [
    "docker.socket",
    "docker.service",
    "smbd.service",
    "smb.service",
    "nekoserver-gateway-lan.service",
];

pub fn run(a: &crate::Args) -> Result<()> {
    common::require_root()?;
    let cfgpath = a.path("config")?;
    let cfg = common::json(&cfgpath, 64000);
    let drives = nas_drives(&cfg)?;
    if drives.is_empty() {
        return Ok(());
    }
    let state = common::json(Path::new(STATE), 64000);
    let held = state["held"] == true;
    let states: Vec<Health> = drives.iter().map(|(p, u, _)| health(p, u)).collect();
    let mut seen = serde_json::Map::new();
    for ((path, _, _), h) in drives.iter().zip(&states) {
        let key = if *path == common::mount_path(cfg["data_mount"].as_str().unwrap_or(""))? {
            "data_ssd"
        } else {
            "media_hdd"
        };
        seen.insert(
            key.into(),
            json!(match h {
                Health::Writes => "writes",
                Health::Fails => "fails",
                Health::Waits => "waits",
            }),
        );
    }
    let mut record = json!({"at": common::now(), "drives": seen, "held": held, "since": state["since"], "stopped_for": state["stopped_for"]});
    let act = decide(&states, held);
    if act == Act::Nothing {
        return save(&record);
    }
    match act {
        Act::Nothing => Ok(()),
        Act::Hold => {
            let failing: Vec<String> = drives
                .iter()
                .zip(&states)
                .filter(|(_, h)| **h == Health::Fails)
                .map(|((p, _, name), _)| format!("{name} ({})", p.display()))
                .collect();
            println!(
                "NAS watch: {} does not take writes (it turned read-only after the network storage box was away for long, most likely). Stopping the apps so nothing carries on half-written; check the drive with  sudo bash install.sh --recover  (\"Check a drive that turned read-only\")",
                failing.join(", ")
            );
            // --no-block: a stop that waits for the drive must never hang this check.
            let mut args = vec!["systemctl", "stop", "--no-block"];
            args.extend(WRITERS);
            let (code, _) = common::command(&args, 60)?;
            // 5: one of them is not installed here (Debian has smbd, Arch smb): the rest stop.
            if code != 0 && code != 5 {
                println!("NAS watch: systemctl stop exited {code}");
            }
            record["held"] = json!(true);
            record["since"] = json!(common::now());
            record["stopped_for"] = json!(failing);
            save(&record)
        }
        Act::Resume => {
            if common::setup_running() {
                println!("NAS watch: the drives take writes again; setup is running and starts the apps itself");
                return Ok(());
            }
            let data = common::mount_path(cfg["data_mount"].as_str().unwrap_or(""))?;
            let media = common::mount_path(cfg["media_mount"].as_str().unwrap_or(""))?;
            println!("NAS watch: the drives on the network storage box take writes again; starting the apps");
            drive_return::start_again(&cfgpath, &data, &media, "NAS watch")?;
            record["held"] = json!(false);
            record["since"] = Value::Null;
            record["stopped_for"] = Value::Null;
            save(&record)
        }
    }
}
/// Root's alone (the dashboard's collector and the recovery menu run as root).
fn save(record: &Value) -> Result<()> {
    common::atomic_json(Path::new(STATE), record)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn the_apps_stop_once_on_a_failing_drive_and_start_only_when_all_take_writes() {
        use Health::*;
        assert_eq!(decide(&[Writes], false), Act::Nothing);
        assert_eq!(decide(&[Fails], false), Act::Hold);
        assert_eq!(decide(&[Fails], true), Act::Nothing); // once
        assert_eq!(decide(&[Writes, Fails], true), Act::Nothing);
        // Still away: the writes wait, nothing is done either way.
        assert_eq!(decide(&[Waits], false), Act::Nothing);
        assert_eq!(decide(&[Waits, Writes], true), Act::Nothing);
        assert_eq!(decide(&[Writes, Writes], true), Act::Resume);
    }
    #[test]
    fn a_test_write_leaves_nothing_and_refuses_a_link() {
        let root = std::env::temp_dir().join(format!("neko-nas-probe-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        write_test(&root).unwrap();
        assert_eq!(fs::read_dir(root.join(PROBE_DIR)).unwrap().count(), 0);
        fs::remove_dir(root.join(PROBE_DIR)).unwrap();
        std::os::unix::fs::symlink("/tmp", root.join(PROBE_DIR)).unwrap();
        assert!(write_test(&root).is_err());
        // Not mounted at its folder: failing (never a write onto the system disk).
        assert_eq!(health(&root, "not-this-uuid"), Health::Fails);
        let _ = fs::remove_dir_all(&root);
    }
    #[test]
    fn only_the_drives_chosen_on_a_nas_are_watched() {
        let cfg = json!({"data_mount": "/srv/nekoserver", "media_mount": "/mnt/nekoserver-media",
            "storage": {"data_ssd": {"uuid": "ssd-1"},
                "media_hdd": {"uuid": "nas-1", "network": {"transport": "iscsi", "wwn": "0x1"}}}});
        let found = nas_drives(&cfg).unwrap();
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].1, "nas-1");
        let mut bad = cfg.clone();
        bad["storage"]["media_hdd"]["uuid"] = json!("x; rm");
        assert!(nas_drives(&bad).is_err());
        assert!(nas_drives(&json!({"storage": {}})).unwrap().is_empty());
    }
}
