use crate::ledger::{self, Ledger, Recorded};
use crate::{
    common::{self, Error, Result},
    Args,
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, Metadata, OpenOptions},
    io::{Read, Write},
    os::unix::{
        fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
        process::CommandExt,
    },
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};
/// The same file, unchanged: identity, size, modification and status-change times.
/// ctime also moves on edits that keep the size and set mtime back.
fn same(a: &Metadata, b: &Metadata) -> bool {
    a.dev() == b.dev()
        && a.ino() == b.ino()
        && a.len() == b.len()
        && a.mtime() == b.mtime()
        && a.mtime_nsec() == b.mtime_nsec()
        && a.ctime() == b.ctime()
        && a.ctime_nsec() == b.ctime_nsec()
}
/// How existing backup copies are checked.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Check {
    /// Skip a copy whose size matches and that was written after the source last changed.
    Quick,
    /// Compare every existing copy byte for byte (checksums).
    Full,
}
/// A backup copy already holds this source's content, decided without reading either
/// file: same size, and the copy was written after the source's last *status change*.
/// The status-change time (ctime) cannot be set back by tools that preserve
/// timestamps; any rewrite, replacement or metadata change moves it forward, so such a
/// source is compared by content. Full checks (every 4 weeks) compare everything.
fn copied_since_change(source: &Metadata, copy: &Metadata) -> bool {
    source.len() == copy.len()
        && (copy.mtime(), copy.mtime_nsec()) >= (source.ctime(), source.ctime_nsec())
}
/// Weekly jobs compare every copy by checksum once every this many seconds (28 days).
const FULL_CHECK_EVERY: f64 = 28.0 * 86400.0;
/// Is a full checksum comparison due for `job`? Recorded in reports/jobs/<job>-full-check.json.
pub fn full_check_due(data: &Path, job: &str) -> bool {
    let last = common::json(
        &data.join(format!("reports/jobs/{job}-full-check.json")),
        64000,
    )["at"]
        .as_f64();
    due_since(last, common::now())
}
/// Due when there is no valid record, when it is older than FULL_CHECK_EVERY, or when it
/// lies in the future: a check made while the clock was wrong (set to 2030, say) must
/// not postpone the next one for years once the clock is corrected.
fn due_since(last: Option<f64>, now: f64) -> bool {
    match last {
        Some(at) if at.is_finite() && at > 0.0 && at <= now + 300.0 => now - at >= FULL_CHECK_EVERY,
        _ => true,
    }
}
fn record_full_check(data: &Path, job: &str) -> Result<()> {
    common::atomic_json(
        &data.join(format!("reports/jobs/{job}-full-check.json")),
        &json!({"at": common::now()}),
    )
}
fn separate(source: &Path, dest: &Path) -> Result<(PathBuf, PathBuf)> {
    common::safe(source)?;
    common::safe(dest)?;
    if !source.is_dir() {
        return Err(Error::new("Source directory missing"));
    }
    let s = fs::canonicalize(source)?;
    let d = if dest.exists() {
        fs::canonicalize(dest)?
    } else {
        dest.to_path_buf()
    };
    if !d.is_absolute() || s == d || s.starts_with(&d) || d.starts_with(&s) {
        return Err(Error::new("Source and destination overlap"));
    }
    if dest.exists() && !dest.is_dir() {
        return Err(Error::new("Destination must be a directory"));
    }
    Ok((s, d))
}
fn list(root: &Path) -> Result<Vec<PathBuf>> {
    let mut rows = fs::read_dir(root)?
        .map(|p| p.map(|p| p.path()))
        .collect::<std::io::Result<Vec<_>>>()?;
    rows.sort();
    Ok(rows)
}
/// `copy_verified` for callers that only need to know whether a copy was (or, on a
/// dry run, would be) made.
pub(crate) fn verified_copy(source: &Path, target: &Path, apply: bool, mode: u32) -> Result<bool> {
    let new = !target.exists();
    let landed = copy_verified(source, target, apply, mode)?;
    Ok(landed.is_some() || (new && !apply))
}
/// Copy `source` to a new `target` and return the checksum that landed (None when the
/// target already held the same content, or on a dry run). The data is streamed into
/// `target.neko-part` while being hashed, fsync'd, then read back from disk and hashed
/// again, and the source is hashed once more: only when all three checksums match and
/// the source did not change (identity, size, mtime, ctime) is the file published
/// (no-replace link or rename, then the folder is fsync'd). Nothing is overwritten.
pub(crate) fn copy_verified(
    source: &Path,
    target: &Path,
    apply: bool,
    mode: u32,
) -> Result<Option<String>> {
    copy_verified_with(source, target, apply, mode, &|| {})
}
/// `copy_verified`; `streamed` runs right after the data was written (tests use it to
/// change the source mid-copy).
fn copy_verified_with(
    source: &Path,
    target: &Path,
    apply: bool,
    mode: u32,
    streamed_hook: &dyn Fn(),
) -> Result<Option<String>> {
    common::safe(source)?;
    common::safe(target)?;
    let before = common::file(source)?.metadata()?;
    if target.exists() {
        if !target.is_file() || common::digest(target)? != common::digest(source)? {
            return Err(Error::new("Collision or changed original; no overwrite"));
        }
        return Ok(None);
    }
    if !apply {
        return Ok(None);
    }
    let parent = target
        .parent()
        .ok_or_else(|| Error::new("Target parent missing"))?;
    common::directory(parent, if mode == 0o600 { 0o700 } else { 0o755 })?;
    common::safe(parent)?;
    let mut name = target
        .file_name()
        .ok_or_else(|| Error::new("Invalid target"))?
        .to_os_string();
    name.push(".neko-part");
    let temp = parent.join(name);
    common::safe(&temp)?;
    if temp.exists() {
        if !fs::symlink_metadata(&temp)?.is_file() {
            return Err(Error::new("Unsafe partial file"));
        }
        fs::remove_file(&temp)?;
    }
    let result = (|| {
        let mut input = common::file(source)?;
        if !same(&before, &input.metadata()?) {
            return Err(Error::new("Source changed before copy"));
        }
        let mut out = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(mode)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(&temp)?;
        out.set_permissions(fs::Permissions::from_mode(mode))?;
        let mut stream = Sha256::new();
        let mut buf = [0u8; 65536];
        loop {
            let n = input.read(&mut buf)?;
            if n == 0 {
                break;
            }
            stream.update(&buf[..n]);
            out.write_all(&buf[..n])?;
        }
        out.flush()?;
        out.sync_all()?;
        drop(out);
        streamed_hook();
        let streamed = format!("{:x}", stream.finalize());
        if !same(&before, &input.metadata()?) || !same(&before, &fs::metadata(source)?) {
            return Err(Error::new("Source changed during the copy; not published"));
        }
        // Re-open the copy from disk: what actually landed, not what was sent.
        let landed = common::digest(&temp)?;
        let original = common::digest(source)?;
        if !same(&before, &fs::metadata(source)?) {
            return Err(Error::new("Source changed during the copy; not published"));
        }
        if streamed != landed || landed != original {
            return Err(Error::new(
                "Copy verification failed (checksums differ); not published",
            ));
        }
        common::publish(&temp, target)?;
        Ok(Some(landed))
    })();
    let _ = fs::remove_file(temp);
    result
}
/// What happened to one file in an add-only copy.
#[derive(Debug, PartialEq)]
pub(crate) enum Added {
    Same,
    New,
    Version,
    /// The existing copy differs from its recorded checksum: it is damaged, not edited.
    Damaged,
}
/// What `kept_copy` did, with what the checksum ledger needs: the saved file it
/// verified (`file`, `sha256`) and how (`how`), and the source's checksum if read.
#[derive(Debug)]
pub(crate) struct Kept {
    pub added: Added,
    pub file: Option<PathBuf>,
    pub sha256: Option<String>,
    pub how: &'static str,
    pub source_sha256: Option<String>,
}
impl Kept {
    fn only(added: Added) -> Kept {
        Kept {
            added,
            file: None,
            sha256: None,
            how: "",
            source_sha256: None,
        }
    }
}
/// `name.ext` -> `name.neko-<stamp>.ext`, keeping the extension so the copy still opens.
fn version_name(target: &Path, stamp: &str) -> PathBuf {
    let name = target.file_name().unwrap_or_default().to_string_lossy();
    let versioned = match name.rsplit_once('.') {
        Some((stem, ext)) if !stem.is_empty() => format!("{stem}.neko-{stamp}.{ext}"),
        _ => format!("{name}.neko-{stamp}"),
    };
    target.with_file_name(versioned)
}
/// Copy `source` to `target` without ever overwriting or deleting anything. If
/// `target` already holds different content (an edited document, an updated photo
/// sidecar), the new content is kept next to it as a dated version instead of
/// failing the whole backup. A version identical to the source is not copied again.
#[cfg(test)]
pub(crate) fn add_only_copy(
    source: &Path,
    target: &Path,
    apply: bool,
    mode: u32,
    check: Check,
) -> Result<Added> {
    Ok(kept_copy(source, target, apply, mode, check, None)?.added)
}
/// `add_only_copy`, told the target's recorded checksum (`expected`, from the ledger):
/// a target that differs from it is reported as `Damaged` instead of being taken for an
/// edit, and the verified files are returned for the ledger.
pub(crate) fn kept_copy(
    source: &Path,
    target: &Path,
    apply: bool,
    mode: u32,
    check: Check,
    expected: Option<&str>,
) -> Result<Kept> {
    if !target.exists() {
        let landed = copy_verified(source, target, apply, mode)?;
        let made = landed.is_some() || !apply;
        return Ok(Kept {
            added: if made { Added::New } else { Added::Same },
            file: Some(target.to_path_buf()),
            source_sha256: landed.clone(),
            sha256: landed,
            how: "copied",
        });
    }
    if !target.is_file() {
        return Err(Error::new("Backup target is not a regular file"));
    }
    let source_meta = fs::metadata(source)?;
    if check == Check::Quick && copied_since_change(&source_meta, &fs::metadata(target)?) {
        return Ok(Kept::only(Added::Same));
    }
    let wanted = common::digest(source)?;
    let have = common::digest(target)?;
    if have == wanted {
        // Back to the first saved content: say so if later versions exist.
        if latest_marker(target).exists() {
            let name = target.file_name().unwrap_or_default().to_string_lossy();
            mark_latest(target, &name, apply)?;
        }
        return Ok(Kept {
            added: Added::Same,
            file: Some(target.to_path_buf()),
            sha256: Some(have),
            how: "agreed",
            source_sha256: Some(wanted),
        });
    }
    if expected.is_some_and(|e| e != have) {
        return Ok(Kept {
            added: Added::Damaged,
            file: Some(target.to_path_buf()),
            sha256: None,
            how: "",
            source_sha256: Some(wanted),
        });
    }
    let name = target
        .file_name()
        .unwrap_or_default()
        .to_string_lossy()
        .to_string();
    let (stem, ext) = match name.rsplit_once('.') {
        Some((s, e)) if !s.is_empty() => (s.to_string(), format!(".{e}")),
        _ => (name.clone(), String::new()),
    };
    let parent = target
        .parent()
        .ok_or_else(|| Error::new("Target parent missing"))?;
    let mut versions = vec![];
    for entry in fs::read_dir(parent)? {
        let path = entry?.path();
        let other = path
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .to_string();
        if other.starts_with(&format!("{stem}.neko-"))
            && other.ends_with(&ext)
            && !other.ends_with(".neko-part")
            && path.is_file()
        {
            versions.push((other, path));
        }
    }
    versions.sort();
    for (other, path) in &versions {
        let quick =
            check == Check::Quick && copied_since_change(&source_meta, &fs::metadata(path)?);
        if quick || common::digest(path)? == wanted {
            // An edit that was undone: no new copy, but this saved version is current again.
            mark_latest(target, other, apply)?;
            return Ok(Kept {
                added: Added::Same,
                file: (!quick).then(|| path.clone()),
                sha256: (!quick).then(|| wanted.clone()),
                how: "agreed",
                source_sha256: Some(wanted),
            });
        }
    }
    // Dated by when the backup saw the change; a file's own timestamp can be set back.
    let stamp = chrono::DateTime::from_timestamp(common::now() as i64, 0)
        .map(|d| d.format("%Y%m%dT%H%M%SZ").to_string())
        .unwrap_or_else(|| (common::now() as i64).to_string());
    let mut versioned = version_name(target, &stamp);
    let mut n = 1;
    while versioned.exists() {
        versioned = version_name(target, &format!("{stamp}-{n}"));
        n += 1;
    }
    let landed = copy_verified(source, &versioned, apply, mode)?;
    mark_latest(
        target,
        &versioned.file_name().unwrap_or_default().to_string_lossy(),
        apply,
    )?;
    Ok(Kept {
        added: Added::Version,
        file: Some(versioned),
        sha256: landed,
        how: "copied",
        source_sha256: Some(wanted),
    })
}
/// `.name.ext.neko-latest` next to a file that has saved versions: names the saved copy
/// (`name.ext` or one of its dated versions) that matches the file as the backup last
/// saw it, so "restore the newest" restores that one, also after an edit was undone.
pub(crate) fn latest_marker(target: &Path) -> PathBuf {
    let name = target.file_name().unwrap_or_default().to_string_lossy();
    target.with_file_name(format!(".{name}.neko-latest"))
}
fn mark_latest(target: &Path, current: &str, apply: bool) -> Result<()> {
    let marker = latest_marker(target);
    if !apply || fs::read(&marker).is_ok_and(|b| b == current.as_bytes()) {
        return Ok(());
    }
    common::atomic(&marker, current.as_bytes(), 0o600)
}
fn walk<F: FnMut(&Path) -> Result<()>>(root: &Path, f: &mut F) -> Result<()> {
    common::safe(root)?;
    for path in list(root)? {
        let meta = fs::symlink_metadata(&path)?;
        if meta.file_type().is_symlink() {
            return Err(Error::new("Source symlink refused"));
        }
        if meta.is_dir() {
            walk(&path, f)?
        } else if meta.is_file() {
            f(&path)?
        } else {
            return Err(Error::new("Special source file refused"));
        }
    }
    Ok(())
}
/// Folder (at the root of a backup tree) that keeps damaged copies aside; never data.
const QUARANTINE: &str = "neko-damaged";
/// What the checksum checks of one backup tree found.
#[derive(Debug, Default, PartialEq)]
pub struct Integrity {
    /// Checksums recorded for the first time this run.
    pub newly_verified: u64,
    /// Files with no recorded checksum and no second copy that agrees yet.
    pub unverified: u64,
    /// Damaged copies replaced from a copy that matches the recorded checksum.
    pub repaired: u64,
    /// The primary copy (HDD archive, or the USB copy of a Nextcloud file) no longer
    /// matches its recorded checksum; the other copy is kept.
    pub damaged: Vec<String>,
    /// Neither copy matches the recorded checksum; both are kept untouched.
    pub both_damaged: Vec<String>,
    /// No checksum recorded yet and the two copies differ; both are kept untouched.
    pub disagree: Vec<String>,
    /// Recorded in the ledger (so backed up and verified once) but no longer there.
    pub missing: Vec<String>,
}
impl Integrity {
    /// Text for a failed job when a copy is damaged; None when everything checks out.
    pub fn problem(&self, what: &str) -> Option<String> {
        let list = |rows: &Vec<String>| {
            let mut text = rows.iter().take(10).cloned().collect::<Vec<_>>().join(", ");
            if rows.len() > 10 {
                text += &format!(" and {} more", rows.len() - 10);
            }
            text
        };
        let mut parts = vec![];
        if !self.damaged.is_empty() {
            parts.push(format!(
                "{} {what} files no longer match their recorded checksum (the other copy is kept): {}",
                self.damaged.len(),
                list(&self.damaged)
            ));
        }
        if !self.both_damaged.is_empty() {
            parts.push(format!(
                "CRITICAL: {} files match their recorded checksum in no copy; every copy is kept untouched: {}",
                self.both_damaged.len(),
                list(&self.both_damaged)
            ));
        }
        if !self.missing.is_empty() {
            parts.push(format!(
                "{} {what} files that were backed up and verified are missing: {}",
                self.missing.len(),
                list(&self.missing)
            ));
        }
        if !self.disagree.is_empty() {
            parts.push(format!(
                "{} files differ between the HDD and the USB drive and had no recorded checksum yet; both are kept untouched: {}",
                self.disagree.len(),
                list(&self.disagree)
            ));
        }
        (!parts.is_empty()).then(|| parts.join(". "))
    }
    fn print(&self, what: &str) {
        if self.newly_verified > 0 {
            println!("{what}: {} checksums recorded", self.newly_verified);
        }
        if self.repaired > 0 {
            println!(
                "{what}: {} damaged copies replaced from a verified copy (the damaged ones are kept in {QUARANTINE}/)",
                self.repaired
            );
        }
        if self.unverified > 0 {
            println!(
                "{what}: {} files have no recorded checksum yet (unverified until a second copy agrees)",
                self.unverified
            );
        }
    }
}
/// Save one tree's check results in `reports/integrity.json` (read by the dashboard and
/// by the recovery menu's repair option). Lists are capped; counts are exact.
pub fn save_integrity(data: &Path, section: &str, report: &Integrity) -> Result<()> {
    let path = data.join("reports/integrity.json");
    let mut all = common::json(&path, 20_000_000);
    if all["format"] != "neko-integrity-v1" {
        all = json!({"format": "neko-integrity-v1", "sections": {}});
    }
    let cap = |rows: &Vec<String>| rows.iter().take(1000).cloned().collect::<Vec<_>>();
    all["sections"][section] = json!({
        "at": common::now(),
        "newly_verified": report.newly_verified,
        "unverified": report.unverified,
        "repaired": report.repaired,
        "damaged": cap(&report.damaged),
        "both_damaged": cap(&report.both_damaged),
        "disagree": cap(&report.disagree),
        "missing": cap(&report.missing),
        "counts": {"damaged": report.damaged.len(), "both_damaged": report.both_damaged.len(), "disagree": report.disagree.len(), "missing": report.missing.len()},
    });
    common::atomic_json(&path, &all)
}
/// `path` relative to `root`, as a ledger key ("upload/u/a.jpg").
fn ledger_key(path: &Path, root: &Path) -> Result<String> {
    path.strip_prefix(root)
        .ok()
        .and_then(|p| p.to_str())
        .filter(|p| ledger::valid_path(p))
        .map(str::to_string)
        .ok_or_else(|| Error::new("File name cannot be recorded in the checksum ledger"))
}
/// Record what `kept_copy` verified; a conflicting existing entry means damage.
fn record_kept(
    ledger: &mut Ledger,
    root: &Path,
    kept: &Kept,
    report: &mut Integrity,
) -> Result<()> {
    let (Some(file), Some(sha)) = (&kept.file, &kept.sha256) else {
        return Ok(());
    };
    let key = ledger_key(file, root)?;
    match ledger.record(&key, sha, fs::metadata(file)?.len(), kept.how)? {
        Recorded::New => report.newly_verified += 1,
        Recorded::Same => {}
        Recorded::Conflict => report.damaged.push(key),
    }
    Ok(())
}
/// Files of a backup tree that are data (not its ledger, notes, partial files or the
/// folder of damaged copies), with their ledger keys.
fn tree_files(root: &Path) -> Result<Vec<(String, PathBuf)>> {
    let mut out = vec![];
    if !root.is_dir() {
        return Ok(out);
    }
    walk(root, &mut |path| {
        let key = ledger_key(path, root)?;
        let name = path.file_name().unwrap_or_default().to_string_lossy();
        if !(key == QUARANTINE
            || key.starts_with(&format!("{QUARANTINE}/"))
            || ledger::own_file(&key)
            || name.ends_with(".neko-latest")
            || name.ends_with(".neko-part")
            || name.starts_with(".neko-"))
        {
            out.push((key, path.to_path_buf()));
        }
        Ok(())
    })?;
    Ok(out)
}
/// The 4-weekly deep audit: read every file again and compare it with its recorded
/// checksum, whatever its size and dates say (silent bit rot keeps both).
/// Ledger entries whose file is gone from `root` (an add-only backup never removes one).
fn missing_from(root: &Path, ledger: &Ledger, report: &mut Integrity) -> Result<()> {
    let present: std::collections::BTreeSet<String> =
        tree_files(root)?.into_iter().map(|(key, _)| key).collect();
    for key in ledger.keys() {
        if !present.contains(key) && !report.missing.contains(key) {
            report.missing.push(key.clone());
        }
    }
    Ok(())
}
fn audit(root: &Path, ledger: &Ledger, report: &mut Integrity) -> Result<()> {
    missing_from(root, ledger, report)?;
    for (key, path) in tree_files(root)? {
        match ledger.get(&key) {
            Some(entry) => {
                if common::digest(&path)? != entry.sha256 && !report.damaged.contains(&key) {
                    report.damaged.push(key);
                }
            }
            None => report.unverified += 1,
        }
    }
    Ok(())
}
fn quarantine_folder(root: &Path) -> PathBuf {
    root.join(QUARANTINE).join(
        chrono::DateTime::from_timestamp(common::now() as i64, 0)
            .map(|d| d.format("%Y%m%dT%H%M%SZ").to_string())
            .unwrap_or_default(),
    )
}
pub fn archive(source: &Path, archive: &Path, apply: bool, age: u64, check: Check) -> Result<u64> {
    let (copied, report) = archive_checked(source, archive, apply, age, check)?;
    match report.problem("HDD archive") {
        Some(problem) => Err(Error::new(&problem)),
        None => Ok(copied),
    }
}
/// The archive copy and what its checksum checks found.
pub fn archive_checked(
    source: &Path,
    archive: &Path,
    apply: bool,
    age: u64,
    check: Check,
) -> Result<(u64, Integrity)> {
    if age < 60 {
        return Err(Error::new("Archive minimum age is 60 seconds"));
    }
    let (source, archive) = separate(source, archive)?;
    let mut copied = 0;
    let mut versions = 0;
    let now = common::now();
    common::directory(&archive, 0o755)?;
    let mut ledger = if apply {
        Ledger::open(&archive)?
    } else {
        Ledger::read(&archive)?
    };
    let mut report = Integrity::default();
    let quarantine = quarantine_folder(&archive);
    for section in ["upload", "library"] {
        let root = source.join(section);
        common::safe(&root)?;
        if !root.exists() {
            continue;
        }
        if !root.is_dir() {
            return Err(Error::new("Immich section must be a directory"));
        }
        // Every regular file in Immich's upload and library folders is kept, whatever
        // its name or format, so formats Immich adds later are backed up too. Symlinks
        // and special files still stop the run (walk), and every copy is checked.
        walk(&root, &mut |path| {
            let meta = fs::metadata(path)?;
            let modified = meta.mtime() as f64 + meta.mtime_nsec() as f64 / 1e9;
            if now - modified < age as f64 {
                return Ok(());
            }
            let target = archive.join(
                path.strip_prefix(&source)
                    .map_err(|_| Error::new("Invalid archive relative path"))?,
            );
            let key = ledger_key(&target, &archive)?;
            let expected = ledger.get(&key).map(|e| e.sha256.clone());
            let kept = kept_copy(path, &target, apply, 0o644, check, expected.as_deref())?;
            match kept.added {
                Added::New => copied += 1,
                Added::Version => versions += 1,
                Added::Same => {}
                // The Immich original still matches what was recorded: it is the
                // trusted copy, so the damaged archive file is replaced from it.
                Added::Damaged if apply && kept.source_sha256 == expected => {
                    replace_damaged(path, &target, &quarantine.join(&key))?;
                    report.repaired += 1;
                }
                Added::Damaged => report.damaged.push(key),
            }
            if apply {
                record_kept(&mut ledger, &archive, &kept, &mut report)?;
            }
            Ok(())
        })?;
    }
    if apply {
        if check == Check::Full {
            audit(&archive, &ledger, &mut report)?;
        } else {
            missing_from(&archive, &ledger, &mut report)?;
        }
        // Published after every file it lists.
        ledger.save()?;
    }
    report.print("Archive");
    if versions > 0 {
        println!("Archive: {versions} changed files kept as dated versions");
    }
    println!(
        "Archive: {copied} new verified originals{}",
        if apply { "" } else { " identified (dry run)" }
    );
    Ok((copied, report))
}
/// Mirror the HDD photo archive to the USB drive, checking both copies against the
/// archive's checksum ledger (recorded when each file was first verified):
///
/// | HDD copy | USB copy | what happens                                              |
/// |----------|----------|-----------------------------------------------------------|
/// | matches  | matches  | nothing                                                   |
/// | matches  | differs  | USB copy moved to `neko-damaged/<time>/`, copied again    |
/// | differs  | matches  | USB copy kept; the HDD file is reported as damaged         |
/// | differs  | differs  | both kept untouched; reported as a critical failure       |
///
/// A file without a recorded checksum is unverified: when both copies agree, their
/// checksum is recorded; when they differ, both are kept and it is reported.
/// Size and dates only let unchanged files be skipped; files named by a
/// current-version note are read every week and everything every 4 weeks (`Full`).
/// The archive's notes and ledger are mirrored after the files they describe.
pub fn copy_tree(source: &Path, dest: &Path, apply: bool, check: Check) -> Result<u64> {
    let (copied, report) = mirror(source, dest, apply, check)?;
    match report.problem("HDD archive") {
        Some(problem) => Err(Error::new(&problem)),
        None => Ok(copied),
    }
}
pub fn mirror(source: &Path, dest: &Path, apply: bool, check: Check) -> Result<(u64, Integrity)> {
    let (source, dest) = separate(source, dest)?;
    let mut ledger = if apply {
        Ledger::open(&source)?
    } else {
        Ledger::read(&source)?
    };
    let mut report = Integrity::default();
    let quarantine = quarantine_folder(&dest);
    let files = tree_files(&source)?;
    // Files a current-version note names: what "restore newest" will take.
    let mut noted = std::collections::BTreeSet::new();
    let mut notes = vec![];
    walk(&source, &mut |path| {
        let name = path.file_name().unwrap_or_default().to_string_lossy();
        if name.starts_with('.') && name.ends_with(".neko-latest") {
            let named = String::from_utf8(common::bytes(path, 4096)?)
                .map_err(|_| Error::new("Unreadable current-version note"))?;
            let folder = path.parent().unwrap_or(&source);
            noted.insert(ledger_key(&folder.join(named.trim()), &source)?);
            notes.push(path.to_path_buf());
        }
        Ok(())
    })?;
    let mut copied = 0;
    for (key, path) in &files {
        let target = dest.join(key);
        let recorded = ledger.get(key).map(|e| e.sha256.clone());
        if !target.exists() {
            // Never copy a damaged archive file onto the USB drive.
            if let Some(expected) = &recorded {
                if common::digest(path)? != *expected {
                    report.damaged.push(key.clone());
                    continue;
                }
            } else {
                report.unverified += 1;
            }
            verified_copy(path, &target, apply, 0o644)?;
            copied += 1;
            continue;
        }
        if !target.is_file() {
            return Err(Error::new("Backup target is not a regular file"));
        }
        let verify = check == Check::Full || noted.contains(key);
        if !verify && copied_since_change(&fs::metadata(path)?, &fs::metadata(&target)?) {
            if recorded.is_none() {
                report.unverified += 1;
            }
            continue;
        }
        let (hdd, usb) = (common::digest(path)?, common::digest(&target)?);
        match recorded {
            Some(expected) => match (hdd == expected, usb == expected) {
                (true, true) => {}
                (true, false) => {
                    if apply {
                        replace_damaged(path, &target, &quarantine.join(key))?;
                    }
                    report.repaired += 1;
                }
                (false, true) => report.damaged.push(key.clone()),
                (false, false) => report.both_damaged.push(key.clone()),
            },
            None if hdd == usb => {
                if apply
                    && ledger.record(key, &hdd, fs::metadata(path)?.len(), "agreed")?
                        == Recorded::New
                {
                    report.newly_verified += 1;
                } else if !apply {
                    report.unverified += 1;
                }
            }
            None => report.disagree.push(key.clone()),
        }
    }
    // Files the ledger expects that are gone from the HDD archive: the USB copy, if it
    // still matches, is kept (repair from it in the recovery menu); if it is gone or
    // damaged too, no good copy is left.
    let present: std::collections::BTreeSet<&String> = files.iter().map(|(k, _)| k).collect();
    let gone: Vec<(String, String)> = ledger
        .keys()
        .filter(|k| !present.contains(k))
        .map(|k| {
            (
                k.clone(),
                ledger.get(k).map(|e| e.sha256.clone()).unwrap_or_default(),
            )
        })
        .collect();
    for (key, expected) in gone {
        let usb = dest.join(&key);
        if usb.is_file() && common::digest(&usb)? == expected {
            report.missing.push(key);
        } else {
            report.both_damaged.push(key);
        }
    }
    if apply {
        for note in &notes {
            let target = dest.join(ledger_key(note, &source)?);
            let text = common::bytes(note, 4096)?;
            if fs::read(&target).ok().as_deref() != Some(text.as_slice()) {
                if let Some(parent) = target.parent() {
                    fs::create_dir_all(parent)?;
                }
                common::atomic(&target, &text, 0o644)?;
            }
        }
        // Ledgers last: the HDD one with this run's agreements, then its USB copy.
        ledger.save()?;
        ledger.publish_copy(&dest)?;
    }
    report.print("Backup");
    println!(
        "Backup: {copied} new verified files{}",
        if apply { "" } else { " identified (dry run)" }
    );
    Ok((copied, report))
}
/// Keep the damaged copy aside (never delete) and put the verified file back.
fn replace_damaged(source: &Path, target: &Path, quarantine: &Path) -> Result<()> {
    if let Some(parent) = quarantine.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::rename(common::safe(target)?, common::safe(quarantine)?)?;
    verified_copy(source, target, true, 0o644)?;
    Ok(())
}
/// Add-only copy of each Nextcloud user's own files (`<user>/files`) to
/// `dest/nextcloud/<user>/files`. Previews (appdata_*), trash, versions, caches and
/// logs are skipped. A file that changes or disappears mid-copy (a live upload or
/// delete) is skipped this week rather than failing the backup.
/// exFAT (and FAT) refuse some names Linux allows: `"*:<>?\|`, control characters,
/// and a trailing dot or space. Such files cannot be stored on that USB drive.
fn fat_safe(relative: &Path) -> bool {
    relative.components().all(|c| {
        let name = c.as_os_str().to_string_lossy();
        !name.chars().any(|ch| ch < ' ' || "\"*:<>?\\|".contains(ch))
            && !name.ends_with('.')
            && !name.ends_with(' ')
    })
}
/// What one Nextcloud copy did. `complete()` is false if any file was left out.
#[derive(Debug, Default, PartialEq)]
pub struct CloudCopy {
    pub copied: u64,
    pub versions: u64,
    pub skipped: u64,
    pub refused: Vec<String>,
    /// Files skipped because they changed or vanished while being copied (in use).
    pub skipped_files: Vec<String>,
    /// What the checksum checks of the USB copy found.
    pub integrity: Integrity,
}
impl CloudCopy {
    pub fn complete(&self) -> bool {
        self.skipped == 0 && self.refused.is_empty()
    }
    /// What was left out, for the job report: counts and (up to 50) file names.
    pub fn missing(&self) -> Option<Value> {
        if self.complete() {
            return None;
        }
        let files: Vec<&String> = self
            .skipped_files
            .iter()
            .chain(&self.refused)
            .take(50)
            .collect();
        Some(json!({
            "in_use": self.skipped,
            "unsupported_names": self.refused.len(),
            "files": files,
            "reason": "Nextcloud files not copied to the USB drive this week",
        }))
    }
}
pub fn copy_cloud(
    cloud: &Path,
    dest: &Path,
    apply: bool,
    fat_names: bool,
    check: Check,
) -> Result<CloudCopy> {
    let (cloud, dest) = separate(cloud, dest)?;
    let (mut copied, mut versions, mut skipped) = (0, 0, 0);
    let mut refused = vec![];
    let mut skipped_files = vec![];
    // The USB copy keeps its own ledger: the Nextcloud files are not on the HDD.
    let root = dest.join("nextcloud");
    common::directory(&root, 0o700)?;
    let mut ledger = if apply {
        Ledger::open(&root)?
    } else {
        Ledger::read(&root)?
    };
    let mut report = Integrity::default();
    let quarantine = quarantine_folder(&root);
    for user_dir in list(&cloud)? {
        let user = user_dir
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .to_string();
        let files = user_dir.join("files");
        if user.starts_with("appdata_")
            || user.starts_with('.')
            || !fs::symlink_metadata(&user_dir)?.is_dir()
            || !fs::symlink_metadata(&files).is_ok_and(|m| m.is_dir())
        {
            continue;
        }
        walk(&files, &mut |path| {
            let name = path.file_name().unwrap_or_default().to_string_lossy();
            if name.ends_with(".part") || name.ends_with(".neko-part") {
                return Ok(());
            }
            let relative = path
                .strip_prefix(&files)
                .map_err(|_| Error::new("Invalid relative path"))?;
            if fat_names && !fat_safe(relative) {
                refused.push(format!("{user}/{}", relative.display()));
                return Ok(());
            }
            let target = dest
                .join("nextcloud")
                .join(&user)
                .join("files")
                .join(relative);
            let key = ledger_key(&target, &root)?;
            let expected = ledger.get(&key).map(|e| e.sha256.clone());
            match kept_copy(path, &target, apply, 0o600, check, expected.as_deref()) {
                Ok(kept) => {
                    match kept.added {
                        Added::New => copied += 1,
                        Added::Version => versions += 1,
                        Added::Same => {}
                        // The live file still matches the recorded checksum: repair the USB copy.
                        Added::Damaged if apply && kept.source_sha256 == expected => {
                            replace_damaged(path, &target, &quarantine.join(&key))?;
                            report.repaired += 1;
                        }
                        Added::Damaged => report.damaged.push(key),
                    }
                    if apply {
                        record_kept(&mut ledger, &root, &kept, &mut report)?;
                    }
                }
                Err(e) if e.to_string().contains("changed") || !path.exists() => {
                    skipped += 1;
                    skipped_files.push(format!("{user}/{}", relative.display()));
                }
                Err(e) => return Err(e),
            }
            Ok(())
        })?;
    }
    if apply {
        if check == Check::Full {
            audit(&root, &ledger, &mut report)?;
        } else {
            missing_from(&root, &ledger, &mut report)?;
        }
        ledger.save()?;
    }
    report.print("Nextcloud");
    println!("Nextcloud: {copied} new files, {versions} changed files kept as dated versions, {skipped} skipped while in use");
    if !refused.is_empty() {
        println!(
            "Nextcloud: {} files not copied because the USB drive (exFAT) cannot store their names; rename them or use an ext4 USB drive: {}",
            refused.len(),
            refused.iter().take(20).cloned().collect::<Vec<_>>().join(", ")
        );
    }
    Ok(CloudCopy {
        copied,
        versions,
        skipped,
        refused,
        skipped_files,
        integrity: report,
    })
}
/// Weekly Immich database copies kept on the USB drive (newest first; older ones go).
const USB_DUMPS: u64 = 4;
/// A database copy older than this is reported as stale (copies are made daily or weekly).
const DUMP_STALE_DAYS: f64 = 8.0;
/// `immich-<stamp>.sql.json` beside each database copy: the Immich version that made it,
/// when, and its checksum. Written when the copy is made; travels with it.
fn dump_note(dump: &Path) -> PathBuf {
    let name = dump.file_name().unwrap_or_default().to_string_lossy();
    dump.with_file_name(format!("{name}.json"))
}
fn env_version(env: &Path) -> Option<String> {
    fs::read_to_string(env)
        .ok()?
        .lines()
        .find_map(|l| l.strip_prefix("IMMICH_VERSION="))
        .map(str::to_string)
        .filter(|v| regex::Regex::new(r"^v\d+\.\d+\.\d+$").unwrap().is_match(v))
}
/// Record version, time and checksum of a database copy that was just made.
pub(crate) fn describe_dump(dump: &Path, immich_env: &Path) -> Result<()> {
    common::atomic_json(
        &dump_note(dump),
        &json!({"format":"neko-immich-dump-v1","immich_version":env_version(immich_env),"created_at":common::now(),"sha256":common::digest(dump)?}),
    )?;
    fs::set_permissions(
        dump_note(dump),
        <fs::Permissions as std::os::unix::fs::PermissionsExt>::from_mode(0o600),
    )?;
    Ok(())
}
/// Put the newest Immich database copy on the USB drive with its note, so the USB alone
/// can rebuild albums, faces and users as well as the photos. Returns what makes the
/// USB copy incomplete (no copy, a stale one, one without a note), or None. A copy
/// whose checksum does not match its note is never copied. Only the version is
/// recorded, never the database password.
pub fn copy_newest_dump(hdd_dumps: &Path, usb: &Path) -> Result<Option<String>> {
    let newest = fs::read_dir(hdd_dumps)
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.file_name().to_string_lossy().to_string())
        .filter(|n| immich_dump(n))
        .max();
    let Some(name) = newest else {
        return Ok(Some(
            "no Immich database copy was found, so the USB drive cannot rebuild albums, faces and users"
                .into(),
        ));
    };
    let source = hdd_dumps.join(&name);
    let note = common::json(&dump_note(&source), 64000);
    let expected = note["sha256"].as_str().map(str::to_string);
    let mut problems = vec![];
    match &expected {
        Some(sum) if common::digest(&source)? != *sum => {
            return Ok(Some(format!(
                "the newest Immich database copy ({name}) is damaged (checksum mismatch) and was not copied"
            )));
        }
        Some(_) => {}
        None => problems.push(format!(
            "the newest Immich database copy ({name}) has no record of its Immich version (made before v0.24.3)"
        )),
    }
    let made = chrono::NaiveDateTime::parse_from_str(&name[7..22], "%Y%m%dT%H%M%S")
        .map(|t| t.and_utc().timestamp() as f64)
        .unwrap_or(0.0);
    let age_days = (common::now() - made) / 86400.0;
    if age_days < -1.0 {
        problems.push(format!(
            "the newest Immich database copy ({name}) is dated in the future; check the server's clock (timedatectl)"
        ));
    } else if age_days > DUMP_STALE_DAYS {
        problems.push(format!(
            "the newest Immich database copy is {} days old; the database backup has not run",
            age_days as u64
        ));
    }
    let folder = usb.join("immich-database");
    common::directory(&folder, 0o700)?;
    let target = folder.join(&name);
    let damaged = |path: &Path| match &expected {
        Some(sum) => common::digest(path).map(|d| d != *sum).unwrap_or(true),
        None => false,
    };
    if target.exists() && damaged(&target) {
        fs::remove_file(common::safe(&target)?)?; // a damaged copy is replaced
    }
    if !target.exists() {
        verified_copy(&source, &target, true, 0o600)?;
    }
    if expected.is_some() {
        common::atomic(
            &dump_note(&target),
            &common::bytes(&dump_note(&source), 64000)?,
            0o600,
        )?;
    }
    let version = note["immich_version"].as_str().unwrap_or("unknown");
    common::atomic(
        &folder.join("IMMICH-VERSION.txt"),
        format!("Immich version that made the newest database copy ({name}): {version}\nEach copy's own version and checksum are in the .json file next to it.\nTo rebuild Immich from this USB drive, see RECOVERY.md, \"Rebuild Immich from the USB drive\".\n").as_bytes(),
        0o600,
    )?;
    prune_dumps(&folder, USB_DUMPS, Some(&name))?;
    println!("USB: Immich database copy {name} (Immich {version})");
    Ok((!problems.is_empty()).then(|| problems.join("; ")))
}
/// `immich-<UTC stamp>.sql`: a weekly Immich database copy (the stamp sorts by time).
fn immich_dump(name: &str) -> bool {
    regex::Regex::new(r"^immich-[0-9]{8}T[0-9]{6}Z\.sql$")
        .unwrap()
        .is_match(name)
}
/// Full monthly snapshots that releases before v0.21.1 wrote to the USB drive. They are
/// never removed automatically: a file deleted from the server long ago may exist only
/// there. The recovery menu lists them and removes them only when you choose to.
pub fn legacy_usb_snapshots(dest: &Path) -> Vec<String> {
    let pattern = regex::Regex::new(r"^[0-9]+-[0-9]+$").unwrap();
    let mut out: Vec<String> = fs::read_dir(dest.join("backups/monthly"))
        .into_iter()
        .flatten()
        .flatten()
        .filter(|e| pattern.is_match(&e.file_name().to_string_lossy()))
        .filter(|e| fs::symlink_metadata(e.path()).is_ok_and(|m| m.is_dir()))
        .filter(|e| {
            common::bytes(&e.path().join("manifest.json"), 2_000_000)
                .ok()
                .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
                .is_some_and(|m| m["format"] == "neko-maintenance-v1")
        })
        .map(|e| e.file_name().to_string_lossy().to_string())
        .collect();
    out.sort();
    out
}
/// Physical disks a block device lives on (through partitions, encryption, LVM).
fn disks_of(device: &str) -> Option<std::collections::BTreeSet<String>> {
    let (code, out) =
        common::command(&["lsblk", "-s", "-n", "-r", "-o", "NAME,TYPE", device], 10).ok()?;
    (code == 0).then(|| parse_disks(&out))
}
fn parse_disks(lsblk: &str) -> std::collections::BTreeSet<String> {
    lsblk
        .lines()
        .filter_map(|line| {
            let mut fields = line.split_whitespace();
            let name = fields.next()?;
            (fields.next()? == "disk").then(|| name.to_string())
        })
        .collect()
}
fn disks_under(path: &Path) -> Option<std::collections::BTreeSet<String>> {
    let (code, out) = common::command(
        &[
            "findmnt",
            "-n",
            "-o",
            "SOURCE",
            "--target",
            &path.to_string_lossy(),
        ],
        10,
    )
    .ok()?;
    // btrfs shows "/dev/sdb1[/subvolume]".
    let source = out.lines().next()?.split('[').next()?.trim().to_string();
    (code == 0 && source.starts_with("/dev/"))
        .then_some(source)
        .and_then(|s| disks_of(&s))
}
/// The USB drive must be a disk of its own: a partition of the system disk, the SSD or
/// the media HDD fails together with what it protects. Checked every week, also for a
/// drive chosen in saved settings rather than in the picker.
fn own_disk(device: &Path, data: &Path, media: &Path) -> Result<()> {
    let usb = disks_of(&device.to_string_lossy())
        .filter(|d| !d.is_empty())
        .ok_or_else(|| Error::new("Could not identify the USB drive's disk"))?;
    for (what, path) in [
        ("the application SSD", data),
        ("the media HDD", media),
        ("the system disk", Path::new("/")),
    ] {
        if let Some(other) = disks_under(path) {
            if !usb.is_disjoint(&other) {
                return Err(Error::new(&format!(
                    "The USB backup drive is on the same disk as {what}; it must be a separate drive"
                )));
            }
        }
    }
    Ok(())
}
/// exFAT has no owners or permissions: `chmod` is silently ignored there and the mount
/// options alone decide who can read the files. The backup holds private copies
/// (Nextcloud files, database dumps), so an exFAT drive is mounted for root only.
const PRIVATE_EXFAT: &str = "uid=0,gid=0,fmask=0177,dmask=0077";
fn usb_mount_options(fstype: &str) -> String {
    if fstype == "exfat" {
        format!("rw,nosuid,nodev,noexec,{PRIVATE_EXFAT}")
    } else {
        "rw,nosuid,nodev".to_string()
    }
}
/// Are these mount options private (root only)? Filesystems with real permissions are,
/// since the backup sets them itself; exFAT needs uid/gid 0 and masks that hide
/// everything from group and others.
fn private_options(options: &str, fstype: &str) -> bool {
    if fstype != "exfat" {
        return true;
    }
    let get = |key: &str| {
        options
            .split(',')
            .find_map(|o| o.strip_prefix(key)?.strip_prefix('='))
    };
    let hidden = |key: &str| {
        get(key)
            .and_then(|v| u32::from_str_radix(v, 8).ok())
            .is_some_and(|m| m & 0o077 == 0o077)
    };
    get("uid").unwrap_or("0") == "0"
        && get("gid").unwrap_or("0") == "0"
        && hidden("fmask")
        && hidden("dmask")
}
fn require_private_mount(dest: &Path, fstype: &str) -> Result<()> {
    let (code, options) = common::command(
        &[
            "findmnt",
            "-n",
            "-o",
            "OPTIONS",
            "--mountpoint",
            &dest.to_string_lossy(),
        ],
        10,
    )?;
    if code != 0 || !private_options(options.trim(), fstype) {
        return Err(Error::new(
            "The USB drive is mounted so that other accounts on this computer could read it. Unmount it and the backup mounts it privately",
        ));
    }
    Ok(())
}
pub fn validate_external(dest: &Path, uuid: &str) -> Result<()> {
    common::safe(dest)?;
    if uuid.trim().is_empty() || !dest.is_dir() {
        return Err(Error::new("External drive identity required"));
    }
    let (code, s) = common::command(
        &[
            "findmnt",
            "-n",
            "-o",
            "UUID",
            "--mountpoint",
            &dest.to_string_lossy(),
        ],
        10,
    )?;
    if code != 0 || !s.trim().eq_ignore_ascii_case(uuid) {
        return Err(Error::new("Wrong or unmounted external drive"));
    }
    Ok(())
}
fn success(args: &[&str], timeout: u64) -> Result<()> {
    let (code, _) = common::command(args, timeout)?;
    if code != 0 {
        return Err(common::failed(args, code, "Storage operation failed"));
    }
    Ok(())
}
/// How a job finished: completely, or with named files left out (`partial`).
#[derive(Debug, Default)]
pub struct Outcome {
    pub partial: Option<Value>,
}
pub fn execute<F: FnOnce() -> Result<()>>(
    data: Option<&Path>,
    name: &str,
    cfg: &Path,
    operation: F,
) -> Result<()> {
    execute_with(data, name, cfg, || operation().map(|_| Outcome::default()))
}
/// Run a job and record its report. A partial result is recorded as "partial" with
/// what was left out, and does not count as a full success (`last_success` stays).
pub fn execute_with<F: FnOnce() -> Result<Outcome>>(
    data: Option<&Path>,
    name: &str,
    cfg: &Path,
    operation: F,
) -> Result<()> {
    let Some(data) = data else {
        return operation().map(|_| ());
    };
    common::safe(data)?;
    guard_data(cfg, data)?;
    if !common::mounted(data) {
        return Err(Error::new("Application SSD missing"));
    }
    let path = data.join(format!("reports/jobs/{name}.json"));
    let old = common::json(&path, 64000);
    let now = common::now();
    let mut row = json!({"job":name,"status":"running","started_at":now,"finished_at":null,"last_success":common::num(&old["last_success"],0.0,now)});
    common::atomic_json(&path, &row)?;
    let result = operation();
    row["finished_at"] = json!(common::now());
    match &result {
        Ok(Outcome {
            partial: Some(missing),
        }) => {
            row["status"] = json!("partial");
            row["missing"] = missing.clone();
            row["last_partial"] = json!(common::now());
        }
        Ok(_) => {
            row["status"] = json!("pass");
            row["last_success"] = json!(common::now());
        }
        Err(e) => {
            row["status"] = json!("fail");
            // Private report: why and at which step, so "inspect logs" is not the only clue.
            row["reason"] = json!(common::sanitize(&e.to_string()));
            row["stage"] = json!(common::current_stage());
        }
    }
    if !common::mounted(data) {
        return Err(Error::new("Application SSD disappeared before report"));
    }
    guard_data(cfg, data)?;
    common::atomic_json(&path, &row)?;
    result.map(|_| ())
}
fn guard_data(cfgpath: &Path, data: &Path) -> Result<()> {
    let c = common::json(cfgpath, 64000);
    let uuid = c["storage"]["data_ssd"]["uuid"].as_str().unwrap_or("");
    let fstype = c["storage"]["data_ssd"]["fstype"].as_str().unwrap_or("");
    if c["data_mount"].as_str() != data.to_str() || uuid.is_empty() || fstype.is_empty() {
        return Err(Error::new("Application SSD identity required"));
    }
    let (code, output) = common::command(
        &[
            "findmnt",
            "-n",
            "-o",
            "UUID,FSTYPE",
            "--mountpoint",
            &data.to_string_lossy(),
        ],
        10,
    )?;
    if code != 0 || output.split_whitespace().collect::<Vec<_>>() != vec![uuid, fstype] {
        return Err(Error::new("Application SSD identity changed before report"));
    }
    Ok(())
}
/// A snapshot job's settings and both drives, checked: root only, both drives
/// mounted where the settings say, with the identities they name.
pub struct Job {
    pub cfgpath: PathBuf,
    pub cfg: Value,
    pub data: PathBuf,
    pub media: PathBuf,
}
pub fn open_job(a: &crate::Args, limit: usize) -> Result<Job> {
    common::require_root()?;
    let cfgpath = a.path("config")?;
    let cfg = common::json(&cfgpath, limit);
    let data = common::mount_path(cfg["data_mount"].as_str().unwrap_or(""))?;
    let media = common::mount_path(cfg["media_mount"].as_str().unwrap_or(""))?;
    guard(&cfgpath, &data, &media)?;
    Ok(Job {
        cfgpath,
        cfg,
        data,
        media,
    })
}
/// A job interrupted while running is recorded as failed.
pub fn mark_interrupted(report: &Path) -> Result<()> {
    let mut row = common::json(report, 64000);
    if row["status"] == "running" {
        row["status"] = json!("fail");
        row["finished_at"] = json!(common::now());
        common::atomic_json(report, &row)?;
    }
    Ok(())
}
/// A resume journal whose work is done: removed, and the removal made durable.
pub fn finish_journal(path: &Path) -> Result<()> {
    fs::remove_file(path)?;
    fs::File::open(path.parent().ok_or_else(|| Error::new("Missing parent"))?)?.sync_all()?;
    Ok(())
}
/// A snapshot being built: `<root>/.snapshot-<time>-<pid>.partial`, private, with its
/// copied files under `payload`.
pub struct Stage {
    pub name: String,
    pub dir: PathBuf,
    pub payload: PathBuf,
}
pub fn stage_snapshot(root: &Path) -> Result<Stage> {
    let name = format!(
        "snapshot-{}-{}",
        (common::now() * 1000000.0) as u128,
        std::process::id()
    );
    let dir = root.join(format!(".{name}.partial"));
    fs::create_dir(&dir)?;
    fs::set_permissions(&dir, fs::Permissions::from_mode(0o700))?;
    let payload = dir.join("payload");
    common::directory(&payload, 0o700)?;
    Ok(Stage { name, dir, payload })
}
/// Make the copied files durable, check (`check`, normally the drives again), write the
/// manifest, publish the snapshot under its final name (never over another one), and
/// keep the newest `keep`.
pub fn publish_snapshot(
    root: &Path,
    stage: &Stage,
    files: &[Value],
    check: impl FnOnce() -> Result<()>,
    manifest: &Value,
    keep: u64,
) -> Result<()> {
    for row in files.iter().rev().filter(|r| r["kind"] == "directory") {
        fs::File::open(stage.payload.join(row["path"].as_str().unwrap_or("")))?.sync_all()?;
    }
    fs::File::open(&stage.payload)?.sync_all()?;
    check()?;
    common::atomic_json(&stage.dir.join("manifest.json"), manifest)?;
    fs::File::open(&stage.dir)?.sync_all()?;
    let target = root.join(&stage.name);
    common::rename_noreplace(&stage.dir, &target)?;
    fs::File::open(root)?.sync_all()?;
    common::prune_snapshots(root, keep, Some(&stage.name))?;
    Ok(())
}
/// Check the drives, take the lock monthly maintenance and the other jobs hold, then
/// check the drives again. Anything that starts or stops services (including resuming
/// after an interrupted backup) runs only after this, so it never acts on the wrong
/// drives or in the middle of a monthly update or its rollback.
pub fn coordinate(cfgpath: &Path, data: &Path, media: &Path) -> Result<fs::File> {
    guard(cfgpath, data, media)?;
    let held = common::lock_wait(
        &data.join("installer/recovery.lock"),
        std::time::Duration::from_secs(3 * 3600),
    )?;
    guard(cfgpath, data, media)?;
    Ok(held)
}
pub fn guard(cfgpath: &Path, data: &Path, media: &Path) -> Result<()> {
    let c = common::json(cfgpath, 64000);
    if c["data_mount"].as_str() != data.to_str() || c["media_mount"].as_str() != media.to_str() {
        return Err(Error::new("Backup paths must match verified configuration"));
    }
    let mut ids = vec![];
    for (key, path) in [("data_ssd", data), ("media_hdd", media)] {
        let uuid = c["storage"][key]["uuid"].as_str().unwrap_or("");
        let fstype = c["storage"][key]["fstype"].as_str().unwrap_or("");
        if uuid.is_empty() || fstype.is_empty() {
            return Err(Error::new("Storage identities required"));
        }
        let (code, s) = common::command(
            &[
                "findmnt",
                "-n",
                "-o",
                "UUID,FSTYPE",
                "--mountpoint",
                &path.to_string_lossy(),
            ],
            10,
        )?;
        let fields = s.split_whitespace().collect::<Vec<_>>();
        if code != 0 || fields != vec![uuid, fstype] {
            return Err(Error::new("Backup storage identity mismatch"));
        }
        ids.push(uuid);
    }
    if ids[0] == ids[1] {
        return Err(Error::new("Distinct backup filesystems required"));
    }
    Ok(())
}
pub fn database(compose: &Path, env: &Path, dest: &Path, apply: bool) -> Result<Option<PathBuf>> {
    common::file(compose)?;
    common::file(env)?;
    common::safe(dest)?;
    if !apply {
        println!("Would create a timestamped logical database backup");
        return Ok(None);
    }
    common::directory(dest, 0o700)?;
    let stamp = chrono::Utc::now().format("%Y%m%dT%H%M%SZ");
    let target = dest.join(format!("immich-{stamp}.sql"));
    let temp = target.with_extension("sql.part");
    if target.exists() || temp.exists() {
        return Err(Error::new("Backup filename exists"));
    }
    let result = (|| {
        let output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(&temp)?;
        output.set_permissions(fs::Permissions::from_mode(0o600))?;
        let mut cmd = Command::new("docker");
        cmd.args([
            "compose",
            "-p",
            "neko-immich",
            "-f",
            &compose.to_string_lossy(),
            "--env-file",
            &env.to_string_lossy(),
            "exec",
            "-T",
            "database",
            "sh",
            "-ec",
            "exec pg_dump -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" --no-owner --no-acl",
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::from(output.try_clone()?))
        .stderr(Stdio::null());
        unsafe {
            cmd.pre_exec(|| {
                if libc::setsid() < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let mut child = cmd.spawn()?;
        let pid = child.id() as i32;
        let start = Instant::now();
        let status = loop {
            if let Some(status) = child.try_wait()? {
                break status;
            }
            if start.elapsed() > Duration::from_secs(1800) {
                unsafe {
                    libc::kill(-pid, libc::SIGKILL);
                }
                let _ = child.wait();
                return Err(Error::new("Database dump timed out"));
            }
            thread::sleep(Duration::from_millis(20));
        };
        unsafe {
            libc::kill(-pid, libc::SIGKILL);
        }
        output.sync_all()?;
        if !status.success() || output.metadata()?.len() < 100 {
            return Err(Error::new("Database dump failed or too small"));
        }
        // A disk that fills up mid-dump can leave a cut-off file even when every
        // program reports success. pg_dump ends a finished dump with this line.
        if !dump_complete(&temp)? {
            return Err(Error::new(
                "Database dump is incomplete (no end marker); the disk may be full",
            ));
        }
        common::publish(&temp, &target)?;
        Ok(Some(target.clone()))
    })();
    if result.is_err() {
        let _ = fs::remove_file(temp);
    }
    result
}
/// Does a plain-format pg_dump file end with pg_dump's own completion line?
pub(crate) fn dump_complete(path: &Path) -> Result<bool> {
    use std::io::{Seek, SeekFrom};
    let mut file = common::file(path)?;
    let len = file.metadata()?.len();
    file.seek(SeekFrom::Start(len.saturating_sub(4096)))?;
    let mut tail = vec![];
    file.read_to_end(&mut tail)?;
    Ok(String::from_utf8_lossy(&tail).contains("-- PostgreSQL database dump complete"))
}
/// Keep the newest `keep` `immich-<UTC stamp>.sql` dumps in `folder` (0 keeps all).
/// The stamp sorts by time; other files are never touched.
/// `newest` (the copy just made) is never removed, even if a clock jump made its name
/// sort before older copies.
pub fn prune_dumps(folder: &Path, keep: u64, newest: Option<&str>) -> Result<Vec<String>> {
    if keep == 0 || !folder.is_dir() {
        return Ok(vec![]);
    }
    let mut dumps = vec![];
    for entry in fs::read_dir(common::safe(folder)?)? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().to_string();
        if immich_dump(&name)
            && Some(name.as_str()) != newest
            && fs::symlink_metadata(entry.path())?.is_file()
        {
            dumps.push(name);
        }
    }
    dumps.sort();
    let kept_aside = usize::from(newest.is_some_and(|n| folder.join(n).is_file()));
    let excess = (dumps.len() + kept_aside).saturating_sub(keep as usize);
    let removed: Vec<String> = dumps.into_iter().take(excess).collect();
    for name in &removed {
        fs::remove_file(common::safe(&folder.join(name))?)?;
        let note = dump_note(&folder.join(name));
        if note.is_file() {
            fs::remove_file(common::safe(&note)?)?;
        }
    }
    if !removed.is_empty() {
        println!(
            "Database: removed {} old weekly dumps from {}",
            removed.len(),
            folder.display()
        );
    }
    Ok(removed)
}
pub fn preservation(a: &Args) -> Result<()> {
    common::require_root()?;
    let data = common::mount_path(a.get("data")?)?;
    let media = common::mount_path(a.get("media")?)?;
    let cfg = a.path("config")?;
    guard(&cfg, &data, &media)?;
    let _lock = common::lock(Path::new("/run/nekoserver-photo-preservation.lock"))?;
    let _recovery = coordinate(&cfg, &data, &media)?;
    execute(
        Some(&data),
        if a.flag("db-backup") {
            "database"
        } else {
            "archive"
        },
        &cfg,
        || {
            guard(&cfg, &data, &media)?;
            let archive_dir = common::relative(&a.value("archive-dir", "photos/archive"))?;
            let full = full_check_due(&data, "archive");
            common::stage("copying new photos and videos to the HDD");
            let (_, checks) = archive_checked(
                &data.join("photos/immich-library"),
                &media.join(archive_dir),
                true,
                a.number("min-age", 600)?,
                if full { Check::Full } else { Check::Quick },
            )?;
            save_integrity(&data, "archive", &checks)?;
            if let Some(problem) = checks.problem("HDD archive") {
                return Err(Error::new(&problem));
            }
            if full {
                println!("Archive: compared every existing copy by checksum (every 4 weeks)");
                record_full_check(&data, "archive")?;
            }
            guard(&cfg, &data, &media)?;
            if a.flag("db-backup") {
                guard(&cfg, &data, &media)?;
                common::stage("making the Immich database copy");
                let created = database(
                    &data.join("compose/immich/compose.yaml"),
                    &data.join("compose/immich/.env"),
                    &data.join("backups/immich-database"),
                    true,
                )?
                .ok_or_else(|| Error::new("Database dump missing"))?;
                guard(&cfg, &data, &media)?;
                let target = media.join("backups/immich-database").join(
                    created
                        .file_name()
                        .ok_or_else(|| Error::new("Invalid database filename"))?,
                );
                describe_dump(&created, &data.join("compose/immich/.env"))?;
                common::stage("copying the Immich database copy to the HDD");
                verified_copy(&created, &target, true, 0o600)?;
                verified_copy(&dump_note(&created), &dump_note(&target), true, 0o600)?;
                // Weekly dumps used to pile up forever on both drives.
                let keep = a.number("db-keep", 8)?;
                for folder in [
                    data.join("backups/immich-database"),
                    media.join("backups/immich-database"),
                ] {
                    prune_dumps(&folder, keep, created.file_name().and_then(|n| n.to_str()))?;
                }
            }
            Ok(())
        },
    )
}
/// One "incomplete" report for the weekly USB copy: Nextcloud files left out and/or a
/// database copy problem.
fn incomplete(
    files: Option<Value>,
    database: Option<String>,
    source: Option<String>,
) -> Option<Value> {
    if files.is_none() && database.is_none() && source.is_none() {
        return None;
    }
    let mut out = files.unwrap_or_else(|| json!({"in_use":0,"unsupported_names":0,"files":[]}));
    if let Some(problem) = database {
        out["database"] = json!(problem);
        out["reason"] = json!("The USB backup is incomplete");
    }
    if let Some(problem) = source {
        out["source"] = json!(problem);
        out["reason"] = json!("The USB backup is incomplete");
    }
    Some(out)
}
/// The Nextcloud files folder is made when Nextcloud is installed, so a configured one
/// that is missing means a problem (a drive or folder gone), never "nothing to copy".
const CLOUD_MISSING: &str = "the Nextcloud files folder is missing, so Nextcloud was not copied";
pub fn weekly(a: &Args) -> Result<()> {
    common::require_root()?;
    let uuid = a.get("uuid")?;
    let fstype = a.get("fstype")?;
    if !(8..=80).contains(&uuid.len())
        || !uuid.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-')
        || !["ext4", "xfs", "btrfs", "exfat"].contains(&fstype)
    {
        return Err(Error::new("Invalid external identity"));
    }
    let source = common::safe(&a.path("source")?)?;
    let dest = common::mount_path(a.get("dest")?)?;
    let media = common::mount_path(a.get("media-root")?)?;
    let data = common::mount_path(a.get("data")?)?;
    let cfg = a.path("config")?;
    let cloud = match a.value("cloud", "").as_str() {
        "" => None,
        value => {
            let path = common::safe(Path::new(value))?;
            if !path.starts_with(&media) || path == media {
                return Err(Error::new("Nextcloud folder outside media mount"));
            }
            Some(path)
        }
    };
    guard(&cfg, &data, &media)?;
    let identity = common::json(&cfg, 64000);
    if ["data_ssd", "media_hdd"]
        .iter()
        .any(|k| identity["storage"][k]["uuid"].as_str() == Some(uuid))
    {
        return Err(Error::new("External UUID must differ from primary drives"));
    }
    if !source.starts_with(&media) || source == media {
        return Err(Error::new("Archive outside media mount"));
    }
    separate(&source, &dest)?;
    let _lock = common::lock(Path::new("/run/nekoserver-photo-backup.lock"))?;
    let full = full_check_due(&data, "external");
    let check = if full { Check::Full } else { Check::Quick };
    execute_with(Some(&data), "external", &cfg, || {
        common::stage("checking the USB drive");
        guard(&cfg, &data, &media)?;
        let device = Path::new("/dev/disk/by-uuid").join(uuid);
        if !device.exists() {
            return Err(Error::new("External drive missing"));
        }
        own_disk(&device, &data, &media)?;
        let (code, s) = common::command(
            &[
                "blkid",
                "-s",
                "TYPE",
                "-o",
                "value",
                &device.to_string_lossy(),
            ],
            10,
        )?;
        if code != 0 || s.trim() != fstype {
            return Err(Error::new("External filesystem mismatch"));
        }
        fs::create_dir_all(&dest)?;
        let already = common::mounted(&dest);
        if !already && fs::read_dir(&dest)?.next().is_some() {
            return Err(Error::new("Nonempty mount destination refused"));
        }
        let mut mounted_here = false;
        let result: Result<Option<Value>> = (|| {
            if !already {
                success(
                    &[
                        "mount",
                        "-o",
                        &usb_mount_options(fstype),
                        &format!("UUID={uuid}"),
                        &dest.to_string_lossy(),
                    ],
                    30,
                )?;
                mounted_here = true;
            }
            validate_external(&dest, uuid)?;
            // Also when someone else mounted it: no private copy onto a readable drive.
            require_private_mount(&dest, fstype)?;
            guard(&cfg, &data, &media)?;
            common::stage("copying photos and videos to the USB drive");
            let (_, photos) = mirror(&source, &dest, true, check)?;
            validate_external(&dest, uuid)?;
            guard(&cfg, &data, &media)?;
            let mut database_problem = None;
            if data.join("compose/immich/compose.yaml").is_file() {
                common::stage("copying the Immich database to the USB drive");
                database_problem = copy_newest_dump(&media.join("backups/immich-database"), &dest)?;
            }
            validate_external(&dest, uuid)?;
            guard(&cfg, &data, &media)?;
            let mut cloud_copy = None;
            let mut cloud_problem = None;
            if let Some(cloud) = &cloud {
                if cloud.is_dir() {
                    common::stage("copying Nextcloud files to the USB drive");
                    cloud_copy = Some(copy_cloud(cloud, &dest, true, fstype == "exfat", check)?);
                    validate_external(&dest, uuid)?;
                    guard(&cfg, &data, &media)?;
                } else {
                    println!("Nextcloud: {CLOUD_MISSING}; this backup is incomplete");
                    cloud_problem = Some(CLOUD_MISSING.to_string());
                }
            }
            let legacy = legacy_usb_snapshots(&dest);
            if !legacy.is_empty() {
                println!(
                    "USB: {} full monthly snapshots from before v0.21.1 are kept ({}). They may hold files that are nowhere else; review or remove them with: sudo bash install.sh --recover",
                    legacy.len(),
                    legacy.join(", ")
                );
            }
            save_integrity(&data, "usb_photos", &photos)?;
            if let Some(cloud) = &cloud_copy {
                save_integrity(&data, "usb_nextcloud", &cloud.integrity)?;
            }
            // Damage found by the checksum checks fails the job, after everything else
            // was copied: nothing is lost by finishing the run first.
            let damage: Vec<String> = [
                photos.problem("HDD archive"),
                cloud_copy
                    .as_ref()
                    .and_then(|c| c.integrity.problem("USB Nextcloud")),
            ]
            .into_iter()
            .flatten()
            .collect();
            if !damage.is_empty() {
                return Err(Error::new(&damage.join(". ")));
            }
            Ok(incomplete(
                cloud_copy.and_then(|c| c.missing()),
                database_problem,
                cloud_problem,
            ))
        })();
        if mounted_here {
            success(&["umount", &dest.to_string_lossy()], 45)?;
        }
        let partial = result?;
        if full {
            println!("Backup: compared every existing copy by checksum (every 4 weeks)");
            record_full_check(&data, "external")?;
        }
        Ok(Outcome { partial })
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn a_missing_nextcloud_folder_makes_the_backup_incomplete() {
        assert!(incomplete(None, None, None).is_none());
        let partial = incomplete(None, None, Some(CLOUD_MISSING.into())).unwrap();
        assert_eq!(partial["source"], CLOUD_MISSING);
        assert_eq!(partial["reason"], "The USB backup is incomplete");
    }
    #[test]
    fn exfat_usb_drives_are_mounted_for_root_only() {
        assert_eq!(usb_mount_options("ext4"), "rw,nosuid,nodev");
        let exfat = usb_mount_options("exfat");
        assert!(
            exfat.contains("fmask=0177") && exfat.contains("dmask=0077") && exfat.contains("uid=0")
        );
        assert!(private_options(&exfat, "exfat"));
        // What the kernel shows for an exFAT drive mounted with default (readable) masks.
        assert!(!private_options(
            "rw,nosuid,nodev,relatime,uid=0,gid=0,fmask=0022,dmask=0022,iocharset=utf8",
            "exfat"
        ));
        assert!(!private_options(
            "rw,relatime,uid=1000,gid=1000,fmask=0177,dmask=0077",
            "exfat"
        ));
        assert!(!private_options("rw,relatime", "exfat"));
        assert!(private_options("rw,relatime", "ext4"));
    }
    #[test]
    fn a_full_check_is_due_after_four_weeks_or_with_a_future_record() {
        let now = 1_800_000_000.0;
        assert!(!due_since(Some(now - 86400.0), now));
        assert!(due_since(Some(now - FULL_CHECK_EVERY), now));
        assert!(due_since(None, now));
        assert!(due_since(Some(0.0), now));
        assert!(due_since(Some(f64::NAN), now));
        // Recorded while the clock said 2030; the clock is now right (2026).
        assert!(due_since(Some(1_900_000_000.0), now));
        assert!(due_since(Some(1_900_000_000.0), now + FULL_CHECK_EVERY));
        assert!(
            !due_since(Some(now + 60.0), now),
            "a small clock difference is not a future record"
        );
    }
    fn scratch(name: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!("neko-backup-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        root
    }
    fn write(path: &Path, body: &[u8]) {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, body).unwrap();
    }
    /// Save `src` into the backup tree `root` under `key` the way the archive job does:
    /// verified copy, checksum recorded in the tree's ledger.
    fn keep(src: &Path, root: &Path, key: &str) {
        let mut ledger = Ledger::open(root).unwrap();
        let kept = kept_copy(src, &root.join(key), true, 0o600, Check::Quick, None).unwrap();
        record_kept(&mut ledger, root, &kept, &mut Integrity::default()).unwrap();
        ledger.save().unwrap();
    }
    fn names(dir: &Path) -> Vec<String> {
        let mut v: Vec<String> = fs::read_dir(dir)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().to_string())
            .collect();
        v.sort();
        v
    }
    #[test]
    fn existing_copies_are_skipped_without_reading_them() {
        let root = scratch("skip");
        let (src, dst) = (root.join("src/a.jpg"), root.join("dst/a.jpg"));
        write(&src, b"photo");
        assert_eq!(
            add_only_copy(&src, &dst, true, 0o644, Check::Quick).unwrap(),
            Added::New
        );
        // Unreadable copy: a re-read would fail, so Same proves it was not read.
        fs::set_permissions(&dst, fs::Permissions::from_mode(0o000)).unwrap();
        if fs::File::open(&dst).is_err() {
            assert_eq!(
                add_only_copy(&src, &dst, true, 0o644, Check::Quick).unwrap(),
                Added::Same
            );
        }
        fs::set_permissions(&dst, fs::Permissions::from_mode(0o644)).unwrap();
        // Same size but changed after the copy: checked by content, kept as a version.
        thread::sleep(Duration::from_millis(20));
        write(&src, b"PHOTO");
        assert_eq!(
            add_only_copy(&src, &dst, true, 0o644, Check::Quick).unwrap(),
            Added::Version
        );
        assert_eq!(fs::read(&dst).unwrap(), b"photo");
        assert_eq!(
            add_only_copy(&src, &dst, true, 0o644, Check::Quick).unwrap(),
            Added::Same
        );
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn replaced_file_with_preserved_timestamp_is_still_backed_up() {
        // The reported case: AAAA backed up, then replaced by BBBB with the old timestamp.
        let root = scratch("same-time");
        let (src, dst) = (root.join("src/a.jpg"), root.join("dst/a.jpg"));
        write(&src, b"AAAA");
        let old_time = fs::metadata(&src).unwrap().modified().unwrap();
        assert_eq!(
            add_only_copy(&src, &dst, true, 0o644, Check::Quick).unwrap(),
            Added::New
        );
        thread::sleep(Duration::from_millis(20));
        write(&src, b"BBBB");
        fs::File::options()
            .write(true)
            .open(&src)
            .unwrap()
            .set_modified(old_time)
            .unwrap();
        assert_eq!(fs::metadata(&src).unwrap().modified().unwrap(), old_time);
        assert_eq!(
            add_only_copy(&src, &dst, true, 0o644, Check::Quick).unwrap(),
            Added::Version
        );
        let kept: Vec<Vec<u8>> = fs::read_dir(root.join("dst"))
            .unwrap()
            .map(|e| fs::read(e.unwrap().path()).unwrap())
            .collect();
        assert!(kept.contains(&b"AAAA".to_vec()) && kept.contains(&b"BBBB".to_vec()));
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn full_check_compares_copies_the_quick_check_trusts() {
        let root = scratch("full");
        let (src, dst) = (root.join("src/a.jpg"), root.join("dst/a.jpg"));
        write(&src, b"good");
        add_only_copy(&src, &dst, true, 0o644, Check::Quick).unwrap();
        // The copy itself went bad later (same size, written after the source).
        write(&dst, b"bad!");
        assert_eq!(
            add_only_copy(&src, &dst, true, 0o644, Check::Quick).unwrap(),
            Added::Same
        );
        assert_eq!(
            add_only_copy(&src, &dst, true, 0o644, Check::Full).unwrap(),
            Added::Version
        );
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn old_usb_snapshots_are_listed_never_removed() {
        let root = scratch("legacy");
        write(
            &root.join("backups/monthly/100-1/manifest.json"),
            br#"{"format":"neko-maintenance-v1"}"#,
        );
        write(&root.join("backups/monthly/200-1/manifest.json"), b"{}");
        write(
            &root.join("backups/monthly/notes/manifest.json"),
            br#"{"format":"neko-maintenance-v1"}"#,
        );
        assert_eq!(legacy_usb_snapshots(&root), ["100-1"]);
        assert!(root.join("backups/monthly/100-1/manifest.json").is_file());
        let _ = fs::remove_dir_all(root);
    }
    fn dump_name(days_ago: f64) -> String {
        let t = chrono::DateTime::from_timestamp((common::now() - days_ago * 86400.0) as i64, 0)
            .unwrap();
        format!("immich-{}.sql", t.format("%Y%m%dT%H%M%SZ"))
    }
    #[test]
    fn usb_gets_the_newest_database_copy_with_the_version_that_made_it() {
        let root = scratch("usb-db");
        let (hdd, usb, env) = (root.join("hdd"), root.join("usb"), root.join(".env"));
        let problem = copy_newest_dump(&hdd, &usb).unwrap().unwrap();
        assert!(problem.contains("no Immich database copy"), "{problem}");
        write(&env, b"IMMICH_VERSION=v2.1.0\nDB_PASSWORD=secret123\n");
        let mut made = vec![];
        for days_ago in (0..6).rev() {
            let name = dump_name(days_ago as f64 + 0.5);
            write(&hdd.join(&name), format!("dump {days_ago}").as_bytes());
            describe_dump(&hdd.join(&name), &env).unwrap();
            assert_eq!(copy_newest_dump(&hdd, &usb).unwrap(), None);
            made.push(name);
        }
        let folder = usb.join("immich-database");
        let mut expected: Vec<String> = made[2..]
            .iter()
            .flat_map(|n| [n.clone(), format!("{n}.json")])
            .collect();
        expected.insert(0, "IMMICH-VERSION.txt".into());
        assert_eq!(names(&folder), expected);
        // Immich was upgraded but the newest copy was made by v2.1.0: the note says so.
        write(&env, b"IMMICH_VERSION=v2.2.0\nDB_PASSWORD=secret123\n");
        copy_newest_dump(&hdd, &usb).unwrap();
        let note = fs::read_to_string(folder.join("IMMICH-VERSION.txt")).unwrap();
        assert!(note.contains("v2.1.0") && !note.contains("v2.2.0") && !note.contains("secret123"));
        // A damaged USB copy is replaced from the checked HDD copy.
        let newest = made.last().unwrap();
        fs::write(folder.join(newest), b"bit rot").unwrap();
        assert_eq!(copy_newest_dump(&hdd, &usb).unwrap(), None);
        assert_eq!(fs::read(folder.join(newest)).unwrap(), b"dump 0");
        // A damaged HDD copy is never copied, and the run is incomplete.
        fs::write(hdd.join(newest), b"bit rot").unwrap();
        let problem = copy_newest_dump(&hdd, &usb).unwrap().unwrap();
        assert!(problem.contains("damaged"), "{problem}");
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn a_stale_or_unrecorded_database_copy_makes_the_usb_backup_incomplete() {
        let root = scratch("usb-db-stale");
        let (hdd, usb, env) = (root.join("hdd"), root.join("usb"), root.join(".env"));
        write(&env, b"IMMICH_VERSION=v2.1.0\n");
        let old = dump_name(10.0);
        write(&hdd.join(&old), b"old dump");
        describe_dump(&hdd.join(&old), &env).unwrap();
        let problem = copy_newest_dump(&hdd, &usb).unwrap().unwrap();
        assert!(problem.contains("10 days old"), "{problem}");
        let legacy = dump_name(1.0);
        write(&hdd.join(&legacy), b"made by an older release");
        let problem = copy_newest_dump(&hdd, &usb).unwrap().unwrap();
        assert!(
            problem.contains("no record of its Immich version"),
            "{problem}"
        );
        let report = incomplete(None, Some(problem), None).unwrap();
        assert!(report["database"].as_str().unwrap().contains("no record"));
        assert_eq!(incomplete(None, None, None), None);
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn disks_are_found_through_partitions_and_encryption() {
        // lsblk -s lists a device and what it sits on, down to the physical disk.
        let usb = parse_disks("sdc1 part\nsdc disk\n");
        let media = parse_disks("luks-1 crypt\nsdb2 part\nsdb disk\n");
        let same = parse_disks("sdb3 part\nsdb disk\n");
        assert_eq!(usb.iter().collect::<Vec<_>>(), ["sdc"]);
        assert!(usb.is_disjoint(&media));
        assert!(!same.is_disjoint(&media));
    }
    #[test]
    fn nothing_is_locked_or_resumed_on_the_wrong_drives() {
        // app-resume, host-resume and the backups call coordinate() before they restart
        // anything; drives that do not match the settings stop them right there.
        let root = scratch("coordinate");
        let (data, media) = (root.join("ssd"), root.join("hdd"));
        fs::create_dir_all(&data).unwrap();
        fs::create_dir_all(&media).unwrap();
        let cfg = root.join("guard.json");
        write(
            &cfg,
            serde_json::to_string(&json!({"data_mount": data, "media_mount": media,
                "storage": {"data_ssd": {"uuid": "aaaa-1111", "fstype": "ext4"},
                            "media_hdd": {"uuid": "bbbb-2222", "fstype": "ext4"}}}))
            .unwrap()
            .as_bytes(),
        );
        let error = coordinate(&cfg, &data, &media).unwrap_err().to_string();
        assert!(error.contains("mismatch"), "{error}");
        assert!(!data.join("installer/recovery.lock").exists());
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn services_are_resumed_only_after_the_drives_and_the_lock_are_checked() {
        for (file, source) in [
            ("app_snapshot.rs", include_str!("app_snapshot.rs")),
            ("host_snapshot.rs", include_str!("host_snapshot.rs")),
        ] {
            for entry in ["pub fn resume_command(", "pub fn run("] {
                let body = &source[source.find(entry).unwrap()..];
                let body = &body[..body.find("\n}\n").unwrap()];
                let checked = body.find("backup::coordinate(").expect(file);
                let resumed = body.find("resume()?").expect(file);
                assert!(checked < resumed, "{file} {entry}");
            }
        }
    }
    #[test]
    fn a_clock_jump_never_deletes_the_copy_just_made_or_hides_a_stale_one() {
        let root = scratch("clock-jump");
        // After the clock jumped back, today's copy sorts before last week's.
        for name in [
            "immich-20261001T000000Z.sql",
            "immich-20261008T000000Z.sql",
            "immich-20261015T000000Z.sql",
            "immich-20250101T000000Z.sql",
        ] {
            write(&root.join(name), b"dump");
        }
        let removed = prune_dumps(&root, 2, Some("immich-20250101T000000Z.sql")).unwrap();
        assert_eq!(
            removed,
            ["immich-20261001T000000Z.sql", "immich-20261008T000000Z.sql"]
        );
        assert!(root.join("immich-20250101T000000Z.sql").is_file());
        // A copy dated in the future is not taken as fresh.
        let (hdd, usb, env) = (root.join("hdd"), root.join("usb"), root.join(".env"));
        write(&env, b"IMMICH_VERSION=v2.1.0\n");
        let future = dump_name(-3.0);
        write(&hdd.join(&future), b"dump");
        describe_dump(&hdd.join(&future), &env).unwrap();
        let problem = copy_newest_dump(&hdd, &usb).unwrap().unwrap();
        assert!(problem.contains("dated in the future"), "{problem}");
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn a_cut_off_database_dump_is_recognised() {
        let root = scratch("dump-end");
        write(
            &root.join("whole.sql"),
            b"CREATE TABLE a();\n--\n-- PostgreSQL database dump complete\n--\n",
        );
        write(
            &root.join("cut.sql"),
            b"CREATE TABLE a();\nINSERT INTO a VALUES",
        );
        assert!(dump_complete(&root.join("whole.sql")).unwrap());
        assert!(!dump_complete(&root.join("cut.sql")).unwrap());
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn a_copy_is_published_only_when_stream_disk_and_source_checksums_agree() {
        let root = scratch("three-hash");
        let (src, dst) = (root.join("a.mov"), root.join("out/a.mov"));
        write(&src, b"original video");
        let landed = copy_verified(&src, &dst, true, 0o600).unwrap().unwrap();
        assert_eq!(landed, common::digest(&src).unwrap());
        assert_eq!(fs::read(&dst).unwrap(), b"original video");
        // The source is rewritten while it is being copied: nothing is published.
        let (src2, dst2) = (root.join("b.mov"), root.join("out/b.mov"));
        write(&src2, b"first take");
        let error = copy_verified_with(&src2, &dst2, true, 0o600, &|| {
            fs::write(&src2, b"second take, longer").unwrap()
        })
        .unwrap_err()
        .to_string();
        assert!(error.contains("changed during the copy"), "{error}");
        assert!(!dst2.exists());
        assert!(!root.join("out/b.mov.neko-part").exists());
        // Same size and mtime put back: ctime still shows the edit.
        let (src3, dst3) = (root.join("c.mov"), root.join("out/c.mov"));
        write(&src3, b"AAAA");
        let error = copy_verified_with(&src3, &dst3, true, 0o600, &|| {
            let m = fs::metadata(&src3).unwrap();
            fs::write(&src3, b"BBBB").unwrap();
            let times = [
                libc::timespec {
                    tv_sec: m.atime(),
                    tv_nsec: m.atime_nsec(),
                },
                libc::timespec {
                    tv_sec: m.mtime(),
                    tv_nsec: m.mtime_nsec(),
                },
            ];
            let path = std::ffi::CString::new(src3.as_os_str().as_encoded_bytes()).unwrap();
            assert_eq!(
                unsafe { libc::utimensat(libc::AT_FDCWD, path.as_ptr(), times.as_ptr(), 0) },
                0
            );
        })
        .unwrap_err()
        .to_string();
        assert!(error.contains("changed during the copy"), "{error}");
        assert!(!dst3.exists());
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn hdd_usb_and_the_ledger_decide_which_copy_is_good() {
        let root = scratch("three-way");
        let (src, hdd, usb) = (root.join("src"), root.join("hdd"), root.join("usb"));
        for name in ["a", "b", "c", "d"] {
            write(&src.join(name), format!("good {name}").as_bytes());
            keep(&src.join(name), &hdd, &format!("upload/{name}.jpg"));
        }
        let (copied, report) = mirror(&hdd, &usb, true, Check::Quick).unwrap();
        assert_eq!((copied, report.problem("HDD archive")), (4, None));
        let recorded = |key: &str| Ledger::read(&hdd).unwrap().get(key).unwrap().sha256.clone();
        let before = recorded("upload/b.jpg");
        // Same-size damage, so only reading the files can find it.
        fs::write(usb.join("upload/a.jpg"), b"ROT? a").unwrap(); // USB wrong, HDD right
        fs::write(hdd.join("upload/b.jpg"), b"ROT? b").unwrap(); // HDD wrong, USB right
        fs::write(hdd.join("upload/c.jpg"), b"ROT1 c").unwrap(); // both wrong
        fs::write(usb.join("upload/c.jpg"), b"ROT2 c").unwrap();
        let (_, report) = mirror(&hdd, &usb, true, Check::Full).unwrap();
        // HDD matches, USB fails: the USB copy is repaired from the HDD.
        assert_eq!(report.repaired, 1);
        assert_eq!(fs::read(usb.join("upload/a.jpg")).unwrap(), b"good a");
        let kept: Vec<_> = fs::read_dir(usb.join(QUARANTINE))
            .unwrap()
            .flatten()
            .collect();
        assert_eq!(
            fs::read(kept[0].path().join("upload/a.jpg")).unwrap(),
            b"ROT? a"
        );
        // USB matches, HDD fails: the USB copy is kept, the HDD file is reported.
        assert_eq!(report.damaged, ["upload/b.jpg"]);
        assert_eq!(fs::read(usb.join("upload/b.jpg")).unwrap(), b"good b");
        // Neither matches: both kept untouched, critical.
        assert_eq!(report.both_damaged, ["upload/c.jpg"]);
        assert_eq!(fs::read(hdd.join("upload/c.jpg")).unwrap(), b"ROT1 c");
        assert_eq!(fs::read(usb.join("upload/c.jpg")).unwrap(), b"ROT2 c");
        assert!(report.problem("HDD archive").unwrap().contains("CRITICAL"));
        assert!(copy_tree(&hdd, &usb, true, Check::Full).is_err());
        // The recorded checksum never follows the damage.
        assert_eq!(recorded("upload/b.jpg"), before);
        // The USB drive carries the same ledger, published after the files.
        assert_eq!(
            Ledger::read(&usb)
                .unwrap()
                .get("upload/d.jpg")
                .unwrap()
                .sha256,
            recorded("upload/d.jpg")
        );
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn files_without_a_checksum_stay_unverified_until_two_copies_agree() {
        let root = scratch("unverified");
        let (hdd, usb) = (root.join("hdd"), root.join("usb"));
        // Archive files from before the ledger existed.
        write(&hdd.join("upload/old.jpg"), b"old photo");
        write(&hdd.join("upload/odd.jpg"), b"hdd version");
        write(&usb.join("upload/old.jpg"), b"old photo");
        write(&usb.join("upload/odd.jpg"), b"usb version");
        let (_, report) = mirror(&hdd, &usb, true, Check::Quick).unwrap();
        assert!(report.unverified >= 1);
        let (_, report) = mirror(&hdd, &usb, true, Check::Full).unwrap();
        assert_eq!(report.newly_verified, 1);
        let ledger = Ledger::read(&hdd).unwrap();
        assert_eq!(ledger.get("upload/old.jpg").unwrap().how, "agreed");
        // Copies that differ with nothing recorded: neither is trusted or changed.
        assert!(ledger.get("upload/odd.jpg").is_none());
        assert_eq!(report.disagree, ["upload/odd.jpg"]);
        assert_eq!(
            fs::read(usb.join("upload/odd.jpg")).unwrap(),
            b"usb version"
        );
        assert_eq!(
            fs::read(hdd.join("upload/odd.jpg")).unwrap(),
            b"hdd version"
        );
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn a_damaged_hdd_file_is_never_copied_to_the_usb_drive() {
        let root = scratch("no-spread");
        let (src, hdd, usb) = (root.join("a.jpg"), root.join("hdd"), root.join("usb"));
        write(&src, b"good");
        keep(&src, &hdd, "upload/a.jpg");
        fs::write(hdd.join("upload/a.jpg"), b"bad!").unwrap();
        let (copied, report) = mirror(&hdd, &usb, true, Check::Quick).unwrap();
        assert_eq!(
            (copied, report.damaged.clone()),
            (0, vec!["upload/a.jpg".to_string()])
        );
        assert!(!usb.join("upload/a.jpg").exists());
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn every_original_is_archived_whatever_its_format() {
        let root = scratch("archive-formats");
        let (library, hdd) = (root.join("library"), root.join("hdd"));
        let old = |path: &Path| {
            let past = libc::timespec {
                tv_sec: 1_000_000_000,
                tv_nsec: 0,
            };
            let c = std::ffi::CString::new(path.as_os_str().as_encoded_bytes()).unwrap();
            unsafe { libc::utimensat(libc::AT_FDCWD, c.as_ptr(), [past, past].as_ptr(), 0) };
        };
        let names = [
            "normal.jpg",
            "camera.jfif",
            "camera.jpe",
            "drawing.svg",
            "scan.jp2",
            "art.psd",
            "clip.mxf",
            "raw.cap",
            "raw.dcr",
            "photo.JPG",
            "no-extension",
        ];
        for name in names {
            let path = library.join("upload/u").join(name);
            write(&path, name.as_bytes());
            old(&path);
        }
        // Immich's folder markers too: a restore needs them, and Immich checks for them.
        for marker in ["upload/.immich", "library/.immich"] {
            write(&library.join(marker), b"");
            old(&library.join(marker));
        }
        let copied = archive(&library, &hdd, true, 60, Check::Quick).unwrap();
        assert_eq!(copied, names.len() as u64 + 2);
        for name in names {
            assert_eq!(
                fs::read(hdd.join("upload/u").join(name)).unwrap(),
                name.as_bytes(),
                "{name}"
            );
        }
        assert!(hdd.join("upload/.immich").is_file() && hdd.join("library/.immich").is_file());
        // A symlink in Immich's folders still stops the run instead of being followed.
        std::os::unix::fs::symlink("/etc/passwd", library.join("upload/u/link.jpg")).unwrap();
        assert!(archive(&library, &hdd, true, 60, Check::Quick).is_err());
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn the_archive_repairs_itself_from_the_immich_original_and_audits_every_file() {
        let root = scratch("archive-ledger");
        let (library, hdd) = (root.join("library"), root.join("hdd"));
        let old = |path: &Path| {
            let past = libc::timespec {
                tv_sec: 1_000_000_000,
                tv_nsec: 0,
            };
            let c = std::ffi::CString::new(path.as_os_str().as_encoded_bytes()).unwrap();
            unsafe { libc::utimensat(libc::AT_FDCWD, c.as_ptr(), [past, past].as_ptr(), 0) };
        };
        write(&library.join("upload/u/a.jpg"), b"photo a");
        write(&library.join("upload/u/b.jpg"), b"photo b");
        old(&library.join("upload/u/a.jpg"));
        old(&library.join("upload/u/b.jpg"));
        archive(&library, &hdd, true, 60, Check::Quick).unwrap();
        let ledger = Ledger::read(&hdd).unwrap();
        assert_eq!(ledger.get("upload/u/a.jpg").unwrap().how, "copied");
        // Bit rot on the HDD; the Immich original still matches: repaired from it.
        fs::write(hdd.join("upload/u/a.jpg"), b"photo ?").unwrap();
        archive(&library, &hdd, true, 60, Check::Full).unwrap();
        assert_eq!(fs::read(hdd.join("upload/u/a.jpg")).unwrap(), b"photo a");
        // A file Immich no longer has: only the audit can see it, and it is reported.
        fs::remove_file(library.join("upload/u/b.jpg")).unwrap();
        fs::write(hdd.join("upload/u/b.jpg"), b"photo ?").unwrap();
        let error = archive(&library, &hdd, true, 60, Check::Full)
            .unwrap_err()
            .to_string();
        assert!(error.contains("upload/u/b.jpg"), "{error}");
        // The damaged file was never taken for an edit: no dated version was made.
        assert!(!names(&hdd.join("upload/u"))
            .iter()
            .any(|n| n.contains(".neko-2")));
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn a_verified_file_that_disappears_is_reported_not_ignored() {
        let root = scratch("missing");
        let (library, hdd, usb) = (root.join("library"), root.join("hdd"), root.join("usb"));
        write(&library.join("upload/u/a.jpg"), b"photo a");
        write(&library.join("upload/u/b.jpg"), b"photo b");
        for f in ["upload/u/a.jpg", "upload/u/b.jpg"] {
            let past = libc::timespec {
                tv_sec: 1_000_000_000,
                tv_nsec: 0,
            };
            let c = std::ffi::CString::new(library.join(f).as_os_str().as_encoded_bytes()).unwrap();
            unsafe { libc::utimensat(libc::AT_FDCWD, c.as_ptr(), [past, past].as_ptr(), 0) };
        }
        archive(&library, &hdd, true, 60, Check::Quick).unwrap();
        copy_tree(&hdd, &usb, true, Check::Quick).unwrap();
        // The Immich original and the archive copy are both gone.
        fs::remove_file(library.join("upload/u/a.jpg")).unwrap();
        fs::remove_file(hdd.join("upload/u/a.jpg")).unwrap();
        let error = archive(&library, &hdd, true, 60, Check::Full)
            .unwrap_err()
            .to_string();
        assert!(
            error.contains("missing") && error.contains("upload/u/a.jpg"),
            "{error}"
        );
        // Even the nightly quick run notices.
        assert!(archive(&library, &hdd, true, 60, Check::Quick).is_err());
        // The USB copy still has it: reported as missing on the HDD, USB copy kept.
        let (_, report) = mirror(&hdd, &usb, true, Check::Quick).unwrap();
        assert_eq!(report.missing, ["upload/u/a.jpg"]);
        assert_eq!(fs::read(usb.join("upload/u/a.jpg")).unwrap(), b"photo a");
        // Gone from the USB drive too: no good copy left anywhere.
        fs::remove_file(usb.join("upload/u/a.jpg")).unwrap();
        let (_, report) = mirror(&hdd, &usb, true, Check::Quick).unwrap();
        assert_eq!(report.both_damaged, ["upload/u/a.jpg"]);
        assert!(copy_tree(&hdd, &usb, true, Check::Quick).is_err());
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn a_symlink_in_a_backup_tree_is_reported_not_skipped() {
        let root = scratch("enumerate");
        write(&root.join("hdd/upload/a.jpg"), b"a");
        std::os::unix::fs::symlink("/etc", root.join("hdd/upload/escape")).unwrap();
        let error = tree_files(&root.join("hdd")).unwrap_err().to_string();
        assert!(error.contains("symlink"), "{error}");
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn incomplete_cloud_copy_is_reported_with_its_files() {
        assert_eq!(CloudCopy::default().missing(), None);
        let partial = CloudCopy {
            skipped: 1,
            skipped_files: vec!["alice/open.docx".into()],
            refused: vec!["bob/a:b.txt".into()],
            ..Default::default()
        };
        let missing = partial.missing().unwrap();
        assert_eq!(missing["in_use"], 1);
        assert_eq!(missing["unsupported_names"], 1);
        assert_eq!(missing["files"], json!(["alice/open.docx", "bob/a:b.txt"]));
    }
    #[test]
    fn changed_files_become_dated_versions_never_overwrites() {
        let root = scratch("versions");
        let (src, dst) = (root.join("src/report.docx"), root.join("dst/report.docx"));
        write(&src, b"v1");
        assert_eq!(
            add_only_copy(&src, &dst, true, 0o600, Check::Quick).unwrap(),
            Added::New
        );
        assert_eq!(
            add_only_copy(&src, &dst, true, 0o600, Check::Quick).unwrap(),
            Added::Same
        );
        // Edited document: the original backup stays, the edit is added beside it.
        write(&src, b"v2 edited");
        assert_eq!(
            add_only_copy(&src, &dst, true, 0o600, Check::Quick).unwrap(),
            Added::Version
        );
        assert_eq!(fs::read(&dst).unwrap(), b"v1");
        let files = names(&root.join("dst"));
        assert_eq!(files.len(), 3); // original, dated version, note of the current one
        let version = files
            .iter()
            .find(|n| n.contains(".neko-") && !n.ends_with(".neko-latest"))
            .unwrap();
        assert_eq!(fs::read_to_string(latest_marker(&dst)).unwrap(), *version);
        assert!(
            version.starts_with("report.neko-") && version.ends_with(".docx"),
            "{version}"
        );
        // Next week, unchanged since that version: nothing new.
        assert_eq!(
            add_only_copy(&src, &dst, true, 0o600, Check::Quick).unwrap(),
            Added::Same
        );
        assert_eq!(names(&root.join("dst")).len(), 3);
        // A dry run never writes.
        write(&src, b"v3");
        assert_eq!(
            add_only_copy(&src, &dst, false, 0o600, Check::Quick).unwrap(),
            Added::Version
        );
        assert_eq!(names(&root.join("dst")).len(), 3);
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn the_usb_copy_of_the_archive_keeps_the_archive_notes() {
        let root = scratch("mirror-notes");
        let (src, hdd, usb) = (root.join("src/a.jpg"), root.join("hdd"), root.join("usb"));
        write(&src, b"AAAA");
        add_only_copy(&src, &hdd.join("a.jpg"), true, 0o600, Check::Quick).unwrap();
        write(&src, b"BBBB");
        add_only_copy(&src, &hdd.join("a.jpg"), true, 0o600, Check::Quick).unwrap();
        copy_tree(&hdd, &usb, true, Check::Quick).unwrap();
        assert_eq!(names(&usb), names(&hdd)); // the ledger and its lock included
        write(&src, b"AAAA"); // undone on the server
        add_only_copy(&src, &hdd.join("a.jpg"), true, 0o600, Check::Quick).unwrap();
        // A monthly full check of the USB copy must not rewrite the mirrored note.
        copy_tree(&hdd, &usb, true, Check::Full).unwrap();
        let note = latest_marker(&usb.join("a.jpg"));
        assert_eq!(fs::read_to_string(note).unwrap(), "a.jpg");
        assert_eq!(names(&usb), names(&hdd));
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn a_damaged_usb_copy_is_replaced_not_kept_as_current() {
        // AAAA -> BBBB on the server; the USB copy of the current version is damaged.
        let root = scratch("damaged-usb");
        let (src, hdd, usb) = (root.join("src/a.jpg"), root.join("hdd"), root.join("usb"));
        write(&src, b"AAAA");
        keep(&src, &hdd, "a.jpg");
        write(&src, b"BBBB");
        keep(&src, &hdd, "a.jpg");
        copy_tree(&hdd, &usb, true, Check::Quick).unwrap();
        let current = fs::read_to_string(latest_marker(&usb.join("a.jpg"))).unwrap();
        fs::write(usb.join(&current), b"ZZZZ").unwrap(); // same size, written later
        copy_tree(&hdd, &usb, true, Check::Quick).unwrap();
        assert_eq!(fs::read(usb.join(&current)).unwrap(), b"BBBB");
        assert_eq!(
            fs::read_to_string(latest_marker(&usb.join("a.jpg"))).unwrap(),
            current
        );
        // No extra dated copy; the damaged one is kept aside, outside the photo folders.
        let mut usb_names = names(&usb);
        usb_names.retain(|n| n != "neko-damaged");
        let mut hdd_names = names(&hdd);
        hdd_names.retain(|n| n != ".neko-ledger.lock");
        usb_names.retain(|n| n != ".neko-ledger.lock");
        assert_eq!(usb_names, hdd_names);
        let kept: Vec<_> = fs::read_dir(usb.join("neko-damaged"))
            .unwrap()
            .flatten()
            .collect();
        assert_eq!(fs::read(kept[0].path().join(&current)).unwrap(), b"ZZZZ");
        // A damaged file no note names is found by the 4-weekly full check.
        fs::write(usb.join("a.jpg"), b"XXXX").unwrap();
        copy_tree(&hdd, &usb, true, Check::Full).unwrap();
        assert_eq!(fs::read(usb.join("a.jpg")).unwrap(), b"AAAA");
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn an_undone_edit_makes_the_original_current_again() {
        // AAAA -> BBBB -> AAAA: no new copy, but the note says the original is current.
        let root = scratch("undo");
        let (src, dst) = (root.join("src/a.txt"), root.join("dst/a.txt"));
        write(&src, b"AAAA");
        add_only_copy(&src, &dst, true, 0o600, Check::Quick).unwrap();
        assert!(!latest_marker(&dst).exists()); // one copy only: no note needed
        write(&src, b"BBBB");
        add_only_copy(&src, &dst, true, 0o600, Check::Quick).unwrap();
        let edited = fs::read_to_string(latest_marker(&dst)).unwrap();
        assert!(edited.starts_with("a.neko-"));
        write(&src, b"AAAA");
        assert_eq!(
            add_only_copy(&src, &dst, true, 0o600, Check::Quick).unwrap(),
            Added::Same
        );
        assert_eq!(fs::read_to_string(latest_marker(&dst)).unwrap(), "a.txt");
        // Redo the edit: that saved version is current again, still without a new copy.
        write(&src, b"BBBB");
        assert_eq!(
            add_only_copy(&src, &dst, true, 0o600, Check::Full).unwrap(),
            Added::Same
        );
        assert_eq!(fs::read_to_string(latest_marker(&dst)).unwrap(), edited);
        assert_eq!(names(&root.join("dst")).len(), 3);
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn version_names_keep_the_extension() {
        assert_eq!(
            version_name(Path::new("/b/IMG_1.HEIC.xmp"), "S"),
            Path::new("/b/IMG_1.HEIC.neko-S.xmp")
        );
        assert_eq!(
            version_name(Path::new("/b/notes"), "S"),
            Path::new("/b/notes.neko-S")
        );
        assert_eq!(
            version_name(Path::new("/b/.profile"), "S"),
            Path::new("/b/.profile.neko-S")
        );
    }
    #[test]
    fn nextcloud_copy_takes_only_users_own_files() {
        let root = scratch("cloud");
        let cloud = root.join("cloud");
        write(&cloud.join("alice/files/Documents/plan.odt"), b"plan");
        write(&cloud.join("alice/files/Photos/a.jpg"), b"jpg");
        write(&cloud.join("alice/files_trashbin/files/old.txt"), b"trash");
        write(&cloud.join("alice/files_versions/plan.odt.v1"), b"old");
        write(&cloud.join("alice/uploads/chunk.part"), b"partial");
        write(&cloud.join("appdata_oc123/preview/1/big.png"), b"preview");
        write(&cloud.join("bob/files/todo.txt"), b"todo");
        write(&cloud.join("nextcloud.log"), b"log");
        write(&cloud.join(".ocdata"), b"");
        let usb = root.join("usb");
        fs::create_dir_all(&usb).unwrap();
        let first = copy_cloud(&cloud, &usb, true, false, Check::Quick).unwrap();
        assert_eq!((first.copied, first.versions, first.skipped), (3, 0, 0));
        assert!(first.complete());
        assert_eq!(
            names(&usb.join("nextcloud")),
            [".neko-ledger.json", ".neko-ledger.lock", "alice", "bob"]
        );
        assert_eq!(names(&usb.join("nextcloud/alice")), ["files"]);
        assert_eq!(
            fs::read(usb.join("nextcloud/alice/files/Documents/plan.odt")).unwrap(),
            b"plan"
        );
        assert_eq!(
            fs::metadata(usb.join("nextcloud/bob/files/todo.txt"))
                .unwrap()
                .mode()
                & 0o777,
            0o600
        );
        // Second week, one edit and one new file.
        write(&cloud.join("alice/files/Documents/plan.odt"), b"plan v2");
        write(&cloud.join("bob/files/new.txt"), b"new");
        let second = copy_cloud(&cloud, &usb, true, false, Check::Quick).unwrap();
        assert_eq!((second.copied, second.versions, second.skipped), (1, 1, 0));
        // Deleted on the server: kept on the USB.
        fs::remove_file(cloud.join("bob/files/todo.txt")).unwrap();
        copy_cloud(&cloud, &usb, true, false, Check::Quick).unwrap();
        assert!(usb.join("nextcloud/bob/files/todo.txt").exists());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn exfat_unsafe_names_are_skipped_not_fatal() {
        assert!(fat_safe(Path::new("Docs/Report 2026.docx")));
        for bad in [
            "Meeting 10:30.docx",
            "what?.txt",
            "a\"b",
            "dir./x",
            "x/trailing ",
            "tab\there",
        ] {
            assert!(!fat_safe(Path::new(bad)), "{bad}");
        }
        let root = scratch("fat");
        let cloud = root.join("cloud");
        write(&cloud.join("alice/files/ok.txt"), b"ok");
        write(&cloud.join("alice/files/Meeting 10:30.docx"), b"colon");
        let usb = root.join("usb");
        fs::create_dir_all(&usb).unwrap();
        let fat = copy_cloud(&cloud, &usb, true, true, Check::Quick).unwrap();
        assert_eq!((fat.copied, fat.refused.len()), (1, 1));
        assert!(!fat.complete());
        assert_eq!(names(&usb.join("nextcloud/alice/files")), ["ok.txt"]);
        // On a Linux filesystem the same file is copied normally.
        let linux = copy_cloud(&cloud, &usb, true, false, Check::Quick).unwrap();
        assert_eq!(linux.copied, 1);
        assert!(linux.complete());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn old_database_dumps_are_pruned_newest_kept() {
        let root = scratch("dumps");
        for stamp in [
            "20260104T001500Z",
            "20260111T001500Z",
            "20260118T001500Z",
            "20251228T001500Z",
        ] {
            write(&root.join(format!("immich-{stamp}.sql")), b"dump");
        }
        write(&root.join("immich-20250101T000000Z.sql.part"), b"partial");
        write(&root.join("notes.txt"), b"mine");
        let removed = prune_dumps(&root, 2, None).unwrap();
        assert_eq!(
            removed,
            ["immich-20251228T001500Z.sql", "immich-20260104T001500Z.sql"]
        );
        assert_eq!(
            names(&root),
            [
                "immich-20250101T000000Z.sql.part",
                "immich-20260111T001500Z.sql",
                "immich-20260118T001500Z.sql",
                "notes.txt"
            ]
        );
        assert!(prune_dumps(&root, 0, None).unwrap().is_empty());
        fs::remove_dir_all(root).unwrap();
    }
}
