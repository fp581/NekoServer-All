//! A crash at every point of the commit protocol, then recovery (format document
//! section 10; acceptance cases A18 to A25). These are simulated crashes: the engine
//! stops where a killed process would; they are not power-loss tests.
use super::catalogue::{Catalogue, CopyState, Memory};
use super::commit::{self, Job, Outcome, Point};
use super::error::Kind;
use super::format;
use super::reconcile::{self, Report};
use super::run::RunState;
use super::source::Candidate;
use super::testkit::*;
use super::volume::Volume;
use std::fs;
use std::path::{Path, PathBuf};

const USABLE: u64 = 4 * MIB;

struct Case {
    _s: Scratch,
    card: PathBuf,
    cat: Memory,
    first: Candidate,
    second: Candidate,
    second_bytes: Vec<u8>,
}

/// A card with one committed file, then a second file stopped at `point` by `hook`.
fn stopped(
    name: &str,
    hook: &mut dyn commit::Hook,
) -> (Case, Result<Outcome, super::error::CardError>) {
    let s = Scratch::new(name);
    let (card_path, vol) = card(&s, 1);
    let src = source(&s, "photos", 1);
    put(&src, "2001/first.jpg", &bytes(1, 3000));
    let second_bytes = bytes(2, 70_000);
    put(&src, "2001/second.jpg", &second_bytes);
    let all = cands(&src);
    let mut cat = Memory::default();
    let r = run(
        &vol,
        &mut cat,
        &card_path,
        USABLE,
        &all[..1],
        &mut commit::no_hook,
    );
    assert_eq!(r.state, RunState::Complete);
    let job = Job {
        cand: &all[1],
        run_id: "dddddddd-0000-4000-8000-000000000002",
        replace: None,
    };
    let result = commit::commit_file(&vol, &mut cat, &job, hook);
    let case = Case {
        _s: s,
        card: card_path,
        cat,
        first: all[0].clone(),
        second: all[1].clone(),
        second_bytes,
    };
    (case, result)
}

fn crashed(name: &str, point: Point) -> Case {
    let (case, result) = stopped(name, &mut crash_at(point));
    assert_eq!(result.unwrap_err().kind, Kind::Interrupted);
    case
}

fn recover(case: &mut Case) -> Report {
    let vol = attach(&case.card, 1);
    let report = reconcile::reconcile(&vol, &mut case.cat).unwrap();
    // a second pass changes nothing
    let again = reconcile::reconcile(&vol, &mut case.cat).unwrap();
    assert_eq!(again, Report::default(), "reconciliation is not idempotent");
    report
}

fn staging(card: &Path) -> Vec<String> {
    files(card, format::STAGING)
}

/// After recovery the card holds the second file exactly once, committed and counted,
/// the first file untouched, and a new run copies nothing.
fn assert_committed_once(case: &mut Case) {
    let recs = records(&case.card);
    assert_eq!(recs.len(), 2);
    assert_eq!(payload(&case.card, &recs[1]), case.second_bytes);
    assert_eq!(payload(&case.card, &recs[0]), bytes(1, 3000));
    assert_eq!(
        files(&case.card, format::OBJECTS).len(),
        2,
        "no second payload"
    );
    assert!(staging(&case.card).is_empty());
    assert!(case.cat.txs.is_empty());
    let copy = case.cat.copy(&recs[1].txn().record_id).unwrap();
    assert_eq!(copy.state, CopyState::Committed);
    let vol = attach(&case.card, 1);
    let both = [case.first.clone(), case.second.clone()];
    let r = run(
        &vol,
        &mut case.cat,
        &case.card,
        USABLE,
        &both,
        &mut commit::no_hook,
    );
    assert_eq!(r.state, RunState::Complete);
    assert_eq!(r.unchanged.len(), 2);
    assert!(r.committed.is_empty());
}

/// After recovery nothing of the second file is on the card or counted; a new run
/// copies it from the start.
fn assert_copied_again(case: &mut Case, report: &Report) {
    assert!(report.retry.contains(&case.second.path), "{report:?}");
    assert_eq!(records(&case.card).len(), 1);
    assert!(staging(&case.card).is_empty(), "{:?}", staging(&case.card));
    assert!(case.cat.txs.is_empty());
    let vol = attach(&case.card, 1);
    let both = [case.first.clone(), case.second.clone()];
    let r = run(
        &vol,
        &mut case.cat,
        &case.card,
        USABLE,
        &both,
        &mut commit::no_hook,
    );
    assert_eq!(r.state, RunState::Complete);
    assert_eq!(r.committed.len(), 1);
    assert_eq!(records(&case.card).len(), 2);
}

#[test]
fn crash_after_the_server_row() {
    let mut case = crashed("d1", Point::Begun);
    assert_eq!(case.cat.txs.len(), 1);
    let report = recover(&mut case);
    assert_eq!(report.cleaned.len(), 1);
    assert_copied_again(&mut case, &report);
}

