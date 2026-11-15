//! Step 3: the catalogue in SQLite, the inventory snapshots on the cards, and the
//! rebuild from the cards (A22, A23, A28 to A31, and the engine's crash cases again with
//! a catalogue that outlives the process).
use super::catalogue::{Catalogue, CopyState};
use super::commit::{self, Point};
use super::inventory;
use super::rebuild;
use super::run::RunState;
use super::store::{SourceRow, Store, VolumeState};
use super::testkit::{
    bytes, cands, card, put, records, run, source, volume_id, Scratch, MIB, RESERVE_FLOOR,
};
use super::volume::Volume;
use std::fs;
use std::path::{Path, PathBuf};

const USABLE: u64 = 4 * MIB;

fn store_at(s: &Scratch) -> (PathBuf, Store) {
    let path = s.path.join("catalogue.db");
    let store = Store::open(&path).unwrap();
    (path, store)
}

fn add_source(store: &mut Store, src: &super::source::Source) {
    store
        .add_source(&SourceRow {
            source_id: src.source_id.clone(),
            display_name: src.display_name.clone(),
            kind: "immich-originals".to_string(),
            created_at: "2001-02-03T04:00:00Z".to_string(),
            retired_at: None,
        })
        .unwrap();
}

/// A run over every file of `src`, with the catalogue's view of the card noted.
fn run_on(
    store: &mut Store,
    vol: &Volume,
    card: &Path,
    usable: u64,
    src: &super::source::Source,
) -> super::run::RunResult {
    store
        .saw_volume(&vol.identity, RESERVE_FLOOR + usable, &super::format::now())
        .unwrap();
    run(vol, store, card, usable, &cands(src), &mut commit::no_hook)
}

#[test]
fn the_catalogue_outlives_the_process() {
    let s = Scratch::new("sq-keep");
    let src = source(&s, "photos", 1);
    for i in 0..3u8 {
        put(&src, &format!("2001/{i}.jpg"), &bytes(i, 2000));
    }
    let (p, v) = card(&s, 1);
    let (path, mut store) = store_at(&s);
    let r = run_on(&mut store, &v, &p, USABLE, &src);
    assert_eq!((r.state, r.committed.len()), (RunState::Complete, 3));
    drop(store);
    let mut store = Store::open(&path).unwrap();
    assert_eq!(store.version_count().unwrap(), 3);
    let r = run_on(&mut store, &v, &p, USABLE, &src);
    assert_eq!(
        (r.state, r.unchanged.len(), r.committed.len()),
        (RunState::Complete, 3, 0)
    );
    // Private to the server: the file is the owner's alone.
    use std::os::unix::fs::PermissionsExt;
    assert_eq!(
        fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o600
    );
}

#[test]
fn every_crash_point_recovers_with_the_catalogue_that_survived() {
    // The process dies at each step (the store is dropped with no cleanup, as a killed
    // process leaves it); the same catalogue file is opened again and the next run
    // finishes. Every file ends on the card exactly once, nothing stays in staging.
    for point in [
        Point::Begun,
        Point::IntentWritten,
        Point::PayloadStarted,
        Point::Checked,
        Point::VerifiedWritten,
        Point::Published,
        Point::RecordTempWritten,
        Point::RecordPublished,
        Point::CatalogueCommitted,
    ] {
        let s = Scratch::new("sq-crash");
        let src = source(&s, "photos", 1);
        put(&src, "a.jpg", &bytes(1, 3000));
        put(&src, "b.jpg", &bytes(2, 5000));
        let (p, v) = card(&s, 1);
        let (path, mut store) = store_at(&s);
        let mut hook = crash_at_second(point);
        let r = run(&v, &mut store, &p, USABLE, &cands(&src), &mut hook);
        assert_ne!(r.state, RunState::Complete, "{point:?}");
        drop(store);
        let mut store = Store::open(&path).unwrap();
        let r = run(
            &v,
            &mut store,
            &p,
            USABLE,
            &cands(&src),
            &mut commit::no_hook,
        );
        assert_eq!(r.state, RunState::Complete, "{point:?}: {r:?}");
        let recs = records(&p);
        assert_eq!(recs.len(), 2, "{point:?}: each file once");
        assert!(
            store.open_txs(&volume_id(1)).unwrap().is_empty(),
            "{point:?}"
        );
        for rec in &recs {
            let copy = store.copy(&rec.txn().record_id).unwrap().unwrap();
            assert_eq!(copy.state, CopyState::Committed, "{point:?}");
        }
        let staging = fs::read_dir(p.join("NekoBackup/staging")).unwrap().count();
        assert_eq!(staging, 0, "{point:?}");
    }
}

