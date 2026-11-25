//! The dashboard's Media folders page, on the control service's side (control.rs; the
//! PIN is already checked when these are called).
//!
//! The page never changes anything itself, and never reads the filesystem: it asks.
//! - `state`: the last answer of media_locations.py (result.json) and whether a change
//!   is at work.
//! - `change`: a change checked field by field, written as request.json in this
//!   service's own folder, and nekoserver-media-folders.service started, which runs
//!   media_locations.py as root (the code setup uses). One change at a time.
//! - `browse`: the folders the person may pick from: the drives and shares mounted under
//!   /mnt, /media, /run/media and /srv, and the folders inside one; names only, a bounded
//!   number, never following a link, with a time limit (a share that stopped answering
//!   would hold a look at it for ever).
//!
//! Every name, path and word list comes from shared.json ("media_folders"), which
//! media_locations.py reads too.
use crate::common::{self, SHARED};
use serde_json::{json, Value};
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::Duration,
};

/// How a command is run: systemctl in the service, a stand-in in the tests.
pub type Runner = Arc<dyn Fn(&[&str]) -> (i32, String) + Send + Sync>;

fn facts() -> &'static Value {
    &SHARED["media_folders"]
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

/// Whether `name` is a slot a folder of `kind` may have: the folder's name under
/// library/existing and /data/existing ("movies", "movies-new", "movies-2" ...), as
/// existing_media.is_slot says it (shared.json media_folders.slot_suffix).
pub fn is_slot(kind: &str, name: &str) -> bool {
    let suffix = fact("slot_suffix");
    let plain = !kind.is_empty() && kind.bytes().all(|b| b.is_ascii_lowercase());
    plain && regex::Regex::new(&format!("^{kind}{suffix}$")).is_ok_and(|rule| rule.is_match(name))
}
/// Whether `name` is a slot of one of the kinds the page knows.
pub fn is_any_slot(name: &str) -> bool {
    listed("kinds").iter().any(|kind| is_slot(kind, name))
}
/// How many folders one kind may have connected (shared.json).
pub fn folders_per_kind() -> usize {
    facts()["folders_per_kind"].as_u64().unwrap_or(8) as usize
}

/// A request younger than this whose service has not started yet is still on its way;
/// an older one was never taken (the service could not start) and is replaced.
const ON_ITS_WAY: Duration = Duration::from_secs(60);
/// How long a look into a folder may take before the page is told it does not answer.
const LOOK_LIMIT: Duration = Duration::from_secs(5);

/// A change while another one is at work (media_locations.py says the same).
fn busy() -> &'static str {
    fact("busy")
}
const NOT_STARTED: &str = "The change could not be started, so nothing was changed. Try again; if it keeps failing, run setup again.";
const STOPPED: &str = "The change stopped before it had finished (the server restarted, or the change failed). Press Look again to see your folders as they are now; nothing more is changed.";
const ASK_PATH: &str =
    "Type the folder as a full path, starting with / (for example /mnt/nas/films).";
/// A drive or share that did not answer a look in time (media_locations.py says the same).
fn away() -> &'static str {
    fact("away")
}

/// The page's folder in the control service's runtime folder, and how it starts the
/// service. One lock: two presses at once never both start a change.
pub struct Folders {
    folder: PathBuf,
    run: Runner,
    one: Mutex<()>,
}
impl Default for Folders {
    fn default() -> Self {
        Folders::new(
            PathBuf::from(fact("folder")),
            Arc::new(|a| common::probe(a, 30)),
        )
    }
}
impl Folders {
    pub fn new(folder: PathBuf, run: Runner) -> Self {
        Folders {
            folder,
            run,
            one: Mutex::new(()),
        }
    }
    fn request(&self) -> PathBuf {
        self.folder.join(fact("request"))
    }
    fn result(&self) -> PathBuf {
        self.folder.join(fact("result"))
    }
    /// The service is at work (systemd says so).
    fn running(&self) -> bool {
        let (_, said) = (self.run)(&["systemctl", "is-active", fact("unit")]);
        matches!(
            said.trim(),
            "active" | "activating" | "reloading" | "deactivating"
        )
    }
    /// How old the request waiting to be taken is, if one is there.
    fn waiting(&self) -> Option<Duration> {
        let seen = fs::symlink_metadata(self.request()).ok()?;
        Some(seen.modified().ok()?.elapsed().unwrap_or_default())
    }
    fn busy(&self) -> bool {
        self.running() || self.waiting().is_some_and(|age| age < ON_ITS_WAY)
    }

