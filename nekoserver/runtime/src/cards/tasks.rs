//! The Backup cards page's two longer tasks (0.52.4), run by the card job's own service so
//! that they hold the backups' lock and go on when the page is closed:
//!
//! - "Take these cards back": the server's own set has no card (the app SSD was replaced,
//!   or NekoServer set up again) and cards of an older set are plugged in. The catalogue
//!   is rebuilt from them (cardset::rebuild) and takes their set; the run then writes on.
//!   A card is never taken back without the person's click.
//! - "Check this card": every copy on a card of the set read again (cardset::recheck). A
//!   damaged copy is marked, and the next run copies that file again.
//!
//! The page writes the request (`cards/request.json`) and starts the service; the job does
//! the task and keeps `cards/task.json` up to date, which the page shows while it runs.
use super::drives::{self, Drive};
use super::status::{self, Place, Seen};
use crate::backup;
use crate::cardset::error::{CardError, Kind};
use crate::cardset::format::Identity;
use crate::cardset::lock::SetLock;
use crate::cardset::store::Store;
use crate::cardset::volume::{Expect, Volume};
use crate::cardset::{rebuild, recheck};
use crate::common::{self, Result};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

pub const TAKE_BACK: &str = "take_back";
pub const CHECK: &str = "check";

fn request_path(place: &Place) -> PathBuf {
    place.folder.join("request.json")
}
fn task_path(place: &Place) -> PathBuf {
    place.folder.join("task.json")
}

/// The card job's service is running (the nightly run, Write to the card now, or a task).
pub fn service_running() -> bool {
    common::probe(
        &["systemctl", "is-active", "--quiet", super::fact("service")],
        10,
    )
    .0 == 0
}

/// A card of an older set, plugged in: what taking it back attaches.
#[derive(Debug, Clone)]
pub struct Older {
    pub mount: PathBuf,
    pub identity: Identity,
    pub expect: Expect,
}

/// The server's own set has a card (or its catalogue can't be read: then nothing is taken).
fn own_set_has_cards(place: &Place) -> bool {
    match place.existing() {
        Ok(None) => false,
        Ok(Some(store)) => store.volumes().map(|v| !v.is_empty()).unwrap_or(true),
        Err(_) => true,
    }
}

/// The cards of another set among `drives` that the server could take back: only while
/// its own set has no card. Each is removable, mounted on its own folder, not one of
/// NekoServer's drives, and its NekoBackup names the filesystem it is on.
pub fn older_cards(place: &Place, drives: &[Drive], cfg: &Value) -> Vec<Older> {
    if own_set_has_cards(place) {
        return vec![];
    }
    let mut found = vec![];
    for d in drives {
        let Some(mount) = &d.mount else { continue };
        if drives::refused(d, cfg).is_some()
            || drives::mounted_device(mount).as_deref() != Some(&d.device)
        {
            continue;
        }
        let Ok(Some(identity)) = drives::identity_at(mount) else {
            continue;
        };
        if identity.filesystem_uuid != d.uuid {
            continue;
        }
        let set = &identity.backup_set_id;
        if let Ok(expect) = drives::expect(mount, set, &identity.volume_id, &d.uuid) {
            found.push(Older {
                mount: mount.clone(),
                identity,
                expect,
            });
        }
    }
    found.sort_by_key(|o| o.identity.sequence_number);
    found
}

/// When the set of `identity` was made, in seconds since 1970 (for the page).
pub fn made_at(identity: &Identity) -> Option<i64> {
    chrono::NaiveDateTime::parse_from_str(&identity.created_at, "%Y-%m-%dT%H:%M:%SZ")
        .ok()
        .map(|t| t.and_utc().timestamp())
}

/// "Card 2", "Cards 1 and 2", "Cards 1, 2 and 3".
fn cards_words(numbers: &[u64]) -> String {
    let n: Vec<String> = numbers.iter().map(u64::to_string).collect();
    match n.as_slice() {
        [one] => format!("Card {one}"),
        [rest @ .., last] => format!("Cards {} and {last}", rest.join(", ")),
        [] => "No card".to_string(),
    }
}