/// A crash at `point` of the second file: the first is committed before it.
fn crash_at_second(point: Point) -> impl FnMut(Point) -> super::error::Res<()> {
    let mut seen = 0;
    move |p| {
        if p == point {
            seen += 1;
            if seen == 2 {
                return Err(super::error::CardError::new(
                    super::error::Kind::Interrupted,
                    "simulated crash",
                ));
            }
        }
        Ok(())
    }
}

#[test]
fn a_search_names_the_card_that_holds_a_file() {
    let s = Scratch::new("sq-search");
    let src = source(&s, "photos", 1);
    put(&src, "2001/beach 100%.jpg", &bytes(1, 1000));
    put(&src, "2001/beach_x.jpg", &bytes(2, 1000));
    put(&src, "2002/city.jpg", &bytes(3, 1000));
    let (p, v) = card(&s, 1);
    let mut store = Store::in_memory().unwrap();
    run_on(&mut store, &v, &p, USABLE, &src);
    let found = store.search("beach", 10).unwrap();
    assert_eq!(found.len(), 2);
    let (copy, label) = &found[0].copies[0];
    assert_eq!(label.as_deref(), Some("Example set - 001"));
    assert_eq!(copy.state, CopyState::Committed);
    // `%` and `_` are what they are, not patterns.
    let percent = store.search("100%", 10).unwrap();
    assert_eq!(percent.len(), 1);
    assert_eq!(percent[0].version.path, "2001/beach 100%.jpg");
    assert_eq!(
        store.search("h_1", 10).unwrap().len(),
        0,
        "_ is no wildcard (\"h 1\" is there)"
    );
    assert!(store.search("nothing like it", 10).unwrap().is_empty());
}

#[test]
fn the_export_is_a_consistent_copy_and_never_overwrites() {
    let s = Scratch::new("sq-export");
    let src = source(&s, "photos", 1);
    put(&src, "a.jpg", &bytes(1, 1000));
    let (p, v) = card(&s, 1);
    let (_, mut store) = store_at(&s);
    run_on(&mut store, &v, &p, USABLE, &src);
    let target = s.path.join("catalogue-export.db");
    store.export(&target).unwrap();
    let copy = Store::open(&target).unwrap();
    assert_eq!(copy.version_count().unwrap(), 1);
    assert_eq!(copy.epoch().unwrap(), store.epoch().unwrap());
    let err = store.export(&target).unwrap_err();
    assert_eq!(err.kind, super::error::Kind::Exists);
    // The review of 0.51.0: the export, the paths of the person's files, was left as the
    // umask made it (0644): readable by every user of the server, for the host backup.
    use std::os::unix::fs::PermissionsExt;
    assert_eq!(
        fs::metadata(&target).unwrap().permissions().mode() & 0o777,
        0o600
    );
}

#[test]
fn a_catalogue_of_another_layout_is_not_used() {
    let s = Scratch::new("sq-layout");
    let (path, store) = store_at(&s);
    drop(store);
    let conn = rusqlite::Connection::open(&path).unwrap();
    conn.execute(
        "UPDATE meta SET value = '2' WHERE key = 'schema_version'",
        [],
    )
    .unwrap();
    drop(conn);
    let err = Store::open(&path).err().unwrap();
    assert_eq!(err.kind, super::error::Kind::Unsupported);
}

