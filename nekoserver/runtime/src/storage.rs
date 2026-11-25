//! The Drives page (1.0.1), on the control service's side (control.rs; the PIN is already
//! checked when these are called, and re-typed for the ops that erase or delete). The
//! contract is plans/1.0.1-contract.md; the names, paths and words are shared.json's
//! "storage_jobs", which storage_jobs.py reads too.
//!
//! As the Media folders page (media_folders.rs): the page never changes anything itself
//! and never reads a disk. A request is checked field by field, written as request.json
//! in this service's own folder, and nekoserver-storage.service started, which runs
//! storage_jobs.py as root. One job at a time; a job cut off by a power cut (its journal
//! still there) is finished or put back at the next start before anything else is taken.
use crate::common::{self, SHARED};
use crate::media_folders::Runner;
use serde_json::{json, Value};
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::Duration,
};

fn facts() -> &'static Value {
    &SHARED["storage_jobs"]
}
fn fact(key: &str) -> &'static str {
    facts()[key].as_str().unwrap_or("")
}
fn listed(key: &str) -> Vec<&'static str> {
    facts()[key]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .collect()
}

/// A request younger than this whose service has not started yet is still on its way.
const ON_ITS_WAY: Duration = Duration::from_secs(60);
const NOT_STARTED: &str = "The change could not be started, so nothing was changed. Try again; if it keeps failing, restart the server.";
const STOPPED: &str = "The change stopped before it had finished, and nothing more is changed. Press Look again to see your drives as they are now.";
const UNFINISHED: &str = "A change to your drives did not finish (the server stopped during it, or a drive it needs is away). Restart the server, or use Try again or Give it up on this page: the change is finished, or put back as it was, before anything else.";
const NOTHING_WAITS: &str = "No change is waiting to be finished.";
const ASK_DISK: &str = "Choose the drive again from the list (press Look again).";

/// The page's ops (serve.rs names them) and the job each one starts.
fn job_of(op: &str) -> Option<&'static str> {
    Some(match op {
        "app_storage_look" => "look",
        "app_drive_use" => "use",
        "app_drive_release" => "release",
        "app_data_move" => "move",
        "app_data_move_back" => "back",
        "app_data_delete_old" => "delete_old",
        "app_mirror_add" => "mirror_add",
        // A change that could not be finished: tried again, or given up (T1-20).
        "app_data_retry" => "retry",
        "app_data_give_up" => "give_up",
        _ => return None,
    })
}
/// The ops that erase a drive or delete a copy: the PIN is typed again for them.
pub fn needs_pin(op: &str, request: &Value) -> bool {
    match op {
        "app_drive_use" => request["how"] != "take_over",
        "app_data_delete_old" | "app_mirror_add" | "app_data_retry" | "app_data_give_up" => true,
        _ => false,
    }
}

/// A disk's stable name as look.json gives it: never /dev/sdX, never a path.
fn by_id(value: &Value) -> Option<&str> {
    value.as_str().filter(|v| {
        (1..=200).contains(&v.len())
            && v.bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b))
            && crate::drives::BY_ID_KINDS
                .iter()
                .any(|kind| v.len() > kind.len() && v.starts_with(kind))
    })
}
/// Printable words of 1 to `most` characters (a serial, a typed confirm).
fn words(value: &Value, most: usize) -> Option<&str> {
    value.as_str().filter(|v| {
        !v.trim().is_empty() && v.chars().count() <= most && !v.chars().any(char::is_control)
    })
}
/// A place for data: the two role drives or an extra drive.
fn place(value: &Value) -> Option<&str> {
    value
        .as_str()
        .filter(|v| ["ssd", "media"].contains(v) || crate::drives::extra(v))
}
fn hex16(value: &Value) -> Option<&str> {
    value.as_str().filter(|v| {
        v.len() == 16
            && v.bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    })
}