#[test]
fn crash_after_the_intent() {
    let mut case = crashed("d2", Point::IntentWritten);
    assert_eq!(staging(&case.card).len(), 1);
    let report = recover(&mut case);
    assert_copied_again(&mut case, &report);
}

#[test]
fn crash_in_the_middle_of_the_payload() {
    // A18: a partial is never complete; the file starts again, the others are untouched.
    let mut case = crashed("d3-mid", Point::PayloadStarted);
    assert!(staging(&case.card).iter().any(|n| n.ends_with(".partial")));
    let report = recover(&mut case);
    assert_copied_again(&mut case, &report);
    assert_eq!(payload(&case.card, &records(&case.card)[0]), bytes(1, 3000));
}

#[test]
fn crash_after_the_payload_and_after_the_check() {
    for point in [Point::PayloadWritten, Point::Checked] {
        let mut case = crashed("d3", point);
        let report = recover(&mut case);
        assert_copied_again(&mut case, &report);
    }
}

#[test]
fn crash_after_the_verified_intent_continues_at_publication() {
    // A19: the partial is hashed again before it is published.
    let mut case = crashed("d4", Point::VerifiedWritten);
    assert_eq!(staging(&case.card).len(), 3);
    let report = recover(&mut case);
    assert_eq!(report.finished.len(), 1);
    assert_committed_once(&mut case);
}

#[test]
fn a_partial_damaged_after_the_verified_intent_is_not_published() {
    let mut case = crashed("d4-bad", Point::VerifiedWritten);
    let partial = staging(&case.card)
        .into_iter()
        .find(|n| n.ends_with(".partial"))
        .unwrap();
    let p = case
        .card
        .join(format::ROOT)
        .join(format::STAGING)
        .join(partial);
    let mut b = fs::read(&p).unwrap();
    b[10] ^= 0xff;
    fs::write(&p, b).unwrap();
    let report = recover(&mut case);
    assert!(report.finished.is_empty());
    assert!(report
        .notes
        .iter()
        .any(|n| n.contains("verification failed")));
    assert_copied_again(&mut case, &report);
}

#[test]
fn crash_after_publication_writes_the_record_without_a_recopy() {
    // A20: final payload, verified intent, no record.
    let mut case = crashed("d5", Point::Published);
    assert_eq!(files(&case.card, format::OBJECTS).len(), 2);
    let report = recover(&mut case);
    assert_eq!(report.finished.len(), 1);
    assert_committed_once(&mut case);
}

#[test]
fn a_published_payload_without_trusted_metadata_is_an_orphan() {
    // A20, second half: the payload is kept where it is, never counted.
    for damage in ["payload", "verified"] {
        let mut case = crashed("d5-orphan", Point::Published);
        let stage = case.card.join(format::ROOT).join(format::STAGING);
        let verified = staging(&case.card)
            .into_iter()
            .find(|n| n.ends_with(".verified.json"))
            .unwrap();
        if damage == "verified" {
            fs::write(stage.join(&verified), b"{\"torn").unwrap();
        } else {
            let obj = case.card.join(format::ROOT).join(format::OBJECTS);
            let target = fs::read_dir(&obj).unwrap().flatten().find_map(|d| {
                fs::read_dir(d.path())
                    .unwrap()
                    .flatten()
                    .map(|f| f.path())
                    .find(|p| fs::read(p).unwrap().len() == case.second_bytes.len())
            });
            fs::write(target.unwrap(), b"something else").unwrap();
        }
        let vol = attach(&case.card, 1);
        let report = reconcile::reconcile(&vol, &mut case.cat).unwrap();
        assert_eq!(report.orphans.len(), 1, "{damage}");
        assert_eq!(case.cat.orphan_rows.len(), 1);
        assert!(case.cat.txs.is_empty());
        // the evidence stays, and a later pass leaves it alone
        assert_eq!(staging(&case.card).len(), 2);
        assert_eq!(
            reconcile::reconcile(&vol, &mut case.cat).unwrap(),
            Report::default()
        );
        assert!(reconcile::staging_clear(&vol, &case.cat).unwrap());
        // the source is copied again under a new record; the orphan stays on the card
        let both = [case.first.clone(), case.second.clone()];
        let r = run(
            &vol,
            &mut case.cat,
            &case.card,
            USABLE,
            &both,
            &mut commit::no_hook,
        );
        assert_eq!(r.committed.len(), 1);
        assert_eq!(files(&case.card, format::OBJECTS).len(), 3);
        assert_eq!(records(&case.card).len(), 2);
    }
}

