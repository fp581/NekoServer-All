use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    ffi::CString,
    fmt,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::{
        ffi::OsStrExt,
        fs::{OpenOptionsExt, PermissionsExt},
        io::AsRawFd,
        process::CommandExt,
    },
    path::{Component, Path, PathBuf},
    process::{Command, Stdio},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
#[derive(Debug)]
pub struct Error(String);
impl Error {
    pub fn new(s: &str) -> Self {
        Self(s.into())
    }
}
impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for Error {}
impl From<std::io::Error> for Error {
    // The system's own reason ("No space left on device") is kept: without it a full or
    // failing drive read as an unexplained failure.
    fn from(e: std::io::Error) -> Self {
        Self(format!("Filesystem or process operation failed: {e}"))
    }
}
impl From<serde_json::Error> for Error {
    fn from(_: serde_json::Error) -> Self {
        Self::new("Invalid JSON")
    }
}
pub type Result<T> = std::result::Result<T, Error>;
/// `result`, with a later failure (unmounting, writing the report) added after its own
/// error instead of replacing it: the first error is the one that explains the run.
pub fn first_error<T>(result: Result<T>, later: Result<()>) -> Result<T> {
    match (result, later) {
        (Err(first), Err(also)) => Err(Error(format!("{first} (also: {also})"))),
        (result, Ok(())) => result,
        (Ok(_), Err(also)) => Err(also),
    }
}
/// A time (seconds since 1970) as saved, for showing: a value from a clock that was
/// ahead is shown as now instead of being hidden; anything that is not a time is null.
/// Saved values are never changed this way, so a clock put back loses nothing.
pub fn shown_time(v: &Value, now: f64) -> Value {
    v.as_f64()
        .filter(|t| t.is_finite() && *t >= 0.0)
        .map(|t| serde_json::json!(t.min(now)))
        .unwrap_or(Value::Null)
}
/// A time kept as it was saved (any finite, non-negative number), or null.
pub fn kept_time(v: &Value) -> Value {
    num(v, 0.0, f64::MAX)
}
/// Changed so recently (or written a moment ahead of the clock) that it may still be
/// written to: less than `age` seconds from now either way. A file dated further ahead
/// was dated by a wrong clock; it is copied (every copy is verified), never left for a
/// time that may never come.
pub fn settling(modified: f64, age: f64) -> bool {
    (now() - modified).abs() < age
}
/// Drives a running job writes its backups to: (where it is mounted, its device number
/// when the job checked it). See `expect_drive`.
static DRIVES: Mutex<Vec<(PathBuf, u64)>> = Mutex::new(Vec::new());
/// Held while a job writes to the drive mounted at `root`: until it is dropped, every
/// folder, file and publish below `root` first checks the drive is still the one that
/// was mounted there. A drive that drops out mid-job leaves an empty folder on the
/// system disk; without this check the backup would carry on writing into it.
pub struct DriveHold(PathBuf, u64);
impl Drop for DriveHold {
    fn drop(&mut self) {
        if let Ok(mut drives) = DRIVES.lock() {
            if let Some(i) = drives.iter().position(|d| d.0 == self.0 && d.1 == self.1) {
                drives.remove(i);
            }
        }
    }
}
/// Remember the drive now mounted at `root` (checked by the caller) for `on_drive`.
pub fn expect_drive(root: &Path) -> Result<DriveHold> {
    use std::os::unix::fs::MetadataExt;
    expect_device(root, fs::metadata(root)?.dev())
}
fn expect_device(root: &Path, dev: u64) -> Result<DriveHold> {
    DRIVES
        .lock()
        .map_err(|_| Error::new("Drive check unavailable"))?
        .push((root.to_path_buf(), dev));
    Ok(DriveHold(root.to_path_buf(), dev))
}
/// Is `path` (to be written) still on the drive the job started with? Only paths below a
/// drive held with `expect_drive` are checked.
pub fn on_drive(path: &Path) -> Result<()> {
    use std::os::unix::fs::MetadataExt;
    let drives = DRIVES
        .lock()
        .map(|d| d.clone())
        .map_err(|_| Error::new("Drive check unavailable"))?;
    for (root, dev) in drives {
        if path.starts_with(&root) && !fs::metadata(&root).is_ok_and(|m| m.dev() == dev) {
            return Err(Error(format!(
                "The drive mounted at {} is gone (disconnected, or it failed) partway through the backup, so the backup stopped; nothing was written onto the system disk instead. Reconnect the drive and run the backup again",
                root.display()
            )));
        }
    }
    Ok(())
}
pub fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
pub fn root() -> bool {
    unsafe { libc::geteuid() == 0 }
}
pub fn require_root() -> Result<()> {
    if !root() {
        return Err(Error::new("Root required"));
    }
    Ok(())
}
pub fn safe(path: &Path) -> Result<PathBuf> {
    for p in path.ancestors() {
        match fs::symlink_metadata(p) {
            Ok(m) if m.file_type().is_symlink() => {
                return Err(Error::new(&format!(
                    "Symlink path refused: {} is a link ({})",
                    p.display(),
                    path.display()
                )))
            }
            Ok(_) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.into()),
        }
    }
    if path.components().any(|c| matches!(c, Component::ParentDir)) {
        return Err(Error::new("Parent traversal refused"));
    }
    Ok(path.to_path_buf())
}
pub fn mount_path(s: &str) -> Result<PathBuf> {
    let p = safe(Path::new(s))?;
    if !p.is_absolute()
        || ["/", "/mnt", "/srv", "/home", "/var", "/tmp"].contains(&s)
        || !s
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"/._-".contains(&c))
    {
        return Err(Error::new("Unsafe mount path"));
    }
    Ok(p)
}
pub fn relative(s: &str) -> Result<PathBuf> {
    let p = PathBuf::from(s);
    if p.is_absolute()
        || s.is_empty()
        || s == "."
        || p.components().any(|c| matches!(c, Component::ParentDir))
        || !s
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"/._-".contains(&c))
    {
        return Err(Error::new("Unsafe relative path"));
    }
    Ok(p)
}
/// Delete the oldest published `snapshot-<time>-<pid>` directories under `root`,
/// keeping the newest `keep` (0 keeps everything). Only complete snapshots (with a
/// manifest) owned privately by this user are touched; hidden `.partial` stages and
/// anything else are left alone. Returns the removed names. `newest` (the snapshot just
/// made) is never removed, even if a clock jump made its name sort before older ones.
/// Keep the newest `keep` snapshots in `root` (0 keeps all). `newest` (the one just
/// published) and `protected` (one that still holds files later runs left out) are
/// never deleted; `protected` is kept on top of `keep`.
/// The age order of a backup named after its time (microseconds, then the process id).
/// One dated more than a day ahead was made while the clock was wrong, so before it was
/// put right: older than anything made since. It sorts first and goes first, instead of
/// looking newest and outliving every backup made with the right time.
pub type AgeKey = (bool, u128, u128);
pub fn age_key(micros: u128, pid: u128) -> AgeKey {
    let ahead = (now() * 1e6) as u128 + 86_400_000_000;
    (micros <= ahead, micros, pid)
}
/// The complete snapshots in `root` (a manifest, owned privately by this user), oldest
/// first by `age_key`, with that key.
pub fn complete_snapshots(root: &Path) -> Result<Vec<(AgeKey, String)>> {
    use std::os::unix::fs::MetadataExt;
    let pattern = regex::Regex::new(r"^snapshot-([0-9]+)-([0-9]+)$").unwrap();
    let owner = unsafe { libc::geteuid() };
    let mut found = vec![];
    if !root.is_dir() {
        return Ok(found);
    }
    for entry in fs::read_dir(safe(root)?)? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().to_string();
        let Some(parts) = pattern.captures(&name) else {
            continue;
        };
        let meta = fs::symlink_metadata(entry.path())?;
        if !meta.is_dir() || meta.uid() != owner || meta.mode() & 0o077 != 0 {
            continue;
        }
        if !entry.path().join("manifest.json").is_file() {
            continue;
        }
        let order = age_key(
            parts[1].parse::<u128>().unwrap_or(0),
            parts[2].parse::<u128>().unwrap_or(0),
        );
        found.push((order, name));
    }
    found.sort();
    Ok(found)
}
pub fn prune_snapshots(
    root: &Path,
    keep: u64,
    newest: Option<&str>,
    protected: Option<&str>,
) -> Result<Vec<String>> {
    if keep == 0 || !root.is_dir() {
        return Ok(vec![]);
    }
    let found: Vec<(AgeKey, String)> = complete_snapshots(root)?
        .into_iter()
        .filter(|(_, name)| Some(name.as_str()) != newest && Some(name.as_str()) != protected)
        .collect();
    let ahead: Vec<&str> = found
        .iter()
        .filter(|(order, _)| !order.0)
        .map(|(_, name)| name.as_str())
        .collect();
    if !ahead.is_empty() {
        println!("Backup retention: {} dated in the future (the clock was wrong when made), counted as the oldest; check the server's clock (timedatectl)", ahead.join(", "));
    }
    let kept_aside = usize::from(newest.is_some_and(|n| root.join(n).is_dir()));
    let excess = (found.len() + kept_aside).saturating_sub(keep as usize);
    let mut removed = vec![];
    clear_leftovers(root, false)?;
    for (_, name) in found.into_iter().take(excess) {
        remove_snapshot(root, &name)?;
        removed.push(name);
    }
    Ok(removed)
}
/// Delete one published snapshot: renamed to `.<name>.deleting` first (never over
/// anything), then removed. A power cut part-way leaves only that hidden name, which
/// nothing counts or restores as a snapshot; removed in place, a half-deleted one would
/// still have its manifest and count as complete.
pub fn remove_snapshot(root: &Path, name: &str) -> Result<()> {
    let doomed = root.join(format!(".{name}.deleting"));
    rename_noreplace(&safe(&root.join(name))?, &safe(&doomed)?)?;
    File::open(root)?.sync_all()?;
    fs::remove_dir_all(&doomed)?;
    File::open(root)?.sync_all()?;
    Ok(())
}
/// Remove what cut-off runs left in a snapshot folder: `.snapshot-*.deleting` (a delete
/// that was cut off) and, with `partial`, `.snapshot-*.partial` (a snapshot never
/// finished; call it only with the job's lock held, when no snapshot is being built).
/// Only this user's private folders are touched. Returns the removed names.
pub fn clear_leftovers(root: &Path, partial: bool) -> Result<Vec<String>> {
    use std::os::unix::fs::MetadataExt;
    let pattern = regex::Regex::new(r"^\.snapshot-[0-9]+-[0-9]+\.(partial|deleting)$").unwrap();
    let owner = unsafe { libc::geteuid() };
    let mut removed = vec![];
    if !root.is_dir() {
        return Ok(removed);
    }
    for entry in fs::read_dir(safe(root)?)? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().to_string();
        let Some(kind) = pattern.captures(&name) else {
            continue;
        };
        let meta = fs::symlink_metadata(entry.path())?;
        if (&kind[1] == "partial" && !partial)
            || !meta.is_dir()
            || meta.uid() != owner
            || meta.mode() & 0o077 != 0
        {
            continue;
        }
        fs::remove_dir_all(safe(&entry.path())?)?;
        removed.push(name);
    }
    if !removed.is_empty() {
        File::open(root)?.sync_all()?;
        println!(
            "Backup: removed what an earlier run that was cut off or failed left behind ({})",
            removed.join(", ")
        );
    }
    Ok(removed)
}
/// Bytes free for this program on the filesystem holding `path`.
pub fn free_bytes(path: &Path) -> Result<u128> {
    let c = CString::new(path.as_os_str().as_bytes()).map_err(|_| Error::new("Invalid path"))?;
    let mut stat = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    // SAFETY: a NUL-terminated path and a buffer statvfs fills on success.
    if unsafe { libc::statvfs(c.as_ptr(), stat.as_mut_ptr()) } != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let stat = unsafe { stat.assume_init() };
    Ok(stat.f_bavail as u128 * stat.f_frsize as u128)
}
pub fn file(path: &Path) -> Result<File> {
    safe(path)?;
    if !fs::symlink_metadata(path)?.is_file() {
        return Err(Error::new("Regular file required"));
    }
    let f = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(path)?;
    if !f.metadata()?.is_file() {
        return Err(Error::new("Regular file required"));
    }
    Ok(f)
}
pub fn bytes(path: &Path, limit: usize) -> Result<Vec<u8>> {
    let f = file(path)?;
    if f.metadata()?.len() > limit as u64 {
        return Err(Error::new("Oversized file"));
    }
    let mut b = Vec::new();
    f.take(limit as u64 + 1).read_to_end(&mut b)?;
    if b.len() > limit {
        return Err(Error::new("Oversized file"));
    }
    Ok(b)
}
pub fn json(path: &Path, limit: usize) -> Value {
    bytes(path, limit)
        .ok()
        .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
        .filter(Value::is_object)
        .unwrap_or_else(|| serde_json::json!({}))
}
static SERIAL: AtomicU64 = AtomicU64::new(0);
pub fn atomic(path: &Path, data: &[u8], mode: u32) -> Result<()> {
    safe(path)?;
    on_drive(path)?;
    let parent = path.parent().ok_or_else(|| Error::new("Missing parent"))?;
    fs::create_dir_all(parent)?;
    safe(parent)?;
    let temp = parent.join(format!(
        ".neko-{}-{}-{}",
        std::process::id(),
        (now() * 1e9) as u128,
        SERIAL.fetch_add(1, Ordering::Relaxed)
    ));
    let result = (|| {
        let mut f = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(mode)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(&temp)?;
        f.set_permissions(fs::Permissions::from_mode(mode))?;
        f.write_all(data)?;
        f.sync_all()?;
        safe(path)?;
        fs::rename(&temp, path)?;
        File::open(parent)?.sync_all()?;
        Ok(())
    })();
    let _ = fs::remove_file(temp);
    result
}
pub fn atomic_json(path: &Path, v: &Value) -> Result<()> {
    let mut b = serde_json::to_vec(v)?;
    b.push(b'\n');
    atomic(path, &b, 0o600)
}
pub fn digest(path: &Path) -> Result<String> {
    let mut f = file(path)?;
    let mut h = Sha256::new();
    let mut b = [0u8; 65536];
    loop {
        let n = f.read(&mut b)?;
        if n == 0 {
            break;
        }
        h.update(&b[..n])
    }
    Ok(format!("{:x}", h.finalize()))
}
pub fn sha(b: &[u8]) -> String {
    format!("{:x}", Sha256::digest(b))
}
/// Move `from` to `to` in one step, never replacing anything already at `to`.
pub fn rename_noreplace(from: &Path, to: &Path) -> Result<()> {
    let a = CString::new(from.as_os_str().as_bytes()).map_err(|_| Error::new("Invalid path"))?;
    let b = CString::new(to.as_os_str().as_bytes()).map_err(|_| Error::new("Invalid path"))?;
    // SAFETY: both paths are NUL-terminated C strings that outlive the call.
    let moved = unsafe {
        libc::syscall(
            libc::SYS_renameat2,
            libc::AT_FDCWD,
            a.as_ptr(),
            libc::AT_FDCWD,
            b.as_ptr(),
            libc::RENAME_NOREPLACE,
        )
    };
    if moved != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok(())
}
pub fn publish(temp: &Path, target: &Path) -> Result<()> {
    safe(temp)?;
    safe(target)?;
    on_drive(target)?;
    match fs::hard_link(temp, target) {
        Ok(()) => fs::remove_file(temp)?,
        Err(e)
            if matches!(
                e.raw_os_error(),
                Some(libc::EPERM) | Some(libc::EOPNOTSUPP) | Some(libc::ENOSYS)
            ) =>
        {
            rename_noreplace(temp, target)?;
        }
        Err(e) => return Err(e.into()),
    }
    File::open(
        target
            .parent()
            .ok_or_else(|| Error::new("Missing parent"))?,
    )?
    .sync_all()?;
    Ok(())
}
/// Send one HTTPS request described by a private curl config (the address and any
/// secret stay out of the process list), then remove the config. Ok only for a 2xx
/// answer; `failure` is the error otherwise.
pub fn deliver(runtime: &Path, name: &str, config: &str, failure: &str) -> Result<()> {
    let path = runtime.join(name);
    atomic(&path, config.as_bytes(), 0o600)?;
    let result = command(
        &[
            "curl",
            "--disable",
            "--config",
            &path.to_string_lossy(),
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--noproxy",
            "*",
            "--globoff",
            "--silent",
            "--output",
            "/dev/null",
            "--write-out",
            "%{http_code}",
            "--connect-timeout",
            "10",
            "--max-time",
            "20",
        ],
        25,
    );
    let _ = fs::remove_file(path);
    let (code, status) = result?;
    if code != 0
        || !status
            .trim()
            .parse::<u16>()
            .is_ok_and(|v| (200..300).contains(&v))
    {
        return Err(Error::new(failure));
    }
    Ok(())
}
pub fn lock(path: &Path) -> Result<File> {
    safe(path)?;
    on_drive(path)?;
    if let Some(p) = path.parent() {
        fs::create_dir_all(p)?;
    }
    let f = OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(path)?;
    if !f.metadata()?.is_file()
        || unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0
    {
        return Err(Error::new("Job already running or unsafe lock"));
    }
    Ok(f)
}
/// Shared recovery lock for scheduled jobs: wait for a job that is already running
/// (polling every 5 s, up to `max`) instead of failing. Scheduled backups and the
/// monthly update overlap at night; failing would skip that night's or month's run.
pub fn lock_wait(path: &Path, max: Duration) -> Result<File> {
    let start = Instant::now();
    let mut announced = false;
    loop {
        match lock(path) {
            Ok(f) => return Ok(f),
            Err(e) => {
                if start.elapsed() >= max {
                    return Err(Error::new(&format!(
                        "Another NekoServer job still held {} after {} minutes",
                        path.display(),
                        max.as_secs() / 60
                    )));
                }
                if !announced {
                    println!("Waiting for another NekoServer job to finish ({e})");
                    announced = true;
                }
                thread::sleep(Duration::from_secs(5).min(max));
            }
        }
    }
}
/// Setup (`sudo bash install.sh`) holds this lock for as long as it runs: install.sh
/// opens it and every setup program inherits it.
pub const SETUP_LOCK: &str = "/run/nekoserver-setup.lock";
/// How a job begins its message when it gave way to setup. Not a failure: the job ends
/// without an error (main.rs), so no alert is sent, and it runs at its next time.
pub const POSTPONED: &str = "Postponed: setup is running";
pub fn postponed(e: &Error) -> bool {
    e.to_string().starts_with(POSTPONED)
}
/// `lock_wait` for the backups' shared lock (installer/recovery.lock), which setup holds
/// too while it changes apps, also while it waits at a question for you. When setup is
/// the one holding it after `while_setup` (at most `max`), the job is postponed
/// (POSTPONED), not failed: a setup left open at a question must not page you.
pub fn shared_lock_wait(path: &Path, max: Duration, while_setup: Duration) -> Result<File> {
    shared_lock_wait_with(path, Path::new(SETUP_LOCK), max, while_setup)
}
fn shared_lock_wait_with(
    path: &Path,
    setup_lock: &Path,
    max: Duration,
    while_setup: Duration,
) -> Result<File> {
    let start = Instant::now();
    let mut announced = false;
    loop {
        match lock(path) {
            Ok(f) => return Ok(f),
            Err(e) => {
                let setup = held_by(path, setup_lock);
                let limit = if setup { while_setup.min(max) } else { max };
                if start.elapsed() >= limit {
                    return Err(if setup {
                        Error::new(&format!(
                            "{POSTPONED} (sudo bash install.sh holds the backups' lock, perhaps waiting at a question); this run did not fail and runs again at its next time"
                        ))
                    } else {
                        Error::new(&format!(
                            "Another NekoServer job still held {} after {} minutes",
                            path.display(),
                            max.as_secs() / 60
                        ))
                    });
                }
                if !announced {
                    println!(
                        "Waiting for {} to finish ({e})",
                        if setup {
                            "setup (sudo bash install.sh)"
                        } else {
                            "another NekoServer job"
                        }
                    );
                    announced = true;
                }
                thread::sleep(Duration::from_secs(5).min(limit));
            }
        }
    }
}
/// Whether setup holds the lock at `path`: a process holding it (the kernel's list of
/// locks) also has setup's own lock (`setup`) open. Read-only: nothing is locked to find
/// out, so a setup starting meanwhile is never kept from taking its own lock.
fn held_by(path: &Path, setup: &Path) -> bool {
    use std::os::unix::fs::MetadataExt;
    let (Ok(lock), Ok(setup)) = (fs::metadata(path), fs::metadata(setup)) else {
        return false;
    };
    let locks = fs::read_to_string("/proc/locks").unwrap_or_default();
    lock_holders(&locks, lock.dev(), lock.ino())
        .into_iter()
        .any(|pid| has_open(pid, setup.dev(), setup.ino()))
}
/// The processes holding a lock on the file (`dev`, `ino`), from /proc/locks lines like
/// "1: FLOCK  ADVISORY  WRITE 1234 08:02:5678 0 EOF" (a waiting one has "->").
fn lock_holders(locks: &str, dev: u64, ino: u64) -> Vec<u32> {
    let file = format!("{:02x}:{:02x}:{ino}", libc::major(dev), libc::minor(dev));
    locks
        .lines()
        .filter(|l| !l.contains("->"))
        .filter_map(|l| {
            let fields: Vec<&str> = l.split_whitespace().collect();
            (fields.len() > 5 && fields[5] == file).then(|| fields[4].parse().ok())?
        })
        .collect()
}
/// Whether process `pid` has the file (`dev`, `ino`) open.
fn has_open(pid: u32, dev: u64, ino: u64) -> bool {
    use std::os::unix::fs::MetadataExt;
    fs::read_dir(format!("/proc/{pid}/fd"))
        .into_iter()
        .flatten()
        .flatten()
        .any(|fd| fs::metadata(fd.path()).is_ok_and(|m| m.dev() == dev && m.ino() == ino))
}
/// What the current job is doing, for its report if it fails ("copying photos").
static STAGE: Mutex<String> = Mutex::new(String::new());
pub fn stage(what: &str) {
    if let Ok(mut s) = STAGE.lock() {
        *s = what.to_string();
    }
}
pub fn current_stage() -> String {
    STAGE.lock().map(|s| s.clone()).unwrap_or_default()
}
/// A short, safe reason for reports and the dashboard: one line, printable, at most
/// 300 characters, with anything that looks like a password or key removed.
pub fn sanitize(text: &str) -> String {
    let secret =
        regex::Regex::new(r"(?i)\b([a-z_]*(pass(word)?|secret|token|key)[a-z_]*)\s*[=:]\s*\S+")
            .unwrap();
    let line = text
        .lines()
        .map(str::trim)
        .rfind(|l| !l.is_empty())
        .unwrap_or("")
        .chars()
        .filter(|c| !c.is_control())
        .collect::<String>();
    secret
        .replace_all(&line, "$1=<removed>")
        .chars()
        .take(300)
        .collect()
}
/// The last part of a command's error output (stderr), sanitized; kept so a failure
/// can say why instead of only that it failed.
static LAST_STDERR: Mutex<String> = Mutex::new(String::new());
pub fn last_stderr() -> String {
    LAST_STDERR.lock().map(|s| s.clone()).unwrap_or_default()
}
/// Turn a failed command into an error that names the program and its reason.
pub fn failed(args: &[&str], code: i32, what: &str) -> Error {
    let program = Path::new(args.first().copied().unwrap_or("command"))
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_default();
    let detail = last_stderr();
    Error::new(&format!(
        "{what}: {program} exited with {code}{}",
        if detail.is_empty() {
            String::new()
        } else {
            format!(" ({detail})")
        }
    ))
}
pub fn command(args: &[&str], timeout: u64) -> Result<(i32, String)> {
    let mut cmd = Command::new(args[0]);
    cmd.args(&args[1..])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env("LC_ALL", "C")
        .env("TZ", "UTC");
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
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| Error::new("No command output"))?;
    // Drain stderr too (a full pipe would block the command); keep only its last 8 KB.
    let errors = child
        .stderr
        .take()
        .ok_or_else(|| Error::new("No command error output"))?;
    let errors = thread::spawn(move || {
        let mut tail: Vec<u8> = Vec::new();
        let mut chunk = [0u8; 4096];
        let mut errors = errors;
        while let Ok(n) = errors.read(&mut chunk) {
            if n == 0 {
                break;
            }
            tail.extend_from_slice(&chunk[..n]);
            if tail.len() > 8192 {
                tail.drain(..tail.len() - 8192);
            }
        }
        tail
    });
    let overflow = Arc::new(AtomicBool::new(false));
    let flag = overflow.clone();
    let read = thread::spawn(move || {
        let mut b = Vec::new();
        let result = stdout.take(2_000_001).read_to_end(&mut b);
        if b.len() > 2_000_000 {
            flag.store(true, Ordering::Relaxed);
        }
        result.map(|_| b)
    });
    let start = Instant::now();
    let status = loop {
        if overflow.load(Ordering::Relaxed) || start.elapsed() >= Duration::from_secs(timeout) {
            unsafe {
                libc::kill(-pid, libc::SIGKILL);
            }
            // A program stuck on a hung drive cannot die until the drive answers: a
            // thread of its own reaps it, so the caller (the dashboard's collector, say)
            // never waits on it.
            thread::spawn(move || {
                let _ = child.wait();
            });
            return Err(Error::new("Command timeout or oversized output"));
        }
        if let Some(s) = child.try_wait()? {
            break s;
        }
        thread::sleep(Duration::from_millis(10));
    };
    unsafe {
        libc::kill(-pid, libc::SIGKILL);
    }
    // A helper it started that is stuck on a hung drive keeps the output open: waited
    // for briefly, never for good.
    let output = joined(read, Duration::from_secs(5))
        .ok_or_else(|| Error::new("Command output did not finish"))??;
    let tail = joined(errors, Duration::from_secs(5)).unwrap_or_default();
    if let Ok(mut last) = LAST_STDERR.lock() {
        *last = sanitize(&String::from_utf8_lossy(&tail));
    }
    if output.len() > 2_000_000 {
        return Err(Error::new("Oversized command output"));
    }
    Ok((
        status.code().unwrap_or(-1),
        String::from_utf8_lossy(&output).into_owned(),
    ))
}
/// A thread's result if it ends within `max`; None (it is left to finish alone) if not.
pub fn joined<T>(handle: thread::JoinHandle<T>, max: Duration) -> Option<T> {
    let start = Instant::now();
    while !handle.is_finished() {
        if start.elapsed() >= max {
            return None;
        }
        thread::sleep(Duration::from_millis(10));
    }
    handle.join().ok()
}
pub fn probe(args: &[&str], timeout: u64) -> (i32, String) {
    command(args, timeout).unwrap_or((-1, String::new()))
}
/// findmnt's columns for the mount in effect at `path` (with mounts stacked on one
/// folder it lists them oldest first; the last is the one in use), by `--mountpoint`
/// or `--target`. `Ok(None)` when nothing is mounted there.
pub fn findmnt(columns: &str, by: &str, path: &Path, timeout: u64) -> Result<Option<String>> {
    let (code, out) = command(
        &["findmnt", "-n", "-o", columns, by, &path.to_string_lossy()],
        timeout,
    )?;
    Ok(mount_in_effect(code, &out))
}
/// The last non-empty line of findmnt's output: the mount in effect when several are
/// stacked on one folder; nothing when findmnt failed (nothing mounted there).
pub fn mount_in_effect(code: i32, out: &str) -> Option<String> {
    if code != 0 {
        return None;
    }
    out.lines()
        .map(str::trim)
        .rfind(|l| !l.is_empty())
        .map(str::to_string)
}
pub fn mounted(path: &Path) -> bool {
    probe(
        &["findmnt", "-n", "--mountpoint", &path.to_string_lossy()],
        10,
    )
    .0 == 0
}
pub fn num(v: &Value, lo: f64, hi: f64) -> Value {
    v.as_f64()
        .filter(|x| x.is_finite() && *x >= lo && *x <= hi)
        .map(|_| v.clone())
        .unwrap_or(Value::Null)
}
pub fn round(x: f64) -> f64 {
    (x * 10.0).round() / 10.0
}

