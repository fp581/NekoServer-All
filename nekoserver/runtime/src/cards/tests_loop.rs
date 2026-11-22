//! Backup cards on a real card: an ext4 filesystem in a loop device, mounted by
//! `tests/cards-loop-tests.sh`, with a second card that already holds a file. Ignored by
//! a plain `cargo test`: it needs root and the script's setup.
//!
//! What it shows that the unit tests cannot: the page and the job on a real mount (the
//! mount-point rule, device numbers, the UUID lsblk reads, the real free space), from
//! the list of drives to a full card. A loop device says it is not removable, which an SD
//! card or a USB stick does: that one fact is set by the test (a stand-in), all else is
//! what lsblk and the kernel say.
//!
//! The script sets `NEKO_CARD` (an empty card's folder), `NEKO_USED_CARD` (a card with a
//! file on it) and `NEKO_WORK` (a folder on the server's own disk).
use super::drives::{self, Drive};
use super::job;
use super::page;
use super::status::{self, Place};
use crate::cardset::run::{self, RunState};
use crate::cardset::source::Source;
use serde_json::json;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

fn env(name: &str) -> PathBuf {
    PathBuf::from(
        std::env::var(name).unwrap_or_else(|_| panic!("{name} is set by cards-loop-tests.sh")),
    )
}

/// lsblk's drives, with the loop devices taken for removable cards. Where udev does not
/// run (a container), lsblk knows no filesystem type or UUID: blkid reads them from the
/// device itself, as udev would have.
fn plugged() -> Vec<Drive> {
    let out = Command::new("lsblk")
        .args([
            "-J",
            "-b",
            "-o",
            "PATH,UUID,FSTYPE,LABEL,SIZE,MOUNTPOINT,RM,HOTPLUG,MAJ:MIN",
        ])
        .output()
        .unwrap();
    let mut listed: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    for row in listed["blockdevices"].as_array_mut().unwrap() {
        let path = row["path"].as_str().unwrap_or("").to_string();
        if !path.starts_with("/dev/loop") || row["fstype"].is_string() {
            continue;
        }
        let probed = Command::new("blkid")
            .args(["-o", "export", &path])
            .output()
            .unwrap();
        for line in String::from_utf8_lossy(&probed.stdout).lines() {
            match line.split_once('=') {
                Some(("UUID", v)) => row["uuid"] = json!(v),
                Some(("TYPE", v)) => row["fstype"] = json!(v),
                _ => {}
            }
        }
    }
    drives::parse(&listed)
        .into_iter()
        .map(|d| Drive {
            removable: d.removable || d.path.starts_with("/dev/loop"),
            ..d
        })
        .collect()
}

fn tool(card: &Path, args: &[&str]) -> String {
    let out = Command::new("python3")
        .arg(card.join("NekoBackup/tools/neko-restore.py"))
        .args(args)
        .output()
        .unwrap();
    let said =
        String::from_utf8_lossy(&out.stdout).into_owned() + &String::from_utf8_lossy(&out.stderr);
    assert!(out.status.success(), "{said}");
    said
}

#[test]
#[ignore]
fn a_real_card_is_enrolled_from_the_page_and_filled_by_the_job() {
    let (card, used, work) = (env("NEKO_CARD"), env("NEKO_USED_CARD"), env("NEKO_WORK"));
    let place = Place {
        folder: work.join("server-cards"),
    };
    let cfg = json!({"cards": {"enabled": true}, "storage": {}});
    let tool_file = Path::new(env!("CARGO_MANIFEST_DIR")).join("../neko_restore.py");
    // The page's list: the empty card can be card 1, the other one already holds files.
    let listed = page::list(&place, &cfg, &plugged()).unwrap();
    let row = |mount: &Path| {
        listed["drives"]
            .as_array()
            .unwrap()
            .iter()
            .find(|d| d["mount"] == json!(mount))
            .cloned()
            .unwrap()
    };
    assert_eq!(row(&card)["usable"], true, "{listed}");
    assert_eq!(
        row(&used)["why"],
        "This card already holds files. Use an empty card: NekoServer never erases one."
    );
    // Enrolled with a page that was out of date (another number), then as it should be.
    let request = |n: u64| json!({"mount": card, "number": n, "acknowledged": true});
    let stale = page::enrol(&place, &cfg, &plugged(), &tool_file, &request(2)).unwrap();
    assert!(
        stale["message"]
            .as_str()
            .unwrap()
            .contains("this card would be card 1"),
        "{stale}"
    );
    assert!(
        !card.join("NekoBackup").exists(),
        "nothing written before every check passed"
    );
    let done = page::enrol(&place, &cfg, &plugged(), &tool_file, &request(1)).unwrap();
    assert_eq!(done["ok"], true, "{done}");
    let identity: serde_json::Value =
        serde_json::from_slice(&fs::read(card.join("NekoBackup/identity.json")).unwrap()).unwrap();
    let uuid = plugged()
        .into_iter()
        .find(|d| d.mount.as_deref() == Some(card.as_path()))
        .unwrap()
        .uuid;
    assert_eq!(identity["filesystem_uuid"], json!(uuid));
    assert_eq!(identity["sequence_number"], 1);
    let again = page::enrol(&place, &cfg, &plugged(), &tool_file, &request(2)).unwrap();
    assert_eq!(again["message"], "This is card 1 of the set already.");
    assert_eq!(row(&used)["usable"], false);
    // The job finds it among the drives, and fills it up to its reserve.
    let mut store = place.open().unwrap();
    let (set_id, _) = store.backup_set().unwrap().unwrap();
    let cards = drives::cards_of_set(&store, &set_id, &plugged(), &cfg);
    assert_eq!(cards.len(), 1);
    let photos = work.join("photos");
    for i in 0..12u8 {
        let file = photos.join(format!("2024/{i:02}.jpg"));
        fs::create_dir_all(file.parent().unwrap()).unwrap();
        fs::write(&file, vec![i; 16 << 20]).unwrap();
    }
    let source = Source {
        source_id: "cccccccc-0000-4000-8000-000000000001".into(),
        display_name: "Immich uploads".into(),
        root: photos,
    };
    let (cands, skipped) = super::candidates(&[source]).unwrap();
    let written = job::write(&mut store, &cards, &cands, &mut run::card_space).unwrap();
    let (n, result) = &written.runs[0];
    assert_eq!(
        (*n, result.state),
        (1, RunState::WaitingForNextCard),
        "{result:?}"
    );
    assert!(!result.committed.is_empty() && !result.waiting.is_empty());
    let counts = status::count(&store, &cands).unwrap();
    let seen = status::Seen {
        plugged: vec![1],
        full: vec![1],
        wrote: true,
        counts: Some(counts),
        ..status::Seen::default()
    };
    let shown = status::save(&place, &store, &seen).unwrap();
    assert_eq!(
        job::left_out(&written, &skipped, &shown),
        ["Card 1 is full: put in a new card (it becomes card 2)"]
    );
    // The card stands on its own: its tool lists and checks it, no server needed.
    let info = tool(&card, &["info"]);
    assert!(info.contains("NekoServer cards - 001"), "{info}");
    let verify = tool(&card, &["verify"]);
    assert!(
        verify.contains(&format!(
            "ok {}, damaged 0, missing 0",
            result.committed.len()
        )),
        "{verify}"
    );
    let room = crate::cardset::fsat::Dir::open_path(&card)
        .unwrap()
        .room()
        .unwrap();
    assert!(
        room.free >= 256 << 20,
        "the reserve is left free: {}",
        room.free
    );
}
