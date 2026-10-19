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
    fn from(_: std::io::Error) -> Self {
        Self::new("Filesystem or process operation failed")
    }
}
impl From<serde_json::Error> for Error {
    fn from(_: serde_json::Error) -> Self {
        Self::new("Invalid JSON")
    }
}
pub type Result<T> = std::result::Result<T, Error>;
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
            Ok(m) if m.file_type().is_symlink() => return Err(Error::new("Symlink path refused")),
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
/// anything else are left alone. Returns the removed names.
pub fn prune_snapshots(root: &Path, keep: u64) -> Result<Vec<String>> {
    use std::os::unix::fs::MetadataExt;
    if keep == 0 || !root.is_dir() {
        return Ok(vec![]);
    }
    let pattern = regex::Regex::new(r"^snapshot-([0-9]+)-([0-9]+)$").unwrap();
    let owner = unsafe { libc::geteuid() };
    let mut found = vec![];
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
        let order = (
            parts[1].parse::<u128>().unwrap_or(0),
            parts[2].parse::<u128>().unwrap_or(0),
        );
        found.push((order, name));
    }
    found.sort();
    let excess = found.len().saturating_sub(keep as usize);
    let mut removed = vec![];
    for (_, name) in found.into_iter().take(excess) {
        fs::remove_dir_all(safe(&root.join(&name))?)?;
        removed.push(name);
    }
    if !removed.is_empty() {
        File::open(root)?.sync_all()?;
    }
    Ok(removed)
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
pub fn publish(temp: &Path, target: &Path) -> Result<()> {
    safe(temp)?;
    safe(target)?;
    match fs::hard_link(temp, target) {
        Ok(()) => fs::remove_file(temp)?,
        Err(e)
            if matches!(
                e.raw_os_error(),
                Some(libc::EPERM) | Some(libc::EOPNOTSUPP) | Some(libc::ENOSYS)
            ) =>
        {
            let a = CString::new(temp.as_os_str().as_bytes())
                .map_err(|_| Error::new("Invalid path"))?;
            let b = CString::new(target.as_os_str().as_bytes())
                .map_err(|_| Error::new("Invalid path"))?;
            if unsafe {
                libc::syscall(
                    libc::SYS_renameat2,
                    libc::AT_FDCWD,
                    a.as_ptr(),
                    libc::AT_FDCWD,
                    b.as_ptr(),
                    libc::RENAME_NOREPLACE,
                )
            } != 0
            {
                return Err(std::io::Error::last_os_error().into());
            }
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
pub fn lock(path: &Path) -> Result<File> {
    safe(path)?;
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
            let _ = child.wait();
            let _ = read.join();
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
    let output = read
        .join()
        .map_err(|_| Error::new("Command output failed"))??;
    let tail = errors.join().unwrap_or_default();
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
pub fn probe(args: &[&str], timeout: u64) -> (i32, String) {
    command(args, timeout).unwrap_or((-1, String::new()))
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
pub fn directory(path: &Path, mode: u32) -> Result<()> {
    safe(path)?;
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
}
