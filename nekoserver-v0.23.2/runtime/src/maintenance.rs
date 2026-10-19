//! Monthly cold snapshots, isolated restored-stack probes and recoverable image updates.
//! The journal on the OS disk survives process death and SSD application restores.
use crate::{
    backup,
    common::{self, Error, Result},
    Args, VERSION,
};
use serde_json::{json, Value};
use std::{
    fs,
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
    thread,
    time::{Duration, Instant},
};
const JOURNAL: &str = "/etc/nekoserver-backups/maintenance-resume.json";
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
        return Err(Error::new(
            "Maintenance subprocess failed; private journal retained",
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
        if info[0]["State"]["Running"] == true
            || info[0]["State"]["ExitCode"] == 137
            || info[0]["State"]["OOMKilled"] == true
        {
            return Err(Error::new("Unclean application shutdown; update refused"));
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
fn tar(root: &Path, paths: &[String], output: &Path) -> Result<()> {
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
/// drive. The sudo-password-protected key travels with the snapshot for restores.
/// Returns "encrypted", "none" (no password folder) or "skipped_no_key".
fn encrypt_secrets(data: &Path, snapshot: &Path) -> Result<&'static str> {
    encrypt_secrets_with(data, snapshot, Path::new(SECRETS_KEY_DIR))
}
fn encrypt_secrets_with(data: &Path, snapshot: &Path, key_dir: &Path) -> Result<&'static str> {
    if !data.join("secrets/accounts").exists() {
        return Ok("none");
    }
    let key = key_dir.join(SECRETS_KEY);
    let cert = key_dir.join(SECRETS_CERT);
    if !key.is_file() || !cert.is_file() {
        return Ok("skipped_no_key");
    }
    private(&key)?;
    private(&cert)?;
    let output = snapshot.join(SECRETS_ARCHIVE);
    let mut archive = std::process::Command::new("tar")
        .args(["--create", "--file", "-", "--acls", "--xattrs", "-C"])
        .arg(data)
        .args(["--", "secrets/accounts"])
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
fn verify(snapshot: &Path) -> Result<Value> {
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
    if let Some(expected) = manifest["sha256"]["secrets"].as_str() {
        if common::digest(&snapshot.join(SECRETS_ARCHIVE))? != expected {
            return Err(Error::new("Snapshot password archive checksum mismatch"));
        }
    }
    Ok(manifest)
}
fn extract(snapshot: &Path, target: &Path) -> Result<()> {
    verify(snapshot)?;
    if target.exists() {
        private(target)?;
        if target.join("extraction-complete.json").exists() {
            return Ok(());
        }
        fs::remove_dir_all(common::safe(target)?)?;
    }
    common::directory(target, 0o700)?;
    for key in ["data", "media"] {
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
fn curl(url: &str, output: Option<&Path>) -> Result<String> {
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
        "seerr" => (5055, "/api/v1/status"),
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
            let expected = if ["immich", "nextcloud"].contains(&name) {
                4
            } else {
                1
            };
            if count != expected {
                return Err(Error::new("Missing healthy services"));
            }
            let binding = compose(file, project, &["port", service, &port.to_string()])?;
            let binding = binding.trim();
            let published = binding
                .strip_prefix("127.0.0.1:")
                .filter(|s| s.parse::<u16>().is_ok())
                .ok_or_else(|| Error::new("Private health port required"))?;
            let body = curl(&format!("http://127.0.0.1:{published}{path}"), None)?;
            if name == "nextcloud" {
                let s: Value = serde_json::from_str(&body)?;
                if s["installed"] != true
                    || s["maintenance"] != false
                    || s["needsDbUpgrade"] != false
                {
                    return Err(Error::new("Nextcloud migration incomplete"));
                }
                // Authenticated real database/file operations, without storing a user's password.
                let token = format!("neko-maintenance-{}", std::process::id());
                let script=format!("require '/var/www/html/lib/base.php'; $u=\\OC::$server->getUserManager()->get(\"admin\"); if(!$u){{exit(3);}} $root=\\OC::$server->getUserFolder($u->getUID()); $n='{token}'; if($root->nodeExists($n)){{exit(4);}} $f=$root->newFile($n); $f->putContent('{token}'); if($f->getContent()!=='{token}'){{exit(5);}} $f->delete();");
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
    model["networks"] = json!({"restore":{"internal":true}});
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
        for volume in v["volumes"].as_array_mut().into_iter().flatten() {
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
fn rehearsal(snapshot: &Path, cfg: &Value, models: &Value, report: &Path) -> Result<()> {
    let stage = snapshot.join("restore-test");
    extract(snapshot, &stage)?;
    let data = Path::new(string(cfg, "data_mount")?);
    let media = Path::new(string(cfg, "media_mount")?);
    for (name, original) in models.as_object().unwrap() {
        let clone = restored_model(original, &stage, data, media)?;
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
    fs::remove_dir_all(common::safe(&stage)?)?;
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
    if ["updating", "restoring"].contains(&phase.as_str()) {
        let snapshot_manifest = verify(&snapshot)?;
        for (name, _) in journal["models"].as_object().unwrap() {
            let file = snapshot.join(format!("{name}-old.json"));
            compose(&file, &format!("neko-{name}"), &["stop", "--timeout", "60"])?;
        }
        if phase == "updating" {
            let restore = snapshot.join("rollback-stage");
            extract(&snapshot, &restore)?;
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
            journal["phase"] = json!("restoring");
            journal["restored"] = json!([]);
            save_journal(&journal)?;
        }
        let mut allowed = roots(&names(cfg)?);
        let has_accounts = snapshot_manifest["paths"]["data"]
            .as_array()
            .is_some_and(|paths| paths.iter().any(|p| p == "secrets/accounts"));
        if !has_accounts {
            allowed.0.retain(|p| p != "secrets/accounts");
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
    fs::remove_file(JOURNAL)?;
    fs::File::open(Path::new(JOURNAL).parent().unwrap())?.sync_all()?;
    Ok(())
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
fn immich_candidate(cfg: &Value, snapshot: &Path) -> Result<Option<PathBuf>> {
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
    let folder = snapshot.join("immich-candidate");
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

fn integrations(cfgpath: &Path, cfg: &Value) -> Result<()> {
    if cfg["media"]["services"]
        .as_array()
        .is_none_or(|v| v.is_empty())
    {
        return Ok(());
    }
    let exe = std::env::current_exe()?;
    let report = Path::new(string(cfg, "data_mount")?).join("reports/media/latest.json");
    let (code, _) = common::command(
        &[
            &exe.to_string_lossy(),
            "media-health",
            "--config",
            &cfgpath.to_string_lossy(),
            "--output",
            &report.to_string_lossy(),
        ],
        900,
    )?;
    // Exit 3 also includes intentionally unwitnessed human playback checks.
    if ![0, 3].contains(&code) || !automated_media_ready(&load(&report)?) {
        return Err(Error::new("Live automated media connections are not ready"));
    }
    Ok(())
}

fn keep_snapshots(cfg: &Value) -> u64 {
    cfg["maintenance"]["keep_snapshots"].as_u64().unwrap_or(3)
}
fn monthly_runs(root: &Path) -> Result<Vec<String>> {
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
    if keep > 0 {
        let cut = complete.len().saturating_sub(keep as usize);
        for run in &complete[..cut] {
            if protected(run) {
                kept.push(run.clone());
                continue;
            }
            remove_run(&hdd.join(run))?;
            removed.push(json!({"run":run,"drive":"hdd"}));
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
    Ok(json!({"keep":keep,"kept":kept,"removed":removed,"incomplete_not_removed":unmanaged}))
}
fn transaction(cfgpath: &Path, cfg: &Value) -> Result<()> {
    mounts(cfgpath, cfg, false)?;
    let data = Path::new(string(cfg, "data_mount")?);
    let media = Path::new(string(cfg, "media_mount")?);
    let names = names(cfg)?;
    let paths = roots(&names);
    let reserve = cfg["maintenance"]["minimum_free_gb"]
        .as_u64()
        .unwrap_or(20)
        .saturating_mul(1 << 30);

    let bytes = size(data, &paths.0)?
        .saturating_add(size(media, &paths.1)?)
        .saturating_add(1 << 30);
    let keep = keep_snapshots(cfg);
    let mut retention = prune_monthly(data, media, keep, None)?;
    let selected_images = docker(&["ps", "-aq", "--no-trunc"])?;
    let mut image_bytes = 0u64;
    for id in selected_images.split_whitespace() {
        let v: Value = serde_json::from_str(&docker(&["inspect", id])?)?;
        let image = string(&v[0], "Image")?;
        let meta: Value = serde_json::from_str(&docker(&["image", "inspect", image])?)?;
        image_bytes = image_bytes.saturating_add(meta[0]["Size"].as_u64().unwrap_or(1 << 30));
    }
    let bytes = bytes.saturating_add(image_bytes);
    // HDD holds the snapshot, the restore test and a rollback stage; the SSD only a
    // rollback stage. The USB drive is not used by monthly maintenance.
    for (root, multiplier) in [(data, 1), (media, 3)] {
        if free(root)? < bytes.saturating_mul(multiplier).saturating_add(reserve) {
            return Err(Error::new(
                "Not enough space for backup and verified recovery",
            ));
        }
    }
    let run = format!("{}-{}", (common::now() * 1e6) as u128, std::process::id());
    let snapshot = media.join("backups/monthly").join(&run);
    common::directory(&snapshot, 0o700)?;
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
    integrations(cfgpath, cfg)?;
    no_other_writers(&rows, data, media, &paths)?;
    common::directory(Path::new("/etc/nekoserver-backups"), 0o700)?;
    save_journal(
        &json!({"format":"neko-maintenance-resume-v1","version":VERSION,"run":run,"phase":"snapshot","snapshot":snapshot,"storage":cfg["storage"],"data_mount":cfg["data_mount"],"media_mount":cfg["media_mount"],"models":models,"containers":rows,"rehearsals":[]}),
    )?;
    stop(&rows)?;
    mounts(cfgpath, cfg, false)?;
    tar(data, &paths.0, &snapshot.join("data.tar"))?;
    tar(media, &paths.1, &snapshot.join("media.tar"))?;
    let password_files = encrypt_secrets(data, &snapshot)?;
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
        manifest["sha256"]["secrets"] = json!(common::digest(&snapshot.join(SECRETS_ARCHIVE))?);
    }
    common::atomic_json(&snapshot.join("manifest.json"), &manifest)?;
    verify(&snapshot)?;
    let mut journal = load(Path::new(JOURNAL))?;
    journal["phase"] = json!("backed_up");
    save_journal(&journal)?;
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
    // Pull the declared version streams. Major-version migrations and OS upgrades are excluded.
    let mut updated = json!({});
    let mut immich_files = None;
    let mut nextcloud_step = None;
    for name in &names {
        let source = if name == "immich" {
            immich_files = immich_candidate(cfg, &snapshot)?;
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
    // Originals have remained stopped since the consistent backup; no writes are lost.
    for name in &names {
        let file = snapshot.join(format!("{name}-new.json"));
        common::atomic_json(&file, &updated[name])?;
        compose(
            &file,
            &format!("neko-{name}"),
            &["up", "-d", "--pull", "never"],
        )?;
    }
    for name in &names {
        health(
            &snapshot.join(format!("{name}-new.json")),
            &format!("neko-{name}"),
            name,
            cfg["maintenance"]["health_wait_seconds"]
                .as_u64()
                .unwrap_or(300),
            false,
            &snapshot,
        )?;
    }
    integrations(cfgpath, cfg)?;
    mounts(cfgpath, cfg, false)?;
    // Persist successful image identities for installer reruns and reboot reproducibility.
    for name in &names {
        write_override(cfg, name, &updated[name])?;
    }
    common::atomic_json(
        &snapshot.join("result.json"),
        &json!({"status":"pass","updated_at":common::now(),"restore_test":"pass","snapshot":"hdd","images":updated}),
    )?;
    fs::remove_file(JOURNAL)?;
    fs::File::open(Path::new(JOURNAL).parent().unwrap())?.sync_all()?;
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
    resume(&cfgpath, &cfg)?;
    if recover {
        let report = data.join("reports/jobs/maintenance.json");
        if report.exists() {
            let mut value = load(&report)?;
            if value["status"] == "running" {
                value["status"] = json!("fail");
                value["finished_at"] = json!(common::now());
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
    backup::execute(Some(&data), "maintenance", &cfgpath, || {
        let result = transaction(&cfgpath, &cfg);
        if result.is_err() {
            resume(&cfgpath, &cfg)?;
        }
        result
    })
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
        // A real-looking snapshot: archives plus a manifest with their checksums.
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
        if complete {
            fs::write(dir.join("restore-tests.json"), b"{}").unwrap();
        }
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
        // No key yet: nothing is written, and nothing is written in plain text.
        assert_eq!(
            encrypt_secrets_with(&data, &snap, &keys).unwrap(),
            "skipped_no_key"
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
    fn rehearsal_never_joins_production_networks() {
        let m = json!({"name":"production","networks":{"media":{"external":true}},"services":{"jellyfin":{"image":"test","container_name":"live","networks":["media"],"ports":[{"host_ip":"0.0.0.0","published":"8096","target":8096}],"volumes":[]}}});
        let clone = restored_model(
            &m,
            Path::new("/tmp/neko-stage"),
            Path::new("/srv/neko"),
            Path::new("/mnt/neko"),
        )
        .unwrap();
        assert_eq!(clone["networks"]["restore"]["internal"], true);
        assert!(clone["services"]["jellyfin"]
            .get("container_name")
            .is_none());
        assert_eq!(clone["services"]["jellyfin"]["ports"][0]["published"], "0");
    }
}