#[test]
fn crash_while_writing_the_record_keeps_every_earlier_record() {
    // A21: the record's temporary file is never trusted; earlier records survive.
    let mut case = crashed("d6-tmp", Point::RecordTempWritten);
    assert!(staging(&case.card)
        .iter()
        .any(|n| n.ends_with(".record.tmp")));
    let report = recover(&mut case);
    assert_eq!(report.finished.len(), 1);
    assert_committed_once(&mut case);
}

#[test]
fn crash_after_the_record_imports_it_without_a_recopy() {
    // A22: committed on the card, not yet in the catalogue.
    let mut case = crashed("d6", Point::RecordPublished);
    assert_eq!(case.cat.copies.len(), 1);
    let report = recover(&mut case);
    assert_eq!(report.imported.len(), 1);
    assert!(report.finished.is_empty());
    assert_committed_once(&mut case);
}

#[test]
fn crash_after_the_catalogue_only_cleans_up() {
    // A23: no false completion and nothing done twice.
    let mut case = crashed("d7", Point::CatalogueCommitted);
    assert_eq!(case.cat.copies.len(), 2);
    let report = recover(&mut case);
    assert!(report.imported.is_empty() && report.finished.is_empty());
    assert_eq!(report.cleaned.len(), 1);
    assert_committed_once(&mut case);
}

#[test]
fn crash_after_cleanup_leaves_nothing_to_do() {
    let mut case = crashed("d8", Point::CleanedUp);
    let report = recover(&mut case);
    assert_eq!(report, Report::default());
    assert_committed_once(&mut case);
}

#[test]
fn a_record_whose_payload_was_damaged_is_never_counted() {
    // A25: damage stays visible, the record is not rewritten, a replacement is made.
    let mut case = crashed("damaged", Point::RecordPublished);
    let recs = records(&case.card);
    let second = recs
        .iter()
        .find(|r| r.meta.byte_size == 70_000)
        .unwrap()
        .clone();
    let p = case
        .card
        .join(format::ROOT)
        .join(&second.txn().archive_relative_path);
    fs::write(&p, b"bit rot").unwrap();
    let vol = attach(&case.card, 1);
    let report = reconcile::reconcile(&vol, &mut case.cat).unwrap();
    assert_eq!(report.damaged, [second.txn().record_id.clone()]);
    let copy = case.cat.copy(&second.txn().record_id).unwrap();
    assert_eq!(copy.state, CopyState::Damaged);
    let both = [case.first.clone(), case.second.clone()];
    let r = run(
        &vol,
        &mut case.cat,
        &case.card,
        USABLE,
        &both,
        &mut commit::no_hook,
    );
    assert_eq!(r.committed.len(), 1);
    let replacement = records(&case.card)
        .into_iter()
        .find(|r| r.txn().replaces_record_id.is_some())
        .unwrap();
    assert_eq!(
        replacement.txn().replaces_record_id.as_deref(),
        Some(second.txn().record_id.as_str())
    );
    assert_eq!(replacement.txn().version_id, second.txn().version_id);
    // the damaged record itself is still there, unchanged
    assert!(records(&case.card).contains(&second));
}

#[test]
fn one_file_under_two_names_stops_writes() {
    // exFAT: after a pulled card a rename may leave both names.
    let mut case = crashed("twonames", Point::VerifiedWritten);
    let stage = case.card.join(format::ROOT).join(format::STAGING);
    let partial = staging(&case.card)
        .into_iter()
        .find(|n| n.ends_with(".partial"))
        .unwrap();
    let verified = staging(&case.card)
        .into_iter()
        .find(|n| n.ends_with(".verified.json"))
        .unwrap();
    let meta = format::CopyMeta::parse_verified(&fs::read(stage.join(verified)).unwrap()).unwrap();
    let target = case
        .card
        .join(format::ROOT)
        .join(&meta.txn.archive_relative_path);
    fs::create_dir_all(target.parent().unwrap()).unwrap();
    fs::hard_link(stage.join(&partial), &target).unwrap();
    let vol = attach(&case.card, 1);
    let report = reconcile::reconcile(&vol, &mut case.cat).unwrap();
    assert!(report.needs_filesystem_check);
    assert!(
        stage.join(&partial).exists() && target.exists(),
        "neither name removed"
    );
    let both = [case.first.clone(), case.second.clone()];
    let r = run(
        &vol,
        &mut case.cat,
        &case.card,
        USABLE,
        &both,
        &mut commit::no_hook,
    );
    assert_eq!(r.state, RunState::Stopped);
    assert!(r.committed.is_empty());
}

#[test]
fn a_verified_payload_under_neither_name_is_copied_again() {
    let mut case = crashed("noname", Point::VerifiedWritten);
    let stage = case.card.join(format::ROOT).join(format::STAGING);
    let partial = staging(&case.card)
        .into_iter()
        .find(|n| n.ends_with(".partial"))
        .unwrap();
    fs::remove_file(stage.join(partial)).unwrap();
    let report = recover(&mut case);
    assert!(report.notes.iter().any(|n| n.contains("lost space")));
    assert_copied_again(&mut case, &report);
}