/// The task as the page shows it: what runs or ran last, how far it got and how it ended.
struct Task<'a> {
    place: &'a Place,
    kind: &'static str,
    card: Option<u64>,
}

impl Task<'_> {
    fn save(&self, running: bool, done: u64, total: u64, ok: bool, message: &str) {
        let v = json!({"kind": self.kind, "card": self.card, "running": running,
            "done": done, "total": total, "ok": ok, "message": message});
        // The page's view only: the task itself never depends on it.
        let _ = common::atomic_json(&task_path(self.place), &v);
    }
    fn progress(&self, done: u64, total: u64, message: &str) {
        self.save(true, done, total, true, message);
    }
    fn end(&self, result: std::result::Result<String, String>) -> Result<()> {
        match result {
            Ok(words) => {
                println!("{words}");
                self.save(false, 0, 0, true, &words);
                Ok(())
            }
            Err(words) => {
                self.save(false, 0, 0, false, &words);
                Err(common::Error::new(&words))
            }
        }
    }
}

/// What the page shows of the last task: None before any; "running" only while the
/// service runs (a task that died says so).
pub fn shown(place: &Place) -> Value {
    let mut v = common::json(&task_path(place), 100_000);
    if !v["kind"].is_string() {
        return Value::Null;
    }
    if v["running"] == true && !service_running() {
        v["running"] = json!(false);
        v["ok"] = json!(false);
        v["message"] = json!("The task stopped before it ended (the server restarted, or the task failed). Nothing was lost: press the button again.");
    }
    v
}

/// The page's request: written for the card job, which is started now.
pub fn start(place: &Place, request: Value, started: &str) -> Result<Value> {
    if service_running() {
        return Ok(refuse(
            "The backup cards are being written or checked now: wait until that is done (the Backups page shows it), then press the button again.",
        ));
    }
    common::directory(&place.folder, 0o700)?;
    common::atomic_json(&request_path(place), &request)?;
    // The page shows the task at once, before the job has read the request.
    let task = Task {
        place,
        kind: if request["kind"] == CHECK {
            CHECK
        } else {
            TAKE_BACK
        },
        card: request["number"].as_u64(),
    };
    task.progress(0, 0, started);
    match common::probe(
        &["systemctl", "start", "--no-block", super::fact("service")],
        30,
    ) {
        (0, _) => Ok(json!({"ok": true, "message": started})),
        _ => {
            let _ = std::fs::remove_file(request_path(place));
            task.save(false, 0, 0, false, "It could not be started.");
            Ok(refuse("It could not be started. Is a backup or an update running? Backups shows which one."))
        }
    }
}

fn refuse(message: &str) -> Value {
    json!({"ok": false, "error": "E42", "message": message})
}

/// The job's part: the page's request, done before the nightly run. Ok(true) when the job
/// has nothing more to do (a check, or a task that failed); Ok(false) when the run goes
/// on (no request, or cards taken back).
pub fn from_page(
    place: &Place,
    cfg: &Value,
    cfgpath: &Path,
    data: &Path,
    media: &Path,
) -> Result<bool> {
    let path = request_path(place);
    if !path.exists() {
        return Ok(false);
    }
    let request = common::json(&path, 10_000);
    std::fs::remove_file(&path).map_err(|e| common::at(e, &path))?;
    let lock = backup::coordinate(cfgpath, data, media);
    match request["kind"].as_str() {
        Some(TAKE_BACK) => {
            let task = Task {
                place,
                kind: TAKE_BACK,
                card: None,
            };
            let words = lock
                .map_err(|e| e.to_string())
                .and_then(|_lock| take_back(place, cfg, &task));
            let ok = words.is_ok();
            task.end(words)?;
            Ok(!ok)
        }
        Some(CHECK) => {
            let number = request["number"].as_u64().unwrap_or(0);
            let task = Task {
                place,
                kind: CHECK,
                card: Some(number),
            };
            let words = lock
                .map_err(|e| e.to_string())
                .and_then(|_lock| check(place, cfg, number, &task));
            task.end(words)?;
            Ok(true)
        }
        _ => Ok(false),
    }
}

