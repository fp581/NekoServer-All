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
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
};
pub(crate) const JOURNAL: &str = "/etc/nekoserver-backups/app-resume.json";
const LOCK: &str = "/run/nekoserver-app-snapshot.lock";
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
            // Cut off while the apps were being stopped: a stop Docker is still finishing
            // would end after the start and leave the app stopped (Docker does not restart
            // an app it was asked to stop). The stop finishes first (nothing to do for an
            // app already stopped), then the app starts.
            docker(&["stop", "--time", "60", id])?;
            docker(&["start", id])?;
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
    backup::finish_journal(path)
}
pub fn resume_command(a: &Args) -> Result<()> {
    let job = backup::open_job(a, 64000)?;
    let report = job.data.join("reports/jobs/applications.json");
    // Nothing to finish (the usual case: right after a backup that ended well, and at
    // every boot): no lock is waited for, so this never waits on another job.
    if !backup::needs_resume(Path::new(JOURNAL), &report) {
        return Ok(());
    }
    // Another resume may be at work (the one run as the cut-off job stopped): wait for
    // it rather than fail. The service allows for the 3-hour wait for the recovery lock
    // below (neko_common.RESUME_TIMEOUT).
    let _lock = common::lock_wait(Path::new(LOCK), std::time::Duration::from_secs(15 * 60))?;
    let _recovery_lock = backup::coordinate(&job.cfgpath, &job.data, &job.media)?;
    resume()?;
    backup::mark_interrupted(&report)
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
    // When it was last changed, so a restore can give that time back (as host snapshots do).
    out.push(json!({"path":name,"kind":if m.is_dir(){"directory"}else{"file"},"mode":m.mode()&0o7777,"uid":m.uid(),"gid":m.gid(),"size":if m.is_file(){m.len()}else{0},"mtime":m.mtime(),"mtime_nsec":m.mtime_nsec()}));
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
/// Everything the snapshot copies, listed in a fixed order.
fn listing(
    data: &Path,
    names: &[String],
    device: u64,
    files: &mut Vec<Value>,
    skipped: &mut Vec<Value>,
) -> Result<()> {
    for service in names {
        for folder in ["appdata", "compose"] {
            entries(
                data,
                &PathBuf::from(folder).join(service),
                device,
                files,
                skipped,
            )?;
        }
    }
    if data.join("installer/releases").is_dir() {
        entries(
            data,
            Path::new("installer/releases"),
            device,
            files,
            skipped,
        )?;
    }
    Ok(())
}
fn unchanged_since_listing(
    data: &Path,
    names: &[String],
    device: u64,
    files: &[Value],
    skipped: &[Value],
) -> Result<()> {
    let (mut again, mut skipped_again) = (vec![], vec![]);
    listing(data, names, device, &mut again, &mut skipped_again)?;
    let strip = |rows: &[Value]| -> Vec<Value> {
        rows.iter()
            .map(|r| {
                let mut r = r.clone();
                if let Some(m) = r.as_object_mut() {
                    m.remove("sha256");
                }
                r
            })
            .collect()
    };
    if strip(files) != again || skipped != skipped_again.as_slice() {
        let changed = files
            .iter()
            .zip(&again)
            .find(|(a, b)| strip(&[(*a).clone()])[0] != **b)
            .map(|(a, _)| a["path"].as_str().unwrap_or("").to_string())
            .unwrap_or_else(|| "a file was added or removed".into());
        return Err(Error::new(&format!(
            "Application files changed while the snapshot was made ({changed}); it was not published and is retried next night"
        )));
    }
    Ok(())
}
/// The staged copy must match its own manifest (size and checksum of every file)
/// before it is published, exactly as the restore check will read it.
fn verify_staged(payload: &Path, files: &[Value]) -> Result<()> {
    for row in files.iter().filter(|r| r["kind"] == "file") {
        let path = payload.join(row["path"].as_str().unwrap_or(""));
        let size = fs::symlink_metadata(&path)?.len();
        if Some(size) != row["size"].as_u64()
            || common::digest(&path)? != row["sha256"].as_str().unwrap_or("")
        {
            return Err(Error::new(&format!(
                "Staged application snapshot does not match its manifest ({}); not published",
                row["path"].as_str().unwrap_or("")
            )));
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
    let job = backup::open_job(a, 64000)?;
    let names = selected(&job.cfg)?;
    let _lock = common::lock(Path::new(LOCK))?;
    let _recovery_lock = backup::coordinate(&job.cfgpath, &job.data, &job.media)?;
    resume()?;
    let result = backup::execute(Some(&job.data), "applications", &job.cfgpath, || {
        backup::no_unfinished_update()?;
        snapshot(&job.cfgpath, &job.data, &job.media, &names)
    });
    if result.is_ok() {
        record_docker_version(&job.data);
    }
    result
}
/// The host checkpoint, setup's record for taking the apps over after Linux is
/// reinstalled, names the Docker version they must run with. The OS's own updates move
/// Docker on, so each nightly backup that passed brings the record up to date (best
/// effort: the backup's result never depends on it).
fn record_docker_version(data: &Path) {
    let path = data.join("installer/host-checkpoint.json");
    let Ok(info) = docker(&["info", "--format", "{{.ServerVersion}} {{.DockerRootDir}}"]) else {
        return;
    };
    let (version, root) = info.trim().split_once(' ').unwrap_or(("", ""));
    if let Some(updated) = current_checkpoint(&common::json(&path, 100_000), version, root) {
        match common::atomic_json(&path, &updated) {
            Ok(()) => println!("Recovery checkpoint: Docker {version} recorded (the version a reinstalled Linux needs to take the apps over)"),
            Err(e) => println!("Recovery checkpoint: could not record Docker {version} ({e})"),
        }
    }
}
/// The checkpoint with the running Docker version, when it describes this Docker store
/// and names another version; None: nothing to change (or no checkpoint at all).
fn current_checkpoint(saved: &Value, version: &str, root: &str) -> Option<Value> {
    // A package suffix is part of the version (openSUSE's Docker says "29.7.2_ce").
    let plausible = !version.is_empty()
        && version
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b".-+~_".contains(&c));
    if saved["format"] != "neko-host-checkpoint-v1"
        || saved["docker_root"] != root
        || !plausible
        || saved["docker_version"] == version
    {
        return None;
    }
    let mut updated = saved.clone();
    updated["docker_version"] = json!(version);
    Some(updated)
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
        let stage = backup::stage_snapshot(&root)?;
        let payload = &stage.payload;
        let device = fs::metadata(data)?.dev();
        let mut files = vec![];
        let mut skipped = vec![];
        listing(data, names, device, &mut files, &mut skipped)?;
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
        // Containers are stopped, but installer and settings files can still have
        // other writers. List everything again: a file that changed, appeared or went
        // away since the first listing makes this snapshot inconsistent.
        unchanged_since_listing(data, names, device, &files, &skipped)?;
        verify_staged(payload, &files)?;
        let manifest = json!({"format":"neko-media-snapshot-v1","version":VERSION,"created_at":common::now(),"containers":containers,"files":files,"skipped":skipped,"storage":common::json(cfgpath,64000)["storage"]});
        let keep = common::json(cfgpath, 64000)["app_backup"]["keep_snapshots"]
            .as_u64()
            .unwrap_or(7);
        backup::publish_snapshot(
            &root,
            &stage,
            &files,
            || backup::guard(cfgpath, data, media),
            &manifest,
            keep,
            None,
        )
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
    fn the_checkpoint_follows_the_docker_the_os_updated_to() {
        let saved = json!({"format":"neko-host-checkpoint-v1","docker_version":"29.9.2","docker_root":"/srv/nekoserver/docker-engine","uid":1000});
        let updated =
            current_checkpoint(&saved, "30.1.0", "/srv/nekoserver/docker-engine").unwrap();
        assert_eq!(updated["docker_version"], "30.1.0");
        // Everything else is kept.
        assert_eq!(updated["uid"], 1000);
        // Same version, another store, no checkpoint or an odd answer: nothing written.
        assert!(current_checkpoint(&saved, "29.9.2", "/srv/nekoserver/docker-engine").is_none());
        assert!(current_checkpoint(&saved, "30.1.0", "/var/lib/docker").is_none());
        assert!(
            current_checkpoint(&json!({}), "30.1.0", "/srv/nekoserver/docker-engine").is_none()
        );
        assert!(current_checkpoint(&saved, "", "/srv/nekoserver/docker-engine").is_none());
        assert!(current_checkpoint(&saved, "30 1", "/srv/nekoserver/docker-engine").is_none());
        // openSUSE's package suffix is kept as it is.
        assert_eq!(
            current_checkpoint(&saved, "29.7.2_ce", "/srv/nekoserver/docker-engine").unwrap()
                ["docker_version"],
            "29.7.2_ce"
        );
    }
    #[test]
    fn a_file_changed_after_listing_is_never_published() {
        let data = scratch("changed-after-listing");
        dir(&data.join("appdata/jellyfin"));
        dir(&data.join("compose/jellyfin"));
        fs::write(data.join("appdata/jellyfin/settings.xml"), b"a").unwrap();
        let names = vec!["jellyfin".to_string()];
        let device = fs::metadata(&data).unwrap().dev();
        let (mut files, mut skipped) = (vec![], vec![]);
        listing(&data, &names, device, &mut files, &mut skipped).unwrap();
        // Each entry keeps when it was last changed, for the restore to give back.
        let changed = fs::metadata(data.join("appdata/jellyfin/settings.xml")).unwrap();
        let row = files
            .iter()
            .find(|r| r["path"] == "appdata/jellyfin/settings.xml")
            .unwrap();
        assert_eq!(
            (row["mtime"].as_i64(), row["mtime_nsec"].as_i64()),
            (Some(changed.mtime()), Some(changed.mtime_nsec()))
        );
        assert!(files
            .iter()
            .all(|r| r["mtime"].is_i64() && r["mtime_nsec"].is_i64()));
        // Another program rewrites the file before it is copied: 1 byte -> 18 bytes.
        fs::write(
            data.join("appdata/jellyfin/settings.xml"),
            b"rewritten settings",
        )
        .unwrap();
        let payload = data.join("stage");
        for row in &mut files {
            let rel = PathBuf::from(row["path"].as_str().unwrap());
            if row["kind"] == "directory" {
                dir(&payload.join(&rel));
            } else {
                row["sha256"] = json!(copy_file(&data.join(&rel), &payload.join(&rel)).unwrap());
            }
        }
        let error = unchanged_since_listing(&data, &names, device, &files, &skipped)
            .unwrap_err()
            .to_string();
        assert!(error.contains("settings.xml"), "{error}");
        let error = verify_staged(&payload, &files).unwrap_err().to_string();
        assert!(error.contains("does not match its manifest"), "{error}");
        // Nothing changed: both checks pass.
        let (mut files, mut skipped) = (vec![], vec![]);
        fs::remove_dir_all(&payload).unwrap();
        listing(&data, &names, device, &mut files, &mut skipped).unwrap();
        for row in &mut files {
            let rel = PathBuf::from(row["path"].as_str().unwrap());
            if row["kind"] == "directory" {
                dir(&payload.join(&rel));
            } else {
                row["sha256"] = json!(copy_file(&data.join(&rel), &payload.join(&rel)).unwrap());
            }
        }
        unchanged_since_listing(&data, &names, device, &files, &skipped).unwrap();
        verify_staged(&payload, &files).unwrap();
        let _ = fs::remove_dir_all(data);
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
        let removed = common::prune_snapshots(&root, 2, None, None).unwrap();
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
        assert!(common::prune_snapshots(&root, 0, None, None)
            .unwrap()
            .is_empty());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn a_clock_jump_never_deletes_the_snapshot_just_made() {
        // Keep 1; a snapshot dated far in the future exists; today's sorts as oldest.
        let root = scratch("clock");
        for name in ["snapshot-99999999999999999-1", "snapshot-1000-7"] {
            dir(&root.join(name));
            fs::write(root.join(name).join("manifest.json"), b"{}").unwrap();
        }
        let removed = common::prune_snapshots(&root, 1, Some("snapshot-1000-7"), None).unwrap();
        assert_eq!(removed, ["snapshot-99999999999999999-1"]);
        assert!(root.join("snapshot-1000-7/manifest.json").is_file());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn a_snapshot_dated_ahead_counts_as_the_oldest() {
        // Keep 3: one made while the clock was far ahead must not outlive the real ones
        // (it would look newest for years, leaving a single real backup).
        let root = scratch("ahead");
        for name in [
            "snapshot-100-1",
            "snapshot-200-1",
            "snapshot-300-1",
            "snapshot-99999999999999999-1",
            "snapshot-400-1",
        ] {
            dir(&root.join(name));
            fs::write(root.join(name).join("manifest.json"), b"{}").unwrap();
        }
        let removed = common::prune_snapshots(&root, 3, Some("snapshot-400-1"), None).unwrap();
        assert_eq!(removed, ["snapshot-99999999999999999-1", "snapshot-100-1"]);
        assert!(root.join("snapshot-200-1").is_dir() && root.join("snapshot-300-1").is_dir());
        fs::remove_dir_all(root).unwrap();
    }
}
