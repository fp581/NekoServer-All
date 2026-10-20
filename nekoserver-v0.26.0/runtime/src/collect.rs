use crate::{
    common::{self, num, probe, round, Error, Result},
    Args, VERSION,
};
use regex::Regex;
use serde_json::{json, Value};
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
};
/// Every monitored app; notify.rs names an alert for each (checked by a test).
pub(crate) const APPS: [(&str, &str, &str, u16); 10] = [
    ("jellyfin", "Jellyfin", "Movies & shows", 8096),
    ("immich", "Immich", "Photos & memories", 2283),
    ("seerr", "Seerr", "Discover & request", 5055),
    ("navidrome", "Navidrome", "Your music library", 4533),
    ("sonarr", "Sonarr", "Series automation", 8989),
    ("radarr", "Radarr", "Movie automation", 7878),
    ("lidarr", "Lidarr", "Music automation", 8686),
    ("prowlarr", "Prowlarr", "Media sources", 9696),
    ("qbittorrent", "qBittorrent", "Download queue", 8080),
    ("nextcloud", "Nextcloud", "Your personal files", 8088),
];
/// Backup trees whose checksum checks the dashboard shows (reports/integrity.json).
pub(crate) const CHECKS: [(&str, &str); 3] = [
    ("archive", "HDD photo archive"),
    ("usb_photos", "USB photo copy"),
    ("usb_nextcloud", "USB Nextcloud copy"),
];
/// What the last checksum checks of each backup tree found.
fn integrity(data: &Path, ok: bool) -> Value {
    let all = if ok {
        common::json(&data.join("reports/integrity.json"), 20_000_000)
    } else {
        empty()
    };
    let mut rows = vec![];
    for (id, name) in CHECKS {
        let s = &all["sections"][id];
        if !s.is_object() {
            continue;
        }
        let count = |key: &str| s["counts"][key].as_u64().unwrap_or(0);
        let files: Vec<String> = ["both_damaged", "damaged", "missing", "disagree"]
            .iter()
            .flat_map(|k| s[k].as_array().cloned().unwrap_or_default())
            .filter_map(|v| v.as_str().map(|f| chars(f, 120)))
            .take(5)
            .collect();
        rows.push(json!({"id": id, "name": name, "at": s["at"],
            "unverified": s["unverified"].as_u64().unwrap_or(0),
            "repaired": s["repaired"].as_u64().unwrap_or(0),
            "damaged": count("damaged"), "both_damaged": count("both_damaged"), "disagree": count("disagree"),
            "missing": count("missing"),
            "files": files}));
    }
    json!(rows)
}
/// Every monitored job; notify.rs names an alert for each (checked by a test).
pub(crate) const JOBS: [(&str, &str, &str, f64); 7] = [
    (
        "music",
        "Music library backup",
        "nekoserver-music-backup",
        172800.0,
    ),
    (
        "archive",
        "Photo archive",
        "nekoserver-photo-preservation",
        10800.0,
    ),
    (
        "database",
        "Immich database",
        "nekoserver-immich-db-backup",
        172800.0,
    ),
    (
        "external",
        "External photo copy",
        "nekoserver-photo-backup",
        864000.0,
    ),
    (
        "applications",
        "Media application snapshot",
        "nekoserver-app-backup",
        172800.0,
    ),
    (
        "host",
        "Host and user snapshot",
        "nekoserver-host-backup",
        172800.0,
    ),
    (
        "maintenance",
        "Monthly backup and updates",
        "nekoserver-maintenance",
        3024000.0,
    ),
];
fn text(v: &Value) -> &str {
    v.as_str().unwrap_or("")
}
fn empty() -> Value {
    json!({})
}
fn chars(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}
fn metrics(previous: &Value) -> (Value, Value) {
    let mut host = json!({"cpu_percent":null,"memory_percent":null,"memory_used":null,"memory_total":null,"temperature":null,"uptime_seconds":null,"cpu_model":null});
    let mut current = empty();
    if let Ok(s) = fs::read_to_string("/proc/stat") {
        let values = s
            .lines()
            .next()
            .unwrap_or("")
            .split_whitespace()
            .skip(1)
            .take(8)
            .map(str::parse::<u64>)
            .collect::<std::result::Result<Vec<_>, _>>();
        if let Ok(v) = values {
            if v.len() == 8 {
                let boot = fs::read_to_string("/proc/sys/kernel/random/boot_id")
                    .unwrap_or_default()
                    .trim()
                    .to_owned();
                let total: u64 = v.iter().sum();
                let idle = v[3] + v[4];
                current = json!({"total":total,"idle":idle,"boot":boot});
                if previous["boot"] == current["boot"] {
                    if let (Some(pt), Some(pi)) =
                        (previous["total"].as_u64(), previous["idle"].as_u64())
                    {
                        if total > pt && idle >= pi && idle - pi <= total - pt {
                            host["cpu_percent"] = json!(round(
                                100.0 * ((total - pt) - (idle - pi)) as f64 / (total - pt) as f64
                            ));
                        }
                    }
                }
            }
        }
    }
    if let Ok(s) = fs::read_to_string("/proc/meminfo") {
        let mut total = 0;
        let mut available = 0;
        for line in s.lines() {
            let mut f = line.split_whitespace();
            match f.next() {
                Some("MemTotal:") => {
                    total = f.next().and_then(|n| n.parse::<u64>().ok()).unwrap_or(0) * 1024
                }
                Some("MemAvailable:") => {
                    available = f.next().and_then(|n| n.parse::<u64>().ok()).unwrap_or(0) * 1024
                }
                _ => {}
            }
        }
        if total > 0 && available <= total {
            host["memory_total"] = json!(total);
            host["memory_used"] = json!(total - available);
            host["memory_percent"] =
                json!(round(100.0 * (total - available) as f64 / total as f64));
        }
    }
    if let Ok(s) = fs::read_to_string("/proc/uptime") {
        host["uptime_seconds"] = num(
            &json!(s
                .split_whitespace()
                .next()
                .unwrap_or("")
                .parse::<f64>()
                .ok()),
            0.0,
            1e18,
        );
    }
    if let Ok(s) = fs::read_to_string("/proc/cpuinfo") {
        if let Some(line) = s.lines().find(|s| s.starts_with("model name")) {
            host["cpu_model"] = json!(chars(
                line.split_once(':').map(|p| p.1.trim()).unwrap_or(""),
                100
            ));
        }
    }
    let mut temps = vec![];
    if let Ok(rows) = fs::read_dir("/sys/class/hwmon") {
        for hw in rows.flatten() {
            if let Ok(name) = fs::read_to_string(hw.path().join("name")) {
                if !["coretemp", "k10temp", "zenpower"].contains(&name.trim()) {
                    continue;
                }
                if let Ok(sensors) = fs::read_dir(hw.path()) {
                    for sensor in sensors.flatten() {
                        let name = sensor.file_name();
                        let name = name.to_string_lossy();
                        if name.starts_with("temp") && name.ends_with("_input") {
                            if let Ok(value) = fs::read_to_string(sensor.path()) {
                                if let Ok(v) = value.trim().parse::<f64>() {
                                    let v = v / 1000.0;
                                    if (-10.0..=150.0).contains(&v) {
                                        temps.push(v)
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    if let Some(v) = temps.into_iter().max_by(f64::total_cmp) {
        host["temperature"] = json!(round(v));
    }
    (host, current)
}
fn mount_info(path: &Path, expected: Option<&str>, fstype: &str) -> Value {
    if expected == Some("") {
        return json!({"status":"not_configured"});
    }
    let (code, s) = probe(
        &[
            "findmnt",
            "-n",
            "-o",
            "UUID,FSTYPE,SOURCE",
            "--mountpoint",
            &path.to_string_lossy(),
        ],
        8,
    );
    let v = s.split_whitespace().collect::<Vec<_>>();
    if code != 0 || v.len() < 3 {
        return json!({"status":"missing"});
    }
    if expected
        .map(|u| u != v[0] || fstype != v[1])
        .unwrap_or(false)
    {
        return json!({"status":"wrong_drive"});
    }
    json!({"status":"mounted","source":v[2].split('[').next().unwrap_or("")})
}
fn device(source: &str) -> Option<String> {
    let partition =
        Regex::new(r"^/dev/(sd[a-z]+[0-9]*|nvme[0-9]+n[0-9]+(?:p[0-9]+)?|vd[a-z]+[0-9]*)$").ok()?;
    if !partition.is_match(source) {
        return None;
    }
    let (code, s) = probe(&["lsblk", "-n", "-o", "PKNAME", source], 8);
    let parent = s.trim().lines().next().unwrap_or("");
    let value = if code == 0 && !parent.is_empty() {
        format!("/dev/{parent}")
    } else {
        source.into()
    };
    Regex::new(r"^/dev/(sd[a-z]+|nvme[0-9]+n[0-9]+|vd[a-z]+)$")
        .ok()?
        .is_match(&value)
        .then_some(value)
}
fn smart_summary(v: &Value, code: i32) -> Value {
    let mut r = json!({"status":"unknown","temperature":null,"wear_percent":null,"reason":"SMART unavailable or drive sleeping; no wake requested"});
    if let Some(pass) = v["smart_status"]["passed"].as_bool() {
        r["status"] = json!(if pass { "pass" } else { "fail" });
        r["reason"] = json!(if pass {
            "SMART health passed"
        } else {
            "SMART health failed"
        });
    } else {
        return r;
    }
    r["temperature"] = num(&v["temperature"]["current"], 0.0, 150.0);
    r["wear_percent"] = num(
        &v["nvme_smart_health_information_log"]["percentage_used"],
        0.0,
        1000.0,
    );
    let critical = num(
        &v["nvme_smart_health_information_log"]["critical_warning"],
        0.0,
        255.0,
    );
    if critical.as_f64().map(|n| n != 0.0).unwrap_or(false) {
        r["status"] = json!("fail");
        r["reason"] = json!("NVMe critical warning");
    } else if r["status"] == "pass" && r["wear_percent"].as_f64().unwrap_or(0.0) >= 90.0 {
        r["status"] = json!("warning");
        r["reason"] = json!("NVMe endurance usage is high");
    }
    if let Some(table) = v["ata_smart_attributes"]["table"].as_array() {
        for row in table {
            if num(&row["raw"]["value"], 0.0, 1e18).as_f64().unwrap_or(0.0) > 0.0 {
                if row["id"] == 197 || row["id"] == 198 {
                    r["status"] = json!("fail");
                    r["reason"] = json!("Pending or uncorrectable sectors");
                } else if row["id"] == 5 && r["status"] == "pass" {
                    r["status"] = json!("warning");
                    r["reason"] = json!("Reallocated sectors detected");
                }
            }
        }
    }
    if code >= 0 && code & 0b11111000 != 0 && r["status"] == "pass" {
        r["status"] = json!("warning");
        r["reason"] = json!("SMART reports historical/current health errors; inspect the disk");
    }
    r
}
fn smart(source: &str, identity: &str, cache: &mut Value, now: f64, interval: f64) -> Value {
    let Some(dev) = device(source) else {
        return json!({"status":"unknown","reason":"Physical disk mapping unavailable","checked_at":null});
    };
    let key = format!("{identity}|{dev}");
    let old = &cache[&key];
    let stamp = num(&old["checked_at"], 0.0, now).as_f64();
    if ["pass", "warning", "fail", "unknown"].contains(&text(&old["status"]))
        && stamp.map(|s| now - s < interval).unwrap_or(false)
    {
        let reasons = [
            "SMART health passed",
            "SMART health failed",
            "NVMe critical warning",
            "NVMe endurance usage is high",
            "Pending or uncorrectable sectors",
            "Reallocated sectors detected",
            "SMART reports historical/current health errors; inspect the disk",
            "SMART unavailable or drive sleeping; no wake requested",
        ];
        return json!({"status":old["status"],"temperature":num(&old["temperature"],0.0,150.0),"wear_percent":num(&old["wear_percent"],0.0,1000.0),"checked_at":old["checked_at"],"reason":if reasons.contains(&text(&old["reason"])){text(&old["reason"])}else{"Inspect drive health"}});
    }
    let (code, s) = probe(
        &[
            "smartctl",
            "--json",
            "--health",
            "--attributes",
            "--nocheck=standby",
            &dev,
        ],
        15,
    );
    let v: Value = serde_json::from_str(&s).unwrap_or_else(|_| empty());
    let mut r = smart_summary(&v, code);
    r["checked_at"] = json!(now);
    cache[&key] = r.clone();
    r
}
fn usage(path: &Path) -> Option<(u64, u64)> {
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;
    let s = CString::new(path.as_os_str().as_bytes()).ok()?;
    let mut stat = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    if unsafe { libc::statvfs(s.as_ptr(), stat.as_mut_ptr()) } != 0 {
        return None;
    }
    let stat = unsafe { stat.assume_init() };
    let total = stat.f_blocks.checked_mul(stat.f_frsize)?;
    let free = stat.f_bfree.checked_mul(stat.f_frsize)?;
    Some((total, total.checked_sub(free)?))
}
fn storage(cfg: &Value, cache: &mut Value, now: f64) -> Result<(Value, bool, bool)> {
    let rows = [
        ("os", "OS SSD", PathBuf::from("/"), None, ""),
        (
            "data_ssd",
            "Application SSD",
            common::mount_path(text(&cfg["data_mount"]))?,
            Some(text(&cfg["storage"]["data_ssd"]["uuid"])),
            text(&cfg["storage"]["data_ssd"]["fstype"]),
        ),
        (
            "media_hdd",
            "Media HDD",
            common::mount_path(text(&cfg["media_mount"]))?,
            Some(text(&cfg["storage"]["media_hdd"]["uuid"])),
            text(&cfg["storage"]["media_hdd"]["fstype"]),
        ),
    ];
    let mut disks = vec![];
    let mut data_ok = false;
    let mut media_ok = false;
    for (key, name, path, expected, fs) in rows {
        let info = mount_info(&path, expected, fs);
        let mut r = json!({"id":key,"name":name,"mount":path,"status":info["status"],"total":null,"used":null,"used_percent":null,"smart":{"status":"unknown","checked_at":null}});
        if info["status"] == "mounted" {
            if let Some((total, used)) = usage(&path) {
                if total > 0 {
                    r["total"] = json!(total);
                    r["used"] = json!(used);
                    r["used_percent"] = json!(round(100.0 * used as f64 / total as f64));
                } else {
                    r["status"] = json!("unavailable")
                }
            } else {
                r["status"] = json!("unavailable")
            }
            let source = text(&info["source"]);
            r["smart"] = smart(
                source,
                expected.unwrap_or(source),
                cache,
                now,
                cfg["dashboard"]["smart_interval_seconds"]
                    .as_f64()
                    .unwrap_or(1800.0),
            );
        }
        if key == "data_ssd" {
            data_ok = r["status"] == "mounted";
        }
        if key == "media_hdd" {
            media_ok = r["status"] == "mounted";
        }
        disks.push(r)
    }
    Ok((json!(disks), data_ok, media_ok))
}
/// Bytes from a size Docker prints ("512MiB", "1.2GiB", "800kB").
fn docker_size(text: &str) -> Option<f64> {
    let text = text.trim();
    let split = text.find(|c: char| c.is_ascii_alphabetic())?;
    let value = text[..split].trim().parse::<f64>().ok()?;
    let unit = match &text[split..] {
        "B" => 1.0,
        "kB" | "KB" => 1e3,
        "KiB" => 1024.0,
        "MB" => 1e6,
        "MiB" => 1024.0 * 1024.0,
        "GB" => 1e9,
        "GiB" => 1024.0 * 1024.0 * 1024.0,
        "TB" => 1e12,
        "TiB" => 1024f64.powi(4),
        _ => return None,
    };
    Some((value * unit).round())
}
/// CPU (% of one core) and memory of each running container, keyed by short ID.
fn docker_stats(output: &str) -> std::collections::HashMap<String, (f64, f64)> {
    let mut out = std::collections::HashMap::new();
    for line in output.lines() {
        let Ok(v) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        let id = text(&v["ID"]);
        let cpu = text(&v["CPUPerc"])
            .trim_end_matches('%')
            .parse::<f64>()
            .ok();
        let memory = text(&v["MemUsage"]).split('/').next().and_then(docker_size);
        if let (12.., Some(cpu), Some(memory)) = (id.len(), cpu, memory) {
            out.insert(id[..12].to_string(), (cpu, memory));
        }
    }
    out
}
/// The NekoServer app a container belongs to (its Compose project is neko-<app>).
fn app_of(labels: &std::collections::BTreeMap<String, String>) -> Option<&str> {
    let app = labels
        .get("com.docker.compose.project")?
        .strip_prefix("neko-")?;
    APPS.iter().find(|a| a.0 == app).map(|a| a.0)
}
fn applications(cfg: &Value) -> (Value, Value) {
    let (code, s) = probe(
        &[
            "docker",
            "--host",
            "unix:///var/run/docker.sock",
            "ps",
            "-a",
            "--format",
            "{{json .}}",
        ],
        15,
    );
    let mut records = vec![];
    if code == 0 {
        for line in s.lines() {
            if let Ok(v) = serde_json::from_str::<Value>(line) {
                let labels = text(&v["Labels"])
                    .split(',')
                    .filter_map(|s| s.split_once('='))
                    .map(|(k, v)| (k.to_string(), v.to_string()))
                    .collect::<std::collections::BTreeMap<_, _>>();
                records.push((labels, v));
            }
        }
    }
    // Which app each container belongs to (the dashboard names processes by it),
    // and how much CPU and memory each app's containers use together.
    let mut containers = serde_json::Map::new();
    for (labels, v) in &records {
        if let (Some(app), id) = (app_of(labels), text(&v["ID"])) {
            if id.len() >= 12 {
                containers.insert(id[..12].to_string(), json!(app));
            }
        }
    }
    let usage = if records.iter().any(|(_, v)| v["State"] == "running") {
        let (code, out) = probe(
            &[
                "docker",
                "--host",
                "unix:///var/run/docker.sock",
                "stats",
                "--no-stream",
                "--format",
                "{{json .}}",
            ],
            30,
        );
        if code == 0 {
            docker_stats(&out)
        } else {
            Default::default()
        }
    } else {
        Default::default()
    };
    let services = cfg["media"]["services"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let mut out = vec![];
    for (app, name, description, port) in APPS {
        if !services.iter().any(|v| v == app)
            && !(app == "immich" && cfg["photos"]["deploy_immich"] == true)
            && !(app == "nextcloud" && cfg["nextcloud"]["enabled"] == true)
        {
            continue;
        }
        let project = format!("neko-{app}");
        let service = if app == "immich" {
            "immich-server"
        } else {
            app
        };
        let found = records
            .iter()
            .filter(|(l, _)| {
                l.get("com.docker.compose.project") == Some(&project)
                    && l.get("com.docker.compose.service").map(String::as_str) == Some(service)
            })
            .collect::<Vec<_>>();
        let mut state = if code != 0 { "unknown" } else { "missing" };
        let mut health = "unknown";
        if found.len() == 1 {
            let v = &found[0].1;
            state = if [
                "running",
                "exited",
                "restarting",
                "paused",
                "created",
                "dead",
            ]
            .contains(&text(&v["State"]))
            {
                text(&v["State"])
            } else {
                "unknown"
            };
            let status = text(&v["Status"]);
            health = if status.contains("(unhealthy)") {
                "unhealthy"
            } else if status.contains("(health: starting)") {
                "starting"
            } else if status.contains("(healthy)") {
                "healthy"
            } else {
                "not_defined"
            };
        } else if found.len() > 1 {
            state = "ambiguous";
        }
        let mine = containers
            .iter()
            .filter(|(_, owner)| *owner == app)
            .filter_map(|(id, _)| usage.get(id))
            .collect::<Vec<_>>();
        let (cpu, memory) = if mine.is_empty() {
            (Value::Null, Value::Null)
        } else {
            (
                json!(round(mine.iter().map(|u| u.0).sum::<f64>())),
                json!(mine.iter().map(|u| u.1).sum::<f64>()),
            )
        };
        out.push(json!({"id":app,"name":name,"description":description,"state":state,"health":health,"url":format!("http://127.0.0.1:{port}/"),"cpu_percent":cpu,"memory_used":memory}));
    }
    (json!(out), Value::Object(containers))
}
/// NVIDIA cards: only root can ask the driver, so the collector does it for the
/// dashboard (which matches each row to its card by PCI address).
fn gpu_telemetry() -> Value {
    let (code, out) = probe(
        &[
            "nvidia-smi",
            "--query-gpu=pci.bus_id,name,utilization.gpu,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ],
        5,
    );
    if code != 0 {
        return json!([]);
    }
    json!(parse_nvidia(&out))
}
fn parse_nvidia(out: &str) -> Vec<Value> {
    out.lines()
        .take(16)
        .filter_map(|line| {
            let f = line.split(',').map(str::trim).collect::<Vec<_>>();
            if f.len() != 5 || !f[0].contains(':') {
                return None;
            }
            let n = |s: &str| s.parse::<f64>().ok().filter(|v| v.is_finite() && *v >= 0.0);
            Some(json!({"bus": chars(f[0], 20), "name": chars(f[1], 100),
                "percent": n(f[2]).filter(|v| *v <= 100.0),
                "memory_used": n(f[3]).map(|m| m * 1024.0 * 1024.0),
                "memory_total": n(f[4]).map(|m| m * 1024.0 * 1024.0)}))
        })
        .collect()
}
/// What starts by itself when the computer boots, in people's words. Only units
/// that are installed are listed.
const STARTUP: &[(&str, &str)] = &[
    ("docker.service", "Apps (Docker)"),
    ("tailscaled.service", "Phone access (Tailscale)"),
    ("nekoserver-dashboard.service", "This dashboard"),
    ("nekoserver-dashboard-collect.timer", "Dashboard readings"),
    ("smb.service", "Shared folders (Samba)"),
    ("nekoserver-hdd-policy.service", "Drive power settings"),
    (
        "nekoserver-app-resume.service",
        "Restart apps after an interrupted backup",
    ),
    (
        "nekoserver-host-resume.service",
        "Finish an interrupted host backup",
    ),
    (
        "nekoserver-maintenance-resume.service",
        "Finish an interrupted monthly update",
    ),
    ("nekoserver-app-backup.timer", "Nightly app backup"),
    ("nekoserver-host-backup.timer", "Nightly host backup"),
    ("nekoserver-music-backup.timer", "Music backup"),
    ("nekoserver-photo-backup.timer", "USB photo backup"),
    ("nekoserver-maintenance.timer", "Monthly update"),
    ("nekoserver-media-health.timer", "App health checks"),
    ("nekoserver-heartbeat.timer", "Outside heartbeat"),
    ("nekoserver-notifications.timer", "Phone notifications"),
];
fn startup() -> Value {
    let mut args = vec![
        "systemctl",
        "show",
        "--property=Id,LoadState,UnitFileState,ActiveState",
    ];
    args.extend(STARTUP.iter().map(|u| u.0));
    let (code, out) = probe(&args, 15);
    if code != 0 {
        return json!([]);
    }
    json!(parse_startup(&out))
}
fn parse_startup(out: &str) -> Vec<Value> {
    out.split("\n\n")
        .filter_map(|block| {
            let fields = block
                .lines()
                .filter_map(|l| l.split_once('='))
                .collect::<std::collections::HashMap<_, _>>();
            let id = *fields.get("Id")?;
            let label = STARTUP.iter().find(|u| u.0 == id)?.1;
            if fields.get("LoadState") != Some(&"loaded") {
                return None;
            }
            let file = fields.get("UnitFileState").copied().unwrap_or("");
            // "static" units start because something else needs them.
            let enabled = ["enabled", "enabled-runtime", "static", "alias"].contains(&file);
            Some(json!({"unit": id, "name": label, "enabled": enabled,
                "active": fields.get("ActiveState").copied().unwrap_or("unknown")}))
        })
        .collect()
}
fn backups(cfg: &Value, now: f64, ok: bool) -> Value {
    let mut out = vec![];
    for (key, name, unit, base_max) in JOBS {
        if key == "music" && cfg["music"]["backup_enabled"] == false {
            continue;
        }
        let weekly =
            cfg["photos"]["backup_schedule"] == "weekly" && ["archive", "database"].contains(&key);
        let report_key = if weekly { "database" } else { key };
        let unit = if weekly {
            "nekoserver-immich-db-backup"
        } else {
            unit
        };
        let max = if weekly {
            864000.0
        } else if key == "archive" {
            cfg["photos"]["archive_interval_minutes"]
                .as_u64()
                .filter(|v| (15..=10080).contains(v))
                .map(|v| base_max.max(v as f64 * 120.0))
                .unwrap_or(base_max)
        } else {
            base_max
        };
        let saved = if ok {
            common::json(
                &Path::new(text(&cfg["data_mount"]))
                    .join(format!("reports/jobs/{report_key}.json")),
                64000,
            )
        } else {
            empty()
        };
        let raw = text(&saved["status"]);
        let status = if ["running", "pass", "fail", "partial"].contains(&raw) {
            raw
        } else {
            "unknown"
        };
        let success = num(&saved["last_success"], 0.0, now + 60.0);
        let start = num(&saved["started_at"], 0.0, now + 60.0);
        let stale = if status == "running" {
            start.as_f64().map(|t| now - t > max).unwrap_or(false)
        } else {
            success.as_f64().map(|t| now - t > max).unwrap_or(false)
        };
        let state = if stale { "stale" } else { status };
        let (code, s) = probe(
            &[
                "systemctl",
                "show",
                &format!("{unit}.timer"),
                "--property=LoadState,ActiveState,NextElapseUSecRealtime",
            ],
            8,
        );
        let fields = s
            .lines()
            .filter_map(|s| s.split_once('='))
            .collect::<std::collections::BTreeMap<_, _>>();
        let next = fields
            .get("NextElapseUSecRealtime")
            .filter(|s| !s.is_empty())
            .map(|s| chars(s, 80));
        out.push(json!({"id":key,"name":name,"status":state,"last_success":success,"last_attempt":start,"timer":{"installed":code==0&&fields.get("LoadState")==Some(&"loaded"),"active":fields.get("ActiveState")==Some(&"active"),"next_run":next},"reason":if status=="fail"{Some(failure_reason(&saved))}else if status=="partial"{Some(partial_reason(&saved["missing"]))}else if success.is_null(){Some("No recorded successful run".to_string())}else if stale{Some("Last success is overdue".to_string())}else{None}}));
    }
    json!(out)
}
/// "Failed while copying photos to the USB drive: cp exited with 1 (No space left on device)".
fn failure_reason(saved: &Value) -> String {
    let stage = chars(text(&saved["stage"]), 120);
    let reason = chars(text(&saved["reason"]), 300);
    match (stage.is_empty(), reason.is_empty()) {
        (true, true) => "Last run failed; inspect its service logs".to_string(),
        (false, true) => format!("Failed while {stage}"),
        (true, false) => format!("Failed: {reason}"),
        (false, false) => format!("Failed while {stage}: {reason}"),
    }
}
/// "3 files were not copied (2 in use, 1 with a name the USB drive cannot store): a, b, c".
fn partial_reason(missing: &Value) -> String {
    let in_use = missing["in_use"].as_u64().unwrap_or(0);
    let names = missing["unsupported_names"].as_u64().unwrap_or(0);
    let files: Vec<String> = missing["files"]
        .as_array()
        .map(|a| {
            a.iter()
                .filter_map(|f| f.as_str())
                .take(5)
                .map(|f| chars(f, 120))
                .collect()
        })
        .unwrap_or_default();
    let mut parts = vec![];
    if in_use + names > 0 || !files.is_empty() {
        let mut text = format!(
            "{} files were not copied ({in_use} in use, {names} with names the USB drive cannot store)",
            in_use + names
        );
        if !files.is_empty() {
            text += &format!(": {}", files.join(", "));
        }
        parts.push(text);
    }
    // The USB copy of the Immich database: missing, stale or without its version note.
    let database = chars(text(&missing["database"]), 300);
    if !database.is_empty() {
        let mut first = database.chars();
        parts.push(
            first
                .next()
                .map(|c| c.to_uppercase().collect::<String>() + first.as_str())
                .unwrap_or_default(),
        );
    }
    if parts.is_empty() {
        return "Incomplete; see the job's report".to_string();
    }
    parts.join(". ")
}
fn heartbeat(cfg: &Value, now: f64) -> Value {
    let row = common::json(Path::new("/run/nekoserver-heartbeat/status.json"), 64000);
    let attempt = num(&row["last_attempt"], 0.0, now + 30.0);
    let (code, text) = probe(
        &[
            "systemctl",
            "show",
            "nekoserver-heartbeat.timer",
            "--property=LoadState,ActiveState,NextElapseUSecRealtime",
        ],
        8,
    );
    let fields = text
        .lines()
        .filter_map(|s| s.split_once('='))
        .collect::<std::collections::BTreeMap<_, _>>();
    let installed = code == 0 && fields.get("LoadState") == Some(&"loaded");
    let interval = cfg["heartbeat"]["interval_seconds"]
        .as_u64()
        .filter(|v| (60..=3600).contains(v))
        .unwrap_or(300);
    let state = if installed
        && attempt
            .as_f64()
            .is_none_or(|t| now - t > interval as f64 + 90.0)
    {
        "stale"
    } else if row["format"] == "neko-heartbeat-v1"
        && ["pass", "fail"].contains(&row["status"].as_str().unwrap_or(""))
    {
        row["status"].as_str().unwrap()
    } else {
        "unknown"
    };
    json!({"status":state,"timer":{"installed":installed,"active":fields.get("ActiveState")==Some(&"active"),"next_run":fields.get("NextElapseUSecRealtime").filter(|s|!s.is_empty()).map(|s|chars(s,80))},"last_attempt":attempt,"last_success":num(&row["last_success"],0.0,now+30.0),"provider_notifications_verified":false})
}

fn media(cfg: &Value, now: f64, ok: bool) -> Value {
    if cfg["monitoring"]["enabled"] == false {
        return json!({"status":"disabled","counts":{},"checks":[],"age_seconds":null});
    }
    let saved = if ok {
        common::json(
            &Path::new(text(&cfg["data_mount"])).join("reports/media/latest.json"),
            2_000_000,
        )
    } else {
        empty()
    };
    let age = chrono::DateTime::parse_from_rfc3339(text(&saved["generated_at"]))
        .ok()
        .map(|t| (now - t.timestamp_millis() as f64 / 1000.0).max(0.0));
    let mut r = json!({"status":if let Some(age)=age{if age>7200.0_f64.max(cfg["monitoring"]["interval_minutes"].as_f64().unwrap_or(60.0)*120.0){"stale"}else{"current"}}else{"unknown"},"age_seconds":age,"counts":{},"checks":[]});
    let re = Regex::new(r"^[a-z0-9_:]+$").unwrap();
    for group in ["services", "integrations", "readiness"] {
        if let Some(rows) = saved[group].as_object() {
            for (key, v) in rows.iter().take(50) {
                let state = text(&v["result"]);
                if re.is_match(key) && ["pass", "warning", "fail", "needs_setup"].contains(&state) {
                    r["checks"].as_array_mut().unwrap().push(
                        json!({"name":key.replace('_'," ").replace(':'," · "),"status":state}),
                    );
                    let count = r["counts"][state].as_u64().unwrap_or(0) + 1;
                    r["counts"][state] = json!(count);
                }
            }
        }
    }
    r
}
fn recovery(cfg: &Value, data_ok: bool, media_ok: bool) -> Value {
    let mut r = json!({"status":"needs_setup","reason":"Full restore acceptance still required","latest_rehearsal":null,"checkpoint":false});
    let data = Path::new(text(&cfg["data_mount"]));
    if data_ok {
        r["checkpoint"] = json!(
            common::json(&data.join("installer/host-checkpoint.json"), 2_000_000)["format"]
                == "neko-host-checkpoint-v1"
        );
    }
    let mut candidates = vec![];
    for (root, ok) in [
        (data, data_ok),
        (Path::new(text(&cfg["media_mount"])), media_ok),
    ] {
        if !ok {
            continue;
        }
        let folder = root.join("recovery-tests");
        if common::safe(&folder).is_err() {
            continue;
        }
        if let Ok(items) = fs::read_dir(folder) {
            for item in items.flatten().take(100) {
                let p = item.path().join("restore-report.json");
                if common::safe(&p).is_ok() {
                    if let Ok(meta) = fs::metadata(&p) {
                        if let Ok(time) = meta.modified() {
                            if let Ok(d) = time.duration_since(std::time::UNIX_EPOCH) {
                                candidates.push((d.as_secs_f64(), p));
                            }
                        }
                    }
                }
            }
        }
    }
    if let Some((at, p)) = candidates.into_iter().max_by(|a, b| a.0.total_cmp(&b.0)) {
        let v = common::json(&p, 64000);
        if v["status"] == "needs_setup" {
            r["latest_rehearsal"] = json!({"at":at,"database_tables":num(&v["database_tables"],0.0,100000.0),"http_ready":v["http_ready"]==true});
        }
    }
    r
}
fn scope(cfg: &Value) -> String {
    if let Some(value) = cfg["acceptance_scope"].as_str().filter(|v| {
        v.len() == 64
            && v.bytes()
                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
    }) {
        return value.into();
    }
    let v = json!({"data_mount":cfg["data_mount"],"media_mount":cfg["media_mount"],"external_backup_mount":cfg["external_backup_mount"],"storage":cfg["storage"],"review_settings":{"server_os":cfg["server_os"],"heartbeat":cfg["heartbeat"],"host_backup":cfg["host_backup"],"tv":cfg["tv"],"networking":cfg["networking"],"dashboard":cfg["dashboard"],"monitoring":cfg["monitoring"],"app_backup":cfg["app_backup"],"nextcloud":cfg["nextcloud"],"maintenance":cfg["maintenance"]},"runtime":{"backend":cfg["runtime"]["backend"]},"gateway":{"services":cfg["gateway"]["services"],"lan_ip":cfg["gateway"]["lan_ip"],"lan_cidr":cfg["gateway"]["lan_cidr"]},"services":cfg["media"]["services"].as_array().cloned().unwrap_or_default(),"photos":cfg["photos"]["deploy_immich"].as_bool().unwrap_or(false)});
    common::sha(&serde_json::to_vec(&v).unwrap())
}
fn manual(key: &str) -> Option<&'static str> {
    match key {
        "host_restore" => Some("Host and user restore rehearsal"),
        "outage_delivery" => Some("Independent outage alert"),
        "server_idle" => Some("Server idle and drive policy"),
        "notification_delivery" => Some("Phone notification delivery"),
        "app_restore" => Some("Media application restore rehearsal"),
        "missing_drive" => Some("Missing-drive protection"),
        "media_workflow" => Some("Media workflow"),
        "music_playback" => Some("Music streaming"),
        "photo_preservation" => Some("Photo deletion safety"),
        "independent_restore" => Some("Independent full restore"),
        "external_backup" => Some("External copy/reconnect"),
        "remote_permissions" => Some("Remote permissions"),
        "tv_session" => Some("TV and controller"),
        "os_recovery" => Some("OS recovery"),
        "gateway_clients" => Some("Application gateway clients"),
        "cloud_client" => Some("Personal cloud clients"),
        _ => None,
    }
}
fn acceptance(saved: &Value, cfg: &Value, now: f64) -> Value {
    let mut r = json!({"status":"unknown","profile":null,"generated_at":null,"automatic":{},"manual":{},"manual_checks":[]});
    let profile = text(&saved["profile"]);
    if saved["format"] != "neko-acceptance-v1"
        || ![
            "server_os",
            "notifications",
            "app_backup",
            "host_backup",
            "heartbeat",
            "foundation",
            "media",
            "music",
            "cloud",
            "photos",
            "full",
            "network",
            "networking",
            "tv",
            "dashboard",
            "media_monitor",
            "lan_access",
            "remote_access",
        ]
        .contains(&profile)
        || num(&saved["generated_at"], 0.0, now + 60.0).is_null()
    {
        return r;
    }
    r["profile"] = json!(profile);
    r["generated_at"] = saved["generated_at"].clone();
    if saved["version"] != VERSION || text(&saved["dashboard_scope"]) != scope(cfg) {
        r["status"] = json!("outdated");
        return r;
    }
    let mut valid = true;
    for group in ["automatic", "manual"] {
        let Some(rows) = saved[group].as_array() else {
            valid = false;
            continue;
        };
        if rows.is_empty() || rows.len() > 200 {
            valid = false;
            continue;
        }
        let mut counts = json!({"pass":0,"fail":0,"needs_setup":0,"warning":0,"blocked":0});
        for row in rows {
            let state = text(&row["status"]);
            if !row.is_object()
                || !["pass", "fail", "needs_setup", "warning", "blocked"].contains(&state)
            {
                valid = false;
                continue;
            }
            counts[state] = json!(counts[state].as_u64().unwrap_or(0) + 1);
            if group == "manual" {
                let Some(label) = manual(text(&row["id"])) else {
                    valid = false;
                    continue;
                };
                if state == "pass"
                    && (row["evidence"] != "user_reported"
                        || num(&row["confirmed_at"], 0.0, now + 60.0).is_null())
                {
                    valid = false;
                }
                r["manual_checks"]
                    .as_array_mut()
                    .unwrap()
                    .push(json!({"name":label,"status":state}));
            }
        }
        r[group] = counts;
    }
    r["status"] = json!(if !valid {
        "unknown"
    } else if now - saved["generated_at"].as_f64().unwrap() > 86400.0 {
        "stale"
    } else if ["automatic", "manual"]
        .iter()
        .any(|g| r[g]["fail"].as_u64().unwrap_or(0) > 0)
    {
        "failed"
    } else if saved["acceptance_complete"] == true
        && ["automatic", "manual"].iter().all(|g| r[g]
            .as_object()
            .unwrap()
            .values()
            .filter_map(Value::as_u64)
            .sum::<u64>()
            == r[g]["pass"].as_u64().unwrap_or(0))
    {
        "complete"
    } else {
        "pending"
    });
    r
}
/// Major app updates that monthly maintenance found but leaves for you to start
/// (read from the newest complete monthly snapshot on the HDD).
fn updates(cfg: &Value, media_ok: bool) -> Value {
    if !media_ok {
        return json!([]);
    }
    let media = Path::new(text(&cfg["media_mount"]));
    let folder = media.join("backups/monthly");
    let newest = crate::maintenance::monthly_runs(media)
        .unwrap_or_default()
        .into_iter()
        .rev()
        .map(|run| folder.join(run))
        .find(|path| path.join("manifest.json").is_file());
    let Some(run) = newest else {
        return json!([]);
    };
    let mut out = vec![];
    for (app, name) in [("immich", "Immich"), ("nextcloud", "Nextcloud")] {
        let pending = common::json(&run.join(format!("{app}-major-pending.json")), 64000);
        if pending["status"] == "manual_major_upgrade_required" {
            out.push(json!({"app":app,"name":name,"current":chars(text(&pending["current"]),40),"available":chars(text(&pending["available"]),40)}));
        }
    }
    json!(out)
}
fn alerts(s: &Value) -> Value {
    let mut out = vec![];
    let mut add = |id: String, level: &str, title: String, detail: &str| {
        out.push(json!({"id":id,"level":level,"title":title,"detail":detail}))
    };
    for unit in s["startup"].as_array().into_iter().flatten() {
        // Schedules can be off on purpose (the backup cards show those); the
        // services everything else depends on must start with the computer.
        let name = text(&unit["unit"]);
        let core = !name.ends_with(".timer") || name == "nekoserver-dashboard-collect.timer";
        if core && unit["enabled"] == false {
            add(
                format!("startup:{}", text(&unit["unit"])),
                "warning",
                format!("{} won't start after a restart", text(&unit["name"])),
                "It is installed but not set to start when the computer boots. Run setup again to fix it.",
            );
        }
    }
    if let Some(disks) = s["storage"].as_array() {
        for d in disks {
            let id = text(&d["id"]);
            let name = text(&d["name"]);
            if d["status"] != "mounted" {
                add(format!("mount:{id}"),"critical",format!("{name} unavailable"),"Mount missing or identity mismatch. Check the drive before restarting dependent services.");
            } else if d["used_percent"].as_f64().unwrap_or(0.0) >= 90.0 {
                add(
                    format!("space:{id}"),
                    if d["used_percent"].as_f64().unwrap_or(0.0) >= 97.0 {
                        "critical"
                    } else {
                        "warning"
                    },
                    format!("{name} is filling up"),
                    "Review free space before uploads or backups.",
                );
            }
            let state = text(&d["smart"]["status"]);
            if state == "fail" || state == "warning" {
                add(
                    format!("smart:{id}"),
                    if state == "fail" {
                        "critical"
                    } else {
                        "warning"
                    },
                    format!("{name} health warning"),
                    text(&d["smart"]["reason"]),
                );
            }
        }
    }
    let used = s["metrics"]["memory_percent"].as_f64().unwrap_or(0.0);
    if used >= 90.0 {
        add(
            "memory-pressure".into(),
            if used >= 97.0 { "critical" } else { "warning" },
            "Available RAM is low".into(),
            "Inspect application memory usage and swap; cached memory alone is normal.",
        );
    }
    let temp = s["metrics"]["temperature"].as_f64().unwrap_or(0.0);
    if temp >= 80.0 {
        add(
            "temperature".into(),
            if temp >= 95.0 { "critical" } else { "warning" },
            "CPU temperature is high".into(),
            "Check cooling and current workload.",
        );
    }
    if let Some(apps) = s["applications"].as_array() {
        for a in apps {
            if [
                "exited",
                "restarting",
                "dead",
                "missing",
                "ambiguous",
                "paused",
                "created",
            ]
            .contains(&text(&a["state"]))
                || a["health"] == "unhealthy"
            {
                add(
                    format!("app:{}", text(&a["id"])),
                    if a["health"] == "unhealthy" {
                        "critical"
                    } else {
                        "warning"
                    },
                    format!("{} needs attention", text(&a["name"])),
                    "Check its container and local application logs.",
                );
            }
        }
    }
    if let Some(jobs) = s["backups"].as_array() {
        for j in jobs {
            if ["fail", "stale", "partial"].contains(&text(&j["status"])) {
                add(
                    format!("backup:{}", text(&j["id"])),
                    if j["status"] == "fail" {
                        "critical"
                    } else {
                        "warning"
                    },
                    format!("{} needs attention", text(&j["name"])),
                    j["reason"]
                        .as_str()
                        .unwrap_or("Review the backup schedule."),
                );
            }
        }
    }
    if let Some(rows) = s["integrity"].as_array() {
        for c in rows {
            let count = |k: &str| c[k].as_u64().unwrap_or(0);
            let bad =
                count("damaged") + count("both_damaged") + count("disagree") + count("missing");
            if bad > 0 {
                let files = c["files"]
                    .as_array()
                    .map(|a| {
                        a.iter()
                            .filter_map(|f| f.as_str())
                            .collect::<Vec<_>>()
                            .join(", ")
                    })
                    .unwrap_or_default();
                add(
                    format!("integrity:{}", text(&c["id"])),
                    if count("both_damaged") + count("damaged") + count("missing") > 0 { "critical" } else { "warning" },
                    format!("{}: {bad} files are damaged or missing", text(&c["name"])),
                    &format!("Run  sudo bash install.sh --recover  and choose \"Repair damaged backup copies\". Files: {files}"),
                );
            }
        }
    }
    if let Some(rows) = s["updates"].as_array() {
        for u in rows {
            add(
                format!("major-update:{}", text(&u["app"])),
                "warning",
                format!(
                    "{} {} is available (you have {})",
                    text(&u["name"]),
                    text(&u["available"]),
                    text(&u["current"])
                ),
                "A major version is not installed automatically, because it can need extra steps. Read MONTHLY-MAINTENANCE.md, \"Major version updates\".",
            );
        }
    }
    if s["media"]["status"] == "stale" {
        add(
            "media-stale".into(),
            "warning",
            "Media checks are stale".into(),
            "Reapply media_monitor or inspect its scheduled job.",
        );
    }
    if s["media"]["counts"]["fail"].as_u64().unwrap_or(0) > 0 {
        add(
            "media-failure".into(),
            "warning",
            "Media connection check failed".into(),
            "Open the media checks below and inspect the relevant service.",
        );
    }
    json!(out)
}
fn notification_status(now: f64) -> Value {
    let saved = common::json(
        Path::new("/run/nekoserver-notifications/delivery.json"),
        64000,
    );
    let (code, output) = probe(
        &[
            "systemctl",
            "show",
            "nekoserver-notifications.timer",
            "--property=LoadState,ActiveState,NextElapseUSecRealtime",
        ],
        8,
    );
    let fields = output
        .lines()
        .filter_map(|line| line.split_once('='))
        .collect::<std::collections::BTreeMap<_, _>>();
    let timer = json!({"installed":code==0&&fields.get("LoadState")==Some(&"loaded"),"active":fields.get("ActiveState")==Some(&"active"),"next_run":fields.get("NextElapseUSecRealtime").filter(|s|!s.is_empty()).map(|s|chars(s,80))});
    let mut status = if saved["format"] == "neko-delivery-v1"
        && ["pass", "fail", "unknown"].contains(&text(&saved["status"]))
    {
        text(&saved["status"])
    } else {
        "unknown"
    };
    if timer["installed"] == true
        && saved["generated_at"]
            .as_f64()
            .is_none_or(|t| t > now + 30.0 || now - t > 180.0)
    {
        status = "stale";
    }
    json!({"status":status,"timer":timer,"last_success":num(&saved["last_success"],0.0,now+30.0),"last_attempt":num(&saved["last_attempt"],0.0,now+30.0),"pending":num(&saved["pending"],0.0,100.0)})
}
pub fn run(a: &Args) -> Result<()> {
    common::require_root()?;
    let cfg = common::json(&a.path("config")?, 2_000_000);
    let public = common::safe(Path::new(
        &a.value("output", "/run/nekoserver-dashboard/status.json"),
    ))?;
    let internal = common::safe(&a.path("state")?)?;
    for key in ["data_mount", "media_mount", "external_backup_mount"] {
        common::mount_path(text(&cfg[key]))?;
    }
    if internal != Path::new(text(&cfg["data_mount"])).join("dashboard-state") {
        return Err(Error::new("Collector state must use application SSD"));
    }
    let parent = public
        .parent()
        .ok_or_else(|| Error::new("Missing output parent"))?;
    let _lock = common::lock(&parent.join("collector.lock"))?;
    let now = common::now();
    let data = Path::new(text(&cfg["data_mount"]));
    let first = mount_info(
        data,
        Some(text(&cfg["storage"]["data_ssd"]["uuid"])),
        text(&cfg["storage"]["data_ssd"]["fstype"]),
    );
    let previous = if first["status"] == "mounted" {
        common::json(&internal.join("collector.json"), 2_000_000)
    } else {
        empty()
    };
    let mut cache = previous["smart"]
        .as_object()
        .map(|m| {
            json!(m
                .iter()
                .rev()
                .take(20)
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect::<serde_json::Map<_, _>>())
        })
        .unwrap_or_else(empty);
    let (disks, ok, media_ok) = storage(&cfg, &mut cache, now)?;
    let (host, cpu) = metrics(&previous["cpu"]);
    let mut history=previous["history"].as_array().map(|rows|rows.iter().rev().take(119).filter_map(|v|{let t=num(&v["at"],0.0,now).as_f64()?;if now-t>7200.0{return None}Some(json!({"at":v["at"],"cpu":num(&v["cpu"],0.0,100.0),"memory":num(&v["memory"],0.0,100.0),"temperature":num(&v["temperature"],-10.0,150.0)}))}).collect::<Vec<_>>()).unwrap_or_default();
    history.reverse();
    history.push(json!({"at":now,"cpu":host["cpu_percent"],"memory":host["memory_percent"],"temperature":host["temperature"]}));
    let (apps, containers) = applications(&cfg);
    let mut result = json!({"format":"neko-dashboard-v1","version":VERSION,"generated_at":now,"metrics":host,"history":history,"storage":disks,"applications":apps,"backups":backups(&cfg,now,ok),"media":media(&cfg,now,ok),"recovery":recovery(&cfg,ok,media_ok),"heartbeat":heartbeat(&cfg,now),"acceptance":acceptance(&if ok{common::json(&data.join("reports/acceptance/latest.json"),2_000_000)}else{empty()},&cfg,now)});
    result["notifications"] = notification_status(now);
    result["containers"] = containers;
    result["gpu_telemetry"] = gpu_telemetry();
    result["startup"] = startup();
    result["updates"] = updates(&cfg, media_ok);
    result["integrity"] = integrity(data, ok);
    result["alerts"] = alerts(&result);
    if ok {
        let check = mount_info(
            data,
            Some(text(&cfg["storage"]["data_ssd"]["uuid"])),
            text(&cfg["storage"]["data_ssd"]["fstype"]),
        );
        if check["status"] != "mounted" {
            return Err(Error::new("Storage changed before state write"));
        }
        fs::create_dir_all(&internal)?;
        fs::set_permissions(&internal, fs::Permissions::from_mode(0o700))?;
        common::atomic_json(
            &internal.join("collector.json"),
            &json!({"cpu":cpu,"history":result["history"],"smart":cache}),
        )?;
    }
    let mut b = serde_json::to_vec(&result)?;
    b.push(b'\n');
    common::atomic(&public, &b, 0o644)?;
    Ok(())
}

#[cfg(test)]
mod os_tests {
    use super::*;
    #[test]
    fn docker_stats_become_per_app_usage() {
        assert_eq!(docker_size("512MiB"), Some(536870912.0));
        assert_eq!(docker_size("1.5GiB"), Some(1610612736.0));
        assert_eq!(docker_size("800kB"), Some(800000.0));
        assert_eq!(docker_size("lots"), None);
        let rows = docker_stats("{\"ID\":\"0123456789abcdef\",\"CPUPerc\":\"12.50%\",\"MemUsage\":\"256MiB / 7.6GiB\"}\nnot json\n{\"ID\":\"short\",\"CPUPerc\":\"1%\",\"MemUsage\":\"1MiB / 1GiB\"}");
        assert_eq!(rows.len(), 1);
        assert_eq!(rows["0123456789ab"], (12.5, 268435456.0));
        let labels = [(
            "com.docker.compose.project".to_string(),
            "neko-immich".to_string(),
        )]
        .into_iter()
        .collect();
        assert_eq!(app_of(&labels), Some("immich"));
        let other = [(
            "com.docker.compose.project".to_string(),
            "someone-else".to_string(),
        )]
        .into_iter()
        .collect();
        assert_eq!(app_of(&other), None);
    }
    #[test]
    fn nvidia_rows_and_startup_units_are_read() {
        let gpus = parse_nvidia(
            "00000000:01:00.0, NVIDIA GeForce RTX 4060, 23, 1024, 8188\nbroken line\n",
        );
        assert_eq!(gpus.len(), 1);
        assert_eq!(gpus[0]["percent"], 23.0);
        assert_eq!(gpus[0]["memory_used"], 1073741824.0);
        let units = parse_startup("Id=docker.service\nLoadState=loaded\nUnitFileState=disabled\nActiveState=active\n\nId=tailscaled.service\nLoadState=not-found\nUnitFileState=\nActiveState=inactive\n\nId=nekoserver-dashboard.service\nLoadState=loaded\nUnitFileState=enabled\nActiveState=active");
        assert_eq!(
            units.len(),
            2,
            "uninstalled units are not listed: {units:?}"
        );
        assert_eq!(units[0]["enabled"], false);
        assert_eq!(units[1]["name"], "This dashboard");
        let shown = alerts(&json!({ "startup": units }));
        let row = shown
            .as_array()
            .unwrap()
            .iter()
            .find(|a| a["id"] == "startup:docker.service")
            .unwrap();
        assert_eq!(row["title"], "Apps (Docker) won't start after a restart");
    }
    #[test]
    fn damaged_backup_copies_reach_the_dashboard_and_raise_an_alert() {
        let data =
            std::env::temp_dir().join(format!("neko-collect-integrity-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&data);
        let report = crate::backup::Integrity {
            unverified: 7,
            damaged: vec!["upload/u/b.jpg".into()],
            ..Default::default()
        };
        crate::backup::save_integrity(&data, "usb_photos", &report).unwrap();
        crate::backup::save_integrity(&data, "archive", &Default::default()).unwrap();
        let rows = integrity(&data, true);
        assert_eq!(rows.as_array().unwrap().len(), 2);
        let usb = rows
            .as_array()
            .unwrap()
            .iter()
            .find(|r| r["id"] == "usb_photos")
            .unwrap();
        assert_eq!(
            (usb["damaged"].as_u64(), usb["unverified"].as_u64()),
            (Some(1), Some(7))
        );
        let shown = alerts(&json!({"integrity": rows}));
        let shown = shown.as_array().unwrap();
        assert_eq!(shown.len(), 1);
        assert_eq!(shown[0]["level"], "critical");
        assert!(text(&shown[0]["detail"]).contains("upload/u/b.jpg"));
        assert!(text(&shown[0]["detail"]).contains("Repair damaged backup copies"));
        let _ = std::fs::remove_dir_all(data);
    }
    #[test]
    fn major_updates_found_by_maintenance_are_alerted() {
        let root = std::env::temp_dir().join(format!("neko-updates-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        for run in ["100-1", "200-1"] {
            fs::create_dir_all(root.join("backups/monthly").join(run)).unwrap();
            fs::write(
                root.join("backups/monthly").join(run).join("manifest.json"),
                "{}",
            )
            .unwrap();
        }
        fs::write(
            root.join("backups/monthly/100-1/immich-major-pending.json"),
            r#"{"status":"manual_major_upgrade_required","current":"v1.9.0","available":"v2.0.1"}"#,
        )
        .unwrap();
        fs::write(
            root.join("backups/monthly/200-1/nextcloud-major-pending.json"),
            r#"{"status":"manual_major_upgrade_required","current":"31","available":"32"}"#,
        )
        .unwrap();
        let cfg = json!({"media_mount": root.to_string_lossy()});
        // Only the newest snapshot counts: the Immich notice is from an older month.
        let found = updates(&cfg, true);
        assert_eq!(found.as_array().unwrap().len(), 1);
        assert_eq!(found[0]["app"], "nextcloud");
        let shown = alerts(&json!({ "updates": found }));
        assert!(text(&shown[0]["title"]).contains("Nextcloud 32 is available"));
        assert_eq!(updates(&cfg, false), json!([]));
        let _ = fs::remove_dir_all(root);
    }
    #[test]
    fn failed_job_says_why_and_where() {
        assert_eq!(
            failure_reason(&json!({"stage":"copying photos and videos to the USB drive","reason":"Storage operation failed: mount exited with 32 (wrong fs type)"})),
            "Failed while copying photos and videos to the USB drive: Storage operation failed: mount exited with 32 (wrong fs type)"
        );
        assert_eq!(
            failure_reason(&json!({})),
            "Last run failed; inspect its service logs"
        );
    }
    #[test]
    fn partial_backup_is_a_warning_with_its_files() {
        let reason = partial_reason(
            &json!({"in_use":1,"unsupported_names":1,"files":["alice/open.docx","bob/a:b.txt"]}),
        );
        assert!(reason.starts_with("2 files were not copied"));
        assert!(reason.contains("alice/open.docx"));
        let db = partial_reason(
            &json!({"in_use":0,"unsupported_names":0,"files":[],"database":"the newest Immich database copy is 10 days old"}),
        );
        assert_eq!(db, "The newest Immich database copy is 10 days old");
        let shown = alerts(
            &json!({"backups":[{"id":"external","name":"USB backup","status":"partial","reason":reason}]}),
        );
        assert_eq!(shown[0]["level"], "warning");
        assert!(text(&shown[0]["detail"]).contains("bob/a:b.txt"));
    }
    #[test]
    fn low_available_memory_is_alerted() {
        let rows = alerts(&json!({"metrics":{"memory_percent":95}}));
        assert!(rows
            .as_array()
            .unwrap()
            .iter()
            .any(|v| v["id"] == "memory-pressure" && v["level"] == "warning"));
    }
    #[test]
    fn normal_or_unknown_memory_does_not_alert() {
        for percent in [json!(20), Value::Null] {
            let rows = alerts(&json!({"metrics":{"memory_percent":percent}}));
            assert!(!rows
                .as_array()
                .unwrap()
                .iter()
                .any(|v| v["id"] == "memory-pressure"));
        }
    }
}