/// Why a card could not be read to the end, in words for the person.
fn read_failed(n: u64, e: &CardError, what: &str) -> String {
    let gone = matches!(e.kind, Kind::NotMounted | Kind::MountChanged)
        || matches!(e.errno, Some(libc::EIO | libc::ENODEV | libc::ENXIO));
    if gone {
        return format!("Card {n} was taken out (or stopped answering) during the {what}: nothing was changed. Plug it in again and press the button again.");
    }
    format!(
        "Card {n} could not be read to the end ({}): nothing was changed. Check it on another computer (fsck for ext4, chkdsk for exFAT), then try again.",
        e.what
    )
}

/// Take the older set's cards plugged in back: the catalogue rebuilt from them, in place
/// of the empty one, and given their set.
fn take_back(place: &Place, cfg: &Value, task: &Task) -> std::result::Result<String, String> {
    let plugged = drives::plugged_in().map_err(|e| e.to_string())?;
    if own_set_has_cards(place) {
        return Err("This server already has cards of its own set: other cards are never taken in with them.".into());
    }
    let olders = older_cards(place, &plugged, cfg);
    let Some(first) = olders.first() else {
        return Err("No card of an earlier set is plugged in (and mounted): put the cards in, then press Take these cards back again.".into());
    };
    let set_id = first.identity.backup_set_id.clone();
    if olders.iter().any(|o| o.identity.backup_set_id != set_id) {
        return Err("Cards of two different sets are plugged in: leave in only the cards of one set, then press Take these cards back again.".into());
    }
    let numbers: Vec<u64> = olders.iter().map(|o| o.identity.sequence_number).collect();
    let _set = SetLock::take(&place.folder, &set_id).map_err(|e| e.what)?;
    let mut vols = vec![];
    for o in &olders {
        let n = o.identity.sequence_number;
        vols.push(Volume::attach(&o.mount, &o.expect).map_err(|e| read_failed(n, &e, "reading"))?);
    }
    task.progress(0, olders.len() as u64, "Reading the cards…");
    // Rebuilt apart, and put in place only once every card was read: a card taken out
    // half-way leaves the server as it was.
    let mut store = Store::in_memory().map_err(|e| e.what)?;
    store
        .take_set(&set_id, &first.identity.set_display_name)
        .map_err(|e| e.what)?;
    let refs: Vec<&Volume> = vols.iter().collect();
    let report =
        rebuild::rebuild(&mut store, &refs).map_err(|e| read_failed(numbers[0], &e, "reading"))?;
    for v in &vols {
        v.confirm()
            .map_err(|e| read_failed(v.identity.sequence_number, &e, "reading"))?;
    }
    put_in_place(place, &store).map_err(|e| e.to_string())?;
    let store = place.open().map_err(|e| e.to_string())?;
    let _ = status::save(
        place,
        &store,
        &Seen {
            plugged: numbers.clone(),
            ..Seen::default()
        },
    );
    let files: usize = report.imported.values().sum();
    let mut words = format!(
        "{} taken back: {} on {} known again. The next run writes on (tonight, or now with Write to the card now).",
        cards_words(&numbers),
        common::plural(files as u64, "file", "files"),
        if numbers.len() == 1 { "it is" } else { "them are" }
    );
    if !report.damaged.is_empty() {
        words.push_str(&format!(
            " {} on the cards no longer match their checksum: those files are copied again.",
            common::plural(report.damaged.len() as u64, "copy", "copies")
        ));
    }
    if !report.unobserved_volumes.is_empty() {
        words.push_str(" Other cards of the set are not plugged in: plug each in once, and its files count again.");
    }
    Ok(words)
}

/// The rebuilt catalogue in place of the empty one (and its journal files).
fn put_in_place(place: &Place, store: &Store) -> Result<()> {
    let target = place.catalogue();
    let fresh = place.folder.join("catalogue-taken-back.tmp");
    let _ = std::fs::remove_file(&fresh);
    store.export(&fresh).map_err(super::card_error)?;
    for end in ["-wal", "-shm", "-journal"] {
        let mut old = target.clone().into_os_string();
        old.push(end);
        let _ = std::fs::remove_file(PathBuf::from(old));
    }
    std::fs::rename(&fresh, &target).map_err(|e| common::at(e, &target))?;
    Ok(())
}

