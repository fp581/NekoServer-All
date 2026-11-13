//! The acceptance cases of the plan and of V2 section 16 that a unit test can show.
//! Cards are folders here (`MountRule::TestFolder`) and their size is chosen by the test;
//! real mounts and loop devices are the filesystem gate's work.
use super::catalogue::{Catalogue, CopyState, Memory};
use super::commit::{self, Point};
use super::error::{CardError, Kind};
use super::format;
use super::run::{self, Decision, RunState};
use super::source::{self, Source};
use super::testkit::{
    attach, bytes, cands, card, files, payload, put, records, run, source, volume_id, Scratch, MIB,
    SET,
};
use std::fs;

fn names(paths: &[String]) -> Vec<&str> {
    paths.iter().map(String::as_str).collect()
}

#[test]
fn a_library_larger_than_one_card_continues_on_the_next() {
    // A01: card 1 fills, the run asks for the next card, card 2 takes the rest, and
    // nothing committed on card 1 is copied again.
    let s = Scratch::new("a01");
    let src = source(&s, "photos", 1);
    for i in 0..5u8 {
        put(&src, &format!("2001/{i}.jpg"), &bytes(i, 300 * 1024));
    }
    let all = cands(&src);
    let mut cat = Memory::default();
    let (p1, v1) = card(&s, 1);
    let r1 = run(&v1, &mut cat, &p1, 1000 * 1024, &all, &mut commit::no_hook);
    assert_eq!(r1.state, RunState::WaitingForNextCard);
    assert!(!r1.committed.is_empty() && !r1.waiting.is_empty());
    let (p2, v2) = card(&s, 2);
    let r2 = run(&v2, &mut cat, &p2, 4 * MIB, &all, &mut commit::no_hook);
    assert_eq!(r2.state, RunState::Complete);
    assert_eq!(r2.unchanged.len(), r1.committed.len());
    assert_eq!(r1.committed.len() + r2.committed.len(), 5);
    let mut paths: Vec<String> = records(&p1)
        .into_iter()
        .chain(records(&p2))
        .map(|r| r.txn().original_relative_path.clone())
        .collect();
    paths.sort();
    paths.dedup();
    assert_eq!(paths.len(), 5, "every file exactly once");
    for r in records(&p1).iter().chain(records(&p2).iter()) {
        let original = fs::read(src.root.join(&r.txn().original_relative_path)).unwrap();
        assert_eq!(
            payload(
                if r.txn().volume_id == volume_id(1) {
                    &p1
                } else {
                    &p2
                },
                r
            ),
            original
        );
    }
}

#[test]
fn a_large_file_that_does_not_fit_lets_smaller_ones_through() {
    // A02: whole files only; the large one waits for a fresh card, the small ones go on.
    let s = Scratch::new("a02");
    let src = source(&s, "photos", 1);
    put(&src, "a-big.mov", &bytes(9, 700 * 1024));
    for i in 0..3u8 {
        put(&src, &format!("b-small-{i}.jpg"), &bytes(i, 50 * 1024));
    }
    let (p, v) = card(&s, 1);
    let mut cat = Memory::default();
    // the big file alone would fit on an empty card of this size
    let r = run(
        &v,
        &mut cat,
        &p,
        900 * 1024,
        &cands(&src)[1..],
        &mut commit::no_hook,
    );
    assert_eq!(r.committed.len(), 3);
    let r = run(
        &v,
        &mut cat,
        &p,
        900 * 1024,
        &cands(&src),
        &mut commit::no_hook,
    );
    assert_eq!(names(&r.waiting), ["a-big.mov"]);
    assert_eq!(r.state, RunState::WaitingForNextCard);
    assert_eq!(r.unchanged.len(), 3);
    assert!(
        records(&p).iter().all(|r| r.meta.byte_size < 700 * 1024),
        "never split"
    );
}

