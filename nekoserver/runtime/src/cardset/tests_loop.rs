//! The engine on real cards: ext4 filesystems in loop devices, mounted by
//! `loop-tests.sh` in this folder, which also kills the process and pulls the card. These
//! tests are `#[ignore]`d: they need root, loop devices and the script's setup, and run
//! only when it starts them (`cargo test cardset::tests_loop -- --ignored`).
//!
//! What the unit tests cannot show and these do: the mount-point rule against a real mount,
//! the real free space and allocation of ext4, recovery after the process was killed
//! with `SIGKILL` (no destructor, no catalogue: the card alone), and a card unmounted
//! while a file is being written. A killed process is not a power cut: the kernel still
//! writes what it was given, and this report says so (archive format 1, section 11).
//!
//! The script sets:
//! - `NEKO_LOOP_CARDS`: the mount folders of fresh cards, separated by commas;
//! - `NEKO_LOOP_WORK`: a folder on the server's own disk for the sources and the signals;
//! - `NEKO_LOOP_KILL_AT`: the step at which the copy waits to be killed (`Point`'s name).
use super::catalogue::Catalogue;
use super::commit::{self, Hook, Point};
use super::error::{CardError, Kind, Res};
use super::format;
use super::run::{self, Run, RunResult, RunState};
use super::source::Source;
use super::store::Store;
use super::testkit::{bytes, cands, enrolment, payload, put, records, volume_id, SET};
use super::volume::{Expect, MountRule, Volume};
use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::thread::sleep;
use std::time::{Duration, Instant};

const MIB: usize = 1 << 20;
/// How long a copy waits at its step for the script (to kill it, or to pull the card).
const WAIT: Duration = Duration::from_secs(120);

fn env(name: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| panic!("{name} is set by loop-tests.sh"))
}

fn cards() -> Vec<PathBuf> {
    env("NEKO_LOOP_CARDS")
        .split(',')
        .map(PathBuf::from)
        .collect()
}

fn work(name: &str) -> PathBuf {
    let p = Path::new(&env("NEKO_LOOP_WORK")).join(name);
    fs::create_dir_all(&p).unwrap();
    p
}

/// The card as the catalogue would expect it: the real mount rule and device.
fn expect(card: &Path, seq: u64) -> Expect {
    Expect {
        device: fs::metadata(card).unwrap().dev(),
        backup_set_id: SET.to_string(),
        volume_id: volume_id(seq),
        filesystem_uuid: super::testkit::fs_uuid(seq),
        rule: MountRule::MountPoint,
    }
}

/// The card, enrolled the first time and attached after that.
fn card(path: &Path, seq: u64) -> Volume {
    if path.join(format::ROOT).exists() {
        Volume::attach(path, &expect(path, seq)).unwrap()
    } else {
        Volume::enrol(path, &expect(path, seq), &enrolment(seq)).unwrap()
    }
}

/// A source of `count` files of `size` MiB under the work folder (made once).
fn source(name: &str, count: u8, size: usize) -> Source {
    let src = Source {
        source_id: "cccccccc-0000-4000-8000-000000000001".to_string(),
        display_name: "Example photos".to_string(),
        root: work(name),
    };
    for i in 0..count {
        if !src.root.join(format!("2001/{i}.jpg")).exists() {
            put(&src, &format!("2001/{i}.jpg"), &bytes(i, size * MIB));
        }
    }
    src
}

/// The catalogue in the work folder: on the server's own disk, so it outlives a killed
/// process and a pulled card, as it does on a server.
fn catalogue() -> Store {
    Store::open(&work("catalogue").join("catalogue.db")).unwrap()
}

/// A run on the real card, with its real free space.
fn go(vol: &Volume, cat: &mut Store, src: &Source, hook: &mut dyn Hook) -> RunResult {
    let run_id = format::new_uuid().unwrap();
    let mut space = run::card_space;
    Run {
        vol,
        cat,
        run_id: &run_id,
        space: &mut space,
        hook,
    }
    .go(&cands(src))
    .unwrap()
}

/// Waits at the `nth` time `point` is reached: writes `ready` into the work folder, then
/// sleeps until `go_on` appears there (or the process is killed meanwhile).
fn wait_at(point: Point, nth: u32, go_on: &'static str) -> impl FnMut(Point) -> Res<()> {
    let mut seen = 0;
    move |p| {
        if p == point {
            seen += 1;
            if seen == nth {
                fs::write(work("signals").join("ready"), format!("{p:?}")).unwrap();
                let start = Instant::now();
                while !work("signals").join(go_on).exists() {
                    if start.elapsed() > WAIT {
                        return Err(CardError::new(Kind::Interrupted, "the script never came"));
                    }
                    sleep(Duration::from_millis(50));
                }
            }
        }
        Ok(())
    }
}

fn point_named(name: &str) -> Point {
    let all = [
        Point::Begun,
        Point::IntentWritten,
        Point::PayloadStarted,
        Point::PayloadWritten,
        Point::Checked,
        Point::VerifiedWritten,
        Point::Published,
        Point::RecordTempWritten,
        Point::RecordPublished,
        Point::CatalogueCommitted,
        Point::CleanedUp,
    ];
    all.into_iter()
        .find(|p| format!("{p:?}") == name)
        .unwrap_or_else(|| panic!("no step called {name}"))
}

