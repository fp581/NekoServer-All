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
pub(crate) static JOURNAL: std::sync::LazyLock<&str> =
    std::sync::LazyLock::new(|| crate::common::journal("maintenance"));
use crate::common::MEDIA_APPS as MEDIA;
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
    let listed = cfg["media"]["services"].as_array();
    if listed.is_some_and(|v| v.iter().any(|n| !n.is_string())) {
        return Err(Error::new("Invalid media selection"));
    }
    for n in common::media_services(cfg) {
        if !MEDIA.contains(&n) || names.contains(&n.to_string()) {
            return Err(Error::new("Invalid media selection"));
        }
        names.push(n.to_string());
    }
    if common::immich_wanted(cfg) {
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
#[cfg(test)]
mod assistant_tests {
    use super::*;
    /// The AI assistant's models (appdata/assistant/models) are downloaded during setup and
    /// never kept in a monthly snapshot: no app name the snapshot takes can be it.
    #[test]
    fn the_monthly_snapshot_never_takes_the_assistants_models() {
        let cfg = json!({"media": {"services": ["jellyfin", "seerr"]}, "photos": {"deploy_immich": true},
            "nextcloud": {"enabled": true}, "assistant": {"wanted": true, "model": "qwen3.5:4b"}});
        let taken = names(&cfg).unwrap();
        assert!(!taken.iter().any(|n| n == "assistant"), "{taken:?}");
        let (data, media) = roots(&taken);
        assert!(
            !data.iter().chain(&media).any(|p| p.contains("assistant")),
            "{data:?}"
        );
        let sneaked = json!({"media": {"services": ["jellyfin", "assistant"]}});
        assert!(names(&sneaked).is_err());
    }
}
/// What a rollback puts back: the paths this run's snapshot holds (its manifest) for
/// the apps this run changed (its journal), nothing else. An app update snapshots only
/// its own app; asking for every app set up would find the others missing and stop the
/// rollback half-way ("Rollback state incomplete"). A snapshot made without a backup key
/// holds the password list (secrets/accounts) in data.tar too.
fn rollback_paths(apps: &[String], manifest: &Value) -> (Vec<String>, Vec<String>) {
    let wanted = roots(apps);
    let held = |kind: &str, ours: &[String], extra: &[&str]| -> Vec<String> {
        manifest["paths"][kind]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .filter(|p| ours.iter().any(|o| o == p) || extra.contains(p))
            .map(str::to_string)
            .collect()
    };
    (
        held("data", &wanted.0, &["secrets/accounts"]),
        held("media", &wanted.1, &[]),
    )
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
/// (free, size) bytes of the drive holding `path`.
fn drive_space(path: &Path) -> Result<(u64, u64)> {
    let name = std::ffi::CString::new(path.as_os_str().as_encoded_bytes())
        .map_err(|_| Error::new("Invalid path"))?;
    let mut s = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    if unsafe { libc::statvfs(name.as_ptr(), s.as_mut_ptr()) } != 0 {
        return Err(Error::new("Cannot check free space"));
    }
    let s = unsafe { s.assume_init() };
    Ok((
        s.f_bavail.saturating_mul(s.f_frsize),
        s.f_blocks.saturating_mul(s.f_frsize),
    ))
}
/// The space the run keeps free on a drive of `size` bytes: `maintenance.minimum_free_gb`
/// (room for the apps' files to keep growing after the update), but at most a fifth of
/// the drive. Setup accepts an SSD with 20 GB free and an HDD with 50 GB, and the
/// setting's default of 20 GB alone would then stop every monthly run for good.
fn reserve_for(setting: u64, size: u64) -> u64 {
    setting.min(size / 5)
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
/// The graphics devices Immich's hardware.yaml hands to its server. Docker refuses to
/// create a container whose device is missing (a graphics card taken out, a driver
/// gone), so without them Immich starts without that file and converts in software.
fn graphics_present() -> bool {
    Path::new("/dev/dri").exists()
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
    let hardware = dir.join(HARDWARE);
    let hardware_name = hardware.to_string_lossy();
    if hardware.exists() && graphics_present() {
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
/// A service asking for more than NekoServer's apps have. Only the keys NekoServer knows
/// what they give an app are allowed (shared.json "compose"): anything else, such as
/// `privileged`, `cap_add`, another container's volumes, a host name space,
/// `use_api_socket` (Docker's own socket), a `post_start` hook, `secrets` or `configs`
/// read from this PC's files, or a key a later Docker adds, is refused. A security
/// option other than no-new-privileges is refused too. A downloaded Compose file (a new
/// Immich release) with one is not used.
fn widens_rights(v: &Value) -> bool {
    let allowed = common::shared_list(&["compose", "security_opt"]);
    let known = common::shared_list(&["compose", "service_keys"]);
    v.as_object().is_none_or(|o| {
        o.iter().any(|(k, value)| {
            // `privileged: false` is what Docker does anyway.
            !(known.contains(&k.as_str()) || (k == "privileged" && *value == false))
        })
    }) || v.get("security_opt").is_some_and(|o| {
        o.as_array().is_none_or(|opts| {
            opts.iter()
                .any(|o| !allowed.contains(&o.as_str().unwrap_or("")))
        })
    })
}
/// The devices a service is given: none, or the graphics chip as NekoServer's apps get it
/// (`/dev/dri`, or one of its card / render nodes, at the same place), for Jellyfin and
/// Immich's server only. Compose shows each as "/dev/dri:/dev/dri[:rwm]" or as
/// {"source", "target", "permissions"}.
fn gpu_only(service: &str, v: &Value) -> bool {
    let Some(devices) = v.get("devices") else {
        return true;
    };
    let Some(list) = devices.as_array() else {
        return false;
    };
    let chip = |path: &str| {
        path == "/dev/dri"
            || path
                .strip_prefix("/dev/dri/")
                .and_then(|n| n.strip_prefix("renderD").or_else(|| n.strip_prefix("card")))
                .is_some_and(|d| {
                    !d.is_empty() && d.len() <= 4 && d.bytes().all(|b| b.is_ascii_digit())
                })
    };
    let mapping = |d: &Value| -> Option<(String, String, String)> {
        if let Some(text) = d.as_str() {
            let mut parts = text.split(':');
            let source = parts.next()?.to_string();
            let target = parts.next().unwrap_or(&source).to_string();
            let rights = parts.next().unwrap_or("rwm").to_string();
            return parts.next().is_none().then_some((source, target, rights));
        }
        let o = d.as_object()?;
        if o.keys()
            .any(|k| !["source", "target", "permissions"].contains(&k.as_str()))
        {
            return None;
        }
        let source = o.get("source")?.as_str()?.to_string();
        let target = o
            .get("target")
            .and_then(Value::as_str)
            .unwrap_or(&source)
            .to_string();
        let rights = o
            .get("permissions")
            .and_then(Value::as_str)
            .unwrap_or("rwm")
            .to_string();
        Some((source, target, rights))
    };
    ["jellyfin", "immich-server"].contains(&service)
        && list.iter().all(|d| {
            mapping(d).is_some_and(|(source, target, rights)| {
                chip(&source)
                    && source == target
                    && !rights.is_empty()
                    && rights.chars().all(|c| "rwm".contains(c))
            })
        })
}
/// A named volume as Docker makes it by itself: on its own storage, never one defined to
/// bind a folder of this PC (driver_opts "o: bind", "device: /"), another driver's, one
/// made outside this Compose file (`external`), or one under another name: Compose would
/// use any volume of that name that already exists, whatever it holds. Its name is the
/// app's own (`neko-<app>_<key>`, as Compose names it).
fn plain_volume(name: &str, key: &str, v: &Value) -> bool {
    let Some(o) = v.as_object() else {
        return v.is_null();
    };
    o.get("name")
        .is_none_or(|n| n.as_str() == Some(format!("neko-{name}_{key}").as_str()))
        && o.get("driver").is_none_or(|d| d == "local")
        && o.get("driver_opts")
            .is_none_or(|d| d.as_object().is_some_and(|m| m.is_empty()))
        && o.get("external").is_none_or(|e| e == false)
        && o.keys()
            .all(|k| ["name", "driver", "driver_opts", "external", "labels"].contains(&k.as_str()))
}
/// The named volumes a model declares, as Docker has them now: each one that already
/// exists must be the one Compose made for this app (its labels), on Docker's own storage
/// with no options. Compose would use any existing volume of that name as it is
/// (`external: false` notwithstanding), also one made by hand to bind a folder of this PC
/// or holding someone else's files.
fn volumes_ours(name: &str, model: &Value) -> Result<()> {
    for (key, v) in model["volumes"].as_object().into_iter().flatten() {
        let volume = v["name"]
            .as_str()
            .map_or_else(|| format!("neko-{name}_{key}"), str::to_string);
        let found = docker(&["volume", "ls", "-q", "--filter", &format!("name={volume}")])?;
        if !found.lines().any(|line| line.trim() == volume) {
            continue; // Compose makes it, with its labels
        }
        let inspected: Value = serde_json::from_str(&docker(&["volume", "inspect", &volume])?)?;
        if !volume_is_ours(name, key, &inspected[0]) {
            return Err(Error::new(&format!(
                "The Docker volume {volume} was not made by NekoServer for {name} (or has storage options): \
                 Compose would use it as it is. Look at it with `sudo docker volume inspect {volume}`; \
                 if it is not needed, remove it (`sudo docker volume rm {volume}`) and run setup again"
            )));
        }
    }
    Ok(())
}
/// One volume as `docker volume inspect` shows it: Compose's own for this app's `key`.
fn volume_is_ours(name: &str, key: &str, v: &Value) -> bool {
    let labels = &v["Labels"];
    v["Driver"] == "local"
        && v["Options"].as_object().is_none_or(|o| o.is_empty())
        && labels["com.docker.compose.project"] == format!("neko-{name}").as_str()
        && labels["com.docker.compose.volume"] == key
}
/// `locations`: where setup put the photos, the music and Nextcloud's files (the
/// settings' "locations"; null: the layout before 0.46.0).
fn valid_model(model: &Value, name: &str, data: &Path, media: &Path, cfg: &Value) -> Result<()> {
    // The folders setup decided (`paths`, or shared.json's defaults for settings written
    // before it kept them): read, never worked out again here (the review of 0.47.0).
    let mut view = cfg.clone();
    if !view.is_object() {
        view = json!({});
    }
    view["data_mount"] = json!(data);
    view["media_mount"] = json!(media);
    let places = Places {
        name,
        data,
        photos: common::saved_path(&view, "photos")?,
        cloud: common::saved_path(&view, "cloud")?,
        music: common::saved_path(&view, "music")?,
        downloads: common::saved_path(&view, "downloads")?,
        library: common::movies_library(&view)?,
    };
    let services = model["services"]
        .as_object()
        .ok_or_else(|| Error::new("Compose services missing"))?;
    let file_keys = common::shared_list(&["compose", "file_keys"]);
    if model
        .as_object()
        .is_none_or(|o| o.keys().any(|k| !file_keys.contains(&k.as_str())))
    {
        // `secrets` and `configs` hand the app files of this PC.
        return Err(Error::new("Unsupported Compose settings"));
    }
    if let Some(volumes) = model.get("volumes") {
        let plain = volumes
            .as_object()
            .is_some_and(|all| all.iter().all(|(key, v)| plain_volume(name, key, v)));
        if !plain {
            return Err(Error::new("Uncovered persistent volume"));
        }
    }
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
    networks_ok(name, model)?;
    for (service, v) in services {
        if !isolated(name, service, v) {
            return Err(Error::new("Unsupported Compose isolation"));
        }
        for volume in v["volumes"].as_array().cloned().unwrap_or_default() {
            places.volume_ok(service, &volume)?;
        }
        published_ok(name, service, v)?;
    }
    if name == "immich" {
        immich_ok(services, data, &places.photos)?;
    }
    Ok(())
}
/// The networks a model may use: its project's own (`neko-<app>_<key>`, made by Compose
/// with no driver or options of its own), and for the media apps the shared
/// `nekoserver-media` they reach each other on. Never the host's network, one bridged
/// straight onto the home network (macvlan), another app's, nor a name or alias on the
/// shared network that could answer for another app (the 0.46.1 audit).
fn networks_ok(name: &str, model: &Value) -> Result<()> {
    let refused = || Err(Error::new("Unsupported Compose network"));
    let shared = MEDIA.contains(&name);
    let mut known = vec![];
    for (key, v) in model["networks"].as_object().into_iter().flatten() {
        let Some(o) = v.as_object() else {
            return refused();
        };
        let ipam_empty = o
            .get("ipam")
            .is_none_or(|i| i.as_object().is_some_and(|m| m.is_empty()));
        let own = o
            .get("name")
            .is_none_or(|n| n.as_str() == Some(format!("neko-{name}_{key}").as_str()))
            && o.get("external").is_none_or(|e| *e == false)
            && o.keys()
                .all(|k| ["name", "ipam", "external", "labels"].contains(&k.as_str()));
        let shared_network = shared
            && o.get("external") == Some(&json!(true))
            && o.get("name") == Some(&json!("nekoserver-media"))
            && o.keys()
                .all(|k| ["name", "ipam", "external"].contains(&k.as_str()));
        if !ipam_empty || !(own || shared_network) {
            return refused();
        }
        known.push(key.as_str());
    }
    for v in model["services"]
        .as_object()
        .into_iter()
        .flatten()
        .map(|(_, v)| v)
    {
        if shared && v.get("container_name").is_some() {
            return refused(); // a name of its own on the shared network
        }
        let joined: Vec<&str> = match &v["networks"] {
            Value::Null => vec![],
            Value::Array(list) => list.iter().filter_map(Value::as_str).collect(),
            Value::Object(map) => {
                if map.values().any(|n| {
                    n.as_object().is_some_and(|o| !o.is_empty()) || !(n.is_null() || n.is_object())
                }) {
                    return refused(); // aliases, a fixed address, a MAC, driver options
                }
                map.keys().map(String::as_str).collect()
            }
            _ => return refused(),
        };
        if joined.iter().any(|n| *n != "default" && !known.contains(n)) {
            return refused();
        }
    }
    Ok(())
}
/// What a snapshot and a restore of Immich rely on (recovery_tools.py validate_model),
/// checked here too so an update never installs a file the snapshot would then refuse:
/// the photos and the database bound where Immich looks for them, no other user, and
/// Immich's server talking to its own database and cache.
fn immich_ok(services: &serde_json::Map<String, Value>, data: &Path, photos: &Path) -> Result<()> {
    immich_binds_ok(services, photos, &data.join("appdata/immich/postgres"))?;
    immich_env_ok(services)
}
/// Where Immich's container `service` sees the photo library (older and newer image) or
/// the database (shared.json "immich_targets"; the acceptance check reads the same list).
fn immich_targets(service: &str) -> Vec<&'static str> {
    common::SHARED["immich_targets"][service]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .collect()
}
/// Immich's photos and database are bound where its snapshot looks: `photos` and `database`,
/// at the places its containers use them, and nothing else (but /etc/localtime).
fn immich_binds_ok(
    services: &serde_json::Map<String, Value>,
    photos: &Path,
    database: &Path,
) -> Result<()> {
    let wanted: [(&str, &Path, Vec<&str>); 2] = [
        ("immich-server", photos, immich_targets("immich-server")),
        ("database", database, immich_targets("database")),
    ];
    for (service, v) in services {
        if v.get("user").is_some() {
            return Err(Error::new(
                "Immich running as another user is not covered by its snapshot",
            ));
        }
        let binds: Vec<&Value> = v["volumes"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|m| m["type"] == "bind" && m["source"] != "/etc/localtime")
            .collect();
        let expected = wanted.iter().find(|(s, _, _)| s == service);
        let fits = |m: &&Value| {
            expected.is_some_and(|(_, source, targets)| {
                Path::new(m["source"].as_str().unwrap_or("")) == *source
                    && targets.contains(&m["target"].as_str().unwrap_or(""))
            })
        };
        if !binds.iter().all(fits) || (expected.is_some() && binds.is_empty()) {
            return Err(Error::new(
                "Immich's photos or database are not where its snapshot looks",
            ));
        }
    }
    Ok(())
}
/// Immich's server uses its own database and cache (the ones its snapshot holds).
fn immich_env_ok(services: &serde_json::Map<String, Value>) -> Result<()> {
    let env = |service: &str, key: &str| {
        services[service]["environment"][key]
            .as_str()
            .map(str::to_string)
    };
    let routed = env("immich-server", "DB_URL").is_none()
        && env("immich-server", "REDIS_URL").is_none()
        && env("immich-server", "DB_HOSTNAME").is_none_or(|h| h == "database")
        && env("immich-server", "REDIS_HOSTNAME").is_none_or(|h| h == "redis");
    let same = [
        ("DB_USERNAME", "POSTGRES_USER", Some("postgres")),
        ("DB_DATABASE_NAME", "POSTGRES_DB", Some("immich")),
        ("DB_PASSWORD", "POSTGRES_PASSWORD", None),
    ]
    .iter()
    .all(|(key, db_key, default)| {
        let or = |v: Option<String>| v.or(default.map(str::to_string));
        or(env("immich-server", key)) == or(env("database", db_key))
    });
    if !(routed && same) {
        return Err(Error::new(
            "Immich's server does not use its own database and cache",
        ));
    }
    Ok(())
}
/// The Immich Compose file a snapshot covers and a restore test rebuilds, with the photos at
/// `upload` and the database at `database` (where the snapshot found them): the update's own
/// keys and isolation (valid_model's), the four standard services, no device or other user
/// (a restore test runs without them), each folder bound once where Immich looks for it
/// (and /etc/localtime only read-only), caches only as named volumes of their own, and
/// Immich's server on its own database and cache. Kept here once: setup asks it (check-compose
/// --upload --database), and kept a copy of its own before, which already disagreed (the
/// review of 0.47.0: `privileged: false` refused there, taken here).
fn snapshot_ok(model: &Value, upload: &Path, database: &Path) -> Result<()> {
    let file_keys = common::shared_list(&["compose", "file_keys"]);
    if model
        .as_object()
        .is_none_or(|o| o.keys().any(|k| !file_keys.contains(&k.as_str())))
    {
        return Err(Error::new(
            "Custom Immich Compose settings (secrets, configs...) require manual isolated recovery",
        ));
    }
    let services = model["services"]
        .as_object()
        .ok_or_else(|| Error::new("Compose services missing"))?;
    if services.len() != 4 || !services.keys().all(|s| expected_service("immich", s)) {
        return Err(Error::new(
            "Restore currently supports the four standard Immich services; custom services require review",
        ));
    }
    let refused = || Error::new("Custom Immich runtime settings require manual isolated recovery");
    for (service, v) in services {
        if !v["image"].is_string() || !isolated("immich", service, v) {
            return Err(refused());
        }
        if v.get("devices").is_some() || v.get("user").is_some() {
            return Err(refused());
        }
        let mut targets = vec![];
        for volume in v["volumes"].as_array().into_iter().flatten() {
            let target = volume["target"].as_str().unwrap_or("");
            if target.is_empty() || targets.contains(&target) {
                return Err(Error::new("Invalid/duplicate Immich volume target"));
            }
            targets.push(target);
            let cache = volume["type"] == "volume"
                && ["immich-machine-learning", "redis"].contains(&service.as_str())
                && volume["source"].as_str().is_some_and(|n| {
                    !n.is_empty()
                        && n.bytes()
                            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
                });
            let clock = volume["type"] == "bind"
                && volume["source"] == "/etc/localtime"
                && volume["read_only"] == true;
            if !(cache || clock || volume["type"] == "bind" && volume["source"] != "/etc/localtime")
            {
                return Err(Error::new("Unsupported Immich persistent volume"));
            }
        }
    }
    immich_binds_ok(services, upload, database)?;
    immich_env_ok(services)
}
/// A service of the app's own that keeps to NekoServer's isolation.
fn isolated(name: &str, service: &str, v: &Value) -> bool {
    expected_service(name, service) && !widens_rights(v) && gpu_only(service, v)
}
/// The folders one app may bind: its own settings, and your files where setup put them.
struct Places<'a> {
    name: &'a str,
    data: &'a Path,
    photos: PathBuf,
    cloud: PathBuf,
    /// The music library and the downloaded music (common::saved_path "music", "downloads").
    music: PathBuf,
    downloads: PathBuf,
    /// The films, the shows and their downloads (common::movies_library).
    library: PathBuf,
}
impl Places<'_> {
    fn volume_ok(&self, service: &str, volume: &Value) -> Result<()> {
        if volume["type"] == "bind" {
            // A bind's own options: only these two (propagation could carry mounts out).
            let options_ok = volume["bind"].as_object().is_none_or(|o| {
                o.keys()
                    .all(|k| ["create_host_path", "selinux"].contains(&k.as_str()))
            });
            if !options_ok {
                return Err(Error::new("Uncovered container bind mount"));
            }
            let source = Path::new(string(volume, "source")?);
            let localtime = source == Path::new("/etc/localtime") && volume["read_only"] == true;
            let p = if localtime {
                source.to_path_buf()
            } else {
                common::safe(source)?
            };
            if !(localtime || self.bind_ok(&p)) {
                return Err(Error::new("Uncovered container bind mount"));
            }
        } else if volume["type"] != "volume"
            || !(service == "redis" || service == "immich-machine-learning")
        {
            return Err(Error::new("Uncovered persistent volume"));
        }
        Ok(())
    }
    fn bind_ok(&self, p: &Path) -> bool {
        let name = self.name;
        let own = self.data.join("appdata").join(name);
        let library = &self.library;
        p.starts_with(&own)
            || (name == "immich" && p == self.photos)
            || (name == "nextcloud" && p == self.cloud)
            || (["radarr", "sonarr", "qbittorrent"].contains(&name) && p == library.as_path())
            || (name == "jellyfin" && ["movies", "tv"].iter().any(|n| p == library.join(n)))
            || (name == "navidrome" && p == self.music)
            // Lidarr sees the folder both music folders are in (it moves downloads in).
            || (name == "lidarr" && self.music.parent().is_some_and(|top| p == top))
            || (name == "qbittorrent" && p == self.downloads)
    }
}
/// The app's own port on this PC only (qBittorrent's peer port excepted); its helpers
/// publish nothing.
fn published_ok(name: &str, service: &str, v: &Value) -> Result<()> {
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
pub(crate) const SECRETS_KEY_DIR: &str = "/etc/nekoserver-backups";
pub(crate) const SECRETS_KEY: &str = "secrets-key.p8";
pub(crate) const SECRETS_CERT: &str = "secrets-recipient.pem";
pub(crate) const SECRETS_ARCHIVE: &str = "secrets.tar.cms";
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
pub(crate) fn sealed_members(data: &Path) -> Vec<String> {
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
    let active = load(Path::new(*JOURNAL))
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
pub(crate) fn secrets_key_ready(key_dir: &Path) -> bool {
    key_dir.join(SECRETS_KEY).is_file() && key_dir.join(SECRETS_CERT).is_file()
}
fn encrypt_secrets(data: &Path, snapshot: &Path) -> Result<&'static str> {
    encrypt_secrets_with(data, snapshot, Path::new(SECRETS_KEY_DIR))
}
pub(crate) fn encrypt_secrets_with(
    data: &Path,
    snapshot: &Path,
    key_dir: &Path,
) -> Result<&'static str> {
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
/// The process ID (on this PC) of `service`'s container: its network is entered there.
fn container_pid(file: &Path, project: &str, service: &str) -> Result<String> {
    let id = compose(file, project, &["ps", "-q", service])?;
    let id = id
        .split_whitespace()
        .next()
        .ok_or_else(|| Error::new("Restore-test container missing"))?;
    let pid = docker(&["inspect", "--format", "{{.State.Pid}}", id])?;
    let pid = pid.trim();
    if pid.parse::<u32>().map_or(true, |p| p == 0) {
        return Err(Error::new("Restore-test container is not running"));
    }
    Ok(pid.to_string())
}
/// `curl_as`, run by this PC's own curl inside the network of the process `pid` (a
/// restore-test container): nothing outside that network is reachable or exposed.
fn curl_inside(pid: &str, url: &str, host: Option<&str>) -> Result<String> {
    let header = host.map(|h| format!("Host: {h}"));
    let mut args = vec![
        "nsenter",
        "--target",
        pid,
        "--net",
        "--",
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
    if let Some(h) = &header {
        args.extend(["--header", h.as_str()]);
    }
    args.push(url);
    checked(&args, 20)
}
fn app_port(name: &str) -> Result<(u16, &'static str)> {
    let path = match name {
        "jellyfin" => "/health",
        "navidrome" | "sonarr" | "radarr" | "lidarr" | "prowlarr" => "/ping",
        // Not /api/v1/status: that makes Seerr ask GitHub whether an update exists, and on
        // the restore test's network (no way out) the question hangs until the probe's
        // own time limit. Its public settings need nothing from outside.
        "seerr" => "/api/v1/settings/public",
        "qbittorrent" => "/",
        "immich" => "/api/server/ping",
        // Probed inside its container, where the web server listens on 80 (the
        // catalogue's port is the one published on the server).
        "nextcloud" => return Ok((80, "/status.php")),
        _ => return Err(Error::new("Unknown application probe")),
    };
    let port = common::app_port(name).ok_or_else(|| Error::new("Unknown application probe"))?;
    Ok((port, path))
}
/// Apps whose first start on a new release can update their database for a long time
/// (Jellyfin's library, Immich's and Nextcloud's databases, the Arr apps'): while one
/// keeps running and working past `health_wait_seconds`, the update waits for it, up to
/// MIGRATION_WAIT, instead of being rolled back half-way every month.
const MIGRATING: [&str; 7] = [
    "immich",
    "nextcloud",
    "jellyfin",
    "sonarr",
    "radarr",
    "lidarr",
    "prowlarr",
];
const MIGRATION_WAIT: u64 = 3600;
/// How a Nextcloud health check that got an answer, but could not write its test file,
/// begins: an exception inside Nextcloud (its files folder not its own any more, say),
/// never a migration still running, so the hour's wait for one would be for nothing.
const PROBE_FAILED: &str = "Nextcloud could not write a test file: ";
/// Seconds a failed test file gets before the health check gives up on it: time enough
/// for Nextcloud's own start (Redis, the first request), far short of the hour.
const PROBE_GRACE: u64 = 120;
/// Whether the health check stops waiting now: the test file failed again after PROBE_GRACE.
fn probe_gave_up(error: &str, elapsed: u64) -> bool {
    error.starts_with(PROBE_FAILED) && elapsed >= PROBE_GRACE
}
/// What the health check says when it gives up on the test file.
fn probe_advice(error: &str, elapsed: u64) -> String {
    format!(
        "{} after {elapsed} seconds, though Nextcloud answers ({}). Its files folder on the HDD may not be Nextcloud's own any more (a new HDD, say): run  sudo bash install.sh  so setup gives it back, or look in Nextcloud's log",
        PROBE_FAILED.trim_end_matches(": "),
        error.trim_start_matches(PROBE_FAILED)
    )
}
/// Whether the health check goes on waiting after `wait` seconds (`elapsed` now): for an
/// app that can migrate for long, not in a restore test, up to MIGRATION_WAIT, and only
/// while `working` says its container is still running and doing something.
fn keep_waiting(
    name: &str,
    elapsed: u64,
    wait: u64,
    rehearsal: bool,
    working: impl FnOnce() -> bool,
) -> bool {
    !rehearsal
        && MIGRATING.contains(&name)
        && elapsed >= wait
        && elapsed < MIGRATION_WAIT.max(wait)
        && working()
}
/// The app's main container is running, has not restarted since the health check began
/// (`seen`: its first start time and restart count), and shows work: something written
/// to its log, or processor use, in the last two minutes.
fn still_working(file: &Path, project: &str, service: &str, seen: &mut Option<Value>) -> bool {
    let Some(id) = compose(file, project, &["ps", "-q", service])
        .ok()
        .and_then(|ids| ids.split_whitespace().next().map(str::to_string))
    else {
        return false;
    };
    let Some(state) = docker(&["inspect", &id])
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .map(|v| v[0]["State"].clone())
    else {
        return false;
    };
    let run = json!([state["StartedAt"], state["RestartCount"]]);
    if state["Running"] != true
        || state["Restarting"] == true
        || *seen.get_or_insert(run.clone()) != run
    {
        return false;
    }
    let logged = docker(&["logs", "--since", "2m", "--tail", "5", &id])
        .is_ok_and(|out| !out.trim().is_empty());
    logged
        || docker(&["stats", "--no-stream", "--format", "{{.CPUPerc}}", &id]).is_ok_and(|out| {
            out.trim()
                .trim_end_matches('%')
                .parse::<f64>()
                .is_ok_and(|cpu| cpu >= 1.0)
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
    let (mut seen, mut said) = (None, false);
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
            let own = format!("127.0.0.1:{port}");
            let host = (name == "qbittorrent").then_some(own.as_str());
            let body = if rehearsal {
                // The restore test runs on an internal network with no published port:
                // asked from inside the app's own network (see restored_model).
                curl_inside(
                    &container_pid(file, project, service)?,
                    &format!("http://{own}{path}"),
                    host,
                )?
            } else {
                let binding = compose(file, project, &["port", service, &port.to_string()])?;
                let published = binding
                    .trim()
                    .strip_prefix("127.0.0.1:")
                    .filter(|s| s.parse::<u16>().is_ok())
                    .ok_or_else(|| Error::new("Private health port required"))?
                    .to_string();
                curl_as(&format!("http://127.0.0.1:{published}{path}"), None, host)?
            };
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
                )
                .map_err(|e| Error::new(&format!("{PROBE_FAILED}{e}")))?;
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
        let elapsed = start.elapsed().as_secs();
        if let Err(e) = &attempt {
            // Nextcloud answers but cannot write: not a migration, so no hour's wait.
            if probe_gave_up(&e.to_string(), elapsed) {
                return Err(Error::new(&probe_advice(&e.to_string(), elapsed)));
            }
        }
        if elapsed >= wait {
            if keep_waiting(name, elapsed, wait, rehearsal, || {
                still_working(file, project, service, &mut seen)
            }) {
                if !said {
                    println!("Monthly update: {name} is still starting after {} minutes and keeps working (a new release may be updating its database); waiting for it up to {} minutes in all", wait / 60, MIGRATION_WAIT.max(wait) / 60);
                    said = true;
                }
                thread::sleep(Duration::from_secs(5));
                continue;
            }
            return attempt.map_err(|e| {
                Error::new(&format!(
                    "{name} was not ready after {} minutes ({e}). If it was still updating its database, raise \"health_wait_seconds\" under \"maintenance\" (up to 1800) and run  sudo bash install.sh  again; the next monthly update then waits longer",
                    elapsed / 60
                ))
            });
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
    // An internal network: the restored apps (with production's settings, so their
    // webhooks and links to other apps) reach neither the home network nor the internet.
    // An internal network alone still lets them reach this PC itself (every service
    // listening on all its addresses, Samba or SSH, say) through the bridge's own
    // address: the bridge gets no address of its own (inhibit_ipv4, known to Docker
    // since 19.03), so there is nothing on this PC to reach either. No port is published; the
    // health check asks the app from inside its own network (curl_inside), which needs
    // no address on the bridge.
    model["networks"] = json!({"restore":{"internal":true,"driver_opts":{"com.docker.network.bridge.inhibit_ipv4":"true"}}});
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
        map.remove("ports");
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
        let mut journal = load(Path::new(*JOURNAL))?;
        journal["rehearsals"]
            .as_array_mut()
            .unwrap()
            .push(json!({"file":file,"project":project}));
        common::atomic_json(Path::new(*JOURNAL), &journal)?;
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
/// What a restore test cut off part-way left behind (a power cut, a restart): its test
/// containers and its unpacked copy of the app data under the snapshot, which would
/// otherwise take a second copy's space on the HDD for months. Best effort: a test
/// container Docker can't remove now never keeps the real apps from being started;
/// what is left is said, and the next run tries again.
fn tidy_restore_test(journal: &Value, snapshot: &Path) {
    for r in journal["rehearsals"]
        .as_array()
        .cloned()
        .unwrap_or_default()
    {
        let (Some(file), Some(project)) = (r["file"].as_str(), r["project"].as_str()) else {
            continue;
        };
        if let Err(e) = compose(
            Path::new(file),
            project,
            &["down", "--volumes", "--remove-orphans"],
        ) {
            println!("Monthly update: the restore test's containers ({project}) could not be removed now ({e}); the apps are started anyway");
        }
    }
    let stage = snapshot.join("restore-test");
    if stage.exists() {
        if let Err(e) =
            common::safe(&stage).and_then(|s| fs::remove_dir_all(s).map_err(Error::from))
        {
            println!(
                "Monthly update: the restore test's unpacked copy at {} could not be removed ({e})",
                stage.display()
            );
        }
    }
}
/// The old apps' images (by ID) put back from the snapshot's images.tar when Docker no
/// longer has one (`docker image prune -a`, a reset Docker store): the old apps start
/// with `--pull never`, and without its image a rolled-back app would stay stopped.
fn restore_images(
    models: &Value,
    snapshot: &Path,
    present: &dyn Fn(&str) -> bool,
    load: &dyn Fn(&str) -> Result<()>,
) -> Result<()> {
    let wanted: Vec<String> = models
        .as_object()
        .into_iter()
        .flat_map(|m| m.values())
        .flat_map(|m| {
            m["services"]
                .as_object()
                .into_iter()
                .flat_map(|s| s.values())
        })
        .filter_map(|v| v["image"].as_str().map(str::to_string))
        .collect();
    let missing: Vec<&String> = wanted.iter().filter(|id| !present(id)).collect();
    if missing.is_empty() {
        return Ok(());
    }
    let archive = snapshot.join("images.tar");
    if !archive.is_file() {
        return Err(Error::new(&format!(
            "The apps' earlier images are gone from Docker ({}) and this snapshot has no saved copy (images.tar); they can't be started again by themselves",
            missing.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(", ")
        )));
    }
    println!("Monthly update: {} earlier app image(s) missing from Docker; loading them from the snapshot", missing.len());
    load(&archive.to_string_lossy())?;
    if let Some(still) = wanted.iter().find(|id| !present(id)) {
        return Err(Error::new(&format!(
            "The snapshot's images.tar does not hold the earlier image {still}"
        )));
    }
    Ok(())
}
fn save_journal(value: &Value) -> Result<()> {
    common::atomic_json(Path::new(*JOURNAL), value)
}
/// A journal this program can finish: the same journal format, for the same drives and
/// folders. Any release may finish it, not only the one that started it, so a fix to
/// the way back can reach a server whose earlier way back failed.
/// Not one written by a newer release (its phases may mean something this one does not
/// know, after a downgrade), and only phases this release knows how to finish.
fn resumable(journal: &Value, cfg: &Value) -> bool {
    journal["format"] == "neko-maintenance-resume-v1"
        && journal["storage"] == cfg["storage"]
        && journal["data_mount"] == cfg["data_mount"]
        && journal["media_mount"] == cfg["media_mount"]
        && journal["version"]
            .as_str()
            .and_then(release)
            .is_some_and(|v| Some(v) <= release(VERSION))
        && journal["phase"].as_str().is_some_and(|p| {
            [
                "snapshot",
                "backed_up",
                "updating",
                "restoring",
                "committed",
            ]
            .contains(&p)
        })
}
/// A committed run finished by resume: its record that the update went in (result.json,
/// when it was cut off before writing it), and the version noted installed, so the
/// update night does not install it again. Best effort: the update stands either way.
fn finish_committed(journal: &Value, snapshot: &Path, data: &Path) {
    let result = snapshot.join("result.json");
    if !result.exists() {
        let audit = journal["kind"] == "audit";
        let _ = common::atomic_json(
            &result,
            &json!({"status":"pass","updated_at":common::now(),"restore_test":if audit {"pass"} else {"not_run"},"snapshot":"hdd","finished_by":"resume"}),
        );
    }
    if let (Some(app), Some(sig)) = (
        journal["candidate"]["app"].as_str(),
        journal["candidate"]["sig"].as_str(),
    ) {
        if let Err(e) = crate::updates::installed(data, app, sig) {
            println!("App update: {app} is installed, but it could not be noted ({e}); the next check notes it");
        }
    }
}
/// "0.39.5" as (0, 39, 5), to compare releases.
fn release(text: &str) -> Option<(u64, u64, u64)> {
    let mut parts = text.split('.').map(|p| p.parse::<u64>().ok());
    let found = (parts.next()??, parts.next()??, parts.next()??);
    parts.next().is_none().then_some(found)
}
fn resume(cfgpath: &Path, cfg: &Value) -> Result<()> {
    if !Path::new(*JOURNAL).exists() {
        return Ok(());
    }
    private(Path::new(*JOURNAL))?;
    let mut journal = load(Path::new(*JOURNAL))?;
    if !resumable(&journal, cfg) {
        return Err(Error::new("Maintenance recovery identity mismatch"));
    }
    mounts(cfgpath, cfg, false)?;
    let snapshot = PathBuf::from(string(&journal, "snapshot")?);
    tidy_restore_test(&journal, &snapshot);
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
        finish_committed(&journal, &snapshot, Path::new(string(cfg, "data_mount")?));
        fs::remove_file(*JOURNAL)?;
        fs::File::open(Path::new(*JOURNAL).parent().unwrap())?.sync_all()?;
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
        let apps: Vec<String> = journal["models"]
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect();
        let allowed = rollback_paths(&apps, &snapshot_manifest);
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
                let run = string(&journal, "run")?.to_string();
                let stage = if kind == "data" {
                    root.join("backups").join(format!("rollback-{run}"))
                } else {
                    snapshot.join("rollback-stage/media")
                };
                let base = if kind == "data" {
                    placed_base(cfg, &relative)?
                } else {
                    None
                };
                put_back(root, &stage, &relative, &run, base.as_deref(), &remount)?;
                journal["restored"].as_array_mut().unwrap().push(json!(key));
                save_journal(&journal)?;
            }
        }
        for (name, model) in journal["models"].as_object().unwrap() {
            write_override(cfg, name, model)?;
        }
    }
    if phase == "snapshot" {
        // Cut off while the apps were being stopped: a stop Docker is still finishing
        // would end after the start below and leave that app stopped (Docker does not
        // restart an app it was asked to stop). Each stop finishes first.
        for (name, _) in journal["models"].as_object().unwrap() {
            let file = snapshot.join(format!("{name}-old.json"));
            compose(&file, &format!("neko-{name}"), &["stop", "--timeout", "60"])?;
        }
    }
    restore_images(
        &journal["models"],
        &snapshot,
        &|id| docker(&["image", "inspect", id]).is_ok(),
        &|archive| docker(&["load", "--input", archive]).map(|_| ()),
    )?;
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
        let rollback = format!("rollback-{}", string(&journal, "run")?);
        let stage = Path::new(string(cfg, "data_mount")?)
            .join("backups")
            .join(&rollback);
        let mut spent = vec![snapshot.join("rollback-stage"), stage];
        // ... and the stage of an app installed on another drive, on that drive.
        for (name, _) in journal["models"].as_object().unwrap() {
            if let Some(base) = common::installed_base(cfg, name)? {
                spent.push(base.join(&rollback));
            }
        }
        for spent in spent {
            if spent.exists() {
                fs::remove_dir_all(common::safe(&spent)?)?;
            }
        }
    }
    fs::remove_file(*JOURNAL)?;
    fs::File::open(Path::new(*JOURNAL).parent().unwrap())?.sync_all()?;
    scrub_all(Path::new(string(cfg, "media_mount")?));
    clear_work(Path::new(string(cfg, "data_mount")?));
    Ok(())
}
/// For the settings folder of an app installed on another drive (0.50.0): the folder
/// on that drive that holds it (<drive>/nekoserver). Its usual place is a mount of
/// <drive>/nekoserver/appdata/<app> and cannot be renamed; on the drive itself it can.
/// None for every other path: those are put back on the app drive as always.
fn placed_base(cfg: &Value, relative: &str) -> Result<Option<PathBuf>> {
    match relative.strip_prefix("appdata/") {
        Some(app) if !app.contains('/') => common::installed_base(cfg, app),
        _ => Ok(None),
    }
}
/// The snapshot's copy of such an app's settings is unpacked on the app drive with the
/// rest; a rename cannot cross drives, so it is copied to the app's own drive first
/// (`on_drive`), whole or not at all: under another name until the copy is complete.
fn stage_on_drive(unpacked: &Path, on_drive: &Path) -> Result<()> {
    if on_drive.exists() || !unpacked.exists() {
        return Ok(()); // copied already (a rollback cut off and run again), or put back
    }
    let parent = on_drive
        .parent()
        .ok_or_else(|| Error::new("Invalid path"))?;
    common::directory(parent, 0o700)?;
    let partial = on_drive.with_file_name(format!(
        "{}.partial",
        on_drive.file_name().unwrap().to_string_lossy()
    ));
    if partial.exists() {
        fs::remove_dir_all(common::safe(&partial)?)?;
    }
    checked(
        &[
            "cp",
            "--archive",
            "--",
            &unpacked.to_string_lossy(),
            &partial.to_string_lossy(),
        ],
        21600,
    )?;
    fs::rename(&partial, on_drive)?;
    fs::File::open(parent)?.sync_all()?;
    fs::remove_dir_all(common::safe(unpacked)?)?;
    Ok(())
}
/// The app's usual place shows the folder now at `home` on its drive: after a rollback
/// swapped that folder, the mount still shows the one set aside until it is made again.
fn remount(place: &Path, home: &Path) -> Result<()> {
    if common::same_folder(place, home) {
        return Ok(());
    }
    let at = place.to_string_lossy();
    if common::command(&["mountpoint", "-q", &at], 30)?.0 == 0 {
        checked(&["umount", &at], 300)?;
    }
    checked(&["mount", &at], 300)?;
    if !common::same_folder(place, home) {
        return Err(Error::new(
            "The app's settings folder is not the one on its drive after the rollback",
        ));
    }
    Ok(())
}
/// One path of a rollback put back: the failed update's copy is set aside beside it
/// (`<name>.neko-failed-<run>`) and the snapshot's copy takes its place, each by a
/// rename, so a rollback cut off half-way finds one or the other and goes on.
/// `base`: for an app installed on another drive, the folder there that holds its
/// settings (placed_base); the two renames are then made on that drive, and `remount`
/// shows the folder put back at the app's usual place.
fn put_back(
    root: &Path,
    stage: &Path,
    relative: &str,
    run: &str,
    base: Option<&Path>,
    remount: &dyn Fn(&Path, &Path) -> Result<()>,
) -> Result<()> {
    let unpacked = common::safe(&stage.join(relative))?;
    let (live, source) = match base {
        Some(base) => {
            let on_drive = base.join(format!("rollback-{run}")).join(relative);
            stage_on_drive(&unpacked, &on_drive)?;
            (
                common::safe(&base.join(relative))?,
                common::safe(&on_drive)?,
            )
        }
        None => (common::safe(&root.join(relative))?, unpacked),
    };
    let quarantine = live.with_file_name(format!(
        "{}.neko-failed-{run}",
        live.file_name().unwrap().to_string_lossy()
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
    if base.is_some() {
        remount(&root.join(relative), &live)?;
    }
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
/// Whether the unfinished run's journal (`active`) needs the snapshot folder `run`.
fn journal_names(active: Option<&Value>, run: &str) -> bool {
    active.is_some_and(|j| {
        j["snapshot"]
            .as_str()
            .is_some_and(|s| Path::new(s).file_name().is_some_and(|f| f == run))
    })
}
/// left resolved passwords in them). Best effort: never fails a finished update.
fn scrub_all(media: &Path) {
    for folder in [MONTHLY_FOLDER, UPDATE_FOLDER] {
        scrub_folder(&media.join(folder));
    }
}
fn scrub_folder(hdd: &Path) {
    let active = load(Path::new(*JOURNAL)).ok();
    for run in runs_in(hdd).unwrap_or_default() {
        let in_use = journal_names(active.as_ref(), &run);
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
        remove_unarchived(Path::new(media));
    }
    if let Ok(data) = string(cfg, "data_mount") {
        clear_work(Path::new(data));
    }
}
/// What a snapshot folder holds once anything was archived into it.
const ARCHIVED: [&str; 6] = [
    "data.tar",
    "media.tar",
    "images.tar",
    "manifest.json",
    "restore-tests.json",
    "result.json",
];
/// Monthly run folders that never got as far as archiving anything (the night ran out
/// before the apps were stopped, a download failed): only the apps' saved settings are
/// in them. They are removed, so they don't pile up month after month and aren't listed
/// as unfinished snapshots taking space. A folder a journal still needs, or one the
/// worker did not make (not private), stays.
fn remove_unarchived(media: &Path) {
    for folder in [MONTHLY_FOLDER, UPDATE_FOLDER] {
        remove_unarchived_in(&media.join(folder));
    }
}
fn remove_unarchived_in(hdd: &Path) {
    let active = load(Path::new(*JOURNAL)).ok();
    for run in runs_in(hdd).unwrap_or_default() {
        let in_use = journal_names(active.as_ref(), &run);
        let folder = hdd.join(&run);
        if in_use
            || ARCHIVED
                .iter()
                .any(|name| fs::symlink_metadata(folder.join(name)).is_ok())
        {
            continue;
        }
        if let Err(e) = remove_run(&folder) {
            println!("Monthly snapshot {run}: an empty run folder was kept ({e})");
        }
    }
}
fn write_override(cfg: &Value, name: &str, model: &Value) -> Result<()> {
    write_pins(Path::new(string(cfg, "data_mount")?), name, model)
}
/// The image IDs `model` runs, saved where setup and every `docker compose up` read them
/// (compose/<app>/maintenance-images.json), so they start exactly those images.
fn write_pins(data: &Path, name: &str, model: &Value) -> Result<()> {
    let services = model["services"]
        .as_object()
        .ok_or_else(|| Error::new("Invalid model"))?
        .iter()
        .map(|(k, v)| (k.clone(), json!({"image":v["image"]})))
        .collect::<serde_json::Map<_, _>>();
    common::atomic_json(
        &data
            .join("compose")
            .join(name)
            .join("maintenance-images.json"),
        &json!({"services":services}),
    )
}
/// Before the first download: each app not pinned yet (no update has gone in for it) is
/// pinned to the images it runs now, by image ID. A download moves the app's tag
/// (jellyfin:latest) to the new image; without the pin, or with a pin by tag, the next
/// `sudo bash install.sh` would start that image, untested and with no snapshot to go
/// back to. A pin by tag (0.42.0 to 0.45.6 wrote those from the daily check) is made
/// again from what the app's containers run. Returns the apps pinned now.
fn pin_unpinned(data: &Path, names: &[String], models: &Value) -> Result<Vec<String>> {
    pin_unpinned_with(data, names, models, &|name, model| {
        containers(name, model).map(drop)
    })
}
fn pin_unpinned_with(
    data: &Path,
    names: &[String],
    models: &Value,
    running: &dyn Fn(&str, &mut Value) -> Result<()>,
) -> Result<Vec<String>> {
    let mut pinned = vec![];
    for name in names {
        if pinned_by_id(data, name) {
            continue;
        }
        // What the containers run now (the image IDs), never the tags the settings name.
        let mut model = models[name.as_str()].clone();
        match running(name, &mut model) {
            Ok(()) if model_images_all_ids(&model) => {
                write_pins(data, name, &model)?;
                pinned.push(name.clone());
            }
            Ok(()) | Err(_) => println!(
                "{name}: not running as set up, so its images were not pinned yet; its new version is not looked for until it is"
            ),
        }
    }
    Ok(pinned)
}
/// The app's pins (maintenance-images.json) name each image by ID.
fn pinned_by_id(data: &Path, name: &str) -> bool {
    let pins = data
        .join("compose")
        .join(name)
        .join("maintenance-images.json");
    fs::symlink_metadata(&pins).is_ok() && model_images_all_ids(&common::json(&pins, 64_000))
}
/// Every service of the model names its image by ID ("sha256:…").
fn model_images_all_ids(model: &Value) -> bool {
    model["services"].as_object().is_some_and(|services| {
        !services.is_empty()
            && services
                .values()
                .all(|v| image_id(v["image"].as_str().unwrap_or("")))
    })
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
    if hardware.exists() && graphics_present() {
        private(&hardware)?;
        args.extend(["-f", &hardware_name]);
    }
    args.extend(["config", "--format", "json"]);
    Ok(serde_json::from_str(&docker(&args)?)?)
}
fn release_fetch(url: &str) -> Result<String> {
    retry_transient(PULL_WAITS, sleep_seconds, || release_fetch_once(url))
}
fn release_fetch_once(url: &str) -> Result<String> {
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

/// Whether the Immich release `tag` may be installed over the one the .env text names:
/// the same major always; another major only while `immich_major_update` approves it.
fn immich_release_allowed(cfg: &Value, env: &str, tag: &str) -> bool {
    let major = |t: &str| {
        t.strip_prefix('v')
            .and_then(|r| r.split('.').next())
            .filter(|m| !m.is_empty() && m.bytes().all(|b| b.is_ascii_digit()))
            .map(str::to_string)
    };
    let installed = env
        .lines()
        .find_map(|line| line.strip_prefix("IMMICH_VERSION="))
        .and_then(major);
    let Some(next) = major(tag) else {
        return false;
    };
    if installed.as_deref() == Some(next.as_str()) {
        return true;
    }
    let wanted = cfg["maintenance"]["immich_major_update"]
        .as_str()
        .unwrap_or("")
        .trim();
    immich_major_approved(wanted, tag, &next)
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
/// The major of a Nextcloud image tag setup accepts: `nextcloud:33-apache`,
/// `nextcloud:33.0-apache` or `nextcloud:33.0.10-apache` (nextcloud.py checks the same).
fn nextcloud_tag_major(image: &str) -> Option<u64> {
    regex::Regex::new(r"^nextcloud:(\d+)(?:\.\d+){0,2}-apache$")
        .unwrap()
        .captures(image)?[1]
        .parse()
        .ok()
}
/// Every saved Nextcloud tag of major `from`, in any accepted form, moved to the
/// approved major's own stream (`nextcloud:{to}-apache`).
fn retag_nextcloud(text: &str, from: u64, to: u64) -> String {
    regex::Regex::new(&format!(r#""nextcloud:{from}(?:\.\d+){{0,2}}-apache""#))
        .unwrap()
        .replace_all(text, format!("\"nextcloud:{to}-apache\"").as_str())
        .into_owned()
}
/// The candidate's Nextcloud and its cron worker moved to the approved major; an error
/// when either would stay on another one (the upgrade would silently not happen).
fn step_nextcloud_images(candidate: &mut Value, from: u64, to: u64) -> Result<()> {
    for service in ["nextcloud", "cron"] {
        let image = &mut candidate["services"][service]["image"];
        if image.as_str().and_then(nextcloud_tag_major) == Some(from) {
            *image = json!(format!("nextcloud:{to}-apache"));
        }
        if image.as_str().and_then(nextcloud_tag_major) != Some(to) {
            return Err(Error::new(&format!(
                "Nextcloud: the approved major {to} could not be selected for its {service} \
                 service (image {}); nothing was changed",
                image.as_str().unwrap_or("missing")
            )));
        }
    }
    Ok(())
}
/// Nextcloud's own `occ status` reports the approved major.
fn reached_major(status: &str, to: u64) -> bool {
    serde_json::from_str::<Value>(status.trim())
        .ok()
        .is_some_and(|v| {
            v["versionstring"]
                .as_str()
                .and_then(|s| s.split('.').next())
                .and_then(|s| s.parse::<u64>().ok())
                == Some(to)
        })
}
/// After the approved major started: Nextcloud really runs it, or the update fails
/// (and is rolled back) instead of reporting a success it did not have.
fn nextcloud_reached(snapshot: &Path, to: u64) -> Result<()> {
    let said = compose(
        &candidate_file(snapshot, "nextcloud", false),
        "neko-nextcloud",
        &[
            "exec",
            "-T",
            "-u",
            "www-data",
            "nextcloud",
            "php",
            "occ",
            "status",
            "--output=json",
        ],
    )?;
    if reached_major(&said, to) {
        Ok(())
    } else {
        Err(Error::new(&format!(
            "Nextcloud did not reach the approved major {to}; the update is rolled back"
        )))
    }
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
    // Its own stage: a failure here was said "while checking free space", the stage before.
    common::stage("checking the connections between the media apps");
    if common::media_services(cfg).is_empty() {
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
    runs_in(&root.join(MONTHLY_FOLDER))
}
/// The weekly app updates' snapshot folders on the HDD, oldest first.
pub(crate) fn update_runs(media: &Path) -> Result<Vec<String>> {
    runs_in(&media.join(UPDATE_FOLDER))
}
/// The run folders in `folder` (`<time>-<pid>`, never symlinks), oldest first.
fn runs_in(folder: &Path) -> Result<Vec<String>> {
    let pattern = regex::Regex::new(r"^[0-9]+-[0-9]+$").unwrap();
    let mut runs = vec![];
    if !folder.exists() {
        return Ok(runs);
    }
    for entry in fs::read_dir(common::safe(folder)?)? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().to_string();
        if entry.file_type()?.is_dir() && pattern.is_match(&name) {
            runs.push(name);
        }
    }
    // Run names start with a microsecond timestamp, so numeric order is age order (a run
    // dated far ahead, made while the clock was wrong, counts as the oldest).
    runs.sort_by_key(|n| {
        let (a, b) = n.split_once('-').unwrap();
        common::age_key(
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
    let mut folders = vec![
        data.to_path_buf(),
        data.join("appdata"),
        data.join("compose"),
        data.join("secrets"),
    ];
    // An app installed on another drive is set aside there, beside its folder.
    for drive in fs::read_dir(common::EXTRA_MOUNTS)
        .into_iter()
        .flatten()
        .flatten()
    {
        folders.push(drive.path().join("nekoserver/appdata"));
    }
    for folder in folders {
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
/// A run is complete once its restore test passed; an incomplete one is removed once
/// `keep` complete runs newer than it are kept, and reported until then. Folders that
/// are not the worker's own are never deleted. Copies that v0.21.0 wrote to the SSD are removed
/// once the same run (identical manifest) is on the HDD. The USB drive is not touched
/// here: the weekly USB job retires the old monthly copies there.
fn prune_monthly(data: &Path, media: &Path, keep: u64, current: Option<&str>) -> Result<Value> {
    let journal = load(Path::new(*JOURNAL)).ok();
    let protected = |run: &str| Some(run) == current || journal_names(journal.as_ref(), run);
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
        // An unfinished run (cut off, or its restore test failed) is worth less than a
        // complete one: it goes once `keep` complete runs newer than it are kept, as a
        // complete run of its age would. Each one is nearly a full snapshot in size.
        if complete.len() >= keep as usize {
            let all = monthly_runs(media)?;
            let oldest_kept = all.iter().position(|r| r == &complete[cut]).unwrap_or(0);
            for run in &all[..oldest_kept] {
                if complete.contains(run) || protected(run) {
                    continue;
                }
                // A folder that is not the worker's own (not private) stays, reported.
                if let Err(e) = remove_run(&hdd.join(run)) {
                    println!("Monthly retention: unfinished snapshot {run} kept ({e})");
                    continue;
                }
                removed.push(json!({"run":run,"drive":"hdd","unfinished":true}));
                for copy in failed_copies(data, run)? {
                    fs::remove_dir_all(common::safe(&copy)?)?;
                    removed.push(json!({"run":run,"drive":"ssd","failed_update":copy}));
                }
            }
        }
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
/// For the Updates page: when the newest snapshot holding `app` was made (its own update
/// snapshot, or a monthly one: every app is in those), and how its last update ended
/// ("installed", or "undone" for one rolled back or cut off). Times and words only.
pub(crate) fn app_history(media: &Path, app: &str) -> Value {
    let made =
        |run: &Path| common::json(&run.join("manifest.json"), 2_000_000)["created_at"].as_f64();
    let folder = media.join(UPDATE_FOLDER);
    let own: Vec<PathBuf> = update_runs(media)
        .unwrap_or_default()
        .into_iter()
        .map(|r| folder.join(r))
        .filter(|r| run_of(r, app))
        .collect();
    let monthly = media.join(MONTHLY_FOLDER);
    let snapshot = own
        .iter()
        .chain(
            monthly_runs(media)
                .unwrap_or_default()
                .iter()
                .map(|r| monthly.join(r))
                .collect::<Vec<_>>()
                .iter(),
        )
        .filter_map(|r| made(r))
        .fold(None, |best: Option<f64>, t| {
            Some(best.map_or(t, |b| b.max(t)))
        });
    let last = own.iter().rev().find_map(|r| {
        let at = made(r)?;
        let how = if r.join("result.json").is_file() {
            "installed"
        } else {
            "undone"
        };
        Some(json!({"at": at, "result": how}))
    });
    json!({"last_snapshot": snapshot, "last_update": last})
}
/// How many snapshots of each app's own updates are kept (the newest complete ones).
const UPDATE_SNAPSHOTS: usize = 2;
/// Whether the update snapshot `run` is of `app`: its manifest says so, or (before it
/// was archived) it holds the app's saved settings.
fn run_of(run: &Path, app: &str) -> bool {
    let manifest = common::json(&run.join("manifest.json"), 2_000_000);
    manifest["apps"]
        .as_array()
        .is_some_and(|a| a.iter().any(|n| n == app))
        || run.join(format!("{app}-old.json")).is_file()
}
/// Keep the newest UPDATE_SNAPSHOTS complete update snapshots of `app` (an update that
/// was installed); older ones go, and so does an unfinished one older than those (its
/// update was undone, or cut off and finished). Of the unfinished ones newer than those,
/// only the newest stays (to look into): updates that keep failing would otherwise pile
/// up a snapshot on the HDD and the failed app data on the SSD each week. Old ones go
/// only while the complete ones kept in their place pass their checksum check, as for
/// the monthly snapshots. With a run, its rollback's copies on the SSD
/// (`<app>.neko-failed-<run>`) go too. Never the one `current` or a journal names, nor a
/// folder that is not the worker's own.
fn prune_updates(data: &Path, media: &Path, app: &str, current: Option<&str>) -> Result<Value> {
    let journal = load(Path::new(*JOURNAL)).ok();
    let protected = |run: &str| Some(run) == current || journal_names(journal.as_ref(), run);
    let folder = media.join(UPDATE_FOLDER);
    let mine: Vec<String> = update_runs(media)?
        .into_iter()
        .filter(|r| run_of(&folder.join(r), app))
        .collect();
    let complete: Vec<&String> = mine
        .iter()
        .filter(|r| folder.join(r).join("result.json").is_file())
        .collect();
    let mut removed = vec![];
    let cut = complete.len().saturating_sub(UPDATE_SNAPSHOTS);
    let damaged: Vec<&String> = if cut > 0 {
        complete[cut..]
            .iter()
            .filter(|r| verify(&folder.join(r)).is_err())
            .copied()
            .collect()
    } else {
        vec![]
    };
    // The runs older than the oldest one kept, when those kept are intact.
    let older = match complete.get(cut) {
        Some(oldest_kept) if cut > 0 && damaged.is_empty() => {
            mine.iter().position(|r| &r == oldest_kept).unwrap_or(0)
        }
        _ => 0,
    };
    let unfinished_newer: Vec<&String> = mine[older..]
        .iter()
        .filter(|r| !complete.contains(r))
        .collect();
    let mut going: Vec<&String> = mine[..older].iter().collect();
    if let Some((_, older_unfinished)) = unfinished_newer.split_last() {
        going.extend(older_unfinished);
    }
    for run in going {
        if protected(run) {
            continue;
        }
        if let Err(e) = remove_update_run(data, &folder, run, &mut removed) {
            println!("App update snapshots: {run} kept ({e})");
        }
    }
    let mut report = json!({"app": app, "keep": UPDATE_SNAPSHOTS, "removed": removed});
    if !damaged.is_empty() {
        let names: Vec<&str> = damaged.iter().map(|r| r.as_str()).collect();
        println!(
            "App update snapshots of {app}: older ones kept, {} failed its checksum check",
            names.join(", ")
        );
        report["held_back"] = json!(names);
    }
    Ok(report)
}
/// One update snapshot off the HDD, and its rollback's copies off the SSD.
fn remove_update_run(
    data: &Path,
    folder: &Path,
    run: &str,
    removed: &mut Vec<Value>,
) -> Result<()> {
    remove_run(&folder.join(run))?;
    removed.push(json!(run));
    for copy in failed_copies(data, run)? {
        fs::remove_dir_all(common::safe(&copy)?)?;
        removed.push(json!({"run": run, "drive": "ssd", "failed_update": copy}));
    }
    Ok(())
}
/// An update that does not fit: the unfinished update snapshots of `app` (its updates
/// that were undone) are removed, oldest first, until it fits, with their copies on the
/// SSD; a complete one is never removed for room. Without this a failed update's
/// snapshot could take the room the next update needs, and nothing would ever free it.
fn make_update_room(data: &Path, media: &Path, app: &str, retention: &mut Value) -> Result<()> {
    let journal = load(Path::new(*JOURNAL)).ok();
    let folder = media.join(UPDATE_FOLDER);
    let mut removed = vec![];
    for run in update_runs(media)? {
        let path = folder.join(&run);
        if !run_of(&path, app)
            || path.join("result.json").is_file()
            || journal_names(journal.as_ref(), &run)
        {
            continue;
        }
        match remove_update_run(data, &folder, &run, &mut removed) {
            Ok(()) => println!(
                "App update snapshots: unfinished snapshot {run} of {app} removed to make room"
            ),
            Err(e) => println!("App update snapshots: {run} kept ({e})"),
        }
        break;
    }
    if let Some(list) = retention["removed"].as_array_mut() {
        list.extend(removed);
    }
    Ok(())
}
/// A full HDD: the unfinished monthly snapshots (cut off, or their restore test failed)
/// that retention still keeps are removed, oldest first, until the run fits (`wanted`
/// bytes free); a complete snapshot is never removed for room. They would otherwise go
/// only once newer complete runs replace them, which the full HDD prevents: a server cut
/// off a few times could never run its monthly job again. Returns the free bytes after.
fn make_room(media: &Path, retention: &mut Value, wanted: u64) -> Result<u64> {
    let folder = media.join("backups/monthly");
    let runs: Vec<String> = retention["incomplete_not_removed"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|r| r.as_str().map(str::to_string))
        .collect();
    let mut have = drive_space(media)?.0;
    let mut left = vec![];
    for run in runs {
        if have >= wanted {
            left.push(run);
            continue;
        }
        match remove_run(&folder.join(&run)) {
            Ok(()) => {
                println!(
                    "Monthly retention: unfinished snapshot {run} removed to make room on the HDD"
                );
                if let Some(removed) = retention["removed"].as_array_mut() {
                    removed
                        .push(json!({"run":run,"drive":"hdd","unfinished":true,"for_room":true}));
                }
                have = drive_space(media)?.0;
            }
            Err(e) => {
                println!("Monthly retention: unfinished snapshot {run} kept ({e})");
                left.push(run);
            }
        }
    }
    retention["incomplete_not_removed"] = json!(left);
    Ok(have)
}
/// For a full HDD: the unfinished snapshots that could not be removed to make room (not
/// the worker's own folders, or in use), named as the room a person can free.
fn unfinished_note(folder: &Path, runs: &[String], bytes: u64) -> String {
    if runs.is_empty() {
        return String::new();
    }
    format!(
        ". Unfinished monthly snapshots (cut off, or their restore test failed) take about {} GB there: {} in {}. They could not be removed to make room, so delete the ones you don't need",
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
/// What the monthly checksum check covers: (the HDD photo archive folder, when Immich
/// is set up; the USB copy's options, when its drive is named and something is copied).
fn copies_to_check(cfg: &Value, cfgpath: &Path) -> (Option<String>, Option<Args>) {
    let photos = &cfg["photos"];
    let immich = common::immich_wanted(cfg);
    let media = Path::new(cfg["media_mount"].as_str().unwrap_or(""));
    let archive_dir = photos["archive_dir"]
        .as_str()
        .unwrap_or("photos/archive")
        .to_string();
    let uuid = photos["external_backup_uuid"].as_str().unwrap_or("").trim();
    let cloud = cfg["nextcloud"]["enabled"] == true;
    let usb = (!uuid.is_empty() && (immich || cloud)).then(|| {
        let mut options = std::collections::BTreeMap::new();
        for (key, value) in [
            ("uuid", uuid.to_string()),
            (
                "fstype",
                photos["external_backup_fstype"]
                    .as_str()
                    .unwrap_or("ext4")
                    .to_string(),
            ),
            (
                "dest",
                cfg["external_backup_mount"]
                    .as_str()
                    .unwrap_or("")
                    .to_string(),
            ),
            ("media-root", media.to_string_lossy().into_owned()),
            ("data", cfg["data_mount"].as_str().unwrap_or("").to_string()),
            ("config", cfgpath.to_string_lossy().into_owned()),
        ] {
            options.insert(key.to_string(), value);
        }
        if immich {
            options.insert(
                "source".into(),
                media.join(&archive_dir).to_string_lossy().into_owned(),
            );
        }
        if cloud {
            if let Ok(cloud) = common::saved_path(cfg, "cloud") {
                options.insert("cloud".into(), cloud.to_string_lossy().into_owned());
            }
        }
        Args(options)
    });
    (immich.then_some(archive_dir), usb)
}
/// The monthly run's first step, before anything is backed up or updated: every backup
/// copy compared with its recorded checksum (the hourly and weekly copies compare only
/// size and dates). Damage stops the run here, with nothing changed. The USB drive is
/// checked when it is plugged in.
fn check_copies(cfgpath: &Path, cfg: &Value) -> Result<Vec<String>> {
    // Each HDD archive file is read once in this check, not by each step again.
    let _once = backup::read_once();
    let data = Path::new(string(cfg, "data_mount")?);
    let media = Path::new(string(cfg, "media_mount")?);
    let (archive, usb) = copies_to_check(cfg, cfgpath);
    let mut notes = vec![];
    if let Some(archive_dir) = archive {
        if media.join(&archive_dir).is_dir() {
            common::stage("checking every photo copy on the HDD by checksum");
            let age = cfg["photos"]["archive_min_age_seconds"]
                .as_u64()
                .unwrap_or(600);
            // A list of files that could not be written stops nothing; the run says so.
            if let Some(why) =
                backup::monthly_archive_check(cfgpath, data, media, &archive_dir, age)?
            {
                notes.push(format!("photo archive: {why}"));
            }
        }
    }
    if let Some(options) = usb {
        let uuid = options.get("uuid")?;
        // A backup folder on the NAS (NFS) has no disk to see plugged in: it is tried,
        // and a NAS that is off is said like a drive that would not mount.
        if common::is_nfs_identity(uuid) || Path::new("/dev/disk/by-uuid").join(uuid).exists() {
            common::stage("checking every copy on the USB drive by checksum");
            if let Err(e) = backup::monthly_usb_check(&options) {
                if usb_damage(&e) {
                    return Err(e);
                }
                // A full drive, one that would not mount, another filesystem: no damage
                // found, so the snapshot and updates go on, and the result says so.
                println!("USB drive: its copies could not be checked this month ({e}); the snapshot and updates go on");
                notes.push(format!("USB check: {e}"));
            }
        } else {
            println!("USB drive not plugged in: its copies are checked by checksum in a monthly run it is plugged in for");
        }
    }
    Ok(notes)
}
/// The USB check found damage (rather than failing to run): that stops the monthly run.
fn usb_damage(e: &Error) -> bool {
    e.to_string().starts_with(backup::INTEGRITY)
}
/// Said when the night ended before the apps were stopped even with the checksum check
/// postponed: no monthly snapshot can be taken until the night is longer.
const NIGHT_TOO_SHORT: &str = "The monthly run cannot fit into the night: it ended before the apps could be stopped, even with the checksum check left for another month, so no monthly snapshot was taken. Start it earlier (the monthly backup time setup asks for: backup_times.monthly) or let it run longer (maintenance.night_end_hour, up to 7)";
/// Kept on the SSD when a month's night ran out before the apps were stopped.
const NIGHT_MARKER: &str = "reports/jobs/maintenance-night-ran-out.json";
fn night_ran_out(data: &Path) -> bool {
    data.join(NIGHT_MARKER).is_file()
}
fn mark_night_ran_out(data: &Path) {
    let _ = common::atomic_json(&data.join(NIGHT_MARKER), &json!({"at": common::now()}));
}
fn clear_night_ran_out(data: &Path) {
    let _ = fs::remove_file(data.join(NIGHT_MARKER));
}
/// Said when the night ended before the apps were stopped: nothing was changed.
const NIGHT_BEFORE_STOP: &str = "Night window ended before the apps were stopped (checking every copy took the night), so no monthly snapshot was taken and nothing was updated this month. Next month the snapshot and updates go first and the check waits a month";
/// Waits between tries of a download refused for now (a busy registry, GitHub's hourly
/// limit, a dropped connection), in seconds.
const PULL_WAITS: &[u64] = &[30, 90, 240];
/// What a download refused for now says (Docker's and curl's own words, lower case).
const TRANSIENT: [&str; 13] = [
    "toomanyrequests",
    "too many requests",
    "tls handshake timeout",
    "unexpected eof",
    "i/o timeout",
    "connection reset",
    "temporary failure",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "timed out",
    "could not resolve",
    "rate limit",
];
/// What a download that can never work says (no such image or release, no access):
/// trying it again only wastes the night, whatever else the message holds.
const PERMANENT: [&str; 4] = ["manifest unknown", "not found", "unauthorized", "denied"];
/// A download refused for now. An HTTP status (429, 502, 503, 504) counts only where it
/// is said as one ("error: 503", "status 429"): the same digits occur in image digests,
/// which the message quotes.
fn transient(said: &str) -> bool {
    let said = said.to_lowercase();
    if PERMANENT.iter().any(|mark| said.contains(mark)) {
        return false;
    }
    let status = regex::Regex::new(
        r"(?:error|status(?: code)?|http(?:/[0-9.]+)?|response)[: ]+(?:429|50[234])\b",
    )
    .unwrap();
    TRANSIENT.iter().any(|mark| said.contains(mark)) || status.is_match(&said)
}
fn sleep_seconds(seconds: u64) {
    std::thread::sleep(std::time::Duration::from_secs(seconds));
}
/// `attempt`, tried again after each wait in `waits` while it fails for a reason that
/// passes (TRANSIENT); any other failure, or one that keeps failing, is returned.
fn retry_transient<T>(
    waits: &[u64],
    sleep: impl Fn(u64),
    mut attempt: impl FnMut() -> Result<T>,
) -> Result<T> {
    let mut left = waits.iter();
    loop {
        match attempt() {
            Ok(value) => return Ok(value),
            Err(e) => match left.next() {
                Some(&wait) if transient(&e.to_string()) => {
                    println!("Monthly update: a download was refused for now ({e}); trying again in {wait} seconds");
                    sleep(wait);
                }
                _ => return Err(e),
            },
        }
    }
}
type Downloads = (Value, Option<PathBuf>, Option<(u64, u64)>, Vec<String>);
/// Each app's new model with its downloaded images pinned by ID, the new Immich release
/// files (when there is one), the approved Nextcloud major step (when there is one) and
/// what was left for later (the next daily check looks again; said in its result).
fn download_updates(
    cfg: &Value,
    names: &[String],
    data: &Path,
    media: &Path,
    snapshot: &Path,
    work: &Path,
) -> Result<Downloads> {
    common::stage("downloading app updates");
    // Pull the declared version streams. Major-version migrations and OS upgrades are excluded.
    let mut updated = json!({});
    let mut immich_files = None;
    let mut nextcloud_step = None;
    let mut deferred = vec![];
    for name in names {
        let source = if name == "immich" {
            // A new Immich release that can't be looked up or checked this month waits
            // for the next one; the other apps (and Immich's own pinned release) update.
            immich_files = immich_candidate(cfg, snapshot, work).unwrap_or_else(|e| {
                println!("Immich: the new release could not be checked ({e}); the next check tries again");
                deferred.push(format!("Immich release: {e}"));
                None
            });
            immich_files
                .clone()
                .unwrap_or_else(|| data.join("compose/immich"))
        } else {
            data.join("compose").join(name)
        };
        let checked = source_model(&source, name).and_then(|m| {
            valid_model(&m, name, data, media, cfg)?;
            volumes_ours(name, &m)?;
            Ok(m)
        });
        let mut candidate = match checked {
            Ok(model) => model,
            // A new Immich release whose files don't pass the checks (another service, a
            // new folder) waits too, like one that can't be looked up: Immich stays on its
            // release, and the other apps update.
            Err(e) if name == "immich" && immich_files.is_some() => {
                println!("Immich: the new release's files did not pass the checks ({e}); the next check tries again");
                deferred.push(format!("Immich release: {e}"));
                immich_files = None;
                let current = data.join("compose/immich");
                let model = source_model(&current, name)?;
                valid_model(&model, name, data, media, cfg)?;
                volumes_ours(name, &model)?;
                model
            }
            Err(e) => return Err(e),
        };
        if name == "nextcloud" {
            let step = nextcloud_major_step(cfg, &source)?;
            // Only report a newer major when none is being installed this month;
            // otherwise the notice would describe the version being replaced.
            if step.is_none() {
                if let Err(e) = nextcloud_major_notice(cfg, snapshot) {
                    println!("Nextcloud: could not check for a newer major version ({e})");
                }
            }
            if let Some((from, to)) = step {
                println!("Nextcloud: installing approved major {to} (from {from})");
                step_nextcloud_images(&mut candidate, from, to)?;
                nextcloud_step = Some((from, to));
            }
        }
        for v in candidate["services"].as_object_mut().unwrap().values_mut() {
            let image = string(v, "image")?.to_string();
            retry_transient(PULL_WAITS, sleep_seconds, || docker(&["pull", &image]))?;
            let info: Value = serde_json::from_str(&docker(&["image", "inspect", &image])?)?;
            let id = info[0]["Id"]
                .as_str()
                .filter(|s| image_id(s))
                .ok_or_else(|| Error::new("Invalid candidate image"))?;
            // Noted at once: a run that stops later (the night ran out, a later download
            // failed, a rollback) leaves no other record of what it downloaded.
            remember_images(data, &[id.to_string()]);
            v["image"] = json!(id);
        }
        updated[name] = candidate;
    }
    Ok((updated, immich_files, nextcloud_step, deferred))
}
/// What a night's run does (MONTHLY-MAINTENANCE.md).
pub(crate) enum Plan {
    /// The monthly check: every backup copy compared with its checksum, a snapshot of
    /// every app with its isolated restore test, and the apps started again as they
    /// were. No app is updated.
    Audit,
    /// The weekly update of one app: a snapshot of that app alone (checked by checksum,
    /// so a failed update can be undone), then the version that settled installed,
    /// checked (signing in too) and kept, or undone.
    Update(Box<Candidate>),
}
/// A version of one app the daily check found (updates.rs), as the weekly update
/// installs it: its model with every image pinned by ID, a new Immich release's files,
/// an approved Nextcloud major step.
pub(crate) struct Candidate {
    pub(crate) app: String,
    pub(crate) sig: String,
    pub(crate) version: String,
    pub(crate) model: Value,
    pub(crate) immich_files: Option<PathBuf>,
    pub(crate) nextcloud_step: Option<(u64, u64)>,
    /// You chose "Update now" on the dashboard: it runs whatever the time of day.
    pub(crate) anytime: bool,
}
/// The snapshot folder of each kind of run, on the HDD.
const MONTHLY_FOLDER: &str = "backups/monthly";
pub(crate) const UPDATE_FOLDER: &str = "backups/updates";
fn transaction(cfgpath: &Path, cfg: &Value, plan: &Plan) -> Result<Vec<String>> {
    mounts(cfgpath, cfg, false)?;
    let audit = matches!(plan, Plan::Audit);
    // The night window, except for an update you started yourself.
    let in_window = || matches!(plan, Plan::Update(c) if c.anytime) || night(cfg);
    let data_root = Path::new(string(cfg, "data_mount")?);
    let (notes, postponed) = checks_first(cfgpath, cfg, audit, data_root)?;
    // A weekly USB copy at work (also when the check above was left out, or the USB
    // drive was not checked) is waited for before any app stops; a later one sees this
    // run and does not start (backup::no_weekly_copy).
    // An app update holds the USB drive's lock already (update_apps, before the
    // backups' lock); the monthly run, which a weekly copy sees and stays out of, waits here.
    if audit {
        common::stage("waiting for a weekly USB copy at work to finish");
        backup::no_weekly_copy()?;
    }
    mounts(cfgpath, cfg, false)?;
    let data = Path::new(string(cfg, "data_mount")?);
    let media = Path::new(string(cfg, "media_mount")?);
    let names = match plan {
        Plan::Audit => names(cfg)?,
        Plan::Update(c) => vec![c.app.clone()],
    };
    let folder = media.join(if audit { MONTHLY_FOLDER } else { UPDATE_FOLDER });
    // Password files go only into the encrypted archive when the backup key exists;
    // without it they stay in data.tar so the snapshot can still be restored.
    let sealing = secrets_key_ready(Path::new(SECRETS_KEY_DIR));
    let paths = snapshot_paths(&names, data, sealing);
    let mut retention = room(cfg, &folder, &names, &paths, audit)?;
    let run = format!("{}-{}", (common::now() * 1e6) as u128, std::process::id());
    common::directory(&folder, 0o700)?;
    let snapshot = folder.join(&run);
    common::directory(&snapshot, 0o700)?;
    clear_work(data);
    let work = work_dir(data, &run);
    common::directory(&data.join("backups/maintenance-work"), 0o700)?;
    common::directory(&work, 0o700)?;
    let (models, rows) = running_models(cfg, &names, &snapshot)?;
    integrations(cfgpath, cfg, false)?;
    no_other_writers(&rows, data, media, &paths)?;
    // The new version was downloaded by the daily check, while the apps ran: the
    // running apps, the snapshot and a rollback use the current images by their IDs. The
    // monthly check updates nothing: it starts the apps again as they were.
    pin_unpinned(data, &names, &models)?;
    let (updated, immich_files, nextcloud_step) = match plan {
        Plan::Audit => (models.clone(), None, None),
        Plan::Update(c) => {
            images_here(&c.model)?;
            (
                json!({ c.app.as_str(): c.model.clone() }),
                c.immich_files.clone(),
                c.nextcloud_step,
            )
        }
    };
    // Whether the app takes its saved login now: checked again after the update.
    let signed_in = match plan {
        Plan::Update(c) => signs_in(data, &c.app),
        Plan::Audit => None,
    };
    if !in_window() {
        return Err(night_ended(cfg, postponed, audit, data_root));
    }
    if audit {
        clear_night_ran_out(data_root);
    }
    common::directory(Path::new("/etc/nekoserver-backups"), 0o700)?;
    // The version an update installs, so a run cut off after its commit still marks it
    // installed (resume): it is not found and installed once more.
    let candidate = match plan {
        Plan::Update(c) => json!({"app": c.app, "sig": c.sig}),
        Plan::Audit => Value::Null,
    };
    save_journal(
        &json!({"format":"neko-maintenance-resume-v1","version":VERSION,"run":run,"kind":if audit {"audit"} else {"update"},"phase":"snapshot","snapshot":snapshot,"storage":cfg["storage"],"data_mount":cfg["data_mount"],"media_mount":cfg["media_mount"],"models":models,"containers":rows,"rehearsals":[],"candidate":candidate}),
    )?;
    common::stage("stopping the apps for a consistent snapshot");
    stop(&rows)?;
    mounts(cfgpath, cfg, false)?;
    write_snapshot(cfg, &snapshot, &work, &names, &paths, sealing, &models)?;
    set_phase("backed_up")?;
    // The isolated restore test is the monthly check's: an update's snapshot is checked
    // by checksum (verify, above), so its rollback is safe, without the extra downtime.
    if audit {
        common::stage("testing the snapshot restores");
        rehearsal(&snapshot, cfg, &models, &snapshot)?;
        common::atomic_json(
            &snapshot.join("restore-tests.json"),
            &json!({"status":"pass","method":"isolated containers started from extracted backups with HTTP probes; Immich and Nextcloud databases restored and checked by SQL","at":common::now()}),
        )?;
    }
    // An update waits for the next night when this one ended meanwhile. The monthly check
    // installs nothing: done here (the snapshot and its restore test passed), it starts
    // the apps again as they were, also past the night. Waiting would make it run again
    // every night (all apps stopped for hours), and each run, complete, would push the
    // earlier months' snapshots out of those kept.
    if !audit && !in_window() {
        return Err(Error::new(
            "Night window ended; backups complete, updates deferred",
        ));
    }
    mounts(cfgpath, cfg, false)?;
    set_phase("updating")?;
    put_new_files(data, immich_files.as_deref(), nextcloud_step)?;
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
        || {
            if let Some((_, to)) = nextcloud_step {
                nextcloud_reached(&snapshot, to)?;
            }
            if signed_in == Some(true) {
                signed_in_again(data, &names[0])?;
            }
            integrations(cfgpath, cfg, true).and_then(|_| mounts(cfgpath, cfg, false))
        },
        || {
            // Persist successful image identities for installer reruns and reboot reproducibility.
            for name in &names {
                write_override(cfg, name, &updated[name])?;
            }
            // Committed first, then the record that says so: a run cut off between the two
            // is finished as the installed update it is (resume writes the record), and one
            // cut off before both is undone with no record saying it was installed.
            set_phase("committed")?;
            common::atomic_json(
                &snapshot.join("result.json"),
                &json!({"status":"pass","updated_at":common::now(),"restore_test":if audit {"pass"} else {"not_run"},"snapshot":"hdd","images":updated}),
            )
        },
    )?;
    // The update is committed. Cleanup is best effort: a failure here must never be
    // mistaken for a failed update, and deletes only touch a re-verified USB drive.
    // Done while the journal still says "committed": checking the snapshots kept can
    // take long, and a run cut off here (a restart, the time limit) is then finished as
    // the installed update it is, not reported as a failure rolled back.
    retention["after_update"] = match mounts(cfgpath, cfg, false).and_then(|_| {
        if audit {
            prune_monthly(data, media, keep_snapshots(cfg), Some(&run))
        } else {
            prune_updates(data, media, &names[0], Some(&run))
        }
    }) {
        Ok(report) => report,
        Err(e) => json!({"status":"skipped","reason":e.to_string()}),
    };
    // The monthly check updated nothing, so no image was replaced.
    if !audit {
        retention["images_removed"] = json!(clean_images(cfg, &run, &names[0], &updated, &models)?);
    }
    let _ = common::atomic_json(&snapshot.join("retention.json"), &retention);
    finish(data, media, notes)
}
/// The monthly check's first step, before anything else: every backup copy compared
/// with its checksum (an update has no such step). When last month's night ran out
/// before the apps could be stopped (checking every copy of a big library takes long),
/// the check waits a month so the snapshot is taken this time. Returns the run's notes
/// and whether the check was postponed.
fn checks_first(
    cfgpath: &Path,
    cfg: &Value,
    audit: bool,
    data_root: &Path,
) -> Result<(Vec<String>, bool)> {
    if !audit {
        return Ok((vec![], false));
    }
    if night_ran_out(data_root) {
        println!("Monthly check: last month's night ran out during the checksum check, so this month the snapshot goes first; the check runs next month");
        return Ok((
            vec!["checksum check of every copy: postponed to next month (last month's night ran out during it)".to_string()],
            true,
        ));
    }
    Ok((check_copies(cfgpath, cfg)?, false))
}
/// Old snapshots tidied first (the monthly ones by keep_snapshots, an app's own update
/// snapshots by UPDATE_SNAPSHOTS), then each drive checked for room: the HDD for the
/// snapshot, the SSD for a rollback copy and the new images, each keeping its reserve.
/// For the monthly check, unfinished monthly snapshots are removed to make room. Returns
/// the retention report.
fn room(
    cfg: &Value,
    folder: &Path,
    names: &[String],
    paths: &(Vec<String>, Vec<String>),
    audit: bool,
) -> Result<Value> {
    let data = Path::new(string(cfg, "data_mount")?);
    let media = Path::new(string(cfg, "media_mount")?);
    let setting = cfg["maintenance"]["minimum_free_gb"]
        .as_u64()
        .unwrap_or(20)
        .saturating_mul(1 << 30);
    let app_bytes = size(data, &paths.0)?;
    let media_bytes = size(media, &paths.1)?;
    common::stage("checking free space");
    let mut retention = if audit {
        prune_monthly(data, media, keep_snapshots(cfg), None)?
    } else {
        prune_updates(data, media, &names[0], None)?
    };
    let (ssd, hdd) = space_needed(app_bytes, media_bytes, image_bytes(names)?);
    // The USB drive is not used by monthly maintenance.
    for (root, needed, what) in [(data, ssd, "SSD"), (media, hdd, "HDD")] {
        let (mut have, drive) = drive_space(root)?;
        let reserve = reserve_for(setting, drive);
        let wanted = needed.saturating_add(reserve);
        if have < wanted && what == "HDD" && audit {
            have = make_room(media, &mut retention, wanted)?;
        }
        // An update: its own undone updates' snapshots (HDD) and their failed app data (SSD).
        while have < wanted && !audit {
            let before = retention["removed"].as_array().map_or(0, Vec::len);
            make_update_room(data, media, &names[0], &mut retention)?;
            if retention["removed"].as_array().map_or(0, Vec::len) == before {
                break;
            }
            have = drive_space(root)?.0;
        }
        if have < wanted {
            let unfinished = if what == "HDD" {
                let runs: Vec<String> = retention["incomplete_not_removed"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(|r| r.as_str().map(str::to_string))
                    .collect();
                unfinished_note(folder, &runs, size(folder, &runs).unwrap_or(0))
            } else {
                String::new()
            };
            return Err(no_room(
                what,
                root,
                have,
                wanted,
                reserve,
                setting,
                &unfinished,
            ));
        }
    }
    Ok(retention)
}
fn no_room(
    what: &str,
    root: &Path,
    have: u64,
    wanted: u64,
    reserve: u64,
    setting: u64,
    unfinished: &str,
) -> Error {
    let gb = |b: u64| (b as f64 / 1e9).ceil() as u64;
    Error::new(&format!(
        "Not enough space on the {what} ({}) for the snapshot and update: {} GB free, about {} GB needed (a copy of the app data and the new app images, plus the {} GB reserve{}). Free up space there, or lower the reserve{unfinished}",
        root.display(),
        gb(have),
        gb(wanted),
        gb(reserve),
        if reserve < setting {
            ": a fifth of the drive, less than maintenance.minimum_free_gb"
        } else {
            " from maintenance.minimum_free_gb"
        }
    ))
}
/// The apps' models as they run now, each saved in the snapshot folder (`<app>-old.json`)
/// and checked to be healthy, with the containers to stop.
fn running_models(cfg: &Value, names: &[String], snapshot: &Path) -> Result<(Value, Vec<Value>)> {
    let data = Path::new(string(cfg, "data_mount")?);
    let media = Path::new(string(cfg, "media_mount")?);
    let mut models = json!({});
    let mut rows = vec![];
    for name in names {
        let mut current = model(cfg, name)?;
        valid_model(&current, name, data, media, cfg)?;
        rows.extend(containers(name, &mut current)?);
        common::atomic_json(&snapshot.join(format!("{name}-old.json")), &current)?;
        models[name] = current;
    }
    common::atomic_json(&snapshot.join("config.json"), cfg)?;
    for name in names {
        health(
            &snapshot.join(format!("{name}-old.json")),
            &format!("neko-{name}"),
            name,
            30,
            false,
            snapshot,
        )?;
    }
    Ok((models, rows))
}
/// The night ended before anything stopped. The monthly check notes it, so the next one
/// puts the snapshot first; when even that was too short, it is a failure, so it is seen.
fn night_ended(cfg: &Value, postponed: bool, audit: bool, data_root: &Path) -> Error {
    if postponed {
        return Error::new(&format!(
            "{NIGHT_TOO_SHORT} (maintenance.night_end_hour is {})",
            cfg["maintenance"]["night_end_hour"].as_i64().unwrap_or(6)
        ));
    }
    if audit {
        mark_night_ran_out(data_root);
    }
    Error::new(NIGHT_BEFORE_STOP)
}
fn set_phase(phase: &str) -> Result<()> {
    let mut journal = load(Path::new(*JOURNAL))?;
    journal["phase"] = json!(phase);
    save_journal(&journal)
}
/// The snapshot of the stopped apps: their data and settings, the password files
/// (encrypted when there is a backup key), their images, and a manifest with every
/// checksum, checked once written.
fn write_snapshot(
    cfg: &Value,
    snapshot: &Path,
    work: &Path,
    names: &[String],
    paths: &(Vec<String>, Vec<String>),
    sealing: bool,
    models: &Value,
) -> Result<()> {
    let data = Path::new(string(cfg, "data_mount")?);
    let media = Path::new(string(cfg, "media_mount")?);
    common::stage("writing the snapshot");
    let exclude: &[&str] = if sealing { &SEALED_EXCLUDES } else { &[] };
    tar(data, &paths.0, &snapshot.join("data.tar"), exclude)?;
    tar(media, &paths.1, &snapshot.join("media.tar"), &[])?;
    let password_files = encrypt_secrets(data, snapshot)?;
    if password_files == "unencrypted_in_data_tar" {
        println!("Monthly maintenance: no backup key yet, so this snapshot keeps the app password files unencrypted inside data.tar (readable by root only). To encrypt them from now on: sudo python3 backup_secrets.py --setup");
    }
    if sealing {
        // data.tar left these out; keep a private SSD copy so a failed update can be
        // rolled back unattended (the encrypted copy needs the sudo password).
        let mut journal = load(Path::new(*JOURNAL))?;
        journal["sealed_copy"] = json!(save_sealed_copy(data, work)?);
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
    let kind = if snapshot
        .parent()
        .is_some_and(|p| p.ends_with(MONTHLY_FOLDER))
    {
        "monthly"
    } else {
        "update"
    };
    let mut manifest = json!({"format":"neko-maintenance-v1","kind":kind,"apps":names,"version":VERSION,"created_at":common::now(),"storage":cfg["storage"],"paths":{"data":paths.0,"media":paths.1},"sha256":{"data":common::digest(&snapshot.join("data.tar"))?,"media":common::digest(&snapshot.join("media.tar"))?,"images":common::digest(&image_archive)?},"password_files":password_files});
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
    verify(snapshot).map(drop)
}
/// The files of the new version, into the app's settings folder: a new Immich release's
/// .env and compose file, the tag of an approved Nextcloud major (so later updates and
/// setup runs stay on it; the snapshot covers these files).
fn put_new_files(
    data: &Path,
    immich_files: Option<&Path>,
    nextcloud_step: Option<(u64, u64)>,
) -> Result<()> {
    if let Some(folder) = immich_files {
        for name in [".env", "compose.yaml"] {
            common::atomic(
                &data.join("compose/immich").join(name),
                &common::bytes(&folder.join(name), 500000)?,
                0o600,
            )?;
        }
    }
    if let Some((from, to)) = nextcloud_step {
        for name in ["compose.yaml", "managed.json"] {
            let path = data.join("compose/nextcloud").join(name);
            if path.exists() {
                let text = String::from_utf8_lossy(&common::bytes(&path, 500000)?).to_string();
                common::atomic(&path, retag_nextcloud(&text, from, to).as_bytes(), 0o600)?;
            }
        }
    }
    Ok(())
}
/// After `app`'s update: the images nothing will use again are removed (its own from
/// two updates ago, and the record's), never another app's way back (what its last
/// update replaced) nor a version the daily check downloaded for a later night. Read
/// before the old snapshots are tidied; the record on the SSD (OLD_IMAGES) also names
/// those whose run folders are gone. Returns the IDs removed.
fn clean_images(
    cfg: &Value,
    run: &str,
    app: &str,
    updated: &Value,
    models: &Value,
) -> Result<Vec<String>> {
    let data = Path::new(string(cfg, "data_mount")?);
    let media = Path::new(string(cfg, "media_mount")?);
    let mut older = previous_images(media, run, app);
    older.extend(recorded_images(data));
    let mut stay = crate::updates::waiting_images(data);
    for other in names(cfg)?.iter().filter(|n| n.as_str() != app) {
        stay.extend(last_replaced(media, run, other));
    }
    let waiting: Vec<String> = older.intersection(&stay).cloned().collect();
    older.retain(|id| !stay.contains(id));
    let own = own_repositories(updated, &image_tags);
    let removed = prune_images(&older, updated, models, &|id| {
        image_tags(id).is_some_and(|tags| only_ours(&tags, &own))
            && docker(&["image", "rm", id]).is_ok()
    });
    let mut recorded = still_recorded(&older, &removed, models, &|id| image_tags(id).is_some());
    // Still needed now (a version waiting, another app's way back), but noted, so the
    // daily check removes them once they are not.
    recorded.extend(waiting);
    if let Err(e) = save_recorded_images(data, &recorded) {
        println!("App update: could not note the replaced images for the next clean-up ({e})");
    }
    if !removed.is_empty() {
        println!("App update: removed {} app image(s) from two updates ago that nothing uses (each kept snapshot still has its own in images.tar)", removed.len());
    }
    Ok(removed)
}
/// The run is over: its journal goes, saved passwords in old snapshots are blanked.
fn finish(data: &Path, media: &Path, notes: Vec<String>) -> Result<Vec<String>> {
    fs::remove_file(*JOURNAL)?;
    fs::File::open(Path::new(*JOURNAL).parent().unwrap())?.sync_all()?;
    scrub_all(media);
    clear_work(data);
    Ok(notes)
}
pub fn run(a: &Args, recover: bool) -> Result<()> {
    common::require_root()?;
    let cfgpath = a.path("config")?;
    private(&cfgpath)?;
    let cfg = load(&cfgpath)?;
    let data = common::mount_path(string(&cfg, "data_mount")?)?;
    if recover && nothing_to_finish(&data) {
        // At every boot, and when setup enables this: nothing to finish, so no wait for
        // the lock a nightly backup may hold for hours.
        return Ok(());
    }
    mounts(&cfgpath, &cfg, false)?;
    let lock = data.join("installer/recovery.lock");
    let _lock = if recover && hourly_retry() {
        // The hourly retry of an unfinished update: when another job holds the lock (the
        // monthly run itself, a backup), it simply tries again next hour.
        match common::lock(&lock) {
            Ok(held) => held,
            Err(_) => {
                println!("Monthly update: another NekoServer job is working; the unfinished update is tried again in an hour");
                return Ok(());
            }
        }
    } else if recover {
        common::lock_wait(&lock, std::time::Duration::from_secs(3 * 3600))?
    } else {
        // Setup holds this lock too, also while it waits at a question: after half an
        // hour the month's run gives way (it would take the night) and comes back the
        // next night, instead of failing and paging you.
        match common::shared_lock_wait(&lock, std::time::Duration::from_secs(3 * 3600), SETUP_WAIT)
        {
            Ok(held) => held,
            Err(e) if common::postponed(&e) => {
                return Err(Error::new(&format!(
                    "{}; {}",
                    common::POSTPONED,
                    again_next_night(&cfg)
                )))
            }
            Err(e) => return Err(e),
        }
    };
    let installed = committed();
    // Read before resume() finishes the run and removes the journal.
    let cut_in = load(Path::new(*JOURNAL))
        .ok()
        .and_then(|j| j["phase"].as_str().map(str::to_string));
    resume(&cfgpath, &cfg)?;
    if recover {
        for job in ["maintenance", "updates"] {
            finish_report(
                &data.join(format!("reports/jobs/{job}.json")),
                installed,
                cut_in.as_deref(),
            )?;
        }
        return Ok(());
    }
    if !night(&cfg) {
        return Err(Error::new(
            "Maintenance runs only within the configured night window",
        ));
    }
    // Read before the run writes its own report: how many nights running it failed.
    let tried = retries_made(&common::json(
        &data.join("reports/jobs/maintenance.json"),
        64000,
    ));
    let undone = std::cell::Cell::new(false);
    let result = backup::execute_with(Some(&data), "maintenance", &cfgpath, || {
        no_upgrade()?;
        let failure = match transaction(&cfgpath, &cfg, &Plan::Audit) {
            Ok(deferred) if deferred.is_empty() => return Ok(backup::Outcome::default()),
            // Updated, with one part left for next month (a new Immich release that
            // could not be checked): done in part, and the dashboard says which.
            Ok(deferred) => {
                return Ok(backup::Outcome {
                    partial: Some(json!(deferred)),
                })
            }
            Err(failure) => failure,
        };
        // Said first, so the reason survives whatever happens while putting things right.
        println!("Monthly update: {failure}");
        let installed = committed();
        let phase = load(Path::new(*JOURNAL))
            .ok()
            .and_then(|j| j["phase"].as_str().map(str::to_string));
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
            // The night ran out: after the snapshot and its restore test (only the update
            // waits), or before the apps were stopped (no snapshot either, said as it is).
            // Done in part, not failed: the next run goes on from there.
            let said = failure.to_string();
            let said = if said == NIGHT_BEFORE_STOP {
                said
            } else {
                format!("app updates: {said}")
            };
            // What the night left undone is taken up again the next night, not next month.
            return Ok(backup::Outcome {
                partial: Some(json!([format!("{said}; {}", again_next_night(&cfg))])),
            });
        }
        undone.set(rolled_back(phase.as_deref()));
        Err(failure)
    });
    if result.is_err() && undone.get() {
        // For the dashboard: the update was undone and the apps run as before.
        let _ =
            backup::note_failed_report(&cfgpath, &data, "maintenance", "rolled_back", json!(true));
    }
    match result {
        // A failed run comes back the next night (a few nights), not next month: a month
        // without a verified snapshot and the updates is too long for a passing trouble.
        Err(failure) if !common::postponed(&failure) && tried < RETRY_NIGHTS => {
            let again = again_next_night(&cfg);
            // On a line of its own too: a long failure text leaves no room for it in the
            // report's reason (300 characters, in the log's last line as well).
            println!(
                "Monthly update: {again} (night {} of {RETRY_NIGHTS})",
                tried + 1
            );
            let note = |key: &str, value: Value| {
                backup::note_failed_report(&cfgpath, &data, "maintenance", key, value)
            };
            let _ = note("retries", json!(tried + 1));
            let _ = note("retry_at", json!(again));
            // The time itself too, for programs (the dashboard), when it was arranged.
            let next = again.strip_prefix(TRIES_AGAIN_ON);
            let _ = note("next_attempt", json!(next));
            Err(Error::new(&format!(
                "{failure}; {again} (night {} of {RETRY_NIGHTS})",
                tried + 1
            )))
        }
        Err(failure) if !common::postponed(&failure) => Err(Error::new(&format!(
            "{failure}; it failed {RETRY_NIGHTS} nights running, so it runs again next month"
        ))),
        other => other,
    }
}
/* ---------- weekly app updates (updates.rs keeps the versions found) ---------- */

/// The USB drive's lock and the backups' one, for an app update. A weekly USB copy holds
/// the USB lock and then takes the backups' lock for its Nextcloud database copy; the
/// monthly run holds the backups' lock and then waits for the USB lock. Holding one while
/// waiting for the other would make one of them wait hours for the other. So: the USB lock
/// (waited for, holding nothing), then the backups' lock only if it is free at once;
/// otherwise the USB lock goes back, the backups' lock is waited for (holding nothing),
/// let go, and it starts again.
fn both_locks<U, R>(
    _data: &Path,
    usb: impl Fn() -> Result<U>,
    try_backups: impl Fn() -> Result<R>,
    wait_backups: impl Fn() -> Result<R>,
) -> Result<(U, R)> {
    for _ in 0..12 {
        let held = usb()?;
        if let Ok(backups) = try_backups() {
            return Ok((held, backups));
        }
        drop(held);
        drop(wait_backups()?);
    }
    Err(Error::new(
        "Another NekoServer job kept the USB drive or the backups busy; the update was not started (it is tried again at its next time)",
    ))
}
/// The backups' shared lock, as the monthly run takes it: setup holding it for half an
/// hour postpones the job (it is not a failure, and it runs at its next time).
fn job_lock(data: &Path) -> Result<fs::File> {
    common::shared_lock_wait(
        &data.join("installer/recovery.lock"),
        Duration::from_secs(3 * 3600),
        SETUP_WAIT,
    )
}
const UNFINISHED_UPDATE: &str = "An update is not finished yet, so nothing else is looked at or installed; it is finished by: sudo systemctl start nekoserver-maintenance-resume.service";
/// A NekoServer upgrade at work or cut off (upgrade.py keeps this record): no app update,
/// check or monthly run starts until it is over, so the two never overlap.
pub(crate) static UPGRADE_JOURNAL: std::sync::LazyLock<&str> =
    std::sync::LazyLock::new(|| crate::common::journal("upgrade"));
const UPGRADING: &str = "NekoServer is being upgraded (or an upgrade was cut off and is being put right), so this waits for its next time; the Updates page, NekoServer tab, shows how far it is";
/// A move of the SSD or the HDD to a new drive at work or cut off (migrate.py).
pub(crate) const MIGRATE_JOURNAL: &str = "/etc/nekoserver-backups/migrate.json";
const MOVING: &str = "A drive is being moved to a new one (or a move was cut off: sudo python3 migrate.py resume), so this waits for its next time";
fn no_upgrade() -> Result<()> {
    if Path::new(*UPGRADE_JOURNAL).exists() {
        return Err(Error::new(UPGRADING));
    }
    if Path::new(MIGRATE_JOURNAL).exists() {
        return Err(Error::new(MOVING));
    }
    Ok(())
}
/// Every image of `model` (pinned by ID) is still on this PC.
fn images_here(model: &Value) -> Result<()> {
    for (service, v) in model["services"].as_object().into_iter().flatten() {
        let image = v["image"].as_str().unwrap_or("");
        if !image_id(image) || docker(&["image", "inspect", "--format", "{{.Id}}", image]).is_err()
        {
            return Err(Error::new(&format!(
                "the image the daily check downloaded for {service} is not on this PC any more; the next check downloads it again"
            )));
        }
    }
    Ok(())
}
/// Whether `app` takes its saved login (account-setup.py signin, of this release): Some
/// answer, or None when there is no saved login to try (accounts off, or none for it)
/// or the tool is not there. Read-only: nothing is made or changed in the app.
fn signs_in(data: &Path, app: &str) -> Option<bool> {
    let release = data.join(format!("installer/releases/v{VERSION}"));
    let tool = release.join("account-setup.py");
    let settings = release.join("config.json");
    if !tool.is_file() || !settings.is_file() {
        return None;
    }
    let (code, said) = common::probe(
        &[
            "python3",
            // No environment or user packages; the tool's own folder holds its modules.
            "-Es",
            &tool.to_string_lossy(),
            "signin",
            app,
            "--config",
            &settings.to_string_lossy(),
        ],
        300,
    );
    match code {
        0 => Some(true),
        1 => {
            println!(
                "{app}: {}",
                said.trim()
                    .lines()
                    .last()
                    .unwrap_or("the saved login was refused")
            );
            Some(false)
        }
        _ => None,
    }
}
/// After an update: the app took its saved login before it, so it must take it now.
fn signed_in_again(data: &Path, app: &str) -> Result<()> {
    match signs_in(data, app) {
        Some(true) => {
            println!("{app}: signing in with its saved login works after the update");
            Ok(())
        }
        _ => Err(Error::new(&format!(
            "{app} no longer accepts its saved login after the update (it did before)"
        ))),
    }
}
/// The service that stands for the app: its version is the one shown.
fn main_service(app: &str) -> &str {
    if app == "immich" {
        "immich-server"
    } else {
        app
    }
}
/// How a version found is shown: an Immich release, a Nextcloud major, the image's own
/// version label, or the day its image was built.
fn version_of(app: &str, model: &Value, release: Option<&str>, step: Option<(u64, u64)>) -> String {
    if let Some(tag) = release {
        return tag.to_string();
    }
    if let Some((_, to)) = step {
        return format!("{to} (a new major version you approved)");
    }
    let image = model["services"][main_service(app)]["image"]
        .as_str()
        .unwrap_or("");
    let inspect = |format: &str| docker(&["image", "inspect", "--format", format, image]).ok();
    let label = inspect("{{index .Config.Labels \"org.opencontainers.image.version\"}}")
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty() && s != "<no value>");
    label.unwrap_or_else(|| {
        let built = inspect("{{.Created}}").unwrap_or_default();
        format!(
            "new build of {}",
            built.trim().get(..10).unwrap_or("an unknown day")
        )
    })
}
/// `update-check` (daily, while the apps run): look for a new version of each app,
/// download it, and note it with the day it was first seen. Nothing is stopped or
/// changed: the weekly update installs a version once it has settled.
/// `check-compose --config <settings> --app <name> --model <file>`: an app's Compose
/// model (as `docker compose config --format json` gives it) checked with the update's
/// own rules (valid_model), for setup to use before it first starts an app, so the rules
/// exist once. Says "ok", or fails with the reason.
pub fn check_compose(a: &Args) -> Result<()> {
    if let (Ok(upload), Ok(database)) = (a.path("upload"), a.path("database")) {
        // A snapshot's Immich file (setup's recovery_tools.validate_model asks this).
        let model: Value = serde_json::from_slice(&common::bytes(&a.path("model")?, 4_000_000)?)?;
        snapshot_ok(&model, &upload, &database)?;
        println!("ok");
        return Ok(());
    }
    let cfg = common::json(&a.path("config")?, 2_000_000);
    let app = a.get("app")?;
    if !["immich", "nextcloud"].contains(&app) && !MEDIA.contains(&app) {
        return Err(Error::new("Unknown app"));
    }
    let model: Value = serde_json::from_slice(&common::bytes(&a.path("model")?, 4_000_000)?)?;
    let data = common::mount_path(string(&cfg, "data_mount")?)?;
    let media = common::mount_path(string(&cfg, "media_mount")?)?;
    valid_model(&model, app, &data, &media, &cfg)?;
    volumes_ours(app, &model)?;
    println!("ok");
    Ok(())
}
pub fn check_updates(a: &Args) -> Result<()> {
    common::require_root()?;
    let cfgpath = a.path("config")?;
    private(&cfgpath)?;
    let cfg = load(&cfgpath)?;
    let data = common::mount_path(string(&cfg, "data_mount")?)?;
    let media = common::mount_path(string(&cfg, "media_mount")?)?;
    mounts(&cfgpath, &cfg, false)?;
    let _lock = job_lock(&data)?;
    backup::execute_with(Some(&data), "update_check", &cfgpath, || {
        no_upgrade()?;
        if Path::new(*JOURNAL).exists() {
            return Err(Error::new(UNFINISHED_UPDATE));
        }
        mounts(&cfgpath, &cfg, false)?;
        let notes = look_for_updates(&cfg, &data, &media)?;
        Ok(backup::Outcome {
            partial: (!notes.is_empty()).then(|| json!(notes)),
        })
    })
}
/// Folders under the SSD's updates folder: major-version notices for the dashboard, and
/// the work folder a new Immich release's files are checked in.
const NOTES: &str = "notes";
const WORK: &str = "work";
fn look_for_updates(cfg: &Value, data: &Path, media: &Path) -> Result<Vec<String>> {
    let names = names(cfg)?;
    let mut models = json!({});
    for name in &names {
        models[name] = model(cfg, name)?;
    }
    // An app never updated yet is pinned to what it runs now, so a later setup run never
    // starts the downloaded version untested.
    if !pin_unpinned(data, &names, &models)?.is_empty() {
        for name in &names {
            models[name] = model(cfg, name)?;
        }
    }
    let root = data.join(crate::updates::FOLDER);
    common::directory(&root, 0o700)?;
    let (notes, work) = (root.join(NOTES), root.join(WORK));
    for folder in [&notes, &work] {
        if fs::symlink_metadata(folder).is_ok() {
            fs::remove_dir_all(common::safe(folder)?)?;
        }
        common::directory(folder, 0o700)?;
    }
    let mut pending = crate::updates::pending(data);
    let settle = crate::updates::settle_days(cfg);
    let mut said = vec![];
    for name in &names {
        // Never a download for an app not pinned by image ID: it would move the app's tag
        // to an image setup would then start untested.
        if !pinned_by_id(data, name) {
            said.push(format!(
                "{name}: not looked for (its images could not be pinned: is it running?)"
            ));
            continue;
        }
        // Docker keeps the images on the SSD: a download that would leave it with less
        // than the reserve waits (the next check looks again), as an update would.
        if let Some(why) = no_download_room(cfg, data, name, drive_space(data), image_bytes) {
            println!("{name}: {why}; the next check tries again");
            said.push(format!("{name}: {why}"));
            continue;
        }
        let (updated, immich_files, step, deferred) =
            match download_updates(cfg, std::slice::from_ref(name), data, media, &notes, &work) {
                Ok(found) => found,
                Err(e) => {
                    println!(
                    "{name}: no new version could be looked for ({e}); the next check tries again"
                );
                    said.push(format!("{name}: {e}"));
                    continue;
                }
            };
        said.extend(deferred);
        let candidate = &updated[name.as_str()];
        let current = &models[name.as_str()];
        let running = match name.as_str() {
            "immich" => immich_release(&data.join("compose/immich")).ok(),
            _ => None,
        };
        let now = version_of(name, current, running.as_deref(), None);
        crate::updates::note_installed(&mut pending, name, &now, common::now());
        let sig = crate::updates::signature(candidate);
        if sig == crate::updates::signature(current) {
            crate::updates::forget(data, &mut pending, name)?;
            continue;
        }
        let release = match &immich_files {
            Some(folder) => Some(immich_release(folder)?),
            None => None,
        };
        let version = version_of(name, candidate, release.as_deref(), step);
        save_candidate(
            data,
            name,
            &sig,
            candidate,
            immich_files.as_deref(),
            release.as_deref(),
            step,
        )?;
        if crate::updates::record(
            data,
            &mut pending,
            name,
            (&sig, &version),
            common::now(),
            settle,
        )? {
            println!("{name}: {version} found; it is installed on an update night once it has waited {settle} days, or at the next one if you choose \"Install at the next update night\" on the dashboard");
        }
    }
    crate::updates::save_pending(data, &pending)?;
    let _ = fs::remove_dir_all(common::safe(&work)?);
    // The images of versions no longer waiting (a newer one replaced them, the app runs
    // them now, setup changed the app): removed now, not only after an update installs.
    let removed = clean_downloads(data, media, &models, &names);
    if !removed.is_empty() {
        println!(
            "Update check: removed {} downloaded image(s) no version waiting needs any more",
            removed.len()
        );
    }
    // NekoServer's own releases: noted for the NekoServer tab, never installed by
    // themselves (an upgrade is yours to start).
    let channel = crate::updates::channel(cfg);
    match release_fetch(crate::updates::RELEASES)
        .and_then(|text| Ok(serde_json::from_str::<Value>(&text)?))
    {
        Ok(list) => {
            let found = crate::updates::newest_release(&list, channel, VERSION);
            if let Some(tag) = found.as_ref().and_then(|r| r["tag"].as_str()) {
                println!("NekoServer: {tag} is available (this server runs {VERSION}); upgrade from the dashboard's Updates page when you are ready");
            }
            crate::updates::save_release(data, VERSION, channel, found, common::now())?;
        }
        Err(e) => said.push(format!("NekoServer releases: could not be looked up ({e})")),
    }
    Ok(said)
}
/// Why the daily check must not download `app`'s new images now, or None: the SSD
/// (Docker's data folder) would keep less than the update's reserve
/// (`maintenance.minimum_free_gb`, at most a fifth of the drive) after images as big as
/// the ones the app runs now.
fn no_download_room(
    cfg: &Value,
    data: &Path,
    app: &str,
    space: Result<(u64, u64)>,
    images: fn(&[String]) -> Result<u64>,
) -> Option<String> {
    let setting = cfg["maintenance"]["minimum_free_gb"]
        .as_u64()
        .unwrap_or(20)
        .saturating_mul(1 << 30);
    let (free, drive) = match space {
        Ok(found) => found,
        Err(e) => {
            return Some(format!(
                "the free space on the SSD ({}) could not be read ({e})",
                data.display()
            ))
        }
    };
    let need = images(&[app.to_string()]).unwrap_or(0);
    let wanted = need.saturating_add(reserve_for(setting, drive));
    (free < wanted).then(|| {
        let gb = |b: u64| (b as f64 / 1e9).ceil() as u64;
        format!(
            "its new version was not downloaded: the SSD has {} GB free, and about {} GB are needed (images as big as the ones it runs now, plus the reserve)",
            gb(free),
            gb(wanted)
        )
    })
}
/// After the daily check: the images it downloaded (noted in OLD_IMAGES) that no
/// version waiting, no app now and no last update's way back uses are removed from
/// Docker; images of your own never (only IDs NekoServer noted, with only its names).
/// Returns those removed. The ones still needed stay noted for a later clean-up.
fn clean_downloads(data: &Path, media: &Path, models: &Value, names: &[String]) -> Vec<String> {
    let noted = recorded_images(data);
    let mut stay = crate::updates::waiting_images(data);
    for name in names {
        stay.extend(last_replaced(media, "", name));
    }
    let older: std::collections::BTreeSet<String> = noted.difference(&stay).cloned().collect();
    let own = own_repositories(models, &image_tags);
    let removed = prune_images(&older, models, &json!({}), &|id| {
        image_tags(id).is_some_and(|tags| only_ours(&tags, &own))
            && docker(&["image", "rm", id]).is_ok()
    });
    let mut recorded = still_recorded(&older, &removed, &json!({}), &|id| image_tags(id).is_some());
    recorded.extend(noted.intersection(&stay).cloned());
    if let Err(e) = save_recorded_images(data, &recorded) {
        println!("Update check: could not note the downloaded images for the next clean-up ({e})");
    }
    removed
}
/// The release a new Immich candidate's files name (IMMICH_VERSION in its .env).
fn immich_release(folder: &Path) -> Result<String> {
    String::from_utf8_lossy(&common::bytes(&folder.join(".env"), 64000)?)
        .lines()
        .find_map(|l| l.strip_prefix("IMMICH_VERSION="))
        .map(|v| v.trim().to_string())
        .filter(|v| {
            regex::Regex::new(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
                .unwrap()
                .is_match(v)
        })
        .ok_or_else(|| Error::new("Invalid Immich release in the new files"))
}
/// What the weekly update needs of a version found, kept on the SSD: the image of each
/// service, a new Immich release's compose file (its .env, with the passwords, is made
/// again from the current one when it is installed) and an approved Nextcloud step.
fn save_candidate(
    data: &Path,
    app: &str,
    sig: &str,
    model: &Value,
    immich_files: Option<&Path>,
    release: Option<&str>,
    step: Option<(u64, u64)>,
) -> Result<()> {
    let folder = crate::updates::candidate_folder(data, app, sig);
    if folder.join("candidate.json").is_file() {
        return Ok(());
    }
    common::directory(folder.parent().unwrap_or(&folder), 0o700)?;
    common::directory(&folder, 0o700)?;
    if let Some(files) = immich_files {
        common::atomic(
            &folder.join("compose.yaml"),
            &common::bytes(&files.join("compose.yaml"), 500000)?,
            0o600,
        )?;
    }
    let images: serde_json::Map<String, Value> = model["services"]
        .as_object()
        .into_iter()
        .flatten()
        .map(|(service, v)| (service.clone(), v["image"].clone()))
        .collect();
    common::atomic_json(
        &folder.join("candidate.json"),
        &json!({"format": "neko-update-candidate-v1", "app": app, "images": images, "immich_release": release, "nextcloud_step": step.map(|(f, t)| [f, t])}),
    )
}
/// The version `found` of `app`, made from the app's settings as they are now (setup may
/// have changed them since the check) with the images the check downloaded. None when
/// its services no longer match what is set up (setup changed the app): the next check
/// looks again.
fn load_candidate(
    cfg: &Value,
    data: &Path,
    app: &str,
    found: &Value,
    work: &Path,
) -> Result<Option<Candidate>> {
    let sig = found["sig"].as_str().unwrap_or("").to_string();
    let folder = crate::updates::candidate_folder(data, app, &sig);
    let saved = load(&folder.join("candidate.json"))?;
    if saved["format"] != "neko-update-candidate-v1" || saved["app"] != app {
        return Err(Error::new("The saved new version is not valid"));
    }
    let current = data.join("compose").join(app);
    let mut immich_files = None;
    let source = match saved["immich_release"].as_str() {
        Some(tag) => {
            // A new major only while you still approve it: an approval taken back after
            // the check downloaded it keeps the cached version from being installed.
            let installed =
                String::from_utf8_lossy(&common::bytes(&current.join(".env"), 64000)?).into_owned();
            if !immich_release_allowed(cfg, &installed, tag) {
                println!("Immich {tag}: a new major that is no longer approved (maintenance.immich_major_update); not installed");
                return Ok(None);
            }
            // The new release's compose file, with today's .env (and graphics file)
            // naming the new release.
            let files = work.join("immich-candidate");
            common::directory(work, 0o700)?;
            common::directory(&files, 0o700)?;
            common::atomic(
                &files.join("compose.yaml"),
                &common::bytes(&folder.join("compose.yaml"), 500000)?,
                0o600,
            )?;
            let env: String =
                String::from_utf8_lossy(&common::bytes(&current.join(".env"), 64000)?)
                    .lines()
                    .map(|l| {
                        if l.starts_with("IMMICH_VERSION=") {
                            format!("IMMICH_VERSION={tag}")
                        } else {
                            l.to_string()
                        }
                    })
                    .collect::<Vec<_>>()
                    .join("\n")
                    + "\n";
            common::atomic(&files.join(".env"), env.as_bytes(), 0o600)?;
            if current.join(HARDWARE).is_file() {
                common::atomic(
                    &files.join(HARDWARE),
                    &common::bytes(&current.join(HARDWARE), 64000)?,
                    0o600,
                )?;
            }
            immich_files = Some(files.clone());
            files
        }
        None => current.clone(),
    };
    let mut model = source_model(&source, app)?;
    let step = match saved["nextcloud_step"]
        .as_array()
        .map(|s| (s[0].as_u64(), s[1].as_u64()))
    {
        Some((Some(from), Some(to))) => {
            // Still approved, and still the next major from what is installed.
            if nextcloud_major_step(cfg, &source)? != Some((from, to)) {
                return Ok(None);
            }
            step_nextcloud_images(&mut model, from, to)?;
            Some((from, to))
        }
        _ => None,
    };
    let images = saved["images"].as_object().cloned().unwrap_or_default();
    let services = model["services"]
        .as_object_mut()
        .ok_or_else(|| Error::new("Invalid model"))?;
    if services.len() != images.len() || services.keys().any(|k| !images.contains_key(k)) {
        return Ok(None);
    }
    for (service, v) in services.iter_mut() {
        v["image"] = images[service].clone();
    }
    valid_model(
        &model,
        app,
        data,
        Path::new(string(cfg, "media_mount")?),
        cfg,
    )?;
    // Checked again when it is installed: a volume of its name may have been made since.
    volumes_ours(app, &model)?;
    Ok(Some(Candidate {
        app: app.to_string(),
        sig,
        version: found["version"].as_str().unwrap_or("").to_string(),
        model,
        immich_files,
        nextcloud_step: step,
        anytime: false,
    }))
}
/// `update-apps` (weekly, on the update night): each app with a version that has
/// settled (or that you chose to install) is updated on its own, one after the other:
/// a snapshot of that app, the update, the checks, then kept or undone. One app's
/// failed update is undone and the others still go ahead.
pub fn update_apps(a: &Args) -> Result<()> {
    common::require_root()?;
    let cfgpath = a.path("config")?;
    private(&cfgpath)?;
    let cfg = load(&cfgpath)?;
    let data = common::mount_path(string(&cfg, "data_mount")?)?;
    mounts(&cfgpath, &cfg, false)?;
    // "Update now" for one app (the dashboard starts nekoserver-update-now@<app>:<sig>):
    // exactly the version you chose there, whatever the time, through the same steps.
    let only = update_now_target(&a.value("app", ""), &names(&cfg)?)?;
    // Both the USB drive's lock and the backups' one, never one held while waiting for
    // the other (see both_locks): a weekly copy takes them in one order, the monthly run
    // in the other.
    common::stage("waiting for a weekly USB copy or another job at work to finish");
    let (_usb, _lock) = both_locks(
        &data,
        backup::hold_usb,
        || common::lock(&data.join("installer/recovery.lock")),
        || job_lock(&data),
    )?;
    // Anything a cut-off run left is finished first (or undone).
    resume(&cfgpath, &cfg)?;
    if only.is_none() && !night(&cfg) {
        return Err(Error::new(
            "App updates run only within the configured night window",
        ));
    }
    let undone = std::cell::Cell::new(false);
    let result = backup::execute_with(Some(&data), "updates", &cfgpath, || {
        no_upgrade()?;
        install_due(
            &cfgpath,
            &cfg,
            &data,
            &undone,
            only.as_ref().map(|(app, sig)| (app.as_str(), sig.as_str())),
        )
    });
    if result.is_err() && undone.get() {
        let _ = backup::note_failed_report(&cfgpath, &data, "updates", "rolled_back", json!(true));
    }
    result
}
/// "Update now"'s `<app>:<sig>`: the app (one set up here) and the version chosen. No
/// value: the update night, every app.
fn update_now_target(value: &str, apps: &[String]) -> Result<Option<(String, String)>> {
    if value.is_empty() {
        return Ok(None);
    }
    let Some((app, sig)) = value.split_once(':') else {
        return Err(Error::new(
            "Update now names the version to install (<app>:<version>); start it from the dashboard's Updates page",
        ));
    };
    if !apps.iter().any(|n| n == app) {
        return Err(Error::new("That app is not set up on this server"));
    }
    if !crate::updates::valid_signature(sig) {
        return Err(Error::new("That is not a version the check found"));
    }
    Ok(Some((app.to_string(), sig.to_string())))
}
/// `only`: "Update now" for that one app, with exactly the version chosen.
fn install_due(
    cfgpath: &Path,
    cfg: &Value,
    data: &Path,
    undone: &std::cell::Cell<bool>,
    only: Option<(&str, &str)>,
) -> Result<backup::Outcome> {
    let pending = crate::updates::pending(data);
    let approved = crate::updates::approved(data);
    let settle = crate::updates::settle_days(cfg);
    let now = common::now();
    let work = data.join(crate::updates::FOLDER).join(WORK);
    let (mut done, mut failed) = (vec![], vec![]);
    let apps = match only {
        Some((app, _)) => vec![app.to_string()],
        None => names(cfg)?,
    };
    for app in apps {
        let ready = |sig: &str| {
            crate::updates::candidate_folder(data, &app, sig)
                .join("candidate.json")
                .is_file()
        };
        let found = match only {
            Some((_, sig)) => crate::updates::exact(&pending, &app, sig, &ready),
            None => crate::updates::due(&pending, &approved, &app, now, settle, &ready),
        };
        let Some(found) = found else {
            if only.is_some() {
                failed.push(format!(
                    "{app}: the version you chose is not waiting any more (Check for updates looks again)"
                ));
            }
            continue;
        };
        if only.is_none() && !night(cfg) {
            failed.push(format!(
                "{app}: the night ended first; it is updated on the next update night"
            ));
            continue;
        }
        let candidate = match load_candidate(cfg, data, &app, &found, &work) {
            Ok(Some(c)) => Candidate {
                anytime: only.is_some(),
                ..c
            },
            Ok(None) => {
                println!("{app}: setup changed it since the new version was found; the next check looks again");
                let _held = crate::updates::hold(data)?;
                let mut left = crate::updates::pending(data);
                crate::updates::forget(data, &mut left, &app)?;
                crate::updates::save_pending(data, &left)?;
                continue;
            }
            Err(e) => {
                failed.push(format!("{app}: {e}"));
                continue;
            }
        };
        let (sig, version) = (candidate.sig.clone(), candidate.version.clone());
        println!(
            "App update: {app} {version} ({}): snapshot of {app}, then the update and its checks",
            if only.is_some() {
                "you chose Update now"
            } else if found["approved"] == true {
                "you chose to install it"
            } else {
                "it has settled"
            }
        );
        let outcome = transaction(cfgpath, cfg, &Plan::Update(Box::new(candidate)));
        let _ = fs::remove_dir_all(&work);
        match outcome {
            Ok(_) => {
                crate::updates::installed(data, &app, &sig)?;
                println!("App update: {app} {version} installed and checked");
                done.push(format!("{app} {version}"));
            }
            Err(failure) => {
                println!("App update: {app}: {failure}");
                let installed = committed();
                let phase = load(Path::new(*JOURNAL))
                    .ok()
                    .and_then(|j| j["phase"].as_str().map(str::to_string));
                if let Err(e) = resume(cfgpath, cfg) {
                    return Err(Error::new(&format!("{app}: {failure}; then: {e}")));
                }
                tidy_after_failure(cfg);
                if installed {
                    crate::updates::installed(data, &app, &sig)?;
                    done.push(format!("{app} {version}"));
                } else if failure.to_string() == NIGHT_BEFORE_STOP {
                    failed.push(format!(
                        "{app}: the night ended first; it is updated on the next update night"
                    ));
                } else {
                    if rolled_back(phase.as_deref()) {
                        undone.set(true);
                    }
                    failed.push(format!("{app} {version}: {failure}; undone, {app} runs its previous version and the update is tried again next week"));
                }
            }
        }
    }
    if done.is_empty() && failed.is_empty() {
        println!(
            "App updates: nothing has settled yet (or nothing new was found); nothing was stopped"
        );
    }
    match (done.is_empty(), failed.is_empty()) {
        (_, true) => Ok(backup::Outcome::default()),
        (false, false) => Ok(backup::Outcome {
            partial: Some(json!(failed)),
        }),
        (true, false) => Err(Error::new(&failed.join("; "))),
    }
}
/// A run's report that still says "running" after it was cut off (a power cut, say),
/// once the resume service has finished it: a success when the update had been
/// installed (it stands), else a failure saying when it was cut off.
fn finish_report(report: &Path, installed: bool, cut_in: Option<&str>) -> Result<()> {
    if !report.exists() {
        return Ok(());
    }
    let mut value = load(report)?;
    if value["status"] != "running" {
        return Ok(());
    }
    let now = common::now();
    value["status"] = json!(if installed { "pass" } else { "fail" });
    value["finished_at"] = json!(now);
    if installed {
        value["last_success"] = json!(now);
    } else {
        value["reason"] = json!(cut_off_reason(cut_in));
        if rolled_back(cut_in) {
            value["rolled_back"] = json!(true);
        }
    }
    common::atomic_json(report, &value)
}
/// How many nights running a failed monthly run is tried again before it is left to the
/// next month.
const RETRY_NIGHTS: u64 = 3;
/// How many nights running the last run failed and was set to try again (its report
/// says, "retries" with "retry_at"); 0 after a success, a partial run or a first failure.
fn retries_made(report: &Value) -> u64 {
    if report["status"] == "fail" && report["retry_at"].is_string() {
        report["retries"].as_u64().unwrap_or(0)
    } else {
        0
    }
}
/// Whether a failed run undid an update: its journal had got as far as installing it
/// (or putting it back). Before that nothing was changed; the apps were only stopped
/// and started again, as they were.
fn rolled_back(phase: Option<&str>) -> bool {
    matches!(phase, Some("updating" | "restoring"))
}
/// Every image ID the apps' models in `models` ({app: model}) name.
fn model_images(models: &Value) -> std::collections::BTreeSet<String> {
    models
        .as_object()
        .into_iter()
        .flat_map(|m| m.values())
        .flat_map(|m| {
            m["services"]
                .as_object()
                .into_iter()
                .flat_map(|s| s.values())
        })
        .filter_map(|v| {
            v["image"]
                .as_str()
                .filter(|s| image_id(s))
                .map(str::to_string)
        })
        .collect()
}
/// The images the last update of `app` before `current` replaced (that run's
/// `<app>-old.json`): the app's own update snapshots first, else (before 0.42.0) the
/// last monthly run that updated every app. A monthly check (kind "monthly") updated
/// nothing, so it is passed over.
fn last_replaced(media: &Path, current: &str, app: &str) -> std::collections::BTreeSet<String> {
    let old = format!("{app}-old.json");
    [UPDATE_FOLDER, MONTHLY_FOLDER]
        .iter()
        .map(|f| media.join(f))
        .find_map(|folder| {
            runs_in(&folder)
                .ok()?
                .into_iter()
                .rev()
                .filter(|r| r != current)
                .map(|r| folder.join(r))
                .find(|run| {
                    run.join("result.json").is_file()
                        && run.join(&old).is_file()
                        && common::json(&run.join("manifest.json"), 2_000_000)["kind"] != "monthly"
                })
        })
        .and_then(|run| load(&run.join(&old)).ok())
        .map(|model| model_images(&json!({ "app": model })))
        .unwrap_or_default()
}
/// The images of `app` nothing will use again once this update is in: those its
/// update before this one replaced (two updates ago now), and those an update of it that
/// was rolled back had downloaded (its `-new.json`, or the `-quarantine.json` Immich,
/// Nextcloud and the library apps are checked from, in a run without a result).
fn previous_images(media: &Path, current: &str, app: &str) -> std::collections::BTreeSet<String> {
    let mut older = last_replaced(media, current, app);
    for folder in [UPDATE_FOLDER, MONTHLY_FOLDER].map(|f| media.join(f)) {
        for run in runs_in(&folder).unwrap_or_default() {
            let run = folder.join(&run);
            if run.file_name().is_some_and(|f| f == current) || run.join("result.json").exists() {
                continue;
            }
            for suffix in ["-new.json", "-quarantine.json"] {
                if let Ok(model) = load(&run.join(format!("{app}{suffix}"))) {
                    older.extend(model_images(&json!({ "app": model })));
                }
            }
        }
    }
    older
}
/// On the SSD, outside every run folder: the image IDs the monthly update downloaded or
/// replaced. The run folders that also name them can be gone by the time the clean-up
/// looks (with keep_snapshots 1 the run of two updates ago is tidied first; a rolled-back
/// or cut-off run's folder goes once newer snapshots replace it, or at once when nothing
/// was archived), so the clean-up reads them from here as well.
const OLD_IMAGES: &str = "backups/maintenance-old-images.json";
const OLD_IMAGES_FORMAT: &str = "neko-old-images-v1";
/// The IDs recorded in OLD_IMAGES. A missing, damaged or unknown file is "none
/// recorded": only something nothing is known about is left in place.
fn recorded_images(data: &Path) -> std::collections::BTreeSet<String> {
    let saved = common::json(&data.join(OLD_IMAGES), 4_000_000);
    if saved["format"] != OLD_IMAGES_FORMAT {
        return Default::default();
    }
    saved["images"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .filter(|s| image_id(s))
        .map(str::to_string)
        .collect()
}
fn save_recorded_images(data: &Path, ids: &std::collections::BTreeSet<String>) -> Result<()> {
    common::atomic_json(
        &data.join(OLD_IMAGES),
        &json!({"format": OLD_IMAGES_FORMAT, "images": ids}),
    )
}
/// Add `ids` to OLD_IMAGES. Best effort: an image that is not recorded is only left on
/// the SSD (as before), so a failure here never stops an update.
fn remember_images(data: &Path, ids: &[String]) {
    let mut all = recorded_images(data);
    let before = all.len();
    all.extend(ids.iter().filter(|s| image_id(s)).cloned());
    if all.len() != before {
        if let Err(e) = save_recorded_images(data, &all) {
            println!("Monthly update: could not note a downloaded image for the clean-up ({e})");
        }
    }
}
/// What OLD_IMAGES keeps after a clean-up: the images this update replaced (they go at
/// the next update) and every one that could not be removed yet (still in use, or
/// carrying a name of your own), except those Docker no longer has.
fn still_recorded(
    older: &std::collections::BTreeSet<String>,
    removed: &[String],
    replaced: &Value,
    present: &dyn Fn(&str) -> bool,
) -> std::collections::BTreeSet<String> {
    let mut keep: std::collections::BTreeSet<String> = older
        .iter()
        .filter(|id| !removed.contains(id))
        .filter(|id| present(id))
        .cloned()
        .collect();
    keep.extend(model_images(replaced));
    keep
}
/// The repositories NekoServer's apps use ("jellyfin/jellyfin"), from the tags of the
/// images this update installed (each was just downloaded under its app's tag).
fn own_repositories(
    updated: &Value,
    tags: &dyn Fn(&str) -> Option<Vec<String>>,
) -> std::collections::BTreeSet<String> {
    model_images(updated)
        .iter()
        .filter_map(|id| tags(id))
        .flatten()
        .map(|tag| repository(&tag).to_string())
        .collect()
}
/// "ghcr.io/immich-app/immich-server" from "ghcr.io/immich-app/immich-server:v2.1.0"
/// (a port in the registry's name, "host:5000/app:1", is kept).
fn repository(tag: &str) -> &str {
    let tag = tag.split('@').next().unwrap_or(tag);
    match tag.rfind(':') {
        Some(at) if !tag[at..].contains('/') => &tag[..at],
        _ => tag,
    }
}
/// An old image may be removed only when every name it still has is one of
/// NekoServer's own repositories: a name of your own on it (your stack's
/// "postgres:16") keeps it, even when no container uses it right now.
fn only_ours(tags: &[String], own: &std::collections::BTreeSet<String>) -> bool {
    tags.iter().all(|tag| own.contains(repository(tag)))
}
/// The names (RepoTags) an image has, or None when Docker can't say (it is gone).
fn image_tags(id: &str) -> Option<Vec<String>> {
    let said = docker(&["image", "inspect", "--format", "{{json .RepoTags}}", id]).ok()?;
    let tags: Option<Vec<String>> = serde_json::from_str(said.trim()).ok()?;
    Some(tags.unwrap_or_default())
}
/// Those of `older` that nothing uses any more, removed from Docker: the images in use
/// now and the ones this update replaced stay. Docker refuses to remove one a container
/// still uses, which is fine. Images of your own are never touched: only IDs a
/// NekoServer snapshot names. Each pulled update otherwise stays on the SSD for good, a
/// few GB a month. Returns the IDs removed.
fn prune_images(
    older: &std::collections::BTreeSet<String>,
    updated: &Value,
    replaced: &Value,
    remove: &dyn Fn(&str) -> bool,
) -> Vec<String> {
    let mut keep = model_images(updated);
    keep.extend(model_images(replaced));
    older
        .iter()
        .filter(|id| !keep.contains(*id))
        .filter(|id| remove(id))
        .cloned()
        .collect()
}
/// Set by nekoserver-maintenance-retry.service, the hourly retry of an unfinished update
/// (modules/maintenance.py): it never waits for the lock.
const RETRY_ENV: &str = "NEKOSERVER_MAINTENANCE_RETRY";
/// How long the monthly run waits for setup to let go of the shared lock.
const SETUP_WAIT: std::time::Duration = std::time::Duration::from_secs(30 * 60);
/// The monthly timer fires on the 1st only: a run that gave way to setup, or failed,
/// comes back at the same time the next night, through a timer file of its own in
/// /etc/systemd/system. It used to be a one-off `systemd-run` timer, which a restart
/// forgot while the report still promised the retry; an enabled timer file is set up
/// again at every start. Each retry rewrites the same file; one whose night has passed
/// never fires again. Says what was arranged.
fn again_next_night(cfg: &Value) -> String {
    let time = cfg["maintenance"]["time"].as_str().unwrap_or("02:30");
    let Some(day) = next_night(time) else {
        return "it runs again next month".to_string();
    };
    // The service runs with TZ set to the server's time zone, like the monthly timer.
    let zone = std::env::var("TZ").unwrap_or_default();
    let when = format!("{day} {time}:00 {zone}").trim().to_string();
    let systemctl = |args: &[&str]| {
        let mut all = vec!["systemctl"];
        all.extend_from_slice(args);
        common::probe(&all, 60).0 == 0
    };
    if retry_timer(Path::new("/etc/systemd/system"), &when, &systemctl) {
        format!("{TRIES_AGAIN_ON}{when}")
    } else {
        "it could not be set to try again tomorrow night, so it runs next month (or start it in a night yourself: sudo systemctl start nekoserver-maintenance.service)".to_string()
    }
}
const TRIES_AGAIN_ON: &str = "the monthly update tries again on ";
pub(crate) const RETRY_NIGHT_TIMER: &str = "nekoserver-maintenance-next-night.timer";
/// Write the next night's timer (`when`, a systemd calendar time) into `units` and switch
/// it on; `systemctl` runs systemctl with the given arguments.
fn retry_timer(units: &Path, when: &str, systemctl: &dyn Fn(&[&str]) -> bool) -> bool {
    let text = format!(
        "[Unit]\nDescription=NekoServer monthly update, tried again the next night\n[Timer]\nOnCalendar={when}\nAccuracySec=1min\nPersistent=false\nUnit=nekoserver-maintenance.service\n[Install]\nWantedBy=timers.target\n"
    );
    common::atomic(&units.join(RETRY_NIGHT_TIMER), text.as_bytes(), 0o644).is_ok()
        && systemctl(&["daemon-reload"])
        && systemctl(&["enable", RETRY_NIGHT_TIMER])
        && systemctl(&["restart", RETRY_NIGHT_TIMER])
}
/// Tomorrow's date (local time, "YYYY-MM-DD") for a valid "HH:MM", else None.
fn next_night(time: &str) -> Option<String> {
    let now = unsafe { libc::time(std::ptr::null_mut()) };
    next_night_at(time, now, &local_date)
}
/// The calendar date `stamp` falls on in the server's time zone (TZ, which the service
/// runs with, like the monthly timer).
fn local_date(stamp: libc::time_t) -> Option<chrono::NaiveDate> {
    let mut value = std::mem::MaybeUninit::<libc::tm>::uninit();
    if unsafe { libc::localtime_r(&stamp, value.as_mut_ptr()) }.is_null() {
        return None;
    }
    let tm = unsafe { value.assume_init() };
    chrono::NaiveDate::from_ymd_opt(tm.tm_year + 1900, (tm.tm_mon + 1) as u32, tm.tm_mday as u32)
}
/// Tomorrow: the calendar day after the one `now` falls on (`local` gives it), never now
/// plus 24 hours. On the night the clocks go back the day has 25 hours, so from any
/// moment before the repeated hour now plus 24 hours was still today: the retry timer
/// was set for a time already passed (Persistent=false, so it never fired) while the
/// report promised the update would try again.
fn next_night_at(
    time: &str,
    now: libc::time_t,
    local: &dyn Fn(libc::time_t) -> Option<chrono::NaiveDate>,
) -> Option<String> {
    let valid = regex::Regex::new(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$").ok()?;
    if !valid.is_match(time) {
        return None;
    }
    Some(local(now)?.succ_opt()?.format("%Y-%m-%d").to_string())
}
fn hourly_retry() -> bool {
    std::env::var_os(RETRY_ENV).is_some_and(|v| v == "1")
}
/// No unfinished monthly run: no journal, and the last run's report doesn't still say
/// "running" (a run cut off before it wrote its journal is marked failed by recovery).
fn nothing_to_finish(data: &Path) -> bool {
    !Path::new(*JOURNAL).exists()
        && ["maintenance", "updates"].iter().all(|job| {
            load(&data.join(format!("reports/jobs/{job}.json")))
                .map_or(true, |r| r["status"] != "running")
        })
}
/// What the dashboard says about a monthly check or an app update that was cut off (a
/// power cut, a restart) before its update was installed, once the resume service has
/// put things right.
fn cut_off_reason(phase: Option<&str>) -> String {
    let when = match phase {
        Some("snapshot") => "while it was taking the snapshot, with the apps stopped",
        Some("backed_up") => "while it was testing the snapshot",
        Some("updating") => "while it was installing the updates",
        Some(_) => "part-way",
        None => "before it took the snapshot",
    };
    format!(
        "The run was cut off (a power cut or a restart) {when}. The apps were started again with their versions from before the run; it is tried again at its next time"
    )
}
/// The update was installed (the journal says so): finishing it never rolls back.
fn committed() -> bool {
    load(Path::new(*JOURNAL)).is_ok_and(|j| j["phase"] == "committed")
}
#[cfg(test)]
mod tests {
    use super::*;
    /// The review of 0.47.0: the update's check worked the folders out again (the drive,
    /// then a fixed name) instead of reading the ones setup wrote down.
    #[test]
    fn the_updates_check_reads_the_folders_setup_wrote_down() {
        let (data, media) = (Path::new("/srv/n"), Path::new("/mnt/m"));
        let music = |library: &str| {
            json!({"services": {"navidrome": {"image": "x", "networks": {"default": null},
                "ports": [{"host_ip": "127.0.0.1", "target": 4533, "published": "4533"}],
                "volumes": [{"type": "bind", "source": library, "target": "/music", "read_only": true}]}}})
        };
        let written = json!({"paths": {"music": "/mnt/m/songs"}});
        assert!(valid_model(&music("/mnt/m/songs"), "navidrome", data, media, &written).is_ok());
        assert!(valid_model(
            &music("/srv/n/library/music"),
            "navidrome",
            data,
            media,
            &written
        )
        .is_err());
        // Settings from before setup wrote them down: shared.json's own folder.
        assert!(valid_model(
            &music("/srv/n/library/music"),
            "navidrome",
            data,
            media,
            &Value::Null
        )
        .is_ok());
    }
    #[test]
    fn a_snapshots_immich_file_is_checked_with_the_updates_own_rules() {
        let (upload, db) = (
            Path::new("/srv/n/photos/immich-library"),
            Path::new("/srv/n/appdata/immich/postgres"),
        );
        let env = json!({"DB_PASSWORD": "x"});
        let model = json!({"services": {
            "immich-server": {"image": "a", "environment": env, "volumes": [
                {"type": "bind", "source": upload, "target": "/data"},
                {"type": "bind", "source": "/etc/localtime", "target": "/etc/localtime", "read_only": true}]},
            "immich-machine-learning": {"image": "b", "volumes": [{"type": "volume", "source": "model-cache", "target": "/cache"}]},
            "redis": {"image": "c", "privileged": false},
            "database": {"image": "d", "environment": {"POSTGRES_PASSWORD": "x"}, "volumes": [
                {"type": "bind", "source": db, "target": "/var/lib/postgresql/data"}]}}});
        snapshot_ok(&model, upload, db).unwrap();
        let mut changed = model.clone();
        changed["services"]["immich-server"]["devices"] = json!(["/dev/dri:/dev/dri"]);
        assert!(snapshot_ok(&changed, upload, db).is_err()); // a restore test runs without it
        let mut changed = model.clone();
        changed["services"]["immich-server"]["volumes"][1]["read_only"] = json!(false);
        assert!(snapshot_ok(&changed, upload, db).is_err()); // the clock only read-only
        let mut changed = model.clone();
        changed["services"]["database"]["environment"]["POSTGRES_PASSWORD"] = json!("y");
        assert!(snapshot_ok(&changed, upload, db).is_err()); // not its own database
        assert!(snapshot_ok(&model, upload, Path::new("/elsewhere")).is_err());
    }
    #[test]
    fn nothing_that_stops_apps_starts_while_nekoserver_is_upgraded() {
        let source = include_str!("maintenance.rs");
        for start in [
            "pub fn check_updates(",
            "pub fn update_apps(",
            "pub fn run(",
        ] {
            let body = &source[source.find(start).unwrap()..];
            let body = &body[..body.find("\n}\n").unwrap()];
            assert!(body.contains("no_upgrade()?;"), "{start}");
        }
        // The same record upgrade.py keeps, and app restarts wait for it too.
        assert!(include_str!("../../upgrade.py").contains("JOURNAL = RECOVERY['upgrade'][0]"));
        assert!(UPGRADE_JOURNAL.ends_with("/upgrade.json"));
        assert!(include_str!("../../migrate.py")
            .contains("JOURNAL = Path('/etc/nekoserver-backups/migrate.json')"));
        assert!(MIGRATE_JOURNAL.ends_with("/migrate.json"));
        assert!(include_str!("apps.rs").contains("Path::new(*maintenance::UPGRADE_JOURNAL)"));
    }
    /// An update snapshot of `app` at `at`: installed (`done`) or not, as the weekly
    /// update leaves it.
    fn update_run(media: &Path, run: &str, app: &str, at: f64, done: bool, old: Option<char>) {
        use std::os::unix::fs::DirBuilderExt;
        let dir = media.join(UPDATE_FOLDER).join(run);
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&dir)
            .unwrap();
        let mut sums = json!({});
        for key in ["data", "media", "images"] {
            let archive = dir.join(format!("{key}.tar"));
            fs::write(&archive, format!("{run} {key}")).unwrap();
            sums[key] = json!(common::digest(&archive).unwrap());
        }
        common::atomic_json(
            &dir.join("manifest.json"),
            &json!({"format":"neko-maintenance-v1","kind":"update","apps":[app],"created_at":at,"sha256":sums}),
        )
        .unwrap();
        if let Some(c) = old {
            common::atomic_json(
                &dir.join(format!("{app}-old.json")),
                &json!({"services":{app:{"image":format!("sha256:{}", c.to_string().repeat(64))}}}),
            )
            .unwrap();
        }
        if done {
            fs::write(dir.join("result.json"), b"{}").unwrap();
        }
    }
    #[test]
    fn each_app_keeps_its_own_two_newest_update_snapshots() {
        let root = std::env::temp_dir().join(format!("neko-update-runs-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let media = root.join("hdd");
        update_run(&media, "100-1", "jellyfin", 100.0, true, None);
        update_run(&media, "150-1", "jellyfin", 150.0, false, None); // undone
        update_run(&media, "200-1", "jellyfin", 200.0, true, None);
        update_run(&media, "250-1", "immich", 250.0, true, None);
        update_run(&media, "300-1", "jellyfin", 300.0, true, None);
        let report = prune_updates(&root.join("ssd"), &media, "jellyfin", Some("300-1")).unwrap();
        assert_eq!(report["removed"], json!(["100-1", "150-1"]));
        // Another app's snapshot is never touched by this one's retention.
        assert_eq!(update_runs(&media).unwrap(), ["200-1", "250-1", "300-1"]);
        // The Updates page: the newest snapshot holding the app, and its last update.
        let history = app_history(&media, "jellyfin");
        assert_eq!(history["last_snapshot"], json!(300.0));
        assert_eq!(
            history["last_update"],
            json!({"at": 300.0, "result": "installed"})
        );
        update_run(&media, "400-1", "jellyfin", 400.0, false, None);
        assert_eq!(
            app_history(&media, "jellyfin")["last_update"]["result"],
            "undone"
        );
        assert_eq!(
            app_history(&media, "sonarr"),
            json!({"last_snapshot": null, "last_update": null})
        );
        let _ = fs::remove_dir_all(root);
    }
    /// 0.45.8, the owner's review: a downloaded Compose file could give an app the host's
    /// whole filesystem through a named volume defined as a bind, or any device.
    #[test]
    fn a_compose_file_gets_no_host_folder_through_a_volume_and_no_device_but_the_chip() {
        let root = scratch("volume-device");
        let (data, media) = (root.join("srv"), root.join("mnt"));
        let port = json!([{"host_ip": "127.0.0.1", "published": "8096", "target": 8096}]);
        let plain = json!({"services": {"jellyfin": {"image": "sha256:8", "ports": port}}});
        let with = |key: &str, value: Value| {
            let mut m = plain.clone();
            m["services"]["jellyfin"][key] = value;
            m
        };
        let ok = |m: &Value| valid_model(m, "jellyfin", &data, &media, &Value::Null).is_ok();
        // The chip, as NekoServer's own files give it, in both of Compose's shapes.
        assert!(ok(&with("devices", json!(["/dev/dri:/dev/dri"]))));
        assert!(ok(&with(
            "devices",
            json!([{"source": "/dev/dri", "target": "/dev/dri", "permissions": "rwm"}])
        )));
        assert!(ok(&with(
            "devices",
            json!(["/dev/dri/renderD128:/dev/dri/renderD128:rw"])
        )));
        for odd in [
            json!(["/dev/sda:/dev/sda"]),
            json!(["/dev/mem"]),
            json!(["/dev/dri:/dev/other"]),
            json!(["/dev/dri/../sda:/dev/dri/../sda"]),
            json!([{"source": "/dev/kmsg", "target": "/dev/kmsg"}]),
            json!([{"source": "/dev/dri", "target": "/dev/dri", "cgroup": "a"}]),
            json!("/dev/dri:/dev/dri"),
        ] {
            assert!(!ok(&with("devices", odd.clone())), "{odd}");
        }
        assert!(!ok(&with("device_cgroup_rules", json!(["a *:* rwm"]))));
        assert!(!ok(&with("gpus", json!("all"))));
        assert!(!ok(&with("runtime", json!("nvidia"))));
        // The chip only for the apps that use it.
        let mut redis = json!({"services": {}});
        for s in [
            "immich-server",
            "immich-machine-learning",
            "database",
            "redis",
        ] {
            redis["services"][s] = json!({"image": "sha256:8"});
        }
        redis["services"]["redis"]["devices"] = json!(["/dev/dri:/dev/dri"]);
        assert!(!gpu_only("redis", &redis["services"]["redis"]));
        assert!(gpu_only(
            "immich-server",
            &json!({"devices": ["/dev/dri:/dev/dri"]})
        ));
        // Named volumes: Docker's own storage only.
        let named = |def: Value| {
            let mut m = plain.clone();
            m["volumes"] = json!({"cache": def});
            ok(&m)
        };
        assert!(named(json!({"name": "neko-jellyfin_cache"})));
        assert!(named(json!(null)));
        assert!(named(
            json!({"name": "neko-jellyfin_cache", "driver": "local", "driver_opts": {}})
        ));
        for odd in [
            json!({"driver_opts": {"type": "none", "o": "bind", "device": "/"}}),
            json!({"driver": "local", "driver_opts": {"device": "/etc"}}),
            json!({"driver": "sshfs"}),
            json!({"external": true, "name": "someone-elses"}),
            json!("bind"),
            // The 0.46.0 review: any other name was used as it is, also a volume that
            // already exists with other storage or someone else's files in it.
            json!({"name": "x"}),
            json!({"name": "neko-nextcloud_cache"}),
        ] {
            assert!(!named(odd.clone()), "{odd}");
        }
        let mut list = plain.clone();
        list["volumes"] = json!(["x"]);
        assert!(!ok(&list));
        let _ = fs::remove_dir_all(root);
    }
    /// 0.46.1: setup's first Immich install uses this same check (check-compose), so the
    /// rules exist once.
    #[test]
    fn setup_checks_a_compose_model_with_the_updates_rules() {
        let root = scratch("check-compose");
        let cfg = root.join("settings.json");
        fs::write(
            &cfg,
            r#"{"data_mount":"/srv/neko","media_mount":"/mnt/neko"}"#,
        )
        .unwrap();
        let model = root.join("model.json");
        let args = |app: &str| {
            Args(std::collections::BTreeMap::from([
                ("config".to_string(), cfg.to_string_lossy().into_owned()),
                ("app".to_string(), app.to_string()),
                ("model".to_string(), model.to_string_lossy().into_owned()),
            ]))
        };
        let port = json!([{"host_ip": "127.0.0.1", "published": "8096", "target": 8096}]);
        fs::write(
            &model,
            json!({"services": {"jellyfin": {"image": "x", "ports": port}}}).to_string(),
        )
        .unwrap();
        assert!(check_compose(&args("jellyfin")).is_ok());
        fs::write(
            &model,
            json!({"services": {"jellyfin": {"image": "x", "ports": port, "cap_add": ["ALL"]}}})
                .to_string(),
        )
        .unwrap();
        assert!(check_compose(&args("jellyfin")).is_err());
        assert!(check_compose(&args("../etc")).is_err());
        let _ = fs::remove_dir_all(root);
    }
    /// 0.46.0: setup asks where the photos, the music and Nextcloud's files live; the
    /// update's check accepts the folders where they are, and only those.
    #[test]
    fn the_apps_folders_are_checked_where_setup_put_them() {
        let (data, media) = (Path::new("/srv/neko"), Path::new("/mnt/neko"));
        let port = json!([{"host_ip": "127.0.0.1", "published": "4533", "target": 4533}]);
        let navidrome = |source: &Path| {
            json!({"services": {"navidrome": {"image": "sha256:8", "ports": port,
                "volumes": [{"type": "bind", "source": source.join("library/music"), "target": "/music", "read_only": true}]}}})
        };
        let on_media = json!({"music": "media"});
        assert!(valid_model(&navidrome(data), "navidrome", data, media, &Value::Null).is_ok());
        assert!(valid_model(&navidrome(media), "navidrome", data, media, &Value::Null).is_err());
        assert!(valid_model(
            &navidrome(media),
            "navidrome",
            data,
            media,
            &json!({"locations": on_media})
        )
        .is_ok());
        assert!(valid_model(
            &navidrome(data),
            "navidrome",
            data,
            media,
            &json!({"locations": on_media})
        )
        .is_err());
        let at = |locations: Value, what: &str| {
            common::saved_path(
                &json!({"data_mount": data, "media_mount": media, "locations": locations}),
                what,
            )
            .unwrap()
        };
        assert!(at(json!({"cloud": "ssd"}), "cloud").starts_with(data));
        assert!(at(Value::Null, "cloud").starts_with(media));
        assert!(at(Value::Null, "photos").starts_with(data));
        let cfg = json!({"data_mount": "/srv/neko", "media_mount": "/mnt/neko", "locations": {"photos": "media"}});
        assert_eq!(
            common::photo_library(&cfg).unwrap(),
            media.join("photos/immich-library")
        );
        assert!(common::files_root(
            &json!({"data_mount": "/srv/neko", "locations": {"photos": "usb"}}),
            "photos"
        )
        .is_err());
    }
    #[test]
    fn a_model_keeps_to_its_own_networks() {
        // The 0.46.1 audit: the networks were never looked at.
        let port = json!([{"host_ip": "127.0.0.1", "published": "5055", "target": 5055}]);
        let (data, media) = (Path::new("/srv/neko"), Path::new("/mnt/neko"));
        let seerr = |networks: Value, service: Value| {
            let mut m = json!({"services": {"seerr": {"image": "sha256:1", "ports": port}}, "networks": networks});
            for (k, v) in service.as_object().unwrap() {
                m["services"]["seerr"][k] = v.clone();
            }
            valid_model(&m, "seerr", data, media, &Value::Null)
        };
        let shared =
            json!({"neko-media": {"name": "nekoserver-media", "ipam": {}, "external": true}});
        assert!(seerr(shared.clone(), json!({"networks": {"neko-media": null}})).is_ok());
        assert!(seerr(shared.clone(), json!({"networks": ["neko-media"]})).is_ok());
        for (networks, service) in [
            (
                json!({"hostnet": {"external": true, "name": "host"}}),
                json!({"networks": ["hostnet"]}),
            ),
            (
                json!({"default": {"driver": "macvlan", "driver_opts": {"parent": "eth0"}}}),
                json!({}),
            ),
            (
                json!({"x": {"external": true, "name": "neko-nextcloud_default"}}),
                json!({}),
            ),
            (
                json!({"default": {"ipam": {"config": [{"subnet": "192.168.1.0/24"}]}}}),
                json!({}),
            ),
            (
                shared.clone(),
                json!({"networks": {"neko-media": {"aliases": ["sonarr", "prowlarr"]}}}),
            ),
            (
                shared.clone(),
                json!({"container_name": "radarr", "networks": ["neko-media"]}),
            ),
            (shared.clone(), json!({"networks": ["elsewhere"]})),
        ] {
            assert!(
                seerr(networks.clone(), service.clone()).is_err(),
                "{networks} {service}"
            );
        }
        // Immich's own file: its project's default network, its own container names.
        let immich = json!({"default": {"name": "neko-immich_default", "ipam": {}}});
        assert!(networks_ok("immich", &json!({"networks": immich, "services": {"database": {"container_name": "immich_postgres", "networks": {"default": null}}}})).is_ok());
        // Only the download apps see the whole media library.
        let library = |name: &str| {
            let m = json!({"services": {name: {"image": "sha256:1", "ports": port,
                "volumes": [{"type": "bind", "source": "/mnt/neko/library", "target": "/lib"}]}}});
            valid_model(&m, name, data, media, &Value::Null).is_ok()
        };
        assert!(library("radarr") && library("sonarr") && library("qbittorrent"));
        assert!(!library("seerr") && !library("prowlarr") && !library("navidrome"));
        // A bind's propagation could carry mounts out of the container.
        let m = json!({"services": {"seerr": {"image": "sha256:1", "ports": port, "volumes": [
            {"type": "bind", "source": "/srv/neko/appdata/seerr", "target": "/c", "bind": {"propagation": "rshared"}}]}}});
        assert!(valid_model(&m, "seerr", data, media, &Value::Null).is_err());
    }
    #[test]
    fn only_the_volume_compose_made_for_the_app_is_used() {
        let ours = json!({"Driver": "local", "Options": null, "Labels": {
            "com.docker.compose.project": "neko-immich", "com.docker.compose.volume": "model-cache"}});
        assert!(volume_is_ours("immich", "model-cache", &ours));
        let mut empty = ours.clone();
        empty["Options"] = json!({});
        assert!(volume_is_ours("immich", "model-cache", &empty));
        // The 0.46.0 review: a volume made by hand under that name (no labels, or another
        // app's, or binding a folder of this PC) was used as it is.
        let mut by_hand = ours.clone();
        by_hand["Labels"] = Value::Null;
        let mut bound = ours.clone();
        bound["Options"] = json!({"type": "none", "o": "bind", "device": "/root"});
        let mut other = ours.clone();
        other["Labels"]["com.docker.compose.project"] = json!("neko-nextcloud");
        let mut driver = ours.clone();
        driver["Driver"] = json!("sshfs");
        for odd in [by_hand, bound, other, driver] {
            assert!(!volume_is_ours("immich", "model-cache", &odd), "{odd}");
        }
        assert!(!volume_is_ours("immich", "cache", &ours));
    }
    #[test]
    fn a_compose_file_never_gains_more_than_neko_servers_apps_have() {
        // The 0.46.0 review: secrets and configs read from this PC's files are refused
        // with the file's other settings.
        let port = json!([{"host_ip": "127.0.0.1", "published": "8096", "target": 8096}]);
        let (data, media) = (Path::new("/srv/neko"), Path::new("/mnt/neko"));
        for (key, value) in [
            ("secrets", json!({"key": {"file": "/root/.ssh/id_ed25519"}})),
            ("configs", json!({"settings": {"file": "/etc/shadow"}})),
        ] {
            let mut m = json!({"services": {"jellyfin": {"image": "sha256:8", "ports": port}}});
            m[key] = value;
            assert_eq!(
                valid_model(&m, "jellyfin", data, media, &Value::Null)
                    .unwrap_err()
                    .to_string(),
                "Unsupported Compose settings"
            );
        }
        // NekoServer's own apps' setting passes.
        assert!(!widens_rights(
            &json!({"image": "x", "security_opt": ["no-new-privileges:true"]})
        ));
        assert!(!widens_rights(&json!({"image": "x"})));
        for extra in [
            json!({"volumes_from": ["neko-nextcloud-nextcloud-1"]}),
            json!({"cap_add": ["SYS_ADMIN"]}),
            json!({"security_opt": ["apparmor:unconfined"]}),
            json!({"security_opt": "seccomp:unconfined"}),
            json!({"userns_mode": "host"}),
            json!({"cgroup": "host"}),
        ] {
            assert!(widens_rights(&extra), "{extra}");
        }
        // The update's check refuses them: a model that asks for one is not used.
        let port = json!([{"host_ip": "127.0.0.1", "published": "8096", "target": 8096}]);
        let (data, media) = (Path::new("/srv/neko"), Path::new("/mnt/neko"));
        let plain = json!({"services": {"jellyfin": {"image": "sha256:8", "ports": port}}});
        assert!(valid_model(&plain, "jellyfin", data, media, &Value::Null).is_ok());
        let mut wider = plain.clone();
        wider["services"]["jellyfin"]["cap_add"] = json!(["SYS_ADMIN"]);
        assert_eq!(
            valid_model(&wider, "jellyfin", data, media, &Value::Null)
                .unwrap_err()
                .to_string(),
            "Unsupported Compose isolation"
        );
    }
    #[test]
    fn an_updates_media_check_waits_for_the_hourly_one() {
        let media = include_str!("media.rs");
        let at = media
            .find("let lock = Path::new(\"/run/nekoserver-media-health.lock\");")
            .unwrap();
        assert!(media[at..at + 400]
            .contains("if a.flag(\"checking\") {\n        common::lock_wait(lock"));
        // Within the time the update gives the whole check (900 s).
        assert!(media[at..at + 400].contains("from_secs(10 * 60)"));
        assert!(include_str!("maintenance.rs")
            .contains("let (code, _) = common::command(&args, 900)?;"));
    }
    #[test]
    fn a_run_cut_off_after_its_commit_is_recorded_and_noted_installed() {
        let root = std::env::temp_dir().join(format!("neko-committed-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let (data, snapshot) = (root.join("ssd"), root.join("hdd/run"));
        fs::create_dir_all(&snapshot).unwrap();
        let mut pending = crate::updates::pending(&data);
        crate::updates::record(
            &data,
            &mut pending,
            "jellyfin",
            ("abcdef0123456789", "10.11.3"),
            1.0,
            7,
        )
        .unwrap();
        crate::updates::save_pending(&data, &pending).unwrap();
        let journal =
            json!({"kind": "update", "candidate": {"app": "jellyfin", "sig": "abcdef0123456789"}});
        finish_committed(&journal, &snapshot, &data);
        assert!(snapshot.join("result.json").is_file());
        assert!(crate::updates::pending(&data)["apps"]["jellyfin"]
            .as_array()
            .is_none_or(Vec::is_empty));
        // The order at the commit: the phase first, then the record.
        let source = include_str!("maintenance.rs");
        let commit = &source[source.find("Persist successful image identities").unwrap()..];
        assert!(
            commit.find("set_phase(\"committed\")?;").unwrap()
                < commit.find("\"result.json\"").unwrap()
        );
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn apps_are_pinned_by_image_id_never_by_tag() {
        let root = std::env::temp_dir().join(format!("neko-pins-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("compose/jellyfin")).unwrap();
        fs::create_dir_all(root.join("compose/immich")).unwrap();
        let id = format!("sha256:{}", "a".repeat(64));
        let tags = json!({"jellyfin": {"services": {"jellyfin": {"image": "jellyfin/jellyfin:latest"}}},
            "immich": {"services": {"immich-server": {"image": "ghcr.io/immich-app/immich-server:v2"}}}});
        // An old pin by tag (as 0.42.0 to 0.45.6 wrote) is made again from what runs.
        common::atomic_json(
            &root.join("compose/jellyfin/maintenance-images.json"),
            &json!({"services": {"jellyfin": {"image": "jellyfin/jellyfin:latest"}}}),
        )
        .unwrap();
        let running = |name: &str, model: &mut Value| -> Result<()> {
            if name == "immich" {
                return Err(Error::new("not running"));
            }
            for v in model["services"].as_object_mut().unwrap().values_mut() {
                v["image"] = json!(format!("sha256:{}", "a".repeat(64)));
            }
            Ok(())
        };
        let pinned = pin_unpinned_with(
            &root,
            &["jellyfin".into(), "immich".into()],
            &tags,
            &running,
        )
        .unwrap();
        assert_eq!(pinned, ["jellyfin"]);
        let pins = common::json(
            &root.join("compose/jellyfin/maintenance-images.json"),
            64_000,
        );
        assert_eq!(pins["services"]["jellyfin"]["image"], json!(id));
        assert!(pinned_by_id(&root, "jellyfin"));
        // Not running: no pin by tag is written, and its new version is not downloaded.
        assert!(!root.join("compose/immich/maintenance-images.json").exists());
        assert!(!pinned_by_id(&root, "immich"));
        let source = include_str!("maintenance.rs");
        let check = &source[source.find("fn look_for_updates(").unwrap()..];
        assert!(
            check.find("if !pinned_by_id(data, name)").unwrap()
                < check.find("download_updates(cfg").unwrap()
        );
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn a_monthly_check_that_runs_past_the_night_is_finished_not_repeated() {
        let source = include_str!("maintenance.rs");
        let body = &source[source.find("fn transaction(").unwrap()..];
        let body = &body[..body.find("\nfn finish(").unwrap()];
        let late = body
            .find("\"Night window ended; backups complete, updates deferred\"")
            .unwrap();
        assert!(body[..late]
            .trim_end()
            .ends_with("if !audit && !in_window() {\n        return Err(Error::new("));
    }
    #[test]
    fn an_update_takes_the_usb_lock_before_the_backups_lock() {
        let source = include_str!("maintenance.rs");
        let body = &source[source.find("pub fn update_apps(").unwrap()..];
        let body = &body[..body.find("\n}\n").unwrap()];
        assert!(body.contains("both_locks(") && body.contains("backup::hold_usb,"));
        // Never one held while waiting for the other: a stand-in where the backups' lock
        // is busy at first lets the USB lock go before it waits.
        use std::cell::RefCell;
        let events = RefCell::new(vec![]);
        let busy = std::cell::Cell::new(1);
        struct Held<'a>(&'a RefCell<Vec<&'static str>>, &'static str);
        impl Drop for Held<'_> {
            fn drop(&mut self) {
                self.0.borrow_mut().push(self.1);
            }
        }
        let got = both_locks(
            Path::new("/x"),
            || {
                events.borrow_mut().push("usb");
                Ok(Held(&events, "usb released"))
            },
            || {
                if busy.get() > 0 {
                    busy.set(busy.get() - 1);
                    Err(Error::new("busy"))
                } else {
                    events.borrow_mut().push("backups");
                    Ok(Held(&events, "backups released"))
                }
            },
            || {
                events.borrow_mut().push("waited for backups");
                Ok(Held(&events, "backups released"))
            },
        )
        .unwrap();
        drop(got);
        assert_eq!(
            events.borrow()[..5],
            [
                "usb",
                "usb released",
                "waited for backups",
                "backups released",
                "usb"
            ]
        );
        // And the transaction never waits for that lock again (it would wait on itself).
        let run = &source[source.find("fn transaction(").unwrap()..];
        let run = &run[..run.find("\n}\n").unwrap()];
        assert!(run.contains("if audit {\n        common::stage(\"waiting for a weekly USB copy"));
        // Seen on a VM: "failed while checking free space: Live automated media connections
        // are not ready". The connection check says its own stage.
        let check = &source[source.find("fn integrations(").unwrap()..];
        assert!(check[..check.find("if cfg[").unwrap()]
            .contains("common::stage(\"checking the connections between the media apps\")"));
        // The weekly copy takes the USB lock first, and the backups' one only later.
        let backup = include_str!("backup.rs");
        let copy = &backup[backup.find("fn usb_copy(").unwrap()..];
        assert!(
            copy.find("Path::new(USB_LOCK)").unwrap() < copy.find("copy_to_usb(&run)").unwrap()
        );
    }
    #[test]
    fn the_daily_check_downloads_only_with_room_and_clears_what_it_no_longer_needs() {
        const GB: u64 = 1 << 30;
        let cfg = json!({"maintenance": {"minimum_free_gb": 20}});
        let images = |_: &[String]| -> Result<u64> { Ok(3 * GB) };
        let data = Path::new("/srv/nekoserver");
        // 500 GB SSD: 20 GB reserve + 3 GB of images.
        assert!(
            no_download_room(&cfg, data, "jellyfin", Ok((24 * GB, 500 * GB)), images).is_none()
        );
        let why =
            no_download_room(&cfg, data, "jellyfin", Ok((22 * GB, 500 * GB)), images).unwrap();
        assert!(why.contains("not downloaded"), "{why}");
        assert!(no_download_room(&cfg, data, "jellyfin", Err(Error::new("x")), images).is_some());
        // The check guards each download, and clears the images left over afterwards.
        let source = include_str!("maintenance.rs");
        let check = &source[source.find("fn look_for_updates(").unwrap()..];
        let check = &check[..check.find("\n}\n").unwrap()];
        assert!(
            check.find("no_download_room(").unwrap() < check.find("download_updates(").unwrap()
        );
        assert!(check.contains("clean_downloads(data, media, &models, &names)"));
        // An update's clean-up keeps noting the images still waiting, for that clean-up.
        let clean = &source[source.find("fn clean_images(").unwrap()..];
        assert!(clean[..clean.find("\n}\n").unwrap()].contains("recorded.extend(waiting);"));
    }
    #[test]
    fn failed_updates_do_not_pile_up_and_a_damaged_snapshot_keeps_the_older_ones() {
        let root = std::env::temp_dir().join(format!("neko-update-fails-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let (ssd, media) = (root.join("ssd"), root.join("hdd"));
        let failed = |run: &str| ssd.join(format!("appdata/jellyfin.neko-failed-{run}"));
        update_run(&media, "100-1", "jellyfin", 100.0, true, None);
        // Three weeks of updates that were undone, each leaving its failed app data.
        for run in ["200-1", "300-1", "400-1"] {
            update_run(&media, run, "jellyfin", 200.0, false, None);
            fs::create_dir_all(failed(run).join("db")).unwrap();
        }
        let report = prune_updates(&ssd, &media, "jellyfin", None).unwrap();
        // Only the newest undone one stays (to look into), with its failed app data.
        assert_eq!(update_runs(&media).unwrap(), ["100-1", "400-1"]);
        assert!(!failed("200-1").exists() && !failed("300-1").exists());
        assert!(failed("400-1").exists());
        assert!(report["removed"]
            .to_string()
            .contains("jellyfin.neko-failed-200-1"));
        // An update that does not fit can free that one too (never a complete one).
        let mut retention = json!({"removed": []});
        make_update_room(&ssd, &media, "jellyfin", &mut retention).unwrap();
        assert_eq!(update_runs(&media).unwrap(), ["100-1"]);
        assert!(!failed("400-1").exists());
        make_update_room(&ssd, &media, "jellyfin", &mut retention).unwrap();
        assert_eq!(update_runs(&media).unwrap(), ["100-1"]);
        // Old complete ones go only while the newer ones kept pass their checksum check.
        update_run(&media, "500-1", "jellyfin", 500.0, true, None);
        update_run(&media, "600-1", "jellyfin", 600.0, true, None);
        fs::write(media.join(UPDATE_FOLDER).join("600-1/data.tar"), b"bit rot").unwrap();
        let report = prune_updates(&ssd, &media, "jellyfin", None).unwrap();
        assert_eq!(report["held_back"], json!(["600-1"]));
        assert_eq!(update_runs(&media).unwrap(), ["100-1", "500-1", "600-1"]);
        update_run(&media, "600-1", "jellyfin", 600.0, true, None); // intact again
        prune_updates(&ssd, &media, "jellyfin", None).unwrap();
        assert_eq!(update_runs(&media).unwrap(), ["500-1", "600-1"]);
        // A weekly update does not run the restore test, and its record says so.
        let source = include_str!("maintenance.rs");
        assert!(source.contains(r#""restore_test":if audit {"pass"} else {"not_run"}"#));
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn an_update_clears_only_its_own_apps_old_images() {
        let root = std::env::temp_dir().join(format!("neko-update-images-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let media = root.join("hdd");
        let id = |c: char| format!("sha256:{}", c.to_string().repeat(64));
        // Jellyfin's last update replaced "a"; Immich's replaced "b" (its way back).
        update_run(&media, "100-1", "jellyfin", 100.0, true, Some('a'));
        update_run(&media, "200-1", "immich", 200.0, true, Some('b'));
        let older = previous_images(&media, "300-1", "jellyfin");
        assert_eq!(older, [id('a')].into_iter().collect());
        assert_eq!(
            last_replaced(&media, "300-1", "immich"),
            [id('b')].into_iter().collect()
        );
        // A monthly check (0.42.0 on) updated nothing: it is never "the last update".
        use std::os::unix::fs::DirBuilderExt;
        let audit = media.join(MONTHLY_FOLDER).join("250-1");
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&audit)
            .unwrap();
        fs::write(audit.join("result.json"), b"{}").unwrap();
        fs::write(audit.join("manifest.json"), br#"{"kind":"monthly"}"#).unwrap();
        common::atomic_json(
            &audit.join("sonarr-old.json"),
            &json!({"services":{"sonarr":{"image":id('c')}}}),
        )
        .unwrap();
        assert!(last_replaced(&media, "300-1", "sonarr").is_empty());
        // The update itself keeps every other app's way back and what waits to install.
        let source = include_str!("maintenance.rs");
        let commit = &source[source.find("fn clean_images(").unwrap()..];
        let commit = &commit[..commit.find("let own = ").unwrap()];
        assert!(commit.contains("crate::updates::waiting_images(data)"));
        assert!(commit.contains("last_replaced(media, run, other)"));
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn the_monthly_check_updates_nothing_and_an_update_skips_the_restore_test() {
        let source = include_str!("maintenance.rs");
        let body = &source[source.find("fn transaction(").unwrap()..];
        let body = &body[..body.find("\nfn finish(").unwrap()];
        // The monthly check starts the apps again as they were.
        assert!(body.contains("Plan::Audit => (models.clone(), None, None),"));
        // The restore test and the full checksum check are the monthly check's.
        let test = body.find("rehearsal(&snapshot").unwrap();
        assert!(body[..test]
            .trim_end()
            .ends_with("common::stage(\"testing the snapshot restores\");"));
        assert!(body[body
            .find("if audit {\n        common::stage(\"testing")
            .unwrap()..]
            .starts_with("if audit {"));
        assert!(body.contains("checks_first(cfgpath, cfg, audit, data_root)?"));
        assert!(source.contains("    if !audit {\n        return Ok((vec![], false));\n    }"));
        // An update signs in before and after.
        assert!(body.find("signs_in(data, &c.app)").unwrap() < body.find("stop(&rows)").unwrap());
        assert!(body.contains("signed_in_again(data, &names[0])?;"));
        // And is snapshotted on its own, with its own retention, never the monthly one's.
        let room = &source[source.find("fn room(").unwrap()..];
        assert!(room.contains("prune_updates(data, media, &names[0], None)?"));
        assert!(room.contains("&& audit {\n            have = make_room("));
    }
    #[test]
    fn the_next_night_retry_is_kept_across_a_restart() {
        // A timer file of its own, switched on (enabled): a restart before the next night
        // keeps it, where the one-off systemd-run timer was forgotten.
        let units = std::env::temp_dir().join(format!("neko-retry-{}", std::process::id()));
        fs::create_dir_all(&units).unwrap();
        let ran = std::cell::RefCell::new(vec![]);
        let systemctl = |args: &[&str]| {
            ran.borrow_mut().push(args.join(" "));
            true
        };
        assert!(retry_timer(
            &units,
            "2026-11-03 02:30:00 Europe/Lisbon",
            &systemctl
        ));
        let text = fs::read_to_string(units.join(RETRY_NIGHT_TIMER)).unwrap();
        assert!(
            text.contains("OnCalendar=2026-11-03 02:30:00 Europe/Lisbon\n"),
            "{text}"
        );
        assert!(text.contains("Unit=nekoserver-maintenance.service\n"));
        assert!(text.contains("WantedBy=timers.target\n"));
        assert_eq!(
            *ran.borrow(),
            [
                "daemon-reload".to_string(),
                format!("enable {RETRY_NIGHT_TIMER}"),
                format!("restart {RETRY_NIGHT_TIMER}")
            ]
        );
        // systemd refusing it is said (the report then promises no retry).
        assert!(!retry_timer(&units, "x", &|_: &[&str]| false));
        let source = include_str!("maintenance.rs");
        let body = &source[source.find("fn again_next_night(").unwrap()..];
        assert!(!body[..body.find("\n}\n").unwrap()].contains("systemd-run"));
        // The report has the time for programs too.
        assert!(source.contains("note(\"next_attempt\", json!(next))"));
        let _ = fs::remove_dir_all(units);
    }
    #[test]
    fn the_monthly_check_covers_the_copies_that_are_set_up() {
        let cfgpath = Path::new("/etc/nekoserver-backups/maintenance.json");
        let both = json!({"data_mount":"/srv/nekoserver","media_mount":"/mnt/m","external_backup_mount":"/mnt/usb",
            "photos":{"deploy_immich":true,"external_backup_uuid":"cccc-3333","external_backup_fstype":"exfat"},
            "nextcloud":{"enabled":true}});
        let (archive, usb) = copies_to_check(&both, cfgpath);
        assert_eq!(archive.as_deref(), Some("photos/archive"));
        let usb = usb.unwrap();
        assert_eq!(usb.get("source").unwrap(), "/mnt/m/photos/archive");
        assert_eq!(usb.get("cloud").unwrap(), "/mnt/m/cloud/files");
        assert_eq!(usb.get("fstype").unwrap(), "exfat");
        assert_eq!(usb.get("config").unwrap(), cfgpath.to_str().unwrap());
        // Nextcloud only: no archive, and the USB copy without photos.
        let cloud = json!({"media_mount":"/mnt/m","photos":{"deploy_immich":false,"external_backup_uuid":"cccc-3333"},
            "nextcloud":{"enabled":true}});
        let (archive, usb) = copies_to_check(&cloud, cfgpath);
        assert!(archive.is_none());
        assert!(!usb.unwrap().flag("source"));
        // No USB drive named, or nothing to copy to it.
        let none = json!({"media_mount":"/mnt/m","photos":{"deploy_immich":true}});
        assert!(copies_to_check(&none, cfgpath).1.is_none());
        let off = json!({"media_mount":"/mnt/m","photos":{"external_backup_uuid":"cccc-3333"}});
        let (archive, usb) = copies_to_check(&off, cfgpath);
        assert!(archive.is_none() && usb.is_none());
    }
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
    /// The rollback of an app whose settings are on another drive (0.50.0): its usual
    /// place is a mount and cannot be renamed, and the snapshot's copy is unpacked on the
    /// app drive, which a rename cannot leave. The copy is taken to the app's drive first
    /// and the swap is made there; then the mount is made again. Cut off anywhere, the
    /// same call goes on from where it stopped.
    #[test]
    fn a_rollback_puts_an_app_on_another_drive_back_on_that_drive() {
        let root = scratch("placed-rollback");
        let (data, base) = (root.join("ssd"), root.join("d3/nekoserver"));
        let stage = data.join("backups/rollback-200-1");
        let home = base.join("appdata/jellyfin");
        for (folder, name, body) in [
            (&home, "library.db", "after the failed update"),
            (
                &stage.join("appdata/jellyfin"),
                "library.db",
                "as the snapshot has it",
            ),
            (&data.join("compose/jellyfin"), "compose.yaml", "new"),
            (&stage.join("compose/jellyfin"), "compose.yaml", "old"),
        ] {
            fs::create_dir_all(folder).unwrap();
            fs::write(folder.join(name), body).unwrap();
        }
        let remounted = std::cell::RefCell::new(vec![]);
        let shown = |place: &Path, home: &Path| {
            remounted
                .borrow_mut()
                .push((place.to_path_buf(), home.to_path_buf()));
            Ok(())
        };
        let read = |path: PathBuf| fs::read_to_string(path).unwrap();
        // Cut off once the copy is on the app's drive: nothing is swapped yet.
        stage_on_drive(
            &stage.join("appdata/jellyfin"),
            &base.join("rollback-200-1/appdata/jellyfin"),
        )
        .unwrap();
        assert!(!stage.join("appdata/jellyfin").exists());
        assert_eq!(read(home.join("library.db")), "after the failed update");
        for _ in 0..2 {
            // ... and run again, twice: the second time finds it done and changes nothing.
            put_back(
                &data,
                &stage,
                "appdata/jellyfin",
                "200-1",
                Some(&base),
                &shown,
            )
            .unwrap();
            assert_eq!(read(home.join("library.db")), "as the snapshot has it");
            assert_eq!(
                read(base.join("appdata/jellyfin.neko-failed-200-1/library.db")),
                "after the failed update"
            );
        }
        // The app's usual place is shown the folder put back, both times.
        assert_eq!(
            *remounted.borrow(),
            vec![(data.join("appdata/jellyfin"), home.clone()); 2]
        );
        // Nothing of it was put on the app drive, where the closed folder under the mount is.
        assert!(!data.join("appdata/jellyfin.neko-failed-200-1").exists());
        // Its Compose file stays on the app drive and is put back there as always.
        put_back(&data, &stage, "compose/jellyfin", "200-1", None, &shown).unwrap();
        assert_eq!(read(data.join("compose/jellyfin/compose.yaml")), "old");
        assert_eq!(remounted.borrow().len(), 2);
        // A copy cut off half-way is made again from the start, never used as it is.
        let again = stage.join("appdata/seerr");
        fs::create_dir_all(&again).unwrap();
        fs::write(again.join("settings.json"), "whole").unwrap();
        let on_drive = base.join("rollback-200-1/appdata/seerr");
        fs::create_dir_all(on_drive.with_file_name("seerr.partial")).unwrap();
        fs::write(on_drive.with_file_name("seerr.partial").join("half"), "x").unwrap();
        stage_on_drive(&again, &on_drive).unwrap();
        assert_eq!(read(on_drive.join("settings.json")), "whole");
        assert!(
            !on_drive.join("half").exists() && !on_drive.with_file_name("seerr.partial").exists()
        );
        // Which paths: an app's settings folder only, for an app the settings place elsewhere.
        let cfg = json!({"storage": {"d3": {"uuid": "u"}}, "installed": {"jellyfin": "d3"}});
        assert_eq!(
            placed_base(&cfg, "appdata/jellyfin").unwrap(),
            Some(PathBuf::from("/mnt/nekoserver-drives/d3/nekoserver"))
        );
        for elsewhere in [
            "compose/jellyfin",
            "appdata/seerr",
            "appdata/jellyfin/config",
            "secrets/accounts",
        ] {
            assert_eq!(placed_base(&cfg, elsewhere).unwrap(), None, "{elsewhere}");
        }
        let _ = fs::remove_dir_all(root);
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
        if Path::new(*JOURNAL).exists() {
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
    fn the_reserve_is_at_most_a_fifth_of_the_drive() {
        let gib = 1u64 << 30;
        // A 24 GB SSD (setup's minimum, about): the 20 GB default would stop every run.
        assert_eq!(reserve_for(20 * gib, 24 * gib), 24 * gib / 5);
        // A big drive keeps the whole setting.
        assert_eq!(reserve_for(20 * gib, 500 * gib), 20 * gib);
        assert_eq!(reserve_for(2 * gib, 0), 0);
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
    fn monthly_retention_removes_unfinished_runs_like_complete_ones_of_their_age() {
        let root = scratch("safe");
        snapshot(&root, "hdd", "100-1", true);
        snapshot(&root, "hdd", "150-1", false); // cut off, older than the kept run: goes
        snapshot(&root, "hdd", "200-1", true);
        snapshot(&root, "hdd", "300-1", true);
        snapshot(&root, "hdd", "350-1", false); // newer than every kept run: stays
        let report = prune_monthly(&root.join("ssd"), &root.join("hdd"), 1, Some("200-1")).unwrap();
        assert_eq!(runs(&root, "hdd"), ["200-1", "300-1", "350-1"]);
        assert_eq!(report["incomplete_not_removed"], json!(["350-1"]));
        assert!(report["removed"]
            .as_array()
            .unwrap()
            .contains(&json!({"run":"150-1","drive":"hdd","unfinished":true})));
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn unfinished_runs_stay_while_fewer_complete_runs_than_kept_or_when_not_ours() {
        let root = scratch("unfinished");
        snapshot(&root, "hdd", "150-1", false);
        snapshot(&root, "hdd", "200-1", true);
        snapshot(&root, "hdd", "300-1", true);
        // Only two complete runs of the three kept: the unfinished one may still matter.
        prune_monthly(&root.join("ssd"), &root.join("hdd"), 3, None).unwrap();
        assert_eq!(runs(&root, "hdd"), ["150-1", "200-1", "300-1"]);
        // The current run, and a folder that is not the worker's own, are never removed.
        prune_monthly(&root.join("ssd"), &root.join("hdd"), 1, Some("150-1")).unwrap();
        assert_eq!(runs(&root, "hdd"), ["150-1", "300-1"]);
        let foreign = root.join("hdd/backups/monthly/150-1");
        fs::set_permissions(
            &foreign,
            <fs::Permissions as std::os::unix::fs::PermissionsExt>::from_mode(0o755),
        )
        .unwrap();
        let report = prune_monthly(&root.join("ssd"), &root.join("hdd"), 1, None).unwrap();
        assert_eq!(runs(&root, "hdd"), ["150-1", "300-1"]);
        assert_eq!(report["incomplete_not_removed"], json!(["150-1"]));
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn a_monthly_run_dated_ahead_counts_as_the_oldest() {
        let root = scratch("ahead");
        for run in ["100-1", "99999999999999999-1", "200-1"] {
            snapshot(&root, "hdd", run, true);
        }
        assert_eq!(
            runs(&root, "hdd"),
            ["99999999999999999-1", "100-1", "200-1"]
        );
        // Keep 2: the run made while the clock was far ahead goes first.
        prune_monthly(&root.join("ssd"), &root.join("hdd"), 2, None).unwrap();
        assert_eq!(runs(&root, "hdd"), ["100-1", "200-1"]);
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
        assert!(note.contains("delete the ones you don't need"), "{note}");
    }
    #[test]
    fn unfinished_runs_make_room_on_a_full_hdd_but_complete_ones_stay() {
        let root = scratch("room");
        snapshot(&root, "hdd", "150-1", false);
        snapshot(&root, "hdd", "200-1", true);
        snapshot(&root, "hdd", "250-1", false);
        let mut retention = prune_monthly(&root.join("ssd"), &root.join("hdd"), 3, None).unwrap();
        assert_eq!(
            retention["incomplete_not_removed"],
            json!(["150-1", "250-1"])
        );
        // Room enough: nothing goes.
        make_room(&root.join("hdd"), &mut retention, 0).unwrap();
        assert_eq!(runs(&root, "hdd"), ["150-1", "200-1", "250-1"]);
        // Never room enough: every unfinished run goes, the complete one stays.
        make_room(&root.join("hdd"), &mut retention, u64::MAX).unwrap();
        assert_eq!(runs(&root, "hdd"), ["200-1"]);
        assert_eq!(retention["incomplete_not_removed"], json!([]));
        let removed = retention["removed"].as_array().unwrap();
        assert_eq!(removed.len(), 2);
        assert_eq!(removed[0]["for_room"], json!(true));
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
    fn a_rollback_puts_back_only_what_its_run_saved() {
        // An update of Jellyfin alone, on a server with Immich and Nextcloud too.
        let manifest = json!({"paths": {"data": ["appdata/jellyfin", "compose/jellyfin", "secrets/accounts"], "media": []}});
        let (data, media) = rollback_paths(&["jellyfin".to_string()], &manifest);
        assert_eq!(
            data,
            ["appdata/jellyfin", "compose/jellyfin", "secrets/accounts"]
        );
        assert!(media.is_empty());
        // Never another app's folders, even if a manifest named them, nor one it lacks.
        let wider = json!({"paths": {"data": ["appdata/jellyfin", "appdata/immich", "../etc"], "media": ["x"]}});
        let (data, media) = rollback_paths(&["jellyfin".to_string()], &wider);
        assert_eq!(data, ["appdata/jellyfin"]);
        assert!(media.is_empty());
        // The source: the journal's apps and the manifest, not every app set up.
        let source = include_str!("maintenance.rs");
        let recovery = &source[source.find("fn resume(cfgpath").unwrap()..];
        let recovery = &recovery[..recovery.find("\n}\n").unwrap()];
        assert!(recovery.contains("rollback_paths(&apps, &snapshot_manifest)"));
        assert!(!recovery.contains("roots(&names(cfg)?)"));
    }
    #[test]
    fn update_now_names_the_app_and_the_version() {
        let apps = vec!["jellyfin".to_string()];
        assert_eq!(update_now_target("", &apps).unwrap(), None);
        assert_eq!(
            update_now_target("jellyfin:0123abcd", &apps).unwrap(),
            Some(("jellyfin".into(), "0123abcd".into()))
        );
        // Without the version (an older unit): refused, never "the newest found".
        assert!(update_now_target("jellyfin", &apps).is_err());
        assert!(update_now_target("immich:0123abcd", &apps).is_err());
        assert!(update_now_target("jellyfin:../../x", &apps).is_err());
    }
    #[test]
    fn a_cached_immich_major_needs_its_approval_still_when_installed() {
        let root = std::env::temp_dir().join(format!("neko-immich-revoke-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let data = root.join("ssd");
        fs::create_dir_all(data.join("compose/immich")).unwrap();
        fs::write(
            data.join("compose/immich/.env"),
            "IMMICH_VERSION=v2.5.0\nDB_PASSWORD=x\n",
        )
        .unwrap();
        let folder = crate::updates::candidate_folder(&data, "immich", "s1");
        fs::create_dir_all(&folder).unwrap();
        fs::write(folder.join("compose.yaml"), "services: {}\n").unwrap();
        fs::write(
            folder.join("candidate.json"),
            json!({"format": "neko-update-candidate-v1", "app": "immich", "images": {}, "immich_release": "v3.0.1"}).to_string(),
        )
        .unwrap();
        let found = json!({"sig": "s1", "version": "v3.0.1"});
        let cfg = |wanted: &str| json!({"media_mount": "/mnt/m", "maintenance": {"immich_major_update": wanted}});
        // The approval was taken back after the check downloaded it: not installed.
        let revoked = load_candidate(&cfg(""), &data, "immich", &found, &root.join("work"));
        assert!(matches!(revoked, Ok(None)));
        // Still approved: it goes on to be loaded (this fake one then fails its model).
        let approved = load_candidate(&cfg("v3"), &data, "immich", &found, &root.join("work"));
        assert!(!matches!(approved, Ok(None)));
        // The same major needs no approval.
        assert!(immich_release_allowed(
            &cfg(""),
            "IMMICH_VERSION=v2.5.0",
            "v2.6.1"
        ));
        assert!(!immich_release_allowed(
            &cfg(""),
            "IMMICH_VERSION=v2.5.0",
            "v3.0.1"
        ));
        assert!(!immich_release_allowed(
            &cfg("v3"),
            "IMMICH_VERSION=v2.5.0",
            "nonsense"
        ));
        fs::remove_dir_all(root).unwrap();
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
        // Exact tags setup accepts move too; another major's tag stays.
        assert_eq!(
            retag_nextcloud(
                "\"nextcloud:33.0.10-apache\" \"nextcloud:33.1-apache\" \"nextcloud:330-apache\"",
                33,
                34
            ),
            "\"nextcloud:34-apache\" \"nextcloud:34-apache\" \"nextcloud:330-apache\""
        );
        assert_eq!(nextcloud_tag_major("nextcloud:33.0.10-apache"), Some(33));
        assert_eq!(nextcloud_tag_major("nextcloud:33-apache"), Some(33));
        assert_eq!(nextcloud_tag_major("nextcloud:33.0.10"), None);
        let mut exact = json!({"services":{"nextcloud":{"image":"nextcloud:33.0.10-apache"},
            "cron":{"image":"nextcloud:33.0.10-apache"}}});
        step_nextcloud_images(&mut exact, 33, 34).unwrap();
        assert_eq!(
            exact["services"]["nextcloud"]["image"],
            "nextcloud:34-apache"
        );
        assert_eq!(exact["services"]["cron"]["image"], "nextcloud:34-apache");
        let mut odd = json!({"services":{"nextcloud":{"image":"nextcloud:33-apache"},
            "cron":{"image":"example/nextcloud-cron:1"}}});
        assert!(step_nextcloud_images(&mut odd, 33, 34).is_err());
        assert!(reached_major(
            "{\"installed\":true,\"versionstring\":\"34.0.1\"}\n",
            34
        ));
        assert!(!reached_major("{\"versionstring\":\"33.0.10\"}", 34));
        assert!(!reached_major("not json", 34));
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
            Path::new("/mnt/neko"),
            &Value::Null
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
        assert!(cut_off_reason(Some("updating")).contains("it is tried again at its next time"));
    }
    /// A failed run is tried again the next night, three nights running, then left to
    /// next month: the count carries over through the report of the failed run only
    /// while that run was set to try again.
    #[test]
    fn a_failed_run_is_tried_again_a_few_nights_before_next_month() {
        assert_eq!(retries_made(&json!({"status": "pass"})), 0);
        assert_eq!(
            retries_made(&json!({"status": "fail"})),
            0,
            "a first failure"
        );
        assert_eq!(
            retries_made(
                &json!({"status": "fail", "retries": 2, "retry_at": "the monthly update tries again on 2026-11-03 02:30:00"})
            ),
            2
        );
        assert_eq!(
            retries_made(&json!({"status": "partial", "retries": 2, "retry_at": "x"})),
            0,
            "a run that got through in part starts the count anew"
        );
        let source = include_str!("maintenance.rs");
        let run = &source[source.find("pub fn run(a: &Args, recover: bool)").unwrap()..];
        let run = &run[..run.find("\n}\n").unwrap()];
        assert!(
            run.contains("tried < RETRY_NIGHTS => {") && run.contains("again_next_night(&cfg)")
        );
        assert!(
            run.contains("!common::postponed(&failure)"),
            "a postponed run already has its night"
        );
    }
    /// Nextcloud answering but unable to write its test file is no migration: the health
    /// check gives up after a short grace, not after the hour a migration may take.
    #[test]
    fn a_test_file_nextcloud_cannot_write_ends_the_wait_early() {
        let probe = format!("{PROBE_FAILED}docker exited with 255 (thrown in /var/www/html/lib/private/Files/Node/Folder.php on line 180)");
        assert!(!probe_gave_up(&probe, PROBE_GRACE - 1));
        assert!(probe_gave_up(&probe, PROBE_GRACE));
        assert!(
            !probe_gave_up("Container not healthy", MIGRATION_WAIT),
            "not the test file"
        );
        let said = probe_advice(&probe, 130);
        assert!(said.starts_with("Nextcloud could not write a test file after 130 seconds, though Nextcloud answers (docker exited with 255"), "{said}");
        assert!(said.contains("sudo bash install.sh"));
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
        // Strictly isolated: an internal network only, with no address of this PC on it
        // (QA-03: the bridge's gateway address reached this PC's own services), and no
        // port on this PC.
        assert_eq!(
            clone["networks"],
            json!({"restore": {"internal": true, "driver_opts": {"com.docker.network.bridge.inhibit_ipv4": "true"}}})
        );
        let app = &clone["services"]["jellyfin"];
        assert_eq!(app["networks"], json!(["restore"]));
        assert!(app.get("container_name").is_none() && app.get("ports").is_none());
        // Its health check asks the app from inside that network.
        let source = include_str!("maintenance.rs");
        let check = &source[source.find(&["fn ", "health("].concat()).unwrap()..];
        let check = &check[..check.find("\n}\n").unwrap()];
        assert!(check.contains("if rehearsal {") && check.contains("curl_inside(\n"));
        assert!(check.contains("&container_pid(file, project, service)?"));
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
    #[test]
    fn only_known_apps_are_selected_and_each_once() {
        let cfg = |services: Value| json!({"media": {"services": services}});
        assert_eq!(
            names(&cfg(json!(["jellyfin", "sonarr"]))).unwrap(),
            ["jellyfin", "sonarr"]
        );
        for bad in [
            json!(["jellyfin", "jellyfin"]),
            json!(["plex"]),
            json!([7]),
            json!(["immich"]),
        ] {
            let error = names(&cfg(bad.clone())).unwrap_err().to_string();
            assert_eq!(error, "Invalid media selection", "{bad}");
        }
        assert_eq!(
            names(&json!({})).unwrap_err().to_string(),
            "No maintenance applications selected"
        );
        let all = json!({"media": {"services": ["navidrome"]}, "photos": {"deploy_immich": true}, "nextcloud": {"enabled": true}});
        assert_eq!(names(&all).unwrap(), ["navidrome", "immich", "nextcloud"]);
        let flags = json!({"photos": {"deploy_immich": "yes"}, "nextcloud": {"enabled": true}});
        assert_eq!(names(&flags).unwrap(), ["nextcloud"]);
    }
    #[test]
    fn an_image_id_is_a_full_hex_digest() {
        assert!(image_id(&format!("sha256:{}", "0f".repeat(32))));
        assert!(!image_id(&format!("sha256:{}", "a".repeat(63))));
        assert!(!image_id(&format!("sha256:{}", "g".repeat(64))));
        assert!(!image_id(&"a".repeat(71)));
    }
    #[test]
    fn the_update_refuses_models_that_leave_their_sandbox() {
        let root = scratch("sandbox");
        let (data, media) = (root.join("srv"), root.join("mnt"));
        fs::create_dir_all(data.join("appdata/jellyfin")).unwrap();
        fs::create_dir_all(data.join("appdata/sonarr")).unwrap();
        fs::create_dir_all(media.join("library/movies")).unwrap();
        let port = json!([{"host_ip": "127.0.0.1", "published": "8096", "target": 8096}]);
        let plain = json!({"services": {"jellyfin": {"image": "sha256:8", "ports": port}}});
        valid_model(&plain, "jellyfin", &data, &media, &Value::Null).unwrap();
        let with = |key: &str, value: Value| {
            let mut m = plain.clone();
            m["services"]["jellyfin"][key] = value;
            m
        };
        let refused = |m: &Value, name: &str| {
            valid_model(m, name, &data, &media, &Value::Null)
                .unwrap_err()
                .to_string()
        };
        for (key, value) in [
            ("privileged", json!(true)),
            ("network_mode", json!("host")),
            ("pid", json!("host")),
            ("ipc", json!("host")),
            ("build", json!({"context": "."})),
            // The 0.46.0 review: Docker's own socket in the container, and a hook that
            // runs as root with every right.
            ("use_api_socket", json!(true)),
            (
                "post_start",
                json!([{"command": "sh", "privileged": true, "user": "root"}]),
            ),
            ("pre_stop", json!([{"command": "sh", "privileged": true}])),
            ("secrets", json!(["key"])),
            ("configs", json!(["settings"])),
            ("cap_add", json!(["SYS_ADMIN"])),
            ("group_add", json!(["docker"])),
            ("a_key_docker_adds_later", json!(true)),
        ] {
            assert_eq!(
                refused(&with(key, value), "jellyfin"),
                "Unsupported Compose isolation",
                "{key}"
            );
        }
        assert!(valid_model(
            &with("privileged", json!(false)),
            "jellyfin",
            &data,
            &media,
            &Value::Null
        )
        .is_ok());
        // Another app's name, an unknown one, or a second service: refused.
        assert_eq!(refused(&plain, "sonarr"), "Unsupported Compose isolation");
        assert_eq!(refused(&plain, "plex"), "Unsupported Compose isolation");
        let mut two = plain.clone();
        two["services"]["helper"] = json!({"image": "sha256:9"});
        assert_eq!(
            refused(&two, "jellyfin"),
            "Unexpected services; automated maintenance refused"
        );
        // Bind mounts: the app's own folder and the library; the clock read-only; nothing else.
        let bind = |source: &Path, read_only: bool| json!([{"type": "bind", "source": source, "target": "/x", "read_only": read_only}]);
        let own = data.join("appdata/jellyfin/cache");
        assert!(valid_model(
            &with("volumes", bind(&own, false)),
            "jellyfin",
            &data,
            &media,
            &Value::Null
        )
        .is_ok());
        let movies = media.join("library/movies");
        assert!(valid_model(
            &with("volumes", bind(&movies, true)),
            "jellyfin",
            &data,
            &media,
            &Value::Null
        )
        .is_ok());
        // Each app's own library folders, and nobody else's.
        for folder in [
            "library/music",
            "library/downloads/music",
            "photos/immich-library",
        ] {
            fs::create_dir_all(data.join(folder)).unwrap();
        }
        fs::create_dir_all(media.join("cloud/files")).unwrap();
        fs::create_dir_all(media.join("library/tv")).unwrap();
        let one = |name: &str, source: &Path| json!({"services": {name: {"image": "sha256:1", "ports": port, "volumes": bind(source, false)}}});
        for (name, folder) in [
            ("jellyfin", media.join("library/tv")),
            ("navidrome", data.join("library/music")),
            ("lidarr", data.join("library")),
            ("qbittorrent", data.join("library/downloads/music")),
            ("sonarr", media.join("library")),
        ] {
            fs::create_dir_all(data.join("appdata").join(name)).unwrap();
            assert!(
                valid_model(&one(name, &folder), name, &data, &media, &Value::Null).is_ok(),
                "{name}"
            );
        }
        for (name, folder) in [
            ("navidrome", data.join("library")),
            ("sonarr", data.join("library/music")),
            ("jellyfin", data.join("library/downloads/music")),
            ("lidarr", media.join("library/tv")),
        ] {
            assert_eq!(
                refused(&one(name, &folder), name),
                "Uncovered container bind mount",
                "{name}"
            );
        }
        let mut photos = models()["immich"].clone();
        let at = |source: &Path, target: &str| json!([{"type": "bind", "source": source, "target": target}]);
        photos["services"]["immich-server"]["volumes"] =
            at(&data.join("photos/immich-library"), "/data");
        fs::create_dir_all(data.join("appdata/immich/postgres")).unwrap();
        photos["services"]["database"]["volumes"] = at(
            &data.join("appdata/immich/postgres"),
            "/var/lib/postgresql/data",
        );
        valid_model(&photos, "immich", &data, &media, &Value::Null).unwrap();
        let immich_model = photos.clone();
        photos["services"]["immich-server"]["volumes"] = bind(&media.join("cloud/files"), false);
        assert_eq!(refused(&photos, "immich"), "Uncovered container bind mount");
        // The 0.46.1 audit: what Immich's snapshot needs is checked by the update too.
        let mut moved = photos.clone();
        moved["services"]["immich-server"]["volumes"] =
            at(&data.join("photos/immich-library"), "/elsewhere");
        assert!(refused(&moved, "immich").contains("not where its snapshot looks"));
        let mut user = photos.clone();
        user["services"]["immich-server"]["volumes"] =
            at(&data.join("photos/immich-library"), "/data");
        user["services"]["immich-server"]["user"] = json!("1000:1000");
        assert!(refused(&user, "immich").contains("another user"));
        let mut routed = photos.clone();
        routed["services"]["immich-server"]["volumes"] =
            at(&data.join("photos/immich-library"), "/data");
        routed["services"]["immich-server"]["environment"]["DB_URL"] =
            json!("postgres://elsewhere");
        assert!(refused(&routed, "immich").contains("own database"));
        let mut cloud = models()["nextcloud"].clone();
        valid_model(&cloud, "nextcloud", &data, &media, &Value::Null).unwrap();
        cloud["services"]["nextcloud"]["volumes"] = bind(&media.join("cloud/files"), false);
        valid_model(&cloud, "nextcloud", &data, &media, &Value::Null).unwrap();
        cloud["services"]["nextcloud"]["volumes"] =
            bind(&data.join("photos/immich-library"), false);
        assert_eq!(
            refused(&cloud, "nextcloud"),
            "Uncovered container bind mount"
        );
        let clock = Path::new("/etc/localtime");
        assert!(valid_model(
            &with("volumes", bind(clock, true)),
            "jellyfin",
            &data,
            &media,
            &Value::Null
        )
        .is_ok());
        assert!(valid_model(
            &with("volumes", bind(clock, false)),
            "jellyfin",
            &data,
            &media,
            &Value::Null
        )
        .is_err());
        let other = data.join("appdata/sonarr");
        assert_eq!(
            refused(&with("volumes", bind(&other, false)), "jellyfin"),
            "Uncovered container bind mount"
        );
        let named = json!([{"type": "volume", "source": "cache", "target": "/x"}]);
        assert_eq!(
            refused(&with("volumes", named), "jellyfin"),
            "Uncovered persistent volume"
        );
        // Ports: the app on loopback only; supporting services publish nothing.
        let open = json!([{"host_ip": "0.0.0.0", "published": "8096", "target": 8096}]);
        assert_eq!(
            refused(&with("ports", open), "jellyfin"),
            "Loopback application ports required"
        );
        assert_eq!(
            refused(&with("ports", json!([])), "jellyfin"),
            "Loopback application ports required"
        );
        let mut immich = immich_model;
        valid_model(&immich, "immich", &data, &media, &Value::Null).unwrap();
        immich["services"]["redis"]["ports"] = port.clone();
        assert_eq!(
            refused(&immich, "immich"),
            "Unexpected supporting service publication"
        );
        immich["services"]["redis"]["ports"] = json!([]);
        immich["services"]["redis"]["volumes"] =
            json!([{"type": "volume", "source": "redis", "target": "/data"}]);
        valid_model(&immich, "immich", &data, &media, &Value::Null).unwrap();
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn every_app_is_probed_on_its_own_port() {
        let probes = [
            ("jellyfin", 8096, "/health"),
            ("navidrome", 4533, "/ping"),
            ("sonarr", 8989, "/ping"),
            ("radarr", 7878, "/ping"),
            ("lidarr", 8686, "/ping"),
            ("prowlarr", 9696, "/ping"),
            ("seerr", 5055, "/api/v1/settings/public"),
            ("qbittorrent", 8080, "/"),
            ("immich", 2283, "/api/server/ping"),
            ("nextcloud", 80, "/status.php"),
        ];
        for (name, port, path) in probes {
            assert_eq!(app_port(name).unwrap(), (port, path), "{name}");
        }
        assert!(app_port("plex").is_err());
    }
    #[test]
    fn the_secrets_key_is_ready_only_with_its_certificate() {
        let dir = scratch("secrets-key");
        assert!(!secrets_key_ready(&dir));
        fs::write(dir.join(SECRETS_KEY), b"key").unwrap();
        assert!(!secrets_key_ready(&dir), "the key alone encrypts nothing");
        fs::write(dir.join(SECRETS_CERT), b"cert").unwrap();
        assert!(secrets_key_ready(&dir));
        fs::remove_file(dir.join(SECRETS_KEY)).unwrap();
        assert!(
            !secrets_key_ready(&dir),
            "the certificate alone decrypts nothing"
        );
        let _ = fs::remove_dir_all(dir);
    }
    #[test]
    fn three_monthly_snapshots_are_kept_unless_the_settings_say_otherwise() {
        assert_eq!(keep_snapshots(&json!({})), 3);
        assert_eq!(
            keep_snapshots(&json!({"maintenance": {"keep_snapshots": 7}})),
            7
        );
        assert_eq!(
            keep_snapshots(&json!({"maintenance": {"keep_snapshots": 0}})),
            0
        );
        assert_eq!(
            keep_snapshots(&json!({"maintenance": {"keep_snapshots": "7"}})),
            3
        );
    }

    // ---- 0.39.5 review: the monthly update

    #[test]
    fn a_refused_download_is_tried_again_and_another_failure_is_not() {
        let tries = std::cell::Cell::new(0);
        let waited = std::cell::RefCell::new(vec![]);
        let result = retry_transient(
            &[30, 90],
            |s| waited.borrow_mut().push(s),
            || {
                tries.set(tries.get() + 1);
                if tries.get() < 3 {
                    Err(Error::new("Error response from daemon: toomanyrequests: You have reached your pull rate limit"))
                } else {
                    Ok("pulled")
                }
            },
        );
        assert_eq!(result.unwrap(), "pulled");
        assert_eq!(*waited.borrow(), vec![30, 90]);
        // Not a passing reason (an image that does not exist): no second try.
        tries.set(0);
        let missing: Result<()> = retry_transient(
            &[30],
            |_| panic!("waited"),
            || {
                tries.set(tries.get() + 1);
                Err(Error::new("manifest unknown"))
            },
        );
        assert!(missing.is_err());
        assert_eq!(tries.get(), 1);
        // One that keeps failing: tried once per wait, then given up.
        tries.set(0);
        let busy: Result<()> = retry_transient(
            &[1, 1],
            |_| (),
            || {
                tries.set(tries.get() + 1);
                Err(Error::new(
                    "curl: (22) The requested URL returned error: 503",
                ))
            },
        );
        assert!(busy.is_err());
        assert_eq!(tries.get(), 3);
    }

    #[test]
    fn the_downloads_and_the_night_check_come_before_any_app_is_stopped() {
        let source = include_str!("maintenance.rs");
        let body = &source[source.find("fn transaction(").unwrap()..];
        let body = &body[..body.find("\nfn finish(").unwrap()];
        let at = |s: &str| body.find(s).unwrap_or_else(|| panic!("{s}"));
        // The daily check downloads, while the apps run; the update night only checks
        // the downloaded images are still here, before anything stops.
        assert!(!body.contains("download_updates("));
        assert!(at("images_here(") < at("save_journal("));
        assert!(at("night_ended(") < at("stop(&rows)"));
        let check = &source[source.find("fn look_for_updates(").unwrap()..];
        let check = &check[..check.find("\n}\n").unwrap()];
        assert!(check.contains("download_updates(") && !check.contains("stop(&"));
        // The night ended before anything stopped: done in part, not a failure.
        assert!(NIGHT_BEFORE_STOP.starts_with("Night window ended"));
    }

    #[test]
    fn a_later_release_may_finish_an_earlier_releases_run() {
        let cfg = json!({"storage":{"data_ssd":{"uuid":"a"}},"data_mount":"/srv/n","media_mount":"/mnt/m"});
        let journal = json!({"format":"neko-maintenance-resume-v1","version":"0.0.1","storage":{"data_ssd":{"uuid":"a"}},
            "data_mount":"/srv/n","media_mount":"/mnt/m","phase":"updating"});
        assert!(resumable(&journal, &cfg));
        // Written by a newer release (a downgrade since), or a phase this one doesn't know.
        let mut newer = journal.clone();
        newer["version"] = json!("999.0.0");
        assert!(!resumable(&newer, &cfg));
        let mut strange = journal.clone();
        strange["phase"] = json!("migrating");
        assert!(!resumable(&strange, &cfg));
        assert_eq!(release("0.39.5"), Some((0, 39, 5)));
        assert_eq!(release("0.39"), None);
        let mut other = journal.clone();
        other["storage"]["data_ssd"]["uuid"] = json!("b");
        assert!(!resumable(&other, &cfg));
        let mut unknown = journal;
        unknown["format"] = json!("neko-maintenance-resume-v2");
        assert!(!resumable(&unknown, &cfg));
    }

    #[test]
    fn a_rollback_puts_back_earlier_images_docker_no_longer_has() {
        let dir = std::env::temp_dir().join(format!("neko-images-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let id = |c: char| format!("sha256:{}", c.to_string().repeat(64));
        let models = json!({"jellyfin":{"services":{"jellyfin":{"image":id('a')}}},
                            "immich":{"services":{"server":{"image":id('b')}}}});
        let have = std::cell::RefCell::new(vec![id('a')]);
        let loads = std::cell::Cell::new(0);
        let present = |i: &str| have.borrow().iter().any(|h| h == i);
        // No saved copy: said plainly instead of an app left stopped without a reason.
        let gone = restore_images(&models, &dir, &present, &|_| Ok(()));
        assert!(gone.unwrap_err().to_string().contains("images.tar"));
        fs::write(dir.join("images.tar"), b"x").unwrap();
        restore_images(&models, &dir, &present, &|_| {
            loads.set(loads.get() + 1);
            have.borrow_mut().push(id('b'));
            Ok(())
        })
        .unwrap();
        assert_eq!(loads.get(), 1);
        // Everything there: nothing loaded.
        restore_images(&models, &dir, &present, &|_| panic!("loaded")).unwrap();
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn images_from_two_updates_ago_are_removed_and_nothing_newer() {
        let root = scratch("images");
        let media = root.join("hdd");
        let id = |c: char| format!("sha256:{}", c.to_string().repeat(64));
        let earlier = media.join("backups/monthly/100-1");
        fs::create_dir_all(&earlier).unwrap();
        fs::write(earlier.join("result.json"), b"{}").unwrap();
        // Two months ago the update replaced images "d" (old) and "e" (still in use now).
        common::atomic_json(
            &earlier.join("jellyfin-old.json"),
            &json!({"services":{"jellyfin":{"image":id('d')},"db":{"image":id('e')}}}),
        )
        .unwrap();
        fs::create_dir_all(media.join("backups/monthly/200-1")).unwrap();
        let replaced =
            json!({"jellyfin":{"services":{"jellyfin":{"image":id('f')},"db":{"image":id('e')}}}});
        let updated =
            json!({"jellyfin":{"services":{"jellyfin":{"image":id('c')},"db":{"image":id('e')}}}});
        let asked = std::cell::RefCell::new(vec![]);
        let older = previous_images(&media, "200-1", "jellyfin");
        // Read before the old snapshots are tidied: still known once that run is gone.
        fs::remove_dir_all(&earlier).unwrap();
        let removed = prune_images(&older, &updated, &replaced, &|i| {
            asked.borrow_mut().push(i.to_string());
            true
        });
        assert_eq!(removed, vec![id('d')]);
        assert_eq!(*asked.borrow(), vec![id('d')]);
        // The first update ever (no earlier run): nothing removed.
        let fresh = scratch("images-fresh");
        assert!(previous_images(&fresh.join("hdd"), "200-1", "jellyfin").is_empty());
        let _ = fs::remove_dir_all(&root);
        let _ = fs::remove_dir_all(&fresh);
    }

    #[test]
    fn a_failed_update_that_was_undone_says_so_in_its_report() {
        // UP-05: the dashboard can tell "undone, your apps run as before" from a failure
        // before anything was changed (the report's "rolled_back").
        assert!(rolled_back(Some("updating")) && rolled_back(Some("restoring")));
        for before in [None, Some("snapshot"), Some("backed_up"), Some("committed")] {
            assert!(!rolled_back(before), "{before:?}");
        }
        let source = include_str!("maintenance.rs");
        assert!(
            source.contains("note_failed_report(&cfgpath, &data, \"maintenance\", \"rolled_back\"")
        );
    }
    #[test]
    fn only_damage_on_the_usb_drive_stops_the_monthly_run() {
        assert!(usb_damage(&Error::new(&format!(
            "{}2 HDD archive files no longer match",
            backup::INTEGRITY
        ))));
        assert!(!usb_damage(&Error::new(
            "Filesystem or process operation failed"
        )));
        assert!(!usb_damage(&Error::new(
            "Nonempty mount destination refused"
        )));
    }

    #[test]
    fn a_night_that_ran_out_lets_the_next_month_take_its_snapshot() {
        // On a big library the checksum check could use up every night, so no monthly
        // snapshot was ever taken. The next month postpones the check; if even that
        // night is too short, the run fails instead of passing "in part".
        let data = scratch("night").join("ssd");
        fs::create_dir_all(data.join("reports/jobs")).unwrap();
        assert!(!night_ran_out(&data));
        mark_night_ran_out(&data);
        assert!(night_ran_out(&data));
        clear_night_ran_out(&data);
        assert!(!night_ran_out(&data));
        assert!(!NIGHT_TOO_SHORT.starts_with("Night window ended")); // a failure, not "done in part"
        let source = include_str!("maintenance.rs");
        let first = &source[source.find("fn checks_first(").unwrap()..];
        assert!(
            first.find("night_ran_out(data_root)").unwrap()
                < first.find("check_copies(cfgpath, cfg)").unwrap()
        );
        let ended = &source[source.find("fn night_ended(").unwrap()..];
        assert!(ended.find("if postponed").unwrap() < ended.find("NIGHT_TOO_SHORT").unwrap());
        let body = &source[source.find("fn transaction(").unwrap()..];
        let at = |s: &str| body.find(s).unwrap_or_else(|| panic!("{s}"));
        assert!(at("night_ended(cfg, postponed, audit, data_root)") < at("stop(&rows)"));
        assert!(at("clear_night_ran_out(data_root)") < at("stop(&rows)"));
    }

    // ---- fixes after the 0.39.5 review
    #[test]
    fn only_a_download_refused_for_now_is_tried_again() {
        // The digits of an HTTP status occur in image digests, which Docker quotes.
        let digest = "Error response from daemon: manifest for ghcr.io/immich-app/postgres@sha256:41eacbe83eca995561fe43814fd4891e16e39632806253848efaf04d3c8a5034 not found: manifest unknown";
        assert!(!transient(digest));
        let tries = std::cell::Cell::new(0);
        let r: Result<()> = retry_transient(
            &[30, 90],
            |_| panic!("waited"),
            || {
                tries.set(tries.get() + 1);
                Err(Error::new(digest))
            },
        );
        assert!(r.is_err());
        assert_eq!(tries.get(), 1);
        for permanent in [
            "unauthorized: authentication required (status 503 nowhere near)",
            "pull access denied for neko/none, repository does not exist",
            "curl: (22) The requested URL returned error: 404",
        ] {
            assert!(!transient(permanent), "{permanent}");
        }
        for passing in [
            "curl: (22) The requested URL returned error: 503",
            "received unexpected HTTP status: 502 Bad Gateway",
            "toomanyrequests: You have reached your pull rate limit",
            "Get \"https://registry-1.docker.io/v2/\": net/http: TLS handshake timeout",
            "status code 429",
            "http/1.1 504",
        ] {
            assert!(transient(passing), "{passing}");
        }
    }
    #[test]
    fn apps_are_pinned_to_the_images_they_run_before_any_download() {
        let data = scratch("pins").join("ssd");
        let id = |c: char| format!("sha256:{}", c.to_string().repeat(64));
        for app in ["jellyfin", "sonarr"] {
            fs::create_dir_all(data.join("compose").join(app)).unwrap();
        }
        // Sonarr was updated before: its pins are the update's and stay as they are.
        let earlier = data.join("compose/sonarr/maintenance-images.json");
        let update = format!(
            "{{\"services\":{{\"sonarr\":{{\"image\":\"{}\"}}}}}}",
            id('c')
        );
        fs::write(&earlier, &update).unwrap();
        let models = json!({"jellyfin":{"services":{"jellyfin":{"image":id('a')}}},
                            "sonarr":{"services":{"sonarr":{"image":id('b')}}}});
        let names = vec!["jellyfin".to_string(), "sonarr".to_string()];
        let as_running = |_: &str, _: &mut Value| -> Result<()> { Ok(()) };
        assert_eq!(
            pin_unpinned_with(&data, &names, &models, &as_running).unwrap(),
            ["jellyfin"]
        );
        let pins = load(&data.join("compose/jellyfin/maintenance-images.json")).unwrap();
        assert_eq!(pins, json!({"services":{"jellyfin":{"image":id('a')}}}));
        assert_eq!(fs::read(&earlier).unwrap(), update.as_bytes());
        // Before the first pull, in the run itself.
        let source = include_str!("maintenance.rs");
        let body = &source[source.find("fn transaction(").unwrap()..];
        let at = |s: &str| body.find(s).unwrap_or_else(|| panic!("{s}"));
        assert!(at("pin_unpinned(data, &names, &models)") < at("download_updates(cfg"));
        let _ = fs::remove_dir_all(data.parent().unwrap());
    }
    #[test]
    fn an_image_with_a_name_of_your_own_is_never_removed() {
        let own: std::collections::BTreeSet<String> = ["jellyfin/jellyfin", "host:5000/app"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        assert_eq!(
            repository("ghcr.io/immich-app/immich-server:v2.1.0"),
            "ghcr.io/immich-app/immich-server"
        );
        assert_eq!(repository("host:5000/app"), "host:5000/app");
        assert_eq!(repository("host:5000/app:1@sha256:abc"), "host:5000/app");
        assert!(only_ours(&[], &own)); // untagged: an old image nothing names
        assert!(only_ours(&["jellyfin/jellyfin:10.10.6".to_string()], &own));
        assert!(!only_ours(
            &[
                "jellyfin/jellyfin:10.10.6".to_string(),
                "postgres:16".to_string()
            ],
            &own
        ));
        let id = |c: char| format!("sha256:{}", c.to_string().repeat(64));
        let updated = json!({"jellyfin":{"services":{"jellyfin":{"image":id('c')}}}});
        let found = own_repositories(&updated, &|_| {
            Some(vec!["jellyfin/jellyfin:latest".to_string()])
        });
        assert!(found.contains("jellyfin/jellyfin"));
    }
    #[test]
    fn images_a_rolled_back_update_downloaded_are_cleaned_up_too() {
        let media = scratch("rolled").join("hdd");
        let id = |c: char| format!("sha256:{}", c.to_string().repeat(64));
        let failed = media.join("backups/monthly/150-1");
        fs::create_dir_all(&failed).unwrap();
        common::atomic_json(
            &failed.join("jellyfin-new.json"),
            &json!({"services":{"jellyfin":{"image":id('e')}}}),
        )
        .unwrap();
        // This run's own candidates are never counted.
        let current = media.join("backups/monthly/200-1");
        fs::create_dir_all(&current).unwrap();
        common::atomic_json(
            &current.join("jellyfin-new.json"),
            &json!({"services":{"jellyfin":{"image":id('f')}}}),
        )
        .unwrap();
        let older = previous_images(&media, "200-1", "jellyfin");
        assert!(older.contains(&id('e')));
        assert!(!older.contains(&id('f')));
        let _ = fs::remove_dir_all(media.parent().unwrap());
    }
    #[test]
    fn images_are_cleaned_up_after_their_run_folders_are_gone() {
        // keep_snapshots 1: the run of two updates ago and a rolled-back run were tidied
        // away before this update's clean-up looked. The record on the SSD still names
        // what they downloaded and replaced.
        let root = scratch("old-images");
        let (data, media) = (root.join("ssd"), root.join("hdd"));
        let id = |c: char| format!("sha256:{}", c.to_string().repeat(64));
        // The last update replaced "a" with "b"; a rolled-back one then downloaded "f";
        // this run downloaded "c" (noted as it was downloaded).
        save_recorded_images(&data, &[id('a')].into_iter().collect()).unwrap();
        remember_images(&data, &[id('f')]);
        remember_images(&data, &[id('c'), "nextcloud:31-apache".to_string()]);
        let mut older = previous_images(&media, "300-1", "jellyfin");
        assert!(older.is_empty());
        older.extend(recorded_images(&data));
        assert_eq!(older, [id('a'), id('c'), id('f')].into_iter().collect());
        let replaced = json!({"jellyfin":{"services":{"jellyfin":{"image":id('b')}}}});
        let updated = json!({"jellyfin":{"services":{"jellyfin":{"image":id('c')}}}});
        let removed = prune_images(&older, &updated, &replaced, &|i| i != id('f'));
        assert_eq!(removed, vec![id('a')]);
        // "f" could not be removed (say, still in use): it stays recorded, with "b" (to
        // go at the next update) and "c" (in use now). An image Docker no longer has
        // ("e") is dropped.
        older.insert(id('e'));
        let kept = still_recorded(&older, &removed, &replaced, &|i| i != id('e'));
        assert_eq!(kept, [id('b'), id('c'), id('f')].into_iter().collect());
        save_recorded_images(&data, &kept).unwrap();
        assert_eq!(recorded_images(&data), kept);
        // A damaged or unknown record is "nothing recorded", never a crash.
        fs::write(data.join(OLD_IMAGES), b"{not json").unwrap();
        assert!(recorded_images(&data).is_empty());
        fs::write(
            data.join(OLD_IMAGES),
            br#"{"format":"other","images":["x"]}"#,
        )
        .unwrap();
        assert!(recorded_images(&data).is_empty());
        // Downloads are noted as they happen; the clean-up reads the record.
        let source = include_str!("maintenance.rs");
        let pulls = &source[source.find("fn download_updates(").unwrap()..];
        let pulls = &pulls[..pulls.find("\nfn transaction(").unwrap()];
        assert!(pulls.contains("remember_images(data, &[id.to_string()]);"));
        let commit = &source[source
            .find("let mut older = previous_images(media, run, app);")
            .unwrap()..];
        assert!(commit[..200].contains("older.extend(recorded_images(data));"));
        let _ = fs::remove_dir_all(&root);
    }
    #[test]
    fn a_rolled_back_immich_update_s_images_are_found_in_its_quarantine_model() {
        let media = scratch("rolled-quarantine").join("hdd");
        let id = |c: char| format!("sha256:{}", c.to_string().repeat(64));
        let failed = media.join("backups/monthly/150-1");
        fs::create_dir_all(&failed).unwrap();
        common::atomic_json(
            &failed.join("immich-quarantine.json"),
            &json!({"services":{"immich-server":{"image":id('e')}}}),
        )
        .unwrap();
        assert!(previous_images(&media, "200-1", "immich").contains(&id('e')));
        let _ = fs::remove_dir_all(media.parent().unwrap());
    }
    #[test]
    fn a_run_folder_with_nothing_archived_is_removed_after_a_failure() {
        use std::os::unix::fs::PermissionsExt;
        let media = scratch("unarchived").join("hdd");
        let folder = media.join("backups/monthly");
        let make = |run: &str, mode: u32| {
            let dir = folder.join(run);
            fs::create_dir_all(&dir).unwrap();
            fs::write(dir.join("jellyfin-old.json"), b"{}").unwrap();
            fs::set_permissions(&dir, fs::Permissions::from_mode(mode)).unwrap();
            dir
        };
        let empty = make("100-1", 0o700);
        let archived = make("200-1", 0o700);
        fs::write(archived.join("data.tar"), b"x").unwrap();
        let not_ours = make("300-1", 0o755);
        remove_unarchived(&media);
        assert!(!empty.exists());
        assert!(archived.exists());
        assert!(not_ours.exists());
        let source = include_str!("maintenance.rs");
        let tidy = &source[source.find("fn tidy_after_failure(").unwrap()..];
        assert!(tidy[..400].contains("remove_unarchived("));
        let _ = fs::remove_dir_all(media.parent().unwrap());
    }
    #[test]
    fn the_hourly_retry_never_waits_for_the_lock() {
        let source = include_str!("maintenance.rs");
        let run = &source[source.find("pub fn run(").unwrap()..];
        let at = |s: &str| run.find(s).unwrap_or_else(|| panic!("{s}"));
        assert!(at("hourly_retry()") < at("common::lock_wait(&lock"));
        assert!(at("common::lock(&lock)") < at("common::lock_wait(&lock"));
        assert_eq!(RETRY_ENV, "NEKOSERVER_MAINTENANCE_RETRY");
        // A night that ran out before the stop is said as it is (no snapshot either).
        assert!(NIGHT_BEFORE_STOP.contains("no monthly snapshot was taken"));
        assert!(run.contains("if said == NIGHT_BEFORE_STOP"));
    }
    #[test]
    fn the_monthly_check_reads_each_archive_file_once() {
        let source = include_str!("maintenance.rs");
        let check = &source[source.find("fn check_copies(").unwrap()..];
        assert!(check[..300].contains("backup::read_once()"));
    }

    #[test]
    fn immich_starts_without_its_graphics_file_when_the_device_is_gone() {
        let source = include_str!("maintenance.rs");
        // Built from parts, so this test's own text is not counted.
        let guarded = format!("if hardware.exists() && {}() {{", "graphics_present");
        assert_eq!(source.matches(guarded.as_str()).count(), 2);
        assert_eq!(graphics_present(), Path::new("/dev/dri").exists());
    }
    #[test]
    fn a_slow_database_update_is_waited_for_while_the_app_keeps_working() {
        let busy = || true;
        let idle = || false;
        // Within health_wait_seconds nothing changes.
        assert!(!keep_waiting("jellyfin", 100, 300, false, busy));
        // Past it: Jellyfin, Immich, Nextcloud and the Arr apps get longer while working.
        for app in [
            "jellyfin",
            "immich",
            "nextcloud",
            "sonarr",
            "radarr",
            "lidarr",
            "prowlarr",
        ] {
            assert!(keep_waiting(app, 301, 300, false, busy), "{app}");
        }
        // Not when it stopped working (or restarted), not for other apps, not in the
        // restore test, and never past the bound.
        assert!(!keep_waiting("jellyfin", 301, 300, false, idle));
        assert!(!keep_waiting("qbittorrent", 301, 300, false, busy));
        assert!(!keep_waiting("jellyfin", 301, 300, true, busy));
        assert!(!keep_waiting("jellyfin", MIGRATION_WAIT, 300, false, busy));
        // A longer health_wait_seconds is never cut short.
        assert!(!keep_waiting("jellyfin", 1799, 1800, false, busy));
    }
    #[test]
    fn a_monthly_run_that_finds_setup_working_gives_way_and_comes_back_the_next_night() {
        let day = next_night("02:30").unwrap();
        assert!(
            regex::Regex::new(r"^\d{4}-\d{2}-\d{2}$")
                .unwrap()
                .is_match(&day),
            "{day}"
        );
        assert!(next_night("2:30; rm -rf /").is_none());
        let source = include_str!("maintenance.rs");
        let run = &source[source.find(&["pub fn ", "run("].concat()).unwrap()..];
        let run = &run[..run.find("\n}\n").unwrap()];
        assert!(run.contains("common::shared_lock_wait("));
        assert!(run.contains("again_next_night(&cfg)"));
        let main = include_str!("main.rs");
        assert!(main.contains("if common::postponed(&e)"));
    }
    /// The retry after a run that gave way on the night the clocks go back was set for
    /// a time already passed: now + 24 hours was still the same calendar date.
    #[test]
    fn the_retry_on_the_night_the_clocks_go_back_is_set_for_tomorrow_not_today() {
        // America/New_York on 2026-11-01, when the clocks go back at 02:00 (06:00 UTC):
        // UTC-4 before that moment, UTC-5 after it.
        let change: libc::time_t = 1_793_512_800;
        let new_york = |t: libc::time_t| {
            let offset = if t < change { -4 * 3600 } else { -5 * 3600 };
            chrono::DateTime::from_timestamp(t + offset, 0).map(|d| d.date_naive())
        };
        // 00:45 local on the change day: the day has 25 hours, so 24 hours later is
        // still 2026-11-01 (23:45), the trap the retry fell into.
        let early = change - 3600 - 15 * 60;
        let date = |t| new_york(t).unwrap().to_string();
        assert_eq!(date(early), "2026-11-01");
        assert_eq!(date(early + 86400), "2026-11-01");
        assert_eq!(
            next_night_at("02:30", early, &new_york).as_deref(),
            Some("2026-11-02")
        );
        // Later that day, and on an ordinary day, tomorrow is what it always was.
        assert_eq!(
            next_night_at("02:30", change + 17 * 3600, &new_york).as_deref(),
            Some("2026-11-02")
        );
        assert_eq!(
            next_night_at("02:30", change - 3 * 3600, &new_york).as_deref(),
            Some("2026-11-01")
        );
        assert_eq!(
            next_night_at("02:30", change + 3 * 86400, &new_york).as_deref(),
            Some("2026-11-05")
        );
        assert!(next_night_at("2:30", early, &new_york).is_none());
        assert!(next_night_at("02:30", early, &|_| None).is_none());
        // The real clock: the day after today's local date, in the same form.
        let today = local_date(unsafe { libc::time(std::ptr::null_mut()) }).unwrap();
        assert_eq!(
            next_night("02:30").as_deref(),
            Some(
                today
                    .succ_opt()
                    .unwrap()
                    .format("%Y-%m-%d")
                    .to_string()
                    .as_str()
            )
        );
    }
}
