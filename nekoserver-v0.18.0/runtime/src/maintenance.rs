//! Monthly cold snapshots, isolated restored-stack probes and recoverable image updates.
//! The journal on the OS disk survives process death and SSD application restores.
use crate::{
    app_snapshot, backup,
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
fn private(path: &Path) -> Result<()> {
    let m = fs::metadata(common::safe(path)?)?;
    if m.uid() != 0 || m.mode() & 0o077 != 0 {
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
    let mut media = vec![];
    for name in names {
        data.extend([format!("appdata/{name}"), format!("compose/{name}")]);
    }
    if names.iter().any(|s| s == "immich") {
        data.push("photos/immich-library".into());
    }
    if names.iter().any(|s| s == "nextcloud") {
        media.push("cloud/files".into());
    }
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
                    || (["navidrome", "lidarr"].contains(&name) && p == data.join("library/music"))
                    || (["lidarr", "qbittorrent"].contains(&name)
                        && p == data.join("library/downloads/music"));
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
            if ports.is_empty() || ports.iter().any(|p| p["host_ip"] != "127.0.0.1") {
                return Err(Error::new("Loopback application ports required"));
            }
        } else if v["ports"].as_array().is_some_and(|p| !p.is_empty()) {
            return Err(Error::new("Unexpected supporting service publication"));
        }
    }
    Ok(())
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
        let result = compose(&file, &project, &["up", "-d", "--pull", "never"]).and_then(|_| {
            health(
                &file,
                &project,
                name,
                cfg["maintenance"]["health_wait_seconds"]
                    .as_u64()
                    .unwrap_or(300),
                true,
                report,
            )
        });
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
        verify(&snapshot)?;
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
        let allowed = roots(&names(cfg)?);
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
fn external_mount(cfg: &Value) -> Result<()> {
    let ext = common::mount_path(string(cfg, "external_backup_mount")?)?;
    let uuid = cfg["photos"]["external_backup_uuid"].as_str().unwrap_or("");
    if uuid.is_empty() || !uuid.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-') {
        return Err(Error::new("External UUID required"));
    }
    if cfg["photos"]["external_backup_fstype"] == "exfat" {
        let (code, targets) = common::command(
            &[
                "findmnt",
                "-rn",
                "--source",
                &format!("UUID={uuid}"),
                "-o",
                "TARGET",
            ],
            10,
        )?;
        if code == 0 && targets.lines().any(|p| p.trim() != ext.to_string_lossy()) {
            return Err(Error::new(
                "Unmount other exFAT aliases before storing private full backups",
            ));
        }
    }
    if !common::mounted(&ext) {
        let kind = cfg["photos"]["external_backup_fstype"]
            .as_str()
            .unwrap_or("ext4");
        if !["ext4", "xfs", "btrfs", "exfat"].contains(&kind) {
            return Err(Error::new("Unsupported external filesystem"));
        }
        common::directory(&ext, 0o755)?;
        let opts = if kind == "exfat" {
            "nodev,nosuid,noexec,uid=0,gid=0,umask=077"
        } else {
            "nodev,nosuid,noexec"
        };
        checked(
            &[
                "mount",
                "-t",
                kind,
                "-o",
                opts,
                &format!("UUID={uuid}"),
                &ext.to_string_lossy(),
            ],
            30,
        )?;
    }
    backup::validate_external(&ext, uuid)?;
    let mut disks = std::collections::BTreeSet::new();
    for key in [
        cfg["storage"]["data_ssd"]["uuid"].as_str().unwrap_or(""),
        cfg["storage"]["media_hdd"]["uuid"].as_str().unwrap_or(""),
        uuid,
    ] {
        let text = checked(
            &[
                "lsblk",
                "-srn",
                "-o",
                "NAME,TYPE",
                &format!("/dev/disk/by-uuid/{key}"),
            ],
            15,
        )?;
        let parents = text
            .lines()
            .filter_map(|line| {
                let cols = line.split_whitespace().collect::<Vec<_>>();
                if cols.len() == 2 && cols[1] == "disk" {
                    Some(cols[0].to_string())
                } else {
                    None
                }
            })
            .collect::<Vec<_>>();
        if parents.len() != 1 || !disks.insert(parents[0].clone()) {
            return Err(Error::new("Three distinct physical disks required"));
        }
    }
    Ok(())
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
    if before[1] != next[1] {
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

fn transaction(cfgpath: &Path, cfg: &Value) -> Result<()> {
    external_mount(cfg)?;
    mounts(cfgpath, cfg, true)?;
    let data = Path::new(string(cfg, "data_mount")?);
    let media = Path::new(string(cfg, "media_mount")?);
    let external = Path::new(string(cfg, "external_backup_mount")?);
    let names = names(cfg)?;
    let paths = roots(&names);
    let reserve = cfg["maintenance"]["minimum_free_gb"]
        .as_u64()
        .unwrap_or(20)
        .saturating_mul(1 << 30);

    let bytes = size(data, &paths.0)?
        .saturating_add(size(media, &paths.1)?)
        .saturating_add(1 << 30);
    let selected_images = docker(&["ps", "-aq", "--no-trunc"])?;
    let mut image_bytes = 0u64;
    for id in selected_images.split_whitespace() {
        let v: Value = serde_json::from_str(&docker(&["inspect", id])?)?;
        let image = string(&v[0], "Image")?;
        let meta: Value = serde_json::from_str(&docker(&["image", "inspect", image])?)?;
        image_bytes = image_bytes.saturating_add(meta[0]["Size"].as_u64().unwrap_or(1 << 30));
    }
    let bytes = bytes.saturating_add(image_bytes);
    // SSD/HDD also need space to stage restoration; never delete old backups to make room.
    for (root, multiplier) in [(data, 2), (media, 3), (external, 1)] {
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
    mounts(cfgpath, cfg, true)?;
    tar(data, &paths.0, &snapshot.join("data.tar"))?;
    tar(media, &paths.1, &snapshot.join("media.tar"))?;
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
    let manifest = json!({"format":"neko-maintenance-v1","version":VERSION,"created_at":common::now(),"storage":cfg["storage"],"paths":{"data":paths.0,"media":paths.1},"sha256":{"data":common::digest(&snapshot.join("data.tar"))?,"media":common::digest(&snapshot.join("media.tar"))?,"images":common::digest(&image_archive)?}});
    common::atomic_json(&snapshot.join("manifest.json"), &manifest)?;
    verify(&snapshot)?;
    let mut journal = load(Path::new(JOURNAL))?;
    journal["phase"] = json!("backed_up");
    save_journal(&journal)?;
    for root in [data, external] {
        mounts(cfgpath, cfg, true)?;
        let dest = root.join("backups/monthly").join(&run);
        common::directory(&dest, 0o700)?;
        private(&dest)?;
        for file in fs::read_dir(&snapshot)? {
            let file = file?;
            if file.file_type()?.is_file() {
                app_snapshot::copy_file(&file.path(), &dest.join(file.file_name()))?;
            }
        }
        verify(&dest)?;
    }
    rehearsal(&snapshot, cfg, &models, &snapshot)?;
    common::atomic_json(
        &snapshot.join("restore-tests.json"),
        &json!({"status":"pass","method":"isolated containers started from extracted backups, HTTP probes, Immich SQL, Nextcloud database/file round trip","at":common::now()}),
    )?;
    if !night(cfg) {
        return Err(Error::new(
            "Night window ended; backups complete, updates deferred",
        ));
    }
    // Pull the declared version streams. Major-version migrations and OS upgrades are excluded.
    let mut updated = json!({});
    let mut immich_files = None;
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
    mounts(cfgpath, cfg, true)?;
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
        &json!({"status":"pass","updated_at":common::now(),"restore_test":"pass","three_copies":"verified","images":updated}),
    )?;
    fs::remove_file(JOURNAL)?;
    fs::File::open(Path::new(JOURNAL).parent().unwrap())?.sync_all()?;
    Ok(())
}
pub fn run(a: &Args, recover: bool) -> Result<()> {
    common::require_root()?;
    let cfgpath = a.path("config")?;
    private(&cfgpath)?;
    let cfg = load(&cfgpath)?;
    let data = common::mount_path(string(&cfg, "data_mount")?)?;
    mounts(&cfgpath, &cfg, false)?;
    let _lock = common::lock(&data.join("installer/recovery.lock"))?;
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
        assert!(b.contains(&"cloud/files".into()));
        assert!(a.contains(&"appdata/navidrome".into()));
        assert!(!a.iter().chain(b.iter()).any(|p| p.starts_with("library")));
    }
    #[test]
    fn image_ids_are_immutable() {
        assert!(image_id(&format!("sha256:{}", "a".repeat(64))));
        assert!(!image_id("nextcloud:latest"));
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
