//! Format 1 files, the planner, the lock, the folder-relative calls and attaching a card.
use super::error::Kind;
use super::format::{self, CopyMeta, Identity, Intent, Record, SourceIdentity, Txn};
use super::fsat::Dir;
use super::lock::SetLock;
use super::planner::{self, Fit, Space};
use super::testkit::*;
use super::volume::{MountRule, Volume};
use serde_json::Value;
use std::fs;
use std::os::unix::fs::symlink;

const RID: &str = "aaaaaaaa-0000-4000-8000-000000000001";

fn txn() -> Txn {
    Txn {
        transaction_id: "eeeeeeee-0000-4000-8000-000000000001".into(),
        run_id: "dddddddd-0000-4000-8000-000000000001".into(),
        record_id: RID.into(),
        version_id: "bbbbbbbb-0000-4000-8000-000000000001".into(),
        replaces_record_id: None,
        backup_set_id: SET.into(),
        volume_id: volume_id(1),
        source_id: "cccccccc-0000-4000-8000-000000000001".into(),
        original_relative_path: "upload/example-user/2001/EXAMPLE 0001.JPG".into(),
        archive_relative_path: format::archive_path(RID, ".jpg"),
        source_mtime_ns: 981_173_106_000_000_000,
        source_identity: SourceIdentity {
            device: 2049,
            inode: 123_456,
            ctime_ns: 981_173_106_000_000_000,
        },
    }
}

fn record() -> Record {
    Record {
        meta: CopyMeta {
            txn: txn(),
            source_display_name: "Example photos".into(),
            byte_size: 123_456,
            checksum: "0123456789abcdef".repeat(4),
            source_observed_at: "2001-02-03T04:05:06Z".into(),
            copy_verified_at: "2001-02-03T04:05:09Z".into(),
        },
        committed_at: "2001-02-03T04:05:10Z".into(),
    }
}

/// The record as JSON with one field replaced.
fn record_with(field: &str, value: Value) -> Vec<u8> {
    let mut v: Value = serde_json::from_slice(&record().to_bytes()).unwrap();
    v[field] = value;
    serde_json::to_vec(&v).unwrap()
}

#[test]
fn record_intent_and_verified_intent_round_trip() {
    let r = record();
    assert_eq!(Record::parse(&r.to_bytes()).unwrap(), r);
    assert_eq!(
        CopyMeta::parse_verified(&r.meta.verified_bytes()).unwrap(),
        r.meta
    );
    let i = Intent {
        txn: txn(),
        expected_checksum: None,
        observed_byte_size: 123_456,
        created_at: "2001-02-03T04:05:06Z".into(),
    };
    assert_eq!(Intent::parse(&i.to_bytes()).unwrap(), i);
    // a verified intent is not a record, and a record is not a verified intent
    assert!(Record::parse(&r.meta.verified_bytes()).is_err());
    assert!(CopyMeta::parse_verified(&r.to_bytes()).is_err());
    assert!(r.to_bytes().ends_with(b"\n"));
}

#[test]
fn hostile_records_are_refused() {
    let traversal = [
        "../outside.jpg",
        "a/../../b.jpg",
        "/absolute.jpg",
        "a//b.jpg",
        "a/./b.jpg",
        "a/b\u{0}.jpg",
        "",
    ];
    for path in traversal {
        let b = record_with("original_relative_path", Value::from(path));
        assert!(Record::parse(&b).is_err(), "{path:?} accepted");
    }
    let long_part = "x".repeat(256);
    assert!(Record::parse(&record_with(
        "original_relative_path",
        Value::from(long_part)
    ))
    .is_err());
    let cases = [
        (
            "archive_relative_path",
            Value::from("objects/bb/aaaaaaaa-0000-4000-8000-000000000001.jpg"),
        ),
        ("archive_relative_path", Value::from("../identity.json")),
        (
            "archive_relative_path",
            Value::from(format!("objects/aa/{RID}.JPG")),
        ),
        (
            "record_id",
            Value::from("AAAAAAAA-0000-4000-8000-000000000001"),
        ),
        ("completion_state", Value::from("verified")),
        ("checksum", Value::from("xyz")),
        ("checksum_algorithm", Value::from("md5")),
        ("byte_size", Value::from(-1)),
        ("byte_size", Value::from(9_007_199_254_740_992u64)),
        ("source_mtime_ns", Value::from(981_173_106)),
        ("committed_at", Value::from("2001-02-03 04:05:10")),
        ("schema_version", Value::from(2)),
        ("original_file_name", Value::from("OTHER.JPG")),
    ];
    for (field, value) in cases {
        assert!(
            Record::parse(&record_with(field, value.clone())).is_err(),
            "{field}={value}"
        );
    }
}

