use crate::{
    common::{self, Error, Result},
    Args,
};
use serde_json::{json, Value};
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
fn same(a: &Metadata, b: &Metadata) -> bool {
    a.dev() == b.dev()
        && a.ino() == b.ino()
        && a.len() == b.len()
        && a.mtime() == b.mtime()
        && a.mtime_nsec() == b.mtime_nsec()
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
        .as_f64()
        .unwrap_or(0.0);
    common::now() - last >= FULL_CHECK_EVERY
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
pub(crate) fn verified_copy(source: &Path, target: &Path, apply: bool, mode: u32) -> Result<bool> {
    common::safe(source)?;
    common::safe(target)?;
    let before = common::file(source)?.metadata()?;
    if target.exists() {
        if !target.is_file() || common::digest(target)? != common::digest(source)? {
            return Err(Error::new("Collision or changed original; no overwrite"));
        }
        return Ok(false);
    }
    if !apply {
        return Ok(true);
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
        let mut buf = [0u8; 65536];
        loop {
            let n = input.read(&mut buf)?;
            if n == 0 {
                break;
            }
            out.write_all(&buf[..n])?;
        }
        out.sync_all()?;
        drop(out);
        if !same(&before, &input.metadata()?)
            || !same(&before, &fs::metadata(source)?)
            || common::digest(&temp)? != common::digest(source)?
            || !same(&before, &fs::metadata(source)?)
        {
            return Err(Error::new("Source changed or verification failed"));
        }
        common::publish(&temp, target)?;
        Ok(true)
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
pub(crate) fn add_only_copy(
    source: &Path,
    target: &Path,
    apply: bool,
    mode: u32,
    check: Check,
) -> Result<Added> {
    if !target.exists() {
        return Ok(if verified_copy(source, target, apply, mode)? {
            Added::New
        } else {
            Added::Same
        });
    }
    if !target.is_file() {
        return Err(Error::new("Backup target is not a regular file"));
    }
    let source_meta = fs::metadata(source)?;
    if check == Check::Quick && copied_since_change(&source_meta, &fs::metadata(target)?) {
        return Ok(Added::Same);
    }
    let wanted = common::digest(source)?;
    if common::digest(target)? == wanted {
        return Ok(Added::Same);
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
    for entry in fs::read_dir(parent)? {
        let path = entry?.path();
        let other = path
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .to_string();
        if other.starts_with(&format!("{stem}.neko-"))
            && other.ends_with(&ext)
            && path.is_file()
            && ((check == Check::Quick && copied_since_change(&source_meta, &fs::metadata(&path)?))
                || common::digest(&path)? == wanted)
        {
            return Ok(Added::Same);
        }
    }
    let modified = source_meta.mtime();
    let stamp = chrono::DateTime::from_timestamp(modified, 0)
        .map(|d| d.format("%Y%m%dT%H%M%SZ").to_string())
        .unwrap_or_else(|| modified.to_string());
    let mut versioned = version_name(target, &stamp);
    let mut n = 1;
    while versioned.exists() {
        versioned = version_name(target, &format!("{stamp}-{n}"));
        n += 1;
    }
    verified_copy(source, &versioned, apply, mode)?;
    Ok(Added::Version)
}
fn media_file(path: &Path) -> bool {
    let ext = path
        .extension()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    [
        "jpg", "jpeg", "png", "heic", "heif", "webp", "gif", "tif", "tiff", "bmp", "dng", "cr2",
        "cr3", "nef", "arw", "raf", "rw2", "orf", "pef", "srw", "avif", "jxl", "raw", "nrw", "k25",
        "kdc", "3fr", "fff", "mrw", "mos", "x3f", "sr2", "srf", "rwl", "iiq", "erf", "hif", "xmp",
        "mov", "mp4", "m4v", "3gp", "3gpp", "avi", "mkv", "webm", "mpg", "mpeg", "mts", "m2ts",
        "wmv", "flv", "vob", "ts", "insv",
    ]
    .contains(&ext.as_str())
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
pub fn archive(source: &Path, archive: &Path, apply: bool, age: u64, check: Check) -> Result<u64> {
    if age < 60 {
        return Err(Error::new("Archive minimum age is 60 seconds"));
    }
    let (source, archive) = separate(source, archive)?;
    let mut copied = 0;
    let mut versions = 0;
    let now = common::now();
    for section in ["upload", "library"] {
        let root = source.join(section);
        common::safe(&root)?;
        if !root.exists() {
            continue;
        }
        if !root.is_dir() {
            return Err(Error::new("Immich section must be a directory"));
        }
        walk(&root, &mut |path| {
            if !media_file(path) {
                return Ok(());
            }
            let meta = fs::metadata(path)?;
            let modified = meta.mtime() as f64 + meta.mtime_nsec() as f64 / 1e9;
            if now - modified < age as f64 {
                return Ok(());
            }
            let target = archive.join(
                path.strip_prefix(&source)
                    .map_err(|_| Error::new("Invalid archive relative path"))?,
            );
            match add_only_copy(path, &target, apply, 0o644, check)? {
                Added::New => copied += 1,
                Added::Version => versions += 1,
                Added::Same => {}
            }
            Ok(())
        })?;
    }
    if versions > 0 {
        println!("Archive: {versions} changed files kept as dated versions");
    }
    println!(
        "Archive: {copied} new verified originals{}",
        if apply { "" } else { " identified (dry run)" }
    );
    Ok(copied)
}
pub fn copy_tree(source: &Path, dest: &Path, apply: bool, check: Check) -> Result<u64> {
    let (source, dest) = separate(source, dest)?;
    let mut copied = 0;
    let mut versions = 0;
    walk(&source, &mut |path| {
        let name = path.file_name().unwrap_or_default().to_string_lossy();
        if name.ends_with(".neko-part") || name.ends_with(".sql.part") {
            return Ok(());
        }
        let target = dest.join(
            path.strip_prefix(&source)
                .map_err(|_| Error::new("Invalid relative path"))?,
        );
        match add_only_copy(path, &target, apply, 0o644, check)? {
            Added::New => copied += 1,
            Added::Version => versions += 1,
            Added::Same => {}
        }
        Ok(())
    })?;
    if versions > 0 {
        println!("Backup: {versions} changed files kept as dated versions");
    }
    println!(
        "Backup: {copied} new verified files{}",
        if apply { "" } else { " identified (dry run)" }
    );
    Ok(copied)
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
            match add_only_copy(path, &target, apply, 0o600, check) {
                Ok(Added::New) => copied += 1,
                Ok(Added::Version) => versions += 1,
                Ok(Added::Same) => {}
                Err(e) if e.to_string().contains("changed") || !path.exists() => {
                    skipped += 1;
                    skipped_files.push(format!("{user}/{}", relative.display()));
                }
                Err(e) => return Err(e),
            }
            Ok(())
        })?;
    }
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
    })
}
/// Weekly Immich database copies kept on the USB drive (newest first; older ones go).
const USB_DUMPS: u64 = 4;
/// Put the newest weekly Immich database copy on the USB drive, with the Immich
/// version that made it, so the USB alone can rebuild albums, faces and users as well
/// as the photos. Only the version is recorded, never the database password.
pub fn copy_newest_dump(hdd_dumps: &Path, usb: &Path, immich_env: &Path) -> Result<Option<String>> {
    let newest = fs::read_dir(hdd_dumps)
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.file_name().to_string_lossy().to_string())
        .filter(|n| immich_dump(n))
        .max();
    let Some(name) = newest else {
        return Ok(None);
    };
    let folder = usb.join("immich-database");
    common::directory(&folder, 0o700)?;
    let target = folder.join(&name);
    if !target.exists() {
        verified_copy(&hdd_dumps.join(&name), &target, true, 0o600)?;
    }
    let version = fs::read_to_string(immich_env)
        .ok()
        .and_then(|env| {
            env.lines()
                .find_map(|l| l.strip_prefix("IMMICH_VERSION="))
                .map(str::to_string)
        })
        .filter(|v| regex::Regex::new(r"^v\d+\.\d+\.\d+$").unwrap().is_match(v))
        .unwrap_or_else(|| "unknown".into());
    common::atomic(
        &folder.join("IMMICH-VERSION.txt"),
        format!("Immich version that made these database copies: {version}\nTo rebuild Immich from this USB drive, see RECOVERY.md, \"Rebuild Immich from the USB drive\".\n").as_bytes(),
        0o600,
    )?;
    prune_dumps(&folder, USB_DUMPS)?;
    println!("USB: Immich database copy {name} (Immich {version})");
    Ok(Some(name))
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
        common::publish(&temp, &target)?;
        Ok(Some(target.clone()))
    })();
    if result.is_err() {
        let _ = fs::remove_file(temp);
    }
    result
}
/// Keep the newest `keep` `immich-<UTC stamp>.sql` dumps in `folder` (0 keeps all).
/// The stamp sorts by time; other files are never touched.
pub fn prune_dumps(folder: &Path, keep: u64) -> Result<Vec<String>> {
    if keep == 0 || !folder.is_dir() {
        return Ok(vec![]);
    }
    let mut dumps = vec![];
    for entry in fs::read_dir(common::safe(folder)?)? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().to_string();
        if immich_dump(&name) && fs::symlink_metadata(entry.path())?.is_file() {
            dumps.push(name);
        }
    }
    dumps.sort();
    let excess = dumps.len().saturating_sub(keep as usize);
    let removed: Vec<String> = dumps.into_iter().take(excess).collect();
    for name in &removed {
        fs::remove_file(common::safe(&folder.join(name))?)?;
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
    let _recovery = common::lock_wait(
        &data.join("installer/recovery.lock"),
        std::time::Duration::from_secs(3 * 3600),
    )?;
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
            archive(
                &data.join("photos/immich-library"),
                &media.join(archive_dir),
                true,
                a.number("min-age", 600)?,
                if full { Check::Full } else { Check::Quick },
            )?;
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
                common::stage("copying the Immich database copy to the HDD");
                verified_copy(&created, &target, true, 0o600)?;
                // Weekly dumps used to pile up forever on both drives.
                let keep = a.number("db-keep", 8)?;
                for folder in [
                    data.join("backups/immich-database"),
                    media.join("backups/immich-database"),
                ] {
                    prune_dumps(&folder, keep)?;
                }
            }
            Ok(())
        },
    )
}
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
                        "rw,nosuid,nodev",
                        &format!("UUID={uuid}"),
                        &dest.to_string_lossy(),
                    ],
                    30,
                )?;
                mounted_here = true;
            }
            validate_external(&dest, uuid)?;
            guard(&cfg, &data, &media)?;
            common::stage("copying photos and videos to the USB drive");
            copy_tree(&source, &dest, true, check)?;
            validate_external(&dest, uuid)?;
            guard(&cfg, &data, &media)?;
            common::stage("copying the Immich database to the USB drive");
            copy_newest_dump(
                &media.join("backups/immich-database"),
                &dest,
                &data.join("compose/immich/.env"),
            )?;
            validate_external(&dest, uuid)?;
            guard(&cfg, &data, &media)?;
            let mut cloud_copy = None;
            if let Some(cloud) = &cloud {
                if cloud.is_dir() {
                    common::stage("copying Nextcloud files to the USB drive");
                    cloud_copy = Some(copy_cloud(cloud, &dest, true, fstype == "exfat", check)?);
                    validate_external(&dest, uuid)?;
                    guard(&cfg, &data, &media)?;
                } else {
                    println!("Nextcloud: no data folder yet; nothing to copy");
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
            Ok(cloud_copy.and_then(|c| c.missing()))
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
    #[test]
    fn usb_gets_the_newest_database_copy_and_the_immich_version_only() {
        let root = scratch("usb-db");
        let (hdd, usb, env) = (root.join("hdd"), root.join("usb"), root.join(".env"));
        assert_eq!(copy_newest_dump(&hdd, &usb, &env).unwrap(), None);
        write(&env, b"IMMICH_VERSION=v2.1.0\nDB_PASSWORD=secret123\n");
        for day in 10..16 {
            write(
                &hdd.join(format!("immich-202610{day}T001500Z.sql")),
                b"dump",
            );
            copy_newest_dump(&hdd, &usb, &env).unwrap();
        }
        let folder = usb.join("immich-database");
        assert_eq!(
            names(&folder),
            [
                "IMMICH-VERSION.txt",
                "immich-20261012T001500Z.sql",
                "immich-20261013T001500Z.sql",
                "immich-20261014T001500Z.sql",
                "immich-20261015T001500Z.sql"
            ]
        );
        let note = fs::read_to_string(folder.join("IMMICH-VERSION.txt")).unwrap();
        assert!(note.contains("v2.1.0") && !note.contains("secret123"));
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
        assert_eq!(files.len(), 2);
        let version = files.iter().find(|n| n.contains(".neko-")).unwrap();
        assert!(
            version.starts_with("report.neko-") && version.ends_with(".docx"),
            "{version}"
        );
        // Next week, unchanged since that version: nothing new.
        assert_eq!(
            add_only_copy(&src, &dst, true, 0o600, Check::Quick).unwrap(),
            Added::Same
        );
        assert_eq!(names(&root.join("dst")).len(), 2);
        // A dry run never writes.
        write(&src, b"v3");
        assert_eq!(
            add_only_copy(&src, &dst, false, 0o600, Check::Quick).unwrap(),
            Added::Version
        );
        assert_eq!(names(&root.join("dst")).len(), 2);
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
        assert_eq!(names(&usb.join("nextcloud")), ["alice", "bob"]);
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
        let removed = prune_dumps(&root, 2).unwrap();
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
        assert!(prune_dumps(&root, 0).unwrap().is_empty());
        fs::remove_dir_all(root).unwrap();
    }
}