#[test]
fn a_file_too_large_for_this_kind_of_card_is_named_not_waited_for() {
    // A03: a clear "needs larger media", and no card is asked for because of it alone.
    let s = Scratch::new("a03");
    let src = source(&s, "videos", 1);
    put(&src, "huge.mov", &bytes(3, 600 * 1024));
    put(&src, "small.jpg", &bytes(4, 10 * 1024));
    let (p, v) = card(&s, 1);
    let mut cat = Memory::default();
    for _ in 0..2 {
        let r = run(
            &v,
            &mut cat,
            &p,
            500 * 1024,
            &cands(&src),
            &mut commit::no_hook,
        );
        assert_eq!(names(&r.needs_larger_media), ["huge.mov"]);
        assert!(r.waiting.is_empty());
        assert_eq!(r.state, RunState::Partial);
    }
    assert_eq!(records(&p).len(), 1);
}

#[test]
fn a_deleted_source_keeps_its_archived_copy() {
    // A05: append-only; the next run is complete and the card copy stays.
    let s = Scratch::new("a05");
    let src = source(&s, "photos", 1);
    put(&src, "gone.jpg", &bytes(1, 5000));
    let (p, v) = card(&s, 1);
    let mut cat = Memory::default();
    run(&v, &mut cat, &p, MIB, &cands(&src), &mut commit::no_hook);
    fs::remove_file(src.root.join("gone.jpg")).unwrap();
    let r = run(&v, &mut cat, &p, MIB, &cands(&src), &mut commit::no_hook);
    assert_eq!(r.state, RunState::Complete);
    assert_eq!(records(&p).len(), 1);
    assert_eq!(payload(&p, &records(&p)[0]), bytes(1, 5000));
    assert!(cat.copies.iter().all(|c| c.state == CopyState::Committed));
}

#[test]
fn a_normal_run_trusts_size_and_dates() {
    // A06 as decided: the same size and dates with other content are not caught by a
    // normal run (the deep recheck, a later step, catches it); other dates are, and make
    // a new version while the old one stays.
    let s = Scratch::new("a06");
    let src = source(&s, "photos", 1);
    put(&src, "edit.jpg", &bytes(1, 4000));
    let (p, v) = card(&s, 1);
    let mut cat = Memory::default();
    run(&v, &mut cat, &p, MIB, &cands(&src), &mut commit::no_hook);
    let version = cat
        .newest_version(&src.source_id, "edit.jpg")
        .unwrap()
        .unwrap();
    put(&src, "edit.jpg", &bytes(2, 4000));
    // the clock's tick may be coarser than two writes: give the edit a date of its own
    filetime(&src, "edit.jpg");
    let mut same_dates = cands(&src)[0].clone();
    same_dates.mtime_ns = version.mtime_ns;
    same_dates.ctime_ns = version.ctime_ns;
    assert_eq!(run::decide(&cat, &same_dates).unwrap(), Decision::Unchanged);
    let r = run(&v, &mut cat, &p, MIB, &cands(&src), &mut commit::no_hook);
    assert_eq!(r.committed.len(), 1);
    let recs = records(&p);
    assert_eq!(recs.len(), 2);
    assert_ne!(recs[0].txn().version_id, recs[1].txn().version_id);
    // a new date on the same content copies nothing and notes the date
    let now = cands(&src)[0].clone();
    let touched = filetime(&src, "edit.jpg");
    assert_ne!(touched, now.mtime_ns);
    let r = run(&v, &mut cat, &p, MIB, &cands(&src), &mut commit::no_hook);
    assert_eq!(names(&r.same_content), ["edit.jpg"]);
    assert_eq!(records(&p).len(), 2);
    let r = run(&v, &mut cat, &p, MIB, &cands(&src), &mut commit::no_hook);
    assert_eq!(names(&r.unchanged), ["edit.jpg"]);
}

/// Give `rel` another modification time without changing its bytes; its new time.
fn filetime(src: &Source, rel: &str) -> i64 {
    let file = fs::File::options()
        .write(true)
        .open(src.root.join(rel))
        .unwrap();
    let old = file.metadata().unwrap().modified().unwrap();
    file.set_modified(old - std::time::Duration::from_secs(3600))
        .unwrap();
    drop(file);
    cands(src)
        .into_iter()
        .find(|c| c.path == rel)
        .unwrap()
        .mtime_ns
}