#[test]
fn oversized_deep_or_marked_files_are_refused() {
    let mut big: Value = serde_json::from_slice(&record().to_bytes()).unwrap();
    big["padding"] = Value::from("p".repeat(17_000));
    assert_eq!(
        Record::parse(&serde_json::to_vec(&big).unwrap())
            .unwrap_err()
            .kind,
        Kind::Invalid
    );
    let deep = record_with("extra", serde_json::json!({"a": {"b": {"c": {"d": 1}}}}));
    assert!(Record::parse(&deep).is_err());
    let mut bom = vec![0xef, 0xbb, 0xbf];
    bom.extend(record().to_bytes());
    assert!(Record::parse(&bom).is_err());
    assert!(Record::parse(b"[1,2]").is_err());
    assert!(Record::parse(b"{\"torn\": ").is_err());
    // an unknown field is ignored
    assert!(Record::parse(&record_with("later_field", Value::from(1))).is_ok());
}

#[test]
fn identity_other_versions_and_labels() {
    let id = Identity {
        backup_set_id: SET.into(),
        volume_id: volume_id(7),
        sequence_number: 7,
        set_display_name: "Family media".into(),
        created_at: "2001-02-03T04:05:06Z".into(),
        filesystem_type: "ext4".into(),
        filesystem_uuid: fs_uuid(7),
        privacy_acknowledged_at: "2001-02-03T04:05:00Z".into(),
    };
    assert_eq!(id.label(), "Family media - 007");
    assert_eq!(Identity::parse(&id.to_bytes()).unwrap(), id);
    let mut v: Value = serde_json::from_slice(&id.to_bytes()).unwrap();
    v["archive_format_version"] = Value::from(2);
    let other = Identity::parse(&serde_json::to_vec(&v).unwrap()).unwrap_err();
    assert_eq!(other.kind, Kind::Unsupported);
    for (field, value) in [
        ("volume_label", Value::from("Family media - 7")),
        ("encrypted", Value::from(true)),
        ("filesystem_type", Value::from("vfat")),
        ("sequence_number", Value::from(0)),
        ("sequence_number", Value::from(10_000)),
    ] {
        let mut v: Value = serde_json::from_slice(&id.to_bytes()).unwrap();
        v[field] = value;
        assert!(
            Identity::parse(&serde_json::to_vec(&v).unwrap()).is_err(),
            "{field}"
        );
    }
}

#[test]
fn ids_names_and_extensions() {
    for _ in 0..50 {
        assert!(format::is_uuid(&format::new_uuid().unwrap()));
    }
    assert!(!format::is_uuid("00000000-0000-1000-8000-000000000000"));
    assert!(format::is_time(&format::now()));
    assert_eq!(format::safe_extension("EXAMPLE 0001.JPG"), ".jpg");
    assert_eq!(format::safe_extension("clip.mp4"), ".mp4");
    assert_eq!(format::safe_extension("archive.tar.gz"), ".gz");
    assert_eq!(format::safe_extension("no-extension"), "");
    assert_eq!(format::safe_extension(".hidden"), "");
    assert_eq!(format::safe_extension("odd.ext-ension"), "");
    assert_eq!(format::safe_extension("long.abcdefghi"), "");
    assert_eq!(format::safe_extension("unicode.jpé"), "");
    let name = format::archive_path(RID, ".jpg");
    assert!(
        name.len() < 64
            && name
                .bytes()
                .all(|c| c.is_ascii() && !c.is_ascii_uppercase())
    );
}

fn space(free: u64, capacity: u64) -> Space {
    Space {
        free,
        capacity,
        unit: 4096,
        max_file: 4 << 30,
    }
}

