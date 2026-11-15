//! The checksum ledger: a permanent record of what each backed-up file must contain.
//!
//! Rules (see STORAGE.md, "How backups are verified"):
//! * A file's checksum is recorded the moment a copy of it is first verified, and is
//!   never replaced afterwards, so later damage can never become the "expected" value.
//! * A file without an entry is *unverified* until two independent copies agree.
//! * The ledger is written whole to `.neko-ledger.json.<uuid>.tmp`, fsync'd, renamed
//!   over the old one and the folder fsync'd, always after the files it lists are
//!   published. Only one process writes it at a time (`.neko-ledger.lock`).
//! * Every path in it is relative, has no `.`/`..`/empty parts, and is opened only
//!   through folders that are not symlinks.
//! * The one way an entry leaves: the recovery menu's "accept that this saved copy is
//!   lost" (recovery_wizard.py). The copy goes to `neko-damaged/`, and its entry moves to
//!   `retired` with when and why, so the checks stop expecting a copy that is gone for
//!   good while the record of what it held stays.
use crate::common::{self, Error, Result};
use serde_core::de::{Deserialize, Deserializer, IgnoredAny, MapAccess, Visitor};
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Component, Path, PathBuf};

pub const LEDGER: &str = ".neko-ledger.json";
const LOCK: &str = ".neko-ledger.lock";
const FORMAT: &str = "neko-ledger-v1";
/// Rewrite the ledger after this many new entries, so an interrupted run loses little.
const CHECKPOINT: usize = 5000;
/// ... or, in a big ledger, after this share of its entries is new: each rewrite is the
/// whole file, and every 5,000 the first copy of a million photos wrote about 22 GB of
/// ledger (deep check 305); this way about 2.4 GB, and a run cut off loses at most a
/// tenth of what it recorded (those files are checked again on the next run).
const CHECKPOINT_SHARE: usize = 10;
/// Whether `unsaved` new entries in a ledger of `entries` are worth a rewrite now.
fn checkpoint_due(unsaved: usize, entries: usize) -> bool {
    unsaved >= CHECKPOINT.max(entries / CHECKPOINT_SHARE)
}

/// Files the ledger keeps next to the tree it describes; never backed up as data.
pub fn own_file(name: &str) -> bool {
    name == LEDGER
        || name == LOCK
        || (name.starts_with(".neko-ledger.json.") && name.ends_with(".tmp"))
}

#[derive(Clone, Debug, PartialEq)]
pub struct Entry {
    pub sha256: String,
    pub size: u64,
    pub verified_at: f64,
    /// How it was first verified: "copied" (three matching checksums while copying)
    /// or "agreed" (two independent copies matched).
    pub how: String,
}

/// What recording a checksum did.
#[derive(Debug, PartialEq)]
pub enum Recorded {
    New,
    Same,
    /// An entry exists with another checksum. It is kept; the caller reports damage.
    Conflict,
}

pub struct Ledger {
    root: PathBuf,
    entries: BTreeMap<String, Entry>,
    /// Entries given up as lost in the recovery menu, kept as written there.
    retired: Map<String, Value>,
    unsaved: usize,
    _lock: Option<File>,
}

/// A ledger path must be plain and relative: no leading `/`, no `.`, `..` or empty
/// parts, no NUL. Anything else means the ledger is not trustworthy.
pub fn valid_path(rel: &str) -> bool {
    !rel.is_empty()
        && !rel.contains('\0')
        && !rel.starts_with('/')
        && !rel
            .split('/')
            .any(|part| part.is_empty() || part == "." || part == "..")
        && Path::new(rel)
            .components()
            .all(|c| matches!(c, Component::Normal(_)))
}

fn sha256_hex(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}