#[test]
fn a_torn_intent_proves_nothing_and_is_removed() {
    let mut case = crashed("torn", Point::IntentWritten);
    let stage = case.card.join(format::ROOT).join(format::STAGING);
    let intent = staging(&case.card).pop().unwrap();
    fs::write(stage.join(&intent), b"{\"schema_version\": 1, \"ki").unwrap();
    let report = recover(&mut case);
    assert_eq!(report.torn, [intent]);
    assert_eq!(records(&case.card).len(), 1);
    assert!(staging(&case.card).is_empty());
}

#[test]
fn input_output_errors_fail_the_transaction() {
    // A24: an error before publication undoes this transaction and defers the file.
    for point in [
        Point::IntentWritten,
        Point::PayloadWritten,
        Point::VerifiedWritten,
    ] {
        for errno in [libc::EIO, libc::EROFS] {
            let (case, result) = stopped("eio", &mut fail_at(point, errno));
            match result.unwrap() {
                Outcome::Deferred(e) => assert_eq!(e.errno, Some(errno)),
                other => panic!("{other:?}"),
            }
            assert!(staging(&case.card).is_empty());
            assert!(case.cat.txs.is_empty());
            assert_eq!(records(&case.card).len(), 1);
        }
    }
    // ENOSPC is "no room": the run asks for the next card
    let (case, result) = stopped("enospc", &mut fail_at(Point::PayloadStarted, libc::ENOSPC));
    match result.unwrap() {
        Outcome::Deferred(e) => assert_eq!(e.kind, Kind::NoRoom),
        other => panic!("{other:?}"),
    }
    assert!(staging(&case.card).is_empty());
}

#[test]
fn input_output_errors_after_publication_stop_and_recover_later() {
    for point in [Point::Published, Point::RecordTempWritten] {
        let (mut case, result) = stopped("eio-late", &mut fail_at(point, libc::EIO));
        assert_eq!(result.unwrap_err().errno, Some(libc::EIO));
        assert!(case.cat.copies.len() == 1, "not counted");
        let report = recover(&mut case);
        assert_eq!(report.finished.len(), 1);
        assert_committed_once(&mut case);
    }
}

#[test]
fn unknown_staging_files_are_left_and_block_writes() {
    let s = Scratch::new("unknown");
    let (path, vol) = card(&s, 1);
    let stage = path.join(format::ROOT).join(format::STAGING);
    fs::write(stage.join("someone-elses.txt"), b"keep").unwrap();
    let mut cat = Memory::default();
    let report = reconcile::reconcile(&vol, &mut cat).unwrap();
    assert_eq!(report.unknown, ["someone-elses.txt"]);
    assert!(!reconcile::staging_clear(&vol, &cat).unwrap());
    assert_eq!(fs::read(stage.join("someone-elses.txt")).unwrap(), b"keep");
    // interrupted inventory files are the engine's own and are removed
    let snap = "ffffffff-0000-4000-8000-000000000001";
    fs::write(stage.join(format!("{snap}.inventory.tmp")), b"").unwrap();
    let report = reconcile::reconcile(&vol, &mut cat).unwrap();
    assert_eq!(report.notes.len(), 1);
    assert!(!stage.join(format!("{snap}.inventory.tmp")).exists());
    for i in 0..70 {
        fs::write(stage.join(format!("extra-{i}")), b"").unwrap();
    }
    assert_eq!(
        reconcile::reconcile(&vol, &mut cat).unwrap_err().kind,
        Kind::Invalid
    );
}

#[test]
fn a_transaction_of_another_card_waits_for_that_card() {
    // The row of an unfinished transaction on card 1 is not touched by card 2.
    let (case, _) = stopped("othercard", &mut crash_at(Point::VerifiedWritten));
    let mut cat = case.cat.clone();
    let (path2, vol2) = card(&case._s, 2);
    let report = reconcile::reconcile(&vol2, &mut cat).unwrap();
    assert_eq!(report, Report::default());
    assert_eq!(cat.txs.len(), 1);
    // the file may be committed to card 2 meanwhile, under a new record
    let both = [case.first.clone(), case.second.clone()];
    let r = run(&vol2, &mut cat, &path2, USABLE, &both, &mut commit::no_hook);
    assert_eq!(r.committed.len(), 1);
    // when card 1 returns its transaction is finished too: two copies, one path
    let vol1: Volume = attach(&case.card, 1);
    let report = reconcile::reconcile(&vol1, &mut cat).unwrap();
    assert_eq!(report.cleaned.len() + report.finished.len(), 1);
    assert!(cat.txs.is_empty());
}
