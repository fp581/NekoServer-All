//! The dashboard's Backup cards page, through the control service (root, behind the PIN):
//! the drives plugged in that could be cards, "Use this as card N of the set"
//! (Volume::enrol, which never formats and never adopts a NekoBackup folder already
//! there), and "Write to the card now" (the card job's own service).
//!
//! A card of another set, a card that holds files, a full card, a drive that is not
//! removable, the system disk and NekoServer's own drives are refused, each with words
//! that say why; the enrolment checks everything again on the drive as it is then.
use super::drives::{self, Drive};
use super::status::{self, Place, Seen};
use super::tasks;
use crate::cardset::format;
use crate::cardset::fsat::Dir;
use crate::cardset::lock::SetLock;
use crate::cardset::planner;
use crate::cardset::volume::{Enrolment, Volume};
use crate::common::{self, Result};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

/// The control service's answer to op `op` of the page (the PIN is already checked).
/// `config`: the dashboard's settings (they say whether backup cards are on, and where
/// the app SSD is); `tool`: the release's restore tool, written onto every card.
pub fn answer(config: Option<&Path>, tool: &Path, op: &str, request: &Value) -> Value {
    let cfg = config
        .map(|c| common::json(c, 2_000_000))
        .unwrap_or(Value::Null);
    if !super::enabled(&cfg) {
        return refuse("Backup cards are switched off. Switch them on on setup's first page (under Storage): run  sudo bash install.sh  on the server.");
    }
    let Ok(data) = common::mount_path(cfg["data_mount"].as_str().unwrap_or("")) else {
        return refuse("The app SSD is not connected, so the backup cards can't be set up now.");
    };
    if !common::mounted(&data) {
        return refuse("The app SSD is not connected, so the backup cards can't be set up now.");
    }
    let place = Place::on(&data);
    let done = match op {
        "cards_list" => drives::plugged_in().and_then(|d| list(&place, &cfg, &d)),
        "cards_enrol" => drives::plugged_in().and_then(|d| enrol(&place, &cfg, &d, tool, request)),
        "cards_write" => write_now(&place, &cfg),
        "cards_take_back" => drives::plugged_in().and_then(|d| take_back(&place, &cfg, &d)),
        "cards_check" => check(&place, &cfg, request),
        _ => return refuse("Request not supported"),
    };
    done.unwrap_or_else(|e| refuse(&e.to_string()))
}

fn refuse(message: &str) -> Value {
    json!({"ok": false, "error": "E42", "message": message})
}

/// Names a card may hold and still be empty: what Linux, Windows and macOS put on a
/// freshly formatted card, or when it was only looked at.
fn left_by_a_computer(name: &str) -> bool {
    [
        "lost+found",
        "System Volume Information",
        ".Trashes",
        ".fseventsd",
        ".Spotlight-V100",
        "$RECYCLE.BIN",
    ]
    .contains(&name)
        || name.starts_with(".Trash-")
}

/// What one mounted drive is to the set.
#[derive(Debug, PartialEq, Eq)]
pub enum Verdict {
    /// Card N of this set.
    Ours(u64),
    /// Can be enrolled as the next card.
    Usable,
    /// Never used, with why.
    Refused(String),
}

/// The smallest card worth enrolling: its reserve (planner) and room for a few photos.
fn too_small(free: u64, capacity: u64, unit: u64) -> Option<u64> {
    let space = planner::Space {
        free,
        capacity,
        unit,
        max_file: u64::MAX,
    };
    let needed = planner::reserve(&space, 0, 2) + 64 * (1 << 20);
    (free < needed).then_some(needed)
}

fn gigabytes(bytes: u64) -> String {
    format!("{:.1} GB", bytes as f64 / 1e9)
}

