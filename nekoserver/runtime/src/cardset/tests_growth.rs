//! 0.52.0: how the inventory snapshots on a card grow (decision doc
//! card-inventory-growth.md, both options): a new snapshot only when the catalogue
//! changed since the last one on that card, and only the newest few kept, removed
//! through staging/. A removal cut off at any step leaves a readable card.
use super::commit;
use super::error::{CardError, Kind};
use super::inventory::{self, Checkpoint};
use super::reconcile;
use super::store::Store;
use super::testkit::{bytes, cands, card, put, run, source, Scratch, MIB, RESERVE_FLOOR};
use super::volume::Volume;
use std::fs;
use std::path::Path;
use std::process::Command;

const USABLE: u64 = 4 * MIB;
const KEEP: usize = 3;

fn names(card: &Path, folder: &str) -> Vec<String> {
    let mut found: Vec<String> = fs::read_dir(card.join("NekoBackup").join(folder))
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    found.sort();
    found
}

/// One more file of the source on the card, and its catalogue rows.
fn add_file(store: &mut Store, vol: &Volume, card: &Path, src: &super::source::Source, n: u8) {
    put(src, &format!("2001/{n}.jpg"), &bytes(n, 1500));
    store
        .saw_volume(&vol.identity, RESERVE_FLOOR + USABLE, &super::format::now())
        .unwrap();
    run(vol, store, card, USABLE, &cands(src), &mut commit::no_hook);
}

fn checkpoint(store: &mut Store, vol: &Volume) -> Checkpoint {
    inventory::checkpoint(vol, store, &super::format::now(), KEEP).unwrap()
}

#[test]
fn a_new_snapshot_is_written_only_when_the_catalogue_changed() {
    let s = Scratch::new("grow-changed");
    let src = source(&s, "photos", 1);
    let (p, v) = card(&s, 1);
    let mut store = Store::in_memory().unwrap();
    add_file(&mut store, &v, &p, &src, 1);
    let Checkpoint::Written(first) = checkpoint(&mut store, &v) else {
        panic!("the first checkpoint writes a snapshot");
    };
    // Nothing new: the same snapshot stays, and nothing is added to the card.
    let before = names(&p, "inventory");
    assert_eq!(
        checkpoint(&mut store, &v),
        Checkpoint::Unchanged(first.clone())
    );
    // A card seen again, and a file whose dates changed with the same content: a
    // snapshot says neither, so neither is a reason for a new one.
    add_file(&mut store, &v, &p, &src, 1);
    let version = store.all_versions().unwrap()[0].version_id.clone();
    super::catalogue::Catalogue::note_dates(&mut store, &version, 7, 8).unwrap();
    assert_eq!(
        checkpoint(&mut store, &v),
        Checkpoint::Unchanged(first.clone())
    );
    assert_eq!(names(&p, "inventory"), before);
    // A new file on the card is: a new snapshot.
    add_file(&mut store, &v, &p, &src, 2);
    assert!(matches!(checkpoint(&mut store, &v), Checkpoint::Written(id) if id != first));
    // The snapshot the catalogue remembers is gone from the card (removed by hand, or
    // cut short by a power cut): a new one is written, though nothing changed.
    let Checkpoint::Unchanged(now) = checkpoint(&mut store, &v) else {
        panic!("nothing changed");
    };
    fs::remove_file(p.join(format!("NekoBackup/inventory/{now}.json"))).unwrap();
    assert!(matches!(checkpoint(&mut store, &v), Checkpoint::Written(_)));
}

#[test]
fn each_card_keeps_only_its_newest_three_snapshots() {
    let s = Scratch::new("grow-keep");
    let src = source(&s, "photos", 1);
    let (p, v) = card(&s, 1);
    let mut store = Store::in_memory().unwrap();
    let mut written = vec![];
    for n in 1..=6 {
        add_file(&mut store, &v, &p, &src, n);
        if let Checkpoint::Written(id) = checkpoint(&mut store, &v) {
            written.push(id);
        }
    }
    assert_eq!(written.len(), 6);
    let found = inventory::read(&v).unwrap();
    let mut kept: Vec<String> = found.valid.iter().map(|h| h.snapshot_id.clone()).collect();
    kept.sort();
    let mut newest = written[3..].to_vec();
    newest.sort();
    assert_eq!(kept, newest, "the three written last");
    assert!(found.headless.is_empty() && found.invalid.is_empty());
    assert_eq!(names(&p, "inventory").len(), 2 * KEEP);
    assert!(names(&p, "staging").is_empty(), "nothing left in staging");
}