/// The request as storage_jobs.py takes it: only the fields the job needs, each one
/// checked; Err(why) in the person's words. The PIN is never written.
pub fn checked(op: &str, request: &Value) -> std::result::Result<Value, &'static str> {
    let job = job_of(op).ok_or("That is not a change this page makes.")?;
    let mut asked = json!({"job": job});
    let disk = |asked: &mut Value| -> std::result::Result<(), &'static str> {
        let id = by_id(&request["disk"]).ok_or(ASK_DISK)?;
        let serial = words(&request["serial"], 100).ok_or(ASK_DISK)?;
        let size = request["size_bytes"]
            .as_u64()
            .filter(|s| *s > 0)
            .ok_or(ASK_DISK)?;
        asked["disk"] = json!({"by_id": id, "serial": serial, "size_bytes": size});
        Ok(())
    };
    let confirm = |asked: &mut Value| -> std::result::Result<(), &'static str> {
        asked["confirm"] =
            json!(words(&request["confirm"], 100).ok_or("Type the words shown, to confirm.")?);
        Ok(())
    };
    let kind = |asked: &mut Value| -> std::result::Result<(), &'static str> {
        let kind = request["kind"]
            .as_str()
            .filter(|k| listed("kinds").contains(k))
            .ok_or("Which of your data?")?;
        asked["kind"] = json!(kind);
        Ok(())
    };
    let copy = |asked: &mut Value| -> std::result::Result<(), &'static str> {
        asked["copy"] = json!(hex16(&request["copy"]).ok_or("Which old copy?")?);
        Ok(())
    };
    match job {
        "look" | "retry" | "give_up" => {}
        "use" => {
            disk(&mut asked)?;
            let how = request["how"]
                .as_str()
                .filter(|h| ["take_over", "erase"].contains(h))
                .ok_or("Use the drive as it is, or erase it?")?;
            asked["how"] = json!(how);
            if how == "erase" {
                confirm(&mut asked)?;
            }
        }
        "release" => {
            let drive = request["drive"]
                .as_str()
                .filter(|d| crate::drives::extra(d))
                .ok_or("Which extra drive?")?;
            asked["drive"] = json!(drive);
        }
        "move" => {
            kind(&mut asked)?;
            asked["to"] = json!(place(&request["to"]).ok_or("Move it to which drive?")?);
            // The tick for a move that backs a kind up on its own drive (storage_jobs.py
            // beside_backups): passed on only when it is true.
            if request["beside_ok"] == true {
                asked["beside_ok"] = json!(true);
            }
        }
        "back" => {
            kind(&mut asked)?;
            copy(&mut asked)?;
        }
        "delete_old" => {
            kind(&mut asked)?;
            copy(&mut asked)?;
            confirm(&mut asked)?;
        }
        _ => {
            let array = request["array"]
                .as_str()
                .filter(|a| {
                    let name = a.strip_prefix("md-").or_else(|| a.strip_prefix("btrfs-"));
                    name.is_some_and(|n| {
                        (1..=64).contains(&n.len())
                            && n.bytes()
                                .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
                    })
                })
                .ok_or("Which mirror?")?;
            asked["array"] = json!(array);
            disk(&mut asked)?;
            confirm(&mut asked)?;
        }
    }
    Ok(asked)
}

fn refuse(code: &str, message: &str) -> Value {
    json!({"ok": false, "error": code, "message": message})
}

/// The page's job folder and the state folder, and how it starts the service. One lock:
/// two presses at once never both start a job.
pub struct Storage {
    folder: PathBuf,
    state: PathBuf,
    run: Runner,
    one: Mutex<()>,
}
impl Default for Storage {
    fn default() -> Self {
        Storage::new(
            PathBuf::from(fact("folder")),
            PathBuf::from(fact("state")),
            Arc::new(|a| common::probe(a, 30)),
        )
    }
}
impl Storage {
    pub fn new(folder: PathBuf, state: PathBuf, run: Runner) -> Self {
        Storage {
            folder,
            state,
            run,
            one: Mutex::new(()),
        }
    }
    fn running(&self) -> bool {
        let (_, said) = (self.run)(&["systemctl", "is-active", fact("unit")]);
        matches!(
            said.trim(),
            "active" | "activating" | "reloading" | "deactivating"
        )
    }
    fn waiting(&self) -> bool {
        fs::symlink_metadata(self.folder.join(fact("request")))
            .ok()
            .and_then(|m| m.modified().ok())
            .is_some_and(|t| t.elapsed().unwrap_or_default() < ON_ITS_WAY)
    }
    fn unfinished(&self) -> bool {
        fs::symlink_metadata(self.state.join(fact("journal"))).is_ok()
    }
    fn read(&self, name: &str, limit: usize) -> Value {
        let found = common::json(&self.state.join(fact(name)), limit);
        if found["format"] == fact("format") {
            found
        } else {
            Value::Null
        }
    }
    /// The last look at the disks (look.json), without its format; null before the first.
    pub fn look(&self) -> Value {
        let mut look = self.read("look", 4_000_000);
        if let Some(o) = look.as_object_mut() {
            o.remove("format");
        }
        look
    }