#[test]
fn an_inventory_snapshot_round_trips_and_counts_up() {
    let s = Scratch::new("inv-round");
    let src = source(&s, "photos", 1);
    put(&src, "a.jpg", &bytes(1, 1000));
    put(&src, "b.jpg", &bytes(2, 1000));
    let (p, v) = card(&s, 1);
    let mut store = Store::in_memory().unwrap();
    add_source(&mut store, &src);
    run_on(&mut store, &v, &p, USABLE, &src);
    let first = inventory::write(&v, &mut store, &super::format::now()).unwrap();
    let second = inventory::write(&v, &mut store, &super::format::now()).unwrap();
    assert_ne!(first, second, "a new snapshot every time, none overwritten");
    let found = inventory::read(&v).unwrap();
    assert!(
        found.invalid.is_empty() && found.headless.is_empty(),
        "{found:?}"
    );
    let mut revisions: Vec<u64> = found.valid.iter().map(|h| h.revision).collect();
    revisions.sort();
    assert_eq!(revisions, vec![1, 2]);
    let header = &found.valid[0];
    assert_eq!(header.sources.len(), 1);
    assert_eq!(header.volumes[0].record_count, 2);
    let mut lines = Vec::new();
    inventory::each_version(&v, header, |l| {
        lines.push(l);
        Ok(())
    })
    .unwrap();
    assert_eq!(lines.len(), 2);
    assert_eq!(lines[0].copies[0].volume_id, volume_id(1));
    // Nothing of the snapshot is left in staging.
    assert_eq!(
        fs::read_dir(p.join("NekoBackup/staging")).unwrap().count(),
        0
    );
}

#[test]
fn a_damaged_or_unfinished_inventory_is_refused_and_left_alone() {
    let s = Scratch::new("inv-bad");
    let src = source(&s, "photos", 1);
    put(&src, "a.jpg", &bytes(1, 1000));
    let (p, v) = card(&s, 1);
    let mut store = Store::in_memory().unwrap();
    run_on(&mut store, &v, &p, USABLE, &src);
    let id = inventory::write(&v, &mut store, &super::format::now()).unwrap();
    let inv = p.join("NekoBackup/inventory");
    let list = inv.join(format!("{id}.versions.jsonl"));
    let mut bytes_now = fs::read(&list).unwrap();
    bytes_now[10] ^= 1;
    fs::write(&list, &bytes_now).unwrap();
    let found = inventory::read(&v).unwrap();
    assert!(found.valid.is_empty());
    assert_eq!(found.invalid.len(), 1, "{found:?}");
    // A version list whose header never came (an interrupted checkpoint).
    let other = inventory::write(&v, &mut store, &super::format::now()).unwrap();
    fs::remove_file(inv.join(format!("{other}.json"))).unwrap();
    let found = inventory::read(&v).unwrap();
    assert_eq!(found.headless, vec![format!("{other}.versions.jsonl")]);
    assert!(
        inv.join(format!("{other}.versions.jsonl")).exists(),
        "left in place"
    );
    // A temporary file in staging is this engine's own: removed at the next attach.
    fs::write(
        p.join(format!("NekoBackup/staging/{other}.inventory.tmp")),
        b"{",
    )
    .unwrap();
    let report = super::reconcile::reconcile(&v, &mut store).unwrap();
    assert_eq!(report.notes.len(), 1);
    assert_eq!(
        fs::read_dir(p.join("NekoBackup/staging")).unwrap().count(),
        0
    );
}

/// Two cards: card 1 fills (snapshot at the swap, it knows card 1 only), card 2 takes the
/// rest (snapshot at the end, it knows both).
struct TwoCards {
    s: Scratch,
    src: super::source::Source,
    p1: PathBuf,
    p2: PathBuf,
    v1: Volume,
    v2: Volume,
    store: Store,
}

fn two_cards(name: &str) -> TwoCards {
    let s = Scratch::new(name);
    let src = source(&s, "photos", 1);
    for i in 0..5u8 {
        put(&src, &format!("2001/{i}.jpg"), &bytes(i, 300 * 1024));
    }
    let mut store = Store::in_memory().unwrap();
    add_source(&mut store, &src);
    let (p1, v1) = card(&s, 1);
    let r1 = run_on(&mut store, &v1, &p1, 1000 * 1024, &src);
    assert_eq!(r1.state, RunState::WaitingForNextCard);
    inventory::write(&v1, &mut store, &super::format::now()).unwrap();
    let (p2, v2) = card(&s, 2);
    let r2 = run_on(&mut store, &v2, &p2, USABLE, &src);
    assert_eq!(r2.state, RunState::Complete);
    inventory::write(&v2, &mut store, &super::format::now()).unwrap();
    TwoCards {
        s,
        src,
        p1,
        p2,
        v1,
        v2,
        store,
    }
}