    /// The last answer and whether a change is at work. An answer left "working" by a
    /// run that ended (a restart, a crash) is said as stopped.
    pub fn state(&self) -> Value {
        let _one = self
            .one
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let working = self.busy();
        let mut result = common::json(&self.result(), 2_000_000);
        if result["format"] != fact("format") {
            result = Value::Null;
        } else if !working && result["state"] == "working" {
            result["state"] = json!("failed");
            result["message"] = json!(STOPPED);
        }
        json!({"ok": true, "working": working, "result": result, "progress": self.progress(working, &result)})
    }
    /// How far the copy of the change at work is (media_locations.CopyProgress): only
    /// while it is at work, and only the one this change wrote (its request's id).
    fn progress(&self, working: bool, result: &Value) -> Value {
        let found = common::json(&self.folder.join(fact("progress")), 100_000);
        let ours = found["format"] == fact("format")
            && result["id"].is_string()
            && found["id"] == result["id"];
        let counted = ["files_done", "files_total", "bytes_done", "bytes_total"]
            .iter()
            .all(|key| found[*key].is_u64());
        if !(working && ours && counted) {
            return Value::Null;
        }
        let mut shown = json!({});
        for key in [
            "kind",
            "source",
            "files_done",
            "files_total",
            "bytes_done",
            "bytes_total",
        ] {
            shown[key] = found[key].clone();
        }
        shown
    }

    /// A change from the page: checked, written for media_locations.py, and its service
    /// started. Refused while another one is at work.
    pub fn change(&self, request: &Value) -> Value {
        let asked = match checked(request) {
            Ok(asked) => asked,
            Err(why) => return refuse("E42", why),
        };
        let _one = self
            .one
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if self.busy() {
            return refuse("E47", busy());
        }
        let Ok(id) = random_id() else {
            return refuse("E46", NOT_STARTED);
        };
        let mut asked = asked;
        asked["id"] = json!(id);
        // The folder is root's and closed (0700); the request is root's file (0600).
        let written = fs::create_dir_all(&self.folder)
            .and_then(|()| fs::set_permissions(&self.folder, fs::Permissions::from_mode(0o700)))
            .map_err(common::Error::from)
            .and_then(|()| common::atomic_json(&self.request(), &asked));
        if written.is_err() {
            return refuse("E46", NOT_STARTED);
        }
        match (self.run)(&["systemctl", "start", "--no-block", fact("unit")]) {
            (0, _) => {
                json!({"ok": true, "id": id, "message": "Working on it. This page shows how it ends."})
            }
            _ => {
                let _ = fs::remove_file(self.request());
                refuse("E47", NOT_STARTED)
            }
        }
    }
}

fn refuse(code: &str, message: &str) -> Value {
    json!({"ok": false, "error": code, "message": message})
}
fn random_id() -> std::io::Result<String> {
    use std::io::Read;
    let mut buf = [0u8; 8];
    fs::File::open("/dev/urandom")?.read_exact(&mut buf)?;
    Ok(buf.iter().map(|b| format!("{b:02x}")).collect())
}

/// An absolute path as written: no '..', '.', empty parts or trailing slash, no control
/// characters or backslashes, of a sensible length. The folder itself is checked as root
/// by existing_media.py.
pub fn plain_path(value: &str) -> bool {
    value.len() > 1
        && value.len() <= 4096
        && value.starts_with('/')
        && !value.ends_with('/')
        && !value.chars().any(|c| c.is_control() || c == '\\')
        && value[1..]
            .split('/')
            .all(|part| !part.is_empty() && part != "." && part != "..")
}

/// A drive's name as setup gives them (neko_common.DRIVE_NAME, common::extra_drive): d3, d4 ...
fn drive_name(name: &str) -> bool {
    name.len() <= 16
        && name.starts_with(|c: char| c.is_ascii_lowercase())
        && name
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
}