    /// /api/status "storage_job": whether a job is at work, how far, and how the last one
    /// ended.
    pub fn job(&self) -> Value {
        let working = self.running() || self.waiting();
        let unfinished = self.unfinished();
        let mut result = self.read("result", 200_000);
        if !working && !unfinished && result["state"] == "working" {
            result["state"] = json!("failed");
            result["message"] = json!(STOPPED);
            result["advice"] = Value::Null;
        }
        if let Some(o) = result.as_object_mut() {
            o.remove("format");
        }
        let found = common::json(&self.folder.join(fact("progress")), 100_000);
        let ours = working && found["format"] == fact("format") && found["id"] == result["id"];
        let count = |key: &str| found[key].as_u64();
        let progress = ours.then(|| {
            json!({"bytes_done": count("bytes_done"), "bytes_total": count("bytes_total"),
                "files_done": count("files_done"), "files_total": count("files_total"),
                "percent": found["percent"].as_f64().filter(|p| (0.0..=100.0).contains(p))})
        });
        let said = |key: &str| {
            if ours {
                found[key].clone()
            } else {
                Value::Null
            }
        };
        json!({
            "working": working,
            "job": if working { result["job"].clone() } else { Value::Null },
            "id": if working { result["id"].clone() } else { Value::Null },
            "step": said("step"),
            "step_words": said("step_words"),
            "progress": progress,
            "result": result,
            "unfinished": unfinished,
        })
    }
    /// `storage-state`: the last look and the job.
    pub fn state(&self, cfg: &Value) -> Value {
        let _one = self
            .one
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let look = self.look();
        json!({"ok": true, "looked_at": look["at"], "look": look, "storage_job": self.job_with(cfg)})
    }
    /// The job as /api/status gives it, with `stuck`.
    pub fn job_with(&self, cfg: &Value) -> Value {
        let mut job = self.job();
        job["stuck"] = self.stuck(cfg);
        job
    }
    /// `stuck`: the job a journal waits with, and the drives it needs (null without one).
    pub fn stuck(&self, cfg: &Value) -> Value {
        if !self.unfinished() {
            return Value::Null;
        }
        let journal = self.read("journal", 1_000_000);
        let table = fs::read_to_string("/proc/self/mountinfo").unwrap_or_default();
        stuck_in(cfg, &journal, &table, Path::new("/dev/disk/by-uuid"))
    }

    /// A job from the page: checked, written for storage_jobs.py, and its service started.
    pub fn start(&self, op: &str, request: &Value) -> Value {
        let asked = match checked(op, request) {
            Ok(asked) => asked,
            Err(why) => return refuse("E42", why),
        };
        let _one = self
            .one
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        // "Try again" and "Give it up" are for the unfinished change only; every other job
        // waits for it (T1-20: never a lock for good).
        let way_on = matches!(asked["job"].as_str(), Some("retry" | "give_up"));
        if self.unfinished() != way_on {
            return refuse("E47", if way_on { NOTHING_WAITS } else { UNFINISHED });
        }
        if self.running() || self.waiting() {
            return refuse("E47", fact("busy"));
        }
        let Ok(id) = crate::control::random_hex(8) else {
            return refuse("E46", NOT_STARTED);
        };
        let mut asked = asked;
        asked["format"] = json!(fact("format"));
        asked["id"] = json!(id);
        asked["asked_at"] = json!(common::now());
        let written = fs::create_dir_all(&self.folder)
            .and_then(|()| fs::set_permissions(&self.folder, fs::Permissions::from_mode(0o700)))
            .map_err(common::Error::from)
            .and_then(|()| common::atomic_json(&self.folder.join(fact("request")), &asked));
        if written.is_err() {
            return refuse("E46", NOT_STARTED);
        }
        match (self.run)(&["systemctl", "start", "--no-block", fact("unit")]) {
            (0, _) => {
                json!({"ok": true, "id": id, "message": "Working on it. This page shows how it ends."})
            }
            _ => {
                let _ = fs::remove_file(self.folder.join(fact("request")));
                refuse("E46", NOT_STARTED)
            }
        }
    }
}