/// Check card `number` of the set, plugged in: every copy read again.
fn check(
    place: &Place,
    cfg: &Value,
    number: u64,
    task: &Task,
) -> std::result::Result<String, String> {
    let not_in = || {
        format!("Card {number} of the set is not plugged in (and mounted): put it in, then press Check this card again.")
    };
    let Some(mut store) = place.existing().map_err(|e| e.to_string())? else {
        return Err(not_in());
    };
    let Some((set_id, _)) = store.backup_set().map_err(|e| e.what)? else {
        return Err(not_in());
    };
    let plugged = drives::plugged_in().map_err(|e| e.to_string())?;
    let cards = drives::cards_of_set(&store, &set_id, &plugged, cfg);
    let Some(card) = cards.iter().find(|c| c.row.sequence_number == number) else {
        return Err(not_in());
    };
    let _set = SetLock::take(&place.folder, &set_id).map_err(|e| e.what)?;
    let vol =
        Volume::attach(&card.mount, &card.expect).map_err(|e| read_failed(number, &e, "check"))?;
    let mut last = 0u64;
    let checked = recheck::check(&mut store, &vol, &mut |done, total| {
        // About once a second's worth of files: the page polls every two seconds.
        if done == 0 || done == total || done >= last + 20 {
            last = done;
            task.progress(done, total, &format!("Checking card {number}…"));
        }
    })
    .map_err(|e| read_failed(number, &e, "check"))?;
    let _ = status::save(
        place,
        &store,
        &Seen {
            plugged: cards.iter().map(|c| c.row.sequence_number).collect(),
            ..Seen::default()
        },
    );
    let fine = common::plural(checked.fine, "copy", "copies");
    if checked.damaged.is_empty() {
        return Ok(format!("Card {number} checked: all {fine} are fine."));
    }
    let named: Vec<&str> = checked.damaged.iter().take(3).map(String::as_str).collect();
    let more = checked.damaged.len().saturating_sub(named.len());
    let mut words = format!(
        "Card {number} checked: {fine} fine, {} damaged ({}{}).",
        checked.damaged.len(),
        named.join(", "),
        if more > 0 {
            format!(" and {more} more")
        } else {
            String::new()
        }
    );
    if checked.waiting.is_empty() {
        words.push_str(" Each of those files has a good copy on the cards already.");
    } else {
        words.push_str(&format!(
            " The next run copies {} again onto the card plugged in then (tonight, or now with Write to the card now). Keep this card: its other copies are fine.",
            if checked.waiting.len() == 1 { "that file".to_string() } else { format!("those {} files", checked.waiting.len()) }
        ));
    }
    Ok(words)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cards_are_named_in_plain_words() {
        assert_eq!(cards_words(&[2]), "Card 2");
        assert_eq!(cards_words(&[1, 2]), "Cards 1 and 2");
        assert_eq!(cards_words(&[1, 2, 3]), "Cards 1, 2 and 3");
    }

    #[test]
    fn a_card_taken_out_is_said_so_and_nothing_is_changed() {
        let e = CardError::new(Kind::MountChanged, "the card's folder is gone");
        let w = read_failed(2, &e, "check");
        assert!(w.starts_with("Card 2 was taken out"), "{w}");
        assert!(w.contains("nothing was changed"), "{w}");
        let io = CardError {
            errno: Some(libc::EIO),
            ..CardError::new(Kind::Io, "read")
        };
        assert!(read_failed(1, &io, "reading").contains("taken out"));
    }

    #[test]
    fn the_day_a_set_was_made_is_read_from_its_identity() {
        let s = crate::cardset::testkit::Scratch::new("tasks-made");
        let (card, _) = crate::cardset::testkit::card(&s, 1);
        let id = drives::identity_at(&card).unwrap().unwrap();
        let t = made_at(&id).unwrap();
        assert!((t - chrono::Utc::now().timestamp()).abs() < 600, "{t}");
    }
}
