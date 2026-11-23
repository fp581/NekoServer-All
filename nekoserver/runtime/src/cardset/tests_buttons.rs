//! The Backup cards page's two buttons (0.52.4), on the engine: "Take these cards back"
//! (a lost catalogue rebuilt from the set's own cards and given their set) and "Check this
//! card" (every copy read again; a damaged one marked, and copied again by the next run).
use super::catalogue::{Catalogue, CopyState};
use super::commit;
use super::format;
use super::inventory;
use super::rebuild;
use super::recheck;
use super::run::RunState;
use super::store::{SourceRow, Store};
use super::testkit::{
    bytes, cands, card, payload, put, records, run, source, volume_id, Scratch, MIB, RESERVE_FLOOR,
    SET,
};
use super::volume::Volume;
use std::fs;
use std::path::Path;

const USABLE: u64 = 4 * MIB;

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

fn run_on(
    store: &mut Store,
    vol: &Volume,
    card: &Path,
    src: &super::source::Source,
) -> super::run::RunResult {
    store
        .saw_volume(&vol.identity, RESERVE_FLOOR + USABLE, &format::now())
        .unwrap();
    run(vol, store, card, USABLE, &cands(src), &mut commit::no_hook)
}

/// One card of the set with four photos on it and its inventory.
fn one_card(s: &Scratch) -> (super::source::Source, std::path::PathBuf, Volume, Store) {
    let src = source(s, "photos", 1);
    for i in 0..4u8 {
        put(&src, &format!("2001/{i}.jpg"), &bytes(i, 200 * 1024));
    }
    let mut store = Store::in_memory().unwrap();
    store.take_set(SET, "Example set").unwrap();
    add_source(&mut store, &src);
    let (p, v) = card(s, 1);
    assert_eq!(run_on(&mut store, &v, &p, &src).state, RunState::Complete);
    inventory::write(&v, &mut store, &format::now()).unwrap();
    (src, p, v, store)
}

#[test]
fn cards_taken_back_give_the_server_their_set_and_every_file_again() {
    let s = Scratch::new("bt-take");
    let (src, p, v, _lost) = one_card(&s);
    // The SSD was replaced: a new, empty catalogue.
    let mut fresh = Store::in_memory().unwrap();
    assert_eq!(fresh.backup_set().unwrap(), None);
    fresh.take_set(SET, "Example set").unwrap();
    let report = rebuild::rebuild(&mut fresh, &[&v]).unwrap();
    assert_eq!(report.imported.values().sum::<usize>(), 4);
    assert_eq!(fresh.backup_set().unwrap().unwrap().0, SET);
    assert_eq!(fresh.copies_on(&volume_id(1)).unwrap().len(), 4);
    // The next run writes on: nothing is copied again, a new photo is.
    put(&src, "2001/new.jpg", &bytes(9, 1000));
    let r = run_on(&mut fresh, &v, &p, &src);
    assert_eq!(r.state, RunState::Complete);
    assert_eq!(r.committed.len(), 1, "only the new photo: {r:?}");
    // A server that has cards of its own set never takes another set.
    assert!(fresh.take_set(super::testkit::OTHER_SET, "Mine").is_err());
}

#[test]
fn a_check_marks_a_damaged_copy_and_the_next_run_copies_it_again() {
    let s = Scratch::new("bt-check");
    let (src, p, v, mut store) = one_card(&s);
    let all = records(&p);
    // One byte of one copy changes on the card (an SD card that went bad).
    let bad = &all[1];
    let path = p.join(format::ROOT).join(&bad.txn().archive_relative_path);
    let mut b = payload(&p, bad);
    b[10] ^= 0xff;
    fs::write(&path, b).unwrap();
    let mut seen = vec![];
    let checked =
        recheck::check(&mut store, &v, &mut |done, total| seen.push((done, total))).unwrap();
    assert_eq!(checked.fine, 3);
    assert_eq!(
        checked.damaged,
        vec![bad.txn().original_relative_path.clone()]
    );
    assert_eq!(checked.waiting, checked.damaged, "no other card holds it");
    assert_eq!(seen.last(), Some(&(4, 4)));
    let state = |store: &Store, id: &str| {
        store
            .copies_on(&volume_id(1))
            .unwrap()
            .into_iter()
            .find(|c| c.record_id == id)
            .unwrap()
            .state
    };
    assert_eq!(state(&store, &bad.txn().record_id), CopyState::Damaged);
    let card = store.volume(&volume_id(1)).unwrap().unwrap();
    assert_eq!(card.last_recheck_result.as_deref(), Some("1 damaged"));
    // The next run copies that file again, and only that one.
    let r = run_on(&mut store, &v, &p, &src);
    assert_eq!(r.state, RunState::Complete);
    assert_eq!(r.committed.len(), 1, "{r:?}");
    let version = store
        .newest_version(&src.source_id, &bad.txn().original_relative_path)
        .unwrap()
        .unwrap();
    assert!(store
        .copies_of(&version.version_id)
        .unwrap()
        .iter()
        .any(|c| c.state == CopyState::Committed));
    // Checked again: the old copy is still damaged, but the file waits for nothing.
    let again = recheck::check(&mut store, &v, &mut |_, _| {}).unwrap();
    assert_eq!(again.damaged, checked.damaged);
    assert!(again.waiting.is_empty(), "{again:?}");
}

#[test]
fn a_check_of_a_card_taken_out_marks_nothing() {
    let s = Scratch::new("bt-pulled");
    let (_src, p, v, mut store) = one_card(&s);
    let bad = &records(&p)[0];
    let path = p.join(format::ROOT).join(&bad.txn().archive_relative_path);
    fs::write(&path, b"changed").unwrap();
    // The card's folder goes away under the check.
    fs::rename(&p, s.path.join("elsewhere")).unwrap();
    assert!(recheck::check(&mut store, &v, &mut |_, _| {}).is_err());
    assert!(store
        .copies_on(&volume_id(1))
        .unwrap()
        .iter()
        .all(|c| c.state == CopyState::Committed));
}

#[test]
fn a_copy_that_cannot_be_read_is_damaged_and_the_check_goes_on() {
    let s = Scratch::new("bt-unreadable");
    let (_src, p, v, mut store) = one_card(&s);
    let bad = &records(&p)[3];
    let path = p.join(format::ROOT).join(&bad.txn().archive_relative_path);
    // A payload that can't be read as a file stands for a bad sector.
    fs::remove_file(&path).unwrap();
    fs::create_dir(&path).unwrap();
    let checked = recheck::check(&mut store, &v, &mut |_, _| {}).unwrap();
    assert_eq!(checked.fine, 3);
    assert_eq!(
        checked.damaged,
        vec![bad.txn().original_relative_path.clone()]
    );
}

#[test]
fn a_copy_whose_record_is_gone_is_marked_damaged() {
    let s = Scratch::new("bt-record");
    let (_src, p, v, mut store) = one_card(&s);
    let gone = &records(&p)[2];
    fs::remove_file(
        p.join(format::ROOT)
            .join(format::RECORDS)
            .join(gone.txn().hh())
            .join(gone.txn().record_name()),
    )
    .unwrap();
    let checked = recheck::check(&mut store, &v, &mut |_, _| {}).unwrap();
    assert_eq!(checked.fine, 3);
    assert_eq!(
        checked.damaged,
        vec![gone.txn().original_relative_path.clone()]
    );
}