impl Ledger {
    /// Open the ledger of `root` for writing (taking the single-writer lock).
    pub fn open(root: &Path) -> Result<Ledger> {
        common::safe(root)?;
        let lock = take_lock(&root.join(LOCK))?;
        let mut ledger = Ledger::read(root)?;
        ledger._lock = Some(lock);
        Ok(ledger)
    }
    /// Open it for writing, waiting up to `max` for another job that has it open (the
    /// weekly USB copy and the photo archive both write the HDD archive's ledger, and
    /// one may still be at work when the other starts).
    pub fn open_waiting(root: &Path, max: std::time::Duration) -> Result<Ledger> {
        common::safe(root)?;
        let lock = common::lock_wait(&root.join(LOCK), max)?;
        let mut ledger = Ledger::read(root)?;
        ledger._lock = Some(lock);
        Ok(ledger)
    }
    /// Read a ledger without locking (restores and checks only read it).
    pub fn read(root: &Path) -> Result<Ledger> {
        let path = root.join(LEDGER);
        let mut entries = BTreeMap::new();
        let mut retired = Map::new();
        if fs::symlink_metadata(&path).is_ok() {
            if !fs::symlink_metadata(&path)?.is_file() {
                return Err(Error::new("Checksum ledger is not a regular file"));
            }
            let reader = std::io::BufReader::with_capacity(1 << 16, common::file(&path)?);
            let read: Contents = serde_json::from_reader(reader).map_err(|e| {
                if e.is_io() {
                    Error::new(&format!(
                        "The checksum list {} could not be read: {e}",
                        path.display()
                    ))
                } else {
                    damaged(&path)
                }
            })?;
            let (Some(FORMAT), Some(rows)) = (read.format.as_deref(), read.entries) else {
                return Err(damaged(&path));
            };
            if let Some(rel) = rows.invalid {
                return Err(Error::new(&format!(
                    "Checksum ledger entry is invalid: {}",
                    common::sanitize(&rel)
                )));
            }
            entries = rows.entries;
            retired = retired_rows(&read.retired)?;
        }
        Ok(Ledger {
            root: root.to_path_buf(),
            entries,
            retired,
            unsaved: 0,
            _lock: None,
        })
    }
    pub fn get(&self, rel: &str) -> Option<&Entry> {
        self.entries.get(rel)
    }
    /// Every recorded path: what the tree is expected to hold.
    pub fn keys(&self) -> impl Iterator<Item = &String> {
        self.entries.keys()
    }
    /// Record a verified checksum once. An existing entry is never replaced.
    pub fn record(&mut self, rel: &str, sha256: &str, size: u64, how: &str) -> Result<Recorded> {
        if !valid_path(rel) || !sha256_hex(sha256) {
            return Err(Error::new("Refusing an invalid checksum ledger entry"));
        }
        if self._lock.is_none() {
            return Err(Error::new("Checksum ledger opened read-only"));
        }
        if let Some(old) = self.entries.get(rel) {
            return Ok(if old.sha256 == sha256 {
                Recorded::Same
            } else {
                Recorded::Conflict
            });
        }
        self.entries.insert(
            rel.to_string(),
            Entry {
                sha256: sha256.to_string(),
                size,
                verified_at: common::now(),
                how: how.to_string(),
            },
        );
        self.unsaved += 1;
        if checkpoint_due(self.unsaved, self.entries.len()) {
            self.save()?;
        }
        Ok(Recorded::New)
    }
    /// Take over a checksum another copy's ledger recorded (when this one lost it), as
    /// it was recorded there. An existing entry is never replaced.
    pub fn adopt(&mut self, rel: &str, entry: &Entry) -> Result<Recorded> {
        if !valid_path(rel) || !sha256_hex(&entry.sha256) {
            return Err(Error::new("Refusing an invalid checksum ledger entry"));
        }
        if let Some(old) = self.entries.get(rel) {
            return Ok(if old.sha256 == entry.sha256 {
                Recorded::Same
            } else {
                Recorded::Conflict
            });
        }
        self.entries.insert(rel.to_string(), entry.clone());
        self.unsaved += 1;
        Ok(Recorded::New)
    }
    /// A saved file (or a folder of them) moved within the tree, as when a copy is kept
    /// aside: its checksums go with it, unchanged. Nothing recorded is dropped.
    pub fn moved(&mut self, from: &str, to: &str) -> Result<()> {
        if !valid_path(from) || !valid_path(to) {
            return Err(Error::new("Refusing an invalid checksum ledger entry"));
        }
        if self._lock.is_none() {
            return Err(Error::new("Checksum ledger opened read-only"));
        }
        let inside = format!("{from}/");
        let keys: Vec<String> = self
            .entries
            .keys()
            .filter(|k| *k == from || k.starts_with(&inside))
            .cloned()
            .collect();
        for key in keys {
            let new = format!("{to}{}", &key[from.len()..]);
            if self.entries.contains_key(&new) {
                return Err(Error::new(
                    "Checksum ledger already has an entry where a copy is moved",
                ));
            }
            if let Some(entry) = self.entries.remove(&key) {
                self.entries.insert(new, entry);
                self.unsaved += 1;
            }
        }
        Ok(())
    }
    /// Every recorded path with its entry.
    pub fn entries(&self) -> impl Iterator<Item = (&String, &Entry)> {
        self.entries.iter()
    }
    /// Whether the recovery menu gave up `rel` as lost while it held `sha256`: another
    /// copy's ledger that still records it then brings back nothing.
    pub fn retired_as(&self, rel: &str, sha256: &str) -> bool {
        self.retired
            .get(rel)
            .is_some_and(|row| row["sha256"] == sha256)
    }
    /// Write the ledger's text to `out`; `kept`'s own entries win over this one's (the
    /// other copy's records are never replaced either). Written row by row from the
    /// entries: a copy of them all and a JSON tree of it took 2.7 GB to add one photo to a
    /// ledger of a million (deep check 223).
    fn write(&self, kept: Option<&Ledger>, out: &mut dyn Write) -> Result<()> {
        let mut all: BTreeMap<&String, &Entry> = self.entries.iter().collect();
        let mut retired = self.retired.clone();
        if let Some(kept) = kept {
            all.extend(
                kept.entries
                    .iter()
                    .filter(|(k, e)| !self.retired_as(k, &e.sha256)),
            );
            for (k, row) in &kept.retired {
                retired.entry(k.clone()).or_insert_with(|| row.clone());
            }
        }
        // The keys in the order a JSON object of them is written (entries, format, retired).
        // Each piece is made first and then written, so a full drive is said as one.
        out.write_all(b"{\"entries\":{")?;
        for (n, (rel, e)) in all.into_iter().enumerate() {
            if n > 0 {
                out.write_all(b",")?;
            }
            out.write_all(&serde_json::to_vec(rel)?)?;
            out.write_all(b":")?;
            let row = json!({"sha256": e.sha256, "size": e.size, "verified_at": e.verified_at, "how": e.how});
            out.write_all(&serde_json::to_vec(&row)?)?;
        }
        write!(out, "}},\"format\":\"{FORMAT}\"")?;
        if !retired.is_empty() {
            out.write_all(b",\"retired\":")?;
            out.write_all(&serde_json::to_vec(&retired)?)?;
        }
        out.write_all(b"}")?;
        Ok(())
    }
    /// Publish the ledger durably (call after the files it lists are published).
    pub fn save(&mut self) -> Result<()> {
        if self._lock.is_none() {
            return Err(Error::new("Checksum ledger opened read-only"));
        }
        if self.unsaved == 0 && self.root.join(LEDGER).exists() {
            return Ok(());
        }
        publish(&self.root, &|out| self.write(None, out))?;
        self.unsaved = 0;
        Ok(())
    }
    /// Publish this ledger's entries as the ledger of another tree (the USB mirror),
    /// keeping every entry that tree's own ledger (`kept`) already has.
    pub fn publish_copy(&self, other_root: &Path, kept: Option<&Ledger>) -> Result<()> {
        common::safe(other_root)?;
        let _lock = take_lock(&other_root.join(LOCK))?;
        publish(other_root, &|out| self.write(kept, out))
    }
}