/// Every source file is on the card exactly once, with its bytes, and staging is empty.
fn assert_whole(card: &Path, src: &Source, count: usize) {
    let recs = records(card);
    let mut paths: Vec<&str> = recs
        .iter()
        .map(|r| r.txn().original_relative_path.as_str())
        .collect();
    paths.dedup();
    assert_eq!(paths.len(), count, "every file exactly once: {paths:?}");
    assert_eq!(recs.len(), count, "no second copy of a file");
    for r in &recs {
        let original = fs::read(src.root.join(&r.txn().original_relative_path)).unwrap();
        assert!(
            payload(card, r) == original,
            "the archived bytes are the file's"
        );
    }
    let staging = card.join(format::ROOT).join(format::STAGING);
    let left: Vec<_> = fs::read_dir(staging).unwrap().flatten().collect();
    assert!(left.is_empty(), "staging is empty: {left:?}");
}

#[test]
#[ignore = "needs root and two loop cards: run loop-tests.sh"]
fn loop_a_library_larger_than_one_card() {
    // A01 on real ext4: the first card fills by its real free space, the second takes
    // the rest, nothing is copied twice.
    let cards = cards();
    let src = source("a01", 6, 12);
    let mut cat = catalogue();
    let v1 = card(&cards[0], 1);
    let r1 = go(&v1, &mut cat, &src, &mut commit::no_hook);
    assert_eq!(r1.state, RunState::WaitingForNextCard, "{r1:?}");
    assert!(!r1.committed.is_empty());
    let v2 = card(&cards[1], 2);
    let r2 = go(&v2, &mut cat, &src, &mut commit::no_hook);
    assert_eq!(r2.state, RunState::Complete, "{r2:?}");
    assert_eq!(r1.committed.len() + r2.committed.len(), 6);
    assert_eq!(r2.unchanged.len(), r1.committed.len());
}

#[test]
#[ignore = "started by loop-tests.sh, which kills it at NEKO_LOOP_KILL_AT"]
fn loop_copy_until_killed() {
    // The second file waits at the step named; the script kills the process there.
    let src = source("killed", 3, 8);
    let vol = card(&cards()[0], 1);
    let mut cat = catalogue();
    let mut hook = wait_at(point_named(&env("NEKO_LOOP_KILL_AT")), 2, "never");
    go(&vol, &mut cat, &src, &mut hook);
    panic!("the script was to kill this process before the run ended");
}

#[test]
#[ignore = "started by loop-tests.sh after loop_copy_until_killed was killed"]
fn loop_recover_after_kill() {
    // No catalogue survived the kill: the card alone is reconciled, the run finishes, and
    // every file is there once with its bytes.
    let src = source("killed", 3, 8);
    let card_path = cards()[0].clone();
    let vol = card(&card_path, 1);
    let mut cat = catalogue();
    let r = go(&vol, &mut cat, &src, &mut commit::no_hook);
    assert_eq!(r.state, RunState::Complete, "{r:?}");
    let report = &r.reconcile;
    assert!(
        report.orphans.is_empty() && report.damaged.is_empty(),
        "{report:?}"
    );
    assert!(!report.needs_filesystem_check);
    assert_whole(&card_path, &src, 3);
    println!(
        "killed at {}: finished {}, imported {}, cleaned {}, copied again {:?}",
        env("NEKO_LOOP_KILL_AT"),
        report.finished.len(),
        report.imported.len(),
        report.cleaned.len(),
        report.retry
    );
}

#[test]
#[ignore = "started by loop-tests.sh, which unmounts the card while a file is written"]
fn loop_card_pulled_mid_copy() {
    // A11 with a real unmount: the copy stops at its next identity check, nothing is
    // counted, and nothing is written into the bare mount folder.
    let src = source("pulled", 2, 32);
    let card_path = cards()[0].clone();
    let vol = card(&card_path, 1);
    let mut cat = catalogue();
    let mut hook = wait_at(Point::PayloadStarted, 1, "pulled");
    let r = go(&vol, &mut cat, &src, &mut hook);
    assert_eq!(r.state, RunState::Stopped, "{r:?}");
    let kind = r.stopped.as_ref().map(|e| e.kind);
    assert!(
        matches!(kind, Some(Kind::MountChanged | Kind::NotMounted)),
        "{kind:?}"
    );
    assert!(
        r.committed.is_empty() && cat.version_count().unwrap() == 0,
        "nothing counted"
    );
    let left: Vec<_> = fs::read_dir(&card_path).unwrap().flatten().collect();
    assert!(left.is_empty(), "nothing in the bare folder: {left:?}");
}

#[test]
#[ignore = "started by loop-tests.sh after the pulled card is mounted again"]
fn loop_card_returns() {
    // A14: the card comes back and is reconciled: the interrupted file is copied again,
    // or, when its verified intent reached the card before the copy stopped (the kernel
    // still writes to an unmounted filesystem that a file holds open), finished from it.
    // Either way nothing is lost and nothing is there twice.
    let src = source("pulled", 2, 32);
    let card_path = cards()[0].clone();
    let vol = card(&card_path, 1);
    let mut cat = catalogue();
    let r = go(&vol, &mut cat, &src, &mut commit::no_hook);
    assert_eq!(r.state, RunState::Complete, "{r:?}");
    let rec = &r.reconcile;
    assert!(!rec.retry.is_empty() || !rec.finished.is_empty(), "{rec:?}");
    assert_whole(&card_path, &src, 2);
}