/// What `drive` (mounted at `mount`) is to the set whose ID is `set_id`, if there is one.
pub fn judge(drive: &Drive, mount: &Path, cfg: &Value, set: Option<&(String, String)>) -> Verdict {
    if let Some(why) = drives::refused(drive, cfg) {
        return Verdict::Refused(why);
    }
    if drives::mounted_device(mount).as_deref() != Some(&drive.device) {
        return Verdict::Refused("Something else is mounted on this card's folder: take the card out and put it in again.".into());
    }
    match drives::identity_at(mount) {
        Ok(Some(id)) if set.is_some_and(|(set_id, _)| *set_id == id.backup_set_id) => {
            return Verdict::Ours(id.sequence_number)
        }
        Ok(Some(id)) => {
            return Verdict::Refused(format!(
                "This card belongs to another backup set (\u{201c}{}\u{201d}): NekoServer leaves it as it is.",
                id.set_display_name
            ))
        }
        Ok(None) => {}
        Err(_) => {
            return Verdict::Refused(
                "This card has a NekoBackup folder that could not be read: NekoServer leaves it as it is.".into(),
            )
        }
    }
    let Ok(root) = Dir::open_path(mount) else {
        return Verdict::Refused("This card's folder could not be opened.".into());
    };
    let names = root.names().unwrap_or_else(|_| vec!["?".into()]);
    if names.iter().any(|n| !left_by_a_computer(n)) {
        return Verdict::Refused(
            "This card already holds files. Use an empty card: NekoServer never erases one.".into(),
        );
    }
    let Ok(room) = root.room() else {
        return Verdict::Refused("This card's free space could not be read.".into());
    };
    if room.read_only {
        return Verdict::Refused(
            "This card is read-only (its lock switch, or a card that is failing).".into(),
        );
    }
    if let Some(needed) = too_small(room.free, room.capacity, room.unit) {
        return Verdict::Refused(format!(
            "This card is full or too small: it has {} free, and a backup card needs at least {}.",
            gigabytes(room.free),
            gigabytes(needed)
        ));
    }
    Verdict::Usable
}

/// The page's list: the set as the status has it, and each drive plugged in and mounted
/// (not the system disk's, nor NekoServer's own) with what it is to the set.
pub fn list(place: &Place, cfg: &Value, plugged: &[Drive]) -> Result<Value> {
    let store = place.existing()?;
    let set = match &store {
        Some(s) => s.backup_set().map_err(super::card_error)?,
        None => None,
    };
    let shown = place.previous();
    let next = shown["next_number"].as_u64().unwrap_or(1);
    // Cards of an earlier set the server could take back (only while its own set has none).
    let older = tasks::older_cards(place, plugged, cfg);
    let mut rows = vec![];
    for d in plugged {
        let Some(mount) = &d.mount else { continue };
        if d.system || drives::own_uuids(cfg).contains(&d.uuid) {
            continue;
        }
        let (card, usable, why) = match judge(d, mount, cfg, set.as_ref()) {
            Verdict::Ours(n) => (json!(n), false, Value::Null),
            Verdict::Usable => (Value::Null, true, Value::Null),
            Verdict::Refused(why) => (Value::Null, false, json!(why)),
        };
        let older_set = older.iter().find(|o| &o.mount == mount).map(
            |o| json!({"made": tasks::made_at(&o.identity), "number": o.identity.sequence_number}),
        );
        rows.push(
            json!({"mount": mount, "label": d.label, "size": d.size, "fstype": d.fstype,
                         "card": card, "usable": usable, "why": why, "older_set": older_set}),
        );
    }
    Ok(
        json!({"ok": true, "set": shown, "drives": rows, "next_number": next, "task": tasks::shown(place)}),
    )
}

/// "Take these cards back": the cards of an earlier set plugged in, taken back by the
/// card job (cards::tasks), which the page then follows.
pub fn take_back(place: &Place, cfg: &Value, plugged: &[Drive]) -> Result<Value> {
    let older = tasks::older_cards(place, plugged, cfg);
    let Some(first) = older.first() else {
        return Ok(refuse("No card of an earlier set is plugged in (and mounted), or this server already has cards of its own set."));
    };
    let set = &first.identity.backup_set_id;
    if older.iter().any(|o| &o.identity.backup_set_id != set) {
        return Ok(refuse("Cards of two different sets are plugged in: leave in only the cards of one set, then press Take these cards back again."));
    }
    tasks::start(
        place,
        json!({"kind": tasks::TAKE_BACK}),
        "Taking the cards back: reading them…",
    )
}

/// "Check this card": card `number` of the set, plugged in at `mount`, read again by the
/// card job (cards::tasks).
pub fn check(place: &Place, cfg: &Value, request: &Value) -> Result<Value> {
    let number = request["number"].as_u64().unwrap_or(0);
    let mount = PathBuf::from(request["mount"].as_str().unwrap_or(""));
    let not_ours = || {
        refuse(
            "This is not a card of this server's set, or it was taken out: look for cards again.",
        )
    };
    let Some(store) = place.existing()? else {
        return Ok(not_ours());
    };
    let Some((set_id, _)) = store.backup_set().map_err(super::card_error)? else {
        return Ok(not_ours());
    };
    let cards = drives::cards_of_set(&store, &set_id, &drives::plugged_in()?, cfg);
    if !cards
        .iter()
        .any(|c| c.row.sequence_number == number && c.mount == mount)
    {
        return Ok(not_ours());
    }
    tasks::start(
        place,
        json!({"kind": tasks::CHECK, "number": number}),
        &format!("Checking card {number}: every copy on it is read again…"),
    )
}

