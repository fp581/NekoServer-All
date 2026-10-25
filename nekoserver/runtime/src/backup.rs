use crate::ledger::{self, Ledger, Recorded, LEDGER};
use crate::{
    common::{self, Error, Result},
    inventory::{self, Inventory},
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
/// A file as it was when its checksum was read: identity, size and both times.
type Stamp = (u64, u64, u64, i64, i64, i64, i64);
fn stamp(m: &Metadata) -> Stamp {
    (
        m.dev(),
        m.ino(),
        m.len(),
        m.mtime(),
        m.mtime_nsec(),
        m.ctime(),
        m.ctime_nsec(),
    )
}
/// Each file (device, inode) with how it was and its checksum.
type Digests = std::collections::HashMap<(u64, u64), (Stamp, String)>;
/// Checksums of HDD archive files already read in this monthly check. The check reads
/// every copy: the archive step reads each one against the Immich original, its audit
/// read them all again and the USB check a third time, which on a big library took the
/// whole night. Within one check each file is read once; a file changed since (its
/// identity, size or times differ) is read again. Off outside the monthly check.
static READ_ONCE: std::sync::Mutex<Option<Digests>> = std::sync::Mutex::new(None);
/// Keeps each file's checksum for the rest of the monthly check while it is held.
pub(crate) struct ReadOnce;
pub(crate) fn read_once() -> ReadOnce {
    if let Ok(mut held) = READ_ONCE.lock() {
        *held = Some(std::collections::HashMap::new());
    }
    ReadOnce
}
impl Drop for ReadOnce {
    fn drop(&mut self) {
        if let Ok(mut held) = READ_ONCE.lock() {
            *held = None;
        }
    }
}
/// `path`'s checksum was just read as `sha256` (kept only during the monthly check).
fn remember_digest(path: &Path, sha256: &str) {
    let Ok(meta) = fs::metadata(path) else {
        return;
    };
    if let Ok(mut held) = READ_ONCE.lock() {
        if let Some(map) = held.as_mut() {
            map.insert((meta.dev(), meta.ino()), (stamp(&meta), sha256.to_string()));
        }
    }
}
/// `path`'s checksum, read from the file unless this monthly check already read it as
/// it is now.
fn digest_once(path: &Path) -> Result<String> {
    let meta = fs::metadata(path)?;
    let known = READ_ONCE.lock().ok().and_then(|held| {
        held.as_ref()?
            .get(&(meta.dev(), meta.ino()))
            .filter(|(seen, _)| *seen == stamp(&meta))
            .map(|(_, sha)| sha.clone())
    });
    if let Some(sha) = known {
        return Ok(sha);
    }
    let sha = common::digest(path)?;
    remember_digest(path, &sha);
    Ok(sha)
}
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
/// source is compared by content. Full checks (the monthly run's first step) compare everything.
/// A copy dated after now was written while the clock was ahead: its date says nothing
/// about when it was made, so it is compared by content too.
fn copied_since_change(source: &Metadata, copy: &Metadata) -> bool {
    let written = copy.mtime() as f64 + copy.mtime_nsec() as f64 / 1e9;
    source.len() == copy.len()
        && written <= common::now()
        && (copy.mtime(), copy.mtime_nsec()) >= (source.ctime(), source.ctime_nsec())
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
    Ok(listed(root)?)
}
fn listed(root: &Path) -> std::io::Result<Vec<PathBuf>> {
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
/// target already held the same content, or on a dry run). The data is streamed into a
/// short partial file next to it (see `partial_name`) while being hashed, fsync'd, then read back from disk and hashed
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
    let temp = parent.join(partial_name(
        &target
            .file_name()
            .ok_or_else(|| Error::new("Invalid target"))?
            .to_string_lossy(),
    ));
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
/// Longest file name ext4 and exFAT accept, in bytes (exFAT counts UTF-16 units, never
/// more than the UTF-8 bytes).
const NAME_MAX: usize = 255;
/// Room kept for a version's date: `.neko-20261022T104800Z-9999`.
const VERSION_TAG: usize = 27;
/// Next to dated versions: the full name behind a shortened version stem ("names"), and
/// since 0.28.7 which file each dated version belongs to ("versions"), so a restore never
/// takes a file that only looks like a version (`report.neko-20261022T120000Z.txt`
/// saved in its own right) for one.
pub(crate) const NAMES: &str = ".neko-names.json";
/// The ledger's files at a tree's root: the ledger, its lock, its temporary copies and
/// one set aside as unreadable (neko_common.not_backup_data says the same).
fn ledger_file(key: &str) -> bool {
    ledger::own_file(key) || key.starts_with(&format!("{}.", ledger::LEDGER))
}
/// Backup metadata kept in any folder of a tree, by exact name or pattern only: the
/// version names, current-version notes, partial copies and atomic-write temporaries.
/// Any other name, a user's own `.neko-notes.txt` among them, is data.
pub(crate) fn metadata_name(name: &str) -> bool {
    let atomic_temp = name.strip_prefix(".neko-").is_some_and(|rest| {
        let parts: Vec<&str> = rest.split('-').collect();
        parts.len() == 3
            && parts
                .iter()
                .all(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit()))
    });
    name == NAMES || name.ends_with(".neko-latest") || name.ends_with(".neko-part") || atomic_temp
}
/// The first 8 hex digits of a name's SHA-256: two long names cut to the same start
/// still get different short names.
fn name_tag(whole: &str) -> String {
    format!(
        "~{}",
        &format!("{:x}", Sha256::digest(whole.as_bytes()))[..8]
    )
}
/// `text` when it fits in `room` bytes; otherwise its start (cut between characters)
/// and the tag of `whole`. The same rule as neko_common.fitted.
fn fitted(text: &str, room: usize, whole: &str) -> String {
    if text.len() <= room {
        return text.to_string();
    }
    let tag = name_tag(whole);
    let mut cut = room.saturating_sub(tag.len());
    while !text.is_char_boundary(cut) {
        cut -= 1;
    }
    format!("{}{tag}", &text[..cut])
}
/// Where a copy is written before it is checked and published: a short name of its own
/// (`.neko-<16 hex>.neko-part`), so a file whose name is already 255 bytes long can be
/// copied too. Always the same for one file, so a part left by a cut-off run is replaced.
fn partial_name(name: &str) -> String {
    format!(
        ".neko-{}.neko-part",
        &format!("{:x}", Sha256::digest(name.as_bytes()))[..16]
    )
}
/// The stem and extension of a file's dated versions: `name.ext` -> `name` and `.ext`.
/// A name too long for the date is shortened (see `fitted`); NAMES keeps its full name.
fn version_parts(name: &str) -> (String, String) {
    let (stem, ext) = match name.rsplit_once('.') {
        Some((stem, ext)) if !stem.is_empty() => (stem, format!(".{ext}")),
        _ => (name, String::new()),
    };
    if stem.len() + VERSION_TAG + ext.len() <= NAME_MAX {
        return (stem.to_string(), ext);
    }
    // An extension so long that little of the name would be left goes into the stem.
    let (stem, ext) = if ext.len() + VERSION_TAG + 24 > NAME_MAX {
        (name, String::new())
    } else {
        (stem, ext)
    };
    (fitted(stem, NAME_MAX - VERSION_TAG - ext.len(), name), ext)
}
/// `name.ext` -> `name.neko-<stamp>.ext`, keeping the extension so the copy still opens.
fn version_name(target: &Path, stamp: &str) -> PathBuf {
    let name = target.file_name().unwrap_or_default().to_string_lossy();
    let (stem, ext) = version_parts(&name);
    target.with_file_name(format!("{stem}.neko-{stamp}{ext}"))
}
/// Write down the full name behind a shortened version stem (in the folder's NAMES), so
/// a restore puts the file back under its own name.
fn remember_name(folder: &Path, stem: &str, name: &str, apply: bool) -> Result<()> {
    let path = folder.join(NAMES);
    let mut saved = common::json(&path, 4_000_000);
    if saved["format"] != "neko-names-v1" || !saved["names"].is_object() {
        saved = json!({"format": "neko-names-v1", "names": {}});
    }
    if !apply || saved["names"][stem] == name {
        return Ok(());
    }
    saved["names"][stem] = json!(name);
    common::atomic_json(&path, &saved)
}
/// Write down that `version` (a dated copy in `folder`) is a later state of `name`.
fn remember_version(folder: &Path, version: &str, name: &str, apply: bool) -> Result<()> {
    let path = folder.join(NAMES);
    let mut saved = common::json(&path, 4_000_000);
    if saved["format"] != "neko-names-v1" || !saved["names"].is_object() {
        saved = json!({"format": "neko-names-v1", "names": {}});
    }
    if !saved["versions"].is_object() {
        saved["versions"] = json!({});
    }
    if !apply || saved["versions"][version] == name {
        return Ok(());
    }
    saved["versions"][version] = json!(name);
    common::atomic_json(&path, &saved)
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
    let (stem, ext) = version_parts(&name);
    let parent = target
        .parent()
        .ok_or_else(|| Error::new("Target parent missing"))?;
    for (other, path) in &saved_versions(target)? {
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
    if format!("{stem}{ext}") != name {
        remember_name(parent, &stem, &name, apply)?;
    }
    let landed = copy_verified(source, &versioned, apply, mode)?;
    remember_version(
        parent,
        &versioned.file_name().unwrap_or_default().to_string_lossy(),
        &name,
        apply,
    )?;
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
/// The dated versions saved beside `target` (name, path), oldest first.
fn saved_versions(target: &Path) -> Result<Vec<(String, PathBuf)>> {
    let name = target
        .file_name()
        .unwrap_or_default()
        .to_string_lossy()
        .to_string();
    let (stem, ext) = version_parts(&name);
    // Versions saved before 0.28.5 kept the whole name even when it was very long.
    let plain = match name.rsplit_once('.') {
        Some((s, _)) if !s.is_empty() => s.to_string(),
        _ => name.clone(),
    };
    let parent = target
        .parent()
        .ok_or_else(|| Error::new("Target parent missing"))?;
    // Exactly `<stem>.neko-<date>[-n]<ext>`: a file without an extension never takes
    // another file's versions (README.neko-<date>.md belongs to README.md, not README).
    let dated = regex::Regex::new(&format!(
        r"^(?:{}|{})\.neko-[0-9]{{8}}T[0-9]{{6}}Z(?:-[0-9]+)?{}$",
        regex::escape(&stem),
        regex::escape(&plain),
        regex::escape(&ext)
    ))
    .map_err(|_| Error::new("Invalid version pattern"))?;
    let mut versions = vec![];
    for entry in fs::read_dir(parent)? {
        let path = entry?.path();
        let other = path
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .to_string();
        if dated.is_match(&other) && path.is_file() {
            versions.push((other, path));
        }
    }
    versions.sort();
    Ok(versions)
}
/// Where a user's Nextcloud copy keeps what a later change of shape replaced (a file
/// that became a folder, or a folder that became a file), beside `files`:
/// `<user>/REPLACED/<when>/<its path in files>`. Restores offer it with the files.
pub(crate) const REPLACED: &str = "neko-replaced";
/// What stands in the way of copying `relative` into `files`: a file where one of its
/// folders must go, or a folder where the file must go (the shape changed since the
/// last copy). The additive copy never deletes, so these are moved aside first.
fn in_the_way(files: &Path, relative: &Path) -> Vec<PathBuf> {
    let mut found = vec![];
    let mut here = files.to_path_buf();
    let parts: Vec<_> = relative.components().collect();
    for (i, part) in parts.iter().enumerate() {
        here.push(part);
        let Ok(meta) = fs::symlink_metadata(&here) else {
            break;
        };
        let last = i + 1 == parts.len();
        if (!last && !meta.is_dir()) || (last && meta.is_dir()) {
            found.push(here.clone());
            break;
        }
    }
    found
}
/// Move `object` (a file with its dated versions and note, or a whole folder) from
/// `files` to the same place under `history`, its checksums moving with it in the
/// ledger. Returns how many saved files moved.
fn move_aside(
    object: &Path,
    files: &Path,
    history: &Path,
    ledger: &mut Ledger,
    root: &Path,
) -> Result<usize> {
    let rel = object
        .strip_prefix(files)
        .map_err(|_| Error::new("Invalid relative path"))?;
    let to = history.join(rel);
    let parent = to
        .parent()
        .ok_or_else(|| Error::new("Target parent missing"))?;
    common::directory(parent, 0o700)?;
    let mut moving = vec![object.to_path_buf()];
    if !fs::symlink_metadata(object)?.is_dir() {
        moving.extend(saved_versions(object)?.into_iter().map(|(_, p)| p));
        let note = latest_marker(object);
        if note.exists() {
            moving.push(note);
        }
        // The folder's record of which version belongs to which file goes too.
        let names = object.with_file_name(NAMES);
        if names.is_file() && !parent.join(NAMES).exists() {
            fs::copy(&names, parent.join(NAMES))?;
        }
    }
    let mut moved = 0;
    for from in moving {
        let target = parent.join(from.file_name().unwrap_or_default());
        if target.exists() {
            return Err(Error::new(&format!(
                "Cannot keep the replaced {} aside: {} exists",
                common::sanitize(&from.to_string_lossy()),
                common::sanitize(&target.to_string_lossy())
            )));
        }
        let (old, new) = (ledger_key(&from, root)?, ledger_key(&target, root)?);
        moved += if from.is_dir() {
            tree_files(&from)?.len()
        } else {
            1
        };
        common::on_drive(&target)?;
        fs::rename(&from, &target)?;
        ledger.moved(&old, &new)?;
        // Saved at once: if the run stopped later, the saved list would still expect the
        // file at its old place and report it missing for good.
        ledger.save()?;
    }
    Ok(moved)
}
/// `.name.ext.neko-latest` next to a file that has saved versions: names the saved copy
/// (`name.ext` or one of its dated versions) that matches the file as the backup last
/// saw it, so "restore the newest" restores that one, also after an edit was undone.
/// A name too long for the note's own name is shortened (as neko_common.latest_note).
pub(crate) fn latest_marker(target: &Path) -> PathBuf {
    let name = target.file_name().unwrap_or_default().to_string_lossy();
    let room = NAME_MAX - ".".len() - ".neko-latest".len();
    target.with_file_name(format!(".{}.neko-latest", fitted(&name, room, &name)))
}
fn mark_latest(target: &Path, current: &str, apply: bool) -> Result<()> {
    let marker = latest_marker(target);
    if !apply || fs::read(&marker).is_ok_and(|b| b == current.as_bytes()) {
        return Ok(());
    }
    common::atomic(&marker, current.as_bytes(), 0o600)
}
/// Every regular file under `root`. A file or folder deleted or renamed while it is
/// walked (a user tidying up during the backup) is passed over: it is gone, so there is
/// nothing to copy, and the next run sees it under its new name.
fn walk<F: FnMut(&Path) -> Result<()>>(root: &Path, f: &mut F) -> Result<()> {
    common::safe(root)?;
    walk_listed(list(root)?, f)
}
fn walk_listed<F: FnMut(&Path) -> Result<()>>(paths: Vec<PathBuf>, f: &mut F) -> Result<()> {
    for path in paths {
        let meta = match fs::symlink_metadata(&path) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            found => found?,
        };
        if meta.file_type().is_symlink() {
            return Err(Error::new("Source symlink refused"));
        }
        if meta.is_dir() {
            common::safe(&path)?;
            match listed(&path) {
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
                found => walk_listed(found?, f)?,
            }
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
    /// The two copies differ and no checksum says which is right (none recorded yet, or
    /// the HDD's and the USB's ledgers record different ones); both are kept untouched.
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
                "{} files differ between the HDD and the USB drive, with no recorded checksum (or two different ones) to tell which is right; both are kept untouched: {}",
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
///
/// Only a full check reads every copy, so only a full check replaces what an earlier one
/// found. A quick run (size and dates) never re-reads a damaged copy, so it keeps the
/// files already listed and adds its own: otherwise the next weekly run would make damage
/// found by the monthly check look repaired. The repair tool clears the files it fixed.
/// `full_at` says when every copy was last compared; the dashboard trusts only that.
pub fn save_integrity(data: &Path, section: &str, report: &Integrity, check: Check) -> Result<()> {
    let path = data.join("reports/integrity.json");
    let mut all = common::json(&path, 20_000_000);
    if all["format"] != "neko-integrity-v1" {
        all = json!({"format": "neko-integrity-v1", "sections": {}});
    }
    let previous = all["sections"][section].clone();
    let full = check == Check::Full;
    let mut lists = serde_json::Map::new();
    let mut counts = serde_json::Map::new();
    for (key, found) in [
        ("damaged", &report.damaged),
        ("both_damaged", &report.both_damaged),
        ("disagree", &report.disagree),
        ("missing", &report.missing),
    ] {
        let (rows, count) = if full {
            (found.clone(), found.len() as u64)
        } else {
            kept_findings(&previous, key, found)
        };
        lists.insert(
            key.into(),
            json!(rows.into_iter().take(1000).collect::<Vec<_>>()),
        );
        counts.insert(key.into(), json!(count));
    }
    let now = common::now();
    let mut entry = json!({
        "at": now,
        "check": if full { "full" } else { "quick" },
        "full_at": if full { json!(now) } else { previous["full_at"].clone() },
        "newly_verified": report.newly_verified,
        "unverified": report.unverified,
        "repaired": report.repaired,
        "counts": counts,
    });
    for (key, rows) in lists {
        entry[key] = rows;
    }
    all["sections"][section] = entry;
    common::atomic_json(&path, &all)
}
/// What a quick run keeps of `key`: the files an earlier check listed, then the ones it
/// found itself, with their count (an earlier count can exceed its capped list).
fn kept_findings(previous: &Value, key: &str, found: &[String]) -> (Vec<String>, u64) {
    let mut rows: Vec<String> = previous[key]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|v| v.as_str().map(str::to_string))
        .collect();
    let listed = rows.len() as u64;
    let earlier = previous["counts"][key].as_u64().unwrap_or(0).max(listed);
    let mut added = 0;
    for file in found {
        if !rows.contains(file) {
            rows.push(file.clone());
            added += 1;
        }
    }
    (rows, earlier + added)
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
            || key.starts_with(&format!("{}/", inventory::FOLDER))
            || ledger_file(&key)
            || metadata_name(&name))
        {
            out.push((key, path.to_path_buf()));
        }
        Ok(())
    })?;
    Ok(out)
}
/// The monthly deep audit: read every file again and compare it with its recorded
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
                if digest_once(&path)? != entry.sha256 && !report.damaged.contains(&key) {
                    report.damaged.push(key);
                }
            }
            None => report.unverified += 1,
        }
    }
    Ok(())
}
/// Write the run's inventory of live files. Best effort: without it a restore offers
/// every file ever kept, as before 0.39.6, so a failure is said but stops nothing.
fn save_inventory(root: &Path, live: &Inventory, ledger: &Ledger, what: &str) {
    match inventory::write(root, live, ledger) {
        Ok(path) => println!(
            "{what}: noted the {} files you have now ({})",
            live.len(),
            path.display()
        ),
        Err(e) => println!("{what}: the list of the files you have now could not be written ({e}); a restore then offers every file ever kept"),
    }
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
    // Files changed in the last `age` seconds (still being written) wait for the next
    // run; files that changed or went away while being copied are left for it too.
    let (mut waiting, mut in_use) = (0, 0);
    common::directory(&archive, 0o755)?;
    let mut ledger = if apply {
        Ledger::open_waiting(&archive, LEDGER_WAIT)?
    } else {
        Ledger::read(&archive)?
    };
    let mut report = Integrity::default();
    let quarantine = quarantine_folder(&archive);
    // The live originals, each with the archive copy that holds it (see inventory.rs).
    let mut live = Inventory::default();
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
            let meta = match fs::metadata(path) {
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                    in_use += 1;
                    return Ok(());
                }
                found => found?,
            };
            // A photo dated ahead of the clock (a camera set wrong) is archived now: the
            // copy is verified anyway, and its date may not come for years.
            let modified = meta.mtime() as f64 + meta.mtime_nsec() as f64 / 1e9;
            let target = archive.join(
                path.strip_prefix(&source)
                    .map_err(|_| Error::new("Invalid archive relative path"))?,
            );
            if common::settling(modified, age as f64) {
                waiting += 1;
                // Still live: its earlier copy (if any) stays in the inventory.
                live.add(&target, &archive);
                return Ok(());
            }
            let key = ledger_key(&target, &archive)?;
            let expected = ledger.get(&key).map(|e| e.sha256.clone());
            // Deleted, renamed or changed while it was copied (Immich at work, someone
            // tidying up): left for the next run, as the Nextcloud copy does.
            let kept = match kept_copy(path, &target, apply, 0o644, check, expected.as_deref()) {
                Ok(kept) => kept,
                Err(e) if e.to_string().contains("changed") || !path.exists() => {
                    in_use += 1;
                    if path.exists() {
                        live.add(&target, &archive);
                    }
                    return Ok(());
                }
                Err(e) => return Err(e),
            };
            live.add(&target, &archive);
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
            // Read once: the audit and the USB check below need not read it again.
            if let (Some(file), Some(sha)) = (&kept.file, &kept.sha256) {
                remember_digest(file, sha);
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
        save_inventory(&archive, &live, &ledger, "Archive");
    }
    report.print("Archive");
    if versions > 0 {
        println!("Archive: {versions} changed files kept as dated versions");
    }
    if waiting > 0 {
        println!("Archive: {waiting} files changed in the last {age} seconds wait for the next run (they may still be being written)");
    }
    if in_use > 0 {
        println!("Archive: {in_use} files changed, moved or were deleted while being copied; the next run copies them if they are still there");
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
/// current-version note are read every week and everything in the monthly check (`Full`).
/// The archive's notes and ledger are mirrored after the files they describe.
pub fn copy_tree(source: &Path, dest: &Path, apply: bool, check: Check) -> Result<u64> {
    let (copied, report) = mirror(source, dest, apply, check)?;
    match report.problem("HDD archive") {
        Some(problem) => Err(Error::new(&problem)),
        None => Ok(copied),
    }
}
/// How long the archive and the weekly USB copy wait for each other's hold on the HDD
/// archive's checksum ledger (a first big archive run can take hours).
const LEDGER_WAIT: std::time::Duration = std::time::Duration::from_secs(6 * 3600);
pub fn mirror(source: &Path, dest: &Path, apply: bool, check: Check) -> Result<(u64, Integrity)> {
    let (source, dest) = separate(source, dest)?;
    let mut ledger = if apply {
        Ledger::open_waiting(&source, LEDGER_WAIT)?
    } else {
        Ledger::read(&source)?
    };
    let mut report = Integrity::default();
    let quarantine = quarantine_folder(&dest);
    // The USB copy's own ledger: its records stay, also when the HDD's ledger was lost
    // or is incomplete (they say which copy is good), and the HDD's ledger takes back
    // the ones it lacks. Two ledgers recording different checksums decide nothing.
    let usb_ledger = match Ledger::read(&dest) {
        Ok(found) => Some(found),
        Err(e) => {
            // Never written over: moved aside (kept), and a new one is started from the HDD's.
            println!("Backup: the USB drive's checksum list could not be read ({e}); it is kept as {LEDGER}.unreadable-*");
            if apply && dest.join(LEDGER).exists() {
                let aside = quarantine_folder(&dest);
                let stamp = aside
                    .file_name()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .to_string();
                fs::rename(
                    dest.join(LEDGER),
                    dest.join(format!("{LEDGER}.unreadable-{stamp}")),
                )?;
            }
            None
        }
    };
    let mut conflicting = std::collections::BTreeSet::new();
    for (key, entry) in usb_ledger.iter().flat_map(Ledger::entries) {
        if ledger.adopt(key, entry)? == Recorded::Conflict {
            conflicting.insert(key.clone());
        }
    }
    let files = tree_files(&source)?;
    // Files a current-version note names: what "restore newest" will take.
    let mut noted = std::collections::BTreeSet::new();
    let mut notes = vec![];
    walk(&source, &mut |path| {
        let name = path.file_name().unwrap_or_default().to_string_lossy();
        if name == NAMES {
            notes.push(path.to_path_buf()); // full names of shortened versions
        } else if name.starts_with('.') && name.ends_with(".neko-latest") {
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
        if conflicting.contains(key) {
            report.disagree.push(key.clone());
            continue;
        }
        let recorded = ledger.get(key).map(|e| e.sha256.clone());
        if !target.exists() {
            // Never copy a damaged archive file onto the USB drive.
            if let Some(expected) = &recorded {
                if digest_once(path)? != *expected {
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
        let (hdd, usb) = (digest_once(path)?, common::digest(&target)?);
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
        .filter(|k| !present.contains(k) && !conflicting.contains(*k))
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
            let text = common::bytes(note, 4_000_000)?;
            if fs::read(&target).ok().as_deref() != Some(text.as_slice()) {
                if let Some(parent) = target.parent() {
                    common::directory(parent, 0o755)?;
                }
                common::atomic(&target, &text, 0o644)?;
            }
        }
        // The inventories of live files, after the files they name.
        if let Err(e) = inventory::mirror(&source, &dest) {
            println!("Backup: the lists of the files you have now could not be copied ({e}); a restore from the USB drive then offers every file ever kept");
        }
        // Ledgers last: the HDD one with this run's agreements, then its USB copy, which
        // keeps every record it already had.
        ledger.save()?;
        ledger.publish_copy(&dest, usb_ledger.as_ref())?;
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
        common::directory(parent, 0o700)?;
    }
    fs::rename(common::safe(target)?, common::safe(quarantine)?)?;
    verified_copy(source, target, true, 0o644)?;
    Ok(())
}
/// Nextcloud's own folder in its data folder, `appdata_<instance id>` (previews, caches):
/// named by `instanceid` in its config.php on the SSD, or, when that cannot be read, a
/// name of the form Nextcloud gives it (`appdata_oc` and 10 lowercase letters and
/// digits). Only that folder is Nextcloud's: a user named `appdata_alice` is a user.
fn nextcloud_internal(data: &Path) -> Option<String> {
    let config = common::bytes(
        &data.join("appdata/nextcloud/html/config/config.php"),
        1_000_000,
    )
    .ok()?;
    regex::Regex::new(r"'instanceid'\s*=>\s*'([a-z0-9]{2,40})'")
        .unwrap()
        .captures(&String::from_utf8_lossy(&config))
        .map(|c| format!("appdata_{}", &c[1]))
}
/// A file Nextcloud is still receiving: `<name>.ocTransferId<digits>.part`. Any other
/// name ending in .part is a user's own file and is backed up (capacity.py agrees).
fn nextcloud_upload_in_progress(name: &str) -> bool {
    regex::Regex::new(r"\.ocTransferId[0-9]+\.part$")
        .unwrap()
        .is_match(name)
}
fn is_nextcloud_internal(name: &str, known: Option<&str>) -> bool {
    match known {
        Some(own) => name == own,
        None => regex::Regex::new(r"^appdata_oc[a-z0-9]{10}$")
            .unwrap()
            .is_match(name),
    }
}
/// Add-only copy of each Nextcloud user's own files (`<user>/files`) to
/// `dest/nextcloud/<user>/files`. Nextcloud's own folder (`internal`, see
/// nextcloud_internal), trash, versions, caches and logs are skipped. A file that changes or disappears mid-copy (a live upload or
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
/// exFAT also takes upper and lower case for the same letter: `Documents/report.pdf` and
/// `documents/report.pdf` would land in one folder, as two versions of one file, and a
/// restore could not tell them apart again. Every folder and file name of a path is
/// compared with the ones already copied this run (`seen`: lower case -> as written);
/// the first one keeps its place and a later one that differs only in case is left out.
/// Returns the earlier path it clashes with.
fn case_clash(path: &str, seen: &mut std::collections::HashMap<String, String>) -> Option<String> {
    let mut prefixes = vec![];
    let mut prefix = String::new();
    for part in path.split('/') {
        if !prefix.is_empty() {
            prefix.push('/');
        }
        prefix.push_str(part);
        prefixes.push(prefix.clone());
    }
    for p in &prefixes {
        if let Some(earlier) = seen.get(&p.to_lowercase()) {
            if earlier != p {
                return Some(earlier.clone());
            }
        }
    }
    for p in prefixes {
        seen.insert(p.to_lowercase(), p);
    }
    None
}
/// Names already kept on the USB drive (its files, and those its ledger lists) take
/// their place in the exFAT check before this week's files: an old `Documents/report.pdf`
/// that stays on the backup still clashes with a new `documents/report.pdf`.
fn remember_kept(path: &str, seen: &mut std::collections::HashMap<String, String>) {
    let mut prefix = String::new();
    for part in path.split('/') {
        if !prefix.is_empty() {
            prefix.push('/');
        }
        prefix.push_str(part);
        seen.entry(prefix.to_lowercase())
            .or_insert_with(|| prefix.clone());
    }
}
fn kept_names(
    root: &Path,
    user: &str,
    ledger: &Ledger,
    seen: &mut std::collections::HashMap<String, String>,
) -> Result<()> {
    let prefix = format!("{user}/files/");
    for key in ledger.keys() {
        if let Some(rest) = key.strip_prefix(&prefix) {
            remember_kept(&format!("{user}/{rest}"), seen);
        }
    }
    let files = root.join(user).join("files");
    if fs::symlink_metadata(&files).is_ok_and(|m| m.is_dir()) {
        walk(&files, &mut |path| {
            if let Some(rest) = path.strip_prefix(&files).ok().and_then(Path::to_str) {
                remember_kept(&format!("{user}/{rest}"), seen);
            }
            Ok(())
        })?;
    }
    Ok(())
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
    internal: Option<&str>,
) -> Result<CloudCopy> {
    let (cloud, dest) = separate(cloud, dest)?;
    let (mut copied, mut versions, mut skipped) = (0, 0, 0);
    let mut refused = vec![];
    let mut clashes = vec![];
    let mut unreadable = vec![];
    let mut seen = std::collections::HashMap::new();
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
    let when = chrono::Utc::now().format("%Y%m%dT%H%M%SZ").to_string();
    let mut replaced = 0;
    // Each user's live files, with the saved copy that holds each (see inventory.rs).
    let mut live = Inventory::default();
    for user_dir in list(&cloud)? {
        let user = user_dir
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .to_string();
        let files = user_dir.join("files");
        if is_nextcloud_internal(&user, internal)
            || user.starts_with('.')
            || !fs::symlink_metadata(&user_dir)?.is_dir()
            || !fs::symlink_metadata(&files).is_ok_and(|m| m.is_dir())
        {
            continue;
        }
        if fat_names {
            kept_names(&root, &user, &ledger, &mut seen)?;
        }
        walk(&files, &mut |path| {
            let name = path.file_name().unwrap_or_default().to_string_lossy();
            if nextcloud_upload_in_progress(&name) || name.ends_with(".neko-part") {
                return Ok(());
            }
            let relative = path
                .strip_prefix(&files)
                .map_err(|_| Error::new("Invalid relative path"))?;
            // A name that is not valid text (made on another system with an old character
            // set) cannot go in the checksum list: that file is left out and named, not
            // the whole copy stopped.
            if !relative
                .to_str()
                .is_some_and(|r| ledger::valid_path(&format!("{user}/files/{r}")))
            {
                let shown = format!("{user}/{}", relative.display());
                unreadable.push(shown.clone());
                refused.push(shown);
                return Ok(());
            }
            if fat_names && !fat_safe(relative) {
                refused.push(format!("{user}/{}", relative.display()));
                return Ok(());
            }
            let shown = format!("{user}/{}", relative.display());
            if fat_names {
                if let Some(earlier) = case_clash(&shown, &mut seen) {
                    clashes.push(format!("{shown} (same as {earlier} on exFAT)"));
                    refused.push(shown);
                    return Ok(());
                }
            }
            let user_files = root.join(&user).join("files");
            let target = user_files.join(relative);
            let in_way = in_the_way(&user_files, relative);
            if !in_way.is_empty() {
                if !apply {
                    copied += 1; // a dry run moves nothing; the copy would go ahead
                    return Ok(());
                }
                let history = root.join(&user).join(REPLACED).join(&when);
                // Each move is saved in the checksum list as it happens (see move_aside).
                for object in &in_way {
                    replaced += move_aside(object, &user_files, &history, &mut ledger, &root)?;
                }
            }
            let key = ledger_key(&target, &root)?;
            let expected = ledger.get(&key).map(|e| e.sha256.clone());
            let result = kept_copy(path, &target, apply, 0o600, check, expected.as_deref());
            if result.is_ok() || path.exists() {
                live.add(&target, &root);
            }
            match result {
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
        save_inventory(&root, &live, &ledger, "Nextcloud");
    }
    report.print("Nextcloud");
    println!("Nextcloud: {copied} new files, {versions} changed files kept as dated versions, {skipped} skipped while in use");
    if replaced > 0 {
        println!("Nextcloud: {replaced} saved files were in the way of a file that became a folder (or a folder that became a file); they are kept in {REPLACED}/{when} beside the user's files");
    }
    if !unreadable.is_empty() {
        println!(
            "Nextcloud: {} files not copied because their names are not valid text (UTF-8), so the backup cannot record them; rename them: {}",
            unreadable.len(),
            unreadable.iter().take(20).cloned().collect::<Vec<_>>().join(", ")
        );
    }
    let exfat: Vec<&String> = refused.iter().filter(|r| !unreadable.contains(r)).collect();
    if !exfat.is_empty() {
        println!(
            "Nextcloud: {} files not copied because the USB drive (exFAT) cannot store their names; rename them or use an ext4 USB drive: {}",
            exfat.len(),
            exfat.iter().take(20).map(|r| r.as_str()).collect::<Vec<_>>().join(", ")
        );
    }
    if !clashes.is_empty() {
        println!(
            "Nextcloud: of these, {} differ from another file or folder only in upper/lower case, which exFAT treats as the same name (they would merge): {}",
            clashes.len(),
            clashes.iter().take(20).cloned().collect::<Vec<_>>().join(", ")
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
/// Weekly Nextcloud database copies kept on the HDD and on the USB drive.
const CLOUD_DUMPS: u64 = 4;
/// An app whose database the weekly jobs copy: Immich (its own weekly job) and
/// Nextcloud (the weekly USB job, before it copies).
struct DumpKind {
    /// The file name prefix, the Compose project (neko-<app>) and the note's version key.
    app: &'static str,
    label: &'static str,
    /// What the database brings back.
    holds: &'static str,
    /// The folder on the HDD (under backups/) and on the USB drive.
    folder: &'static str,
    /// What a copy without a note lacks.
    no_note: &'static str,
    /// The RECOVERY.md section that says how to use it.
    guide: &'static str,
}
const IMMICH_DUMPS: DumpKind = DumpKind {
    app: "immich",
    label: "Immich",
    holds: "albums, faces and users",
    folder: "immich-database",
    no_note: "has no record of its Immich version (made before v0.24.3)",
    guide: "To rebuild Immich from this USB drive, see RECOVERY.md, \"Rebuild Immich from the USB drive\".",
};
const NEXTCLOUD_DUMPS: DumpKind = DumpKind {
    app: "nextcloud",
    label: "Nextcloud",
    holds: "users, shares, calendars and contacts",
    folder: "nextcloud-database",
    no_note: "has no note with its checksum",
    guide: "To bring back Nextcloud's database, see RECOVERY.md, \"Nextcloud's database\".",
};
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
/// The Immich database goes to the USB drive only with the photos it belongs to: a
/// Nextcloud-only run never asks for a database Immich no longer backs up (its Compose
/// file stays when Immich is switched off, so it is no sign Immich is in use).
fn wants_database(photos_selected: bool, data: &Path) -> bool {
    photos_selected && data.join("compose/immich/compose.yaml").is_file()
}
/// Nextcloud's database is copied only with its files (the weekly job is given them
/// only while Nextcloud is switched on) and only once Nextcloud is installed.
fn wants_cloud_database(cloud_selected: bool, data: &Path) -> bool {
    cloud_selected && data.join("compose/nextcloud/compose.yaml").is_file()
}
/// The Nextcloud version installed, from its own version.php ("31.0.5"), if readable.
fn nextcloud_version(data: &Path) -> Option<String> {
    let text = common::bytes(&data.join("appdata/nextcloud/html/version.php"), 64000).ok()?;
    let found = regex::Regex::new(r"\$OC_VersionString\s*=\s*'([0-9]+\.[0-9]+\.[0-9]+)'")
        .unwrap()
        .captures(&String::from_utf8_lossy(&text))?
        .get(1)?
        .as_str()
        .to_string();
    Some(found)
}
/// The weekly copy of Nextcloud's database (users, shares, calendars, contacts), on the
/// HDD in backups/nextcloud-database with a note of the version that made it and its
/// checksum; the newest CLOUD_DUMPS stay. Until 0.39.6 only the monthly snapshot held it.
fn nextcloud_dump(cfg: &Path, data: &Path, media: &Path) -> Result<String> {
    guard(cfg, data, media)?;
    let _drives = (common::expect_drive(media)?, common::expect_drive(data)?);
    common::stage("making the Nextcloud database copy");
    let folder = data.join("compose/nextcloud");
    let dest = media.join("backups").join(NEXTCLOUD_DUMPS.folder);
    let created = database_of(
        NEXTCLOUD_DUMPS.app,
        &folder.join("compose.yaml"),
        &folder.join(".env"),
        &dest,
        true,
    )?
    .ok_or_else(|| Error::new("Database dump missing"))?;
    common::atomic_json(
        &dump_note(&created),
        &json!({"format":"neko-nextcloud-dump-v1","nextcloud_version":nextcloud_version(data),"created_at":common::now(),"sha256":common::digest(&created)?}),
    )?;
    let name = created
        .file_name()
        .unwrap_or_default()
        .to_string_lossy()
        .to_string();
    prune_dumps_of(NEXTCLOUD_DUMPS.app, &dest, CLOUD_DUMPS, Some(&name))?;
    println!("Nextcloud: database copy {name} made on the HDD");
    Ok(name)
}
/// Put the newest Immich database copy on the USB drive with its note, so the USB alone
/// can rebuild albums, faces and users as well as the photos. Returns what makes the
/// USB copy incomplete (no copy, a stale one, one without a note), or None. A copy
/// whose checksum does not match its note is never copied. Only the version is
/// recorded, never the database password.
pub fn copy_newest_dump(hdd_dumps: &Path, usb: &Path) -> Result<Option<String>> {
    copy_newest_of(&IMMICH_DUMPS, hdd_dumps, usb)
}
fn copy_newest_of(kind: &DumpKind, hdd_dumps: &Path, usb: &Path) -> Result<Option<String>> {
    let now = common::now();
    let label = kind.label;
    let dumps: Vec<String> = fs::read_dir(hdd_dumps)
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.file_name().to_string_lossy().to_string())
        .filter(|n| is_dump(kind.app, n))
        .collect();
    let (newest, future) = newest_believable(dumps, now);
    let Some(name) = newest else {
        return Ok(Some(format!(
            "no {label} database copy was found, so the USB drive cannot rebuild {}",
            kind.holds
        )));
    };
    let source = hdd_dumps.join(&name);
    let note = common::json(&dump_note(&source), 64000);
    let expected = note["sha256"].as_str().map(str::to_string);
    let mut problems = vec![];
    if !future.is_empty() {
        // Kept on the HDD (and never pruned): made while the clock was wrong, so their
        // names cannot say when. The newest copy with a believable date goes to the USB.
        problems.push(format!(
            "{} {label} database copies are dated in the future ({}); they were made while the server's clock was wrong (check it with timedatectl) and are kept, but the newest copy with a believable date was used",
            future.len(),
            future.join(", ")
        ));
    }
    match &expected {
        Some(sum) if common::digest(&source)? != *sum => {
            return Ok(Some(format!(
                "the newest {label} database copy ({name}) is damaged (checksum mismatch) and was not copied"
            )));
        }
        Some(_) => {}
        None => problems.push(format!(
            "the newest {label} database copy ({name}) {}",
            kind.no_note
        )),
    }
    let age_days = (now - dump_time(&name)) / 86400.0;
    if age_days < -1.0 {
        problems.push(format!(
            "the newest {label} database copy ({name}) is dated in the future; check the server's clock (timedatectl)"
        ));
    } else if age_days > DUMP_STALE_DAYS {
        problems.push(format!(
            "the newest {label} database copy is {} days old; the database backup has not run",
            age_days as u64
        ));
    }
    let folder = usb.join(kind.folder);
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
    let version = note[format!("{}_version", kind.app)]
        .as_str()
        .unwrap_or("unknown");
    common::atomic(
        &folder.join(format!("{}-VERSION.txt", kind.app.to_uppercase())),
        format!("{label} version that made the newest database copy ({name}): {version}\nEach copy's own version and checksum are in the .json file next to it.\n{}\n", kind.guide).as_bytes(),
        0o600,
    )?;
    prune_dumps_of(kind.app, &folder, USB_DUMPS, Some(&name))?;
    println!("USB: {label} database copy {name} ({label} {version})");
    Ok((!problems.is_empty()).then(|| problems.join("; ")))
}
/// When a database copy was made, from its name (0 when the name cannot say).
fn dump_time(name: &str) -> f64 {
    let stamp = name.split_once('-').map_or("", |(_, rest)| rest);
    chrono::NaiveDateTime::parse_from_str(stamp.get(..15).unwrap_or(""), "%Y%m%dT%H%M%S")
        .map(|t| t.and_utc().timestamp() as f64)
        .unwrap_or(0.0)
}
/// A copy dated more than a day ahead of the clock was made while the clock was wrong.
fn dated_in_future(name: &str, now: f64) -> bool {
    dump_time(name) > now + 86400.0
}
/// (the newest copy with a believable date, the ones dated in the future). With only
/// future-dated copies, the newest of them, flagged by its date.
fn newest_believable(mut dumps: Vec<String>, now: f64) -> (Option<String>, Vec<String>) {
    dumps.sort();
    let (future, fine): (Vec<String>, Vec<String>) =
        dumps.into_iter().partition(|n| dated_in_future(n, now));
    match fine.last() {
        Some(newest) => (Some(newest.clone()), future),
        None => (future.last().cloned(), vec![]),
    }
}
/// `<app>-<UTC stamp>.sql`: a weekly database copy of that app (Immich, Nextcloud; the
/// stamp sorts by time).
fn is_dump(app: &str, name: &str) -> bool {
    name.strip_prefix(app)
        .and_then(|rest| rest.strip_prefix('-'))
        .is_some_and(|rest| {
            regex::Regex::new(r"^[0-9]{8}T[0-9]{6}Z\.sql$")
                .unwrap()
                .is_match(rest)
        })
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
    let out = common::findmnt("SOURCE", "--target", path, 10).ok()??;
    // btrfs shows "/dev/sdb1[/subvolume]".
    let source = out.split('[').next()?.trim().to_string();
    source
        .starts_with("/dev/")
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
    let options = common::findmnt("OPTIONS", "--mountpoint", dest, 10)?.unwrap_or_default();
    if !private_options(&options, fstype) {
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
    let s = common::findmnt("UUID", "--mountpoint", dest, 10)?.unwrap_or_default();
    if !s.eq_ignore_ascii_case(uuid) {
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
    let mut row = running_row(name, now, &old);
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
    // The report lives on the SSD: with the SSD gone it cannot be written, and the job's
    // own error still comes first.
    let reported = (|| {
        if !common::mounted(data) {
            return Err(Error::new("Application SSD disappeared before report"));
        }
        guard_data(cfg, data)?;
        common::atomic_json(&path, &row)
    })();
    common::first_error(result, reported).map(|_| ())
}
/// A job's report as it starts. The last success is kept as saved: one saved while the
/// clock was ahead is still the last success after the clock is put back (the dashboard
/// shows it as now).
fn running_row(name: &str, now: f64, old: &Value) -> Value {
    json!({"job":name,"status":"running","started_at":now,"finished_at":null,"last_success":common::kept_time(&old["last_success"])})
}
fn guard_data(cfgpath: &Path, data: &Path) -> Result<()> {
    let c = common::json(cfgpath, 64000);
    let uuid = c["storage"]["data_ssd"]["uuid"].as_str().unwrap_or("");
    let fstype = c["storage"]["data_ssd"]["fstype"].as_str().unwrap_or("");
    if c["data_mount"].as_str() != data.to_str() || uuid.is_empty() || fstype.is_empty() {
        return Err(Error::new("Application SSD identity required"));
    }
    let output = common::findmnt("UUID,FSTYPE", "--mountpoint", data, 10)?.unwrap_or_default();
    if output.split_whitespace().collect::<Vec<_>>() != vec![uuid, fstype] {
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
/// A cut-off snapshot left something to finish: its resume journal, or a report still
/// saying "running" (cut off before it wrote a journal).
pub fn needs_resume(journal: &Path, report: &Path) -> bool {
    journal.exists() || common::json(report, 64000)["status"] == "running"
}
pub fn mark_interrupted(report: &Path) -> Result<()> {
    let mut row = common::json(report, 64000);
    if row["status"] == "running" {
        row["status"] = json!("fail");
        row["finished_at"] = json!(common::now());
        // Said on the dashboard: without it a cut-off run read as a failure with no cause.
        row["reason"] = json!(INTERRUPTED);
        common::atomic_json(report, &row)?;
    }
    Ok(())
}
pub const INTERRUPTED: &str = "This backup was cut off (a power cut or a restart). What it had stopped was started again and the earlier backups are kept; the next scheduled run makes a new one";
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
/// Called with the job's lock held: what an earlier run that failed or was cut off left
/// half-made (each as big as a whole snapshot) is removed first, or a full drive would
/// stay full and every later run fail too.
pub fn stage_snapshot(root: &Path) -> Result<Stage> {
    common::clear_leftovers(root, true)?;
    common::on_drive(root)?;
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
    protected: Option<&str>,
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
    common::prune_snapshots(root, keep, Some(&stage.name), protected)?;
    Ok(())
}
/// Room in `root` for a new snapshot: `needed` bytes free (its files plus a reserve).
/// While the drive is short, the oldest snapshots are removed first, but the newest
/// complete one always stays, as the one good backup. Still short: the error `short`
/// gives (told what is free). Without this a full drive failed every night for good.
pub fn make_room(root: &Path, needed: u128, short: &dyn Fn(u128) -> String) -> Result<()> {
    make_room_with(root, needed, &|| common::free_bytes(root), short)
}
fn make_room_with(
    root: &Path,
    needed: u128,
    free: &dyn Fn() -> Result<u128>,
    short: &dyn Fn(u128) -> String,
) -> Result<()> {
    loop {
        let have = free()?;
        if have >= needed {
            return Ok(());
        }
        let found = common::complete_snapshots(root)?;
        let [(_, oldest), _, ..] = found.as_slice() else {
            return Err(Error::new(&short(have)));
        };
        common::remove_snapshot(root, oldest)?;
        println!("Backup: the drive was short of space, so the oldest snapshot ({oldest}) was removed early; the newest ones are kept");
    }
}
/// Check the drives, take the lock monthly maintenance and the other jobs hold, then
/// check the drives again. Anything that starts or stops services (including resuming
/// after an interrupted backup) runs only after this, so it never acts on the wrong
/// drives or in the middle of a monthly update or its rollback.
/// Refused while a monthly update was cut off half-way (its journal says "updating" or
/// "restoring"): after a power cut its half-updated apps would otherwise be saved as the
/// newest backup, just before its way back replaces them. In the other phases the apps'
/// data is as it should be: before the update ("snapshot", "backed_up") nothing was
/// changed, and once "committed" the update stands and the apps are in use (only their
/// reopening is left), so the nightly backups go on. A journal that cannot be read
/// could be any phase, so it is refused. Checked with the shared lock held, after which
/// the monthly run cannot start or finish. The resume steps don't call this: they must
/// still be able to start the apps; nor does the settings backup, whose files the
/// monthly update never rolls back.
pub(crate) fn no_unfinished_update() -> Result<()> {
    no_unfinished_update_at(Path::new(crate::maintenance::JOURNAL))
}
const UNFINISHED: &str = "A monthly update has not finished yet, so this backup waits (it would save half-updated apps). It is finished by: sudo systemctl start nekoserver-maintenance-resume.service";
fn no_unfinished_update_at(journal: &Path) -> Result<()> {
    if !journal.exists() {
        return Ok(());
    }
    let saved = common::bytes(journal, 4_000_000)
        .ok()
        .and_then(|b| serde_json::from_slice::<Value>(&b).ok());
    match saved.as_ref().and_then(|j| j["phase"].as_str()) {
        Some("snapshot" | "backed_up" | "committed") => Ok(()),
        Some(_) => Err(Error::new(UNFINISHED)),
        None => Err(Error::new(&format!(
            "{UNFINISHED} (its journal, {}, could not be read)",
            journal.display()
        ))),
    }
}
pub fn coordinate(cfgpath: &Path, data: &Path, media: &Path) -> Result<fs::File> {
    guard(cfgpath, data, media)?;
    let held = common::lock_wait(
        &data.join("installer/recovery.lock"),
        std::time::Duration::from_secs(3 * 3600),
    )?;
    guard(cfgpath, data, media)?;
    Ok(held)
}
/// The subvolume in findmnt's source ("/dev/sdb1[/@data]" -> "/@data"; "" for the
/// filesystem's top level or another filesystem type).
fn subvolume_of(source: &str) -> &str {
    source
        .split_once('[')
        .map(|(_, rest)| rest.trim_end_matches(']').trim_end_matches('/'))
        .filter(|s| *s != "/")
        .unwrap_or("")
}
/// What the drive check expected at `path` and what findmnt found there ("UUID FSTYPE
/// SOURCE", empty when nothing is mounted), so a wrong or missing drive is named.
fn identity_mismatch(key: &str, path: &Path, uuid: &str, fstype: &str, found: &str) -> String {
    let drive = if key == "data_ssd" { "SSD" } else { "HDD" };
    let found = match found.trim() {
        "" => "nothing is mounted there".to_string(),
        seen => format!("found {seen}"),
    };
    format!(
        "Backup storage identity mismatch: {} should be the {drive} (UUID {uuid}, {fstype}); {found}",
        path.display()
    )
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
        let s =
            common::findmnt("UUID,FSTYPE,SOURCE", "--mountpoint", path, 10)?.unwrap_or_default();
        let fields = s.split_whitespace().collect::<Vec<_>>();
        if fields.len() < 3 || fields[..2] != [uuid, fstype] {
            return Err(Error::new(&identity_mismatch(key, path, uuid, fstype, &s)));
        }
        // A Btrfs drive used with a subvolume must be mounted with that one: the same
        // filesystem mounted with another holds a different tree.
        let wanted = c["storage"][key]["subvolume"].as_str().unwrap_or("");
        if subvolume_of(fields[2]) != wanted.trim_end_matches('/') {
            return Err(Error::new("Backup storage subvolume mismatch"));
        }
        ids.push(uuid);
    }
    if ids[0] == ids[1] {
        return Err(Error::new("Distinct backup filesystems required"));
    }
    Ok(())
}
/// Run inside the database container. Stopping `docker compose exec` when the dump
/// takes too long leaves pg_dump itself running there, so it gets its own time limit
/// inside, a little shorter than the 30 minutes waited for here (images without
/// `timeout` run it as before).
const DUMP_SCRIPT: &str = "if command -v timeout >/dev/null 2>&1; then exec timeout 1780 pg_dump -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" --no-owner --no-acl; else exec pg_dump -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" --no-owner --no-acl; fi";
pub fn database(compose: &Path, env: &Path, dest: &Path, apply: bool) -> Result<Option<PathBuf>> {
    database_of("immich", compose, env, dest, apply)
}
/// A logical copy (pg_dump) of `app`'s database, from the `database` service of its
/// Compose project neko-<app>, as `<dest>/<app>-<UTC stamp>.sql`.
fn database_of(
    app: &str,
    compose: &Path,
    env: &Path,
    dest: &Path,
    apply: bool,
) -> Result<Option<PathBuf>> {
    common::file(compose)?;
    common::file(env)?;
    common::safe(dest)?;
    if !apply {
        println!("Would create a timestamped logical database backup");
        return Ok(None);
    }
    common::directory(dest, 0o700)?;
    clear_stale_dumps(app, dest, common::now())?;
    let stamp = chrono::Utc::now().format("%Y%m%dT%H%M%SZ");
    let target = dest.join(format!("{app}-{stamp}.sql"));
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
            &format!("neko-{app}"),
            "-f",
            &compose.to_string_lossy(),
            "--env-file",
            &env.to_string_lossy(),
            "exec",
            "-T",
            "database",
            "sh",
            "-ec",
            DUMP_SCRIPT,
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
/// Remove `immich-<stamp>.sql.part` files a cut-off dump left (a power cut, or the job
/// stopped): each can be as big as the database. One written to in the last hour may be
/// a dump still running (they stop after 30 minutes), so it stays.
fn clear_stale_dumps(app: &str, dest: &Path, now: f64) -> Result<()> {
    for entry in fs::read_dir(common::safe(dest)?)? {
        let entry = entry?;
        let meta = fs::symlink_metadata(entry.path())?;
        let name = entry.file_name().to_string_lossy().to_string();
        if name
            .strip_suffix(".part")
            .is_some_and(|whole| is_dump(app, whole))
            && meta.is_file()
            && now - meta.mtime() as f64 > 3600.0
        {
            fs::remove_file(entry.path())?;
            println!(
                "Database: removed {}, left by a database copy that was cut off",
                entry.file_name().to_string_lossy()
            );
        }
    }
    Ok(())
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
/// sort before older copies; copies dated in the future are kept aside (never removed,
/// not counted), so they cannot push out the real ones.
pub fn prune_dumps(folder: &Path, keep: u64, newest: Option<&str>) -> Result<Vec<String>> {
    prune_dumps_of("immich", folder, keep, newest)
}
fn prune_dumps_of(
    app: &str,
    folder: &Path,
    keep: u64,
    newest: Option<&str>,
) -> Result<Vec<String>> {
    if keep == 0 || !folder.is_dir() {
        return Ok(vec![]);
    }
    let now = common::now();
    let mut dumps = vec![];
    for entry in fs::read_dir(common::safe(folder)?)? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().to_string();
        // A copy dated in the future (made while the clock was wrong) would sort as the
        // newest and push out the real ones: it is kept aside, never removed here.
        if is_dump(app, &name)
            && !dated_in_future(&name, now)
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
/// New Immich originals copied to the HDD archive, the copies checked as `check` says.
fn archive_step(data: &Path, archive: &Path, min_age: u64, check: Check) -> Result<()> {
    common::stage("copying new photos and videos to the HDD");
    let (_, checks) = archive_checked(
        &data.join("photos/immich-library"),
        archive,
        true,
        min_age,
        check,
    )?;
    save_integrity(data, "archive", &checks, check)?;
    if let Some(problem) = checks.problem("HDD archive") {
        return Err(Error::new(&problem));
    }
    Ok(())
}
/// The monthly run's first step for the HDD photo archive: every copy compared with its
/// checksum, before anything is backed up or updated. Damage stops the monthly run.
/// The caller holds the recovery lock already.
pub fn monthly_archive_check(
    cfg: &Path,
    data: &Path,
    media: &Path,
    archive_dir: &str,
    min_age: u64,
) -> Result<()> {
    guard(cfg, data, media)?;
    let _lock = common::lock_wait(
        Path::new("/run/nekoserver-photo-preservation.lock"),
        std::time::Duration::from_secs(3600),
    )?;
    guard(cfg, data, media)?;
    let _drives = (common::expect_drive(media)?, common::expect_drive(data)?);
    archive_step(
        data,
        &media.join(common::relative(archive_dir)?),
        min_age,
        Check::Full,
    )?;
    println!("Archive: compared every copy with its checksum (monthly check)");
    Ok(())
}
pub fn preservation(a: &Args) -> Result<()> {
    common::require_root()?;
    let data = common::mount_path(a.get("data")?)?;
    let media = common::mount_path(a.get("media")?)?;
    let cfg = a.path("config")?;
    guard(&cfg, &data, &media)?;
    // The recovery lock first: the monthly run holds it while it checks the archive
    // (and then waits for this lock), so taking them the other way round could deadlock.
    let _recovery = coordinate(&cfg, &data, &media)?;
    let _lock = common::lock(Path::new("/run/nekoserver-photo-preservation.lock"))?;
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
            // A drive that drops out mid-run stops the run (see common::expect_drive).
            let _drives = (common::expect_drive(&media)?, common::expect_drive(&data)?);
            let archive_dir = common::relative(&a.value("archive-dir", "photos/archive"))?;
            // Every copy is compared by checksum only in the monthly run (monthly_archive_check).
            archive_step(
                &data,
                &media.join(archive_dir),
                a.number("min-age", 600)?,
                Check::Quick,
            )?;
            guard(&cfg, &data, &media)?;
            if a.flag("db-backup") {
                guard(&cfg, &data, &media)?;
                // An update cut off while it installed (or rolled back) may have left
                // Immich's database half-migrated: not saved as the newest copy (the
                // photos above are copied anyway, they are never rolled back).
                no_unfinished_update()?;
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
                let keep = a.number("db-keep", 4)?;
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
/// The problems said together ("; "), or None when there are none.
fn joined(problems: impl Iterator<Item = String>) -> Option<String> {
    let all: Vec<String> = problems.collect();
    (!all.is_empty()).then(|| all.join("; "))
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
/// Nextcloud's server-side encryption keeps each file encrypted in the files folder and
/// its keys in `files_encryption` folders (the server's at the top, each user's in their
/// own folder). The weekly copy takes only the files, so it couldn't be decrypted after
/// losing the server: the backup is reported as incomplete, never as a good one.
const CLOUD_ENCRYPTED: &str = "Nextcloud's server-side encryption is on, and this backup copies the encrypted files without the keys needed to read them, so it can't restore them on its own. Keep the files readable (Nextcloud: Administration → Security → turn encryption off and decrypt), or keep your own backup of the whole Nextcloud data folder";
/// Whether Nextcloud's server-side encryption is (or was) in use in this files folder.
fn cloud_encrypted(cloud: &Path) -> bool {
    let keys =
        |dir: &Path| fs::symlink_metadata(dir.join("files_encryption")).is_ok_and(|m| m.is_dir());
    keys(cloud)
        || list(cloud).is_ok_and(|users| {
            users
                .iter()
                .any(|u| fs::symlink_metadata(u).is_ok_and(|m| m.is_dir()) && keys(u))
        })
}
/// The weekly USB copy. It never starts during the monthly snapshot and update, which
/// read the same HDD (the monthly run makes its own copy of the drive): it waits for
/// the shared lock, checks no monthly run is under way, and lets the lock go again.
/// Held for the whole copy (hours, for a first or big one), the lock made the nightly
/// backups wait and fail, and setup report itself busy. A monthly run that starts
/// during the copy waits for the USB drive's own lock before it copies to the drive.
/// A failure here is recorded in the job's report like any other.
pub fn weekly(a: &Args) -> Result<()> {
    let cfg = a.path("config")?;
    let data = common::mount_path(a.get("data")?)?;
    let media = common::mount_path(a.get("media-root")?)?;
    let cloud = wants_cloud_database(!a.value("cloud", "").is_empty(), &data);
    // Nextcloud's weekly database copy is made while the shared lock is held, so no
    // monthly run can stop or update Nextcloud during it. A copy that fails (Nextcloud
    // not running, say) makes the week's backup "incomplete"; its files are copied anyway.
    let made = no_monthly_run(&cfg, &data, &media, || {
        if !cloud {
            return None;
        }
        match nextcloud_dump(&cfg, &data, &media) {
            Ok(_) => None,
            Err(e) => {
                println!("Nextcloud: the database copy could not be made ({e})");
                Some(format!(
                    "the Nextcloud database copy could not be made ({e})"
                ))
            }
        }
    });
    match made {
        Ok(problem) => usb_copy(a, Check::Quick, problem),
        Err(e) => execute(Some(&data), "external", &cfg, move || Err(e)),
    }
}
/// A one-shot service at work is "activating" (never "active": that is only after it
/// stays up); stopping, its clean-up steps still run.
fn service_busy(state: &str) -> bool {
    ["active", "activating", "deactivating", "reloading"].contains(&state.trim())
}
/// Wait for the shared lock, then check (holding it) that no monthly run is under way:
/// none cut off half-way and its service not running, and do `then` (the Nextcloud
/// database copy). The lock is let go on return.
fn no_monthly_run<F: FnOnce() -> Option<String>>(
    cfg: &Path,
    data: &Path,
    media: &Path,
    then: F,
) -> Result<Option<String>> {
    let _recovery = coordinate(cfg, data, media)?;
    no_unfinished_update()?;
    for unit in [
        "nekoserver-maintenance.service",
        "nekoserver-maintenance-resume.service",
    ] {
        let (_, state) = common::probe(
            &[
                "systemctl",
                "show",
                "--property=ActiveState",
                "--value",
                unit,
            ],
            15,
        );
        if service_busy(&state) {
            return Err(Error::new(
                "The monthly update is running, so this week's USB copy did not start (the monthly run makes its own copy of the drive)",
            ));
        }
    }
    Ok(then())
}
/// How a USB copy failure that is damage (a copy no longer matching its checksum, a
/// verified file gone) begins, unlike a drive that is full or would not mount: the
/// monthly run stops only for damage.
pub(crate) const INTEGRITY: &str = "Backup copies failed their checksum check: ";
/// The monthly run's first step for the USB drive: the weekly copy, with every copy on
/// the drive compared with its checksum (the weekly runs compare size and date only).
pub fn monthly_usb_check(a: &Args) -> Result<()> {
    usb_copy(a, Check::Full, None)
}
/// `dump_problem`: why this week's Nextcloud database copy could not be made, if so.
fn usb_copy(a: &Args, check: Check, dump_problem: Option<String>) -> Result<()> {
    common::require_root()?;
    let uuid = a.get("uuid")?;
    let fstype = a.get("fstype")?;
    if !(8..=80).contains(&uuid.len())
        || !uuid.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-')
        || !["ext4", "xfs", "btrfs", "exfat"].contains(&fstype)
    {
        return Err(Error::new("Invalid external identity"));
    }
    // The photo archive (Immich) and the Nextcloud files are each optional: a server
    // with only Nextcloud still gets its weekly USB copy.
    let source = match a.value("source", "").as_str() {
        "" => None,
        value => Some(common::safe(Path::new(value))?),
    };
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
    if source.is_none() && cloud.is_none() {
        return Err(Error::new(
            "Nothing to copy: give --source, --cloud or both",
        ));
    }
    if let Some(source) = &source {
        if !source.starts_with(&media) || source == &media {
            return Err(Error::new("Archive outside media mount"));
        }
        separate(source, &dest)?;
    }
    // A weekly copy, the monthly check of the same drive and a restore from it (the
    // recovery menu holds this lock while it reads the drive) wait for each other.
    let _lock = common::lock_wait(
        Path::new("/run/nekoserver-photo-backup.lock"),
        std::time::Duration::from_secs(8 * 3600),
    )?;
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
            // From here every folder and file made on the USB drive first checks it is
            // still mounted: unplugged mid-copy, the copy stops instead of filling the
            // system disk through the empty folder it was mounted on.
            let _usb = common::expect_drive(&dest)?;
            let mut photos = None;
            if let Some(source) = &source {
                common::stage("copying photos and videos to the USB drive");
                photos = Some(mirror(source, &dest, true, check)?.1);
                validate_external(&dest, uuid)?;
                guard(&cfg, &data, &media)?;
            }
            let mut database_problem: Option<String> = None;
            if wants_database(source.is_some(), &data) {
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
                    cloud_copy = Some(copy_cloud(
                        cloud,
                        &dest,
                        true,
                        fstype == "exfat",
                        check,
                        nextcloud_internal(&data).as_deref(),
                    )?);
                    validate_external(&dest, uuid)?;
                    guard(&cfg, &data, &media)?;
                    if cloud_encrypted(cloud) {
                        println!("Nextcloud: {CLOUD_ENCRYPTED}; this backup is incomplete");
                        cloud_problem = Some(CLOUD_ENCRYPTED.to_string());
                    }
                    if wants_cloud_database(true, &data) {
                        common::stage("copying the Nextcloud database to the USB drive");
                        let copied = copy_newest_of(
                            &NEXTCLOUD_DUMPS,
                            &media.join("backups").join(NEXTCLOUD_DUMPS.folder),
                            &dest,
                        )?;
                        database_problem = joined(
                            [database_problem.take(), dump_problem.clone(), copied]
                                .into_iter()
                                .flatten(),
                        );
                        validate_external(&dest, uuid)?;
                    }
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
            if let Some(photos) = &photos {
                save_integrity(&data, "usb_photos", photos, check)?;
            }
            if let Some(cloud) = &cloud_copy {
                save_integrity(&data, "usb_nextcloud", &cloud.integrity, check)?;
            }
            // Damage found by the checksum checks fails the job, after everything else
            // was copied: nothing is lost by finishing the run first.
            let damage: Vec<String> = [
                photos.as_ref().and_then(|p| p.problem("HDD archive")),
                cloud_copy
                    .as_ref()
                    .and_then(|c| c.integrity.problem("USB Nextcloud")),
            ]
            .into_iter()
            .flatten()
            .collect();
            if !damage.is_empty() {
                return Err(Error::new(&format!("{INTEGRITY}{}", damage.join(". "))));
            }
            Ok(incomplete(
                cloud_copy.and_then(|c| c.missing()),
                database_problem,
                cloud_problem,
            ))
        })();
        // A drive that would not unmount is reported after the copy's own error, which
        // is the one that says what went wrong (and damage still reads as damage: the
        // monthly run stops only for an error that starts with INTEGRITY).
        let unmounted = if mounted_here {
            success(&["umount", &dest.to_string_lossy()], 45)
        } else {
            Ok(())
        };
        if let Err(e) = &unmounted {
            println!("USB: the drive could not be unmounted afterwards ({e})");
        }
        let partial = common::first_error(result, unmounted)?;
        if check == Check::Full {
            println!(
                "Backup: compared every copy on the USB drive with its checksum (monthly check)"
            );
        }
        Ok(Outcome { partial })
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_named_metadata_is_left_out_not_every_neko_name() {
        for name in [
            NAMES,
            ".IMG_1.jpg.neko-latest",
            "a.jpg.neko-part",
            ".neko-123-456-7",
        ] {
            assert!(metadata_name(name), "{name}");
        }
        for name in [
            ".neko-notes.txt",
            ".neko-ledger.json",
            "notes.neko-20261022T120000Z.txt",
            ".neko-1-2",
        ] {
            assert!(!metadata_name(name), "{name}");
        }
        for key in [
            ledger::LEDGER,
            ".neko-ledger.lock",
            ".neko-ledger.json.1.tmp",
            ".neko-ledger.json.unreadable-1",
        ] {
            assert!(ledger_file(key), "{key}");
        }
        assert!(!ledger_file("alice/.neko-ledger.json"));
        assert!(!ledger_file(".neko-notes.txt"));
        let root = scratch("neko-named-data");
        fs::create_dir_all(root.join("alice")).unwrap();
        for name in [
            ".neko-notes.txt",
            "alice/.neko-notes.txt",
            ".neko-ledger.lock",
            "alice/.neko-names.json",
        ] {
            fs::write(root.join(name), b"x").unwrap();
        }
        let keys: Vec<String> = tree_files(&root)
            .unwrap()
            .into_iter()
            .map(|(k, _)| k)
            .collect();
        assert_eq!(keys, vec![".neko-notes.txt", "alice/.neko-notes.txt"]);
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn a_resume_has_work_only_with_a_journal_or_a_cut_off_report() {
        let root = scratch("needs-resume");
        let (journal, report) = (root.join("resume.json"), root.join("report.json"));
        assert!(!needs_resume(&journal, &report));
        fs::write(&report, br#"{"status":"pass"}"#).unwrap();
        assert!(!needs_resume(&journal, &report));
        fs::write(&report, br#"{"status":"running"}"#).unwrap();
        assert!(needs_resume(&journal, &report));
        fs::write(&report, br#"{"status":"pass"}"#).unwrap();
        fs::write(&journal, b"{}").unwrap();
        assert!(needs_resume(&journal, &report));
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn a_file_without_an_extension_never_takes_another_files_versions() {
        let root = scratch("ext-less-versions");
        fs::create_dir_all(&root).unwrap();
        for name in [
            "README",
            "README.md",
            "README.neko-20261020T010000Z.md",
            "README.neko-20261021T010000Z",
            "README.neko-20261022T010000Z-1",
            "notes.",
            "notes.neko-20261020T010000Z.",
        ] {
            fs::write(root.join(name), b"x").unwrap();
        }
        let names = |target: &str| {
            saved_versions(&root.join(target))
                .unwrap()
                .into_iter()
                .map(|(n, _)| n)
                .collect::<Vec<_>>()
        };
        assert_eq!(
            names("README"),
            [
                "README.neko-20261021T010000Z",
                "README.neko-20261022T010000Z-1"
            ]
        );
        assert_eq!(names("README.md"), ["README.neko-20261020T010000Z.md"]);
        assert_eq!(names("notes."), ["notes.neko-20261020T010000Z."]);
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn a_dump_dated_in_the_future_never_hides_the_fresh_one() {
        let now = 1_792_900_000.0; // 2026-10-25
        let names = |v: &[&str]| v.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        let (newest, future) = newest_believable(
            names(&[
                "immich-20261018T021500Z.sql",
                "immich-20300101T021500Z.sql",
                "immich-20261025T021500Z.sql",
            ]),
            now,
        );
        assert_eq!(newest.as_deref(), Some("immich-20261025T021500Z.sql"));
        assert_eq!(future, ["immich-20300101T021500Z.sql"]);
        // Only a future-dated one: it is used, and flagged by its date.
        let (newest, future) = newest_believable(names(&["immich-20300101T021500Z.sql"]), now);
        assert_eq!(newest.as_deref(), Some("immich-20300101T021500Z.sql"));
        assert!(future.is_empty());
        // Copied end to end: the fresh dump reaches the USB drive, the 2030 one is named.
        let root = scratch("future-dump");
        let (hdd, usb) = (root.join("hdd"), root.join("usb"));
        fs::create_dir_all(&hdd).unwrap();
        fs::create_dir_all(&usb).unwrap();
        let today = chrono::Utc::now()
            .format("immich-%Y%m%dT%H%M%SZ.sql")
            .to_string();
        for name in [today.as_str(), "immich-20990101T021500Z.sql"] {
            let dump = hdd.join(name);
            fs::write(&dump, b"-- PostgreSQL database dump complete\n").unwrap();
            fs::write(
                dump_note(&dump),
                serde_json::to_vec(
                    &json!({"sha256": common::digest(&dump).unwrap(), "immich_version": "v2.1.0"}),
                )
                .unwrap(),
            )
            .unwrap();
        }
        let said = copy_newest_dump(&hdd, &usb).unwrap().unwrap();
        assert!(usb.join("immich-database").join(&today).is_file());
        assert!(!usb
            .join("immich-database/immich-20990101T021500Z.sql")
            .exists());
        assert!(
            said.contains("immich-20990101T021500Z.sql") && said.contains("dated in the future")
        );
        // Retention keeps it aside and never counts it against the real ones.
        let removed = prune_dumps(&hdd, 1, Some(&today)).unwrap();
        assert!(removed.is_empty());
        assert!(hdd.join("immich-20990101T021500Z.sql").is_file());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn a_nextcloud_only_run_never_needs_the_immich_database() {
        let data = std::env::temp_dir().join(format!("neko-wants-db-{}", std::process::id()));
        fs::create_dir_all(data.join("compose/immich")).unwrap();
        // Immich switched off keeps its Compose file: no photos selected, no database.
        fs::write(data.join("compose/immich/compose.yaml"), "services: {}\n").unwrap();
        assert!(!wants_database(false, &data));
        assert!(wants_database(true, &data));
        fs::remove_file(data.join("compose/immich/compose.yaml")).unwrap();
        assert!(!wants_database(true, &data));
        fs::remove_dir_all(&data).unwrap();
    }
    #[test]
    fn a_drive_mismatch_names_what_was_expected_and_found() {
        let data = Path::new("/srv/nekoserver");
        let wrong = identity_mismatch(
            "data_ssd",
            data,
            "aaaa-1111",
            "ext4",
            "bbbb-2222 ext4 /dev/sdc1\n",
        );
        assert_eq!(
            wrong,
            "Backup storage identity mismatch: /srv/nekoserver should be the SSD (UUID aaaa-1111, ext4); \
             found bbbb-2222 ext4 /dev/sdc1"
        );
        let media = Path::new("/mnt/nekoserver-media");
        assert!(identity_mismatch("media_hdd", media, "cccc", "xfs", "")
            .ends_with("should be the HDD (UUID cccc, xfs); nothing is mounted there"));
    }
    #[test]
    fn a_cut_off_backup_says_why() {
        let dir = std::env::temp_dir().join(format!("neko-interrupted-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let report = dir.join("applications.json");
        fs::write(
            &report,
            r#"{"job":"applications","status":"running","started_at":1}"#,
        )
        .unwrap();
        mark_interrupted(&report).unwrap();
        let row: Value = serde_json::from_str(&fs::read_to_string(&report).unwrap()).unwrap();
        assert_eq!(row["status"], "fail");
        assert_eq!(row["reason"], INTERRUPTED);
        // A finished report is left as it is.
        fs::write(&report, r#"{"job":"applications","status":"pass"}"#).unwrap();
        mark_interrupted(&report).unwrap();
        assert!(fs::read_to_string(&report).unwrap().contains("\"pass\""));
        let _ = fs::remove_dir_all(dir);
    }
    #[test]
    fn a_btrfs_subvolume_is_part_of_the_drive_identity() {
        assert_eq!(subvolume_of("/dev/sdb1[/@data]"), "/@data");
        assert_eq!(subvolume_of("/dev/sdb1[/]"), "");
        assert_eq!(subvolume_of("/dev/sdb1"), "");
        assert_eq!(subvolume_of("/dev/sdb1[/@data/]"), "/@data");
    }
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
        // Original, dated version, note of the current one, record of whose version it is.
        assert_eq!(files.len(), 4);
        let version = files
            .iter()
            .find(|n| n.contains(".neko-") && !n.ends_with(".neko-latest") && *n != NAMES)
            .unwrap();
        assert_eq!(fs::read_to_string(latest_marker(&dst)).unwrap(), *version);
        let record = common::json(&root.join("dst").join(NAMES), 64000);
        assert_eq!(record["versions"][version.as_str()], "report.docx");
        assert!(
            version.starts_with("report.neko-") && version.ends_with(".docx"),
            "{version}"
        );
        // Next week, unchanged since that version: nothing new.
        assert_eq!(
            add_only_copy(&src, &dst, true, 0o600, Check::Quick).unwrap(),
            Added::Same
        );
        assert_eq!(names(&root.join("dst")).len(), 4);
        // A dry run never writes.
        write(&src, b"v3");
        assert_eq!(
            add_only_copy(&src, &dst, false, 0o600, Check::Quick).unwrap(),
            Added::Version
        );
        assert_eq!(names(&root.join("dst")).len(), 4);
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
        // A damaged file no note names is found by the monthly full check.
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
        assert_eq!(names(&root.join("dst")).len(), 4); // with the record of its versions
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
    fn the_nextcloud_database_copy_goes_to_the_usb_drive_with_its_note() {
        let root = scratch("cloud-dump");
        let (hdd, usb, data) = (root.join("hdd"), root.join("usb"), root.join("ssd"));
        fs::create_dir_all(&usb).unwrap();
        assert!(is_dump("nextcloud", "nextcloud-20261025T030000Z.sql"));
        assert!(!is_dump("nextcloud", "immich-20261025T030000Z.sql"));
        assert!(!is_dump("immich", "nextcloud-20261025T030000Z.sql"));
        assert_eq!(
            dump_time("nextcloud-20261025T030000Z.sql"),
            dump_time("immich-20261025T030000Z.sql")
        );
        assert!(dump_time("nextcloud-20261025T030000Z.sql") > 0.0);
        // None made yet: said, with what is missing.
        let said = copy_newest_of(&NEXTCLOUD_DUMPS, &hdd, &usb)
            .unwrap()
            .unwrap();
        assert!(said.contains("no Nextcloud database copy"), "{said}");
        assert!(said.contains("calendars and contacts"), "{said}");
        let name = chrono::Utc::now()
            .format("nextcloud-%Y%m%dT%H%M%SZ.sql")
            .to_string();
        write(&hdd.join(&name), b"-- PostgreSQL database dump complete\n");
        write(
            &data.join("appdata/nextcloud/html/version.php"),
            b"<?php\n$OC_Version = array(31,0,5,1);\n$OC_VersionString = '31.0.5';\n",
        );
        assert_eq!(nextcloud_version(&data).as_deref(), Some("31.0.5"));
        common::atomic_json(
            &dump_note(&hdd.join(&name)),
            &json!({"format":"neko-nextcloud-dump-v1","nextcloud_version":"31.0.5","sha256":common::digest(&hdd.join(&name)).unwrap()}),
        )
        .unwrap();
        assert_eq!(copy_newest_of(&NEXTCLOUD_DUMPS, &hdd, &usb).unwrap(), None);
        let folder = usb.join("nextcloud-database");
        assert!(folder.join(&name).is_file());
        assert!(dump_note(&folder.join(&name)).is_file());
        let version = fs::read_to_string(folder.join("NEXTCLOUD-VERSION.txt")).unwrap();
        assert!(version.contains("31.0.5") && version.contains("Nextcloud's database"));
        // Only with Nextcloud on and installed.
        assert!(!wants_cloud_database(true, &data));
        write(&data.join("compose/nextcloud/compose.yaml"), b"{}");
        assert!(wants_cloud_database(true, &data));
        assert!(!wants_cloud_database(false, &data));
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn the_weekly_job_copies_nextcloud_s_database_while_it_holds_the_shared_lock() {
        let source = include_str!("backup.rs");
        let weekly = &source[source.find("pub fn weekly(").unwrap()..];
        let weekly = &weekly[..weekly.find("\n}\n").unwrap()];
        // Inside the closure no_monthly_run runs while it holds the lock and after it
        // checked that no monthly update is unfinished or running.
        let gate = weekly
            .find("no_monthly_run(&cfg, &data, &media, ||")
            .unwrap();
        assert!(gate < weekly.find("nextcloud_dump(&cfg, &data, &media)").unwrap());
        let check = &source[source.find("fn no_monthly_run<").unwrap()..];
        let at = |s: &str| check.find(s).unwrap();
        assert!(at("coordinate(cfg, data, media)?") < at("no_unfinished_update()?"));
        assert!(at("no_unfinished_update()?") < at("Ok(then())"));
        assert!(at("service_busy(&state)") < at("Ok(then())"));
        let copy = &source[source.find("fn usb_copy(").unwrap()..];
        assert!(copy.contains("copy_newest_of(\n                            &NEXTCLOUD_DUMPS"));
    }
    /// The newest inventory of a tree, unpacked.
    fn newest_inventory(root: &Path) -> Value {
        all_inventories(root).pop().unwrap()
    }
    #[test]
    fn the_nextcloud_inventory_lists_only_the_files_you_have_now() {
        let root = scratch("cloud-inventory");
        let cloud = root.join("cloud");
        let usb = root.join("usb");
        fs::create_dir_all(&usb).unwrap();
        for name in ["keep.txt", "gone.txt", "old-name.txt"] {
            write(&cloud.join("alice/files").join(name), name.as_bytes());
        }
        copy_cloud(&cloud, &usb, true, false, Check::Quick, None).unwrap();
        let first = newest_inventory(&usb.join("nextcloud"));
        assert_eq!(first["format"], "neko-inventory-v2");
        assert_eq!(first["files"].as_object().unwrap().len(), 3);
        // Next week: one deleted, one renamed, one edited.
        fs::remove_file(cloud.join("alice/files/gone.txt")).unwrap();
        fs::rename(
            cloud.join("alice/files/old-name.txt"),
            cloud.join("alice/files/new-name.txt"),
        )
        .unwrap();
        write(&cloud.join("alice/files/keep.txt"), b"edited");
        std::thread::sleep(std::time::Duration::from_millis(1100));
        let made = copy_cloud(&cloud, &usb, true, false, Check::Quick, None).unwrap();
        assert!(made.complete());
        let files = newest_inventory(&usb.join("nextcloud"))["files"].clone();
        let keys: Vec<&String> = files.as_object().unwrap().keys().collect();
        assert_eq!(keys, ["alice/files/keep.txt", "alice/files/new-name.txt"]);
        // The edit's saved copy holds the content you have now.
        let copy = files["alice/files/keep.txt"]["copy"].as_str().unwrap();
        assert_eq!(
            files["alice/files/keep.txt"]["sha256"],
            common::sha(b"edited")
        );
        assert!(copy.starts_with("keep.neko-"), "{copy}");
        assert_eq!(
            fs::read(usb.join("nextcloud/alice/files").join(copy)).unwrap(),
            b"edited"
        );
        assert_eq!(
            files["alice/files/new-name.txt"],
            json!({"copy": "new-name.txt", "sha256": common::sha(b"old-name.txt")})
        );
        // Everything is still kept, and the inventories are not counted as data.
        assert!(usb.join("nextcloud/alice/files/gone.txt").is_file());
        let keys: Vec<String> = tree_files(&usb.join("nextcloud"))
            .unwrap()
            .into_iter()
            .map(|(k, _)| k)
            .collect();
        assert!(keys.iter().all(|k| !k.starts_with(".neko-inventory")));
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn the_photo_inventory_travels_to_the_usb_drive_and_old_ones_go() {
        let root = scratch("photo-inventory");
        let (library, hdd, usb) = (root.join("lib"), root.join("hdd"), root.join("usb"));
        fs::create_dir_all(&usb).unwrap();
        let old = |path: &Path| {
            let then = std::time::SystemTime::now() - std::time::Duration::from_secs(3600);
            fs::File::options()
                .write(true)
                .open(path)
                .unwrap()
                .set_modified(then)
                .unwrap();
        };
        for name in ["a.jpg", "b.jpg"] {
            write(&library.join("upload/u").join(name), name.as_bytes());
            old(&library.join("upload/u").join(name));
        }
        archive(&library, &hdd, true, 60, Check::Quick).unwrap();
        fs::remove_file(library.join("upload/u/b.jpg")).unwrap();
        // A photo still being written is not backed up yet: not listed.
        write(&library.join("upload/u/new.jpg"), b"new");
        std::thread::sleep(std::time::Duration::from_millis(1100));
        archive(&library, &hdd, true, 60, Check::Quick).unwrap();
        let files = newest_inventory(&hdd)["files"].clone();
        assert_eq!(
            files,
            json!({"upload/u/a.jpg": {"copy": "a.jpg", "sha256": common::sha(b"a.jpg")}})
        );
        mirror(&hdd, &usb, true, Check::Quick).unwrap();
        assert_eq!(newest_inventory(&usb)["files"], files);
        // Only the newest KEPT stay.
        let folder = hdd.join(inventory::FOLDER);
        for day in 10..40 {
            fs::write(folder.join(format!("200001{day:02}T000000Z.json.gz")), b"x").unwrap();
        }
        inventory::prune(&folder, inventory::KEPT, "none").unwrap();
        assert_eq!(fs::read_dir(&folder).unwrap().count(), inventory::KEPT);
        assert!(!folder.join("20000110T000000Z.json.gz").exists());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn a_file_that_became_a_folder_and_back_never_blocks_the_copy() {
        let root = scratch("cloud-shape");
        let cloud = root.join("cloud");
        let usb = root.join("usb");
        fs::create_dir_all(&usb).unwrap();
        write(&cloud.join("alice/files/Projects"), b"a file");
        copy_cloud(&cloud, &usb, true, false, Check::Quick, None).unwrap();
        write(&cloud.join("alice/files/Projects"), b"a file, edited");
        copy_cloud(&cloud, &usb, true, false, Check::Quick, None).unwrap(); // a dated version too
                                                                            // The file is deleted and a folder of the same name made.
        fs::remove_file(cloud.join("alice/files/Projects")).unwrap();
        write(&cloud.join("alice/files/Projects/notes.txt"), b"notes");
        let made = copy_cloud(&cloud, &usb, true, false, Check::Quick, None).unwrap();
        assert!(made.complete());
        let user = usb.join("nextcloud/alice");
        assert_eq!(
            fs::read(user.join("files/Projects/notes.txt")).unwrap(),
            b"notes"
        );
        let history = fs::read_dir(user.join(REPLACED))
            .unwrap()
            .next()
            .unwrap()
            .unwrap()
            .path();
        let kept = names(&history);
        assert!(kept.contains(&"Projects".to_string()), "{kept:?}");
        assert!(
            kept.iter().any(|n| n.starts_with("Projects.neko-2")),
            "the version went too"
        );
        assert_eq!(fs::read(history.join("Projects")).unwrap(), b"a file");
        // The checksums moved with the files: nothing is reported missing or damaged.
        let full = copy_cloud(&cloud, &usb, true, false, Check::Full, None).unwrap();
        assert!(full.integrity.problem("USB Nextcloud").is_none());
        let ledger = Ledger::read(&usb.join("nextcloud")).unwrap();
        assert!(ledger.get("alice/files/Projects").is_none());
        assert!(ledger
            .keys()
            .any(|k| k.starts_with(&format!("alice/{REPLACED}/")) && k.ends_with("/Projects")));
        // And back: the folder becomes a file again.
        fs::remove_dir_all(cloud.join("alice/files/Projects")).unwrap();
        write(&cloud.join("alice/files/Projects"), b"a file again");
        std::thread::sleep(std::time::Duration::from_millis(1100)); // a new run's time
        let back = copy_cloud(&cloud, &usb, true, false, Check::Quick, None).unwrap();
        assert!(back.complete());
        assert_eq!(
            fs::read(user.join("files/Projects")).unwrap(),
            b"a file again"
        );
        let kept: Vec<String> = fs::read_dir(user.join(REPLACED))
            .unwrap()
            .flatten()
            .filter(|e| e.path().join("Projects/notes.txt").is_file())
            .map(|e| e.file_name().to_string_lossy().to_string())
            .collect();
        assert_eq!(kept.len(), 1, "the folder is kept aside with its file");
        let full = copy_cloud(&cloud, &usb, true, false, Check::Full, None).unwrap();
        assert!(full.integrity.problem("USB Nextcloud").is_none());
        fs::remove_dir_all(root).unwrap();
    }
    /// Every inventory of a tree, oldest first, unpacked.
    fn all_inventories(root: &Path) -> Vec<Value> {
        let mut names: Vec<String> = fs::read_dir(root.join(inventory::FOLDER))
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().to_string())
            .collect();
        names.sort();
        names
            .iter()
            .map(|n| {
                let out = Command::new("gzip")
                    .args(["-dc"])
                    .arg(root.join(inventory::FOLDER).join(n))
                    .output()
                    .unwrap();
                serde_json::from_slice(&out.stdout).unwrap()
            })
            .collect()
    }
    #[test]
    fn each_inventory_keeps_the_checksum_of_what_you_had_then() {
        // File, then folder, then file again under the same name, across three weekly
        // copies. The first file's copy is moved into neko-replaced and a new copy takes
        // its place, so only the checksum still says which content the first list meant.
        let root = scratch("cloud-inventory-history");
        let (cloud, usb) = (root.join("cloud"), root.join("usb"));
        fs::create_dir_all(&usb).unwrap();
        let projects = cloud.join("alice/files/Projects");
        let weeks: [&dyn Fn(); 3] = [
            &|| write(&projects, b"OLD CONTENT"),
            &|| {
                fs::remove_file(&projects).unwrap();
                write(&projects.join("notes.txt"), b"notes");
            },
            &|| {
                fs::remove_dir_all(&projects).unwrap();
                write(&projects, b"NEW CONTENT");
            },
        ];
        for change in weeks {
            change();
            copy_cloud(&cloud, &usb, true, false, Check::Quick, None).unwrap();
            std::thread::sleep(std::time::Duration::from_millis(1100)); // the next run's time
        }
        let tree = usb.join("nextcloud");
        let lists = all_inventories(&tree);
        assert_eq!(lists.len(), 3);
        let entry = |n: usize, key: &str| lists[n]["files"][key].clone();
        assert_eq!(lists[0]["format"], "neko-inventory-v2");
        assert_eq!(
            entry(0, "alice/files/Projects")["sha256"],
            common::sha(b"OLD CONTENT")
        );
        assert_eq!(
            entry(1, "alice/files/Projects/notes.txt")["sha256"],
            common::sha(b"notes")
        );
        assert!(entry(1, "alice/files/Projects").is_null());
        assert_eq!(
            entry(2, "alice/files/Projects")["sha256"],
            common::sha(b"NEW CONTENT")
        );
        // The same copy name points at the new content now; the old content is still
        // saved (in neko-replaced), and the checksum list knows where.
        assert_eq!(
            fs::read(tree.join("alice/files/Projects")).unwrap(),
            b"NEW CONTENT"
        );
        let ledger = Ledger::read(&tree).unwrap();
        let old: Vec<&String> = ledger
            .entries()
            .filter(|(_, e)| e.sha256 == common::sha(b"OLD CONTENT"))
            .map(|(k, _)| k)
            .collect();
        assert_eq!(old.len(), 1);
        assert!(old[0].starts_with(&format!("alice/{REPLACED}/")), "{old:?}");
        assert_eq!(fs::read(tree.join(old[0])).unwrap(), b"OLD CONTENT");
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn the_archive_and_the_usb_copy_wait_for_each_others_ledger() {
        let root = scratch("ledger-wait");
        fs::create_dir_all(&root).unwrap();
        let held = Ledger::open(&root).unwrap();
        let waiter = {
            let root = root.clone();
            std::thread::spawn(move || {
                Ledger::open_waiting(&root, std::time::Duration::from_secs(30)).is_ok()
            })
        };
        std::thread::sleep(std::time::Duration::from_secs(3)); // longer than open's 2 s
        drop(held);
        assert!(
            waiter.join().unwrap(),
            "the second job got the ledger once the first let go"
        );
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn only_nextclouds_own_unfinished_uploads_are_left_out() {
        assert!(nextcloud_upload_in_progress(
            "video.mp4.ocTransferId123456.part"
        ));
        assert!(!nextcloud_upload_in_progress("Chapter 1.part"));
        let root = scratch("cloud-part");
        let cloud = root.join("cloud");
        write(&cloud.join("alice/files/Chapter 1.part"), b"mine");
        write(
            &cloud.join("alice/files/v.mp4.ocTransferId42.part"),
            b"upload",
        );
        let usb = root.join("usb");
        fs::create_dir_all(&usb).unwrap();
        let made = copy_cloud(&cloud, &usb, true, false, Check::Quick, None).unwrap();
        assert_eq!(made.copied, 1);
        assert!(usb.join("nextcloud/alice/files/Chapter 1.part").is_file());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn a_user_named_appdata_something_is_backed_up() {
        let root = scratch("cloud-appdata-user");
        let cloud = root.join("cloud");
        write(&cloud.join("appdata_alice/files/notes.txt"), b"notes");
        write(&cloud.join("appdata_ocq1w2e3r4t5/files/x.css"), b"internal");
        let usb = root.join("usb");
        fs::create_dir_all(&usb).unwrap();
        // Known from config.php: only that folder is Nextcloud's.
        let made = copy_cloud(
            &cloud,
            &usb,
            true,
            false,
            Check::Quick,
            Some("appdata_ocq1w2e3r4t5"),
        )
        .unwrap();
        assert_eq!(made.copied, 1);
        assert!(usb
            .join("nextcloud/appdata_alice/files/notes.txt")
            .is_file());
        assert!(!usb.join("nextcloud/appdata_ocq1w2e3r4t5").exists());
        // Unknown: Nextcloud's own name form only.
        assert!(is_nextcloud_internal("appdata_ocq1w2e3r4t5", None));
        assert!(!is_nextcloud_internal("appdata_alice", None));
        assert!(!is_nextcloud_internal(
            "appdata_ocq1w2e3r4t5",
            Some("appdata_oczzzzzzzzzz")
        ));
        // The instance id is read from config.php.
        let data = root.join("ssd");
        fs::create_dir_all(data.join("appdata/nextcloud/html/config")).unwrap();
        fs::write(
            data.join("appdata/nextcloud/html/config/config.php"),
            "<?php\n$CONFIG = array (\n  'instanceid' => 'ocq1w2e3r4t5',\n);\n",
        )
        .unwrap();
        assert_eq!(
            nextcloud_internal(&data).as_deref(),
            Some("appdata_ocq1w2e3r4t5")
        );
        assert_eq!(nextcloud_internal(&root.join("none")), None);
        fs::remove_dir_all(root).unwrap();
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
        write(
            &cloud.join("appdata_oc1a2b3c4d5e/preview/1/big.png"),
            b"preview",
        );
        write(&cloud.join("bob/files/todo.txt"), b"todo");
        write(&cloud.join("nextcloud.log"), b"log");
        write(&cloud.join(".ocdata"), b"");
        let usb = root.join("usb");
        fs::create_dir_all(&usb).unwrap();
        let first = copy_cloud(&cloud, &usb, true, false, Check::Quick, None).unwrap();
        assert_eq!((first.copied, first.versions, first.skipped), (3, 0, 0));
        assert!(first.complete());
        assert_eq!(
            names(&usb.join("nextcloud")),
            [
                ".neko-inventory",
                ".neko-ledger.json",
                ".neko-ledger.lock",
                "alice",
                "bob"
            ]
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
        let second = copy_cloud(&cloud, &usb, true, false, Check::Quick, None).unwrap();
        assert_eq!((second.copied, second.versions, second.skipped), (1, 1, 0));
        // Deleted on the server: kept on the USB.
        fs::remove_file(cloud.join("bob/files/todo.txt")).unwrap();
        copy_cloud(&cloud, &usb, true, false, Check::Quick, None).unwrap();
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
        let fat = copy_cloud(&cloud, &usb, true, true, Check::Quick, None).unwrap();
        assert_eq!((fat.copied, fat.refused.len()), (1, 1));
        assert!(!fat.complete());
        assert_eq!(names(&usb.join("nextcloud/alice/files")), ["ok.txt"]);
        // On a Linux filesystem the same file is copied normally.
        let linux = copy_cloud(&cloud, &usb, true, false, Check::Quick, None).unwrap();
        assert_eq!(linux.copied, 1);
        assert!(linux.complete());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn a_name_kept_from_an_earlier_week_still_counts_on_exfat() {
        let root = scratch("case-kept");
        let cloud = root.join("cloud");
        let usb = root.join("usb");
        fs::create_dir_all(&usb).unwrap();
        write(&cloud.join("alice/files/Documents/report.pdf"), b"old");
        copy_cloud(&cloud, &usb, true, true, Check::Quick, None).unwrap();
        // The folder is removed from Nextcloud and made again in lower case: the old
        // backup stays (add-only), so the new file would merge into its folder.
        fs::remove_dir_all(cloud.join("alice/files/Documents")).unwrap();
        write(&cloud.join("alice/files/documents/report.pdf"), b"new");
        write(
            &cloud.join("alice/files/Documents/plan.pdf"),
            b"same folder, same case",
        );
        let fat = copy_cloud(&cloud, &usb, true, true, Check::Quick, None).unwrap();
        assert_eq!(fat.refused, ["alice/documents/report.pdf"]);
        assert_eq!(fat.copied, 1);
        assert_eq!(
            fs::read(usb.join("nextcloud/alice/files/Documents/report.pdf")).unwrap(),
            b"old"
        );
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn names_differing_only_in_case_are_not_merged_on_exfat() {
        let root = scratch("case");
        let cloud = root.join("cloud");
        write(&cloud.join("alice/files/Documents/report.pdf"), b"one");
        write(&cloud.join("alice/files/documents/report.pdf"), b"two");
        write(&cloud.join("alice/files/documents/other.pdf"), b"three"); // its folder clashes
        write(&cloud.join("alice/files/Photo.JPG"), b"four");
        write(&cloud.join("alice/files/photo.jpg"), b"five");
        write(&cloud.join("alice/files/Notes/a.txt"), b"six"); // no clash
        let usb = root.join("usb");
        fs::create_dir_all(&usb).unwrap();
        let fat = copy_cloud(&cloud, &usb, true, true, Check::Quick, None).unwrap();
        // The first of each clash keeps its place; the others are left out and reported.
        assert_eq!(fat.copied, 3);
        assert_eq!(
            fat.refused,
            [
                "alice/documents/other.pdf",
                "alice/documents/report.pdf",
                "alice/photo.jpg"
            ]
        );
        assert!(!fat.complete());
        assert_eq!(fat.versions, 0, "never turned into versions of one another");
        assert_eq!(
            fs::read(usb.join("nextcloud/alice/files/Documents/report.pdf")).unwrap(),
            b"one"
        );
        // Two users whose names differ only in case clash as well.
        write(&cloud.join("Bob/files/x.txt"), b"bob");
        write(&cloud.join("bob/files/y.txt"), b"other bob");
        let fat = copy_cloud(&cloud, &usb, true, true, Check::Quick, None).unwrap();
        assert!(
            fat.refused.contains(&"bob/y.txt".to_string()),
            "{:?}",
            fat.refused
        );
        // A Linux filesystem keeps them apart, so everything is copied there.
        let linux_usb = root.join("ext4");
        fs::create_dir_all(&linux_usb).unwrap();
        let linux = copy_cloud(&cloud, &linux_usb, true, false, Check::Quick, None).unwrap();
        assert!(linux.complete());
        assert_eq!(linux.copied, 8);
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn the_longest_names_can_be_backed_up_and_versioned() {
        let root = scratch("long-names");
        let usb = root.join("usb");
        fs::create_dir_all(&usb).unwrap();
        let cloud = root.join("cloud");
        // 255 bytes is the most ext4 and exFAT allow; "é" checks cutting between characters.
        let mut names = vec![];
        for len in [224, 245, 246, 255] {
            let ext = ".docx";
            let stem = "é".repeat(20) + &"n".repeat(len - ext.len() - 40);
            let name = format!("{stem}{ext}");
            assert_eq!(name.len(), len);
            write(&cloud.join("alice/files").join(&name), b"first");
            names.push(name);
        }
        let first = copy_cloud(&cloud, &usb, true, false, Check::Quick, None).unwrap();
        assert_eq!((first.copied, first.complete()), (4, true));
        let folder = usb.join("nextcloud/alice/files");
        for name in &names {
            assert_eq!(
                fs::read(folder.join(name)).unwrap(),
                b"first",
                "{}",
                name.len()
            );
        }
        // An edit becomes a dated version whose name fits, and its full name is kept.
        for name in &names {
            write(&cloud.join("alice/files").join(name), b"edited");
        }
        let edit = copy_cloud(&cloud, &usb, true, false, Check::Full, None).unwrap();
        assert_eq!((edit.versions, edit.complete()), (4, true), "{edit:?}");
        let saved = names_in(&folder);
        assert!(saved.iter().all(|n| n.len() <= NAME_MAX), "{saved:?}");
        let full = common::json(&folder.join(NAMES), 4_000_000);
        for name in &names {
            let marker = latest_marker(&folder.join(name));
            assert!(marker.file_name().unwrap().len() <= NAME_MAX);
            let current = fs::read_to_string(&marker).unwrap();
            assert_eq!(fs::read(folder.join(&current)).unwrap(), b"edited");
            let (stem, ext) = version_parts(name);
            assert!(current.starts_with(&format!("{stem}.neko-")) && current.ends_with(&ext));
            if format!("{stem}{ext}") != *name {
                assert_eq!(full["names"][&stem], json!(name));
            }
        }
        // The two longest names start alike but keep apart.
        assert_ne!(version_parts(&names[2]).0, version_parts(&names[3]).0);
        // An edit undone is still found among the shortened versions (no new copy).
        for name in &names {
            write(&cloud.join("alice/files").join(name), b"first");
        }
        let undone = copy_cloud(&cloud, &usb, true, false, Check::Full, None).unwrap();
        assert_eq!((undone.versions, undone.copied), (0, 0));
        // No partial file is left behind, whatever the name's length.
        assert!(!names_in(&folder).iter().any(|n| n.ends_with(".neko-part")));
        fs::remove_dir_all(root).unwrap();
    }
    /// The same names as neko_common.py gives (tests/test_long_names.py checks these too),
    /// so the restore finds what the backup wrote.
    #[test]
    fn long_names_are_shortened_the_same_way_as_the_restore_expects() {
        let name = "n".repeat(250) + ".pdf";
        let (stem, ext) = version_parts(&name);
        assert_eq!(
            (stem.len(), &stem[stem.len() - 12..], ext.as_str()),
            (224, "nnn~b93fc58d", ".pdf")
        );
        let marker = latest_marker(Path::new(&name));
        let marker = marker.to_string_lossy();
        assert!(marker.ends_with("nnn~b93fc58d.neko-latest") && marker.len() == 255);
        assert_eq!(partial_name(&name), ".neko-b93fc58dc2275fe3.neko-part");
        let (stem, ext) = version_parts(&("é".repeat(120) + ".txt"));
        assert_eq!((stem.len(), ext.as_str()), (223, ".txt"));
        assert!(stem.ends_with("ééé~ead92854"), "{stem}");
        // Short names are untouched.
        assert_eq!(
            version_parts("photo.jpg"),
            ("photo".to_string(), ".jpg".to_string())
        );
        assert_eq!(
            latest_marker(Path::new("photo.jpg")),
            Path::new(".photo.jpg.neko-latest")
        );
    }
    fn names_in(dir: &Path) -> Vec<String> {
        names(dir)
    }
    #[test]
    fn a_lost_hdd_ledger_never_erases_the_usb_drives_records() {
        let root = scratch("ledger-lost");
        let (src, hdd, usb) = (root.join("src"), root.join("hdd"), root.join("usb"));
        for name in ["a", "b"] {
            write(&src.join(name), format!("good {name}").as_bytes());
            keep(&src.join(name), &hdd, &format!("upload/{name}.jpg"));
        }
        mirror(&hdd, &usb, true, Check::Quick).unwrap();
        let good = Ledger::read(&usb)
            .unwrap()
            .get("upload/a.jpg")
            .unwrap()
            .clone();
        // The HDD's ledger is lost, then its copy of a.jpg is damaged.
        fs::remove_file(hdd.join(LEDGER)).unwrap();
        fs::write(hdd.join("upload/a.jpg"), b"ROT! a").unwrap();
        let (_, report) = mirror(&hdd, &usb, true, Check::Full).unwrap();
        // The USB's record still says which copy is good: the HDD one is the damaged one.
        assert_eq!(report.damaged, ["upload/a.jpg"]);
        assert!(report.disagree.is_empty());
        assert_eq!(fs::read(usb.join("upload/a.jpg")).unwrap(), b"good a");
        // Both ledgers keep the record, as it was first made.
        assert_eq!(Ledger::read(&usb).unwrap().get("upload/a.jpg"), Some(&good));
        assert_eq!(Ledger::read(&hdd).unwrap().get("upload/a.jpg"), Some(&good));
        // A run that fails part way leaves the USB ledger as it was too.
        fs::remove_file(hdd.join(LEDGER)).unwrap();
        fs::write(hdd.join("upload/z.jpg"), b"new").unwrap();
        fs::create_dir_all(usb.join("upload/z.jpg")).unwrap(); // something in the way
        assert!(mirror(&hdd, &usb, true, Check::Full).is_err());
        assert_eq!(Ledger::read(&usb).unwrap().get("upload/a.jpg"), Some(&good));
        fs::remove_dir_all(usb.join("upload/z.jpg")).unwrap();
        // Two ledgers that record different checksums decide nothing: both copies stay.
        let mut changed = Ledger::open(&hdd).unwrap();
        changed
            .adopt(
                "upload/b.jpg",
                &ledger::Entry {
                    sha256: "0".repeat(64),
                    ..good.clone()
                },
            )
            .unwrap();
        changed.save().unwrap();
        drop(changed);
        let (_, report) = mirror(&hdd, &usb, true, Check::Full).unwrap();
        assert!(
            report.disagree.contains(&"upload/b.jpg".to_string()),
            "{report:?}"
        );
        assert_ne!(
            Ledger::read(&usb)
                .unwrap()
                .get("upload/b.jpg")
                .unwrap()
                .sha256,
            "0".repeat(64)
        );
        // An unreadable USB ledger is moved aside, never written over.
        fs::write(usb.join(LEDGER), b"{broken").unwrap();
        mirror(&hdd, &usb, true, Check::Quick).unwrap();
        assert!(names(&usb)
            .iter()
            .any(|n| n.starts_with(".neko-ledger.json.unreadable-")));
        assert!(Ledger::read(&usb).is_ok());
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn nextcloud_encryption_is_noticed_and_makes_the_backup_incomplete() {
        let root = scratch("nc-encryption");
        let cloud = root.join("cloud");
        write(
            &cloud.join("alice/files/report.pdf"),
            b"HBEGIN:oc_encryption_module:OC_DEFAULT_MODULE",
        );
        write(&cloud.join("appdata_x/preview/1.png"), b"png");
        assert!(!cloud_encrypted(&cloud));
        // A user's own keys (per-user keys), or the server's (master key).
        fs::create_dir_all(cloud.join("alice/files_encryption/keys")).unwrap();
        assert!(cloud_encrypted(&cloud));
        fs::remove_dir_all(cloud.join("alice/files_encryption")).unwrap();
        fs::create_dir_all(cloud.join("files_encryption/OC_DEFAULT_MODULE")).unwrap();
        assert!(cloud_encrypted(&cloud));
        // The job's report says so, in the words the dashboard shows.
        let report = incomplete(None, None, Some(CLOUD_ENCRYPTED.to_string())).unwrap();
        assert_eq!(report["reason"], "The USB backup is incomplete");
        assert!(report["source"]
            .as_str()
            .unwrap()
            .contains("without the keys"));
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
    #[test]
    fn a_backup_never_copies_into_itself_or_a_parent() {
        let root = scratch("separate");
        let source = root.join("hdd/archive");
        fs::create_dir_all(source.join("inner")).unwrap();
        fs::create_dir_all(root.join("usb")).unwrap();
        let (s, d) = separate(&source, &root.join("usb/archive")).unwrap();
        assert!(s.is_absolute() && d.ends_with("usb/archive"));
        for dest in [
            source.clone(),
            source.join("inner"),
            root.join("hdd"),
            root.clone(),
        ] {
            let error = separate(&source, &dest).unwrap_err().to_string();
            assert!(error.contains("overlap"), "{}: {error}", dest.display());
        }
        write(&root.join("usb/file"), b"not a folder");
        let error = separate(&source, &root.join("usb/file"))
            .unwrap_err()
            .to_string();
        assert!(error.contains("must be a directory"), "{error}");
        let error = separate(&root.join("missing"), &root.join("usb"))
            .unwrap_err()
            .to_string();
        assert!(error.contains("Source directory missing"), "{error}");
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn a_dry_run_says_which_copies_it_would_make() {
        let root = scratch("dry-run");
        let (src, dst) = (root.join("a.txt"), root.join("out/a.txt"));
        write(&src, b"content");
        assert!(
            verified_copy(&src, &dst, false, 0o644).unwrap(),
            "would be made"
        );
        assert!(!root.join("out").exists(), "a dry run writes nothing");
        assert!(verified_copy(&src, &dst, true, 0o644).unwrap());
        assert!(
            !verified_copy(&src, &dst, false, 0o644).unwrap(),
            "already there"
        );
        assert!(!verified_copy(&src, &dst, true, 0o644).unwrap());
        // Another file in the way, or a folder, is a collision: nothing is overwritten.
        write(&root.join("out/b.txt"), b"theirs");
        write(&root.join("b.txt"), b"ours");
        let error = copy_verified(&root.join("b.txt"), &root.join("out/b.txt"), true, 0o644)
            .unwrap_err()
            .to_string();
        assert!(error.contains("Collision"), "{error}");
        assert_eq!(fs::read(root.join("out/b.txt")).unwrap(), b"theirs");
        fs::create_dir_all(root.join("out/c.txt")).unwrap();
        write(&root.join("c.txt"), b"ours");
        let error = copy_verified(&root.join("c.txt"), &root.join("out/c.txt"), true, 0o644)
            .unwrap_err()
            .to_string();
        assert!(error.contains("Collision"), "{error}");
        // A dry run of a file not backed up yet says it would be new, and writes nothing.
        write(&root.join("d.txt"), b"new");
        let kept = kept_copy(
            &root.join("d.txt"),
            &root.join("out/d.txt"),
            false,
            0o644,
            Check::Quick,
            None,
        )
        .unwrap();
        assert_eq!(kept.added, Added::New);
        assert!(!root.join("out/d.txt").exists());
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn private_copies_get_a_private_folder() {
        let root = scratch("private-folder");
        write(&root.join("key"), b"secret");
        write(&root.join("photo.jpg"), b"public");
        copy_verified(&root.join("key"), &root.join("secrets/key"), true, 0o600).unwrap();
        copy_verified(
            &root.join("photo.jpg"),
            &root.join("shared/photo.jpg"),
            true,
            0o644,
        )
        .unwrap();
        let mode = |p: &str| fs::metadata(root.join(p)).unwrap().mode() & 0o777;
        assert_eq!((mode("secrets"), mode("secrets/key")), (0o700, 0o600));
        assert_eq!((mode("shared"), mode("shared/photo.jpg")), (0o755, 0o644));
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn a_partial_file_that_is_not_a_file_is_refused() {
        let root = scratch("partial");
        write(&root.join("a.mov"), b"video");
        let partial = root.join("out").join(partial_name("a.mov"));
        fs::create_dir_all(&partial).unwrap();
        let error = copy_verified(&root.join("a.mov"), &root.join("out/a.mov"), true, 0o644)
            .unwrap_err()
            .to_string();
        assert!(error.contains("Unsafe partial file"), "{error}");
        assert!(partial.is_dir(), "left for checking");
        assert!(!root.join("out/a.mov").exists());
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn dry_runs_write_no_names_and_versions_accumulate() {
        let root = scratch("names");
        let name = "a very long name.txt";
        remember_name(&root, "short", name, false).unwrap();
        remember_version(&root, "short.neko-1", name, false).unwrap();
        assert!(!root.join(NAMES).exists(), "a dry run writes nothing");
        remember_name(&root, "short", name, true).unwrap();
        remember_version(&root, "short.neko-1", name, true).unwrap();
        remember_version(&root, "short.neko-2", name, true).unwrap();
        let saved = common::json(&root.join(NAMES), 1_000_000);
        assert_eq!(saved["names"]["short"], name);
        assert_eq!(saved["versions"]["short.neko-1"], name);
        assert_eq!(
            saved["versions"]["short.neko-2"], name,
            "the first version is kept"
        );
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn changes_within_one_second_get_numbered_versions() {
        let root = scratch("same-second");
        let (src, dst) = (root.join("notes.txt"), root.join("out/notes.txt"));
        for body in [b"one".as_slice(), b"two", b"three", b"four"] {
            write(&src, body);
            kept_copy(&src, &dst, true, 0o644, Check::Full, None).unwrap();
        }
        let versions: Vec<String> = names(&root.join("out"))
            .into_iter()
            .filter(|n| n.contains(".neko-") && !n.starts_with('.'))
            .collect();
        assert_eq!(versions.len(), 3, "{versions:?}");
        let dated = regex::Regex::new(r"^notes\.neko-\d{8}T\d{6}Z(-[1-9]\d*)?\.txt$").unwrap();
        for name in &versions {
            assert!(dated.is_match(name), "{name}");
        }
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn the_problem_text_lists_ten_files_and_counts_the_rest() {
        let mut report = Integrity::default();
        assert!(report.problem("archive").is_none());
        report.damaged = (1..=10).map(|n| format!("photo{n}.jpg")).collect();
        let text = report.problem("archive").unwrap();
        assert!(
            text.starts_with("10 archive files no longer match"),
            "{text}"
        );
        assert!(
            text.ends_with("photo10.jpg") && !text.contains("more"),
            "{text}"
        );
        report.damaged.push("photo11.jpg".into());
        let text = report.problem("archive").unwrap();
        assert!(text.ends_with("photo10.jpg and 1 more"), "{text}");
        assert!(!text.contains("photo11"));
    }
    #[test]
    fn backup_bookkeeping_is_not_part_of_the_tree() {
        let root = scratch("tree-files");
        write(&root.join("upload/photo.jpg"), b"photo");
        write(&root.join(LEDGER), b"{}");
        write(&root.join(".neko-ledger.lock"), b"");
        write(&root.join(".neko-ledger.json.123.tmp"), b"");
        write(&root.join(NAMES), b"{}");
        write(&latest_marker(&root.join("upload/photo.jpg")), b"");
        write(
            &root.join("upload").join(partial_name("photo.jpg")),
            b"half",
        );
        write(
            &root
                .join(QUARANTINE)
                .join("20260101T000000Z/upload/photo.jpg"),
            b"damaged",
        );
        let keys: Vec<String> = tree_files(&root)
            .unwrap()
            .into_iter()
            .map(|(k, _)| k)
            .collect();
        assert_eq!(keys, ["upload/photo.jpg"]);
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn an_audit_counts_every_file_without_a_checksum() {
        let root = scratch("audit-count");
        write(&root.join("upload/a.jpg"), b"a");
        write(&root.join("upload/b.jpg"), b"b");
        let ledger = Ledger::open(&root).unwrap();
        let mut report = Integrity::default();
        audit(&root, &ledger, &mut report).unwrap();
        assert_eq!(report.unverified, 2);
        assert!(report.damaged.is_empty() && report.missing.is_empty());
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn a_name_kept_only_on_the_usb_drive_still_counts_on_exfat() {
        let root = scratch("case-kept-only");
        let cloud = root.join("cloud");
        let usb = root.join("usb");
        fs::create_dir_all(&usb).unwrap();
        write(&cloud.join("alice/files/Documents/report.pdf"), b"old");
        copy_cloud(&cloud, &usb, true, true, Check::Quick, None).unwrap();
        // The folder is renamed to lower case in Nextcloud and nothing of the old
        // spelling is left there: only the USB drive (add-only) still has it, and that
        // alone must keep the new file out of a merged folder.
        fs::remove_dir_all(cloud.join("alice/files/Documents")).unwrap();
        write(&cloud.join("alice/files/documents/report.pdf"), b"new");
        let fat = copy_cloud(&cloud, &usb, true, true, Check::Quick, None).unwrap();
        assert_eq!(fat.refused, ["alice/documents/report.pdf"]);
        assert_eq!(fat.copied, 0);
        assert_eq!(
            fs::read(usb.join("nextcloud/alice/files/Documents/report.pdf")).unwrap(),
            b"old"
        );
        assert!(!usb.join("nextcloud/alice/files/documents").exists());
        fs::remove_dir_all(root).unwrap();
    }
    fn dated(path: &Path, when: std::time::SystemTime) {
        fs::File::options()
            .write(true)
            .open(path)
            .unwrap()
            .set_modified(when)
            .unwrap();
    }
    const DAY: Duration = Duration::from_secs(86400);
    #[test]
    fn a_copy_dated_in_the_future_is_compared_not_trusted() {
        let root = scratch("future-copy");
        let (src, dst) = (root.join("src/a.jpg"), root.join("dst/a.jpg"));
        write(&src, b"photo");
        add_only_copy(&src, &dst, true, 0o644, Check::Quick).unwrap();
        // The copy was written while the clock was a day ahead; then the photo changes
        // (same size). Its date would say it was copied after the change.
        dated(&dst, std::time::SystemTime::now() + DAY);
        thread::sleep(Duration::from_millis(20));
        write(&src, b"PHOTO");
        assert_eq!(
            add_only_copy(&src, &dst, true, 0o644, Check::Quick).unwrap(),
            Added::Version
        );
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn a_photo_dated_ahead_is_archived_and_a_fresh_one_waits() {
        let root = scratch("future-photo");
        let (immich, archive) = (root.join("immich"), root.join("archive"));
        let now = std::time::SystemTime::now();
        for (name, when) in [
            ("ahead.jpg", now + DAY * 30),
            ("old.jpg", now - DAY),
            ("fresh.jpg", now),
        ] {
            write(&immich.join("upload/u").join(name), name.as_bytes());
            dated(&immich.join("upload/u").join(name), when);
        }
        let (copied, report) = archive_checked(&immich, &archive, true, 600, Check::Quick).unwrap();
        assert_eq!(copied, 2);
        assert!(report.problem("HDD archive").is_none());
        assert!(archive.join("upload/u/ahead.jpg").is_file());
        assert!(archive.join("upload/u/old.jpg").is_file());
        assert!(!archive.join("upload/u/fresh.jpg").exists());
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn a_file_or_folder_removed_during_the_walk_is_passed_over() {
        let root = scratch("vanished");
        write(&root.join("here.jpg"), b"x");
        let mut seen = vec![];
        walk_listed(
            vec![
                root.join("deleted.jpg"),
                root.join("renamed-folder"),
                root.join("here.jpg"),
            ],
            &mut |p| {
                seen.push(p.to_path_buf());
                Ok(())
            },
        )
        .unwrap();
        assert_eq!(seen, [root.join("here.jpg")]);
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn a_file_moved_aside_is_saved_in_the_checksum_list_at_once() {
        let root = scratch("aside-saved");
        let nc = root.join("nextcloud");
        write(&root.join("src/Projects"), b"a file");
        keep(&root.join("src/Projects"), &nc, "alice/files/Projects");
        let mut ledger = Ledger::open(&nc).unwrap();
        let history = nc.join("alice").join(REPLACED).join("T");
        move_aside(
            &nc.join("alice/files/Projects"),
            &nc.join("alice/files"),
            &history,
            &mut ledger,
            &nc,
        )
        .unwrap();
        // The run stops before its end (the list is not saved there): the move is kept.
        drop(ledger);
        let saved = Ledger::read(&nc).unwrap();
        assert!(saved.get("alice/files/Projects").is_none());
        assert!(saved.get(&format!("alice/{REPLACED}/T/Projects")).is_some());
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn a_name_that_is_not_text_is_left_out_not_fatal() {
        use std::os::unix::ffi::OsStrExt;
        let root = scratch("not-utf8");
        let (cloud, usb) = (root.join("cloud"), root.join("usb"));
        fs::create_dir_all(&usb).unwrap();
        write(&cloud.join("alice/files/ok.txt"), b"fine");
        let odd = std::ffi::OsStr::from_bytes(b"caf\xe9.txt");
        write(&cloud.join("alice/files").join(odd), b"latin-1 name");
        let made = copy_cloud(&cloud, &usb, true, false, Check::Quick, None).unwrap();
        assert_eq!(made.copied, 1);
        assert_eq!(made.refused, ["alice/caf\u{fffd}.txt"]);
        assert!(!made.complete());
        assert!(usb.join("nextcloud/alice/files/ok.txt").is_file());
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn a_full_drive_removes_the_oldest_snapshots_but_keeps_the_newest() {
        let root = scratch("make-room");
        for name in ["snapshot-100-1", "snapshot-200-1", "snapshot-300-1"] {
            fs::create_dir_all(root.join(name)).unwrap();
            fs::set_permissions(root.join(name), fs::Permissions::from_mode(0o700)).unwrap();
            fs::write(root.join(name).join("manifest.json"), b"{}").unwrap();
        }
        // Each snapshot takes 40 of the drive's 130.
        let count = || common::complete_snapshots(&root).unwrap().len() as u128;
        let free = || Ok(130 - 40 * count());
        let short = |have: u128| format!("short: {have} free");
        make_room_with(&root, 50, &free, &short).unwrap();
        assert_eq!(names(&root), ["snapshot-200-1", "snapshot-300-1"]);
        let error = make_room_with(&root, 100, &free, &short).unwrap_err();
        assert_eq!(error.to_string(), "short: 90 free");
        assert_eq!(names(&root), ["snapshot-300-1"]);
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn a_failed_runs_half_made_snapshot_is_removed_at_the_next_start() {
        let root = scratch("stale-stage");
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
        let stale = root.join(".snapshot-10-1.partial");
        fs::create_dir_all(stale.join("payload")).unwrap();
        fs::set_permissions(&stale, fs::Permissions::from_mode(0o700)).unwrap();
        fs::write(stale.join("payload/big.db"), b"a whole snapshot").unwrap();
        let stage = stage_snapshot(&root).unwrap();
        assert!(!stale.exists());
        assert_eq!(names(&root), [format!(".{}.partial", stage.name)]);
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn a_cut_off_database_dump_is_cleared_but_a_running_one_is_not() {
        let root = scratch("stale-dump");
        let old = root.join("immich-20260101T000000Z.sql.part");
        let running = root.join("immich-20260102T000000Z.sql.part");
        for path in [&old, &running] {
            write(path, b"partial");
        }
        write(&root.join("immich-20260101T000000Z.sql"), b"kept");
        write(&root.join("mine.sql.part"), b"kept");
        dated(
            &old,
            std::time::SystemTime::now() - Duration::from_secs(7200),
        );
        clear_stale_dumps("immich", &root, common::now()).unwrap();
        assert_eq!(
            names(&root),
            [
                "immich-20260101T000000Z.sql",
                "immich-20260102T000000Z.sql.part",
                "mine.sql.part"
            ]
        );
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn the_database_dump_has_its_own_time_limit_inside_the_container() {
        let root = scratch("dump-script");
        let fake = |name: &str, body: &str| {
            write(&root.join(name), format!("#!/bin/sh\n{body}\n").as_bytes());
            fs::set_permissions(root.join(name), fs::Permissions::from_mode(0o755)).unwrap();
        };
        fake("pg_dump", "echo pg_dump \"$@\"");
        let path = format!("{}", root.display());
        let run = || {
            let out = Command::new("/bin/sh")
                .args(["-ec", DUMP_SCRIPT])
                .env("PATH", &path)
                .env("POSTGRES_USER", "u")
                .env("POSTGRES_DB", "d")
                .output()
                .unwrap();
            String::from_utf8_lossy(&out.stdout).trim().to_string()
        };
        // Without `timeout` in the image the dump runs as before.
        assert_eq!(run(), "pg_dump -U u -d d --no-owner --no-acl");
        fake("timeout", "echo timeout \"$@\"");
        assert_eq!(run(), "timeout 1780 pg_dump -U u -d d --no-owner --no-acl");
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn only_an_update_cut_off_half_way_holds_the_nightly_backups() {
        let root = scratch("unfinished-update");
        let journal = root.join("maintenance-resume.json");
        assert!(no_unfinished_update_at(&journal).is_ok());
        for (phase, waits) in [
            ("snapshot", false),
            ("backed_up", false),
            ("committed", false),
            ("updating", true),
            ("restoring", true),
        ] {
            fs::write(&journal, json!({"phase": phase}).to_string()).unwrap();
            assert_eq!(no_unfinished_update_at(&journal).is_err(), waits, "{phase}");
        }
        fs::write(&journal, b"{not json").unwrap();
        let error = no_unfinished_update_at(&journal).unwrap_err().to_string();
        assert!(error.contains("could not be read"), "{error}");
        // The settings backup is never held back by it.
        let host = include_str!("host_snapshot.rs");
        assert!(!host.contains(&["backup::no_unfinished", "_update()"].concat()));
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn damage_found_on_the_usb_drive_still_reads_as_damage_if_unmounting_fails() {
        let copy: Result<()> = Err(Error::new(&format!("{INTEGRITY}2 HDD archive files")));
        let failed = common::first_error(copy, Err(Error::new("umount failed")));
        let said = failed.unwrap_err().to_string();
        assert!(said.starts_with(INTEGRITY), "{said}");
        assert!(said.contains("umount failed"), "{said}");
        let source = include_str!("backup.rs");
        let body = &source[source.find(&["fn usb", "_copy("].concat()).unwrap()..];
        assert!(body.contains("common::first_error(result, unmounted)"));
    }
    #[test]
    fn the_weekly_copy_lets_the_shared_lock_go_before_copying() {
        let source = include_str!("backup.rs");
        let weekly = &source[source.find(&["pub fn ", "weekly("].concat()).unwrap()..];
        let weekly = &weekly[..weekly.find("\n}\n").unwrap()];
        // The lock is held only inside no_monthly_run, and a failure there is reported.
        assert!(!weekly.contains("let _recovery"));
        assert!(weekly.contains("execute(Some(&data), \"external\""));
        let check = &source[source.find(&["fn usb", "_copy("].concat()).unwrap()..];
        assert!(!check[..check.find("\n}\n").unwrap()].contains("coordinate("));
        for (state, busy) in [
            ("activating\n", true),
            ("active", true),
            ("deactivating", true),
            ("inactive\n", false),
            ("failed", false),
            ("", false),
        ] {
            assert_eq!(service_busy(state), busy, "{state}");
        }
    }
    #[test]
    fn a_clock_put_back_keeps_the_last_success() {
        let now = 1_800_000_000.0;
        let row = running_row("archive", now, &json!({"last_success": now + 86400.0}));
        assert_eq!(row["last_success"], now + 86400.0);
        assert!(running_row("archive", now, &json!({}))["last_success"].is_null());
    }

    fn bytes_read() -> u64 {
        let io = fs::read_to_string("/proc/thread-self/io").unwrap();
        io.lines()
            .find_map(|l| l.strip_prefix("rchar: "))
            .unwrap()
            .trim()
            .parse()
            .unwrap()
    }
    #[test]
    fn the_monthly_check_reads_each_hdd_copy_once_and_still_sees_a_change() {
        // Measured on the release: the monthly check read the HDD archive three times
        // (archive step, its audit, the USB check), which on a big library took the night.
        let root = scratch("read-once");
        let (ssd, hdd, usb) = (root.join("ssd"), root.join("hdd/archive"), root.join("usb"));
        let size = 4usize << 20;
        let old = std::time::SystemTime::now() - Duration::from_secs(3600);
        for i in 0..6 {
            let p = ssd.join(format!("upload/u/{i}.jpg"));
            write(&p, &vec![i as u8; size]);
            fs::File::options()
                .write(true)
                .open(&p)
                .unwrap()
                .set_modified(old)
                .unwrap();
        }
        fs::create_dir_all(&usb).unwrap();
        archive_checked(&ssd, &hdd, true, 600, Check::Quick).unwrap();
        mirror(&hdd, &usb, true, Check::Quick).unwrap();
        let payload = (6 * size) as u64;
        let once = read_once();
        let before = bytes_read();
        archive_checked(&ssd, &hdd, true, 600, Check::Full).unwrap();
        let archived = bytes_read();
        // A copy on the HDD that changes after the archive step is read again (its times
        // differ), so the USB check still finds it damaged.
        let changed = hdd.join("upload/u/3.jpg");
        let at = fs::metadata(&changed).unwrap().modified().unwrap();
        fs::write(&changed, vec![9u8; size]).unwrap();
        fs::File::options()
            .write(true)
            .open(&changed)
            .unwrap()
            .set_modified(at)
            .unwrap();
        let (_, report) = mirror(&hdd, &usb, true, Check::Full).unwrap();
        let mirrored = bytes_read();
        drop(once);
        // Archive step: the SSD original and the HDD copy once each (the audit reads
        // nothing more). USB check: the USB copies, plus the one HDD copy that changed.
        let slack = payload / 4;
        assert!(
            archived - before <= 2 * payload + slack,
            "archive read {}",
            archived - before
        );
        assert!(
            mirrored - archived <= payload + size as u64 + slack,
            "mirror read {}",
            mirrored - archived
        );
        assert_eq!(report.damaged, vec!["upload/u/3.jpg".to_string()]);
        // Outside the monthly check nothing is kept: every read is a real one.
        assert!(READ_ONCE.lock().unwrap().is_none());
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn the_immich_database_copy_waits_for_an_unfinished_update() {
        let source = include_str!("backup.rs");
        let body = &source[source.find(&["pub fn ", "preservation("].concat()).unwrap()..];
        let at = |s: &str| body.find(s).unwrap_or_else(|| panic!("{s}"));
        assert!(at("no_unfinished_update()?") < at("database(\n"));
        // The photos are copied first either way.
        assert!(at("archive_step(") < at("no_unfinished_update()?"));
    }
}
