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
pub(crate) static STATE: std::sync::LazyLock<&str> =
    std::sync::LazyLock::new(|| common::shared("nas_watch", "state"));
/// The folder at each drive's top that holds the test file.
pub(crate) static PROBE_DIR: std::sync::LazyLock<&str> =
    std::sync::LazyLock::new(|| common::shared("nas_watch", "probe_dir"));
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
    /// Not mounted, and its disk is not connected either: the NAS was away when this PC
    /// started (or logged it out). The apps on it are stopped already (Docker is bound to
    /// its drives); drive-return mounts it and starts them when the disk appears.
    Absent,
}

/// One test write at `top` (a drive's folder): written, synced and removed.
pub(crate) fn write_test(top: &Path) -> std::io::Result<()> {
    use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
    let dir = top.join(*PROBE_DIR);
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
        let connected = Path::new("/dev/disk/by-uuid").join(uuid).exists();
        return if connected {
            Health::Fails
        } else {
            Health::Absent
        };
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
    /// Stopped before, the drive still failing: the writers must still be stopped.
    StillHeld,
    Resume,
}
fn decide(states: &[Health], held: bool) -> Act {
    if held && states.iter().any(|h| *h != Health::Writes) {
        // Held until every drive takes writes again: a drive gone or waiting meanwhile
        // never lets a writer that started again (drive-return, by hand) carry on (the
        // 0.46.0 review).
        Act::StillHeld
    } else if states.contains(&Health::Absent) {
        // Nothing to stop (see Absent) and nothing to start yet: drive-return does that.
        Act::Nothing
    } else if states.contains(&Health::Fails) {
        if held {
            Act::StillHeld
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
fn writers() -> Vec<&'static str> {
    common::shared_list(&["writers"])
}

pub fn run(a: &crate::Args) -> Result<()> {
    common::require_root()?;
    let cfgpath = a.path("config")?;
    let cfg = common::json(&cfgpath, 64000);
    // One at a time with the recovery menu, which writes "held" here too (the 0.46.1
    // audit: a check already running wrote over the menu's note, and the apps the menu
    // stopped were never started).
    let _turn = state_lock()?;
    let drives = nas_drives(&cfg)?;
    let states: Vec<Health> = drives.iter().map(|(p, u, _)| health(p, u)).collect();
    // Logged in again when a drive is gone, or a target NekoServer uses has no session
    // (the backup drive's too, which no drive above shows: the 0.46.1 audit).
    if states.contains(&Health::Absent) || !missing_sessions(&cfg, &sessions()).is_empty() {
        log_in_again(&cfg);
    }
    let state = common::json(Path::new(*STATE), 64000);
    let held = state["held"] == true;
    if drives.is_empty() {
        // No NAS drive to hold the apps for (only the backup drive is on the NAS): a hold
        // left from before is lifted, or nothing would ever lift it (the audit of 0.46.2).
        if held {
            save(&json!({"at": common::now(), "drives": {}, "held": false}))?;
        }
        return Ok(());
    }
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
                Health::Absent => "absent",
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
            if !stop_writers()? {
                // Not "held": the next check (in a minute) stops them again (the 0.46.0
                // review, H06: a failed stop was recorded as done and never tried again).
                record["stop_failed"] = json!(true);
                return save(&record);
            }
            record["held"] = json!(true);
            record["since"] = json!(common::now());
            record["stopped_for"] = json!(failing);
            save(&record)
        }
        Act::StillHeld => {
            // A writer started again since (by hand, or a stop that didn't finish): stopped
            // again, so "held" always means stopped.
            let running = running_writers();
            if !running.is_empty() {
                println!(
                    "NAS watch: {} still running while the drive does not take writes; stopping it again",
                    running.join(", ")
                );
                if !stop_writers()? {
                    record["stop_failed"] = json!(true);
                }
            }
            save(&record)
        }
        Act::Resume => {
            if common::setup_running() {
                println!("NAS watch: the drives take writes again; setup is running and starts the apps itself");
                return Ok(());
            }
            let data = common::mount_path(cfg["data_mount"].as_str().unwrap_or(""))?;
            let media = common::mount_path(cfg["media_mount"].as_str().unwrap_or(""))?;
            // The right drives first: until they are, the apps stay held (tried again in a
            // minute).
            crate::backup::guard(&cfgpath, &data, &media)?;
            println!("NAS watch: the drives on the network storage box take writes again; starting the apps");
            // No longer held before the start: the start guard of Docker, Samba and the
            // gateway refuses while held (startup.rs held_back).
            let held = record.clone();
            record["held"] = json!(false);
            save(&record)?;
            if let Err(e) = drive_return::start_again(&cfgpath, &data, &media, "NAS watch") {
                if !drive_return::apps_up() {
                    // Docker did not start: held again, so
                    // the next check (in a minute) starts them again (the review of 0.46.3:
                    // the hold was lifted, and nothing started them any more).
                    println!(
                        "NAS watch: the apps did not start again ({e}); trying again in a minute"
                    );
                    let _ = stop_writers(); // what did start, stopped again while held
                    return save(&held);
                }
                // The apps' services run; something after them failed (a job's own resume).
                // No longer held: retrying every minute would restart everything each time
                // (the 0.46.1 audit). Said, in this check's log.
                println!("NAS watch: the apps run again, but not everything finished ({e}); see the journal of the service named");
            }
            record["held"] = json!(false);
            record["since"] = Value::Null;
            record["stopped_for"] = Value::Null;
            save(&record)
        }
    }
}
/// The writers asked to stop. False when systemctl refused (anything but 5: one of them
/// not installed here, as Debian has smbd and Arch smb). --no-block: a stop that waits for
/// the drive must never hang this check; whether they stopped is looked at next time
/// (running_writers).
fn stop_writers() -> Result<bool> {
    stop_writers_with(&|args| common::command(args, 60))
}
/// How a command is run (systemctl here; a stand-in in the tests): its exit code and output.
type Run<'a> = &'a dyn Fn(&[&str]) -> Result<(i32, String)>;
fn stop_writers_with(systemctl: Run) -> Result<bool> {
    let mut args = vec!["systemctl", "stop", "--no-block"];
    args.extend(writers());
    let (code, _) = systemctl(&args)?;
    if code != 0 && code != 5 {
        println!("NAS watch: stopping the apps did not work (systemctl exited {code}); trying again in a minute");
        return Ok(false);
    }
    Ok(true)
}
/// The writers that are running (or starting) now.
fn running_writers() -> Vec<&'static str> {
    writers()
        .into_iter()
        .filter(|unit| {
            common::command(&["systemctl", "is-active", "--quiet", unit], 15)
                .is_ok_and(|(code, _)| code == 0)
                || common::command(&["systemctl", "is-active", unit], 15)
                    .is_ok_and(|(_, out)| out.trim() == "activating")
        })
        .collect()
}
/// A NAS drive is not connected: the NAS was away when this PC started, and open-iscsi
/// gives up its logins after about two minutes (seen in the lab: the drive then stayed
/// missing for good once the NAS came on). Once the NAS answers on its iSCSI port again,
/// every target saved to log in at start is logged in again; the disk then appears, and
/// drive-return mounts it and starts the apps.
fn log_in_again(cfg: &Value) {
    let portal = cfg["network_storage"]["portal"].as_str().unwrap_or("");
    if !answers(portal) {
        return;
    }
    match common::command(&["iscsiadm", "-m", "node", "--loginall=automatic"], 60) {
        Ok((0, _)) => println!("NAS watch: the network storage box answers again; logged in to its drives"),
        // 15: logged in already.
        Ok((15, _)) => {}
        Ok((code, _)) => println!("NAS watch: logging in to the network storage box did not work yet (iscsiadm exited {code}); trying again in a minute"),
        Err(e) => println!("NAS watch: logging in to the network storage box did not work yet ({e})"),
    }
}
/// The lock on this check's record (shared.json nas_watch.lock), waited for.
fn state_lock() -> Result<std::fs::File> {
    use std::os::unix::fs::OpenOptionsExt;
    use std::os::unix::io::AsRawFd;
    let file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(common::shared("nas_watch", "lock"))?;
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) } != 0 {
        return Err(Error::new("The NAS check's lock could not be taken"));
    }
    Ok(file)
}
/// What `iscsiadm -m session` lists now ("" when no session, or it can't say).
fn sessions() -> String {
    common::command(&["iscsiadm", "-m", "session"], 15)
        .map(|(_, out)| out)
        .unwrap_or_default()
}
/// The targets in the settings (network_storage.targets) with no session in `listed`.
fn missing_sessions<'a>(cfg: &'a Value, listed: &str) -> Vec<&'a str> {
    cfg["network_storage"]["targets"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .filter(|iqn| !listed.split_whitespace().any(|word| word == *iqn))
        .collect()
}
/// The NAS answers at `portal` (host:port, the settings' network_storage.portal) within 3 s.
fn answers(portal: &str) -> bool {
    use std::net::{TcpStream, ToSocketAddrs};
    let Some(addresses) = portal.to_socket_addrs().ok() else {
        return false;
    };
    addresses
        .take(4)
        .any(|a| TcpStream::connect_timeout(&a, Duration::from_secs(3)).is_ok())
}
/// For drive-return, before it starts the apps: when a NAS drive fails writes now (it
/// turned read-only just before, and this check hasn't seen it yet), the apps are held,
/// as this check would hold them, and true is returned. This check starts them once every
/// NAS drive takes writes. A drive only waiting for the NAS is no reason: its writes wait.
pub(crate) fn hold_if_failing(cfg: &Value) -> Result<bool> {
    let drives = nas_drives(cfg)?;
    let failing: Vec<String> = drives
        .iter()
        .filter(|(path, uuid, _)| health(path, uuid) == Health::Fails)
        .map(|(path, _, name)| format!("{name} ({})", path.display()))
        .collect();
    if failing.is_empty() {
        return Ok(false);
    }
    let _turn = state_lock()?;
    let mut state = common::json(Path::new(*STATE), 64000);
    if !state.is_object() {
        state = json!({});
    }
    state["held"] = json!(true);
    state["since"] = json!(common::now());
    state["stopped_for"] = json!(failing);
    save(&state)?;
    Ok(true)
}
/// True when this check stopped the apps for a NAS drive that does not take writes: they
/// stay stopped until it does (it starts them itself then), whoever else would start them.
pub(crate) fn held() -> bool {
    held_in(Path::new(*STATE))
}
fn held_in(state: &Path) -> bool {
    common::json(state, 64000)["held"] == true
}
/// Root's alone (the dashboard's collector and the recovery menu run as root).
fn save(record: &Value) -> Result<()> {
    common::atomic_json(Path::new(*STATE), record)
}