/// The request as media_locations.py takes it: only the fields the change needs, each
/// one of its fixed words (shared.json) or a plain path; Err(why) in the person's words.
pub fn checked(request: &Value) -> std::result::Result<Value, &'static str> {
    let one_of = |key: &str, list: &str| {
        request[key]
            .as_str()
            .filter(|v| listed(list).contains(v))
            .map(str::to_string)
    };
    let change = one_of("change", "changes").ok_or("That is not a change this page makes.")?;
    if change == "look" {
        return Ok(json!({"change": change}));
    }
    let kind = one_of("kind", "kinds").ok_or("Choose films, shows or music.")?;
    let mut asked = json!({"change": change, "kind": kind});
    match change.as_str() {
        "add" => {
            let path = request["path"]
                .as_str()
                .filter(|p| plain_path(p))
                .ok_or(ASK_PATH)?;
            let how = one_of("how", "how").ok_or("Use the folder where it is, or copy it?")?;
            let downloads = request["downloads"]
                .as_bool()
                .ok_or("Should new downloads go there?")?;
            if how == "copy" && downloads {
                return Err(
                    "A folder that is copied is not connected, so new downloads cannot go there.",
                );
            }
            asked["path"] = json!(path);
            asked["how"] = json!(how);
            asked["downloads"] = json!(downloads);
            // Setup's "Back this up too?" for music used where it is (only a NAS's is).
            let backup = &request["backup"];
            if !backup.is_null() && !backup.is_boolean() {
                return Err("Should the music backup copy it too?");
            }
            if backup.is_boolean() && kind == "music" && how == "in_place" {
                asked["backup"] = backup.clone();
            }
        }
        // A folder is named by its slot; new downloads go to one, or to "library".
        "remove" => {
            let slot = request["slot"]
                .as_str()
                .filter(|s| is_slot(&kind, s))
                .ok_or("Which folder?")?;
            asked["slot"] = json!(slot);
        }
        // "A folder on this Extra drive" (1.0.1, part C): the drive by its name (that it
        // is one of this server's Extra drives, connected, is media_locations.py's to say).
        "make" => {
            let drive = request["drive"]
                .as_str()
                .filter(|d| drive_name(d))
                .ok_or("Which Extra drive?")?;
            let downloads = request["downloads"]
                .as_bool()
                .ok_or("Should new downloads go there?")?;
            asked["drive"] = json!(drive);
            asked["downloads"] = json!(downloads);
            let backup = &request["backup"];
            if !backup.is_null() && !backup.is_boolean() {
                return Err("Should the music backup copy it too?");
            }
            if backup.is_boolean() && kind == "music" {
                asked["backup"] = backup.clone();
            }
        }
        _ => {
            let target = request["target"]
                .as_str()
                .filter(|t| *t == "library" || is_slot(&kind, t))
                .ok_or("Where should new downloads go?")?;
            asked["target"] = json!(target);
        }
    }
    // The folder the page showed under that slot: a slot is given again to a folder added
    // after one was taken away, so a page left open on another device would change the
    // new one (media_locations.shown_there refuses that).
    if ["remove", "target"].contains(&change.as_str()) && !request["path"].is_null() {
        let shown = request["path"]
            .as_str()
            .filter(|p| plain_path(p))
            .ok_or("Which folder?")?;
        asked["path"] = json!(shown);
    }
    Ok(asked)
}

/* ---------- picking a folder ---------- */

/// Filesystems that are never a drive or a share someone keeps media on.
const NOT_MEDIA: [&str; 20] = [
    "proc",
    "sysfs",
    "tmpfs",
    "devtmpfs",
    "devpts",
    "cgroup",
    "cgroup2",
    "securityfs",
    "debugfs",
    "tracefs",
    "configfs",
    "fusectl",
    "mqueue",
    "hugetlbfs",
    "pstore",
    "bpf",
    "binfmt_misc",
    "nsfs",
    "ramfs",
    "efivarfs",
];
const NETWORK: [&str; 5] = ["nfs", "nfs4", "cifs", "smb3", "fuse.sshfs"];

/// Under one of the places the page may look (or one of them).
fn under_roots(path: &str) -> bool {
    listed("browse_roots")
        .iter()
        .any(|root| path == *root || path.starts_with(&format!("{root}/")))
}
/// NekoServer's own folders (and what is inside them): in use already, never offered.
fn own_folders(cfg: &Value) -> Vec<String> {
    let mut own: Vec<String> = ["data_mount", "media_mount", "external_backup_mount"]
        .iter()
        .filter_map(|key| cfg[*key].as_str())
        .filter(|p| p.starts_with('/') && *p != "/")
        .map(str::to_string)
        .collect();
    // A kind of data the Drives page moved to an Extra drive (1.0.1): its folders there,
    // as existing_media.own_on_drives refuses them (T3-101-2).
    let backups = ["backups", "photos/archive"];
    for what in ["photos", "music", "cloud", "movies", "backups"] {
        let Some(drive) = cfg["locations"][what].as_str() else {
            continue;
        };
        if !common::extra_drive(cfg, drive) {
            continue;
        }
        let folders: Vec<&str> = if what == "backups" {
            backups.to_vec()
        } else {
            (0..4)
                .map(|n| common::shared_folder(what, n))
                .filter(|f| !f.is_empty())
                .collect()
        };
        for folder in folders {
            own.push(format!("{}/{drive}/{folder}", common::EXTRA_MOUNTS));
        }
    }
    own
}
fn is_own(path: &str, own: &[String]) -> bool {
    own.iter()
        .any(|o| path == o || path.starts_with(&format!("{o}/")))
}
/// The folders the Drives page's jobs make at a drive's top (contract §5): an old copy, a
/// copy under way, one being deleted. Never offered, nor anything inside them.
const JOB_FOLDERS: [&str; 3] = ["nekoserver-old-", "nekoserver-new-", "nekoserver-deleting-"];
fn is_job_folder(name: &str) -> bool {
    JOB_FOLDERS.iter().any(|start| name.starts_with(start))
}