#[test]
fn a_lost_catalogue_is_rebuilt_from_the_cards() {
    // A28: every record of both cards comes back, hashed; a run afterwards copies nothing.
    let t = two_cards("rb-all");
    let before = t.store.epoch().unwrap();
    let mut fresh = Store::in_memory().unwrap();
    let report = rebuild::rebuild(&mut fresh, &[&t.v1, &t.v2]).unwrap();
    let total: usize = report.imported.values().sum();
    assert_eq!(total, 5);
    assert!(
        report.damaged.is_empty() && report.quarantined.is_empty() && report.conflicts.is_empty()
    );
    assert!(report.unobserved_volumes.is_empty());
    assert_ne!(
        fresh.epoch().unwrap(),
        before,
        "a rebuilt catalogue is a new epoch"
    );
    assert_eq!(fresh.sources().unwrap().len(), 1);
    let r = run_on(&mut fresh, &t.v2, &t.p2, USABLE, &t.src);
    assert_eq!(r.state, RunState::Complete);
    assert!(r.committed.is_empty(), "nothing copied again: {r:?}");
    // The old catalogue is untouched by the rebuild of another.
    assert_eq!(t.store.epoch().unwrap(), before);
}

#[test]
fn a_lost_catalogue_and_a_missing_card_say_what_is_not_known() {
    // A29 (a): card 2 only. Its inventory knows card 1's files; those copies are
    // historical, not protection, and the coverage statement says what cannot be known.
    let t = two_cards("rb-missing");
    let mut fresh = Store::in_memory().unwrap();
    let report = rebuild::rebuild(&mut fresh, &[&t.v2]).unwrap();
    assert_eq!(report.unobserved_volumes, vec![volume_id(1)]);
    let on_card_1 = records(&t.p1).len();
    assert_eq!(report.historical_copies, on_card_1);
    let unprotected = fresh.unprotected().unwrap();
    assert_eq!(
        unprotected.len(),
        on_card_1,
        "card 1's files are not counted as protected"
    );
    assert!(
        unprotected.iter().all(|(_, unrecoverable)| !unrecoverable),
        "only unobserved, not lost"
    );
    let card1 = fresh.volume(&volume_id(1)).unwrap().unwrap();
    assert!(!card1.observed);
    assert_eq!(card1.state, VolumeState::Unavailable);
    assert!(report.unknown_coverage().contains("is not known"));
    // A29 (b): card 1 only. Its inventory was written before card 2 existed: card 2 and
    // its files are simply unknown, which the statement says.
    let mut other = Store::in_memory().unwrap();
    let report = rebuild::rebuild(&mut other, &[&t.v1]).unwrap();
    assert!(other.volume(&volume_id(2)).unwrap().is_none());
    assert_eq!(other.version_count().unwrap() as usize, on_card_1);
    assert_eq!(report.newest.len(), 1);
    assert!(report
        .unknown_coverage()
        .contains("after the newest of these is not known"));
}

#[test]
fn a_lost_card_is_never_brought_back_by_another_history() {
    // A30: a later catalogue epoch with a higher revision calls card 2 in use; the
    // original catalogue marked it lost afterwards. Revisions of different epochs are not
    // compared and no date decides: card 2 stays lost, its copies count for nothing.
    let mut t = two_cards("rb-epochs");
    let mut second = Store::in_memory().unwrap();
    rebuild::rebuild(&mut second, &[&t.v1, &t.v2]).unwrap();
    for _ in 0..5 {
        second.take_revision().unwrap();
    }
    inventory::write(&t.v2, &mut second, &super::format::now()).unwrap();
    t.store
        .set_volume_state(&volume_id(2), VolumeState::Lost, &super::format::now())
        .unwrap();
    inventory::write(&t.v1, &mut t.store, &super::format::now()).unwrap();
    let mut fresh = Store::in_memory().unwrap();
    let report = rebuild::rebuild(&mut fresh, &[&t.v1]).unwrap();
    assert_eq!(
        fresh.volume(&volume_id(2)).unwrap().unwrap().state,
        VolumeState::Lost
    );
    assert_eq!(report.newest.len(), 1, "only card 1's epoch was read");
    // Even with card 2 attached: found again, it needs a person's reinstatement first.
    let mut both = Store::in_memory().unwrap();
    let report = rebuild::rebuild(&mut both, &[&t.v1, &t.v2]).unwrap();
    assert_eq!(
        report.newest.len(),
        2,
        "two histories, each with its own revisions"
    );
    assert_eq!(
        both.volume(&volume_id(2)).unwrap().unwrap().state,
        VolumeState::Lost
    );
    for c in both.copies_on(&volume_id(2)).unwrap() {
        assert!(!c.state.protects());
    }
}