/// One of this page's ops (the web server passes their fields on).
/// A cut-off job, as the page shows it: the job, its kind, whether it is past its commit
/// point, and the drives it needs, each with why it is away (null when it is there), as
/// storage_jobs.py cut_places and neko_common.drive_away say it.
pub(crate) fn stuck_in(cfg: &Value, journal: &Value, table: &str, by_uuid: &Path) -> Value {
    let undo = &journal["undo"];
    let named = |key: &str| undo[key].as_str().map(str::to_string);
    let needs: Vec<String> = match journal["job"].as_str() {
        Some("delete_old") => named("deleting")
            .and_then(|d| place_holding(cfg, Path::new(&d).parent()?))
            .into_iter()
            .collect(),
        Some("move" | "back") if undo["here_away"] == true => named("to").into_iter().collect(),
        Some("move" | "back") => [named("here"), named("to")].into_iter().flatten().collect(),
        _ => Vec::new(),
    };
    let needs: Vec<Value> = needs
        .iter()
        .map(|place| {
            json!({"place": place, "words": crate::drives::drive_words(place),
                "away": away_in(cfg, place, table, by_uuid)})
        })
        .collect();
    json!({"job": journal["job"], "kind": journal["request"]["kind"],
        "committed": journal["committed"] == true, "needs": needs})
}
/// Why the drive `place` is away: "missing" (nothing mounted at its folder, or no such
/// drive), "other" (another filesystem there), None when it is there.
fn away_in(cfg: &Value, place: &str, table: &str, by_uuid: &Path) -> Option<&'static str> {
    let Some(root) = common::drive_folder(cfg, place) else {
        return Some("missing");
    };
    let key = match place {
        "ssd" => "data_ssd",
        "media" => "media_hdd",
        name => name,
    };
    let Some(uuid) = cfg["storage"][key]["uuid"]
        .as_str()
        .filter(|u| !u.is_empty())
    else {
        // A layout that names no filesystem for it needs its folder.
        return (!root.is_dir()).then_some("missing");
    };
    let Some(entry) = common::mount_table_entry_in(table, &root, by_uuid) else {
        return Some("missing");
    };
    let now = entry.split(' ').next().unwrap_or("");
    (!now.is_empty() && now != uuid).then_some("other")
}
/// The drive whose folder holds `path`, the deepest one.
fn place_holding(cfg: &Value, path: &Path) -> Option<String> {
    let extras = cfg["storage"]
        .as_object()
        .into_iter()
        .flatten()
        .map(|(k, _)| k.as_str());
    ["ssd", "media"]
        .into_iter()
        .chain(extras.filter(|k| common::extra_drive(cfg, k)))
        .filter_map(|place| Some((place, common::drive_folder(cfg, place)?)))
        .filter(|(_, root)| path.starts_with(root))
        .max_by_key(|(_, root)| root.components().count())
        .map(|(place, _)| place.to_string())
}

pub fn is_op(op: &str) -> bool {
    op == "app_storage_state" || job_of(op).is_some()
}
/// The ops of this page, as the control service answers them.
pub fn answer(storage: &Storage, op: &str, request: &Value, cfg: &Value) -> Option<Value> {
    if op == "app_storage_state" {
        return Some(storage.state(cfg));
    }
    job_of(op)?;
    Some(storage.start(op, request))
}

/// A kind's name on the page ("Photos", "Films and shows").
fn kind_name(kind: &str) -> String {
    let said = facts()["kind_words"][kind].as_str().unwrap_or(kind);
    let said = said.strip_prefix("your ").unwrap_or(said);
    let mut chars = said.chars();
    chars
        .next()
        .map(|c| c.to_uppercase().collect::<String>() + chars.as_str())
        .unwrap_or_default()
}