#[cfg(test)]
mod tests {
    use super::*;
    /// The review of 0.47.0: the start guard refuses while held, so the hold is lifted (and
    /// saved) before the apps are started, and put back when Docker did not start.
    #[test]
    fn the_hold_is_lifted_before_the_apps_start_and_put_back_if_they_did_not() {
        let source = include_str!("nas_watch.rs");
        let resume = &source[source.find("Act::Resume => {").unwrap()..];
        let lifted = resume
            .find("record[\"held\"] = json!(false);\n            save(&record)?;")
            .unwrap();
        let started = resume.find("drive_return::start_again(").unwrap();
        let put_back = resume.find("return save(&held);").unwrap();
        assert!(lifted < started && started < put_back);
    }
    #[test]
    fn the_apps_stop_once_on_a_failing_drive_and_start_only_when_all_take_writes() {
        use Health::*;
        assert_eq!(decide(&[Writes], false), Act::Nothing);
        assert_eq!(decide(&[Fails], false), Act::Hold);
        // Held: the writers are made sure of (stopped again if one runs), not left alone.
        assert_eq!(decide(&[Fails], true), Act::StillHeld);
        assert_eq!(decide(&[Writes, Fails], true), Act::StillHeld);
        // Still away: the writes wait, nothing is done either way.
        assert_eq!(decide(&[Waits], false), Act::Nothing);
        // The 0.46.0 review: held, with a drive gone or waiting, a writer started again
        // (the SSD plugged in again, drive-return starting the apps) was left running.
        assert_eq!(decide(&[Waits, Writes], true), Act::StillHeld);
        assert_eq!(decide(&[Absent], true), Act::StillHeld);
        assert_eq!(decide(&[Writes, Writes], true), Act::Resume);
        // 0.46.1: a drive not connected at all (the NAS was away at start) stops nothing
        // and starts nothing: drive-return does that when the disk appears.
        assert_eq!(decide(&[Absent], false), Act::Nothing);
        assert_eq!(decide(&[Absent, Fails], false), Act::Nothing);
        assert_eq!(decide(&[Absent, Writes], false), Act::Nothing);
    }
    #[test]
    fn a_hold_is_read_from_the_checks_record() {
        let dir = std::env::temp_dir().join(format!("neko-held-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let state = dir.join("state.json");
        assert!(!held_in(&state)); // never ran
        std::fs::write(&state, r#"{"held": true}"#).unwrap();
        assert!(held_in(&state));
        std::fs::write(&state, r#"{"held": false}"#).unwrap();
        assert!(!held_in(&state));
        let _ = std::fs::remove_dir_all(dir);
    }
    #[test]
    fn a_target_without_a_session_is_noticed() {
        let cfg = json!({"network_storage": {"targets": ["iqn.x:media", "iqn.x:backup"]}});
        let both = "tcp: [1] 10.0.0.2:3260,1 iqn.x:backup (non-flash)\ntcp: [2] 10.0.0.2:3260,1 iqn.x:media (non-flash)\n";
        assert!(missing_sessions(&cfg, both).is_empty());
        // The 0.46.1 audit: the backup drive's target gone while the media drive is fine.
        let media_only = "tcp: [2] 10.0.0.2:3260,1 iqn.x:media (non-flash)\n";
        assert_eq!(missing_sessions(&cfg, media_only), ["iqn.x:backup"]);
        assert_eq!(missing_sessions(&cfg, "").len(), 2);
        assert!(missing_sessions(&json!({}), "").is_empty());
    }
    /// The 0.46.0 review, H06: a refused stop was recorded as done.
    #[test]
    fn a_refused_stop_is_never_taken_for_a_stop() {
        let asked = std::cell::RefCell::new(vec![]);
        let asked = &asked;
        let answer = |code: i32| {
            move |args: &[&str]| {
                asked.borrow_mut().push(args.join(" "));
                Ok((code, String::new()))
            }
        };
        assert!(!stop_writers_with(&answer(1)).unwrap());
        assert!(stop_writers_with(&answer(0)).unwrap());
        // 5: one of them is not installed here (smb on Debian): the rest stopped.
        assert!(stop_writers_with(&answer(5)).unwrap());
        assert!(
            asked.borrow()[0].starts_with("systemctl stop --no-block docker.socket docker.service")
        );
    }
    #[test]
    fn a_test_write_leaves_nothing_and_refuses_a_link() {
        let root = std::env::temp_dir().join(format!("neko-nas-probe-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        write_test(&root).unwrap();
        assert_eq!(fs::read_dir(root.join(*PROBE_DIR)).unwrap().count(), 0);
        fs::remove_dir(root.join(*PROBE_DIR)).unwrap();
        std::os::unix::fs::symlink("/tmp", root.join(*PROBE_DIR)).unwrap();
        assert!(write_test(&root).is_err());
        // Not mounted at its folder, and no such disk connected: absent (never a write
        // onto the system disk).
        assert_eq!(health(&root, "not-this-uuid"), Health::Absent);
        assert!(!answers("not a portal"));
        assert!(!answers(""));
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