#[test]
fn an_unchanged_file_on_an_offline_card_is_not_copied_again() {
    // A07: card 1 is away; card 2 receives nothing for the unchanged file.
    let s = Scratch::new("a07");
    let src = source(&s, "photos", 1);
    put(&src, "kept.jpg", &bytes(1, 5000));
    let mut cat = Memory::default();
    {
        let (p1, v1) = card(&s, 1);
        run(&v1, &mut cat, &p1, MIB, &cands(&src), &mut commit::no_hook);
    }
    let (p2, v2) = card(&s, 2);
    let r = run(&v2, &mut cat, &p2, MIB, &cands(&src), &mut commit::no_hook);
    assert_eq!(names(&r.unchanged), ["kept.jpg"]);
    assert!(files(&p2, format::OBJECTS).is_empty());
}

#[test]
fn a_lost_copy_is_replaced_with_the_same_version() {
    // A08: unchanged content whose only copy is lost gets a new copy of that version.
    let s = Scratch::new("a08");
    let src = source(&s, "photos", 1);
    put(&src, "only.jpg", &bytes(1, 5000));
    let mut cat = Memory::default();
    let (p1, v1) = card(&s, 1);
    run(&v1, &mut cat, &p1, MIB, &cands(&src), &mut commit::no_hook);
    let lost = records(&p1).remove(0);
    cat.set_copy_state(&lost.txn().record_id, CopyState::Lost)
        .unwrap();
    let (p2, v2) = card(&s, 2);
    let r = run(&v2, &mut cat, &p2, MIB, &cands(&src), &mut commit::no_hook);
    assert_eq!(r.committed.len(), 1);
    let new = records(&p2).remove(0);
    assert_eq!(new.txn().version_id, lost.txn().version_id);
    assert_eq!(
        new.txn().replaces_record_id.as_deref(),
        Some(lost.txn().record_id.as_str())
    );
    assert_eq!(new.meta.checksum, lost.meta.checksum);
}

#[test]
fn a_source_that_changes_during_the_copy_is_retried_then_deferred() {
    // A09: never accepted while it changes; a change that stops is copied as it is now.
    let s = Scratch::new("a09");
    let src = source(&s, "photos", 1);
    put(&src, "busy.jpg", &bytes(1, 200_000));
    let (p, v) = card(&s, 1);
    let mut cat = Memory::default();
    let path = src.root.join("busy.jpg");
    let mut always = |point: Point| {
        if point == Point::PayloadStarted {
            let mut b = fs::read(&path).unwrap();
            b.push(1);
            fs::write(&path, b).unwrap();
        }
        Ok::<(), CardError>(())
    };
    let r = run(&v, &mut cat, &p, MIB, &cands(&src), &mut always);
    assert_eq!(r.deferred.len(), 1);
    assert_eq!(r.deferred[0].1.kind, Kind::SourceChanged);
    assert_eq!(r.state, RunState::Partial);
    assert!(records(&p).is_empty());
    assert!(files(&p, format::STAGING).is_empty());
    assert!(cat.txs.is_empty());
    let mut changes = 0;
    let mut once = |point: Point| {
        if point == Point::PayloadStarted && changes == 0 {
            changes += 1;
            fs::write(&path, bytes(7, 150_000)).unwrap();
        }
        Ok::<(), CardError>(())
    };
    let r = run(&v, &mut cat, &p, MIB, &cands(&src), &mut once);
    assert_eq!(r.committed.len(), 1);
    assert_eq!(payload(&p, &records(&p)[0]), bytes(7, 150_000));
}

#[test]
fn a_source_file_that_vanished_after_the_scan_is_deferred() {
    let s = Scratch::new("a09b");
    let src = source(&s, "photos", 1);
    put(&src, "a.jpg", &bytes(1, 1000));
    put(&src, "b.jpg", &bytes(2, 1000));
    let all = cands(&src);
    fs::remove_file(src.root.join("a.jpg")).unwrap();
    let (p, v) = card(&s, 1);
    let mut cat = Memory::default();
    let r = run(&v, &mut cat, &p, MIB, &all, &mut commit::no_hook);
    assert_eq!(r.deferred.len(), 1);
    assert_eq!(r.committed.len(), 1);
    assert_eq!(r.state, RunState::Partial);
}