#[test]
fn reserve_is_the_larger_of_256_mib_and_one_percent() {
    let small = space(0, 8 << 30);
    let r = planner::reserve(&small, 0, 2);
    assert!((256 * MIB..257 * MIB).contains(&r));
    let big = space(0, 100 << 30);
    assert!(planner::reserve(&big, 0, 0) >= (100 << 30) / 100);
    // the inventory budget grows with the versions of the set
    assert!(planner::reserve(&small, 100_000, 2) > r + 50 * MIB / 2);
    assert_eq!(planner::round_up(1, 4096), 4096);
    assert_eq!(planner::round_up(0, 4096), 0);
}

#[test]
fn whole_files_fit_or_wait_or_need_larger_media() {
    let s = space(300 * MIB, 8 << 30);
    let r = planner::reserve(&s, 0, 2);
    assert_eq!(planner::fit(10 * MIB, &s, r), Fit::Fits);
    // fits on a fresh card, not on what is left of this one (A02, A04)
    assert_eq!(planner::fit(100 * MIB, &s, r), Fit::NotNow);
    // larger than the filesystem allows, or than such a card can ever hold (A03)
    assert_eq!(planner::fit(5 << 30, &s, r), Fit::NeedsLargerMedia);
    let tiny = space(300 * MIB, 300 * MIB);
    assert_eq!(planner::fit(100 * MIB, &tiny, r), Fit::NeedsLargerMedia);
    assert_eq!(planner::max_file_size("ext4"), 16 << 40);
}

#[test]
fn a_second_writer_of_the_same_set_is_refused() {
    let s = Scratch::new("lock");
    let first = SetLock::take(&s.path, SET).unwrap();
    assert_eq!(SetLock::take(&s.path, SET).unwrap_err().kind, Kind::Busy);
    // another set is independent
    let other = SetLock::take(&s.path, OTHER_SET).unwrap();
    drop(first);
    assert!(SetLock::take(&s.path, SET).is_ok());
    drop(other);
    assert!(SetLock::take(&s.path, "not-a-set").is_err());
}

#[test]
fn folder_calls_never_replace_and_never_follow_links() {
    let s = Scratch::new("fsat");
    let dir = Dir::open_path(&s.path).unwrap();
    dir.write_new("a", b"first").unwrap();
    assert_eq!(
        dir.write_new("a", b"second").unwrap_err().kind,
        Kind::Exists
    );
    dir.write_new("b", b"other").unwrap();
    assert_eq!(
        dir.rename_noreplace("b", &dir, "a").unwrap_err().kind,
        Kind::Exists
    );
    assert_eq!(fs::read(s.path.join("a")).unwrap(), b"first");
    let outside = s.dir("outside");
    symlink(&outside, s.path.join("link")).unwrap();
    assert!(dir.open_dir("link").is_err());
    assert!(dir.create_new("link").is_err());
    assert!(Dir::open_path(&s.path.join("link")).is_err());
    assert!(dir.open_dir("..").is_err());
    assert!(dir.create_new("x/y").is_err());
    assert_eq!(fs::read_dir(&outside).unwrap().count(), 0);
    assert_eq!(dir.read_limited("a", 3).unwrap_err().kind, Kind::Invalid);
}

#[test]
fn an_unmounted_folder_is_never_written() {
    let s = Scratch::new("unmounted");
    let folder = s.dir("mnt");
    let mut e = expect(&folder, 1);
    e.rule = MountRule::MountPoint;
    let err = Volume::enrol(&folder, &e, &enrolment(1)).unwrap_err();
    assert_eq!(err.kind, Kind::NotMounted);
    assert_eq!(
        Volume::attach(&folder, &e).unwrap_err().kind,
        Kind::NotMounted
    );
    assert_eq!(
        fs::read_dir(&folder).unwrap().count(),
        0,
        "nothing written into the folder"
    );
}

#[test]
fn another_device_is_not_the_card() {
    let s = Scratch::new("device");
    let (path, _) = card(&s, 1);
    let mut e = expect(&path, 1);
    e.device += 1;
    assert_eq!(
        Volume::attach(&path, &e).unwrap_err().kind,
        Kind::WrongVolume
    );
}

