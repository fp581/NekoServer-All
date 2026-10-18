//! Private, immutable media configuration snapshots on the distinct media HDD.
//! A durable resume journal is committed before stopping any application.
use crate::{
    backup,
    common::{self, Error, Result},
    Args, VERSION,
};
use serde_json::{json, Value};
use std::{
    fs::{self, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
};
const JOURNAL: &str = "/etc/nekoserver-backups/app-resume.json";
const SERVICES: [&str; 8] = [
    "jellyfin",
    "navidrome",
    "sonarr",
    "radarr",
    "lidarr",
    "prowlarr",
    "seerr",
    "qbittorrent",
];
fn docker(args: &[&str]) -> Result<String> {
    let mut command = vec!["docker", "--host", "unix:///var/run/docker.sock"];
    command.extend_from_slice(args);
    let (code, output) = common::command(&command, 120)?;
    if code != 0 {
        return Err(Error::new("Application Docker operation failed"));
    }
    Ok(output)
}
fn inspect(id: &str) -> Result<Value> {
    if id.len() != 64 || !id.bytes().all(|c| c.is_ascii_hexdigit()) {
        return Err(Error::new("Invalid container identity"));
    }
    let v: Value = serde_json::from_str(&docker(&["inspect", id])?)?;
    v.as_array()
        .filter(|a| a.len() == 1)
        .and_then(|a| a.first())
        .cloned()
        .ok_or_else(|| Error::new("Container missing"))
}
fn identity(v: &Value, service: &str, id: &str) -> bool {
    v["Id"] == id
        && v["Config"]["Labels"]["com.docker.compose.project"] == format!("neko-{service}")
        && v["Config"]["Labels"]["com.docker.compose.service"] == service
}
fn resume() -> Result<()> {
    let path = Path::new(JOURNAL);
    if !path.exists() {
        return Ok(());
    }
    let v: Value = serde_json::from_slice(&common::bytes(path, 64000)?)?;
    if v["format"] != "neko-app-resume-v1" {
        return Err(Error::new("Invalid resume journal"));
    }
    let rows = v["containers"]
        .as_array()
        .ok_or_else(|| Error::new("Invalid resume journal"))?;
    if rows.len() > 8 {
        return Err(Error::new("Invalid resume journal"));
    }
    let mut failed = false;
    for row in rows {
        let service = row["service"].as_str().unwrap_or("");
        let id = row["id"].as_str().unwrap_or("");
        let restored = (|| {
            if !SERVICES.contains(&service) {
                return Err(Error::new("Unknown application"));
            }
            let state = inspect(id)?;
            if !identity(&state, service, id) {
                return Err(Error::new("Container identity changed"));
            }
            if state["State"]["Running"] != true {
                docker(&["start", id])?;
            }
            if inspect(id)?["State"]["Running"] != true {
                return Err(Error::new("Application did not restart"));
            }
            Ok(())
        })();
        if restored.is_err() {
            failed = true;
        }
    }
    if failed {
        return Err(Error::new(
            "Applications still need recovery; journal retained",
        ));
    }
    fs::remove_file(path)?;
    fs::File::open(path.parent().unwrap())?.sync_all()?;
    Ok(())
}
pub fn resume_command(a: &Args) -> Result<()> {
    common::require_root()?;
    let _lock = common::lock(Path::new("/run/nekoserver-app-snapshot.lock"))?;
    resume()?;
    let cfgpath = a.path("config")?;
    let cfg = common::json(&cfgpath, 64000);
    let data = common::mount_path(cfg["data_mount"].as_str().unwrap_or(""))?;
    let media = common::mount_path(cfg["media_mount"].as_str().unwrap_or(""))?;
    backup::guard(&cfgpath, &data, &media)?;
    let path = data.join("reports/jobs/applications.json");
    let mut row = common::json(&path, 64000);
    if row["status"] == "running" {
        row["status"] = json!("fail");
        row["finished_at"] = json!(common::now());
        common::atomic_json(&path, &row)?;
    }
    Ok(())
}
/// Collect regular files and directories. Symlinks, sockets, FIFOs, devices and
/// nested filesystems are not application state worth restoring; they are skipped
/// and listed in the manifest rather than failing the whole night's backup.
/// Hard-linked files are copied as independent files.
fn entries(
    root: &Path,
    relative: &Path,
    device: u64,
    out: &mut Vec<Value>,
    skipped: &mut Vec<Value>,
) -> Result<()> {
    // Check the parents for symlinks; the entry itself may be one (and is then skipped).
    let path = root.join(relative);
    common::safe(path.parent().ok_or_else(|| Error::new("Invalid path"))?)?;
    let m = fs::symlink_metadata(&path)?;
    let name = relative
        .to_str()
        .ok_or_else(|| Error::new("Non-UTF8 application path refused"))?;
    let reason = if m.file_type().is_symlink() {
        Some("symlink")
    } else if m.dev() != device {
        Some("nested filesystem")
    } else if !m.is_dir() && !m.is_file() {
        Some("special file")
    } else {
        None
    };
    if let Some(reason) = reason {
        if relative.components().count() <= 2 {
            // The application folder itself must be real, or nothing is backed up.
            return Err(Error::new("Application folder is not a regular directory"));
        }
        skipped.push(json!({"path":name,"reason":reason}));
        return Ok(());
    }
    out.push(json!({"path":name,"kind":if m.is_dir(){"directory"}else{"file"},"mode":m.mode()&0o7777,"uid":m.uid(),"gid":m.gid(),"size":if m.is_file(){m.len()}else{0}}));
    if out.len() > 200000 {
        return Err(Error::new("Application snapshot file limit exceeded"));
    }
    if m.is_dir() {
        let mut children = fs::read_dir(path)?.collect::<std::io::Result<Vec<_>>>()?;
        children.sort_by_key(|p| p.file_name());
        for child in children {
            entries(
                root,
                &relative.join(child.file_name()),
                device,
                out,
                skipped,
            )?;
        }
    }
    Ok(())
}
pub(crate) fn copy_file(source: &Path, target: &Path) -> Result<String> {
    let mut input = common::file(source)?;
    let before = input.metadata()?;
    let mut output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(target)?;
    let mut buf = [0u8; 65536];
    loop {
        let count = input.read(&mut buf)?;
        if count == 0 {
            break;
        }
        output.write_all(&buf[..count])?;
    }
    output.sync_all()?;
    let after = input.metadata()?;
    if (
        before.ino(),
        before.len(),
        before.mtime(),
        before.mtime_nsec(),
    ) != (after.ino(), after.len(), after.mtime(), after.mtime_nsec())
    {
        return Err(Error::new("Application file changed during snapshot"));
    }
    let hash = common::digest(target)?;
    if common::digest(source)? != hash {
        return Err(Error::new("Application file verification failed"));
    }
    Ok(hash)
}
fn selected(cfg: &Value) -> Result<Vec<String>> {
    let items = cfg["media"]["services"]
        .as_array()
        .ok_or_else(|| Error::new("Application services required"))?;
    let mut names = vec![];
    for v in items {
        let s = v.as_str().ok_or_else(|| Error::new("Invalid service"))?;
        if !SERVICES.contains(&s) || names.contains(&s.to_string()) {
            return Err(Error::new("Unknown or repeated service"));
        }
        names.push(s.to_string());
    }
    if names.is_empty() {
        return Err(Error::new("No application services"));
    }
    Ok(names)
}
pub fn run(a: &Args) -> Result<()> {
    common::require_root()?;
    let cfgpath = a.path("config")?;
    let cfg = common::json(&cfgpath, 64000);
    let data = common::mount_path(cfg["data_mount"].as_str().unwrap_or(""))?;
    let media = common::mount_path(cfg["media_mount"].as_str().unwrap_or(""))?;
    let names = selected(&cfg)?;
    backup::guard(&cfgpath, &data, &media)?;
    let _lock = common::lock(Path::new("/run/nekoserver-app-snapshot.lock"))?;
    resume()?;
    let _recovery_lock = common::lock(&data.join("installer/recovery.lock"))?;
    backup::execute(Some(&data), "applications", &cfgpath, || {
        snapshot(&cfgpath, &data, &media, &names)
    })
}
fn snapshot(cfgpath: &Path, data: &Path, media: &Path, names: &[String]) -> Result<()> {
    let mut containers = vec![];
    let mut running = vec![];
    for name in names {
        let project = format!("label=com.docker.compose.project=neko-{name}");
        let service = format!("label=com.docker.compose.service={name}");
        let ids = docker(&[
            "ps",
            "-aq",
            "--no-trunc",
            "--filter",
            &project,
            "--filter",
            &service,
        ])?;
        let ids = ids.split_whitespace().collect::<Vec<_>>();
        if ids.len() != 1 {
            return Err(Error::new("Exactly one container per application required"));
        }
        let v = inspect(ids[0])?;
        if !identity(&v, name, ids[0])
            || v["State"]["Paused"] == true
            || v["State"]["Restarting"] == true
        {
            return Err(Error::new("Application state unsupported"));
        }
        let expected = data.join("appdata").join(name);
        let mounts = v["Mounts"]
            .as_array()
            .ok_or_else(|| Error::new("Container mounts required"))?;
        let destination = match name.as_str() {
            "seerr" => "/app/config",
            "navidrome" => "/data",
            _ => "/config",
        };
        if !mounts.iter().any(|m| {
            m["Type"] == "bind"
                && m["Source"] == expected.to_string_lossy().as_ref()
                && m["Destination"] == destination
        }) {
            return Err(Error::new("Persistent application mount missing"));
        }
        // Refuse extra writable state: it would make a backup silently incomplete.
        for m in mounts {
            if m["RW"] == true
                && !(m["Type"] == "bind"
                    && (m["Source"] == expected.to_string_lossy().as_ref()
                        || m["Source"] == media.join("library").to_string_lossy().as_ref()
                        || Path::new(m["Source"].as_str().unwrap_or(""))
                            .starts_with(data.join("library"))))
            {
                return Err(Error::new("Unmanaged writable container state"));
            }
        }
        let row = json!({"id":ids[0],"service":name,"image":v["Image"],"was_running":v["State"]["Running"]==true});
        if row["was_running"] == true {
            running.push(row.clone());
        }
        containers.push(row);
    }
    // Refuse an unselected container writing into any selected appdata subtree.
    let all = docker(&["ps", "-aq", "--no-trunc"])?;
    for id in all.split_whitespace() {
        if containers.iter().any(|r| r["id"] == id) {
            continue;
        }
        let v = inspect(id)?;
        for m in v["Mounts"]
            .as_array()
            .ok_or_else(|| Error::new("Container mounts required"))?
        {
            if m["RW"] == true {
                let source = PathBuf::from(m["Source"].as_str().unwrap_or(""));
                if names.iter().any(|n| {
                    let p = data.join("appdata").join(n);
                    source.starts_with(&p) || p.starts_with(&source)
                }) {
                    return Err(Error::new("Application storage has another writer"));
                }
            }
        }
    }
    common::directory(Path::new("/etc/nekoserver-backups"), 0o700)?;
    common::atomic_json(
        Path::new(JOURNAL),
        &json!({"format":"neko-app-resume-v1","containers":running}),
    )?;
    let result = (|| {
        for row in &running {
            docker(&["stop", "--time", "30", row["id"].as_str().unwrap()])?;
        }
        for row in &containers {
            let v = inspect(row["id"].as_str().unwrap())?;
            if v["State"]["Running"] == true {
                return Err(Error::new("Application writer still running"));
            }
            if row["was_running"] == true
                && (v["State"]["ExitCode"] == 137 || v["State"]["OOMKilled"] == true)
            {
                return Err(Error::new("Application did not shut down cleanly"));
            }
        }
        backup::guard(cfgpath, data, media)?;
        let root = media.join("backups/media-applications");
        common::directory(&root, 0o700)?;
        // Existing backup destination must also be private; do not silently chmod it.
        if fs::metadata(&root)?.mode() & 0o077 != 0 || fs::metadata(&root)?.uid() != 0 {
            return Err(Error::new("Private application backup directory required"));
        }
        let name = format!(
            "snapshot-{}-{}",
            (common::now() * 1000000.0) as u128,
            std::process::id()
        );
        let stage = root.join(format!(".{name}.partial"));
        fs::create_dir(&stage)?;
        fs::set_permissions(&stage, fs::Permissions::from_mode(0o700))?;
        let payload = stage.join("payload");
        common::directory(&payload, 0o700)?;
        let device = fs::metadata(data)?.dev();
        let mut files = vec![];
        let mut skipped = vec![];
        for service in names {
            for folder in ["appdata", "compose"] {
                entries(
                    data,
                    &PathBuf::from(folder).join(service),
                    device,
                    &mut files,
                    &mut skipped,
                )?;
            }
        }
        if data.join("installer/releases").is_dir() {
            entries(
                data,
                Path::new("installer/releases"),
                device,
                &mut files,
                &mut skipped,
            )?;
        }
        for row in &mut files {
            let rel = PathBuf::from(row["path"].as_str().unwrap());
            let target = payload.join(&rel);
            if row["kind"] == "directory" {
                common::directory(&target, 0o700)?;
            } else {
                common::directory(target.parent().unwrap(), 0o700)?;
                row["sha256"] = json!(copy_file(&data.join(&rel), &target)?);
            }
        }
        for row in files.iter().rev().filter(|row| row["kind"] == "directory") {
            fs::File::open(payload.join(row["path"].as_str().unwrap()))?.sync_all()?;
        }
        fs::File::open(&payload)?.sync_all()?;
        backup::guard(cfgpath, data, media)?;
        common::atomic_json(
            &stage.join("manifest.json"),
            &json!({"format":"neko-media-snapshot-v1","version":VERSION,"created_at":common::now(),"containers":containers,"files":files,"skipped":skipped,"storage":common::json(cfgpath,64000)["storage"]}),
        )?;
        fs::File::open(&stage)?.sync_all()?;
        let target = root.join(name);
        // Linux atomic directory publication without replacing any prior backup.
        let from = std::ffi::CString::new(stage.as_os_str().as_encoded_bytes())
            .map_err(|_| Error::new("Invalid snapshot path"))?;
        let to = std::ffi::CString::new(target.as_os_str().as_encoded_bytes())
            .map_err(|_| Error::new("Invalid snapshot path"))?;
        if unsafe {
            libc::syscall(
                libc::SYS_renameat2,
                libc::AT_FDCWD,
                from.as_ptr(),
                libc::AT_FDCWD,
                to.as_ptr(),
                libc::RENAME_NOREPLACE,
            )
        } != 0
        {
            return Err(std::io::Error::last_os_error().into());
        }
        fs::File::open(&root)?.sync_all()?;
        let keep = common::json(cfgpath, 64000)["app_backup"]["keep_snapshots"]
            .as_u64()
            .unwrap_or(14);
        common::prune_snapshots(&root, keep)?;
        Ok(())
    })();
    let restarted = resume();
    result.and(restarted)
}
#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::DirBuilderExt;
    fn dir(path: &Path) {
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(path)
            .unwrap();
    }
    fn scratch(name: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!("neko-app-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        dir(&root);
        root
    }
    #[test]
    fn unusual_entries_are_skipped_not_fatal() {
        let root = scratch("skip");
        let app = root.join("appdata/sonarr");
        dir(&app.join("logs"));
        fs::write(app.join("config.xml"), b"<Config/>").unwrap();
        fs::write(app.join("logs/sonarr.txt"), b"log").unwrap();
        std::os::unix::fs::symlink("/etc/passwd", app.join("link")).unwrap();
        fs::hard_link(app.join("config.xml"), app.join("config.hardlink")).unwrap();
        let socket = std::os::unix::net::UnixListener::bind(app.join("ipc.sock")).unwrap();
        let device = fs::metadata(&root).unwrap().dev();
        let (mut files, mut skipped) = (vec![], vec![]);
        entries(
            &root,
            Path::new("appdata/sonarr"),
            device,
            &mut files,
            &mut skipped,
        )
        .unwrap();
        let names: Vec<&str> = files.iter().map(|f| f["path"].as_str().unwrap()).collect();
        assert!(names.contains(&"appdata/sonarr/config.xml"));
        assert!(names.contains(&"appdata/sonarr/config.hardlink"));
        assert!(names.contains(&"appdata/sonarr/logs/sonarr.txt"));
        assert!(!names
            .iter()
            .any(|n| n.ends_with("link") && !n.ends_with("hardlink")));
        assert_eq!(
            skipped,
            vec![
                json!({"path":"appdata/sonarr/ipc.sock","reason":"special file"}),
                json!({"path":"appdata/sonarr/link","reason":"symlink"}),
            ]
        );
        drop(socket);
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn symlinked_application_folder_is_still_refused() {
        let root = scratch("top");
        dir(&root.join("real"));
        dir(&root.join("appdata"));
        std::os::unix::fs::symlink(root.join("real"), root.join("appdata/sonarr")).unwrap();
        let device = fs::metadata(&root).unwrap().dev();
        let (mut files, mut skipped) = (vec![], vec![]);
        assert!(entries(
            &root,
            Path::new("appdata/sonarr"),
            device,
            &mut files,
            &mut skipped
        )
        .is_err());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn nightly_retention_keeps_newest_complete_snapshots() {
        let root = scratch("prune");
        for name in [
            "snapshot-100-1",
            "snapshot-900-1",
            "snapshot-1000-1",
            "snapshot-50-1",
            ".snapshot-10-1.partial",
            "snapshot-20-1",
            "notes",
        ] {
            dir(&root.join(name));
            if name != "snapshot-20-1" {
                fs::write(root.join(name).join("manifest.json"), b"{}").unwrap();
            }
        }
        let removed = common::prune_snapshots(&root, 2).unwrap();
        assert_eq!(removed, ["snapshot-50-1", "snapshot-100-1"]);
        let mut left: Vec<String> = fs::read_dir(&root)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().to_string())
            .collect();
        left.sort();
        // Newest two complete ones, plus everything that is not a complete snapshot.
        assert_eq!(
            left,
            [
                ".snapshot-10-1.partial",
                "notes",
                "snapshot-1000-1",
                "snapshot-20-1",
                "snapshot-900-1"
            ]
        );
        assert!(common::prune_snapshots(&root, 0).unwrap().is_empty());
        fs::remove_dir_all(root).unwrap();
    }
}
