//! Shared pieces of the card engine's tests: a scratch folder that stands for the mounted
//! card (`MountRule::TestFolder`), sources with files, a card size that the tests choose,
//! and hooks that stop at a point as a killed process would.
use super::catalogue::Memory;
use super::commit::Point;
use super::error::{CardError, Kind, Res};
use super::format::{self, Record};
use super::planner::Space;
use super::run::{Run, RunResult};
use super::source::{self, Candidate, Source};
use super::volume::{Enrolment, Expect, MountRule, Volume};
use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU32, Ordering};

pub const MIB: u64 = 1 << 20;
/// The reserve is at least 256 MiB; a test card is that plus `usable` bytes.
pub const RESERVE_FLOOR: u64 = 256 * MIB + 128 * 1024;

static COUNT: AtomicU32 = AtomicU32::new(0);

pub struct Scratch {
    pub path: PathBuf,
}

impl Scratch {
    pub fn new(name: &str) -> Scratch {
        let n = COUNT.fetch_add(1, Ordering::SeqCst);
        let path =
            std::env::temp_dir().join(format!("neko-cards-{}-{n}-{name}", std::process::id()));
        fs::create_dir_all(&path).unwrap();
        Scratch { path }
    }
    pub fn dir(&self, name: &str) -> PathBuf {
        let p = self.path.join(name);
        fs::create_dir_all(&p).unwrap();
        p
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}

pub const SET: &str = "00000000-0000-4000-8000-0000000000aa";
pub const OTHER_SET: &str = "00000000-0000-4000-8000-0000000000bb";

pub fn volume_id(seq: u64) -> String {
    format!("00000000-0000-4000-8000-{seq:012}")
}

pub fn fs_uuid(seq: u64) -> String {
    format!("11111111-2222-3333-4444-{seq:012}")
}

pub fn expect(card: &Path, seq: u64) -> Expect {
    Expect {
        device: fs::metadata(card).unwrap().dev(),
        backup_set_id: SET.to_string(),
        volume_id: volume_id(seq),
        filesystem_uuid: fs_uuid(seq),
        rule: MountRule::TestFolder,
    }
}

pub fn enrolment(seq: u64) -> Enrolment {
    Enrolment {
        backup_set_id: SET.to_string(),
        volume_id: volume_id(seq),
        sequence_number: seq,
        set_display_name: "Example set".to_string(),
        filesystem_type: "ext4".to_string(),
        filesystem_uuid: fs_uuid(seq),
        privacy_acknowledged_at: "2001-02-03T04:05:00Z".to_string(),
    }
}

/// A fresh card folder `card-<seq>` in the scratch folder, enrolled.
pub fn card(s: &Scratch, seq: u64) -> (PathBuf, Volume) {
    let path = s.dir(&format!("card-{seq}"));
    let vol = Volume::enrol(&path, &expect(&path, seq), &enrolment(seq)).unwrap();
    (path, vol)
}

pub fn attach(path: &Path, seq: u64) -> Volume {
    Volume::attach(path, &expect(path, seq)).unwrap()
}

pub fn source(s: &Scratch, name: &str, n: u64) -> Source {
    Source {
        source_id: format!("cccccccc-0000-4000-8000-{n:012}"),
        display_name: format!("Example {name}"),
        root: s.dir(name),
    }
}

pub fn put(src: &Source, rel: &str, bytes: &[u8]) {
    let p = src.root.join(rel);
    fs::create_dir_all(p.parent().unwrap()).unwrap();
    fs::write(p, bytes).unwrap();
}

/// `len` bytes that differ from file to file.
pub fn bytes(seed: u8, len: usize) -> Vec<u8> {
    (0..len)
        .map(|i| (i as u8).wrapping_mul(31).wrapping_add(seed))
        .collect()
}

pub fn cands(src: &Source) -> Vec<Candidate> {
    source::scan(src).unwrap().candidates
}

fn used(path: &Path) -> u64 {
    let mut total = 0;
    for entry in fs::read_dir(path).unwrap().flatten() {
        let meta = entry.metadata().unwrap();
        total += if meta.is_dir() {
            4096 + used(&entry.path())
        } else {
            meta.len().div_ceil(4096) * 4096
        };
    }
    total
}

/// A card of `usable` bytes above the reserve's floor; free space follows what is on it.
pub fn space_of(card: PathBuf, usable: u64) -> impl FnMut(&Volume) -> Res<Space> {
    move |_| {
        let capacity = RESERVE_FLOOR + usable;
        Ok(Space {
            free: capacity.saturating_sub(used(&card)),
            capacity,
            unit: 4096,
            max_file: u64::MAX,
        })
    }
}

/// A hook that stops (a simulated crash) the first time `point` is reached.
pub fn crash_at(point: Point) -> impl FnMut(Point) -> Res<()> {
    let mut done = false;
    move |p| {
        if p == point && !done {
            done = true;
            return Err(CardError::new(Kind::Interrupted, "simulated crash"));
        }
        Ok(())
    }
}

/// A hook that makes `point` fail with the system error `errno` (EIO, ENOSPC, EROFS).
pub fn fail_at(point: Point, errno: i32) -> impl FnMut(Point) -> Res<()> {
    move |p| {
        if p == point {
            return Err(CardError::io(
                "injected",
                &std::io::Error::from_raw_os_error(errno),
            ));
        }
        Ok(())
    }
}

/// Run over `cands` on `vol` with a card of `usable` bytes.
pub fn run(
    vol: &Volume,
    cat: &mut Memory,
    card: &Path,
    usable: u64,
    cands: &[Candidate],
    hook: &mut dyn super::commit::Hook,
) -> RunResult {
    let mut space = space_of(card.to_path_buf(), usable);
    let run_id = format::new_uuid().unwrap();
    Run {
        vol,
        cat,
        run_id: &run_id,
        space: &mut space,
        hook,
    }
    .go(cands)
    .unwrap()
}

/// The files in `NekoBackup/<top>` and its `<hh>` folders.
pub fn files(card: &Path, top: &str) -> Vec<String> {
    let base = card.join(format::ROOT).join(top);
    let mut out = Vec::new();
    for entry in fs::read_dir(&base).unwrap().flatten() {
        if entry.path().is_dir() {
            for inner in fs::read_dir(entry.path()).unwrap().flatten() {
                out.push(inner.file_name().to_string_lossy().into_owned());
            }
        } else {
            out.push(entry.file_name().to_string_lossy().into_owned());
        }
    }
    out.sort();
    out
}

/// Every committed record on the card.
pub fn records(card: &Path) -> Vec<Record> {
    let base = card.join(format::ROOT).join(format::RECORDS);
    let mut out = Vec::new();
    for entry in fs::read_dir(&base).unwrap().flatten() {
        for inner in fs::read_dir(entry.path()).unwrap().flatten() {
            out.push(Record::parse(&fs::read(inner.path()).unwrap()).unwrap());
        }
    }
    out.sort_by(|a, b| {
        a.txn()
            .original_relative_path
            .cmp(&b.txn().original_relative_path)
    });
    out
}

/// The archived bytes of `record`.
pub fn payload(card: &Path, record: &Record) -> Vec<u8> {
    fs::read(
        card.join(format::ROOT)
            .join(&record.txn().archive_relative_path),
    )
    .unwrap()
}