/// A ledger that can't be read (cut short, overwritten): which file, and what to do. The
/// archive and the weekly copy failed every run with "Checksum ledger is unreadable",
/// naming neither (0.51.2 review, a truncated HDD archive ledger). Set aside, it is
/// started anew: the weekly copy takes back what the USB drive's copy of it recorded.
fn damaged(path: &Path) -> Error {
    let shown = path.display();
    Error::new(&format!(
        "The checksum list {shown} is damaged and can't be read. Move it aside with  sudo mv {shown} {shown}.unreadable  and the next backup starts a new one"
    ))
}
/// The `retired` part of a ledger: {path: {sha256, retired_at, why, ...}}. Like the
/// entries, a row with an unsafe path or no SHA-256 means the ledger is not trusted.
fn retired_rows(value: &Value) -> Result<Map<String, Value>> {
    let Some(rows) = value.as_object() else {
        return Ok(Map::new());
    };
    for (rel, row) in rows {
        if !valid_path(rel) || !row["sha256"].as_str().is_some_and(sha256_hex) {
            return Err(Error::new(&format!(
                "Checksum ledger entry is invalid: {}",
                common::sanitize(rel)
            )));
        }
    }
    Ok(rows.clone())
}
/// A ledger file as read, straight into its entries. Read as one JSON tree first, a
/// ledger of a million photos (217 MB) took 1.5 GB of memory (deep check 223); each row
/// is now looked at on its own and only the entry is kept.
#[derive(Default)]
struct Contents {
    format: Option<String>,
    entries: Option<Rows>,
    retired: Value,
}
#[derive(Default)]
struct Rows {
    entries: BTreeMap<String, Entry>,
    /// The first row that is not a valid entry, said once the file is known to be a ledger.
    invalid: Option<String>,
}
/// The entry a ledger row stands for, None when the row is not a valid one.
fn entry_of(rel: &str, row: &Value) -> Option<Entry> {
    let sha256 = row["sha256"].as_str().unwrap_or("");
    if !valid_path(rel) || !sha256_hex(sha256) || !row["size"].is_u64() {
        return None;
    }
    Some(Entry {
        sha256: sha256.to_string(),
        size: row["size"].as_u64().unwrap_or(0),
        verified_at: row["verified_at"].as_f64().unwrap_or(0.0),
        how: row["how"].as_str().unwrap_or("").to_string(),
    })
}
impl<'de> Deserialize<'de> for Contents {
    fn deserialize<D: Deserializer<'de>>(d: D) -> std::result::Result<Self, D::Error> {
        struct File;
        impl<'de> Visitor<'de> for File {
            type Value = Contents;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("a checksum ledger")
            }
            fn visit_map<A: MapAccess<'de>>(
                self,
                mut map: A,
            ) -> std::result::Result<Contents, A::Error> {
                let mut read = Contents::default();
                while let Some(key) = map.next_key::<String>()? {
                    match key.as_str() {
                        "format" => {
                            read.format = map.next_value::<Value>()?.as_str().map(String::from)
                        }
                        "entries" => read.entries = Some(map.next_value()?),
                        "retired" => read.retired = map.next_value()?,
                        _ => {
                            map.next_value::<IgnoredAny>()?;
                        }
                    }
                }
                Ok(read)
            }
        }
        d.deserialize_map(File)
    }
}
impl<'de> Deserialize<'de> for Rows {
    fn deserialize<D: Deserializer<'de>>(d: D) -> std::result::Result<Self, D::Error> {
        struct Each;
        impl<'de> Visitor<'de> for Each {
            type Value = Rows;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("the ledger's entries")
            }
            fn visit_map<A: MapAccess<'de>>(
                self,
                mut map: A,
            ) -> std::result::Result<Rows, A::Error> {
                let mut rows = Rows::default();
                while let Some((rel, row)) = map.next_entry::<String, Value>()? {
                    match entry_of(&rel, &row) {
                        Some(entry) => {
                            rows.entries.insert(rel, entry);
                        }
                        None => {
                            rows.invalid.get_or_insert(rel);
                        }
                    }
                }
                Ok(rows)
            }
        }
        d.deserialize_map(Each)
    }
}
/// The single-writer lock. A program another thread of this job just started can hold
/// a copy of the lock for an instant before it starts running (its copy is closed then),
/// so a busy lock is retried briefly before it counts as another writer.
fn take_lock(path: &Path) -> Result<File> {
    let start = std::time::Instant::now();
    loop {
        match common::lock(path) {
            Ok(file) => return Ok(file),
            Err(_) if start.elapsed() < std::time::Duration::from_secs(2) => {
                std::thread::sleep(std::time::Duration::from_millis(50))
            }
            Err(_) => {
                return Err(Error::new(
                    "The checksum ledger is in use by another NekoServer job",
                ))
            }
        }
    }
}
/// `.neko-ledger.json.<uuid>.tmp` -> fsync -> rename -> fsync the folder.
fn publish(root: &Path, text: &dyn Fn(&mut dyn Write) -> Result<()>) -> Result<()> {
    let temp = root.join(format!("{LEDGER}.{}.tmp", uuid4()?));
    let result = (|| {
        let f = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(&temp)?;
        let mut out = std::io::BufWriter::with_capacity(1 << 16, f);
        text(&mut out)?;
        let f = out.into_inner().map_err(|e| e.into_error())?;
        f.sync_all()?;
        drop(f);
        common::safe(&root.join(LEDGER))?;
        fs::rename(&temp, root.join(LEDGER))?;
        File::open(root)?.sync_all()?;
        Ok(())
    })();
    let _ = fs::remove_file(&temp);
    result
}