#[test]
fn a_cleanup_cut_off_is_finished_by_the_next_run_even_when_nothing_changed() {
    let s = Scratch::new("grow-unfinished");
    let src = source(&s, "photos", 1);
    let (p, v) = card(&s, 1);
    let mut store = Store::in_memory().unwrap();
    // Four snapshots and no cleanup after them: as a run that stopped (a power cut, the
    // card taken out) between writing its snapshot and removing the oldest leaves them.
    for n in 1..=4 {
        add_file(&mut store, &v, &p, &src, n);
        inventory::checkpoint(&v, &mut store, &super::format::now(), 10).unwrap();
    }
    assert_eq!(inventory::read(&v).unwrap().valid.len(), 4);
    // The next run has nothing new to write, and still keeps only the newest three.
    assert!(matches!(
        checkpoint(&mut store, &v),
        Checkpoint::Unchanged(_)
    ));
    assert_eq!(inventory::read(&v).unwrap().valid.len(), KEEP);
    assert!(names(&p, "staging").is_empty(), "nothing left in staging");
}

#[test]
fn a_catalogue_made_before_the_change_counter_gets_it() {
    let s = Scratch::new("grow-old");
    let path = s.path.join("catalogue.db");
    drop(Store::open(&path).unwrap());
    // As 0.51 made it: no triggers, no counter, no marks.
    let conn = rusqlite::Connection::open(&path).unwrap();
    let triggers: Vec<String> = conn
        .prepare("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        .unwrap()
        .query_map([], |r| r.get(0))
        .unwrap()
        .map(Result::unwrap)
        .collect();
    assert!(!triggers.is_empty());
    for t in triggers {
        conn.execute_batch(&format!("DROP TRIGGER {t}")).unwrap();
    }
    conn.execute_batch("DELETE FROM meta WHERE key = 'content'; DROP TABLE inventory_marks;")
        .unwrap();
    drop(conn);
    let mut store = Store::open(&path).unwrap();
    assert_eq!(store.content().unwrap().1, 0);
    let src = source(&s, "photos", 1);
    let (p, v) = card(&s, 1);
    add_file(&mut store, &v, &p, &src, 1);
    assert!(store.content().unwrap().1 > 0);
    assert!(matches!(checkpoint(&mut store, &v), Checkpoint::Written(_)));
}

/// The tool on the card says which card held a file, from the newest snapshot it reads.
fn tool_where(card: &Path) -> String {
    let out = Command::new("python3")
        .arg(card.join("NekoBackup/tools/neko-restore.py"))
        .args(["where", "2001"])
        .output()
        .expect("python3 runs the restore tool");
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8_lossy(&out.stdout).into_owned()
}

#[test]
fn a_removal_cut_off_at_any_step_leaves_a_readable_snapshot() {
    // Each step of removing the oldest of four snapshots (header set aside, removed;
    // version list set aside, removed): the process stops there, as at a power cut.
    for cut in 0..4 {
        let s = Scratch::new(&format!("grow-cut-{cut}"));
        let src = source(&s, "photos", 1);
        let (p, v) = card(&s, 1);
        let mut store = Store::in_memory().unwrap();
        let mut written = vec![];
        for n in 1..=4 {
            add_file(&mut store, &v, &p, &src, n);
            written.push(inventory::write(&v, &mut store, &super::format::now()).unwrap());
        }
        let mut steps = 0;
        let mut stop = |_: &str| {
            steps += 1;
            if steps > cut {
                return Err(CardError::new(Kind::Interrupted, "simulated crash"));
            }
            Ok(())
        };
        let e = inventory::prune(&v, KEEP, &mut stop).unwrap_err();
        assert!(e.is_crash());
        // The newest snapshot is whole, and the card's own tool reads it.
        let found = inventory::read(&v).unwrap();
        assert!(
            found.valid.iter().any(|h| h.snapshot_id == written[3]),
            "cut {cut}"
        );
        assert!(found.invalid.is_empty(), "cut {cut}: {found:?}");
        assert!(tool_where(&p).contains("2001/4.jpg"), "cut {cut}");
        // The next run tidies up: recovery removes what was set aside in staging, and
        // the next removal finishes the job.
        let attached = super::testkit::attach(&p, 1);
        reconcile::reconcile(&attached, &mut store).unwrap();
        inventory::prune(&attached, KEEP, &mut |_| Ok(())).unwrap();
        let found = inventory::read(&attached).unwrap();
        assert_eq!(found.valid.len(), KEEP, "cut {cut}");
        assert!(found.headless.is_empty(), "cut {cut}: {found:?}");
        assert!(names(&p, "staging").is_empty(), "cut {cut}");
        assert_eq!(names(&p, "inventory").len(), 2 * KEEP);
    }
}