/// Set explicit permissions only on directories we create, independent of umask.
/// Existing populated directories retain the administrator's ownership and mode.
/// Below a drive a job holds (`expect_drive`), nothing is made once that drive is gone,
/// and the folder it is mounted on is never made again.
pub fn directory(path: &Path, mode: u32) -> Result<()> {
    safe(path)?;
    on_drive(path)?;
    if path.exists() {
        if !path.is_dir() {
            return Err(Error::new("Directory required"));
        }
        return Ok(());
    }
    let parent = path.parent().ok_or_else(|| Error::new("Missing parent"))?;
    directory(parent, mode)?;
    match fs::create_dir(path) {
        Ok(()) => fs::set_permissions(path, fs::Permissions::from_mode(mode))?,
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
            safe(path)?;
            if !path.is_dir() {
                return Err(Error::new("Directory required"));
            }
        }
        Err(e) => return Err(e.into()),
    }
    Ok(())
}
#[cfg(test)]
mod lock_tests {
    #[test]
    fn failure_reasons_keep_the_cause_but_not_secrets() {
        assert_eq!(
            sanitize("first line\nERROR: password=hunter2 rejected for DB_PASSWORD: x9y8\n"),
            "ERROR: password=<removed> rejected for DB_PASSWORD=<removed>"
        );
        assert!(sanitize(&"x".repeat(1000)).len() <= 300);
        let (code, _) = command(
            &[
                "sh",
                "-c",
                "echo 'disk full: No space left on device' >&2; exit 3",
            ],
            10,
        )
        .unwrap();
        assert_eq!(code, 3);
        let error = failed(&["/usr/bin/sh"], code, "Copy failed").to_string();
        assert!(
            error.contains("sh exited with 3") && error.contains("No space left on device"),
            "{error}"
        );
    }
    use super::*;
    fn private(path: &Path) {
        fs::create_dir_all(path).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
    }
    fn scratch(name: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!("neko-common-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        private(&root);
        root
    }
    #[test]
    fn a_drive_that_drops_out_is_never_written_through() {
        use std::os::unix::fs::MetadataExt;
        let root = scratch("drive-gone");
        let mount = root.join("usb");
        fs::create_dir_all(&mount).unwrap();
        let dev = fs::metadata(&mount).unwrap().dev();
        {
            // Still the drive the job started with: folders and files are made.
            let _held = expect_device(&mount, dev).unwrap();
            directory(&mount.join("backups/photos"), 0o755).unwrap();
            atomic(&mount.join("backups/note.txt"), b"x", 0o600).unwrap();
        }
        {
            // Another filesystem is there now (the drive was unmounted, leaving the empty
            // folder on the system disk): nothing is made or published below it.
            let _held = expect_device(&mount, dev + 1).unwrap();
            let error = directory(&mount.join("backups/more"), 0o755).unwrap_err();
            assert!(error.to_string().contains("is gone"), "{error}");
            assert!(atomic(&mount.join("backups/note2.txt"), b"x", 0o600).is_err());
            fs::write(mount.join("backups/temp"), b"x").unwrap();
            assert!(publish(&mount.join("backups/temp"), &mount.join("backups/done")).is_err());
            assert!(!mount.join("backups/more").exists() && !mount.join("backups/done").exists());
        }
        {
            // The folder it was mounted on is gone: it is never made again.
            let gone = root.join("gone");
            let _held = expect_device(&gone, dev).unwrap();
            assert!(directory(&gone.join("backups/photos"), 0o755).is_err());
            assert!(!gone.exists());
        }
        // Released: paths are no longer checked.
        directory(&root.join("gone/backups"), 0o755).unwrap();
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn the_first_error_stays_first() {
        let first: Result<()> = Err(Error::new("Copy failed: No space left on device"));
        let both = first_error(first, Err(Error::new("umount failed"))).unwrap_err();
        assert_eq!(
            both.to_string(),
            "Copy failed: No space left on device (also: umount failed)"
        );
        assert_eq!(
            first_error(Ok(1), Err(Error::new("umount failed")))
                .unwrap_err()
                .to_string(),
            "umount failed"
        );
        assert_eq!(first_error(Ok(1), Ok(())).unwrap(), 1);
    }
    #[test]
    fn a_system_error_keeps_its_reason() {
        let error = Error::from(std::io::Error::from_raw_os_error(libc::ENOSPC)).to_string();
        assert!(error.contains("No space left on device"), "{error}");
        assert!(error.starts_with("Filesystem or process operation failed"));
    }
    #[test]
    fn a_time_from_a_clock_that_was_ahead_is_shown_not_hidden() {
        let now = 1_800_000_000.0;
        assert_eq!(shown_time(&secs(now + 86400.0), now), secs(now));
        assert_eq!(shown_time(&secs(now - 5.0), now), secs(now - 5.0));
        assert!(shown_time(&Value::String("x".into()), now).is_null());
        assert_eq!(kept_time(&secs(now + 86400.0)), secs(now + 86400.0));
        assert!(kept_time(&secs(-1.0)).is_null());
    }
    fn secs(v: f64) -> Value {
        serde_json::json!(v)
    }
    #[test]
    fn a_delete_cut_off_never_leaves_a_snapshot_that_counts() {
        let root = scratch("deleting");
        for name in ["snapshot-100-1", "snapshot-200-1", "snapshot-300-1"] {
            private(&root.join(name));
            fs::write(root.join(name).join("manifest.json"), b"{}").unwrap();
        }
        // What a power cut during an earlier delete leaves: the manifest may still be there.
        private(&root.join(".snapshot-50-1.deleting"));
        fs::write(root.join(".snapshot-50-1.deleting/manifest.json"), b"{}").unwrap();
        private(&root.join(".snapshot-60-1.partial"));
        let listed: Vec<String> = complete_snapshots(&root)
            .unwrap()
            .into_iter()
            .map(|(_, n)| n)
            .collect();
        assert_eq!(
            listed,
            ["snapshot-100-1", "snapshot-200-1", "snapshot-300-1"]
        );
        assert_eq!(
            prune_snapshots(&root, 2, Some("snapshot-300-1"), None).unwrap(),
            ["snapshot-100-1"]
        );
        let mut left: Vec<String> = fs::read_dir(&root)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().to_string())
            .collect();
        left.sort();
        // The leftover delete is finished; a snapshot being built is not touched by pruning.
        assert_eq!(
            left,
            [".snapshot-60-1.partial", "snapshot-200-1", "snapshot-300-1"]
        );
        assert_eq!(
            clear_leftovers(&root, true).unwrap(),
            [".snapshot-60-1.partial"]
        );
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn a_helper_that_keeps_the_output_open_never_holds_the_caller() {
        // A program that ends but leaves a helper (in a session of its own, so it is not
        // killed with it) holding its output, as one stuck on a hung drive would.
        let start = Instant::now();
        let result = command(
            &["sh", "-c", "setsid sleep 30 & sleep 0.5; echo started"],
            10,
        );
        assert!(
            start.elapsed() < Duration::from_secs(15),
            "{:?}",
            start.elapsed()
        );
        assert!(result.is_err());
        // A program that times out returns at its time limit.
        let start = Instant::now();
        assert!(command(&["sleep", "30"], 1).is_err());
        assert!(start.elapsed() < Duration::from_secs(5));
    }
    #[test]
    fn stacked_mounts_give_the_one_in_effect() {
        use super::mount_in_effect;
        assert_eq!(
            mount_in_effect(0, "old-uuid ext4\nnew-uuid btrfs\n"),
            Some("new-uuid btrfs".to_string())
        );
        assert_eq!(mount_in_effect(0, "only\n"), Some("only".to_string()));
        assert_eq!(mount_in_effect(1, ""), None);
        assert_eq!(mount_in_effect(0, "\n"), None);
    }

    #[test]
    fn scheduled_jobs_wait_for_the_lock_then_proceed() {
        let path = std::env::temp_dir().join(format!("neko-lock-{}", std::process::id()));
        let held = lock(&path).unwrap();
        // Busy: the plain lock fails at once; the waiting lock gives up after its budget.
        assert!(lock(&path).is_err());
        let start = Instant::now();
        assert!(lock_wait(&path, Duration::from_secs(1)).is_err());
        assert!(start.elapsed() >= Duration::from_secs(1));
        // Released while waiting: the waiter gets it.
        let releaser = thread::spawn(move || {
            thread::sleep(Duration::from_millis(300));
            drop(held);
        });
        let got = lock_wait(&path, Duration::from_secs(60));
        releaser.join().unwrap();
        assert!(got.is_ok());
        drop(got);
        fs::remove_file(path).unwrap();
    }
    #[test]
    fn a_job_that_finds_setup_holding_the_shared_lock_is_postponed_not_failed() {
        use std::os::unix::fs::MetadataExt;
        let root = scratch("setup-holds");
        let (shared, setup) = (root.join("recovery.lock"), root.join("setup.lock"));
        let line = |pid: u32, file: &Path| {
            let m = fs::metadata(file).unwrap();
            format!(
                "1: FLOCK  ADVISORY  WRITE {pid} {:02x}:{:02x}:{} 0 EOF",
                libc::major(m.dev()),
                libc::minor(m.dev()),
                m.ino()
            )
        };
        let held = lock(&shared).unwrap();
        // The kernel's list names this process as the holder; a waiter ("->") does not count.
        let me = std::process::id();
        let waiting = format!("1: -> {}", &line(7, &shared)[3..]);
        let meta = fs::metadata(&shared).unwrap();
        assert_eq!(
            lock_holders(
                &format!("{}\n{waiting}", line(me, &shared)),
                meta.dev(),
                meta.ino()
            ),
            vec![me]
        );
        let short = Duration::from_millis(50);
        // Another job (no setup lock open) still holds it after the wait: a failure, as before.
        fs::write(&setup, b"").unwrap();
        let other = shared_lock_wait_with(&shared, &setup, short, short).unwrap_err();
        assert!(
            !postponed(&other) && other.to_string().contains("still held"),
            "{other}"
        );
        // Setup holds it (the holder has setup's own lock open, as install.sh's programs do).
        let setup_open = File::open(&setup).unwrap();
        let waited =
            shared_lock_wait_with(&shared, &setup, Duration::from_secs(60), short).unwrap_err();
        assert!(postponed(&waited), "{waited}");
        drop(setup_open);
        drop(held);
        assert!(shared_lock_wait_with(&shared, &setup, short, short).is_ok());
        let _ = fs::remove_dir_all(root);
    }
}