/// "Use this as card N of the set": the drive mounted at the request's `mount`, checked
/// again as it is now, enrolled as card `number` (the page's, which must still be the
/// next one), with the person's word that the copies are not encrypted.
pub fn enrol(
    place: &Place,
    cfg: &Value,
    plugged: &[Drive],
    tool: &Path,
    request: &Value,
) -> Result<Value> {
    let wanted = PathBuf::from(request["mount"].as_str().unwrap_or(""));
    let Some(drive) = plugged
        .iter()
        .find(|d| d.mount.as_deref() == Some(wanted.as_path()))
    else {
        return Ok(refuse(
            "That card is no longer plugged in (or no longer mounted there): look again.",
        ));
    };
    if request["acknowledged"] != true {
        return Ok(refuse(
            "Tick that you understand the copies on the cards are not encrypted first.",
        ));
    }
    let mut store = place.open()?;
    let set = store.backup_set().map_err(super::card_error)?;
    match judge(drive, &wanted, cfg, set.as_ref()) {
        Verdict::Usable => {}
        Verdict::Ours(n) => return Ok(refuse(&format!("This is card {n} of the set already."))),
        Verdict::Refused(why) => return Ok(refuse(&why)),
    }
    let (set_id, set_name) = store
        .start_set(super::fact("set_name"))
        .map_err(super::card_error)?;
    let _set = SetLock::take(&place.folder, &set_id).map_err(super::card_error)?;
    let next = store
        .volumes()
        .map_err(super::card_error)?
        .iter()
        .map(|v| v.sequence_number)
        .max()
        .unwrap_or(0)
        + 1;
    if request["number"].as_u64() != Some(next) {
        return Ok(refuse(&format!(
            "The page was out of date: this card would be card {next}. Look again."
        )));
    }
    let restore_tool = std::fs::read(tool).map_err(|e| common::at(e, tool))?;
    let volume_id = format::new_uuid().map_err(super::card_error)?;
    let enrolment = Enrolment {
        backup_set_id: set_id.clone(),
        volume_id: volume_id.clone(),
        sequence_number: next,
        set_display_name: set_name,
        filesystem_type: drive.fstype.clone(),
        filesystem_uuid: drive.uuid.clone(),
        privacy_acknowledged_at: format::now(),
        restore_tool,
    };
    let expect = drives::expect(&wanted, &set_id, &volume_id, &drive.uuid)?;
    let vol = Volume::enrol(&wanted, &expect, &enrolment).map_err(super::card_error)?;
    let room = vol.room().map_err(super::card_error)?;
    store
        .saw_volume(&vol.identity, room.capacity, &format::now())
        .map_err(super::card_error)?;
    let seen = Seen {
        plugged: vec![next],
        ..Seen::default()
    };
    status::save(place, &store, &seen)?;
    Ok(json!({"ok": true, "number": next, "message": format!(
        "Card {next} is ready. Write \u{201c}{next}\u{201d} on it. It is filled tonight while it is plugged in, or now with Write to the card now."
    )}))
}

