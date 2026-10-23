//! Monthly cold snapshots, isolated restored-stack probes and recoverable image updates.
//! The journal on the OS disk survives process death and SSD application restores.
use crate::{
    backup,
    common::{self, Error, Result},
    media, Args, VERSION,
};
use serde_json::{json, Value};
use std::{
    fs,
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
    thread,
    time::{Duration, Instant},
};
pub(crate) const JOURNAL: &str = "/etc/nekoserver-backups/maintenance-resume.json";
const MEDIA: [&str; 8] = [
    "jellyfin",
    "navidrome",
    "sonarr",
    "radarr",
    "lidarr",
    "prowlarr",
    "seerr",
    "qbittorrent",
];
fn checked(args: &[&str], seconds: u64) -> Result<String> {
    let (code, output) = common::command(args, seconds)?;
    if code != 0 {
        return Err(common::failed(
            args,
            code,
            "Maintenance step failed; private journal retained",
        ));
    }
    Ok(output)
}
fn docker(args: &[&str]) -> Result<String> {
    let mut all = vec!["docker", "--host", "unix:///var/run/docker.sock"];
    all.extend_from_slice(args);
    checked(&all, 1800)
}
fn compose(file: &Path, project: &str, args: &[&str]) -> Result<String> {
    let file = file.to_string_lossy();
    let mut all = vec!["compose", "-p", project, "-f", &file];
    all.extend_from_slice(args);
    docker(&all)
}
fn load(path: &Path) -> Result<Value> {
    Ok(serde_json::from_slice(&common::bytes(path, 2_000_000)?)?)
}
fn string<'a>(v: &'a Value, key: &str) -> Result<&'a str> {
    v[key]
        .as_str()
        .ok_or_else(|| Error::new("Missing transaction field"))
}
/// Owner required for private maintenance state: root in production. Unit tests run
/// unprivileged in CI, so they expect the current user instead.
#[cfg(not(test))]
fn state_owner() -> u32 {
    0
}
#[cfg(test)]
fn state_owner() -> u32 {
    unsafe { libc::geteuid() }
}
fn private(path: &Path) -> Result<()> {
    let m = fs::metadata(common::safe(path)?)?;
    if m.uid() != state_owner() || m.mode() & 0o077 != 0 {
        return Err(Error::new("Private root-owned maintenance state required"));
    }
    Ok(())
}
fn names(cfg: &Value) -> Result<Vec<String>> {
    let mut names = vec![];
    for value in cfg["media"]["services"]
        .as_array()
        .cloned()
        .unwrap_or_default()
    {
        let n = value
            .as_str()
            .ok_or_else(|| Error::new("Invalid media selection"))?;
        if !MEDIA.contains(&n) || names.contains(&n.to_string()) {
            return Err(Error::new("Invalid media selection"));
        }
        names.push(n.to_string());
    }
    if cfg["photos"]["deploy_immich"] == true {
        names.push("immich".into());
    }
    if cfg["nextcloud"]["enabled"] == true {
        names.push("nextcloud".into());
    }
    if names.is_empty() {
        return Err(Error::new("No maintenance applications selected"));
    }
    Ok(names)
}
fn roots(names: &[String]) -> (Vec<String>, Vec<String>) {
    let mut data = vec![];
    let media = vec![];
    for name in names {
        data.extend([format!("appdata/{name}"), format!("compose/{name}")]);
    }
    // Photos and Nextcloud files are not in the monthly snapshot: they get
    // add-only weekly copies (HDD and USB for photos, USB for Nextcloud files).
    // Updates change the databases in appdata, which the snapshot does cover.
    // App password files are never archived in plain text: see encrypt_secrets.
    (data, media)
}
/// What data.tar and media.tar hold. Without a backup key the app password list
/// (secrets/accounts) goes into data.tar too, root-only and disclosed in the manifest
/// ("unencrypted_in_data_tar"): no encrypted archive is written then, and leaving it
/// out would leave the snapshot without it at all.
fn snapshot_paths(names: &[String], data: &Path, sealing: bool) -> (Vec<String>, Vec<String>) {
    let mut paths = roots(names);
    if !sealing && data.join("secrets/accounts").is_dir() {
        paths.0.push("secrets/accounts".to_string());
    }
    paths
}
/// The size of the images the selected apps' containers use, each image counted once
/// (several containers may share one; other containers on this Docker are not ours).
fn image_bytes(names: &[String]) -> Result<u64> {
    let mut images = std::collections::BTreeMap::new();
    for name in names {
        let label = format!("label=com.docker.compose.project=neko-{name}");
        let ids = docker(&["ps", "-aq", "--no-trunc", "--filter", &label])?;
        for id in ids.split_whitespace() {
            let v: Value = serde_json::from_str(&docker(&["inspect", id])?)?;
            let image = string(&v[0], "Image")?.to_string();
            if let std::collections::btree_map::Entry::Vacant(slot) = images.entry(image) {
                let meta: Value =
                    serde_json::from_str(&docker(&["image", "inspect", slot.key()])?)?;
                slot.insert(meta[0]["Size"].as_u64().unwrap_or(1 << 30));
            }
        }
    }
    Ok(images
        .values()
        .fold(0u64, |sum, size| sum.saturating_add(*size)))
}
/// (SSD, HDD) bytes a monthly run needs, before the reserve. The HDD holds the snapshot
/// (app data, media paths and the saved images), the unpacked restore test and, for a
/// rollback, an unpacked copy of the media paths. The SSD holds the rollback copy of the
/// app data and the new images the update downloads. 1 GiB to spare on each.
fn space_needed(app: u64, media: u64, images: u64) -> (u64, u64) {
    let spare = 1u64 << 30;
    let ssd = app.saturating_add(images).saturating_add(spare);
    let hdd = app
        .saturating_add(media)
        .saturating_add(images)
        .saturating_add(app.saturating_add(media))
        .saturating_add(media)
        .saturating_add(spare);
    (ssd, hdd)
}
fn free(path: &Path) -> Result<u64> {
    let name = std::ffi::CString::new(path.as_os_str().as_encoded_bytes())
        .map_err(|_| Error::new("Invalid path"))?;
    let mut s = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    if unsafe { libc::statvfs(name.as_ptr(), s.as_mut_ptr()) } != 0 {
        return Err(Error::new("Cannot check free space"));
    }
    let s = unsafe { s.assume_init() };
    Ok(s.f_bavail.saturating_mul(s.f_frsize))
}
fn size(root: &Path, paths: &[String]) -> Result<u64> {
    if paths.is_empty() {
        return Ok(0);
    }
    let mut args = vec!["du", "-s", "-B1", "--apparent-size", "--"];
    let all = paths
        .iter()
        .map(|p| root.join(p).to_string_lossy().into_owned())
        .collect::<Vec<_>>();
    args.extend(all.iter().map(String::as_str));
    checked(&args, 1800)?.lines().try_fold(0u64, |total, line| {
        let n = line
            .split_whitespace()
            .next()
            .and_then(|s| s.parse::<u64>().ok())
            .ok_or_else(|| Error::new("Invalid source size"))?;
        total
            .checked_add(n)
            .ok_or_else(|| Error::new("Source size overflow"))
    })
}
fn night(cfg: &Value) -> bool {
    let stamp = unsafe { libc::time(std::ptr::null_mut()) };
    let mut value = std::mem::MaybeUninit::<libc::tm>::uninit();
    if unsafe { libc::localtime_r(&stamp, value.as_mut_ptr()) }.is_null() {
        return false;
    }
    let hour = unsafe { value.assume_init() }.tm_hour;
    (0..cfg["maintenance"]["night_end_hour"].as_i64().unwrap_or(6)).contains(&(hour as i64))
}
fn mounts(cfgpath: &Path, cfg: &Value, external: bool) -> Result<()> {
    let data = common::mount_path(string(cfg, "data_mount")?)?;
    let media = common::mount_path(string(cfg, "media_mount")?)?;
    backup::guard(cfgpath, &data, &media)?;
    if external {
        let ext = common::mount_path(string(cfg, "external_backup_mount")?)?;
        let uuid = cfg["photos"]["external_backup_uuid"].as_str().unwrap_or("");
        if uuid.is_empty()
            || [
                cfg["storage"]["data_ssd"]["uuid"].as_str().unwrap_or(""),
                cfg["storage"]["media_hdd"]["uuid"].as_str().unwrap_or(""),
            ]
            .contains(&uuid)
        {
            return Err(Error::new("Distinct external UUID required"));
        }
        backup::validate_external(&ext, uuid)?;
    }
    Ok(())
}
/// Optional per-app Compose addition written by the installer's hardware video step
/// (graphics device for Immich). Kept next to compose.yaml and applied on every update.
const HARDWARE: &str = "hardware.yaml";
fn model(config: &Value, name: &str) -> Result<Value> {
    let dir = Path::new(string(config, "data_mount")?)
        .join("compose")
        .join(name);
    let manifest = common::safe(&dir.join("compose.yaml"))?;
    let file = manifest.to_string_lossy();
    let project = format!("neko-{name}");
    let env = dir.join(".env");
    let envs = env.to_string_lossy();
    let mut args = vec!["compose", "-p", &project, "-f", &file];
    if env.exists() {
        private(&env)?;
        args.extend(["--env-file", &envs]);
    }
    let hardware = dir.join(HARDWARE);
    let hardware_name = hardware.to_string_lossy();
    if hardware.exists() {
        private(&hardware)?;
        args.extend(["-f", &hardware_name]);
    }
    let override_file = dir.join("maintenance-images.json");
    let override_name = override_file.to_string_lossy();
    if override_file.exists() {
        private(&override_file)?;
        args.extend(["-f", &override_name]);
    }
    args.extend(["config", "--format", "json"]);
    Ok(serde_json::from_str(&docker(&args)?)?)
}
fn expected_service(name: &str, service: &str) -> bool {
    match name {
        "immich" => [
            "immich-server",
            "immich-machine-learning",
            "database",
            "redis",
        ]
        .contains(&service),
        "nextcloud" => ["nextcloud", "cron", "database", "redis"].contains(&service),
        _ => service == name && MEDIA.contains(&name),
    }
}
fn image_id(s: &str) -> bool {
    s.strip_prefix("sha256:")
        .is_some_and(|s| s.len() == 64 && s.bytes().all(|c| c.is_ascii_hexdigit()))
}
fn valid_model(model: &Value, name: &str, data: &Path, media: &Path) -> Result<()> {
    let services = model["services"]
        .as_object()
        .ok_or_else(|| Error::new("Compose services missing"))?;
    let expected = if ["immich", "nextcloud"].contains(&name) {
        4
    } else {
        1
    };
    if services.len() != expected {
        return Err(Error::new(
            "Unexpected services; automated maintenance refused",
        ));
    }
    for (service, v) in services {
        if !expected_service(name, service)
            || v["privileged"] == true
            || v.get("network_mode").is_some()
            || v.get("pid").is_some()
            || v.get("ipc").is_some()
            || v.get("build").is_some()
        {
            return Err(Error::new("Unsupported Compose isolation"));
        }
        for volume in v["volumes"].as_array().cloned().unwrap_or_default() {
            if volume["type"] == "bind" {
                let source = Path::new(string(&volume, "source")?);
                let p = if source == Path::new("/etc/localtime") && volume["read_only"] == true {
                    source.to_path_buf()
                } else {
                    common::safe(source)?
                };
                let allowed = p == data.join("appdata").join(name)
                    || p.starts_with(data.join("appdata").join(name));
                let photo = name == "immich" && p == data.join("photos/immich-library");
                let cloud = name == "nextcloud" && p == media.join("cloud/files");
                let library = p == media.join("library")
                    || (name == "jellyfin"
                        && ["movies", "tv"]
                            .iter()
                            .any(|n| p == media.join("library").join(n)))
                    || (name == "navidrome" && p == data.join("library/music"))
                    || (name == "lidarr" && p == data.join("library"))
                    || (name == "qbittorrent" && p == data.join("library/downloads/music"));
                let localtime = p == Path::new("/etc/localtime") && volume["read_only"] == true;
                if !(allowed || photo || cloud || library || localtime) {
                    return Err(Error::new("Uncovered container bind mount"));
                }
            } else if volume["type"] != "volume"
                || !(service == "redis" || service == "immich-machine-learning")
            {
                return Err(Error::new("Uncovered persistent volume"));
            }
        }
        if service == name || service == "immich-server" {
            let ports = v["ports"]
                .as_array()
                .ok_or_else(|| Error::new("Application port missing"))?;
            let peer = peer_port(service, v);
            let public = |p: &Value| p["host_ip"] != "127.0.0.1" && !is_peer_binding(p, peer);
            if ports.is_empty() || ports.iter().any(public) {
                return Err(Error::new("Loopback application ports required"));
            }
        } else if v["ports"].as_array().is_some_and(|p| !p.is_empty()) {
            return Err(Error::new("Unexpected supporting service publication"));
        }
    }
    Ok(())
}
/// qBittorrent's optional peer port (TORRENTING_PORT), the one publication allowed
/// beyond loopback so other peers can connect in.
fn peer_port(service: &str, v: &Value) -> Option<u16> {
    if service != "qbittorrent" {
        return None;
    }
    let env = &v["environment"];
    let value = env["TORRENTING_PORT"]
        .as_str()
        .map(str::to_string)
        .or_else(|| {
            env.as_array()?.iter().find_map(|e| {
                e.as_str()?
                    .strip_prefix("TORRENTING_PORT=")
                    .map(str::to_string)
            })
        })?;
    value.parse::<u16>().ok().filter(|p| *p >= 1024)
}
fn is_peer_binding(p: &Value, peer: Option<u16>) -> bool {
    let Some(peer) = peer else {
        return false;
    };
    let host = p["host_ip"].as_str().unwrap_or("");
    let published = p["published"]
        .as_str()
        .map(str::to_string)
        .or_else(|| p["published"].as_u64().map(|n| n.to_string()));
    ["", "0.0.0.0", "::"].contains(&host)
        && p["target"].as_u64() == Some(peer as u64)
        && published.as_deref() == Some(peer.to_string().as_str())
        && ["tcp", "udp"].contains(&p["protocol"].as_str().unwrap_or("tcp"))
}
fn containers(name: &str, model: &mut Value) -> Result<Vec<Value>> {
    let mut out = vec![];
    for (service, v) in model["services"]
        .as_object_mut()
        .ok_or_else(|| Error::new("Missing services"))?
    {
        let project = format!("label=com.docker.compose.project=neko-{name}");
        let label = format!("label=com.docker.compose.service={service}");
        let ids = docker(&[
            "ps",
            "-aq",
            "--no-trunc",
            "--filter",
            &project,
            "--filter",
            &label,
        ])?;
        let ids = ids.split_whitespace().collect::<Vec<_>>();
        if ids.len() != 1 {
            return Err(Error::new(
                "Exactly one managed container per service required",
            ));
        }
        let info: Value = serde_json::from_str(&docker(&["inspect", ids[0]])?)?;
        let c = &info[0];
        if c["State"]["Running"] != true
            || c["State"]["Paused"] == true
            || c["State"]["Restarting"] == true
            || c["Config"]["Labels"]["com.docker.compose.project"] != format!("neko-{name}")
            || c["Config"]["Labels"]["com.docker.compose.service"] != *service
            || !image_id(c["Image"].as_str().unwrap_or(""))
        {
            return Err(Error::new(
                "Managed application is not in a healthy running baseline",
            ));
        }
        // The running model must really use the declared persistent mounts.
        for volume in v["volumes"]
            .as_array()
            .cloned()
            .unwrap_or_default()
            .iter()
            .filter(|m| m["type"] == "bind")
        {
            if !c["Mounts"].as_array().is_some_and(|mounts| {
                mounts.iter().any(|m| {
                    m["Type"] == "bind"
                        && m["Source"] == volume["source"]
                        && m["Destination"] == volume["target"]
                })
            }) {
                return Err(Error::new("Running persistent mounts differ from Compose"));
            }
        }
        out.push(json!({"id":ids[0],"project":format!("neko-{name}"),"service":service}));
        v["image"] = c["Image"].clone();
    }
    Ok(out)
}
fn stop(rows: &[Value]) -> Result<()> {
    // Stop frontends/cron before databases, then verify clean exits.
    let mut rows = rows.to_vec();
    rows.sort_by_key(|r| r["service"] == "database");
    for row in &rows {
        docker(&["stop", "--time", "60", string(row, "id")?])?;
    }
    for row in &rows {
        let info: Value = serde_json::from_str(&docker(&["inspect", string(row, "id")?])?)?;
        let state = &info[0]["State"];
        let why = if state["Running"] == true {
            Some("still running after 60 seconds".to_string())
        } else if state["OOMKilled"] == true {
            Some("killed for running out of memory".to_string())
        } else if state["ExitCode"] == 137 {
            Some("did not stop within 60 seconds and was killed (exit code 137)".to_string())
        } else {
            None
        };
        if let Some(why) = why {
            return Err(Error::new(&format!(
                "The app {} ({}) did not shut down cleanly: it {}. The update is refused so the snapshot stays consistent; the apps are started again. See its log with  docker logs {}",
                string(row, "service")?,
                string(row, "project")?,
                why,
                string(row, "id")?
            )));
        }
    }
    Ok(())
}
fn no_other_writers(
    rows: &[Value],
    data: &Path,
    media: &Path,
    paths: &(Vec<String>, Vec<String>),
) -> Result<()> {
    let ids = docker(&["ps", "-q", "--no-trunc"])?;
    let protected = paths
        .0
        .iter()
        .map(|p| data.join(p))
        .chain(paths.1.iter().map(|p| media.join(p)))
        .collect::<Vec<_>>();
    for id in ids
        .split_whitespace()
        .filter(|id| !rows.iter().any(|r| r["id"] == *id))
    {
        let v: Value = serde_json::from_str(&docker(&["inspect", id])?)?;
        for mount in v[0]["Mounts"].as_array().cloned().unwrap_or_default() {
            if mount["RW"] == true {
                let p = PathBuf::from(mount["Source"].as_str().unwrap_or("/"));
                if protected
                    .iter()
                    .any(|root| p.starts_with(root) || root.starts_with(&p))
                {
                    return Err(Error::new("Unmanaged writer overlaps backup"));
                }
            }
        }
    }
    Ok(())
}
fn tar(root: &Path, paths: &[String], output: &Path, exclude: &[&str]) -> Result<()> {
    let root = root.to_string_lossy();
    let output = output.to_string_lossy();
    let mut args = vec![
        "tar",
        "--create",
        "--file",
        &output,
        "--acls",
        "--xattrs",
        "--one-file-system",
        "-C",
        &root,
        "--",
    ];
    let excluded: Vec<String> = exclude.iter().map(|e| format!("--exclude={e}")).collect();
    // Exclusions go before the -C/-- part so tar applies them to every path.
    let at = args
        .iter()
        .position(|a| *a == "--one-file-system")
        .unwrap_or(args.len());
    for (offset, e) in excluded.iter().enumerate() {
        args.insert(at + offset, e);
    }
    if paths.is_empty() {
        args.pop();
        args.extend(["--files-from", "/dev/null"]);
    } else {
        args.extend(paths.iter().map(String::as_str));
    }
    checked(&args, 21600)?;
    fs::File::open(Path::new(output.as_ref()))?.sync_all()?;
    Ok(())
}
const SECRETS_KEY_DIR: &str = "/etc/nekoserver-backups";
const SECRETS_KEY: &str = "secrets-key.p8";
const SECRETS_CERT: &str = "secrets-recipient.pem";
const SECRETS_ARCHIVE: &str = "secrets.tar.cms";
/// Store secrets/accounts only as a CMS envelope for the backup certificate. The tar
/// stream is piped straight into openssl, so no plain-text copy reaches the backup
/// drive. The password-protected key travels with the snapshot for restores.
/// Returns "encrypted", "none" (no password files) or, without a backup key,
/// "unencrypted_in_data_tar": then data.tar keeps them (see SEALED_EXCLUDES), readable
/// by root only, and the snapshot's manifest says so.
/// Files that hold passwords or secret keys in plain text: the app password list, each
/// app's Compose `.env` (database and Nextcloud admin passwords) and Nextcloud's
/// config.php (database password, secret). With a backup key they are kept only in the
/// encrypted archive; data.tar leaves them out.
const SEALED_EXCLUDES: [&str; 2] = ["compose/*/.env", "appdata/nextcloud/html/config/config.php"];
fn sealed_members(data: &Path) -> Vec<String> {
    let mut out = vec![];
    if data.join("secrets/accounts").exists() {
        out.push("secrets/accounts".to_string());
    }
    let mut envs: Vec<String> = fs::read_dir(data.join("compose"))
        .into_iter()
        .flatten()
        .flatten()
        .filter(|e| e.path().join(".env").is_file())
        .map(|e| format!("compose/{}/.env", e.file_name().to_string_lossy()))
        .collect();
    envs.sort();
    out.extend(envs);
    if data.join(SEALED_EXCLUDES[1]).is_file() {
        out.push(SEALED_EXCLUDES[1].to_string());
    }
    out
}
/// Private per-run folder on the data SSD for files an update needs but the portable
/// snapshot must not hold in plain text: a copy of the sealed config files (so an
/// automatic rollback restores them without the sudo password) and the Immich
/// candidate settings. The live copies of these files sit on the same SSD already.
/// Removed once the update or its rollback is over.
fn work_dir(data: &Path, run: &str) -> PathBuf {
    data.join("backups/maintenance-work").join(run)
}
const SEALED_COPY: &str = "sealed-config.tar";
/// Keep the sealed files of this run on the SSD for an unattended rollback; returns its checksum.
fn save_sealed_copy(data: &Path, work: &Path) -> Result<Option<String>> {
    let members: Vec<String> = sealed_members(data)
        .into_iter()
        .filter(|m| m != "secrets/accounts")
        .collect();
    if members.is_empty() {
        return Ok(None);
    }
    let output = work.join(SEALED_COPY);
    tar(data, &members, &output, &[])?;
    fs::set_permissions(
        &output,
        <fs::Permissions as std::os::unix::fs::PermissionsExt>::from_mode(0o600),
    )?;
    Ok(Some(common::digest(&output)?))
}
/// Put the sealed config files back into a rollback stage before the old apps restart.
fn restore_sealed_copy(journal: &Value, data: &Path, stage: &Path) -> Result<()> {
    let Some(expected) = journal["sealed_copy"].as_str() else {
        return Ok(()); // nothing was left out of data.tar
    };
    let copy = work_dir(data, string(journal, "run")?).join(SEALED_COPY);
    if !copy.is_file() || common::digest(&copy)? != expected {
        return Err(Error::new(
            "Rollback stopped: the saved copy of the app password files is missing or damaged. \
             Restore them with monthly-recovery.py --secrets (see MONTHLY-MAINTENANCE.md)",
        ));
    }
    for action in ["--extract", "--compare"] {
        let mut args = vec!["tar", action, "--file"];
        let file = copy.to_string_lossy();
        let dir = stage.to_string_lossy();
        args.push(&file);
        if action == "--extract" {
            args.extend(["--acls", "--xattrs", "--numeric-owner", "--same-owner"]);
        }
        args.extend(["-C", &dir]);
        checked(&args, 3600)?;
    }
    Ok(())
}
/// Remove the work folders of finished runs (all but the run an unfinished update uses).
fn clear_work(data: &Path) {
    let active = load(Path::new(JOURNAL))
        .ok()
        .and_then(|j| j["run"].as_str().map(str::to_string));
    let root = data.join("backups/maintenance-work");
    for entry in fs::read_dir(&root).into_iter().flatten().flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        if Some(&name) != active.as_ref() && entry.file_type().is_ok_and(|t| t.is_dir()) {
            if let Err(e) = common::safe(&entry.path()).and_then(|p| Ok(fs::remove_dir_all(p)?)) {
                println!("Monthly maintenance: could not remove old work folder {name} ({e})");
            }
        }
    }
}
fn secrets_key_ready(key_dir: &Path) -> bool {
    key_dir.join(SECRETS_KEY).is_file() && key_dir.join(SECRETS_CERT).is_file()
}
fn encrypt_secrets(data: &Path, snapshot: &Path) -> Result<&'static str> {
    encrypt_secrets_with(data, snapshot, Path::new(SECRETS_KEY_DIR))
}
fn encrypt_secrets_with(data: &Path, snapshot: &Path, key_dir: &Path) -> Result<&'static str> {
    let members = sealed_members(data);
    if members.is_empty() {
        return Ok("none");
    }
    let key = key_dir.join(SECRETS_KEY);
    let cert = key_dir.join(SECRETS_CERT);
    if !key.is_file() || !cert.is_file() {
        return Ok("unencrypted_in_data_tar");
    }
    private(&key)?;
    private(&cert)?;
    let output = snapshot.join(SECRETS_ARCHIVE);
    let mut archive = std::process::Command::new("tar")
        .args(["--create", "--file", "-", "--acls", "--xattrs", "-C"])
        .arg(data)
        .arg("--")
        .args(&members)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .spawn()?;
    let stream = archive
        .stdout
        .take()
        .ok_or_else(|| Error::new("Password archive stream unavailable"))?;
    let sealed = std::process::Command::new("openssl")
        .args([
            "cms", "-encrypt", "-binary", "-aes256", "-outform", "DER", "-out",
        ])
        .arg(&output)
        .arg(&cert)
        .stdin(stream)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()?;
    let archived = archive.wait()?;
    if !archived.success() || !sealed.success() {
        let _ = fs::remove_file(&output);
        return Err(Error::new("Could not encrypt the app password files"));
    }
    fs::set_permissions(
        &output,
        <fs::Permissions as std::os::unix::fs::PermissionsExt>::from_mode(0o600),
    )?;
    fs::File::open(&output)?.sync_all()?;
    for (source, name) in [(&key, SECRETS_KEY), (&cert, SECRETS_CERT)] {
        common::atomic(&snapshot.join(name), &common::bytes(source, 64_000)?, 0o600)?;
    }
    Ok("encrypted")
}
pub(crate) fn verify(snapshot: &Path) -> Result<Value> {
    private(snapshot)?;
    let manifest = load(&snapshot.join("manifest.json"))?;
    if manifest["format"] != "neko-maintenance-v1" {
        return Err(Error::new("Unknown snapshot"));
    }
    for key in ["data", "media", "images"] {
        let archive = snapshot.join(format!("{key}.tar"));
        if common::digest(&archive)? != manifest["sha256"][key].as_str().unwrap_or("") {
            return Err(Error::new("Snapshot archive checksum mismatch"));
        }
    }
    for (key, name, what) in [
        ("secrets", SECRETS_ARCHIVE, "password archive"),
        ("secrets_key", SECRETS_KEY, "backup key copy"),
        ("secrets_cert", SECRETS_CERT, "backup certificate"),
    ] {
        if let Some(expected) = manifest["sha256"][key].as_str() {
            if common::digest(&snapshot.join(name))? != expected {
                return Err(Error::new(&format!("Snapshot {what} checksum mismatch")));
            }
        }
    }
    Ok(manifest)
}
/// Unpack (and compare) the named archives of a snapshot whose checksums the caller has
/// just verified. Done once: a finished extraction is marked and reused after a crash.
fn extract(snapshot: &Path, target: &Path, keys: &[&str]) -> Result<()> {
    if target.exists() {
        private(target)?;
        if target.join("extraction-complete.json").exists() {
            return Ok(());
        }
        fs::remove_dir_all(common::safe(target)?)?;
    }
    common::directory(target, 0o700)?;
    for key in keys {
        let dir = target.join(key);
        common::directory(&dir, 0o700)?;
        checked(
            &[
                "tar",
                "--extract",
                "--file",
                &snapshot.join(format!("{key}.tar")).to_string_lossy(),
                "--acls",
                "--xattrs",
                "--numeric-owner",
                "--same-owner",
                "-C",
                &dir.to_string_lossy(),
            ],
            21600,
        )?;
        checked(
            &[
                "tar",
                "--compare",
                "--file",
                &snapshot.join(format!("{key}.tar")).to_string_lossy(),
                "-C",
                &dir.to_string_lossy(),
            ],
            21600,
        )?;
    }
    common::atomic_json(
        &target.join("extraction-complete.json"),
        &json!({"status":"pass"}),
    )?;
    Ok(())
}
/// `host`: a Host header to send instead of the address in the URL (qBittorrent refuses
/// a request whose Host carries another port than its own, and the restore test reaches it
/// on a port chosen for the test).
fn curl_as(url: &str, output: Option<&Path>, host: Option<&str>) -> Result<String> {
    let header;
    let mut args = vec![
        "curl",
        "--disable",
        "--noproxy",
        "*",
        "--silent",
        "--fail",
        "--max-time",
        "15",
        "--max-redirs",
        "0",
    ];
    let out = output.map(|p| p.to_string_lossy().into_owned());
    if let Some(p) = &out {
        args.extend(["--output", p]);
    }
    if let Some(h) = host {
        header = format!("Host: {h}");
        args.extend(["--header", &header]);
    }
    args.push(url);
    checked(&args, 20)
}
fn app_port(name: &str) -> Result<(u16, &'static str)> {
    Ok(match name {
        "jellyfin" => (8096, "/health"),
        "navidrome" => (4533, "/ping"),
        "sonarr" => (8989, "/ping"),
        "radarr" => (7878, "/ping"),
        "lidarr" => (8686, "/ping"),
        "prowlarr" => (9696, "/ping"),
        // Not /api/v1/status: that makes Seerr ask GitHub whether an update exists, and on
        // the restore test's network (no way out) the question hangs until the probe's
        // own time limit. Its public settings need nothing from outside.
        "seerr" => (5055, "/api/v1/settings/public"),
        "qbittorrent" => (8080, "/"),
        "immich" => (2283, "/api/server/ping"),
        "nextcloud" => (80, "/status.php"),
        _ => return Err(Error::new("Unknown application probe")),
    })
}
fn health(
    file: &Path,
    project: &str,
    name: &str,
    wait: u64,
    rehearsal: bool,
    scratch: &Path,
) -> Result<()> {
    let service = if name == "immich" {
        "immich-server"
    } else {
        name
    };
    let (port, path) = app_port(name)?;
    let start = Instant::now();
    loop {
        let attempt = (|| {
            let ids = compose(file, project, &["ps", "-q"])?;
            let mut count = 0;
            for id in ids.split_whitespace() {
                let info: Value = serde_json::from_str(&docker(&["inspect", id])?)?;
                let state = &info[0]["State"];
                if state["Running"] != true
                    || state["Restarting"] == true
                    || ["unhealthy", "starting"]
                        .contains(&state["Health"]["Status"].as_str().unwrap_or(""))
                {
                    return Err(Error::new("Container not healthy"));
                }
                count += 1;
            }
            // Every service of this model (a quarantined Nextcloud runs without its cron).
            let expected = load(file)?["services"]
                .as_object()
                .map_or(0, serde_json::Map::len);
            if count != expected {
                return Err(Error::new("Missing healthy services"));
            }
            let binding = compose(file, project, &["port", service, &port.to_string()])?;
            let binding = binding.trim();
            let published = binding
                .strip_prefix("127.0.0.1:")
                .filter(|s| s.parse::<u16>().is_ok())
                .ok_or_else(|| Error::new("Private health port required"))?;
            let own = format!("127.0.0.1:{port}");
            let host = (name == "qbittorrent").then_some(own.as_str());
            let body = curl_as(&format!("http://127.0.0.1:{published}{path}"), None, host)?;
            if name == "nextcloud" {
                let s: Value = serde_json::from_str(&body)?;
                if s["installed"] != true
                    || s["maintenance"] != false
                    || s["needsDbUpgrade"] != false
                {
                    return Err(Error::new("Nextcloud migration incomplete"));
                }
                // Authenticated real database/file operations, without storing a user's password.
                // A new name each attempt, written down before the file is made. A test
                // file a cut-off attempt left behind is removed by the next one: only a
                // name on that list, and only while it still holds what NekoServer wrote
                // (its own name), never a file of the user's that merely looks alike.
                let token = format!(
                    "neko-maintenance-{}-{}",
                    std::process::id(),
                    (common::now() * 1e6) as u128
                );
                let earlier = probe_names(scratch, &token)?;
                let script = probe_script(&earlier, &token);
                // Use the recorded initial administrator username instead of assuming it is admin.
                let cfg = load(&scratch.join("config.json"))?;
                let user = cfg["nextcloud"]["admin_user"].as_str().unwrap_or("admin");
                let script = script.replace(
                    "get(\"admin\")",
                    &format!("get({})", serde_json::to_string(user)?),
                );
                compose(
                    file,
                    project,
                    &[
                        "exec",
                        "-T",
                        "--user",
                        "www-data",
                        "nextcloud",
                        "php",
                        "-r",
                        &script,
                    ],
                )?;
            }
            if name == "immich" {
                let result = compose(
                    file,
                    project,
                    &[
                        "exec",
                        "-T",
                        "database",
                        "sh",
                        "-c",
                        "psql -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" -Atc 'SELECT 1'",
                    ],
                )?;
                if result.trim() != "1" {
                    return Err(Error::new("Restored Immich database probe failed"));
                }
            }
            if rehearsal {
                common::atomic_json(
                    &scratch.join(format!("{name}-restore-test.json")),
                    &json!({"status":"pass","database_and_files":name=="nextcloud","database_sql":name=="immich","http":true}),
                )?;
            }
            Ok(())
        })();
        if attempt.is_ok() {
            return Ok(());
        }
        if start.elapsed() >= Duration::from_secs(wait) {
            return attempt;
        }
        thread::sleep(Duration::from_secs(5));
    }
}
fn restored_model(original: &Value, stage: &Path, data: &Path, media: &Path) -> Result<Value> {
    let mut model = original.clone();
    model
        .as_object_mut()
        .ok_or_else(|| Error::new("Invalid model"))?
        .remove("name");
    // Docker publishes no port for a container on an "internal" network, and the health
    // check needs the restored app's published port. A bridge without IP masquerading
    // keeps the restored apps cut off from the outside (their packets get no way back)
    // while this PC can reach the port.
    model["networks"] = json!({"restore":{"driver_opts":{"com.docker.network.bridge.enable_ip_masquerade":"false"}}});
    model.as_object_mut().unwrap().remove("volumes");
    for (service, v) in model["services"].as_object_mut().unwrap() {
        let map = v.as_object_mut().unwrap();
        for key in [
            "container_name",
            "devices",
            "deploy",
            "networks",
            "restart",
            "labels",
            "extra_hosts",
        ] {
            map.remove(key);
        }
        map.insert("networks".into(), json!(["restore"]));
        map.insert("restart".into(), json!("no"));
        if let Some(ports) = map.get_mut("ports").and_then(Value::as_array_mut) {
            for p in ports {
                p["published"] = json!("0");
                p["host_ip"] = json!("127.0.0.1");
            }
        }
        // Only a service that has volumes is touched: indexing a missing key would
        // insert "volumes": null, which Compose rejects ("must be a array").
        let listed = v
            .get_mut("volumes")
            .and_then(Value::as_array_mut)
            .into_iter()
            .flatten();
        for volume in listed {
            if volume["type"] == "bind" {
                let source = Path::new(string(volume, "source")?);
                let target = if let Ok(relative) = source.strip_prefix(data) {
                    stage.join("data").join(relative)
                } else if let Ok(relative) = source.strip_prefix(media) {
                    stage.join("media").join(relative)
                } else if source == Path::new("/etc/localtime") {
                    continue;
                } else {
                    return Err(Error::new("Unsafe restored bind mount"));
                };
                if !target.exists() {
                    common::directory(&target, 0o755)?;
                    volume["read_only"] = json!(true);
                }
                volume["source"] = json!(target);
            } else {
                // Cache volumes are intentionally recreated in the rehearsal's private namespace.
                let name = format!("cache-{service}");
                volume["source"] = json!(name);
            }
        }
    }
    let mut volumes = serde_json::Map::new();
    for v in model["services"].as_object().unwrap().values() {
        for m in v["volumes"].as_array().into_iter().flatten() {
            if m["type"] == "volume" {
                volumes.insert(string(m, "source")?.to_string(), json!({}));
            }
        }
    }
    if !volumes.is_empty() {
        model["volumes"] = Value::Object(volumes);
    }
    Ok(model)
}
/// Apps whose user data (photos, Nextcloud files) is kept out of the monthly
/// snapshot, and a table that proves their restored database is the real one.
/// The Nextcloud test files of this run so far (in the snapshot folder), with `token`
/// added before its file is made; returns the earlier ones.
fn probe_names(scratch: &Path, token: &str) -> Result<Vec<String>> {
    let path = scratch.join("nextcloud-probes.json");
    let mut names: Vec<String> = common::json(&path, 64000)["names"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|v| v.as_str())
        .filter(|n| probe_name(n))
        .map(str::to_string)
        .collect();
    let earlier = names.clone();
    names.push(token.to_string());
    let keep = names.len().saturating_sub(50);
    common::atomic_json(&path, &json!({"names": names[keep..]}))?;
    Ok(earlier)
}
fn probe_name(name: &str) -> bool {
    name.strip_prefix("neko-maintenance-").is_some_and(|rest| {
        !rest.is_empty() && rest.bytes().all(|b| b.is_ascii_digit() || b == b'-')
    })
}
/// The PHP that writes, reads back and removes one test file in the administrator's
/// files (the trash paused), after removing this run's earlier test files that are still
/// exactly what NekoServer wrote.
fn probe_script(earlier: &[String], token: &str) -> String {
    let list = earlier
        .iter()
        .filter(|n| probe_name(n))
        .map(|n| format!("'{n}'"))
        .collect::<Vec<_>>()
        .join(",");
    format!("require '/var/www/html/lib/base.php'; $u=\\OC::$server->getUserManager()->get(\"admin\"); if(!$u){{exit(3);}} $root=\\OC::$server->getUserFolder($u->getUID()); $t=null; try{{$t=\\OC::$server->get(\\OCA\\Files_Trashbin\\Trash\\ITrashManager::class); $t->pauseTrash();}}catch(\\Throwable $e){{$t=null;}} foreach([{list}] as $old){{if($root->nodeExists($old)){{$o=$root->get($old); if($o instanceof \\OCP\\Files\\File && $o->getContent()===$old){{$o->delete();}}}}}} $n='{token}'; if($root->nodeExists($n)){{exit(4);}} $f=$root->newFile($n); $f->putContent('{token}'); if($f->getContent()!=='{token}'){{exit(5);}} $f->delete(); if($t){{$t->resumeTrash();}}")
}
fn database_only_table(name: &str) -> Option<&'static str> {
    match name {
        "immich" => Some("asset"),
        "nextcloud" => Some("oc_filecache"),
        _ => None,
    }
}
fn database_probe(
    file: &Path,
    project: &str,
    name: &str,
    table: &str,
    wait: u64,
    report: &Path,
) -> Result<()> {
    // Immich v2 calls the table "asset"; v1 called it "assets".
    let query = format!(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name IN ('{table}','{table}s')"
    );
    let command = format!("psql -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" -Atc \"{query}\"");
    let start = Instant::now();
    loop {
        let attempt = compose(
            file,
            project,
            &["exec", "-T", "database", "sh", "-c", &command],
        )
        .and_then(|out| {
            if out.trim().parse::<u32>().unwrap_or(0) >= 1 {
                Ok(())
            } else {
                Err(Error::new(
                    "Restored database is missing its application tables",
                ))
            }
        });
        if attempt.is_ok() {
            return common::atomic_json(
                &report.join(format!("{name}-restore-test.json")),
                &json!({"status":"pass","database_sql":true,"table":table,"http":false,"files":"not in monthly snapshot; weekly add-only copies"}),
            );
        }
        if start.elapsed() >= Duration::from_secs(wait) {
            return attempt;
        }
        thread::sleep(Duration::from_secs(5));
    }
}
/// Restore the snapshot into throwaway containers and prove it works. The unpacked test
/// copy is removed whatever the result, so a failed test never leaves a second copy of
/// the apps' data on the HDD.
fn rehearsal(snapshot: &Path, cfg: &Value, models: &Value, report: &Path) -> Result<()> {
    let stage = snapshot.join("restore-test");
    let result = rehearse(snapshot, &stage, cfg, models, report);
    let tidy = if stage.exists() {
        fs::remove_dir_all(common::safe(&stage)?).map_err(Error::from)
    } else {
        Ok(())
    };
    result.and(tidy)
}
fn rehearse(
    snapshot: &Path,
    stage: &Path,
    cfg: &Value,
    models: &Value,
    report: &Path,
) -> Result<()> {
    extract(snapshot, stage, &["data", "media"])?;
    let data = Path::new(string(cfg, "data_mount")?);
    let media = Path::new(string(cfg, "media_mount")?);
    for (name, original) in models.as_object().unwrap() {
        let clone = restored_model(original, stage, data, media)?;
        let file = report.join(format!("{name}-rehearsal.json"));
        common::atomic_json(&file, &clone)?;
        let project = format!("neko-rehearsal-{name}");
        // Record cleanup before creating any containers so interruption recovery can remove only these projects.
        let mut journal = load(Path::new(JOURNAL))?;
        journal["rehearsals"]
            .as_array_mut()
            .unwrap()
            .push(json!({"file":file,"project":project}));
        common::atomic_json(Path::new(JOURNAL), &journal)?;
        let wait = cfg["maintenance"]["health_wait_seconds"]
            .as_u64()
            .unwrap_or(300);
        let result = if let Some(table) = database_only_table(name) {
            // The snapshot holds this app's database but not its photos/files, so the
            // full app cannot start from it. Prove the restored database instead.
            compose(
                &file,
                &project,
                &["up", "-d", "--pull", "never", "database"],
            )
            .and_then(|_| database_probe(&file, &project, name, table, wait, report))
        } else {
            compose(&file, &project, &["up", "-d", "--pull", "never"])
                .and_then(|_| health(&file, &project, name, wait, true, report))
        };
        let down = compose(&file, &project, &["down", "--volumes", "--remove-orphans"]);
        result.and(down)?;
    }
    Ok(())
}
fn save_journal(value: &Value) -> Result<()> {
    common::atomic_json(Path::new(JOURNAL), value)
}
fn resume(cfgpath: &Path, cfg: &Value) -> Result<()> {
    if !Path::new(JOURNAL).exists() {
        return Ok(());
    }
    private(Path::new(JOURNAL))?;
    let mut journal = load(Path::new(JOURNAL))?;
    if journal["format"] != "neko-maintenance-resume-v1"
        || journal["version"] != VERSION
        || journal["storage"] != cfg["storage"]
        || journal["data_mount"] != cfg["data_mount"]
        || journal["media_mount"] != cfg["media_mount"]
    {
        return Err(Error::new("Maintenance recovery identity mismatch"));
    }
    mounts(cfgpath, cfg, false)?;
    for r in journal["rehearsals"]
        .as_array()
        .cloned()
        .unwrap_or_default()
    {
        compose(
            Path::new(string(&r, "file")?),
            string(&r, "project")?,
            &["down", "--volumes", "--remove-orphans"],
        )?;
    }
    let snapshot = PathBuf::from(string(&journal, "snapshot")?);
    let phase = string(&journal, "phase")?.to_string();
    if phase == "committed" {
        // The update stands: finish opening the apps up, never roll back.
        let names: Vec<String> = journal["models"]
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect();
        let wait = cfg["maintenance"]["health_wait_seconds"]
            .as_u64()
            .unwrap_or(300);
        open_up(
            &names,
            &snapshot,
            &|file, name| live(file, name),
            &|file, name| health(file, &format!("neko-{name}"), name, wait, false, &snapshot),
        )?;
        fs::remove_file(JOURNAL)?;
        fs::File::open(Path::new(JOURNAL).parent().unwrap())?.sync_all()?;
        scrub_all(Path::new(string(cfg, "media_mount")?));
        clear_work(Path::new(string(cfg, "data_mount")?));
        return Ok(());
    }
    if ["updating", "restoring"].contains(&phase.as_str()) {
        let snapshot_manifest = verify(&snapshot)?;
        for (name, _) in journal["models"].as_object().unwrap() {
            let file = snapshot.join(format!("{name}-old.json"));
            compose(&file, &format!("neko-{name}"), &["stop", "--timeout", "60"])?;
        }
        if phase == "updating" {
            // The app data is unpacked straight onto the SSD below; the HDD stage only
            // ever serves media paths, so it is unpacked only when the snapshot has any.
            let has_media = snapshot_manifest["paths"]["media"]
                .as_array()
                .is_some_and(|paths| !paths.is_empty());
            if has_media {
                extract(&snapshot, &snapshot.join("rollback-stage"), &["media"])?;
            }
            let ssd_stage = Path::new(string(cfg, "data_mount")?)
                .join("backups")
                .join(format!("rollback-{}", string(&journal, "run")?));
            common::directory(&ssd_stage, 0o700)?;
            checked(
                &[
                    "tar",
                    "--extract",
                    "--file",
                    &snapshot.join("data.tar").to_string_lossy(),
                    "--acls",
                    "--xattrs",
                    "--numeric-owner",
                    "--same-owner",
                    "-C",
                    &ssd_stage.to_string_lossy(),
                ],
                21600,
            )?;
            checked(
                &[
                    "tar",
                    "--compare",
                    "--file",
                    &snapshot.join("data.tar").to_string_lossy(),
                    "-C",
                    &ssd_stage.to_string_lossy(),
                ],
                21600,
            )?;
            restore_sealed_copy(&journal, Path::new(string(cfg, "data_mount")?), &ssd_stage)?;
            journal["phase"] = json!("restoring");
            journal["restored"] = json!([]);
            save_journal(&journal)?;
        }
        let mut allowed = roots(&names(cfg)?);
        let has_accounts = snapshot_manifest["paths"]["data"]
            .as_array()
            .is_some_and(|paths| paths.iter().any(|p| p == "secrets/accounts"));
        // A snapshot made without a backup key holds the password list in data.tar.
        if has_accounts {
            allowed.0.push("secrets/accounts".to_string());
        }
        for (kind, root, paths) in [
            ("data", Path::new(string(cfg, "data_mount")?), allowed.0),
            ("media", Path::new(string(cfg, "media_mount")?), allowed.1),
        ] {
            for relative in paths {
                let key = format!("{kind}/{relative}");
                if journal["restored"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|v| v == &key)
                {
                    continue;
                }
                let live = common::safe(&root.join(&relative))?;
                let stage = if kind == "data" {
                    root.join("backups")
                        .join(format!("rollback-{}", string(&journal, "run")?))
                } else {
                    snapshot.join("rollback-stage/media")
                };
                let source = common::safe(&stage.join(&relative))?;
                let quarantine = live.with_file_name(format!(
                    "{}.neko-failed-{}",
                    live.file_name().unwrap().to_string_lossy(),
                    string(&journal, "run")?
                ));
                if source.exists() {
                    if live.exists() && !quarantine.exists() {
                        fs::rename(&live, &quarantine)?;
                        fs::File::open(live.parent().unwrap())?.sync_all()?;
                    }
                    if live.exists() {
                        return Err(Error::new("Rollback destination collision"));
                    }
                    fs::rename(&source, &live)?;
                    fs::File::open(live.parent().unwrap())?.sync_all()?;
                } else if !live.exists() || !quarantine.exists() {
                    return Err(Error::new("Rollback state incomplete"));
                }
                journal["restored"].as_array_mut().unwrap().push(json!(key));
                save_journal(&journal)?;
            }
        }
        for (name, model) in journal["models"].as_object().unwrap() {
            write_override(cfg, name, model)?;
        }
    }
    for (name, _) in journal["models"].as_object().unwrap() {
        let file = snapshot.join(format!("{name}-old.json"));
        compose(
            &file,
            &format!("neko-{name}"),
            &["up", "-d", "--pull", "never"],
        )?;
    }
    if phase != "snapshot" {
        for (name, _) in journal["models"].as_object().unwrap() {
            let file = snapshot.join(format!("{name}-old.json"));
            health(
                &file,
                &format!("neko-{name}"),
                name,
                cfg["maintenance"]["health_wait_seconds"]
                    .as_u64()
                    .unwrap_or(300),
                false,
                &snapshot,
            )?;
        }
    }
    if phase != "snapshot" && phase != "backed_up" {
        // Rolled back and healthy: the stages the old data came from are spent.
        let stage = Path::new(string(cfg, "data_mount")?)
            .join("backups")
            .join(format!("rollback-{}", string(&journal, "run")?));
        for spent in [snapshot.join("rollback-stage"), stage] {
            if spent.exists() {
                fs::remove_dir_all(common::safe(&spent)?)?;
            }
        }
    }
    fs::remove_file(JOURNAL)?;
    fs::File::open(Path::new(JOURNAL).parent().unwrap())?.sync_all()?;
    scrub_all(Path::new(string(cfg, "media_mount")?));
    clear_work(Path::new(string(cfg, "data_mount")?));
    Ok(())
}
/// Environment names that carry a password or secret.
fn secret_name(name: &str) -> bool {
    let upper = name.to_ascii_uppercase();
    ["PASSWORD", "SECRET", "TOKEN", "PASS", "KEY"]
        .iter()
        .any(|w| upper.contains(w))
}
const REMOVED: &str = "<removed: kept in the encrypted secrets archive>";
/// Blank password values in a saved app definition (Compose `environment` maps or
/// `NAME=value` lists), leaving everything else.
fn redact(value: &mut Value) {
    match value {
        Value::Object(map) => {
            for (key, item) in map.iter_mut() {
                if key == "environment" {
                    redact_environment(item);
                } else {
                    redact(item);
                }
            }
        }
        Value::Array(items) => items.iter_mut().for_each(redact),
        _ => {}
    }
}
fn redact_environment(env: &mut Value) {
    match env {
        Value::Object(map) => {
            for (key, item) in map.iter_mut() {
                if secret_name(key) && item.is_string() {
                    *item = json!(REMOVED);
                }
            }
        }
        Value::Array(items) => {
            for item in items.iter_mut() {
                let name = item
                    .as_str()
                    .and_then(|s| s.split_once('='))
                    .map(|(k, _)| k.to_string());
                if let Some(name) = name.filter(|k| secret_name(k)) {
                    *item = json!(format!("{name}={REMOVED}"));
                }
            }
        }
        _ => {}
    }
}
/// Once an update (or its rollback) is over, the saved app definitions in a monthly
/// snapshot no longer need their resolved passwords: blank them so the snapshot holds
/// passwords only inside the encrypted archive. Not used while a rollback may need them.
fn scrub_snapshot(snapshot: &Path) -> Result<()> {
    // Releases up to 0.24.2 left the Immich candidate settings (database password
    // included) in the snapshot; the update is over, so they are not needed.
    let candidate_env = snapshot.join("immich-candidate/.env");
    if fs::symlink_metadata(&candidate_env).is_ok_and(|m| m.is_file()) {
        fs::remove_file(&candidate_env)?;
    }
    for entry in fs::read_dir(common::safe(snapshot)?)? {
        let path = entry?.path();
        let name = path
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .to_string();
        let model = name.ends_with("-old.json")
            || name.ends_with("-new.json")
            || name.ends_with("-quarantine.json")
            || name.ends_with("-rehearsal.json")
            || name == "result.json";
        if !model || !fs::symlink_metadata(&path)?.is_file() {
            continue;
        }
        let mut value: Value = serde_json::from_slice(&common::bytes(&path, 50_000_000)?)?;
        let before = value.clone();
        redact(&mut value);
        if value != before {
            common::atomic_json(&path, &value)?;
        }
    }
    Ok(())
}
/// Scrub every monthly snapshot no unfinished update still depends on (older releases
/// left resolved passwords in them). Best effort: never fails a finished update.
fn scrub_all(media: &Path) {
    let active = load(Path::new(JOURNAL)).ok();
    let hdd = media.join("backups/monthly");
    for run in monthly_runs(media).unwrap_or_default() {
        let in_use = active.as_ref().is_some_and(|j| {
            j["snapshot"]
                .as_str()
                .is_some_and(|s| Path::new(s).file_name().is_some_and(|f| f == run.as_str()))
        });
        if !in_use {
            if let Err(e) = scrub_snapshot(&hdd.join(&run)) {
                println!("Monthly snapshot {run}: could not blank saved passwords ({e})");
            }
        }
    }
}
/// A run can fail before its recovery journal exists (an app's settings or a health
/// check before the snapshot), with its saved definitions, passwords included, already
/// written. Nothing will roll it back, so they are blanked now; a run a journal still
/// needs is left alone (scrub_all and clear_work skip it).
fn tidy_after_failure(cfg: &Value) {
    if let Ok(media) = string(cfg, "media_mount") {
        scrub_all(Path::new(media));
    }
    if let Ok(data) = string(cfg, "data_mount") {
        clear_work(Path::new(data));
    }
}
fn write_override(cfg: &Value, name: &str, model: &Value) -> Result<()> {
    let services = model["services"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(k, v)| (k.clone(), json!({"image":v["image"]})))
        .collect::<serde_json::Map<_, _>>();
    common::atomic_json(
        &Path::new(string(cfg, "data_mount")?)
            .join("compose")
            .join(name)
            .join("maintenance-images.json"),
        &json!({"services":services}),
    )
}
fn source_model(folder: &Path, name: &str) -> Result<Value> {
    let manifest = folder.join("compose.yaml");
    let file = manifest.to_string_lossy();
    let project = format!("neko-{name}");
    let env = folder.join(".env");
    let env_name = env.to_string_lossy();
    let mut args = vec!["compose", "-p", &project, "-f", &file];
    if env.exists() {
        private(&env)?;
        args.extend(["--env-file", &env_name]);
    }
    let hardware = folder.join(HARDWARE);
    let hardware_name = hardware.to_string_lossy();
    if hardware.exists() {
        private(&hardware)?;
        args.extend(["-f", &hardware_name]);
    }
    args.extend(["config", "--format", "json"]);
    Ok(serde_json::from_str(&docker(&args)?)?)
}
fn release_fetch(url: &str) -> Result<String> {
    checked(
        &[
            "curl",
            "--disable",
            "--silent",
            "--show-error",
            "--fail",
            "--location",
            "--max-redirs",
            "3",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--max-time",
            "60",
            "--max-filesize",
            "500000",
            "--user-agent",
            "NekoServer-maintenance",
            url,
        ],
        70,
    )
}
fn immich_candidate(cfg: &Value, snapshot: &Path, work: &Path) -> Result<Option<PathBuf>> {
    let original = Path::new(string(cfg, "data_mount")?).join("compose/immich");
    let env = String::from_utf8(common::bytes(&original.join(".env"), 64000)?)
        .map_err(|_| Error::new("Invalid Immich environment"))?;
    let current = env
        .lines()
        .find_map(|line| line.strip_prefix("IMMICH_VERSION="))
        .ok_or_else(|| Error::new("Pinned Immich release missing"))?;
    let version = regex::Regex::new(r"^v([0-9]+)\.([0-9]+)\.([0-9]+)$").unwrap();
    let before = version
        .captures(current)
        .ok_or_else(|| Error::new("Invalid installed Immich version"))?;
    let release: Value = serde_json::from_str(&release_fetch(
        "https://api.github.com/repos/immich-app/immich/releases/latest",
    )?)?;
    let tag = string(&release, "tag_name")?;
    let next = version
        .captures(tag)
        .ok_or_else(|| Error::new("Invalid stable Immich release"))?;
    if release["prerelease"] == true || release["draft"] == true {
        return Err(Error::new("Unstable Immich release refused"));
    }
    // A different major is installed only when you approved exactly this release.
    let wanted = cfg["maintenance"]["immich_major_update"]
        .as_str()
        .unwrap_or("")
        .trim();
    let approved = immich_major_approved(wanted, tag, &next[1]);
    if before[1] != next[1] && !approved {
        common::atomic_json(
            &snapshot.join("immich-major-pending.json"),
            &json!({"status":"manual_major_upgrade_required","current":current,"available":tag}),
        )?;
        return Ok(None);
    }
    let parts = |c: &regex::Captures<'_>| -> Result<Vec<u64>> {
        (1..=3)
            .map(|i| {
                c[i].parse()
                    .map_err(|_| Error::new("Invalid version number"))
            })
            .collect()
    };
    if parts(&next)? <= parts(&before)? {
        return Ok(None);
    }
    let manifest = release_fetch(&format!(
        "https://github.com/immich-app/immich/releases/download/{tag}/docker-compose.yml"
    ))?;
    let mut count = 0;
    let mut lines = vec![];
    for line in manifest.lines() {
        let indent = &line[..line.len() - line.trim_start().len()];
        let target = match line.trim() {
            "- 2283:2283" => Some("- 127.0.0.1:2283:2283"),
            "- '2283:2283'" => Some("- '127.0.0.1:2283:2283'"),
            "- \"2283:2283\"" => Some("- \"127.0.0.1:2283:2283\""),
            _ => None,
        };
        if let Some(target) = target {
            count += 1;
            lines.push(format!("{indent}{target}"));
        } else {
            lines.push(line.to_string());
        }
    }
    if count != 1 {
        return Err(Error::new(
            "Unknown upstream Immich port mapping; update refused",
        ));
    }
    // On the SSD work folder, not in the snapshot: its .env holds the database password.
    let folder = work.join("immich-candidate");
    common::directory(&folder, 0o700)?;
    let env = env
        .lines()
        .map(|line| {
            if line.starts_with("IMMICH_VERSION=") {
                format!("IMMICH_VERSION={tag}")
            } else {
                line.to_string()
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
        + "\n";
    common::atomic(&folder.join(".env"), env.as_bytes(), 0o600)?;
    common::atomic(
        &folder.join("compose.yaml"),
        (lines.join("\n") + "\n").as_bytes(),
        0o600,
    )?;
    // The new release keeps the graphics device the installer gave Immich.
    if original.join(HARDWARE).is_file() {
        common::atomic(
            &folder.join(HARDWARE),
            &common::bytes(&original.join(HARDWARE), 64000)?,
            0o600,
        )?;
    }
    Ok(Some(folder))
}

/// You approve a new Immich major by naming it ("v3") or its exact release ("v3.0.1").
/// Naming the major keeps working when a fix release comes out before the night it runs.
fn immich_major_approved(wanted: &str, tag: &str, major: &str) -> bool {
    !wanted.is_empty() && (wanted == tag || wanted.strip_prefix('v') == Some(major))
}
/// The Nextcloud major worth running now: a brand-new x.0.0 or x.0.1 counts as the
/// previous major, as on a fresh install.
fn nextcloud_recommended_major(tag: &str) -> Option<u64> {
    let caps = regex::Regex::new(r"^v(\d+)\.(\d+)\.(\d+)$")
        .unwrap()
        .captures(tag)?;
    let major: u64 = caps[1].parse().ok()?;
    let minor: u64 = caps[2].parse().ok()?;
    let patch: u64 = caps[3].parse().ok()?;
    Some(if minor == 0 && patch < 2 {
        major.saturating_sub(1)
    } else {
        major
    })
}
fn installed_nextcloud_major(manifest: &str) -> Option<u64> {
    regex::Regex::new(r"nextcloud:(\d+)(?:\.\d+)*-apache")
        .unwrap()
        .captures(manifest)?[1]
        .parse()
        .ok()
}
/// Record, never apply, a newer Nextcloud major: monthly updates stay on the installed
/// major, and Nextcloud must be upgraded one major at a time. The dashboard shows it.
fn nextcloud_major_notice(cfg: &Value, snapshot: &Path) -> Result<()> {
    let manifest = common::bytes(
        &Path::new(string(cfg, "data_mount")?).join("compose/nextcloud/compose.yaml"),
        200000,
    )?;
    let Some(current) = installed_nextcloud_major(&String::from_utf8_lossy(&manifest)) else {
        return Ok(());
    };
    let release: Value = serde_json::from_str(&release_fetch(
        "https://api.github.com/repos/nextcloud/server/releases/latest",
    )?)?;
    if release["prerelease"] == true || release["draft"] == true {
        return Ok(());
    }
    match nextcloud_recommended_major(string(&release, "tag_name")?) {
        Some(next) if next > current => common::atomic_json(
            &snapshot.join("nextcloud-major-pending.json"),
            &json!({"status":"manual_major_upgrade_required","current":current.to_string(),"available":next.to_string(),"next_step":(current + 1).to_string()}),
        ),
        _ => Ok(()),
    }
}

/// Files that move Nextcloud to the approved next major, or None. Nextcloud upgrades
/// one major at a time, so only `installed + 1` is ever accepted.
fn nextcloud_major_step(cfg: &Value, folder: &Path) -> Result<Option<(u64, u64)>> {
    let Some(approved) = cfg["maintenance"]["nextcloud_major_update"]
        .as_str()
        .and_then(|s| s.trim().parse::<u64>().ok())
    else {
        return Ok(None);
    };
    let manifest =
        String::from_utf8_lossy(&common::bytes(&folder.join("compose.yaml"), 200000)?).to_string();
    match installed_nextcloud_major(&manifest) {
        Some(current) if approved == current + 1 => Ok(Some((current, approved))),
        Some(current) if approved > current + 1 => {
            println!("Nextcloud: {approved} approved, but it must be reached one major at a time; installing {} first is not automatic. Set nextcloud_major_update to {}", current + 1, current + 1);
            Ok(None)
        }
        _ => Ok(None),
    }
}
fn retag_nextcloud(text: &str, from: u64, to: u64) -> String {
    text.replace(
        &format!("\"nextcloud:{from}-apache\""),
        &format!("\"nextcloud:{to}-apache\""),
    )
}

fn automated_media_ready(report: &Value) -> bool {
    let manual = [
        "end_to_end_media",
        "navidrome:authenticated_playback_or_transfer",
        "qbittorrent:authenticated_playback_or_transfer",
    ];
    if report["services"].as_object().is_none_or(|v| v.is_empty()) {
        return false;
    }
    for group in ["services", "integrations", "readiness"] {
        let Some(rows) = report[group].as_object() else {
            return false;
        };
        for (key, row) in rows {
            if row["result"] == "pass" {
                continue;
            }
            if group == "readiness"
                && manual.contains(&key.as_str())
                && row["result"] == "needs_setup"
            {
                continue;
            }
            return false;
        }
    }
    true
}

fn integrations(cfgpath: &Path, cfg: &Value, checking: bool) -> Result<()> {
    if cfg["media"]["services"]
        .as_array()
        .is_none_or(|v| v.is_empty())
    {
        return Ok(());
    }
    let exe = std::env::current_exe()?;
    let report = Path::new(string(cfg, "data_mount")?).join("reports/media/latest.json");
    let exe = exe.to_string_lossy();
    let config = cfgpath.to_string_lossy();
    let output = report.to_string_lossy();
    let mut args = vec![
        &*exe,
        "media-health",
        "--config",
        &*config,
        "--output",
        &*output,
    ];
    if checking {
        args.push("--checking");
    }
    let (code, _) = common::command(&args, 900)?;
    // Exit 3 also includes intentionally unwitnessed human playback checks.
    if ![0, 3].contains(&code) || !automated_media_ready(&load(&report)?) {
        return Err(Error::new("Live automated media connections are not ready"));
    }
    Ok(())
}

fn keep_snapshots(cfg: &Value) -> u64 {
    cfg["maintenance"]["keep_snapshots"].as_u64().unwrap_or(3)
}
/// Monthly snapshot folders (`<time>-<pid>`, never symlinks) under `root/backups/monthly`,
/// oldest first. Shared by maintenance, the weekly USB job and the dashboard.
pub(crate) fn monthly_runs(root: &Path) -> Result<Vec<String>> {
    let folder = root.join("backups/monthly");
    let pattern = regex::Regex::new(r"^[0-9]+-[0-9]+$").unwrap();
    let mut runs = vec![];
    if !folder.exists() {
        return Ok(runs);
    }
    for entry in fs::read_dir(common::safe(&folder)?)? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().to_string();
        if entry.file_type()?.is_dir() && pattern.is_match(&name) {
            runs.push(name);
        }
    }
    // Run names start with a microsecond timestamp, so numeric order is age order.
    runs.sort_by_key(|n| {
        let (a, b) = n.split_once('-').unwrap();
        (
            a.parse::<u128>().unwrap_or(0),
            b.parse::<u128>().unwrap_or(0),
        )
    });
    Ok(runs)
}
fn same_manifest(a: &Path, b: &Path) -> bool {
    match (
        fs::read(a.join("manifest.json")),
        fs::read(b.join("manifest.json")),
    ) {
        (Ok(x), Ok(y)) => x == y,
        _ => false,
    }
}
/// What a rollback set aside on the SSD for that run: the failed update's app data
/// (`appdata/<app>.neko-failed-<run>`, `compose/<app>.neko-failed-<run>`, ...). Kept
/// to look into until the run's snapshot itself is retired, then removed with it.
fn failed_copies(data: &Path, run: &str) -> Result<Vec<PathBuf>> {
    let suffix = format!(".neko-failed-{run}");
    let mut found = vec![];
    for folder in [
        data.to_path_buf(),
        data.join("appdata"),
        data.join("compose"),
        data.join("secrets"),
    ] {
        let Ok(entries) = fs::read_dir(&folder) else {
            continue;
        };
        for entry in entries {
            let entry = entry?;
            if entry.file_type()?.is_dir() && entry.file_name().to_string_lossy().ends_with(&suffix)
            {
                found.push(entry.path());
            }
        }
    }
    Ok(found)
}
fn remove_run(path: &Path) -> Result<()> {
    private(path)?;
    fs::remove_dir_all(common::safe(path)?)?;
    Ok(())
}
/// Keep the newest `keep` complete monthly snapshots on the HDD (0 keeps everything).
/// A run is complete once its restore test passed; incomplete or unrecognised runs
/// are never deleted, only reported. Copies that v0.21.0 wrote to the SSD are removed
/// once the same run (identical manifest) is on the HDD. The USB drive is not touched
/// here: the weekly USB job retires the old monthly copies there.
fn prune_monthly(data: &Path, media: &Path, keep: u64, current: Option<&str>) -> Result<Value> {
    let journal = load(Path::new(JOURNAL)).ok();
    let protected = |run: &str| {
        Some(run) == current
            || journal.as_ref().is_some_and(|j| {
                j["snapshot"]
                    .as_str()
                    .is_some_and(|s| Path::new(s).file_name().is_some_and(|f| f == run))
            })
    };
    let hdd = media.join("backups/monthly");
    let complete: Vec<String> = monthly_runs(media)?
        .into_iter()
        .filter(|r| hdd.join(r).join("restore-tests.json").is_file())
        .filter(|r| hdd.join(r).join("manifest.json").is_file())
        .collect();
    let mut removed = vec![];
    let mut kept = vec![];
    let mut held_back = None;
    let cut = complete.len().saturating_sub(keep as usize);
    // Old snapshots are only deleted while the ones kept in their place are intact.
    let damaged: Vec<&String> = if keep > 0 && cut > 0 {
        complete[cut..]
            .iter()
            .filter(|r| verify(&hdd.join(r)).is_err())
            .collect()
    } else {
        vec![]
    };
    if !damaged.is_empty() {
        held_back = Some(format!(
            "older snapshots kept: {} failed its checksum check",
            damaged
                .iter()
                .map(|s| s.as_str())
                .collect::<Vec<_>>()
                .join(", ")
        ));
        println!("Monthly retention: {}", held_back.as_deref().unwrap_or(""));
        kept.extend(complete.iter().cloned());
    } else if keep > 0 {
        for run in &complete[..cut] {
            if protected(run) {
                kept.push(run.clone());
                continue;
            }
            remove_run(&hdd.join(run))?;
            removed.push(json!({"run":run,"drive":"hdd"}));
            for copy in failed_copies(data, run)? {
                fs::remove_dir_all(common::safe(&copy)?)?;
                removed.push(json!({"run":run,"drive":"ssd","failed_update":copy}));
            }
        }
        kept.extend(complete[cut..].iter().cloned());
    } else {
        kept.extend(complete.iter().cloned());
    }
    let ssd = data.join("backups/monthly");
    for run in monthly_runs(data)? {
        if protected(&run) {
            continue;
        }
        let copy = ssd.join(&run);
        let original = hdd.join(&run);
        // The SSD copy may be the only good copy left. Delete it only when the HDD run is
        // complete (restore-tested) and its archives really match their checksums; a
        // matching manifest alone proves nothing about the archives next to it.
        if complete.contains(&run) && same_manifest(&copy, &original) && verify(&original).is_ok() {
            remove_run(&copy)?;
            removed.push(json!({"run":run,"drive":"ssd"}));
        }
    }
    let unmanaged: Vec<String> = monthly_runs(media)?
        .into_iter()
        .filter(|r| !complete.contains(r) && !protected(r))
        .collect();
    Ok(
        json!({"keep":keep,"kept":kept,"removed":removed,"incomplete_not_removed":unmanaged,"held_back":held_back}),
    )
}
/// For a full HDD: the unfinished snapshots retention keeps there, which are often
/// the room a person can free, so the space message names them.
fn unfinished_note(folder: &Path, runs: &[String], bytes: u64) -> String {
    if runs.is_empty() {
        return String::new();
    }
    format!(
        ". Unfinished monthly snapshots (cut off, or their restore test failed) are never deleted automatically and take about {} GB there: {} in {}; delete the ones you don't need",
        (bytes as f64 / 1e9).ceil() as u64,
        runs.join(", "),
        folder.display()
    )
}
fn live(file: &Path, name: &str) -> Result<()> {
    compose(
        file,
        &format!("neko-{name}"),
        &["up", "-d", "--pull", "never"],
    )
    .map(|_| ())
}
/// Apps whose user files (photos, Nextcloud files) the monthly snapshot leaves out: a
/// rollback restores their database but not their files, so nothing may change those
/// files until the update is committed.
fn guarded(name: &str) -> bool {
    database_only_table(name).is_some()
}
/// Apps held back until the update commits: the guarded ones, and the ones that import,
/// move or delete media in the library (also not in the snapshot) while they are checked.
fn held(name: &str) -> bool {
    guarded(name) || media::LIBRARY_WRITERS.contains(&name)
}
/// The updated app as it runs while it is checked, before the update is committed.
/// Immich and Nextcloud: no client can reach them (a random port on this computer only,
/// so the gateway, Tailscale and phones get nothing) and they do no background work of
/// their own (no Nextcloud cron; Immich answers requests but runs no jobs). Sonarr,
/// Radarr and Lidarr keep their ports (the media audit uses them) but see the library
/// read-only, so no import, rename or delete can change it before the commit.
fn quarantined(model: &Value, name: &str) -> Result<Value> {
    let mut model = model.clone();
    let services = model["services"]
        .as_object_mut()
        .ok_or_else(|| Error::new("Invalid model"))?;
    if media::LIBRARY_WRITERS.contains(&name) {
        for v in services.values_mut() {
            // get_mut: indexing a service without volumes would insert "volumes": null.
            let listed = v
                .get_mut("volumes")
                .and_then(Value::as_array_mut)
                .into_iter()
                .flatten();
            for volume in listed {
                if volume["type"] == "bind" && volume["target"] != "/config" {
                    volume["read_only"] = json!(true);
                }
            }
        }
        return Ok(model);
    }
    if name == "nextcloud" {
        services.remove("cron");
    }
    for (service, v) in services.iter_mut() {
        if let Some(ports) = v.get_mut("ports").and_then(Value::as_array_mut) {
            for p in ports {
                p["published"] = json!("0");
                p["host_ip"] = json!("127.0.0.1");
            }
        }
        if name == "immich" && service == "immich-server" {
            if !v["environment"].is_object() {
                v["environment"] = json!({});
            }
            v["environment"]["IMMICH_WORKERS_INCLUDE"] = json!("api");
            // An exclusion list as well would leave it no worker at all.
            v["environment"]
                .as_object_mut()
                .unwrap()
                .remove("IMMICH_WORKERS_EXCLUDE");
        }
    }
    Ok(model)
}
fn candidate_file(snapshot: &Path, name: &str, committed: bool) -> PathBuf {
    let kind = if held(name) && !committed {
        "quarantine"
    } else {
        "new"
    };
    snapshot.join(format!("{name}-{kind}.json"))
}
/// Start and check the updated apps, commit, and only then let clients reach the apps
/// whose user files a rollback cannot restore. A failure before the commit rolls back
/// (nothing has changed those files); after it, the update stands and only the opening
/// up is repeated (resume, phase "committed").
fn go_live(
    names: &[String],
    updated: &Value,
    snapshot: &Path,
    start: &dyn Fn(&Path, &str) -> Result<()>,
    check: &dyn Fn(&Path, &str) -> Result<()>,
    verify: impl FnOnce() -> Result<()>,
    commit: impl FnOnce() -> Result<()>,
) -> Result<()> {
    for name in names {
        common::atomic_json(&candidate_file(snapshot, name, true), &updated[name])?;
        if held(name) {
            common::atomic_json(
                &candidate_file(snapshot, name, false),
                &quarantined(&updated[name], name)?,
            )?;
        }
        start(&candidate_file(snapshot, name, false), name)?;
    }
    for name in names {
        check(&candidate_file(snapshot, name, false), name)?;
    }
    verify()?;
    commit()?;
    open_up(names, snapshot, start, check).map_err(|e| {
        Error::new(&format!(
            "Update installed, but Immich/Nextcloud could not be opened up again ({e}); \
             nekoserver-maintenance-resume.service retries this, without rolling back"
        ))
    })
}
fn open_up(
    names: &[String],
    snapshot: &Path,
    start: &dyn Fn(&Path, &str) -> Result<()>,
    check: &dyn Fn(&Path, &str) -> Result<()>,
) -> Result<()> {
    let opened: Vec<&String> = names.iter().filter(|n| held(n)).collect();
    for name in &opened {
        start(&candidate_file(snapshot, name, true), name)?;
    }
    for name in &opened {
        check(&candidate_file(snapshot, name, true), name)?;
    }
    Ok(())
}
fn transaction(cfgpath: &Path, cfg: &Value) -> Result<()> {
    mounts(cfgpath, cfg, false)?;
    let data = Path::new(string(cfg, "data_mount")?);
    let media = Path::new(string(cfg, "media_mount")?);
    let names = names(cfg)?;
    // Password files go only into the encrypted archive when the backup key exists;
    // without it they stay in data.tar so the snapshot can still be restored.
    let sealing = secrets_key_ready(Path::new(SECRETS_KEY_DIR));
    let paths = snapshot_paths(&names, data, sealing);
    let reserve = cfg["maintenance"]["minimum_free_gb"]
        .as_u64()
        .unwrap_or(20)
        .saturating_mul(1 << 30);

    let app_bytes = size(data, &paths.0)?;
    let media_bytes = size(media, &paths.1)?;
    common::stage("checking free space");
    let keep = keep_snapshots(cfg);
    let mut retention = prune_monthly(data, media, keep, None)?;
    let (ssd, hdd) = space_needed(app_bytes, media_bytes, image_bytes(&names)?);
    // The USB drive is not used by monthly maintenance.
    for (root, needed, what) in [(data, ssd, "SSD"), (media, hdd, "HDD")] {
        let have = free(root)?;
        let wanted = needed.saturating_add(reserve);
        if have < wanted {
            let gb = |b: u64| (b as f64 / 1e9).ceil() as u64;
            let unfinished = if what == "HDD" {
                let runs: Vec<String> = retention["incomplete_not_removed"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(|r| r.as_str().map(str::to_string))
                    .collect();
                let folder = media.join("backups/monthly");
                unfinished_note(&folder, &runs, size(&folder, &runs).unwrap_or(0))
            } else {
                String::new()
            };
            return Err(Error::new(&format!(
                "Not enough space on the {what} ({}) for the monthly snapshot and update: {} GB free, about {} GB needed (a copy of the app data and the new app images, plus the {} GB reserve from maintenance.minimum_free_gb). Free up space there, or lower the reserve{unfinished}",
                root.display(),
                gb(have),
                gb(wanted),
                gb(reserve)
            )));
        }
    }
    let run = format!("{}-{}", (common::now() * 1e6) as u128, std::process::id());
    let snapshot = media.join("backups/monthly").join(&run);
    common::directory(&snapshot, 0o700)?;
    clear_work(data);
    let work = work_dir(data, &run);
    common::directory(&data.join("backups/maintenance-work"), 0o700)?;
    common::directory(&work, 0o700)?;
    let mut models = json!({});
    let mut rows = vec![];
    for name in &names {
        let mut current = model(cfg, name)?;
        valid_model(&current, name, data, media)?;
        rows.extend(containers(name, &mut current)?);
        let file = snapshot.join(format!("{name}-old.json"));
        common::atomic_json(&file, &current)?;
        models[name] = current;
    }
    common::atomic_json(&snapshot.join("config.json"), cfg)?;
    for name in &names {
        health(
            &snapshot.join(format!("{name}-old.json")),
            &format!("neko-{name}"),
            name,
            30,
            false,
            &snapshot,
        )?;
    }
    integrations(cfgpath, cfg, false)?;
    no_other_writers(&rows, data, media, &paths)?;
    common::directory(Path::new("/etc/nekoserver-backups"), 0o700)?;
    save_journal(
        &json!({"format":"neko-maintenance-resume-v1","version":VERSION,"run":run,"phase":"snapshot","snapshot":snapshot,"storage":cfg["storage"],"data_mount":cfg["data_mount"],"media_mount":cfg["media_mount"],"models":models,"containers":rows,"rehearsals":[]}),
    )?;
    common::stage("stopping the apps for a consistent snapshot");
    stop(&rows)?;
    mounts(cfgpath, cfg, false)?;
    common::stage("writing the snapshot");
    let exclude: &[&str] = if sealing { &SEALED_EXCLUDES } else { &[] };
    tar(data, &paths.0, &snapshot.join("data.tar"), exclude)?;
    tar(media, &paths.1, &snapshot.join("media.tar"), &[])?;
    let password_files = encrypt_secrets(data, &snapshot)?;
    if password_files == "unencrypted_in_data_tar" {
        println!("Monthly maintenance: no backup key yet, so this snapshot keeps the app password files unencrypted inside data.tar (readable by root only). To encrypt them from now on: sudo python3 backup_secrets.py --setup");
    }
    if sealing {
        // data.tar left these out; keep a private SSD copy so a failed update can be
        // rolled back unattended (the encrypted copy needs the sudo password).
        let mut journal = load(Path::new(JOURNAL))?;
        journal["sealed_copy"] = json!(save_sealed_copy(data, &work)?);
        save_journal(&journal)?;
    }
    let mut images = std::collections::BTreeSet::new();
    for m in models.as_object().unwrap().values() {
        for v in m["services"].as_object().unwrap().values() {
            images.insert(string(v, "image")?.to_string());
        }
    }
    let image_archive = snapshot.join("images.tar");
    let image_name = image_archive.to_string_lossy();
    let mut image_args = vec!["image", "save", "--output", &image_name];
    image_args.extend(images.iter().map(String::as_str));
    docker(&image_args)?;
    fs::File::open(&image_archive)?.sync_all()?;
    let mut manifest = json!({"format":"neko-maintenance-v1","version":VERSION,"created_at":common::now(),"storage":cfg["storage"],"paths":{"data":paths.0,"media":paths.1},"sha256":{"data":common::digest(&snapshot.join("data.tar"))?,"media":common::digest(&snapshot.join("media.tar"))?,"images":common::digest(&image_archive)?},"password_files":password_files});
    if password_files == "encrypted" {
        // The archive and the key copy and certificate that open it: a damaged key would
        // otherwise only show up at a restore, as a "wrong password".
        for (key, name) in [
            ("secrets", SECRETS_ARCHIVE),
            ("secrets_key", SECRETS_KEY),
            ("secrets_cert", SECRETS_CERT),
        ] {
            manifest["sha256"][key] = json!(common::digest(&snapshot.join(name))?);
        }
    }
    common::atomic_json(&snapshot.join("manifest.json"), &manifest)?;
    verify(&snapshot)?;
    let mut journal = load(Path::new(JOURNAL))?;
    journal["phase"] = json!("backed_up");
    save_journal(&journal)?;
    common::stage("testing the snapshot restores");
    rehearsal(&snapshot, cfg, &models, &snapshot)?;
    common::atomic_json(
        &snapshot.join("restore-tests.json"),
        &json!({"status":"pass","method":"isolated containers started from extracted backups with HTTP probes; Immich and Nextcloud databases restored and checked by SQL","at":common::now()}),
    )?;
    if !night(cfg) {
        return Err(Error::new(
            "Night window ended; backups complete, updates deferred",
        ));
    }
    common::stage("downloading app updates");
    // Pull the declared version streams. Major-version migrations and OS upgrades are excluded.
    let mut updated = json!({});
    let mut immich_files = None;
    let mut nextcloud_step = None;
    for name in &names {
        let source = if name == "immich" {
            immich_files = immich_candidate(cfg, &snapshot, &work)?;
            immich_files
                .clone()
                .unwrap_or_else(|| data.join("compose/immich"))
        } else {
            data.join("compose").join(name)
        };
        let mut candidate = source_model(&source, name)?;
        valid_model(&candidate, name, data, media)?;
        if name == "nextcloud" {
            let step = nextcloud_major_step(cfg, &source)?;
            // Only report a newer major when none is being installed this month;
            // otherwise the notice would describe the version being replaced.
            if step.is_none() {
                if let Err(e) = nextcloud_major_notice(cfg, &snapshot) {
                    println!("Nextcloud: could not check for a newer major version ({e})");
                }
            }
            if let Some((from, to)) = step {
                println!("Nextcloud: installing approved major {to} (from {from})");
                for service in ["nextcloud", "cron"] {
                    if candidate["services"][service]["image"]
                        == json!(format!("nextcloud:{from}-apache"))
                    {
                        candidate["services"][service]["image"] =
                            json!(format!("nextcloud:{to}-apache"));
                    }
                }
                nextcloud_step = Some((from, to));
            }
        }
        for v in candidate["services"].as_object_mut().unwrap().values_mut() {
            let image = string(v, "image")?.to_string();
            docker(&["pull", &image])?;
            let info: Value = serde_json::from_str(&docker(&["image", "inspect", &image])?)?;
            let id = info[0]["Id"]
                .as_str()
                .filter(|s| image_id(s))
                .ok_or_else(|| Error::new("Invalid candidate image"))?;
            v["image"] = json!(id);
        }
        updated[name] = candidate;
    }
    if !night(cfg) {
        return Err(Error::new(
            "Night window ended during image downloads; updates deferred",
        ));
    }
    mounts(cfgpath, cfg, false)?;
    journal = load(Path::new(JOURNAL))?;
    journal["phase"] = json!("updating");
    save_journal(&journal)?;
    if let Some(folder) = immich_files {
        for name in [".env", "compose.yaml"] {
            common::atomic(
                &data.join("compose/immich").join(name),
                &common::bytes(&folder.join(name), 500000)?,
                0o600,
            )?;
        }
    }
    // The declared tag moves with an approved Nextcloud major, so later monthly
    // updates and installer re-runs stay on it (the snapshot covers these files).
    if let Some((from, to)) = nextcloud_step {
        for name in ["compose.yaml", "managed.json"] {
            let path = data.join("compose/nextcloud").join(name);
            if path.exists() {
                let text = String::from_utf8_lossy(&common::bytes(&path, 500000)?).to_string();
                common::atomic(&path, retag_nextcloud(&text, from, to).as_bytes(), 0o600)?;
            }
        }
    }
    common::stage("starting the updated apps");
    // Originals have remained stopped since the consistent backup; no writes are lost.
    let wait = cfg["maintenance"]["health_wait_seconds"]
        .as_u64()
        .unwrap_or(300);
    let start = |file: &Path, name: &str| live(file, name);
    let check = |file: &Path, name: &str| {
        health(file, &format!("neko-{name}"), name, wait, false, &snapshot)
    };
    go_live(
        &names,
        &updated,
        &snapshot,
        &start,
        &check,
        || integrations(cfgpath, cfg, true).and_then(|_| mounts(cfgpath, cfg, false)),
        || {
            // Persist successful image identities for installer reruns and reboot reproducibility.
            for name in &names {
                write_override(cfg, name, &updated[name])?;
            }
            common::atomic_json(
                &snapshot.join("result.json"),
                &json!({"status":"pass","updated_at":common::now(),"restore_test":"pass","snapshot":"hdd","images":updated}),
            )?;
            let mut journal = load(Path::new(JOURNAL))?;
            journal["phase"] = json!("committed");
            save_journal(&journal)
        },
    )?;
    fs::remove_file(JOURNAL)?;
    fs::File::open(Path::new(JOURNAL).parent().unwrap())?.sync_all()?;
    scrub_all(media);
    clear_work(data);
    // The update is committed. Cleanup is best effort: a failure here must never be
    // mistaken for a failed update, and deletes only touch a re-verified USB drive.
    retention["after_update"] = match mounts(cfgpath, cfg, false)
        .and_then(|_| prune_monthly(data, media, keep, Some(&run)))
    {
        Ok(report) => report,
        Err(e) => json!({"status":"skipped","reason":e.to_string()}),
    };
    let _ = common::atomic_json(&snapshot.join("retention.json"), &retention);
    Ok(())
}
pub fn run(a: &Args, recover: bool) -> Result<()> {
    common::require_root()?;
    let cfgpath = a.path("config")?;
    private(&cfgpath)?;
    let cfg = load(&cfgpath)?;
    let data = common::mount_path(string(&cfg, "data_mount")?)?;
    mounts(&cfgpath, &cfg, false)?;
    let _lock = common::lock_wait(
        &data.join("installer/recovery.lock"),
        std::time::Duration::from_secs(3 * 3600),
    )?;
    let installed = committed();
    // Read before resume() finishes the run and removes the journal.
    let cut_in = load(Path::new(JOURNAL))
        .ok()
        .and_then(|j| j["phase"].as_str().map(str::to_string));
    resume(&cfgpath, &cfg)?;
    if recover {
        let report = data.join("reports/jobs/maintenance.json");
        if report.exists() {
            let mut value = load(&report)?;
            if value["status"] == "running" {
                // Cut off (a power cut, say) after the update was installed: it stands
                // and was just finished, so it is a success, not a failure.
                let now = common::now();
                value["status"] = json!(if installed { "pass" } else { "fail" });
                value["finished_at"] = json!(now);
                if installed {
                    value["last_success"] = json!(now);
                } else {
                    value["reason"] = json!(cut_off_reason(cut_in.as_deref()));
                }
                common::atomic_json(&report, &value)?;
            }
        }
        return Ok(());
    }
    if !night(&cfg) {
        return Err(Error::new(
            "Maintenance runs only within the configured night window",
        ));
    }
    backup::execute_with(Some(&data), "maintenance", &cfgpath, || {
        let Err(failure) = transaction(&cfgpath, &cfg) else {
            return Ok(backup::Outcome::default());
        };
        // Said first, so the reason survives whatever happens while putting things right.
        println!("Monthly update: {failure}");
        let installed = committed();
        if let Err(e) = resume(&cfgpath, &cfg) {
            return Err(Error::new(&format!("{failure}; then: {e}")));
        }
        tidy_after_failure(&cfg);
        if installed {
            // The update was installed and its apps are now open again: a success.
            println!("Monthly update: installed; reopening the apps needed a second try");
            return Ok(backup::Outcome::default());
        }
        if failure.to_string().starts_with("Night window ended") {
            // The backups and their restore tests are complete; only the update waited
            // for a night that ran out. Done in part, not failed: the next run updates.
            return Ok(backup::Outcome {
                partial: Some(json!([format!("app updates: {failure}")])),
            });
        }
        Err(failure)
    })
}
/// What the dashboard says about a monthly run that was cut off (a power cut, a restart)
/// before its update was installed, once the resume service has put things right.
fn cut_off_reason(phase: Option<&str>) -> String {
    let when = match phase {
        Some("snapshot") => "while it was taking the snapshot, with the apps stopped",
        Some("backed_up") => "while it was testing the snapshot",
        Some("updating") => "while it was installing the updates",
        Some(_) => "part-way",
        None => "before it took the snapshot",
    };
    format!(
        "The monthly run was cut off (a power cut or a restart) {when}. The apps were started again with their versions from before the run; the next monthly run tries again"
    )
}
/// The update was installed (the journal says so): finishing it never rolls back.
fn committed() -> bool {
    load(Path::new(JOURNAL)).is_ok_and(|j| j["phase"] == "committed")
}
#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::DirBuilderExt;
    fn scratch(name: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!("neko-prune-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        for drive in ["ssd", "hdd", "usb"] {
            fs::DirBuilder::new()
                .recursive(true)
                .mode(0o700)
                .create(root.join(drive).join("backups/monthly"))
                .unwrap();
        }
        root
    }
    fn snapshot(root: &Path, drive: &str, run: &str, complete: bool) {
        let dir = root.join(drive).join("backups/monthly").join(run);
        fs::DirBuilder::new().mode(0o700).create(&dir).unwrap();
        snapshot_archives(&dir, run);
        if complete {
            fs::write(dir.join("restore-tests.json"), b"{}").unwrap();
        }
    }
    /// A real-looking snapshot: archives plus a manifest with their checksums.
    fn snapshot_archives(dir: &Path, run: &str) {
        let mut sums = serde_json::Map::new();
        for key in ["data", "media", "images"] {
            let path = dir.join(format!("{key}.tar"));
            fs::write(&path, format!("{run}-{key}")).unwrap();
            sums.insert(key.into(), json!(common::digest(&path).unwrap()));
        }
        fs::write(
            dir.join("manifest.json"),
            json!({"format":"neko-maintenance-v1","run":run,"sha256":sums}).to_string(),
        )
        .unwrap();
    }
    fn runs(root: &Path, drive: &str) -> Vec<String> {
        monthly_runs(&root.join(drive)).unwrap()
    }
    fn sh(args: &[&str], stdin: Option<&[u8]>) -> Vec<u8> {
        use std::io::Write;
        let mut child = std::process::Command::new(args[0])
            .args(&args[1..])
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .spawn()
            .unwrap();
        child
            .stdin
            .take()
            .unwrap()
            .write_all(stdin.unwrap_or(b""))
            .unwrap();
        let out = child.wait_with_output().unwrap();
        assert!(out.status.success(), "{args:?}");
        out.stdout
    }
    #[test]
    fn rollback_with_encryption_restores_the_left_out_config_files() {
        // A failed update with a backup key: data.tar lacks .env and config.php, and the
        // rollback stage must get them back without anyone typing the sudo password.
        let root = scratch("seal-rollback");
        let data = root.join("ssd");
        for (rel, body) in [
            ("compose/nextcloud/.env", "ADMIN_PASSWORD=nc-admin-pw\n"),
            (
                "compose/immich/.env",
                "DB_PASSWORD=im-db-pw\nIMMICH_VERSION=v1.2.3\n",
            ),
            ("compose/immich/compose.yaml", "services: {}\n"),
            (
                "appdata/nextcloud/html/config/config.php",
                "'dbpassword' => 'nc-db-pw'\n",
            ),
            ("appdata/nextcloud/html/index.php", "<?php\n"),
            (
                "secrets/accounts/APP-CREDENTIALS.txt",
                "only in the encrypted archive\n",
            ),
        ] {
            fs::create_dir_all(data.join(rel).parent().unwrap()).unwrap();
            fs::write(data.join(rel), body).unwrap();
        }
        let paths = ["appdata/nextcloud".to_string(), "compose".to_string()];
        let snapshot = root.join("hdd/run");
        fs::create_dir_all(&snapshot).unwrap();
        tar(&data, &paths, &snapshot.join("data.tar"), &SEALED_EXCLUDES).unwrap();
        let work = work_dir(&data, "run-1");
        fs::create_dir_all(&work).unwrap();
        let journal =
            json!({"run": "run-1", "sealed_copy": save_sealed_copy(&data, &work).unwrap()});
        let copy = fs::read(work.join(SEALED_COPY)).unwrap();
        assert!(!copy.windows(13).any(|w| w == b"encrypted arc")); // accounts list stays sealed only
                                                                   // The update fails and changes the live files; rollback stages the snapshot.
        fs::write(
            data.join("compose/immich/.env"),
            "DB_PASSWORD=im-db-pw\nIMMICH_VERSION=v9.9.9\n",
        )
        .unwrap();
        let stage = data.join("backups/rollback-run-1");
        fs::create_dir_all(&stage).unwrap();
        sh(
            &[
                "tar",
                "-x",
                "-f",
                &snapshot.join("data.tar").to_string_lossy(),
                "-C",
                &stage.to_string_lossy(),
            ],
            None,
        );
        assert!(!stage.join("compose/nextcloud/.env").exists());
        restore_sealed_copy(&journal, &data, &stage).unwrap();
        assert_eq!(
            fs::read_to_string(stage.join("compose/immich/.env")).unwrap(),
            "DB_PASSWORD=im-db-pw\nIMMICH_VERSION=v1.2.3\n"
        );
        assert!(
            fs::read_to_string(stage.join("appdata/nextcloud/html/config/config.php"))
                .unwrap()
                .contains("nc-db-pw")
        );
        assert!(stage.join("compose/nextcloud/.env").is_file());
        // A damaged copy stops the rollback with instructions instead of starting broken apps.
        fs::write(work.join(SEALED_COPY), b"damaged").unwrap();
        let error = restore_sealed_copy(&journal, &data, &stage)
            .unwrap_err()
            .to_string();
        assert!(error.contains("monthly-recovery.py --secrets"), "{error}");
        // Nothing left out (no backup key): nothing to restore.
        restore_sealed_copy(&json!({"run": "run-1"}), &data, &stage).unwrap();
        // Finished runs' work folders are removed.
        clear_work(&data);
        assert!(!work.exists());
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn a_run_that_failed_before_its_journal_keeps_no_passwords() {
        // Only meaningful without a live journal on this machine (never in CI).
        if Path::new(JOURNAL).exists() {
            return;
        }
        let root = scratch("early-failure");
        let run = root.join("hdd/backups/monthly/1700000000000000-42");
        fs::create_dir_all(&run).unwrap();
        let model = json!({"services":{"database":{"environment":{"DB_PASSWORD":"pw"}}}});
        fs::write(run.join("immich-old.json"), model.to_string()).unwrap();
        tidy_after_failure(
            &json!({"media_mount": root.join("hdd"), "data_mount": root.join("ssd")}),
        );
        let saved: Value =
            serde_json::from_slice(&fs::read(run.join("immich-old.json")).unwrap()).unwrap();
        assert_eq!(
            saved["services"]["database"]["environment"]["DB_PASSWORD"],
            REMOVED
        );
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn scrub_blanks_passwords_in_every_saved_model() {
        let root = scratch("scrub-models");
        let model =
            json!({"services":{"database":{"environment":{"POSTGRES_PASSWORD":"pw","TZ":"UTC"}}}});
        for name in [
            "immich-old.json",
            "immich-new.json",
            "immich-quarantine.json",
            "nextcloud-quarantine.json",
        ] {
            fs::write(root.join(name), model.to_string()).unwrap();
        }
        scrub_snapshot(&root).unwrap();
        for name in [
            "immich-old.json",
            "immich-new.json",
            "immich-quarantine.json",
            "nextcloud-quarantine.json",
        ] {
            let saved: Value = serde_json::from_slice(&fs::read(root.join(name)).unwrap()).unwrap();
            let env = &saved["services"]["database"]["environment"];
            assert_eq!(env["POSTGRES_PASSWORD"], REMOVED, "{name}");
            assert_eq!(env["TZ"], "UTC", "{name}");
        }
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn scrub_removes_the_old_immich_candidate_password_file() {
        let root = scratch("scrub-candidate");
        fs::create_dir_all(root.join("immich-candidate")).unwrap();
        fs::write(root.join("immich-candidate/.env"), "DB_PASSWORD=secret\n").unwrap();
        fs::write(root.join("immich-candidate/compose.yaml"), "services: {}\n").unwrap();
        scrub_snapshot(&root).unwrap();
        assert!(!root.join("immich-candidate/.env").exists());
        assert!(root.join("immich-candidate/compose.yaml").exists());
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn plain_data_archive_leaves_out_password_files() {
        let root = scratch("seal-tar");
        let data = root.join("ssd");
        for (rel, body) in [
            ("compose/nextcloud/.env", "ADMIN_PASSWORD=nc-admin-pw\n"),
            ("compose/nextcloud/compose.yaml", "services: {}\n"),
            (
                "appdata/nextcloud/html/config/config.php",
                "'dbpassword' => 'nc-db-pw'\n",
            ),
            ("appdata/nextcloud/html/index.php", "<?php\n"),
        ] {
            fs::create_dir_all(data.join(rel).parent().unwrap()).unwrap();
            fs::write(data.join(rel), body).unwrap();
        }
        let out = root.join("data.tar");
        let paths = [
            "appdata/nextcloud".to_string(),
            "compose/nextcloud".to_string(),
        ];
        tar(&data, &paths, &out, &SEALED_EXCLUDES).unwrap();
        let listing =
            String::from_utf8(sh(&["tar", "-t", "-f", &out.to_string_lossy()], None)).unwrap();
        assert!(listing.contains("compose/nextcloud/compose.yaml"));
        assert!(listing.contains("appdata/nextcloud/html/index.php"));
        assert!(
            !listing.contains(".env") && !listing.contains("config.php"),
            "{listing}"
        );
        assert!(!fs::read(&out)
            .unwrap()
            .windows(9)
            .any(|w| w == b"nc-db-pw'"));
        // Without a backup key nothing is left out, so the snapshot stays restorable.
        tar(&data, &paths, &out, &[]).unwrap();
        let listing =
            String::from_utf8(sh(&["tar", "-t", "-f", &out.to_string_lossy()], None)).unwrap();
        assert!(listing.contains("compose/nextcloud/.env"));
        assert_eq!(
            sealed_members(&data),
            [
                "compose/nextcloud/.env",
                "appdata/nextcloud/html/config/config.php"
            ]
        );
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn saved_app_definitions_lose_their_passwords_once_the_update_is_over() {
        let root = scratch("scrub");
        fs::create_dir_all(&root).unwrap();
        let model = json!({"services":{"nextcloud":{"image":"nextcloud:31-apache","environment":{"POSTGRES_PASSWORD":"pw1","NEXTCLOUD_ADMIN_PASSWORD":"pw2","POSTGRES_HOST":"database"}},"x":{"environment":["DB_PASSWORD=pw3","TZ=UTC"]}}});
        common::atomic_json(&root.join("nextcloud-old.json"), &model).unwrap();
        common::atomic_json(
            &root.join("result.json"),
            &json!({"images":{"nextcloud":model}}),
        )
        .unwrap();
        common::atomic_json(&root.join("manifest.json"), &json!({"keep":"as is"})).unwrap();
        scrub_snapshot(&root).unwrap();
        for name in ["nextcloud-old.json", "result.json"] {
            let text = fs::read_to_string(root.join(name)).unwrap();
            assert!(
                !text.contains("pw1") && !text.contains("pw2") && !text.contains("pw3"),
                "{name}"
            );
            assert!(
                text.contains("database")
                    && text.contains("TZ=UTC")
                    && text.contains("nextcloud:31-apache")
            );
        }
        let kept: Value =
            serde_json::from_str(&fs::read_to_string(root.join("manifest.json")).unwrap()).unwrap();
        assert_eq!(kept, json!({"keep":"as is"}));
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn password_files_are_encrypted_and_open_only_with_the_password() {
        let root = scratch("secrets");
        let data = root.join("ssd");
        let keys = root.join("keys");
        let snap = root.join("hdd/backups/monthly/500-1");
        for dir in [data.join("secrets/accounts"), keys.clone(), snap.clone()] {
            fs::DirBuilder::new()
                .recursive(true)
                .mode(0o700)
                .create(dir)
                .unwrap();
        }
        fs::write(
            data.join("secrets/accounts/APP-CREDENTIALS.txt"),
            b"jellyfin admin hunter2-topsecret\n",
        )
        .unwrap();
        // No key yet: no encrypted archive, and the manifest says the files stay in
        // data.tar unencrypted (the snapshot still restores; the label says how).
        assert_eq!(
            encrypt_secrets_with(&data, &snap, &keys).unwrap(),
            "unencrypted_in_data_tar"
        );
        assert!(fs::read_dir(&snap).unwrap().next().is_none());
        // Same key format backup_secrets.py creates.
        let plain = sh(
            &[
                "openssl",
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
            ],
            None,
        );
        let locked = sh(
            &[
                "openssl",
                "pkcs8",
                "-topk8",
                "-v2",
                "aes-256-cbc",
                "-v2prf",
                "hmacWithSHA256",
                "-iter",
                "1000",
                "-passout",
                "pass:sudo-pw",
            ],
            Some(&plain),
        );
        let cert = sh(
            &[
                "openssl",
                "req",
                "-new",
                "-x509",
                "-key",
                "/dev/stdin",
                "-passin",
                "pass:sudo-pw",
                "-subj",
                "/CN=test",
                "-days",
                "1",
            ],
            Some(&locked),
        );
        for (name, bytes) in [(SECRETS_KEY, &locked), (SECRETS_CERT, &cert)] {
            fs::write(keys.join(name), bytes).unwrap();
            fs::set_permissions(
                keys.join(name),
                <fs::Permissions as std::os::unix::fs::PermissionsExt>::from_mode(0o600),
            )
            .unwrap();
        }
        assert_eq!(
            encrypt_secrets_with(&data, &snap, &keys).unwrap(),
            "encrypted"
        );
        let sealed = fs::read(snap.join(SECRETS_ARCHIVE)).unwrap();
        assert!(!sealed.windows(9).any(|w| w == b"topsecret"));
        for name in [SECRETS_ARCHIVE, SECRETS_KEY, SECRETS_CERT] {
            assert_eq!(
                fs::metadata(snap.join(name)).unwrap().mode() & 0o777,
                0o600,
                "{name}"
            );
        }
        let archive = snap.join(SECRETS_ARCHIVE).to_string_lossy().to_string();
        let key = snap.join(SECRETS_KEY).to_string_lossy().to_string();
        let recip = snap.join(SECRETS_CERT).to_string_lossy().to_string();
        let opened = sh(
            &[
                "openssl",
                "cms",
                "-decrypt",
                "-binary",
                "-inform",
                "DER",
                "-in",
                &archive,
                "-inkey",
                &key,
                "-passin",
                "pass:sudo-pw",
                "-recip",
                &recip,
            ],
            None,
        );
        let text = sh(&["tar", "-xO", "-f", "-"], Some(&opened));
        assert_eq!(text, b"jellyfin admin hunter2-topsecret\n");
        // App .env files and Nextcloud's config.php are sealed too, never in plain text.
        for (rel, body) in [
            ("compose/immich/.env", "DB_PASSWORD=immich-db-pw\n"),
            ("compose/nextcloud/.env", "ADMIN_PASSWORD=nc-admin-pw\n"),
            (
                "appdata/nextcloud/html/config/config.php",
                "<?php $CONFIG = ['dbpassword' => 'nc-db-pw'];\n",
            ),
        ] {
            fs::create_dir_all(data.join(rel).parent().unwrap()).unwrap();
            fs::write(data.join(rel), body).unwrap();
        }
        let snap2 = root.join("hdd/backups/monthly/600-1");
        fs::DirBuilder::new().mode(0o700).create(&snap2).unwrap();
        assert_eq!(
            encrypt_secrets_with(&data, &snap2, &keys).unwrap(),
            "encrypted"
        );
        let sealed = fs::read(snap2.join(SECRETS_ARCHIVE)).unwrap();
        for secret in [&b"immich-db-pw"[..], b"nc-admin-pw", b"nc-db-pw"] {
            assert!(!sealed.windows(secret.len()).any(|w| w == secret));
        }
        let archive2 = snap2.join(SECRETS_ARCHIVE).to_string_lossy().to_string();
        let opened2 = sh(
            &[
                "openssl",
                "cms",
                "-decrypt",
                "-binary",
                "-inform",
                "DER",
                "-in",
                &archive2,
                "-inkey",
                &key,
                "-passin",
                "pass:sudo-pw",
                "-recip",
                &recip,
            ],
            None,
        );
        let listing = String::from_utf8(sh(&["tar", "-t", "-f", "-"], Some(&opened2))).unwrap();
        for member in [
            "secrets/accounts/APP-CREDENTIALS.txt",
            "compose/immich/.env",
            "compose/nextcloud/.env",
            "appdata/nextcloud/html/config/config.php",
        ] {
            assert!(
                listing.lines().any(|l| l == member),
                "{member} in {listing}"
            );
        }
        let wrong = std::process::Command::new("openssl")
            .args([
                "cms",
                "-decrypt",
                "-binary",
                "-inform",
                "DER",
                "-in",
                &archive,
                "-inkey",
                &key,
                "-passin",
                "pass:wrong",
                "-recip",
                &recip,
            ])
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status()
            .unwrap();
        assert!(!wrong.success());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn only_the_configured_qbittorrent_peer_port_may_be_public() {
        let qbit = json!({"environment":{"TORRENTING_PORT":"51413"}});
        let peer = peer_port("qbittorrent", &qbit);
        assert_eq!(peer, Some(51413));
        let tcp = json!({"target":51413,"published":"51413","protocol":"tcp"});
        let udp = json!({"host_ip":"0.0.0.0","target":51413,"published":"51413","protocol":"udp"});
        assert!(is_peer_binding(&tcp, peer) && is_peer_binding(&udp, peer));
        // Web UI, a different port, or a remapped port stays forbidden.
        for p in [
            json!({"target":8080,"published":"8080","protocol":"tcp"}),
            json!({"target":51414,"published":"51414","protocol":"tcp"}),
            json!({"target":51413,"published":"6881","protocol":"tcp"}),
        ] {
            assert!(!is_peer_binding(&p, peer), "{p}");
        }
        assert_eq!(peer_port("jellyfin", &qbit), None);
        assert_eq!(peer_port("qbittorrent", &json!({"environment":{}})), None);
        assert_eq!(
            peer_port(
                "qbittorrent",
                &json!({"environment":["TORRENTING_PORT=51413"]})
            ),
            Some(51413)
        );
    }
    #[test]
    fn monthly_snapshot_holds_app_state_not_photos_or_cloud_files() {
        let (data, media) = roots(&["immich".into(), "nextcloud".into(), "sonarr".into()]);
        assert!(data.contains(&"appdata/immich".into()));
        assert!(data.contains(&"appdata/nextcloud".into()));
        assert!(!data.iter().any(|p| p.starts_with("photos")));
        assert!(media.is_empty());
        assert_eq!(database_only_table("immich"), Some("asset"));
        assert_eq!(database_only_table("nextcloud"), Some("oc_filecache"));
        assert_eq!(database_only_table("jellyfin"), None);
    }
    #[test]
    fn nextcloud_test_files_are_cleaned_up_by_exact_name_only() {
        let root = scratch("probes");
        let first = probe_names(&root, "neko-maintenance-1-100").unwrap();
        assert!(first.is_empty());
        let second = probe_names(&root, "neko-maintenance-1-200").unwrap();
        assert_eq!(second, ["neko-maintenance-1-100"]);
        let php = probe_script(&second, "neko-maintenance-1-200");
        // Only the recorded name, and only while it holds its own name.
        assert!(php.contains("foreach(['neko-maintenance-1-100'] as $old)"));
        assert!(php.contains("$o->getContent()===$old"));
        assert!(!php.contains("strncmp"));
        assert!(!php.contains("getDirectoryListing"));
        // The saved administrator's name replaces this (see health).
        assert!(php.contains("getUserManager()->get(\"admin\")"));
        assert!(php.starts_with("require '/var/www/html/lib/base.php'; $u=\\OC::$server"));
        // A user's file that merely starts the same way can never be on the list.
        assert!(!probe_name("neko-maintenance-notes.txt"));
        assert!(!probe_name("neko-maintenance-1'); evil"));
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn space_is_counted_per_drive_and_each_image_once() {
        let gib = 1u64 << 30;
        // 10 GiB of app data, no media paths, 1 GiB of images (shared by 20 containers:
        // image_bytes counts it once).
        let (ssd, hdd) = space_needed(10 * gib, 0, gib);
        assert_eq!(ssd, 12 * gib); // rollback copy + new images + spare
        assert_eq!(hdd, 22 * gib); // snapshot (11) + restore test (10) + spare
        assert_eq!(space_needed(u64::MAX, 1, 1).0, u64::MAX);
    }
    #[test]
    fn a_damaged_backup_key_copy_fails_the_snapshot_check() {
        let root = scratch("key");
        let dir = root.join("hdd/backups/monthly/100-1");
        snapshot(&root, "hdd", "100-1", true);
        for (name, body) in [
            (SECRETS_ARCHIVE, "sealed"),
            (SECRETS_KEY, "key"),
            (SECRETS_CERT, "cert"),
        ] {
            fs::write(dir.join(name), body).unwrap();
        }
        let mut manifest = load(&dir.join("manifest.json")).unwrap();
        for (key, name) in [
            ("secrets", SECRETS_ARCHIVE),
            ("secrets_key", SECRETS_KEY),
            ("secrets_cert", SECRETS_CERT),
        ] {
            manifest["sha256"][key] = json!(common::digest(&dir.join(name)).unwrap());
        }
        common::atomic_json(&dir.join("manifest.json"), &manifest).unwrap();
        assert!(verify(&dir).is_ok());
        fs::write(dir.join(SECRETS_KEY), "kez").unwrap();
        let e = verify(&dir).unwrap_err().to_string();
        assert!(e.contains("backup key copy"), "{e}");
        fs::write(dir.join(SECRETS_KEY), "key").unwrap();
        fs::write(dir.join(SECRETS_CERT), "cerz").unwrap();
        assert!(verify(&dir)
            .unwrap_err()
            .to_string()
            .contains("certificate"));
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn monthly_retention_keeps_newest_complete_runs_on_the_hdd() {
        let root = scratch("keep");
        for run in ["100-1", "200-1", "300-1", "400-1", "1000-1"] {
            snapshot(&root, "hdd", run, true);
        }
        snapshot(&root, "usb", "100-1", false);
        let report = prune_monthly(&root.join("ssd"), &root.join("hdd"), 3, None).unwrap();
        // Numeric, not lexical, order: 1000-1 is the newest.
        assert_eq!(runs(&root, "hdd"), ["300-1", "400-1", "1000-1"]);
        assert_eq!(report["removed"].as_array().unwrap().len(), 2);
        // Monthly maintenance never touches the USB drive.
        assert_eq!(runs(&root, "usb"), ["100-1"]);
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn a_retired_run_takes_the_failed_update_data_it_set_aside_with_it() {
        let root = scratch("failed");
        let ssd = root.join("ssd");
        for run in ["100-1", "200-1"] {
            snapshot(&root, "hdd", run, true);
            for folder in ["appdata/jellyfin", "compose/jellyfin"] {
                fs::create_dir_all(ssd.join(format!("{folder}.neko-failed-{run}/x"))).unwrap();
            }
        }
        fs::create_dir_all(ssd.join("appdata/jellyfin")).unwrap();
        fs::create_dir_all(ssd.join("appdata/notes.neko-failed-100-1-keep")).unwrap();
        prune_monthly(&ssd, &root.join("hdd"), 1, None).unwrap();
        assert!(!ssd.join("appdata/jellyfin.neko-failed-100-1").exists());
        assert!(!ssd.join("compose/jellyfin.neko-failed-100-1").exists());
        // The kept run's copy, the live app and anything merely similar stay.
        assert!(ssd.join("appdata/jellyfin.neko-failed-200-1/x").exists());
        assert!(ssd.join("appdata/jellyfin").exists());
        assert!(ssd.join("appdata/notes.neko-failed-100-1-keep").exists());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn monthly_retention_never_removes_incomplete_or_current_runs() {
        let root = scratch("safe");
        snapshot(&root, "hdd", "100-1", true);
        snapshot(&root, "hdd", "150-1", false); // failed/partial run: reported, kept
        snapshot(&root, "hdd", "200-1", true);
        snapshot(&root, "hdd", "300-1", true);
        let report = prune_monthly(&root.join("ssd"), &root.join("hdd"), 1, Some("200-1")).unwrap();
        assert_eq!(runs(&root, "hdd"), ["150-1", "200-1", "300-1"]);
        assert_eq!(report["incomplete_not_removed"], json!(["150-1"]));
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn a_full_hdd_names_the_unfinished_snapshots_kept_there() {
        let folder = Path::new("/mnt/media/backups/monthly");
        assert_eq!(unfinished_note(folder, &[], 0), "");
        let note = unfinished_note(
            folder,
            &["150-1".to_string(), "250-1".to_string()],
            9_300_000_000,
        );
        assert!(note.contains("about 10 GB"), "{note}");
        assert!(
            note.contains("150-1, 250-1 in /mnt/media/backups/monthly"),
            "{note}"
        );
        assert!(note.contains("never deleted automatically"), "{note}");
    }
    #[test]
    fn monthly_retention_zero_keeps_everything_and_reclaims_old_ssd_copies() {
        let root = scratch("ssd");
        for run in ["100-1", "200-1"] {
            snapshot(&root, "hdd", run, true);
            snapshot(&root, "ssd", run, false);
        }
        snapshot(&root, "ssd", "300-1", false); // only on the SSD: the only copy, keep it
        fs::write(
            root.join("ssd/backups/monthly/200-1/manifest.json"),
            b"different",
        )
        .unwrap();
        prune_monthly(&root.join("ssd"), &root.join("hdd"), 0, None).unwrap();
        assert_eq!(runs(&root, "hdd"), ["100-1", "200-1"]);
        // 200-1 on the SSD differs from the HDD copy, so it is not ours to delete.
        assert_eq!(runs(&root, "ssd"), ["200-1", "300-1"]);
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn old_snapshots_stay_while_a_newer_kept_one_is_damaged() {
        let root = scratch("hold");
        for run in ["100-1", "200-1", "300-1"] {
            snapshot(&root, "hdd", run, true);
        }
        fs::write(root.join("hdd/backups/monthly/300-1/data.tar"), b"bit rot").unwrap();
        let report = prune_monthly(&root.join("ssd"), &root.join("hdd"), 1, None).unwrap();
        assert_eq!(runs(&root, "hdd"), ["100-1", "200-1", "300-1"]);
        assert!(report["held_back"].as_str().unwrap().contains("300-1"));
        // Once the newest is intact again, retention removes the older ones as usual.
        snapshot_archives(&root.join("hdd/backups/monthly/300-1"), "300-1");
        prune_monthly(&root.join("ssd"), &root.join("hdd"), 1, None).unwrap();
        assert_eq!(runs(&root, "hdd"), ["300-1"]);
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn ssd_copy_is_kept_when_the_hdd_archives_are_missing_or_damaged() {
        let root = scratch("ssd-verify");
        for run in ["100-1", "200-1", "300-1"] {
            snapshot(&root, "hdd", run, true);
            snapshot(&root, "ssd", run, false);
        }
        // Same manifest on both drives, but the HDD archive is gone or altered.
        fs::remove_file(root.join("hdd/backups/monthly/200-1/data.tar")).unwrap();
        fs::write(
            root.join("hdd/backups/monthly/300-1/images.tar"),
            b"bit rot",
        )
        .unwrap();
        prune_monthly(&root.join("ssd"), &root.join("hdd"), 0, None).unwrap();
        // Only 100-1 has a verified HDD copy, so only its SSD copy is reclaimed.
        assert_eq!(runs(&root, "ssd"), ["200-1", "300-1"]);
        assert_eq!(runs(&root, "hdd"), ["100-1", "200-1", "300-1"]);
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn monthly_manual_playback_does_not_block_healthy_automation() {
        let report = json!({"services":{"navidrome":{"result":"pass"}},"integrations":{},"readiness":{"navidrome:catalogue":{"result":"pass"},"end_to_end_media":{"result":"needs_setup"},"navidrome:authenticated_playback_or_transfer":{"result":"needs_setup"}}});
        assert!(automated_media_ready(&report));
    }
    #[test]
    fn monthly_automated_failures_and_missing_configuration_still_block() {
        for state in ["fail", "warning", "needs_setup"] {
            let report = json!({"services":{"navidrome":{"result":"pass"}},"integrations":{},"readiness":{"navidrome:catalogue":{"result":state}}});
            assert!(!automated_media_ready(&report));
        }
        assert!(!automated_media_ready(
            &json!({"services":{},"integrations":{},"readiness":{}})
        ));
    }
    #[test]
    fn root_scope_excludes_downloads_and_includes_all_state() {
        let (a, b) = roots(&[
            "immich".into(),
            "nextcloud".into(),
            "jellyfin".into(),
            "navidrome".into(),
            "lidarr".into(),
            "qbittorrent".into(),
        ]);
        assert!(a.contains(&"appdata/immich".into()));
        assert!(!b.contains(&"cloud/files".into()));
        assert!(a.contains(&"appdata/navidrome".into()));
        assert!(!a.contains(&"secrets/accounts".into()));
        assert!(!a.iter().chain(b.iter()).any(|p| p.starts_with("library")));
    }
    #[test]
    fn without_a_backup_key_the_password_list_is_in_data_tar() {
        let root = scratch("no-key");
        let data = root.join("ssd");
        for (rel, body) in [
            (
                "secrets/accounts/APP-CREDENTIALS.txt",
                "jellyfin: admin / pw\n",
            ),
            ("secrets/accounts/credentials.json", "{}\n"),
            ("appdata/jellyfin/config.xml", "<x/>\n"),
            ("compose/jellyfin/compose.yaml", "services: {}\n"),
            ("compose/jellyfin/.env", "TZ=UTC\n"),
        ] {
            fs::create_dir_all(data.join(rel).parent().unwrap()).unwrap();
            fs::write(data.join(rel), body).unwrap();
        }
        let names = vec!["jellyfin".to_string()];
        let archive = root.join("hdd/data.tar");
        fs::create_dir_all(archive.parent().unwrap()).unwrap();
        // No key: everything, the password list included, in the archive itself.
        let (paths, _) = snapshot_paths(&names, &data, false);
        tar(&data, &paths, &archive, &[]).unwrap();
        let listed =
            String::from_utf8(sh(&["tar", "-t", "-f", &archive.to_string_lossy()], None)).unwrap();
        for member in [
            "secrets/accounts/APP-CREDENTIALS.txt",
            "secrets/accounts/credentials.json",
            "compose/jellyfin/.env",
            "appdata/jellyfin/config.xml",
        ] {
            assert!(
                listed.lines().any(|l| l == member),
                "{member} missing:\n{listed}"
            );
        }
        // With a key they go only into the encrypted archive.
        let (sealed, _) = snapshot_paths(&names, &data, true);
        assert!(!sealed.contains(&"secrets/accounts".into()));
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn image_ids_are_immutable() {
        assert!(image_id(&format!("sha256:{}", "a".repeat(64))));
        assert!(!image_id("nextcloud:latest"));
    }
    #[test]
    fn nextcloud_major_notice_waits_for_early_fixes() {
        assert!(immich_major_approved("v3", "v3.0.2", "3"));
        assert!(immich_major_approved("v3.0.2", "v3.0.2", "3"));
        assert!(!immich_major_approved("v3.0.1", "v3.0.2", "3"));
        assert!(!immich_major_approved("v2", "v3.0.2", "3"));
        assert!(!immich_major_approved("", "v3.0.2", "3"));
        assert_eq!(nextcloud_recommended_major("v32.0.0"), Some(31));
        assert_eq!(nextcloud_recommended_major("v32.0.2"), Some(32));
        assert_eq!(nextcloud_recommended_major("v32.1.0"), Some(32));
        assert_eq!(nextcloud_recommended_major("32.1.0"), None);
        assert_eq!(
            installed_nextcloud_major("\"image\": \"nextcloud:31-apache\""),
            Some(31)
        );
        assert_eq!(
            installed_nextcloud_major("\"image\": \"postgres:17\""),
            None
        );
        assert_eq!(
            retag_nextcloud(
                "{\"image\": \"nextcloud:31-apache\", \"x\": \"postgres:17\"}",
                31,
                32
            ),
            "{\"image\": \"nextcloud:32-apache\", \"x\": \"postgres:17\"}"
        );
        let root = std::env::temp_dir().join(format!("neko-ncstep-{}", std::process::id()));
        fs::create_dir_all(&root).unwrap();
        fs::write(
            root.join("compose.yaml"),
            "{\"image\": \"nextcloud:31-apache\"}",
        )
        .unwrap();
        let cfg = |v: &str| json!({"maintenance":{"nextcloud_major_update":v}});
        assert_eq!(
            nextcloud_major_step(&cfg("32"), &root).unwrap(),
            Some((31, 32))
        );
        assert_eq!(nextcloud_major_step(&cfg("33"), &root).unwrap(), None);
        assert_eq!(nextcloud_major_step(&cfg(""), &root).unwrap(), None);
        assert_eq!(nextcloud_major_step(&json!({}), &root).unwrap(), None);
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn reject_uncovered_persistent_state() {
        let m = json!({"services":{"jellyfin":{"image":"test","ports":[{"host_ip":"127.0.0.1"}],"volumes":[{"type":"bind","source":"/etc","target":"/config"}]}}});
        assert!(valid_model(
            &m,
            "jellyfin",
            Path::new("/srv/neko"),
            Path::new("/mnt/neko")
        )
        .is_err());
    }
    #[test]
    fn a_cut_off_run_says_when_it_was_cut_off() {
        assert!(
            cut_off_reason(Some("snapshot")).contains("taking the snapshot, with the apps stopped")
        );
        assert!(cut_off_reason(Some("backed_up")).contains("testing the snapshot"));
        assert!(cut_off_reason(None).contains("before it took the snapshot"));
        assert!(cut_off_reason(Some("odd")).contains("restart) part-way"));
        assert!(cut_off_reason(Some("updating")).contains("the next monthly run tries again"));
    }
    /// Immich's redis has no volumes: the rehearsal copy must not gain "volumes": null
    /// (Compose refused the whole file with "services.redis.volumes must be a array").
    #[test]
    fn a_service_without_volumes_stays_without_them_in_the_rehearsal() {
        let m = json!({"services":{"redis":{"image":"valkey"},"server":{"image":"immich","volumes":[{"type":"bind","source":"/srv/neko/photos","target":"/data"}]}}});
        let clone = restored_model(
            &m,
            Path::new("/tmp/neko-stage-novol"),
            Path::new("/srv/neko"),
            Path::new("/mnt/neko"),
        )
        .unwrap();
        assert!(clone["services"]["redis"].get("volumes").is_none());
        assert!(clone["services"]["server"]["volumes"].is_array());
        let _ = fs::remove_dir_all("/tmp/neko-stage-novol");
    }
    #[test]
    fn rehearsal_never_joins_production_networks() {
        let m = json!({"name":"production","networks":{"media":{"external":true}},"services":{"jellyfin":{"image":"test","container_name":"live","networks":["media"],"ports":[{"host_ip":"0.0.0.0","published":"8096","target":8096}],"volumes":[]}}});
        let clone = restored_model(
            &m,
            Path::new("/tmp/neko-stage"),
            Path::new("/srv/neko"),
            Path::new("/mnt/neko"),
        )
        .unwrap();
        assert!(clone["networks"]["restore"].get("internal").is_none());
        assert_eq!(
            clone["networks"]["restore"]["driver_opts"]
                ["com.docker.network.bridge.enable_ip_masquerade"],
            "false"
        );
        assert!(clone["services"]["jellyfin"]
            .get("container_name")
            .is_none());
        assert_eq!(clone["services"]["jellyfin"]["ports"][0]["published"], "0");
    }
    fn models() -> Value {
        let port = |n: u16| json!([{"host_ip":"127.0.0.1","published":n.to_string(),"target":n}]);
        json!({
            "immich": {"services": {
                "immich-server": {"image":"sha256:1","ports":port(2283),"environment":{"DB_HOSTNAME":"database"}},
                "immich-machine-learning": {"image":"sha256:2"},
                "redis": {"image":"sha256:3"},
                "database": {"image":"sha256:4"}}},
            "nextcloud": {"services": {
                "nextcloud": {"image":"sha256:5","ports":port(8081)},
                "cron": {"image":"sha256:5"},
                "redis": {"image":"sha256:6"},
                "database": {"image":"sha256:7"}}},
            "jellyfin": {"services": {"jellyfin": {"image":"sha256:8","ports":port(8096)}}},
            "sonarr": {"services": {"sonarr": {"image":"sha256:9","ports":port(8989),"volumes":[
                {"type":"bind","source":"/srv/nekoserver/appdata/sonarr","target":"/config"},
                {"type":"bind","source":"/mnt/media/library","target":"/data"}]}}}
        })
    }
    #[test]
    fn checked_apps_with_user_files_are_unreachable_and_idle() {
        let all = models();
        let immich = quarantined(&all["immich"], "immich").unwrap();
        let server = &immich["services"]["immich-server"];
        assert_eq!(server["ports"][0]["published"], "0");
        assert_eq!(server["ports"][0]["host_ip"], "127.0.0.1");
        assert_eq!(server["environment"]["IMMICH_WORKERS_INCLUDE"], "api");
        assert_eq!(server["environment"]["DB_HOSTNAME"], "database");
        // A split set-up's exclusion list would leave the checked server no worker at all.
        let mut split = all["immich"].clone();
        split["services"]["immich-server"]["environment"]["IMMICH_WORKERS_EXCLUDE"] = json!("api");
        let checked = quarantined(&split, "immich").unwrap();
        let env = &checked["services"]["immich-server"]["environment"];
        assert!(env.get("IMMICH_WORKERS_EXCLUDE").is_none());
        assert_eq!(env["IMMICH_WORKERS_INCLUDE"], "api");
        assert_eq!(immich["services"].as_object().unwrap().len(), 4);
        let nextcloud = quarantined(&all["nextcloud"], "nextcloud").unwrap();
        assert!(nextcloud["services"].get("cron").is_none());
        assert_eq!(
            nextcloud["services"]["nextcloud"]["ports"][0]["published"],
            "0"
        );
        // The production model itself is untouched.
        assert_eq!(
            all["nextcloud"]["services"]["nextcloud"]["ports"][0]["published"],
            "8081"
        );
        assert!(guarded("immich") && guarded("nextcloud") && !guarded("jellyfin"));
        // Library writers keep their port (the media audit uses it) but see the library read-only.
        let sonarr = quarantined(&all["sonarr"], "sonarr").unwrap();
        let mounts = &sonarr["services"]["sonarr"]["volumes"];
        assert_eq!(
            mounts[0]["read_only"],
            Value::Null,
            "its own settings stay writable"
        );
        assert_eq!(mounts[1]["read_only"], true);
        assert_eq!(
            sonarr["services"]["sonarr"]["ports"][0]["published"],
            "8989"
        );
        assert!(
            held("sonarr")
                && held("radarr")
                && held("lidarr")
                && !held("jellyfin")
                && !held("qbittorrent")
        );
    }
    /// Runs go_live with stand-ins for docker; `fail` names the step that fails.
    fn rehearse(fail: &str) -> (Result<()>, Vec<String>) {
        let snapshot = scratch(&format!("go-live-{}", fail.replace([' ', '/'], "-")));
        let names: Vec<String> = ["immich", "nextcloud", "jellyfin", "sonarr"]
            .map(String::from)
            .to_vec();
        let steps = std::cell::RefCell::new(Vec::<String>::new());
        let step = |kind: &str, file: &Path| -> Result<()> {
            let label = format!("{kind} {}", file.file_name().unwrap().to_string_lossy());
            steps.borrow_mut().push(label.clone());
            if label == fail {
                Err(Error::new("induced failure"))
            } else {
                Ok(())
            }
        };
        let start = |file: &Path, _: &str| step("start", file);
        let check = |file: &Path, _: &str| step("check", file);
        let result = go_live(
            &names,
            &models(),
            &snapshot,
            &start,
            &check,
            || step("verify", Path::new("integrations")),
            || step("commit", Path::new("journal")),
        );
        let _ = fs::remove_dir_all(&snapshot);
        (result, steps.into_inner())
    }
    #[test]
    fn nothing_reaches_user_files_before_the_update_commits() {
        let (result, steps) = rehearse("");
        result.unwrap();
        assert_eq!(
            steps,
            [
                "start immich-quarantine.json",
                "start nextcloud-quarantine.json",
                "start jellyfin-new.json",
                "start sonarr-quarantine.json",
                "check immich-quarantine.json",
                "check nextcloud-quarantine.json",
                "check jellyfin-new.json",
                "check sonarr-quarantine.json",
                "verify integrations",
                "commit journal",
                "start immich-new.json",
                "start nextcloud-new.json",
                "start sonarr-new.json",
                "check immich-new.json",
                "check nextcloud-new.json",
                "check sonarr-new.json",
            ]
        );
    }
    #[test]
    fn a_failed_check_rolls_back_before_clients_could_write() {
        for fail in [
            "check nextcloud-quarantine.json",
            "check jellyfin-new.json",
            "check sonarr-quarantine.json",
            "verify integrations",
        ] {
            let (result, steps) = rehearse(fail);
            assert!(result.is_err(), "{fail}");
            // No commit, and the apps with user files never ran reachable or with jobs.
            assert!(
                !steps.iter().any(|s| s == "commit journal"),
                "{fail}: {steps:?}"
            );
            assert!(
                !steps.iter().any(|s| s == "start immich-new.json"
                    || s == "start nextcloud-new.json"
                    || s == "start sonarr-new.json"),
                "{fail}: {steps:?}"
            );
        }
    }
    #[test]
    fn a_failure_after_the_commit_keeps_the_update() {
        let (result, steps) = rehearse("check nextcloud-new.json");
        let message = result.unwrap_err().to_string();
        assert!(message.contains("Update installed"), "{message}");
        assert!(message.contains("without rolling back"), "{message}");
        let commit = steps.iter().position(|s| s == "commit journal").unwrap();
        let opened = steps
            .iter()
            .position(|s| s == "start nextcloud-new.json")
            .unwrap();
        assert!(commit < opened);
    }
}