#[test]
fn a_missing_source_is_not_an_empty_library() {
    // A10: a source that is not there stops the run; it is never a successful empty scan.
    let s = Scratch::new("a10");
    let src = source(&s, "nas", 1);
    put(&src, "a.jpg", &bytes(1, 1000));
    let all = cands(&src);
    fs::remove_dir_all(&src.root).unwrap();
    assert_eq!(source::scan(&src).unwrap_err().kind, Kind::SourceMissing);
    let (p, v) = card(&s, 1);
    let mut cat = Memory::default();
    let r = run(&v, &mut cat, &p, MIB, &all, &mut commit::no_hook);
    assert_eq!(r.state, RunState::Stopped);
    assert_eq!(r.stopped.unwrap().kind, Kind::SourceMissing);
}

#[test]
fn a_card_removed_mid_copy_stops_and_writes_nothing_into_the_folder() {
    // A11: the card's folder is swapped for an empty one in the middle of the payload,
    // as an unmount leaves it. The run stops, nothing is counted, the empty folder stays
    // empty, and the card recovers the file when it comes back.
    let s = Scratch::new("a11");
    let src = source(&s, "photos", 1);
    put(&src, "a.jpg", &bytes(1, 200_000));
    let (p, v) = card(&s, 1);
    let away = s.path.join("card-1-away");
    let mut cat = Memory::default();
    let (pp, aa) = (p.clone(), away.clone());
    let mut pull = move |point: Point| {
        if point == Point::PayloadStarted {
            fs::rename(&pp, &aa).unwrap();
            fs::create_dir(&pp).unwrap();
        }
        Ok::<(), CardError>(())
    };
    let r = run(&v, &mut cat, &p, MIB, &cands(&src), &mut pull);
    assert_eq!(r.state, RunState::Stopped);
    assert_eq!(r.stopped.unwrap().kind, Kind::MountChanged);
    assert!(cat.copies.is_empty(), "not marked verified");
    assert_eq!(
        fs::read_dir(&p).unwrap().count(),
        0,
        "nothing written into the folder"
    );
    drop(v);
    fs::remove_dir(&p).unwrap();
    fs::rename(&away, &p).unwrap();
    let v = attach(&p, 1);
    let r = run(&v, &mut cat, &p, MIB, &cands(&src), &mut commit::no_hook);
    assert_eq!(r.reconcile.finished.len(), 1);
    assert_eq!(r.state, RunState::Complete);
    assert_eq!(records(&p).len(), 1);
}

#[test]
fn a_card_replaced_before_the_copy_is_never_written() {
    // A12: attached, then the folder leads elsewhere before any write.
    let s = Scratch::new("a12");
    let src = source(&s, "photos", 1);
    put(&src, "a.jpg", &bytes(1, 1000));
    let (p, v) = card(&s, 1);
    fs::rename(&p, s.path.join("moved")).unwrap();
    fs::create_dir(&p).unwrap();
    let mut cat = Memory::default();
    let r = run(&v, &mut cat, &p, MIB, &cands(&src), &mut commit::no_hook);
    assert_eq!(r.state, RunState::Stopped);
    assert_eq!(fs::read_dir(&p).unwrap().count(), 0);
    assert!(cat.txs.is_empty() && cat.copies.is_empty());
}

#[test]
fn the_same_card_returning_is_recognised() {
    // A14: attached again, reconciled, nothing copied twice.
    let s = Scratch::new("a14");
    let src = source(&s, "photos", 1);
    put(&src, "a.jpg", &bytes(1, 1000));
    put(&src, "b.jpg", &bytes(2, 1000));
    let (p, v) = card(&s, 1);
    let mut cat = Memory::default();
    run(&v, &mut cat, &p, MIB, &cands(&src), &mut commit::no_hook);
    drop(v);
    let v = attach(&p, 1);
    let r = run(&v, &mut cat, &p, MIB, &cands(&src), &mut commit::no_hook);
    assert_eq!(r.unchanged.len(), 2);
    assert_eq!(files(&p, format::OBJECTS).len(), 2);
}

#[test]
fn a_source_overlapping_the_card_is_refused() {
    // A16: the card inside a source, or a source inside the card.
    let s = Scratch::new("a16");
    let (p, v) = card(&s, 1);
    let inside = Source {
        source_id: "cccccccc-0000-4000-8000-000000000009".into(),
        display_name: "inside".into(),
        root: p.join("photos"),
    };
    put(&inside, "a.jpg", &bytes(1, 1000));
    let around = Source {
        source_id: "cccccccc-0000-4000-8000-000000000008".into(),
        display_name: "around".into(),
        root: s.path.clone(),
    };
    let mut cat = Memory::default();
    for src in [inside, around] {
        let all = vec![source::Candidate {
            source: src.clone(),
            path: "a.jpg".into(),
            size: 1000,
            mtime_ns: 0,
            ctime_ns: 0,
        }];
        let r = run(&v, &mut cat, &p, MIB, &all, &mut commit::no_hook);
        assert_eq!(r.state, RunState::Stopped);
        assert_eq!(r.stopped.unwrap().kind, Kind::Invalid);
    }
    assert!(records(&p).is_empty());
}