/// "Write to the card now": the card job's service, when a card of the set is in.
pub fn write_now(place: &Place, cfg: &Value) -> Result<Value> {
    let Some(store) = place.existing()? else {
        return Ok(refuse(
            "No backup card is set up yet: plug in an empty card and choose Use this as card 1.",
        ));
    };
    let Some((set_id, _)) = store.backup_set().map_err(super::card_error)? else {
        return Ok(refuse(
            "No backup card is set up yet: plug in an empty card and choose Use this as card 1.",
        ));
    };
    let cards = drives::cards_of_set(&store, &set_id, &drives::plugged_in()?, cfg);
    let Some(first) = cards.first() else {
        return Ok(refuse("No card of the set is plugged in (and mounted): put one in, then press Write to the card now again."));
    };
    match common::probe(&["systemctl", "start", "--no-block", super::fact("service")], 30) {
        (0, _) => Ok(json!({"ok": true, "message": format!(
            "Writing to card {} now. It goes on if you close this page; the Backups page shows how it ends. Leave the card in until then.",
            first.row.sequence_number
        )})),
        _ => Ok(refuse("The copy onto the card could not be started. Is a backup or an update running? Backups shows which one.")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cardset::testkit::{self, Scratch};

    fn drive(mount: &Path) -> Drive {
        Drive {
            path: "/dev/sdx1".into(),
            uuid: "1234-ABCD".into(),
            fstype: "exfat".into(),
            label: "CARD".into(),
            size: 64_000_000_000,
            mount: Some(mount.to_path_buf()),
            removable: true,
            system: false,
            device: drives::mounted_device(mount).unwrap(),
        }
    }

    #[test]
    fn an_empty_card_is_usable_and_one_with_files_or_another_sets_is_refused() {
        let s = Scratch::new("page-judge");
        let empty = s.dir("empty");
        std::fs::create_dir(empty.join("lost+found")).unwrap();
        std::fs::create_dir(empty.join("System Volume Information")).unwrap();
        let cfg = json!({});
        assert_eq!(judge(&drive(&empty), &empty, &cfg, None), Verdict::Usable);
        let used = s.dir("used");
        std::fs::write(used.join("holiday.jpg"), b"photo").unwrap();
        let Verdict::Refused(why) = judge(&drive(&used), &used, &cfg, None) else {
            panic!()
        };
        assert_eq!(
            why,
            "This card already holds files. Use an empty card: NekoServer never erases one."
        );
        // A card of a set: ours, or another set's (testkit's set is SET).
        let (card, _) = testkit::card(&s, 1);
        let ours = (testkit::SET.to_string(), "Example set".to_string());
        assert_eq!(
            judge(&drive(&card), &card, &cfg, Some(&ours)),
            Verdict::Ours(1)
        );
        let theirs = (testkit::OTHER_SET.to_string(), "Mine".to_string());
        let Verdict::Refused(why) = judge(&drive(&card), &card, &cfg, Some(&theirs)) else {
            panic!()
        };
        assert!(
            why.starts_with(
                "This card belongs to another backup set (\u{201c}Example set\u{201d})"
            ),
            "{why}"
        );
        // A card the page looked at is checked against what is mounted on its folder.
        let moved = Drive {
            device: "0:0".into(),
            ..drive(&empty)
        };
        assert!(
            matches!(judge(&moved, &empty, &cfg, None), Verdict::Refused(w) if w.contains("Something else is mounted"))
        );
    }

    #[test]
    fn a_full_card_is_refused_with_how_much_room_it_needs() {
        let needed = too_small(10 << 20, 1 << 30, 4096).unwrap();
        assert!(
            needed > 256 << 20,
            "the reserve, at least 256 MiB, and room for some photos"
        );
        assert_eq!(too_small(32 << 30, 64 << 30, 4096), None);
    }

    /// T2b-04 (0.52.4): a card of an earlier set is offered back ("older_set", with its
    /// number and the day its set was made) only while the server's own set has no card.
    #[test]
    fn a_card_of_an_earlier_set_is_offered_back_only_while_the_server_has_no_card() {
        let s = Scratch::new("page-older");
        let (card, _) = testkit::card(&s, 2);
        let d = Drive {
            uuid: testkit::fs_uuid(2),
            ..drive(&card)
        };
        let place = Place {
            folder: s.dir("ssd-cards"),
        };
        let cfg = json!({});
        let listed = list(&place, &cfg, std::slice::from_ref(&d)).unwrap();
        let row = &listed["drives"][0];
        assert_eq!(row["older_set"]["number"], 2, "{listed}");
        assert!(row["older_set"]["made"].is_i64(), "{listed}");
        assert!(listed["task"].is_null());
        // The server has a card of its own set: nothing is offered back.
        let mut own = drives::identity_at(&card).unwrap().unwrap();
        own.backup_set_id = testkit::OTHER_SET.to_string();
        own.volume_id = testkit::volume_id(9);
        let mut store = place.open().unwrap();
        store.take_set(testkit::OTHER_SET, "Mine").unwrap();
        store.saw_volume(&own, 1 << 30, &format::now()).unwrap();
        let listed = list(&place, &cfg, std::slice::from_ref(&d)).unwrap();
        assert!(listed["drives"][0]["older_set"].is_null(), "{listed}");
    }
}
