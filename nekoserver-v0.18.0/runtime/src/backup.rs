use crate::{
    common::{self, Error, Result},
    Args,
};
use serde_json::json;
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
pub fn archive(source: &Path, archive: &Path, apply: bool, age: u64) -> Result<u64> {
    if age < 60 {
        return Err(Error::new("Archive minimum age is 60 seconds"));
    }
    let (source, archive) = separate(source, archive)?;
    let mut copied = 0;
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
            if verified_copy(path, &target, apply, 0o644)? {
                copied += 1
            }
            Ok(())
        })?;
    }
    println!(
        "Archive: {copied} new verified originals{}",
        if apply { "" } else { " identified (dry run)" }
    );
    Ok(copied)
}
pub fn copy_tree(source: &Path, dest: &Path, apply: bool) -> Result<u64> {
    let (source, dest) = separate(source, dest)?;
    let mut copied = 0;
    walk(&source, &mut |path| {
        let name = path.file_name().unwrap_or_default().to_string_lossy();
        if name.ends_with(".neko-part") || name.ends_with(".sql.part") {
            return Ok(());
        }
        let target = dest.join(
            path.strip_prefix(&source)
                .map_err(|_| Error::new("Invalid relative path"))?,
        );
        if verified_copy(path, &target, apply, 0o644)? {
            copied += 1
        }
        Ok(())
    })?;
    println!(
        "Backup: {copied} new verified files{}",
        if apply { "" } else { " identified (dry run)" }
    );
    Ok(copied)
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
    if common::command(args, timeout)?.0 != 0 {
        return Err(Error::new("Storage operation failed"));
    }
    Ok(())
}
pub fn execute<F: FnOnce() -> Result<()>>(
    data: Option<&Path>,
    name: &str,
    cfg: &Path,
    operation: F,
) -> Result<()> {
    let Some(data) = data else { return operation() };
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
    row["status"] = json!(if result.is_ok() { "pass" } else { "fail" });
    row["finished_at"] = json!(common::now());
    if result.is_ok() {
        row["last_success"] = json!(common::now());
    }
    if !common::mounted(data) {
        return Err(Error::new("Application SSD disappeared before report"));
    }
    guard_data(cfg, data)?;
    common::atomic_json(&path, &row)?;
    result
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
pub fn preservation(a: &Args) -> Result<()> {
    common::require_root()?;
    let data = common::mount_path(a.get("data")?)?;
    let media = common::mount_path(a.get("media")?)?;
    let cfg = a.path("config")?;
    guard(&cfg, &data, &media)?;
    let _lock = common::lock(Path::new("/run/nekoserver-photo-preservation.lock"))?;
    let _recovery = common::lock(&data.join("installer/recovery.lock"))?;
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
            archive(
                &data.join("photos/immich-library"),
                &media.join(archive_dir),
                true,
                a.number("min-age", 600)?,
            )?;
            guard(&cfg, &data, &media)?;
            if a.flag("db-backup") {
                guard(&cfg, &data, &media)?;
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
                verified_copy(&created, &target, true, 0o600)?;
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
    execute(Some(&data), "external", &cfg, || {
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
        let result = (|| {
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
            copy_tree(&source, &dest, true)?;
            validate_external(&dest, uuid)?;
            guard(&cfg, &data, &media)?;
            Ok(())
        })();
        if mounted_here {
            success(&["umount", &dest.to_string_lossy()], 45)?;
        }
        result
    })
}