#[test]
fn a_version_described_two_ways_is_kept_as_a_conflict() {
    // A30: a forged inventory gives a version of card 1 another checksum. The card's own
    // record wins nothing by date either: it is what the card holds, and the snapshot's
    // other description is kept as a conflict for a person.
    let t = two_cards("rb-conflict");
    let rec = records(&t.p1)[0].clone();
    // A snapshot on card 2 whose line gives card 1's version another checksum.
    let mut liar = Store::in_memory().unwrap();
    let mut lie = rec.clone();
    lie.meta.checksum = "f".repeat(64);
    liar.import_copy(&lie, CopyState::Committed, None).unwrap();
    inventory::write(&t.v2, &mut liar, &super::format::now()).unwrap();
    let mut fresh = Store::in_memory().unwrap();
    let report = rebuild::rebuild(&mut fresh, &[&t.v1, &t.v2]).unwrap();
    assert_eq!(report.conflicts.len(), 1, "{report:?}");
    assert_eq!(fresh.conflicts().unwrap().len(), 1);
    let version = fresh.version(&rec.txn().version_id).unwrap().unwrap();
    assert_eq!(
        version.checksum, rec.meta.checksum,
        "the card's own record is kept"
    );
    let copy = fresh.copy(&rec.txn().record_id).unwrap().unwrap();
    assert_eq!(copy.state, CopyState::Committed);
}

#[test]
fn a_version_whose_only_copy_is_lost_is_unrecoverable() {
    // A31: card 1 lost; the versions only it held are named as not recoverable from any
    // known copy (the live files may have changed since; they are no replacement).
    let mut t = two_cards("rb-unrecoverable");
    t.store
        .set_volume_state(&volume_id(1), VolumeState::Lost, &super::format::now())
        .unwrap();
    let unprotected = t.store.unprotected().unwrap();
    assert_eq!(unprotected.len(), records(&t.p1).len());
    assert!(unprotected.iter().all(|(_, unrecoverable)| *unrecoverable));
    for c in t.store.copies_on(&volume_id(1)).unwrap() {
        assert_eq!(c.state, CopyState::Lost);
    }
}

#[test]
fn damaged_and_foreign_records_are_counted_for_nothing() {
    // A25, A27 on a rebuild: a payload with other bytes is damaged (its record kept, its
    // checksum not rewritten); a record file that contradicts its card is quarantined.
    let t = two_cards("rb-damaged");
    let recs = records(&t.p1);
    let target =
        t.p1.join("NekoBackup")
            .join(&recs[0].txn().archive_relative_path);
    let mut bytes_now = fs::read(&target).unwrap();
    bytes_now[0] ^= 0xff;
    fs::write(&target, &bytes_now).unwrap();
    let hh = &recs[1].txn().record_id[..2];
    let foreign = t.p1.join(format!(
        "NekoBackup/records/{hh}/bbbbbbbb-0000-4000-8000-000000000999.json"
    ));
    fs::write(&foreign, b"{\"schema_version\": 1}\n").unwrap();
    let mut fresh = Store::in_memory().unwrap();
    let report = rebuild::rebuild(&mut fresh, &[&t.v1]).unwrap();
    assert_eq!(report.damaged, vec![recs[0].txn().record_id.clone()]);
    assert_eq!(report.quarantined.len(), 1, "{report:?}");
    let version = fresh.version(&recs[0].txn().version_id).unwrap().unwrap();
    assert_eq!(
        version.checksum, recs[0].meta.checksum,
        "never rewritten to match"
    );
    assert!(foreign.exists(), "left in place");
}