#[test]
fn names_that_collide_elsewhere_are_all_kept() {
    // A26: the same name in two sources, case-only differences, Unicode in both forms,
    // characters exFAT forbids: each its own record, names kept byte for byte.
    let s = Scratch::new("a26");
    let photos = source(&s, "photos", 1);
    let files_src = source(&s, "files", 2);
    let tricky = [
        "IMG_0001.JPG",
        "img_0001.jpg",
        "caf\u{e9}.jpg",
        "cafe\u{301}.jpg",
        "what?:*.jpg",
        "trailing dot.",
        "\u{1f408} cat.heic",
    ];
    for (i, name) in tricky.iter().enumerate() {
        put(&photos, name, &bytes(i as u8, 100 + i));
    }
    put(&files_src, "IMG_0001.JPG", &bytes(99, 50));
    let mut all = cands(&photos);
    all.extend(cands(&files_src));
    let (p, v) = card(&s, 1);
    let mut cat = Memory::default();
    let r = run(&v, &mut cat, &p, MIB, &all, &mut commit::no_hook);
    assert_eq!(r.committed.len(), tricky.len() + 1);
    let recs = records(&p);
    let mut kept: Vec<&str> = recs
        .iter()
        .map(|r| r.txn().original_relative_path.as_str())
        .collect();
    kept.sort();
    let mut want: Vec<&str> = tricky.to_vec();
    want.push("IMG_0001.JPG");
    want.sort();
    assert_eq!(kept, want);
    for r in &recs {
        let name = r.txn().archive_relative_path.rsplit('/').next().unwrap();
        assert!(name
            .bytes()
            .all(|c| !c.is_ascii_uppercase() && c.is_ascii()));
        let root = if r.txn().source_id == photos.source_id {
            &photos.root
        } else {
            &files_src.root
        };
        assert_eq!(
            payload(&p, r),
            fs::read(root.join(&r.txn().original_relative_path)).unwrap()
        );
    }
}

#[test]
fn links_special_files_and_other_mounts_are_not_candidates() {
    let s = Scratch::new("scan");
    let src = source(&s, "photos", 1);
    put(&src, "real.jpg", &bytes(1, 10));
    std::os::unix::fs::symlink("/etc/hostname", src.root.join("link.jpg")).unwrap();
    let c = std::ffi::CString::new(src.root.join("pipe").to_str().unwrap()).unwrap();
    // SAFETY: a NUL-terminated path in the test's own folder.
    assert_eq!(unsafe { libc::mkfifo(c.as_ptr(), 0o600) }, 0);
    let scan = source::scan(&src).unwrap();
    assert_eq!(scan.candidates.len(), 1);
    let mut special = scan.skipped.special.clone();
    special.sort();
    assert_eq!(special, ["link.jpg", "pipe"]);
}

#[test]
fn every_committed_copy_is_the_whole_file_and_its_checksum() {
    let s = Scratch::new("whole");
    let src = source(&s, "photos", 1);
    put(&src, "empty.jpg", b"");
    put(&src, "one.jpg", &bytes(5, 123_457));
    let (p, v) = card(&s, 1);
    let mut cat = Memory::default();
    let r = run(&v, &mut cat, &p, MIB, &cands(&src), &mut commit::no_hook);
    assert_eq!(r.state, RunState::Complete);
    for rec in records(&p) {
        let bytes = payload(&p, &rec);
        assert_eq!(bytes.len() as u64, rec.meta.byte_size);
        let (sum, _) = source::hash_reader(&bytes[..]).unwrap();
        assert_eq!(sum, rec.meta.checksum);
        assert_eq!(rec.txn().volume_id, volume_id(1));
        assert_eq!(rec.txn().backup_set_id, SET);
    }
}