/// The mounts of the kernel's table: (where, filesystem type), the last one for a folder
/// in effect.
pub fn mounts(table: &str) -> Vec<(String, String)> {
    let mut found: Vec<(String, String)> = Vec::new();
    for line in table.lines() {
        let Some((left, right)) = line.split_once(" - ") else {
            continue;
        };
        let (Some(point), Some(fstype)) = (left.split(' ').nth(4), right.split(' ').next()) else {
            continue;
        };
        let point = common::mount_table_name(point);
        found.retain(|(p, _)| *p != point);
        found.push((point, fstype.to_string()));
    }
    found
}

/// The drives and shares mounted where the page may look, as places to start from.
pub fn places(table: &str, cfg: &Value) -> Value {
    let own = own_folders(cfg);
    let limit = facts()["browse_limit"].as_u64().unwrap_or(200) as usize;
    let mut found: Vec<Value> = mounts(table)
        .into_iter()
        .filter(|(point, fstype)| {
            under_roots(point)
                && !is_own(point, &own)
                && !NOT_MEDIA.contains(&fstype.as_str())
                && plain_path(point)
        })
        .map(|(point, fstype)| {
            let share = NETWORK.contains(&fstype.as_str()) || fstype == "autofs";
            json!({"name": point, "path": point, "mount": true, "share": share})
        })
        .collect();
    found.sort_by(|a, b| a["path"].as_str().cmp(&b["path"].as_str()));
    let more = found.len() > limit;
    found.truncate(limit);
    json!({"ok": true, "path": Value::Null, "parent": Value::Null, "folders": found, "more": more})
}

/// The folders inside `path` (a plain path under the places the page may look): names
/// only, sorted, at most browse_limit of them; links, hidden folders and NekoServer's own
/// folders left out. Looked at in a thread of its own, given up after LOOK_LIMIT.
pub fn inside(path: &str, table: &str, cfg: &Value) -> Value {
    if !plain_path(path) || !under_roots(path) {
        let roots = listed("browse_roots").join(", ");
        return refuse(
            "E42",
            &format!("Pick a folder under {roots}, or type its full path in the box."),
        );
    }
    let own = own_folders(cfg);
    if is_own(path, &own) {
        return refuse(
            "E42",
            "That is one of NekoServer's own folders, which are used already. Choose a folder of yours.",
        );
    }
    if path.split('/').any(is_job_folder) {
        return refuse(
            "E42",
            "That folder belongs to a move on the Drives page (a copy it keeps or deletes). Choose a folder of yours.",
        );
    }
    let points: Vec<String> = mounts(table).into_iter().map(|(p, _)| p).collect();
    let limit = facts()["browse_limit"].as_u64().unwrap_or(200) as usize;
    let wanted = PathBuf::from(path);
    let look = std::thread::spawn(move || names_in(&wanted, limit));
    let Some(found) = common::joined(look, LOOK_LIMIT) else {
        return refuse("E47", away());
    };
    let (names, more) = match found {
        Ok(found) => found,
        Err(why) => return refuse("E42", why),
    };
    let folders: Vec<Value> = names
        .into_iter()
        .map(|name| format!("{path}/{name}"))
        .filter(|p| !is_own(p, &own))
        .map(|p| {
            let name = p.rsplit('/').next().unwrap_or("").to_string();
            json!({"name": name, "mount": points.contains(&p), "path": p})
        })
        .collect();
    let parent = Path::new(path)
        .parent()
        .map(|p| p.to_string_lossy().to_string())
        .filter(|p| under_roots(p));
    json!({"ok": true, "path": path, "parent": parent, "folders": folders, "more": more})
}

/// The names of the folders in `path`, never through a link: (sorted names, whether
/// there were more than `limit`).
fn names_in(path: &Path, limit: usize) -> std::result::Result<(Vec<String>, bool), &'static str> {
    // No link anywhere on the way: a link would lead the look out of the places offered.
    if common::safe(path).is_err() {
        return Err("That folder is reached through a link, so it is not offered. Pick the folder it leads to.");
    }
    let seen = fs::symlink_metadata(path).map_err(|_| "That folder is not there (any more).")?;
    if !seen.is_dir() {
        return Err("That is not a folder.");
    }
    let mut listing = fs::read_dir(path).map_err(|_| "That folder cannot be opened.")?;
    let mut names = Vec::new();
    // At most ten times the limit are read: a folder of a million files is not listed whole.
    for entry in listing.by_ref().take(limit * 10).flatten() {
        // The entry's own type: a link to a folder is a link, never followed.
        if !entry.file_type().is_ok_and(|t| t.is_dir()) {
            continue;
        }
        let Some(name) = entry.file_name().to_str().map(str::to_string) else {
            continue; // not UTF-8: no device writes such a name, and the page could not show it
        };
        if name.starts_with('.')
            || name.chars().any(char::is_control)
            || name.contains('\\')
            || is_job_folder(&name)
        {
            continue;
        }
        names.push(name);
    }
    // Names left unread are said as "more": a folder of a few thousand songs and the
    // folders beside them showed only some of them, or "No folders in it", with no word
    // that there were more (deep check 850).
    let mut more = listing.next().is_some();
    names.sort();
    if names.len() > limit {
        names.truncate(limit);
        more = true;
    }
    Ok((names, more))
}