/// A random (version 4) UUID from the kernel's random source.
fn uuid4() -> Result<String> {
    let mut b = [0u8; 16];
    File::open("/dev/urandom")?.read_exact(&mut b)?;
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    let h: Vec<String> = b.iter().map(|x| format!("{x:02x}")).collect();
    Ok(format!(
        "{}-{}-{}-{}-{}",
        h[0..4].concat(),
        h[4..6].concat(),
        h[6..8].concat(),
        h[8..10].concat(),
        h[10..16].concat()
    ))
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    fn scratch(name: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!("neko-ledger-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        root
    }
    const A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    #[test]
    fn the_first_copy_of_a_million_photos_does_not_rewrite_the_ledger_for_ever() {
        // Deep check 305: a rewrite every 5,000 new files wrote about 22 GB of ledger
        // (219 bytes a row, measured on a ledger of a million). Counted as `record` saves.
        let (mut written, mut unsaved) = (0u64, 0usize);
        for entries in 1..=1_000_000usize {
            unsaved += 1;
            if checkpoint_due(unsaved, entries) {
                written += entries as u64 * 219;
                unsaved = 0;
            }
        }
        assert!(written < 3_000_000_000, "{written} bytes written");
        // A small ledger is still saved every 5,000, and a big one at most a tenth behind.
        assert!(checkpoint_due(CHECKPOINT, 10_000) && !checkpoint_due(CHECKPOINT - 1, 10_000));
        assert!(checkpoint_due(100_000, 1_000_000) && !checkpoint_due(99_999, 1_000_000));
    }
    const B: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
    /// Seen with the real weekly copy (0.51.2 review): a cut-short HDD archive ledger
    /// failed it, and every archive run, with "Checksum ledger is unreadable".
    #[test]
    fn a_damaged_ledger_is_named_with_what_to_do() {
        let root = scratch("damaged");
        let mut ledger = Ledger::open(&root).unwrap();
        ledger.record("upload/a.jpg", A, 4, "copied").unwrap();
        ledger.save().unwrap();
        let path = root.join(LEDGER);
        let whole = fs::read(&path).unwrap();
        for bad in [
            &whole[..whole.len() / 2],
            br#"{"format":"other"}"#.as_slice(),
        ] {
            fs::write(&path, bad).unwrap();
            let said = Ledger::read(&root).err().unwrap().to_string();
            let shown = path.display().to_string();
            assert!(
                said.contains(&format!("sudo mv {shown} {shown}.unreadable ")),
                "{said}"
            );
        }
        // Done as it says, the tree has no ledger, and a new one is started.
        fs::rename(&path, root.join(format!("{LEDGER}.unreadable"))).unwrap();
        assert_eq!(Ledger::read(&root).unwrap().keys().count(), 0);
        let _ = fs::remove_dir_all(&root);
    }
    #[test]
    fn a_recorded_checksum_is_never_replaced() {
        let root = scratch("immutable");
        let mut ledger = Ledger::open(&root).unwrap();
        assert_eq!(
            ledger.record("upload/a.jpg", A, 4, "copied").unwrap(),
            Recorded::New
        );
        assert_eq!(
            ledger.record("upload/a.jpg", A, 4, "copied").unwrap(),
            Recorded::Same
        );
        // Live contents changed (or rotted): the history keeps the first checksum.
        assert_eq!(
            ledger.record("upload/a.jpg", B, 4, "agreed").unwrap(),
            Recorded::Conflict
        );
        ledger.save().unwrap();
        drop(ledger);
        let again = Ledger::read(&root).unwrap();
        assert_eq!(again.get("upload/a.jpg").unwrap().sha256, A);
        let names: Vec<String> = fs::read_dir(&root)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().to_string())
            .collect();
        assert!(!names.iter().any(|n| n.ends_with(".tmp")), "{names:?}");
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn unsafe_paths_are_refused_and_a_tampered_ledger_is_not_trusted() {
        for bad in [
            "/etc/passwd",
            "../x",
            "a/../b",
            "a//b",
            "./a",
            "",
            "a/.",
            "a\0b",
        ] {
            assert!(!valid_path(bad), "{bad:?}");
        }
        assert!(valid_path("upload/u/IMG 1.jpg"));
        let root = scratch("tampered");
        let mut ledger = Ledger::open(&root).unwrap();
        assert!(ledger.record("../escape", A, 1, "copied").is_err());
        drop(ledger);
        fs::write(
            root.join(LEDGER),
            format!(r#"{{"format":"{FORMAT}","entries":{{"../../etc/shadow":{{"sha256":"{A}","size":1}}}}}}"#),
        )
        .unwrap();
        assert!(Ledger::read(&root).is_err());
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn only_one_writer_at_a_time_and_readers_cannot_write() {
        let root = scratch("lock");
        let first = Ledger::open(&root).unwrap();
        assert!(Ledger::open(&root).is_err());
        drop(first);
        let mut reader = Ledger::read(&root).unwrap();
        assert!(reader.record("a", A, 1, "copied").is_err());
        assert!(Ledger::open(&root).is_ok());
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn checksums_are_sha256() {
        // FIPS 180-2 test vectors: "abc" and the empty input.
        let root = scratch("sha256");
        fs::write(root.join("abc"), b"abc").unwrap();
        fs::write(root.join("empty"), b"").unwrap();
        assert_eq!(
            common::digest(&root.join("abc")).unwrap(),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        assert_eq!(
            common::digest(&root.join("empty")).unwrap(),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        // The ledger only takes SHA-256 values (64 lowercase hex characters).
        let mut ledger = Ledger::open(&root).unwrap();
        assert!(ledger.record("a", "abc123", 1, "copied").is_err()); // too short
        assert!(ledger.record("a", &"A".repeat(64), 1, "copied").is_err()); // not lowercase hex
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn an_entry_given_up_as_lost_stays_retired_and_is_not_brought_back() {
        let root = scratch("retired");
        let other = scratch("retired-usb");
        // As the recovery menu writes it: the entry moved to "retired", with why.
        let text = format!(
            r#"{{"format":"{FORMAT}","entries":{{"b.txt":{{"sha256":"{B}","size":1}}}},"retired":{{"a.txt":{{"sha256":"{A}","size":1,"retired_at":1,"why":"lost"}}}}}}"#
        );
        fs::write(root.join(LEDGER), &text).unwrap();
        // The other copy's ledger still records the lost copy.
        fs::write(
            other.join(LEDGER),
            format!(r#"{{"format":"{FORMAT}","entries":{{"a.txt":{{"sha256":"{A}","size":1}}}}}}"#),
        )
        .unwrap();
        let mut ledger = Ledger::open(&root).unwrap();
        assert!(ledger.get("a.txt").is_none());
        assert!(ledger.retired_as("a.txt", A) && !ledger.retired_as("a.txt", B));
        ledger.record("c.txt", A, 1, "copied").unwrap();
        ledger.save().unwrap();
        let usb = Ledger::read(&other).unwrap();
        ledger.publish_copy(&other, Some(&usb)).unwrap();
        for tree in [&root, &other] {
            let saved: Value =
                serde_json::from_slice(&fs::read(tree.join(LEDGER)).unwrap()).unwrap();
            assert!(saved["entries"].get("a.txt").is_none(), "{saved}");
            assert_eq!(saved["retired"]["a.txt"]["why"], "lost", "{saved}");
        }
        // A retired row that is not safe makes the ledger untrusted, like an entry.
        fs::write(root.join(LEDGER), text.replace("a.txt", "../a.txt")).unwrap();
        drop(ledger);
        assert!(Ledger::read(&root).is_err());
        let _ = fs::remove_dir_all(root);
        let _ = fs::remove_dir_all(other);
    }
    /// Memory this thread holds from the allocator (now, most since `measured` began). Counted
    /// per thread, so the tests running beside one don't count in it.
    struct Counting;
    thread_local! {
        static HELD: std::cell::Cell<(isize, isize)> = const { std::cell::Cell::new((0, 0)) };
    }
    fn note(change: isize) {
        let _ = HELD.try_with(|held| {
            let (now, most) = held.get();
            held.set((now + change, most.max(now + change)));
        });
    }
    unsafe impl std::alloc::GlobalAlloc for Counting {
        unsafe fn alloc(&self, layout: std::alloc::Layout) -> *mut u8 {
            note(layout.size() as isize);
            std::alloc::System.alloc(layout)
        }
        unsafe fn dealloc(&self, ptr: *mut u8, layout: std::alloc::Layout) {
            note(-(layout.size() as isize));
            std::alloc::System.dealloc(ptr, layout)
        }
        unsafe fn realloc(&self, ptr: *mut u8, layout: std::alloc::Layout, size: usize) -> *mut u8 {
            note(size as isize - layout.size() as isize);
            std::alloc::System.realloc(ptr, layout, size)
        }
    }
    #[global_allocator]
    static COUNTING: Counting = Counting;
    /// What `work` gave back, the memory it still holds after, and the most it held.
    pub(crate) fn measured<T>(work: impl FnOnce() -> T) -> (T, isize, isize) {
        let start = HELD.with(|held| {
            let (now, _) = held.get();
            held.set((now, now));
            now
        });
        let out = work();
        let (now, most) = HELD.with(|held| held.get());
        (out, now - start, most - start)
    }
    /// Deep check 223 (a ledger of a million photos, 217 MB): reading it took 1.5 GB, and
    /// the archive that added one photo to it 2.7 GB, as the whole file, a JSON tree of it
    /// and a copy of every entry were in memory at once.
    #[test]
    fn a_big_ledger_is_read_and_saved_without_a_copy_of_it_in_memory() {
        let root = scratch("big");
        let mut ledger = Ledger::open(&root).unwrap();
        for n in 0..20_000 {
            let rel = format!("upload/library/2026/IMG_{n:06}.jpg");
            ledger
                .record(&rel, &format!("{n:064x}"), 3_000_000, "copied")
                .unwrap();
        }
        ledger.save().unwrap();
        drop(ledger);
        let file = fs::metadata(root.join(LEDGER)).unwrap().len() as isize;
        let (mut ledger, kept, most) = measured(|| Ledger::open(&root).unwrap());
        assert_eq!(ledger.keys().count(), 20_000);
        assert!(
            most - kept < file / 2,
            "read: {most} at most, {kept} kept, file {file}"
        );
        ledger.record("upload/new.jpg", A, 1, "copied").unwrap();
        let ((), _, most) = measured(|| ledger.save().unwrap());
        assert!(most < file / 2, "save: {most} at most, file {file}");
        let other = scratch("big-copy");
        let ((), _, most) = measured(|| ledger.publish_copy(&other, Some(&ledger)).unwrap());
        assert!(most < file / 2, "copy: {most} at most, file {file}");
        // What was written reads back the same, byte for byte as before.
        drop(ledger);
        let again = Ledger::read(&root).unwrap();
        assert_eq!(again.keys().count(), 20_001);
        assert_eq!(again.get("upload/new.jpg").unwrap().sha256, A);
        let text: Value = serde_json::from_slice(&fs::read(root.join(LEDGER)).unwrap()).unwrap();
        assert_eq!(
            serde_json::to_vec(&text).unwrap(),
            fs::read(root.join(LEDGER)).unwrap()
        );
        assert_eq!(
            fs::read(other.join(LEDGER)).unwrap(),
            fs::read(root.join(LEDGER)).unwrap()
        );
        let _ = fs::remove_dir_all(root);
        let _ = fs::remove_dir_all(other);
    }
    #[test]
    fn uuids_are_random_version_4() {
        let (a, b) = (uuid4().unwrap(), uuid4().unwrap());
        assert_ne!(a, b);
        assert_eq!(a.len(), 36);
        assert_eq!(&a[14..15], "4");
    }
}