#[test]
fn unknown_and_foreign_cards_are_refused_and_left_alone() {
    let s = Scratch::new("foreign");
    let blank = s.dir("blank");
    fs::write(blank.join("holiday.jpg"), b"someone's photo").unwrap();
    let err = Volume::attach(&blank, &expect(&blank, 1)).unwrap_err();
    assert_eq!(err.kind, Kind::WrongVolume);
    assert_eq!(fs::read_dir(&blank).unwrap().count(), 1);
    // a NekoBackup folder of any case is never adopted or overwritten
    let taken = s.dir("taken");
    fs::create_dir(taken.join("nekobackup")).unwrap();
    fs::write(taken.join("nekobackup").join("mine.txt"), b"keep").unwrap();
    let err = Volume::enrol(&taken, &expect(&taken, 1), &enrolment(1)).unwrap_err();
    assert_eq!(err.kind, Kind::Exists);
    assert_eq!(
        fs::read(taken.join("nekobackup").join("mine.txt")).unwrap(),
        b"keep"
    );
    // another set's card (A13)
    let (path, _) = card(&s, 2);
    let mut e = expect(&path, 2);
    e.backup_set_id = OTHER_SET.into();
    assert_eq!(
        Volume::attach(&path, &e).unwrap_err().kind,
        Kind::WrongVolume
    );
    // the expected set, but another card than the one asked for
    let mut e = expect(&path, 2);
    e.volume_id = volume_id(3);
    assert_eq!(
        Volume::attach(&path, &e).unwrap_err().kind,
        Kind::WrongVolume
    );
}

#[test]
fn a_copied_card_folder_is_an_identity_conflict() {
    // A15: NekoBackup copied onto another filesystem keeps its IDs, not its filesystem.
    let s = Scratch::new("conflict");
    let (path, _) = card(&s, 1);
    let mut e = expect(&path, 1);
    e.filesystem_uuid = fs_uuid(9);
    let err = Volume::attach(&path, &e).unwrap_err();
    assert_eq!(err.kind, Kind::WrongVolume);
    assert!(err.what.contains("identity conflict"));
}

#[test]
fn links_on_the_way_to_the_card_are_refused() {
    // A16: a symlinked mount path, and a NekoBackup that is a link.
    let s = Scratch::new("links");
    let (path, _) = card(&s, 1);
    let link = s.path.join("card-link");
    symlink(&path, &link).unwrap();
    let mut e = expect(&path, 1);
    assert!(Volume::attach(&link, &e).is_err());
    let other = s.dir("other");
    symlink(path.join(format::ROOT), other.join(format::ROOT)).unwrap();
    e.device = expect(&other, 1).device;
    assert!(Volume::attach(&other, &e).is_err());
}

#[test]
fn enrolment_writes_identity_readme_and_folders() {
    let s = Scratch::new("enrol");
    let (path, vol) = card(&s, 3);
    assert_eq!(vol.identity.label(), "Example set - 003");
    let nb = path.join(format::ROOT);
    let mut names: Vec<String> = fs::read_dir(&nb)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    assert_eq!(
        names,
        [
            "README.txt",
            "identity.json",
            "inventory",
            "objects",
            "records",
            "staging",
            "tools"
        ]
    );
    let readme = fs::read_to_string(nb.join("README.txt")).unwrap();
    assert!(readme.contains("NOT encrypted"));
    assert!(readme.contains("python3 NekoBackup/tools/neko-restore.py restore"));
    // The standalone restore tool is on every card, byte for byte the release's.
    assert_eq!(
        fs::read(nb.join("tools/neko-restore.py")).unwrap(),
        RESTORE_TOOL_SOURCE
    );
    let mut without = enrolment(4);
    without.restore_tool.clear();
    let other = s.dir("card-without-tool");
    assert!(Volume::enrol(&other, &expect(&other, 4), &without).is_err());
    assert_eq!(fs::read_dir(&other).unwrap().count(), 0, "nothing made");
    // a second enrolment of the same card is refused
    assert_eq!(
        Volume::enrol(&path, &expect(&path, 3), &enrolment(3))
            .unwrap_err()
            .kind,
        Kind::Exists
    );
}