/// /api/status "data_kinds": where each kind of data is (the settings), and from the last
/// look its size, its old copy and the drives it may move to.
pub fn data_kinds(cfg: &Value, look: &Value) -> Vec<Value> {
    listed("kinds")
        .into_iter()
        .map(|kind| {
            let fallback = if kind == "backups" {
                "media"
            } else {
                SHARED["locations"][kind]["default"]
                    .as_str()
                    .unwrap_or("media")
            };
            let place = cfg["locations"][kind]
                .as_str()
                .filter(|p| place(&json!(p)).is_some())
                .unwrap_or(fallback);
            let old = look["old_copies"]
                .as_array()
                .into_iter()
                .flatten()
                .filter(|c| c["kind"] == kind)
                .max_by(|a, b| {
                    a["at"]
                        .as_f64()
                        .unwrap_or(0.0)
                        .total_cmp(&b["at"].as_f64().unwrap_or(0.0))
                })
                .cloned();
            let size = &look["sizes"][kind];
            json!({
                "kind": kind,
                "words": kind_name(kind),
                "place": place,
                "place_words": crate::drives::drive_words(place),
                "bytes": size["bytes"].as_u64(),
                "measured_at": size["at"].as_f64(),
                "old_copy": old,
                "targets": look["targets"][kind].as_array().cloned().unwrap_or_default(),
                // Backed up on the drive it is on (the last look; the backups page's "same drive").
                "same_drive": look["same_drive"].as_array().is_some_and(|k| k.iter().any(|v| v.as_str() == Some(kind))),
            })
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};

    /// The collector's settings as setup writes them for a server with photos, music and
    /// Nextcloud's files on the media drive and no third drive (tests/golden, made by
    /// dashboard.collector_settings): each kind is shown where it is (T1-1).
    #[test]
    fn each_kind_is_where_the_settings_setup_writes_say() {
        let cfg: Value = serde_json::from_str(include_str!(
            "../../tests/golden/media-drive/dashboard-config.json"
        ))
        .unwrap();
        let kinds = data_kinds(&cfg, &json!({}));
        let place = |kind: &str| kinds.iter().find(|k| k["kind"] == kind).unwrap()["place"].clone();
        for kind in ["photos", "music", "cloud"] {
            assert_eq!(place(kind), "media", "{kind}");
        }
        assert_eq!(place("movies"), "media");
    }

    struct Systemd {
        active: AtomicBool,
        asked: Mutex<Vec<String>>,
    }
    fn storage(name: &str) -> (Storage, Arc<Systemd>, PathBuf) {
        let root = std::env::temp_dir().join(format!("neko-storage-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("state")).unwrap();
        let systemd = Arc::new(Systemd {
            active: AtomicBool::new(false),
            asked: Mutex::new(Vec::new()),
        });
        let seen = systemd.clone();
        let run: Runner = Arc::new(move |args: &[&str]| {
            seen.asked.lock().unwrap().push(args.join(" "));
            match args.get(1) {
                Some(&"is-active") if seen.active.load(Ordering::SeqCst) => (0, "active\n".into()),
                Some(&"is-active") => (3, "inactive\n".into()),
                _ => (0, String::new()),
            }
        });
        (
            Storage::new(root.join("run"), root.join("state"), run),
            systemd,
            root,
        )
    }
    fn erase() -> Value {
        json!({"disk": "ata-WDC_WD40EFRX_WD-WCC4E1234567", "serial": "WD-WCC4E1234567",
            "size_bytes": 4_000_787_030_016u64, "how": "erase", "confirm": "4 TB 4567", "pin": "123456"})
    }

    /// Contract §3: each field checked, only the ones the job needs passed on, the PIN never.
    #[test]
    fn every_field_is_checked_and_only_the_needed_ones_go_on() {
        assert_eq!(
            checked("app_drive_use", &erase()).unwrap(),
            json!({"job": "use", "disk": {"by_id": "ata-WDC_WD40EFRX_WD-WCC4E1234567",
                "serial": "WD-WCC4E1234567", "size_bytes": 4_000_787_030_016u64},
                "how": "erase", "confirm": "4 TB 4567"})
        );
        let mut take = erase();
        take["how"] = json!("take_over");
        take.as_object_mut().unwrap().remove("confirm");
        assert!(
            checked("app_drive_use", &take).is_ok(),
            "taking over needs no confirm"
        );
        assert!(!needs_pin("app_drive_use", &take));
        assert!(needs_pin("app_drive_use", &erase()));
        assert!(needs_pin("app_data_delete_old", &json!({})));
        assert!(needs_pin("app_mirror_add", &json!({})));
        // Try again and Give it up (T1-18): the PIN again, nothing else asked.
        for op in ["app_data_retry", "app_data_give_up"] {
            assert!(needs_pin(op, &json!({})), "{op}");
            let asked = checked(op, &json!({"kind": "photos", "to": "d3", "pin": "1"})).unwrap();
            assert_eq!(
                asked["job"],
                if op.ends_with("retry") {
                    "retry"
                } else {
                    "give_up"
                }
            );
            assert_eq!(asked.as_object().unwrap().len(), 1, "{asked}");
        }
        assert!(!needs_pin("app_data_move", &json!({})));
        for (key, bad) in [
            ("disk", json!("sdc")),
            ("disk", json!("/dev/sdc")),
            ("disk", json!("../sda")),
            ("disk", json!("ata-x/../../sda")),
            ("disk", json!(null)),
            ("serial", json!("")),
            ("serial", json!("a\nb")),
            ("size_bytes", json!(0)),
            ("size_bytes", json!("4000")),
            ("how", json!("format")),
            ("confirm", json!(null)),
        ] {
            let mut wrong = erase();
            wrong[key] = bad.clone();
            assert!(checked("app_drive_use", &wrong).is_err(), "{key}: {bad}");
        }
        // Never /dev/sdX: a request naming only a device is refused.
        assert!(checked("app_drive_use", &json!({"dev": "sdc", "how": "take_over"})).is_err());
        assert_eq!(
            checked(
                "app_data_move",
                &json!({"kind": "photos", "to": "d3", "x": 1})
            )
            .unwrap(),
            json!({"job": "move", "kind": "photos", "to": "d3"})
        );
        // The tick for a move that backs a kind up on its own drive goes on; only true.
        assert_eq!(
            checked(
                "app_data_move",
                &json!({"kind": "photos", "to": "d3", "beside_ok": true})
            )
            .unwrap(),
            json!({"job": "move", "kind": "photos", "to": "d3", "beside_ok": true})
        );
        assert_eq!(
            checked(
                "app_data_move",
                &json!({"kind": "photos", "to": "d3", "beside_ok": "yes"})
            )
            .unwrap(),
            json!({"job": "move", "kind": "photos", "to": "d3"})
        );
        for to in ["d2", "d100", "d03", "/mnt/x", "hdd"] {
            assert!(
                checked("app_data_move", &json!({"kind": "photos", "to": to})).is_err(),
                "{to}"
            );
        }
        assert!(checked("app_data_move", &json!({"kind": "tv", "to": "d3"})).is_err());
        assert!(checked("app_drive_release", &json!({"drive": "media"})).is_err());
        assert_eq!(
            checked(
                "app_drive_release",
                &json!({"drive": "d3", "kind": "photos"})
            )
            .unwrap(),
            json!({"job": "release", "drive": "d3"})
        );
        // A kernel name of any length is no stable name, nor one of a known kind only.
        for disk in ["sdcdefghijk", "md127abcdef", "ata-"] {
            let mut wrong = erase();
            wrong["disk"] = json!(disk);
            assert!(checked("app_drive_use", &wrong).is_err(), "{disk}");
        }
        let copy = json!({"kind": "music", "copy": "0123456789abcdef", "confirm": "delete music"});
        assert!(checked("app_data_delete_old", &copy).is_ok());
        assert_eq!(
            checked("app_data_move_back", &copy).unwrap(),
            json!({"job": "back", "kind": "music", "copy": "0123456789abcdef"})
        );
        // An old copy's id is 16 lower-case hex digits, as storage_jobs.py writes it.
        for id in ["0123456789ABCDEF", "0123456789abcdeg", "0123456789abcde/"] {
            let mut wrong = copy.clone();
            wrong["copy"] = json!(id);
            assert!(checked("app_data_move_back", &wrong).is_err(), "{id}");
        }
        let mut short = copy.clone();
        short["copy"] = json!("0123");
        assert!(checked("app_data_move_back", &short).is_err());
        let mirror = |array: &str| {
            let mut m = erase();
            m["array"] = json!(array);
            checked("app_mirror_add", &m)
        };
        assert_eq!(mirror("md-md0").unwrap()["array"], "md-md0");
        assert!(mirror("btrfs-1b2c3d4e-aaaa").is_ok());
        assert!(mirror("md0").is_err() && mirror("md-../x").is_err());
        assert!(checked("app_format", &json!({})).is_err());
    }

    /// A job is written for the root tool and its unit started; one at a time, and none
    /// while a cut-off job's journal waits for the next start.
    #[test]
    fn one_job_at_a_time_and_never_past_an_unfinished_one() {
        let (s, systemd, root) = storage("one");
        let said = s.start("app_data_move", &json!({"kind": "photos", "to": "d3"}));
        assert_eq!(said["ok"], true, "{said}");
        let request = common::json(&root.join("run/request.json"), 10_000);
        assert_eq!(request["format"], "neko-storage-v1");
        assert_eq!(request["job"], "move");
        assert_eq!(request["id"], said["id"]);
        assert!(systemd
            .asked
            .lock()
            .unwrap()
            .contains(&"systemctl start --no-block nekoserver-storage.service".to_string()));
        // The request still waits to be taken: busy.
        let again = s.start("app_storage_look", &json!({}));
        assert_eq!(again["error"], "E47");
        fs::remove_file(root.join("run/request.json")).unwrap();
        systemd.active.store(true, Ordering::SeqCst);
        assert_eq!(s.start("app_storage_look", &json!({}))["error"], "E47");
        systemd.active.store(false, Ordering::SeqCst);
        fs::write(root.join("state/journal.json"), "{}").unwrap();
        let cut = s.start("app_storage_look", &json!({}));
        assert_eq!(cut["error"], "E47");
        let said = cut["message"].as_str().unwrap();
        assert!(
            said.contains("Try again") && said.contains("Give it up"),
            "{said}"
        );
        assert!(s.job()["unfinished"] == true);
        // T1-20: "Try again" and "Give it up" are the way on past it, never a lock for good.
        assert_eq!(
            checked("app_data_retry", &json!({"x": 1})).unwrap(),
            json!({"job": "retry"})
        );
        assert_eq!(
            checked("app_data_give_up", &json!({})).unwrap(),
            json!({"job": "give_up"})
        );
        assert_eq!(s.start("app_data_retry", &json!({}))["ok"], true);
        fs::remove_file(root.join("run/request.json")).unwrap();
        assert_eq!(s.start("app_data_give_up", &json!({}))["ok"], true);
        fs::remove_file(root.join("run/request.json")).unwrap();
        fs::remove_file(root.join("state/journal.json")).unwrap();
        assert_eq!(
            s.start("app_data_retry", &json!({}))["error"],
            "E47",
            "nothing to try again"
        );
        assert_eq!(s.start("app_storage_look", &json!({}))["ok"], true);
        let _ = fs::remove_dir_all(&root);
    }

    /// The stuck job (T1-18) as /api/status gives it: the drives it needs, and which is
    /// away and why (nothing mounted there, another filesystem there, or there).
    #[test]
    fn a_stuck_job_names_the_drives_it_needs_and_the_one_that_is_away() {
        let root = std::env::temp_dir().join(format!("neko-stuck-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let by_uuid = root.join("by-uuid");
        fs::create_dir_all(&by_uuid).unwrap();
        let (one, two) = (root.join("sdx1"), root.join("sdy1"));
        fs::write(&one, b"").unwrap();
        fs::write(&two, b"").unwrap();
        std::os::unix::fs::symlink(&one, by_uuid.join("aaaa")).unwrap();
        std::os::unix::fs::symlink(&two, by_uuid.join("bbbb")).unwrap();
        let cfg = json!({"data_mount": "/srv/nekoserver", "media_mount": "/mnt/nekoserver-media",
            "storage": {"media_hdd": {"uuid": "aaaa"}, "d3": {"uuid": "cccc"}, "d4": {"uuid": "dddd"}}});
        let (one, two) = (one.display(), two.display());
        // The media drive there; d3 holds another disk; d4 is not mounted.
        let table = format!(
            "36 30 8:1 / /mnt/nekoserver-media rw - ext4 {one} rw\n\
             37 30 8:2 / /mnt/nekoserver-drives/d3 rw - ext4 {two} rw\n"
        );
        let journal = json!({"job": "move", "committed": true, "request": {"kind": "photos"},
            "undo": {"here": "media", "to": "d3"}});
        let stuck = stuck_in(&cfg, &journal, &table, &by_uuid);
        assert_eq!(
            stuck,
            json!({"job": "move", "kind": "photos", "committed": true, "needs": [
                {"place": "media", "words": "Media and backups drive", "away": null},
                {"place": "d3", "words": "Extra drive d3", "away": "other"}]})
        );
        // Leaving a lost drive: only the one it went to.
        let left = json!({"job": "back", "undo": {"here": "d4", "to": "media", "here_away": true}});
        assert_eq!(
            stuck_in(&cfg, &left, &table, &by_uuid)["needs"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        // A delete: the drive that holds the old copy, the deepest.
        let delete = json!({"job": "delete_old", "committed": true,
            "undo": {"deleting": "/mnt/nekoserver-drives/d4/nekoserver-deleting-x"}});
        let needs = &stuck_in(&cfg, &delete, &table, &by_uuid)["needs"];
        assert_eq!(
            needs,
            &json!([{"place": "d4", "words": "Extra drive d4", "away": "missing"}])
        );
        // Another job, or a journal that can't be read: nothing named.
        assert_eq!(
            stuck_in(&cfg, &Value::Null, "", &by_uuid)["needs"],
            json!([])
        );
        let (s, _, state) = storage("stuck");
        assert_eq!(s.stuck(&cfg), Value::Null, "no journal, nothing stuck");
        fs::write(state.join("state/journal.json"), "{}").unwrap();
        assert_eq!(s.job_with(&cfg)["stuck"]["needs"], json!([]));
        let _ = fs::remove_dir_all(&state);
        let _ = fs::remove_dir_all(&root);
    }

    /// storage-state: null before the first look; a result left "working" by a run that
    /// ended is said as stopped; progress only for the job at work.
    #[test]
    fn the_state_says_how_the_last_job_ended() {
        let (s, systemd, root) = storage("state");
        let state = s.state(&json!({}));
        assert_eq!(state["looked_at"], Value::Null);
        assert_eq!(state["storage_job"]["working"], false);
        assert_eq!(state["storage_job"]["result"], Value::Null);
        common::atomic_json(
            &root.join("state/look.json"),
            &json!({"format": "neko-storage-v1", "at": 5.0, "disks": []}),
        )
        .unwrap();
        common::atomic_json(&root.join("state/result.json"),
            &json!({"format": "neko-storage-v1", "id": "aa", "job": "move", "state": "working", "message": "m"})).unwrap();
        fs::create_dir_all(root.join("run")).unwrap();
        common::atomic_json(&root.join("run/progress.json"),
            &json!({"format": "neko-storage-v1", "id": "aa", "step": "copy", "step_words": "Copying your photos",
                "bytes_done": 5, "bytes_total": 10, "files_done": 1, "files_total": 2, "percent": 50.0})).unwrap();
        let state = s.state(&json!({}));
        assert_eq!(state["looked_at"], 5.0);
        assert!(state["look"].get("format").is_none());
        let job = &state["storage_job"];
        assert_eq!(job["result"]["state"], "failed");
        assert_eq!(job["progress"], Value::Null, "nothing at work");
        systemd.active.store(true, Ordering::SeqCst);
        let job = s.job();
        assert_eq!(job["working"], true);
        assert_eq!(job["job"], "move");
        assert_eq!(job["result"]["state"], "working");
        assert_eq!(job["step_words"], "Copying your photos");
        assert_eq!(job["progress"]["bytes_done"], 5);
        assert_eq!(job["progress"]["percent"], 50.0);
        let _ = fs::remove_dir_all(&root);
    }

    /// data_kinds: the place from the settings (backups on the media drive unless moved),
    /// the rest from the last look.
    #[test]
    fn each_kind_says_where_it_is() {
        let cfg = json!({"locations": {"photos": "d3", "music": "nowhere"}});
        let look = json!({"sizes": {"photos": {"bytes": 7, "at": 2.0}},
            "old_copies": [{"kind": "photos", "copy": "a", "at": 1.0}, {"kind": "photos", "copy": "b", "at": 3.0}],
            "targets": {"photos": [{"to": "media", "ok": true}]}});
        let rows = data_kinds(&cfg, &look);
        let row = |k: &str| rows.iter().find(|r| r["kind"] == k).unwrap().clone();
        assert_eq!(rows.len(), 5);
        assert_eq!(row("photos")["place"], "d3");
        assert_eq!(row("photos")["place_words"], "Extra drive d3");
        assert_eq!(row("photos")["bytes"], 7);
        assert_eq!(row("photos")["old_copy"]["copy"], "b");
        assert_eq!(row("photos")["targets"][0]["to"], "media");
        assert_eq!(
            row("music")["place"],
            "ssd",
            "a wrong setting reads as the default"
        );
        assert_eq!(row("movies")["words"], "Films and shows");
        assert_eq!(row("backups")["place"], "media");
        assert_eq!(row("backups")["place_words"], "Media and backups drive");
        assert_eq!(row("cloud")["bytes"], Value::Null);
        assert_eq!(row("cloud")["targets"], json!([]));
        assert_eq!(row("photos")["same_drive"], false);
        let look = json!({"same_drive": ["photos"]});
        let rows = data_kinds(&cfg, &look);
        let same = |k: &str| rows.iter().find(|r| r["kind"] == k).unwrap()["same_drive"].clone();
        assert_eq!(
            same("photos"),
            true,
            "the look says the photos are backed up on their own drive"
        );
        assert_eq!(same("music"), false);
    }
}