/// What `browse` answers: the places to start from, or the folders inside one.
pub fn browse(request: &Value, cfg: &Value) -> Value {
    let table = fs::read_to_string("/proc/self/mountinfo").unwrap_or_default();
    match request["path"].as_str().filter(|p| !p.is_empty()) {
        None => places(&table, cfg),
        Some(path) => inside(path, &table, cfg),
    }
}

/// The ops of this page, as the web server names them (serve.rs) and the control service
/// answers them.
pub fn answer(folders: &Folders, op: &str, request: &Value, cfg: &Value) -> Option<Value> {
    Some(match op {
        "app_media_state" => folders.state(),
        "app_media_change" => folders.change(request),
        "app_media_browse" => browse(request, cfg),
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};

    /// A stand-in systemctl: whether the service is at work, and what was asked of it.
    struct Systemd {
        active: AtomicBool,
        refuse: AtomicBool,
        asked: Mutex<Vec<String>>,
    }
    fn folders(name: &str) -> (Folders, Arc<Systemd>, PathBuf) {
        let folder =
            std::env::temp_dir().join(format!("neko-media-folders-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&folder);
        let systemd = Arc::new(Systemd {
            active: AtomicBool::new(false),
            refuse: AtomicBool::new(false),
            asked: Mutex::new(Vec::new()),
        });
        let seen = systemd.clone();
        let run: Runner = Arc::new(move |args: &[&str]| {
            seen.asked.lock().unwrap().push(args.join(" "));
            match args.get(1) {
                Some(&"is-active") if seen.active.load(Ordering::SeqCst) => {
                    (0, "activating\n".into())
                }
                Some(&"is-active") => (3, "inactive\n".into()),
                Some(&"start") if seen.refuse.load(Ordering::SeqCst) => (1, String::new()),
                _ => (0, String::new()),
            }
        });
        (Folders::new(folder.clone(), run), systemd, folder)
    }

    #[test]
    fn every_field_is_checked_before_anything_is_written() {
        let add = json!({"change": "add", "kind": "movies", "path": "/mnt/nas/films", "how": "in_place", "downloads": true});
        assert_eq!(
            checked(&add).unwrap(),
            json!({"change": "add", "kind": "movies", "path": "/mnt/nas/films", "how": "in_place", "downloads": true})
        );
        // Only the fields the change needs go on: anything else the page sent is dropped.
        let mut extra = add.clone();
        extra["run"] = json!("rm -rf /");
        assert_eq!(checked(&extra).unwrap(), checked(&add).unwrap());
        for (key, bad) in [
            ("kind", json!("photos")),
            ("kind", json!(null)),
            ("change", json!("format")),
            ("how", json!("move")),
            ("downloads", json!("yes")),
            ("path", json!("mnt/films")),
            ("path", json!("/mnt/../etc")),
            ("path", json!("/mnt/./films")),
            ("path", json!("/mnt//films")),
            ("path", json!("/mnt/films/")),
            ("path", json!("/mnt/fi\nlms")),
            ("path", json!("/mnt/fi\\lms")),
            ("path", json!("/")),
            ("path", json!(7)),
        ] {
            let mut wrong = add.clone();
            wrong[key] = bad.clone();
            assert!(checked(&wrong).is_err(), "{key} = {bad}");
        }
        // The music backup's answer goes on for music used where it is, and only then.
        let music = json!({"change": "add", "kind": "music", "path": "/mnt/nas/music", "how": "in_place", "downloads": false, "backup": false});
        assert_eq!(checked(&music).unwrap()["backup"], false);
        let mut films = music.clone();
        films["kind"] = json!("movies");
        assert!(checked(&films).unwrap().get("backup").is_none());
        let mut copied = music.clone();
        copied["how"] = json!("copy");
        assert!(checked(&copied).unwrap().get("backup").is_none());
        let mut odd = music.clone();
        odd["backup"] = json!("no");
        assert!(checked(&odd).is_err());
        let copy = json!({"change": "add", "kind": "music", "path": "/mnt/m", "how": "copy", "downloads": true});
        assert!(checked(&copy).unwrap_err().contains("copied"));
        // A folder is named by its slot, of the kind asked about.
        for slot in [
            "everything",
            "movies-2",
            "tv-0",
            "tv-100",
            "../tv",
            "tv-new2",
            "",
        ] {
            assert!(
                checked(&json!({"change": "remove", "kind": "tv", "slot": slot})).is_err(),
                "{slot}"
            );
        }
        for slot in ["tv", "tv-new", "tv-2", "tv-99"] {
            assert_eq!(
                checked(&json!({"change": "remove", "kind": "tv", "slot": slot})).unwrap(),
                json!({"change": "remove", "kind": "tv", "slot": slot})
            );
        }
        assert!(checked(&json!({"change": "remove", "kind": "tv", "use": "in_place"})).is_err());
        // The folder the page showed there goes on, for media_locations.py to compare.
        for change in [
            json!({"change": "remove", "kind": "tv", "slot": "tv-2", "path": "/mnt/nas/Shows"}),
            json!({"change": "target", "kind": "tv", "target": "tv-2", "path": "/mnt/nas/Shows"}),
        ] {
            assert_eq!(
                checked(&change).unwrap()["path"],
                "/mnt/nas/Shows",
                "{change}"
            );
            let mut odd = change.clone();
            odd["path"] = json!("/mnt/../etc");
            assert!(checked(&odd).is_err(), "{odd}");
        }
        assert!(checked(&json!({"change": "target", "kind": "tv", "target": "/srv"})).is_err());
        assert!(checked(&json!({"change": "target", "kind": "tv", "target": "in_place"})).is_err());
        for target in ["library", "tv-3"] {
            assert_eq!(
                checked(&json!({"change": "target", "kind": "tv", "target": target})).unwrap(),
                json!({"change": "target", "kind": "tv", "target": target})
            );
        }
        assert!(is_any_slot("music-new") && !is_any_slot("photos") && !is_any_slot("Movies"));
        assert_eq!(folders_per_kind(), 8);
        assert_eq!(
            checked(&json!({"change": "look", "kind": "x"})).unwrap(),
            json!({"change": "look"})
        );
        // A path with spaces and accents, as phones and PCs name folders, is fine.
        assert!(plain_path("/mnt/nas/Films de Zoé (2024)"));
    }

    #[test]
    fn a_folder_made_on_an_extra_drive_names_the_drive_only() {
        // 1.0.1, part C: no path goes on (media_locations.py makes <drive>/media/<kind>).
        let make = json!({"change": "make", "kind": "music", "drive": "d4", "downloads": false,
            "backup": true, "path": "/etc", "how": "copy"});
        assert_eq!(
            checked(&make).unwrap(),
            json!({"change": "make", "kind": "music", "drive": "d4", "downloads": false, "backup": true})
        );
        let mut films = make.clone();
        films["kind"] = json!("movies");
        assert!(checked(&films).unwrap().get("backup").is_none());
        for (key, bad) in [
            ("drive", json!("../d4")),
            ("drive", json!("D4")),
            ("drive", json!("4d")),
            ("drive", json!("")),
            ("drive", json!("d12345678901234567")),
            ("drive", json!(null)),
            ("downloads", json!("yes")),
            ("downloads", json!(null)),
            ("backup", json!("no")),
        ] {
            let mut wrong = make.clone();
            wrong[key] = bad.clone();
            assert!(checked(&wrong).is_err(), "{key} = {bad}");
        }
    }

    #[test]
    fn a_change_is_written_for_root_and_its_service_started_once() {
        let (f, systemd, folder) = folders("start");
        let asked = json!({"op": "app_media_change", "change": "remove", "kind": "tv", "slot": "tv", "token": "x"});
        let started = f.change(&asked);
        assert_eq!(started["ok"], true, "{started}");
        let written: Value =
            serde_json::from_slice(&fs::read(folder.join("request.json")).unwrap()).unwrap();
        let id = written["id"].as_str().unwrap().to_string();
        assert_eq!(id.len(), 16);
        assert_eq!(
            written,
            json!({"change": "remove", "kind": "tv", "slot": "tv", "id": id})
        );
        let mode = fs::metadata(folder.join("request.json"))
            .unwrap()
            .permissions()
            .mode();
        assert_eq!(mode & 0o777, 0o600);
        assert_eq!(
            fs::metadata(&folder).unwrap().permissions().mode() & 0o777,
            0o700
        );
        assert!(systemd
            .asked
            .lock()
            .unwrap()
            .contains(&"systemctl start --no-block nekoserver-media-folders.service".to_string()));
        // A second press before the first was taken: already working on it.
        let again = f.change(&asked);
        assert_eq!(again["error"], "E47");
        assert_eq!(again["message"], busy());
        // Taken (media_locations.py removes the request) and still at work: the same.
        fs::remove_file(folder.join("request.json")).unwrap();
        systemd.active.store(true, Ordering::SeqCst);
        assert_eq!(f.change(&asked)["message"], busy());
        assert_eq!(f.state()["working"], true);
        // Finished: the next one goes.
        systemd.active.store(false, Ordering::SeqCst);
        assert_eq!(f.change(&asked)["ok"], true);
        // A refused field never reaches the folder or systemd.
        fs::remove_file(folder.join("request.json")).unwrap();
        let before = systemd.asked.lock().unwrap().len();
        let bad = f.change(&json!({"change": "add", "kind": "movies", "path": "/mnt/../etc", "how": "in_place", "downloads": false}));
        assert_eq!(bad["error"], "E42");
        assert!(!folder.join("request.json").exists());
        assert_eq!(systemd.asked.lock().unwrap().len(), before);
        fs::remove_dir_all(&folder).unwrap();
    }

    #[test]
    fn a_service_that_would_not_start_leaves_no_request_behind() {
        let (f, systemd, folder) = folders("refused");
        systemd.refuse.store(true, Ordering::SeqCst);
        let r = f.change(&json!({"change": "look"}));
        assert_eq!(r["error"], "E47");
        assert!(!folder.join("request.json").exists());
        // ... so the next try is not "already working on it".
        systemd.refuse.store(false, Ordering::SeqCst);
        assert_eq!(f.change(&json!({"change": "look"}))["ok"], true);
        fs::remove_dir_all(&folder).unwrap();
    }

    #[test]
    fn an_answer_left_working_by_a_run_that_ended_is_said_as_stopped() {
        let (f, systemd, folder) = folders("state");
        assert_eq!(
            f.state(),
            json!({"ok": true, "working": false, "result": null, "progress": null})
        );
        fs::create_dir_all(&folder).unwrap();
        let working = json!({"format": "neko-media-folders-v1", "state": "working", "message": "Working on it…"});
        common::atomic_json(&folder.join("result.json"), &working).unwrap();
        systemd.active.store(true, Ordering::SeqCst);
        assert_eq!(f.state()["result"]["state"], "working");
        systemd.active.store(false, Ordering::SeqCst);
        let ended = f.state();
        assert_eq!(ended["result"]["state"], "failed");
        assert_eq!(ended["result"]["message"], STOPPED);
        // Something else in the file is not an answer of this page.
        common::atomic_json(&folder.join("result.json"), &json!({"state": "done"})).unwrap();
        assert_eq!(f.state()["result"], Value::Null);
        fs::remove_dir_all(&folder).unwrap();
    }

    /// The owner's decision 3 on PR 89: a copy into the library shows how far it is, the
    /// same for every browser (the job's own file, never the page's memory).
    #[test]
    fn a_copy_at_work_hands_its_progress_to_the_page() {
        let (f, systemd, folder) = folders("progress");
        fs::create_dir_all(&folder).unwrap();
        let working = json!({"format": "neko-media-folders-v1", "id": "ab12cd34ab12cd34", "state": "working"});
        common::atomic_json(&folder.join("result.json"), &working).unwrap();
        let progress = json!({
            "format": "neko-media-folders-v1", "id": "ab12cd34ab12cd34", "kind": "tv",
            "source": "/mnt/nas/shows", "files_done": 3, "files_total": 10,
            "bytes_done": 300, "bytes_total": 1000, "at": 1.0,
        });
        common::atomic_json(&folder.join("progress.json"), &progress).unwrap();
        systemd.active.store(true, Ordering::SeqCst);
        assert_eq!(
            f.state()["progress"],
            json!({"kind": "tv", "source": "/mnt/nas/shows", "files_done": 3, "files_total": 10, "bytes_done": 300, "bytes_total": 1000})
        );
        // Another change's file, one that is not whole, or a change that ended: none.
        for (key, value) in [("id", json!("ffffffffffffffff")), ("bytes_done", json!(-1))] {
            let mut other = progress.clone();
            other[key] = value;
            common::atomic_json(&folder.join("progress.json"), &other).unwrap();
            assert_eq!(f.state()["progress"], Value::Null, "{key}");
        }
        common::atomic_json(&folder.join("progress.json"), &progress).unwrap();
        systemd.active.store(false, Ordering::SeqCst);
        assert_eq!(f.state()["progress"], Value::Null);
        fs::remove_dir_all(&folder).unwrap();
    }

    const TABLE: &str = "\
22 1 8:1 / / rw - ext4 /dev/sda1 rw
30 22 0:5 / /proc rw - proc proc rw
40 22 8:17 / /mnt/nekoserver-media rw - ext4 /dev/sdb1 rw
41 22 8:33 / /mnt/big\\040disk rw - ext4 /dev/sdc1 rw
42 22 0:50 / /mnt/nas rw - nfs4 nas:/tank rw
43 22 0:51 / /run/media/amy/USB rw - vfat /dev/sdd1 rw
44 22 0:52 / /srv/nekoserver rw - ext4 /dev/sde1 rw
45 22 0:53 / /home/amy/films rw - ext4 /dev/sdf1 rw
46 22 0:54 / /run/user/1000 rw - tmpfs tmpfs rw
";

    #[test]
    fn the_places_offered_are_the_drives_and_shares_mounted_where_media_is_kept() {
        let cfg = json!({"data_mount": "/srv/nekoserver", "media_mount": "/mnt/nekoserver-media"});
        let offered = places(TABLE, &cfg);
        let paths: Vec<&str> = offered["folders"]
            .as_array()
            .unwrap()
            .iter()
            .map(|f| f["path"].as_str().unwrap())
            .collect();
        // NekoServer's own drives, the system's, a home folder and /run/user are not offered.
        assert_eq!(paths, ["/mnt/big disk", "/mnt/nas", "/run/media/amy/USB"]);
        assert_eq!(offered["folders"][1]["share"], true);
        assert_eq!(offered["more"], false);
    }

    #[test]
    fn the_folders_a_move_put_on_an_extra_drive_are_never_offered() {
        // T3-101-2: photos moved to d4 (and the backups): their folders there are
        // NekoServer's own, as Media folders refuses them.
        let cfg = json!({"data_mount": "/srv/nekoserver", "media_mount": "/mnt/nekoserver-media",
            "storage": {"d4": {"uuid": "x"}, "d5": {"uuid": "y"}},
            "locations": {"photos": "d4", "backups": "d4", "music": "media", "movies": "d9"}});
        let own = own_folders(&cfg);
        for folder in [
            "/mnt/nekoserver-drives/d4/photos/immich-library",
            "/mnt/nekoserver-drives/d4/backups",
            "/mnt/nekoserver-drives/d4/photos/archive",
        ] {
            assert!(own.iter().any(|o| o == folder), "{folder}: {own:?}");
        }
        assert!(
            !own.iter()
                .any(|o| o.contains("/d9/") || o.contains("library/music")),
            "{own:?}"
        );
        let refused = inside(
            "/mnt/nekoserver-drives/d4/photos/immich-library/x",
            "",
            &cfg,
        );
        assert_eq!(refused["error"], "E42", "{refused}");
        assert!(is_own("/mnt/nekoserver-drives/d4/backups/photos", &own));
        assert!(!is_own("/mnt/nekoserver-drives/d4/films", &own));
    }

    #[test]
    fn the_folders_inside_are_names_only_without_links_hidden_or_own_folders() {
        let top = std::env::temp_dir().join(format!("neko-media-browse-{}", std::process::id()));
        let _ = fs::remove_dir_all(&top);
        // Looked at as if under /mnt: the page may look only there, so the test's own
        // folder stands in through the check that matters here (names_in).
        // The Drives page's own folders (an old copy, a copy under way, one being deleted)
        // are never offered: picking one would put an app's folder on a copy the page deletes.
        for name in [
            "Films",
            "Shows",
            ".hidden",
            "lost+found",
            "nekoserver-old-photos-0123456789abcdef",
            "nekoserver-new-movies-0123456789abcdef",
            "nekoserver-deleting-music-0123456789abcdef",
            // A name the page could not show or send back as it is: each is left out.
            "back\\slash",
            "new\nline",
        ] {
            fs::create_dir_all(top.join(name)).unwrap();
        }
        fs::write(top.join("notes.txt"), "x").unwrap();
        std::os::unix::fs::symlink("/etc", top.join("etc-link")).unwrap();
        let (names, more) = names_in(&top, 200).unwrap();
        assert_eq!(names, ["Films", "Shows", "lost+found"]);
        assert!(!more);
        let (names, more) = names_in(&top, 2).unwrap();
        assert_eq!((names.len(), more), (2, true));
        // More names than are read (ten times the limit): said as more, even when none of
        // those read was a folder (a folder of songs with the albums' folders beside them).
        let songs = top.join("Music");
        fs::create_dir_all(songs.join("Abba")).unwrap();
        for n in 0..12 {
            fs::write(songs.join(format!("song {n}.mp3")), "x").unwrap();
        }
        let (_, more) = names_in(&songs, 1).unwrap();
        assert!(more, "names left unread are said as more");
        // A folder reached through a link is not looked into.
        assert!(names_in(&top.join("etc-link"), 200).is_err());
        fs::remove_dir_all(&top).unwrap();
        // Only under the places offered, as a plain path, and never NekoServer's own.
        let cfg = json!({"data_mount": "/srv/nekoserver", "media_mount": "/mnt/nekoserver-media"});
        for path in ["/etc", "/home/amy", "/mnt/../etc", "/mntx", "relative"] {
            assert_eq!(inside(path, TABLE, &cfg)["error"], "E42", "{path}");
        }
        assert_eq!(
            inside("/srv/nekoserver/appdata", TABLE, &cfg)["error"],
            "E42"
        );
        for path in [
            "/mnt/nekoserver-drives/d3/nekoserver-old-photos-0123456789abcdef",
            "/mnt/nekoserver-drives/d3/nekoserver-new-movies-0123456789abcdef/Films",
        ] {
            let refused = inside(path, TABLE, &cfg);
            assert_eq!(refused["error"], "E42", "{path}");
            assert!(refused["message"].as_str().unwrap().contains("Drives page"));
        }
        let missing = inside("/mnt/not-there-at-all", TABLE, &cfg);
        assert_eq!(missing["error"], "E42");
        assert!(missing["message"].as_str().unwrap().contains("not there"));
    }
}
