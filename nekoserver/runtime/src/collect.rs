/// Every monitored app (shared.json); notify.rs names an alert for each (checked by a test).
pub(crate) use crate::common::APPS;
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
/// Backup trees whose checksum checks the dashboard shows (reports/integrity.json).
pub(crate) const CHECKS: [(&str, &str); 3] = [
    ("archive", "HDD photo archive"),
    ("usb_photos", "USB photo copy"),
    ("usb_nextcloud", "USB Nextcloud copy"),
];
/// What the last checksum checks of each backup tree found. `away` names the drive that
/// is missing (None while both are there): the HDD archive can't be checked or kept up to
/// date without the HDD, so its row says so instead of an old "matches".
/// The checksum checks' results. A USB copy's are shown only when they were made on the USB
/// drive the settings name now (`usb`): a new drive has had no check of its own yet.
fn integrity(data: &Path, ok: bool, away: Option<&str>, usb: &str) -> Value {
    let all = if ok {
        common::json(&data.join("reports/integrity.json"), 20_000_000)
    } else {
        empty()
    };
    let mut rows = vec![];
    for (id, name) in CHECKS {
        let s = &all["sections"][id];
        if !s.is_object() || !made_on_this_drive(id, s, usb) {
            continue;
        }
        let count = |key: &str| s["counts"][key].as_u64().unwrap_or(0);
        // Counts only: the status file can be read by every account on the server, so the
        // names of the person's own files stay in the root-only integrity report (the
        // repair menu lists them).
        // When every copy was last compared with its checksum: a quick run (size and
        // dates) proves nothing about copies already there. None before 0.39.6.
        let full_at = s["full_at"].as_f64().filter(|t| t.is_finite() && *t > 0.0);
        rows.push(json!({"id": id, "name": name, "at": s["at"], "full_at": full_at,
            "check": if s["check"] == "quick" { "quick" } else if s["check"] == "full" { "full" } else { "unknown" },
            "unverified": s["unverified"].as_u64().unwrap_or(0),
            "repaired": s["repaired"].as_u64().unwrap_or(0),
            "damaged": count("damaged"), "both_damaged": count("both_damaged"), "disagree": count("disagree"),
            "missing": count("missing")}));
        if id == "archive" {
            if let (Some(row), Some(drive)) = (rows.last_mut(), away) {
                row["blocked"] = json!(format!(
                    "The {drive} is missing, so this copy can't be checked or kept up to date until it is back."
                ));
            }
        }
    }
    json!(rows)
}
/// A tree's check result belongs to the drive the settings name now. A USB copy's
/// result counts only when it names that drive: one without a drive id was written
/// before ids were kept and may be any drive's, the same rule save_integrity_on keeps
/// (the review of 0.47.10: a replaced drive showed the old one's full check). With no
/// USB drive set, and for the HDD archive, only a result that names another drive is left out.
fn made_on_this_drive(id: &str, s: &Value, usb: &str) -> bool {
    let drive = s["drive"].as_str();
    if id.starts_with("usb_") && !usb.is_empty() {
        return drive == Some(usb);
    }
    !drive.is_some_and(|drive| drive != usb)
}
/// Every monitored job; notify.rs names an alert for each (checked by a test).
pub(crate) const JOBS: [(&str, &str, &str, f64); 10] = [
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
        "USB copy (photos and files)",
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
        "Monthly check and snapshot",
        "nekoserver-maintenance",
        3024000.0,
    ),
    (
        "updates",
        "Weekly app updates",
        "nekoserver-updates",
        777600.0,
    ),
    (
        "update_check",
        "Daily update check",
        "nekoserver-update-check",
        216000.0,
    ),
    // Started by you only: never overdue.
    (
        "upgrade",
        "NekoServer upgrade",
        "nekoserver-upgrade",
        f64::MAX,
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
                                    // The chip's own critical limit, when it reports one.
                                    let crit = fs::read_to_string(
                                        hw.path().join(name.replace("_input", "_crit")),
                                    )
                                    .ok()
                                    .and_then(|c| c.trim().parse::<f64>().ok())
                                    .map(|c| c / 1000.0)
                                    .filter(|c| (40.0..=150.0).contains(c));
                                    if (-10.0..=150.0).contains(&v) {
                                        temps.push((v, crit))
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    if let Some((v, crit)) = temps.into_iter().max_by(|a, b| a.0.total_cmp(&b.0)) {
        host["temperature"] = json!(round(v));
        host["temperature_crit"] = json!(crit);
    }
    (host, current)
}
/// mount_info for drive `key`; the app SSD and the media HDD also pass the check every job
/// makes (UUID, filesystem, Btrfs subvolume, the NAS disk's WWN), or they count as the
/// wrong drive: nothing is read from them or written to them. With the UUID and filesystem
/// alone, the page showed the reports of a drive every backup refused as current (the
/// review of 0.47.2).
fn app_drive(cfg: &Value, key: &str, path: &Path, expected: Option<&str>, fstype: &str) -> Value {
    let info = mount_info(path, expected, fstype);
    let checked = key == "os" || crate::backup::checked_drive(cfg, key, path).is_ok();
    if info["status"] == "mounted" && !checked {
        json!({"status": "wrong_drive"})
    } else {
        info
    }
}
fn mount_info(path: &Path, expected: Option<&str>, fstype: &str) -> Value {
    if expected == Some("") {
        return json!({"status":"not_configured"});
    }
    let s = match common::drive_at(path, 8) {
        Ok(found) => found.unwrap_or_default(),
        // findmnt killed after its 8 s: it reads the filesystem's label block, which a
        // drive saturated by a backup can hold back. The kernel's mount table says the
        // same without touching the drive (the 0.47.10 round: the page called the HDD
        // missing, and every backup blocked, each time a copy kept the drive busy).
        Err(_) => common::mount_table_entry(path).unwrap_or_default(),
    };
    let v = s.split_whitespace().collect::<Vec<_>>();
    if v.len() < 3 {
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
    // A drive with no sensor (a virtual disk, some USB and SAS drives) says 0: that is no
    // reading, and the page showed "0 °C" for it.
    r["temperature"] = num(&v["temperature"]["current"], 1.0, 150.0);
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
        return json!({"status":old["status"],"temperature":num(&old["temperature"],1.0,150.0),"wear_percent":num(&old["wear_percent"],0.0,1000.0),"checked_at":old["checked_at"],"reason":if reasons.contains(&text(&old["reason"])){text(&old["reason"])}else{"Inspect drive health"}});
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
/// Drives whose space check has not answered yet (a hung drive): not asked again until
/// that one answers, so a stuck drive never piles up waiting threads.
static ASKING: std::sync::Mutex<Vec<PathBuf>> = std::sync::Mutex::new(Vec::new());
/// `ask()` about `key`, on a thread of its own: None when it gives no answer within
/// `max` (it is left to finish alone) or is still busy with an earlier question.
fn answered_within<T: Send + 'static>(
    key: &Path,
    max: std::time::Duration,
    ask: impl FnOnce() -> Option<T> + Send + 'static,
) -> Option<T> {
    {
        let mut asking = ASKING.lock().ok()?;
        if asking.iter().any(|p| p == key) {
            return None;
        }
        asking.push(key.to_path_buf());
    }
    let owned = key.to_path_buf();
    let handle = std::thread::spawn(move || {
        let answer = ask();
        if let Ok(mut asking) = ASKING.lock() {
            asking.retain(|p| *p != owned);
        }
        answer
    });
    common::joined(handle, max).flatten()
}
/// A drive's space. Asking a hung drive blocks until it answers, so the question is
/// asked aside: no answer within 5 seconds shows the drive as unavailable, and the
/// rest of the dashboard carries on.
fn usage(path: &Path) -> Option<(u64, u64, bool)> {
    let owned = path.to_path_buf();
    answered_within(path, std::time::Duration::from_secs(5), move || {
        statvfs_usage(&owned)
    })
}
/// (space, used, mounted read-only): a drive that met write errors (a NAS disk that lost
/// its connection for long, say) is turned read-only by Linux, and the apps' writes fail.
fn statvfs_usage(path: &Path) -> Option<(u64, u64, bool)> {
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;
    let s = CString::new(path.as_os_str().as_bytes()).ok()?;
    let mut stat = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    if unsafe { libc::statvfs(s.as_ptr(), stat.as_mut_ptr()) } != 0 {
        return None;
    }
    let stat = unsafe { stat.assume_init() };
    let (total, used) = space(stat.f_blocks, stat.f_bfree, stat.f_bavail, stat.f_frsize)?;
    let mounts = fs::read_to_string("/proc/self/mountinfo").unwrap_or_default();
    Some((total, used, filesystem_read_only(&mounts, path)))
}
/// Whether the filesystem mounted at `path` is itself read-only, from a mountinfo list:
/// the filesystem's own options (after the " - ", its type and its source), never the
/// mount's (the sixth field). This collector runs with ProtectSystem=strict, so in its
/// own view every drive is a read-only mount, and statvfs (ST_RDONLY) says so too: every
/// server with a drive of its own showed "turned read-only" for the SSD and the HDD, as
/// critical alerts (seen on the rig, 0.46.2 and 0.46.3). What Linux does after write
/// errors is turn the filesystem read-only, which shows here. The last line for the
/// folder counts (a later mount covers an earlier one).
fn filesystem_read_only(mountinfo: &str, path: &Path) -> bool {
    let wanted = path.to_string_lossy();
    mountinfo
        .lines()
        .filter_map(|line| {
            let (mount, filesystem) = line.split_once(" - ")?;
            (mount.split(' ').nth(4)? == wanted).then_some(filesystem)
        })
        .next_back()
        .and_then(|filesystem| filesystem.split(' ').nth(2))
        .is_some_and(|options| options.split(',').any(|o| o == "ro"))
}
/// (space the apps can use, used), as `df` counts them: the part of the drive kept for
/// the system (5% on ext4 unless changed) is not free for the apps, so a drive they
/// have filled reads 100%, not 95%.
fn space(blocks: u64, free: u64, available: u64, block: u64) -> Option<(u64, u64)> {
    let used = blocks.checked_sub(free)?.checked_mul(block)?;
    Some((
        used.checked_add(available.min(free).checked_mul(block)?)?,
        used,
    ))
}
/// Said of a drive on a NAS in place of its SMART health.
const NAS_HEALTH: &str =
    "Its disks' health (SMART) and RAID are watched by the network storage box: keep the NAS's own alerts on";
/// The drive `key` was chosen on a network storage box (storage.<key>.network, setup).
fn on_nas(cfg: &Value, key: &str) -> bool {
    key != "os" && cfg["storage"][key]["network"].is_object()
}
/// A drive on a NAS: marked so, and what the NAS check (nas_watch.rs) found when it last
/// ran: it runs as its own service (this collector sees the drives read-only), and one
/// older than three minutes says nothing. Linux still shows an aborted ext4 as mounted
/// "rw" (statvfs says so too, seen in the lab): only a write tells.
fn nas_drive(r: &mut Value, key: &str, now: f64) {
    r["network"] = json!(true);
    if r["status"] != "mounted" {
        return;
    }
    let seen = common::json(Path::new(*crate::nas_watch::STATE), 64000);
    match nas_verdict(&seen, key, now, nas_check_busy) {
        Some("fails") => r["read_only"] = json!(true),
        Some("waits") => r["waiting"] = json!(true),
        _ => {}
    }
    if seen["held"] == true {
        r["held_by_nas_watch"] = json!(true);
    }
}
/// Seconds after which readings taken every `interval` seconds count as late: two
/// intervals and 30 s for a slow run, never under 3 minutes (so a slow minute never
/// alerts). Readers also allow readings up to LATE_AHEAD s ahead, for a clock a little off.
pub(crate) fn late_after(interval: u64) -> u64 {
    (2 * interval + 30).max(180)
}
pub(crate) const LATE_AHEAD: f64 = 60.0;
/// What the NAS check found for drive `key`: its record when recent; with an old record and
/// a check still at work (`busy`), "waits": its test write hangs, as on a NAS that does not
/// answer (the review of 0.47.0: "waiting" went, and the phone was told the drive was back).
fn nas_verdict<'a>(
    seen: &'a Value,
    key: &str,
    now: f64,
    busy: impl Fn() -> bool,
) -> Option<&'a str> {
    let fresh = seen["at"]
        .as_f64()
        .is_some_and(|t| (now - 180.0..=now + 60.0).contains(&t));
    if fresh {
        seen["drives"][key].as_str()
    } else {
        busy().then_some("waits")
    }
}
/// The NAS check is at work right now (its service starting: a check runs as a oneshot).
fn nas_check_busy() -> bool {
    common::command(
        &[
            "systemctl",
            "is-active",
            common::shared("nas_watch", "service"),
        ],
        5,
    )
    .is_ok_and(|(_, state)| state.trim() == "activating")
}
/// The NAS connections this PC keeps (network_storage.targets in the settings) and how
/// each is, from Linux's iSCSI sessions (/sys/class/iscsi_session/session<n>): null when
/// no drive is on a NAS.
fn network_storage(cfg: &Value, sessions: &Path) -> Value {
    let wanted: Vec<&str> = cfg["network_storage"]["targets"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .filter(|t| t.len() <= 223 && t.bytes().all(|b| b.is_ascii_graphic()))
        .collect();
    if cfg["network_storage"]["wanted"] != true || wanted.is_empty() {
        return Value::Null;
    }
    let mut seen = std::collections::BTreeMap::new();
    for entry in fs::read_dir(sessions).into_iter().flatten().flatten() {
        let read = |name: &str| {
            common::bytes(&entry.path().join(name), 4096)
                .map(|b| String::from_utf8_lossy(&b).trim().to_string())
                .unwrap_or_default()
        };
        seen.insert(read("targetname"), read("state"));
    }
    let targets: Vec<Value> = wanted
        .iter()
        .map(|t| {
            let state = seen.get(*t).map(String::as_str).unwrap_or("");
            json!({"target": t, "state": match state {
                "LOGGED_IN" => "connected",
                "FAILED" | "FREE" => "lost",
                "" => "not_connected",
                _ => "reconnecting",
            }})
        })
        .collect();
    json!({"portal": chars(text(&cfg["network_storage"]["portal"]), 80), "targets": targets})
}
/// A mounted drive's row filled in: its size and use, read-only, filesystem errors and
/// health. `drive`: (id, folder, device, filesystem type, expected UUID).
fn mounted_details(
    r: &mut Value,
    cfg: &Value,
    cache: &mut Value,
    now: f64,
    drive: (&str, &Path, &str, &str, Option<&str>),
) {
    let (key, path, source, fs, expected) = drive;
    match usage(path) {
        Some((total, used, read_only)) if total > 0 => {
            r["total"] = json!(total);
            r["used"] = json!(used);
            r["used_percent"] = json!(round(100.0 * used as f64 / total as f64));
            if read_only && key != "os" {
                r["read_only"] = json!(true);
            }
        }
        _ => r["status"] = json!("unavailable"),
    }
    // Errors Linux met on the filesystem (a cable, a NAS away for long): it may have
    // turned read-only, or may still hold half-written changes.
    if fs == "ext4" || key == "os" {
        if let Some(n) = common::ext4_errors(source, Path::new("/sys/fs/ext4")).filter(|n| *n > 0) {
            r["fs_errors"] = json!(n);
        }
    }
    r["smart"] = if on_nas(cfg, key) {
        // This PC sees no SMART or RAID state through iSCSI: the NAS does.
        json!({"status":"nas","reason":NAS_HEALTH,"checked_at":null})
    } else {
        smart(
            source,
            expected.unwrap_or(source),
            cache,
            now,
            cfg["dashboard"]["smart_interval_seconds"]
                .as_f64()
                .unwrap_or(1800.0),
        )
    };
}
fn storage(cfg: &Value, cache: &mut Value, now: f64) -> Result<(Value, Vec<Value>, bool, bool)> {
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
    let mut mounted = vec![];
    let mut data_ok = false;
    let mut media_ok = false;
    for (key, name, path, expected, fs) in rows {
        let info = app_drive(cfg, key, &path, expected, fs);
        let mut r = json!({"id":key,"name":name,"mount":path,"status":info["status"],"total":null,"used":null,"used_percent":null,"smart":{"status":"unknown","checked_at":null}});
        if info["status"] == "mounted" {
            let source = text(&info["source"]);
            mounted_details(&mut r, cfg, cache, now, (key, &path, source, fs, expected));
            mounted.push(json!({"id":key,"name":name,"source":source,"mount":path,"fstype":fs,"uuid":expected.unwrap_or("")}));
        }
        if on_nas(cfg, key) {
            nas_drive(&mut r, key, now);
        }
        if key == "data_ssd" {
            data_ok = r["status"] == "mounted";
        }
        if key == "media_hdd" {
            media_ok = r["status"] == "mounted";
        }
        disks.push(r)
    }
    Ok((json!(disks), mounted, data_ok, media_ok))
}
/// The NekoServer drives (by their ids and names) an array holds: the SSD mounted from
/// /dev/md0 belongs to the RAID1 row md0, so its alerts can say which drive is at risk.
fn locate_arrays(rows: &mut [Value], mounted: &[Value]) {
    for row in rows.iter_mut() {
        let Some(name) = row["id"].as_str().and_then(|id| id.strip_prefix("md-")) else {
            continue;
        };
        let pattern = Regex::new(&format!("^/dev/{}(p[0-9]+)?$", regex::escape(name))).unwrap();
        let holders: Vec<&Value> = mounted
            .iter()
            .filter(|m| pattern.is_match(text(&m["source"])))
            .collect();
        row["holds"] = json!(holders.iter().map(|m| m["id"].clone()).collect::<Vec<_>>());
        row["holds_names"] = json!(holders
            .iter()
            .map(|m| m["name"].clone())
            .collect::<Vec<_>>());
    }
}
/// A member drive's SMART health, folded into its array row: a member whose SMART fails
/// marks the whole array as failing, so the drive is replaced before it takes the array
/// down with it.
fn member_smart(row: &mut Value, cache: &mut Value, now: f64, interval: f64) {
    let mut failing: Option<String> = None;
    for member in row["members"].as_array_mut().into_iter().flatten() {
        let device = text(&member["device"]).to_string();
        if device.is_empty() {
            continue;
        }
        let health = smart(
            &format!("/dev/{device}"),
            &format!("array-member:{device}"),
            cache,
            now,
            interval,
        );
        if health["status"] == "fail" && failing.is_none() {
            failing = Some(format!(
                "SMART health failed on {device} ({}): replace that drive before it takes the array down",
                text(&health["reason"])
            ));
        }
        member["smart"] = json!({"status": health["status"], "reason": health["reason"]});
    }
    if let Some(reason) = failing {
        if ["healthy", "warning", "rebuilding"].contains(&text(&row["status"])) {
            row["status"] = json!("failing");
            row["reason"] = json!(reason);
        }
    }
}
/// The health of every RAID array and Btrfs pool: the snapshot's "arrays" rows.
fn arrays_section(mounted: &[Value], cache: &mut Value, now: f64, interval: f64) -> Value {
    let mut rows = crate::array_health::md_arrays(Path::new("/sys/block"));
    locate_arrays(&mut rows, mounted);
    for m in mounted {
        if m["fstype"] != "btrfs" || text(&m["uuid"]).is_empty() {
            continue;
        }
        let uuid = text(&m["uuid"]).to_string();
        let sys = Path::new("/sys/fs/btrfs").join(&uuid);
        let mut missing = vec![];
        let mut devices = 0;
        for entry in std::fs::read_dir(sys.join("devinfo"))
            .into_iter()
            .flatten()
            .flatten()
        {
            devices += 1;
            let flag = std::fs::read_to_string(entry.path().join("missing")).unwrap_or_default();
            if flag.trim() == "1" {
                missing.push(format!("device {}", entry.file_name().to_string_lossy()));
            }
        }
        let (code, stats) = common::probe(&["btrfs", "device", "stats", text(&m["mount"])], 15);
        let mut row =
            crate::array_health::btrfs_pool(&uuid, (code == 0).then_some(stats.as_str()), &missing);
        // Only a pool, or a drive with something to say, earns a row of its own: a
        // healthy single Btrfs drive's health is already on its drive card. A pool whose
        // counters can't be read stays listed as unknown (the kernel still lists its
        // drives): dropped, its open alert would be taken as stopped on purpose.
        let members = row["members"].as_array().map(|m| m.len()).unwrap_or(0);
        if !shown_as_pool(members, devices, text(&row["status"])) {
            continue;
        }
        row["holds"] = json!([m["id"]]);
        row["holds_names"] = json!([m["name"]]);
        row["name"] = json!(format!("Btrfs drives of the {}", text(&m["name"])));
        rows.push(row);
    }
    for row in rows.iter_mut() {
        if row["id"].as_str().is_some_and(|id| id.starts_with("md-")) {
            member_smart(row, cache, now, interval);
        }
    }
    json!(rows)
}
/// Whether a Btrfs filesystem gets a row of its own: a pool (two drives or more, by its
/// counters or by the kernel's list), or one with something to say.
fn shown_as_pool(members: usize, devices: usize, status: &str) -> bool {
    members.max(devices) >= 2 || !["healthy", "unknown"].contains(&status)
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
/// The apps with their state and health, and each container's app and Compose service
/// (the dashboard names processes by them, and keeps databases out of reach).
/// `data` is the app SSD while it is mounted: an app setup never got to (no Compose file
/// there, and no container) is "not_set_up", not an app that stopped working.
fn applications(
    cfg: &Value,
    report: Option<&MediaReport>,
    data: Option<&Path>,
) -> (Value, Value, Value) {
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
    let mut services = serde_json::Map::new();
    for (labels, v) in &records {
        if let (Some(app), id) = (app_of(labels), text(&v["ID"])) {
            if id.len() >= 12 {
                containers.insert(id[..12].to_string(), json!(app));
                let service = labels
                    .get("com.docker.compose.service")
                    .map_or("", String::as_str);
                services.insert(id[..12].to_string(), json!(chars(service, 64)));
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
    // When each running app container last started, exactly (Docker's own "Up 5
    // minutes" is rounded down, which could make a newer check look older than it).
    let running: Vec<&str> = records
        .iter()
        .filter(|(labels, v)| v["State"] == "running" && app_of(labels).is_some())
        .map(|(_, v)| text(&v["ID"]))
        .filter(|id| !id.is_empty() && id.bytes().all(|b| b.is_ascii_hexdigit()))
        .collect();
    let starts = if running.is_empty() {
        Default::default()
    } else {
        let mut args = vec![
            "docker",
            "--host",
            "unix:///var/run/docker.sock",
            "inspect",
            "--format",
            "{{.Id}} {{.State.StartedAt}}",
        ];
        args.extend(&running);
        let (code, out) = probe(&args, 15);
        if code == 0 {
            started_at(&out)
        } else {
            Default::default()
        }
    };
    let now = common::now();
    let selected = common::media_services(cfg);
    let mut out = vec![];
    for &(app, name, description, port) in APPS.iter() {
        if !selected.contains(&app)
            && !(app == "immich" && common::immich_wanted(cfg))
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
        let mut up_for = None;
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
            let id = text(&v["ID"]);
            up_for = starts
                .iter()
                .find(|(full, _)| !id.is_empty() && full.starts_with(id))
                .map(|(_, at)| (now - at).max(0.0));
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
        } else if data.is_some_and(|d| !set_up(d, app)) {
            state = "not_set_up";
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
        // Its name, description and ports from the shared list: the page shows these
        // (it kept a copy of its own, already worded differently).
        out.push(json!({"id":app,"name":name,"description":description,"state":state,"health":health,"responding":responding(report,app,up_for),"url":format!("http://127.0.0.1:{port}/"),"port":port,"tailnet":common::tailnet_port(app),"cpu_percent":cpu,"memory_used":memory}));
    }
    (
        json!(out),
        Value::Object(containers),
        Value::Object(services),
    )
}
/// Whether the TV step (Steam Big Picture) is set up: its report is on the app SSD
/// (modules/tv.py writes it once the TV shortcuts are in place) and the TV was not
/// switched off since ("wanted": false under "tv", which the collector's settings carry,
/// modules/dashboard.py collector_settings). Not said while the SSD is missing, so the
/// page keeps what it showed.
fn tv(cfg: &Value, data: Option<&Path>) -> Value {
    let on = cfg["tv"]["wanted"] != false;
    match data {
        Some(d) => json!({"set_up": on && d.join("reports/tv-setup.json").is_file()}),
        None if !on => json!({"set_up": false}),
        None => json!({"set_up": null}),
    }
}
/// The AI assistant, for the dashboard's Ask Neko page (shown only when it is set up):
/// chosen in setup ("wanted" under "assistant", which the collector's settings carry)
/// and its model named in setup's report on the app SSD, with the memory and processor
/// share it may use. Not said while the SSD is missing, so the page keeps what it showed.
fn assistant(cfg: &Value, data: Option<&Path>) -> Value {
    let chosen = cfg["assistant"]["wanted"] == true;
    let Some(d) = data else {
        return json!({"set_up": if chosen { Value::Null } else { json!(false) }});
    };
    let report = common::json(&d.join("reports/assistant.json"), 64000);
    match report["model"].as_str() {
        Some(model) if chosen => json!({"set_up": true, "model": chars(model, 64),
            "memory": chars(report["memory"].as_str().unwrap_or(""), 16),
            "cpus": chars(report["cpus"].as_str().unwrap_or(""), 8)}),
        _ => json!({"set_up": false}),
    }
}
/// "1 file", "3 files".
fn plural(n: u64, one: &str, many: &str) -> String {
    format!("{n} {}", if n == 1 { one } else { many })
}
/// Setup writes an app's Compose file last, once its settings are in place (media.py,
/// photos.py, nextcloud.py): without one, setup has not set the app up yet.
fn set_up(data: &Path, app: &str) -> bool {
    data.join("compose").join(app).join("compose.yaml").exists()
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
pub(crate) const STARTUP: &[(&str, &str)] = &[
    ("docker.service", "Apps (Docker)"),
    ("tailscaled.service", "Phone access (Tailscale)"),
    ("nekoserver-dashboard.service", "This dashboard"),
    ("nekoserver-dashboard-collect.timer", "Dashboard readings"),
    ("nekoserver-control.service", "Fan & light control"),
    ("nekoserver-openrgb.service", "RGB lighting (OpenRGB)"),
    // Arch's name and Debian/Ubuntu's; only the installed one is listed.
    ("smb.service", "Shared folders (Samba)"),
    ("smbd.service", "Shared folders (Samba)"),
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
    ("nekoserver-maintenance.timer", "Monthly check"),
    ("nekoserver-updates.timer", "Weekly app updates"),
    ("nekoserver-update-check.timer", "Daily update check"),
    ("nekoserver-media-health.timer", "App health checks"),
    ("nekoserver-heartbeat.timer", "Outside heartbeat"),
    ("nekoserver-notifications.timer", "Phone notifications"),
];
/// Services that run all the time once started (not one-shot resume services or
/// timers): "inactive" means they are down, not finished.
pub(crate) fn stays_running(unit: &str) -> bool {
    unit.ends_with(".service")
        && !unit.ends_with("-resume.service")
        && unit != "nekoserver-hdd-policy.service"
}
/// The sockets that start a listed service when it is asked for (the dashboard's, since
/// 0.46.3): asked about too, so a service resting until then is not taken for stopped.
const STARTUP_SOCKETS: &[&str] = &["nekoserver-dashboard.socket"];
fn startup() -> Value {
    let mut args = vec![
        "systemctl",
        "show",
        "--property=Id,LoadState,UnitFileState,ActiveState,TriggeredBy",
    ];
    args.extend(STARTUP.iter().map(|u| u.0));
    args.extend(STARTUP_SOCKETS);
    let (code, out) = probe(&args, 15);
    if code != 0 {
        return json!([]);
    }
    json!(parse_startup(&out))
}
fn parse_startup(out: &str) -> Vec<Value> {
    // A name that is an alias of another listed unit (Debian's smb.service is a link to
    // smbd.service) is answered with that unit's Id: each unit is listed once.
    let mut seen = std::collections::HashSet::new();
    let blocks: Vec<std::collections::HashMap<&str, &str>> = out
        .split("\n\n")
        .map(|block| block.lines().filter_map(|l| l.split_once('=')).collect())
        .collect();
    // Seen on a fresh Debian server: the dashboard rests until its socket starts it, and
    // read while nobody had it open it was "inactive", so Home said, as urgent, that the
    // very dashboard showing it was not running. A listening socket stands for it.
    let listening = |name: &str| {
        blocks
            .iter()
            .any(|b| b.get("Id") == Some(&name) && b.get("ActiveState") == Some(&"active"))
    };
    blocks
        .iter()
        .filter_map(|fields| {
            let id = *fields.get("Id")?;
            let label = STARTUP.iter().find(|u| u.0 == id)?.1;
            if fields.get("LoadState") != Some(&"loaded") || !seen.insert(id) {
                return None;
            }
            let file = fields.get("UnitFileState").copied().unwrap_or("");
            // "static" units start because something else needs them.
            let enabled = ["enabled", "enabled-runtime", "static", "alias"].contains(&file);
            let mut active = fields.get("ActiveState").copied().unwrap_or("unknown");
            let socket = fields.get("TriggeredBy").copied().unwrap_or("");
            if active == "inactive"
                && !socket.is_empty()
                && socket.split_whitespace().any(&listening)
            {
                active = "active";
            }
            Some(json!({"unit": id, "name": label, "enabled": enabled, "active": active}))
        })
        .collect()
}
/// Why a job is intentionally not part of this server now (its app or backup switched
/// off), or None when its schedule is expected.
fn retired(cfg: &Value, key: &str) -> Option<&'static str> {
    let immich = common::immich_wanted(cfg);
    match key {
        "music" if cfg["music"]["backup_enabled"] == false => {
            Some("Switched off: the music backup is turned off in the settings")
        }
        "music" | "applications" if !common::media_wanted(cfg) => {
            Some("Switched off: the media apps are left out")
        }
        "archive" | "database" if !immich => Some("Switched off: Immich is not set up"),
        "external" if !immich && cfg["nextcloud"]["enabled"] != true => {
            Some("Switched off: neither Immich nor Nextcloud is set up")
        }
        _ => None,
    }
}
/// The witnessed restore tests (acceptance.py manual tests) that show this backup can
/// be restored: one per kind of data it holds.
fn restore_tests(cfg: &Value, key: &str) -> Value {
    let immich = common::immich_wanted(cfg);
    let cloud = cfg["nextcloud"]["enabled"] == true;
    let tests: Vec<&str> = match key {
        "archive" | "database" => vec!["independent_restore"],
        "external" => [(immich, "independent_restore"), (cloud, "cloud_restore")]
            .into_iter()
            .filter_map(|(on, test)| on.then_some(test))
            .collect(),
        "music" => vec!["music_restore"],
        "applications" => vec!["app_restore"],
        "host" => vec!["host_restore"],
        _ => vec![],
    };
    json!(tests)
}
/// The backup's timer as the dashboard shows it, and whether systemd could be asked.
fn timer_of(unit: &str) -> (Value, bool) {
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
    let at = next.as_deref().and_then(utc_seconds);
    (
        json!({"installed":code==0&&fields.get("LoadState")==Some(&"loaded"),"active":fields.get("ActiveState")==Some(&"active"),"next_run":next,"next_run_at":at}),
        code == 0 && fields.contains_key("LoadState"),
    )
}
/// The time systemd prints ("Sun 2026-11-01 03:00:00 UTC"; programs run with TZ=UTC, see
/// common::command) as seconds since 1970, so the page can show it in the household's own
/// time. None for anything else.
fn utc_seconds(shown: &str) -> Option<f64> {
    let mut parts = shown.split_whitespace();
    let (_day, date, time, zone) = (parts.next()?, parts.next()?, parts.next()?, parts.next()?);
    if zone != "UTC" || parts.next().is_some() {
        return None;
    }
    chrono::NaiveDateTime::parse_from_str(&format!("{date} {time}"), "%Y-%m-%d %H:%M:%S")
        .ok()
        .map(|t| t.and_utc().timestamp() as f64)
}
/// Whether this job runs on a schedule of its own. The upgrade is started by you only:
/// without this, every upgrade that worked was shown as "needs attention: its schedule is
/// gone" (seen on the rig).
fn scheduled(key: &str) -> bool {
    key != "upgrade"
}
/// A schedule that stopped while its job is still wanted: switched off, or gone after it
/// had run. None while it is in place (or systemd could not be asked).
fn schedule_stopped(timer: &Value, known: bool, ever_ran: bool) -> Option<String> {
    if !known {
        None
    } else if timer["installed"] == true && timer["active"] != true {
        Some("Run setup again to switch its schedule back on.".to_string())
    } else if timer["installed"] != true && ever_ran {
        Some(
            "Its schedule is gone, so it no longer runs: run setup again to put it back."
                .to_string(),
        )
    } else {
        None
    }
}
/// Every backup job works with both the app SSD and the media HDD (backup::guard), so
/// while one of them is missing (`away`, its name) none can run: each job says so rather
/// than "Passed", and gets no alert of its own (the drive's alert is the one to act on).
fn backups(cfg: &Value, now: f64, ok: bool, away: Option<&str>) -> Value {
    let mut out = vec![];
    for (key, name, unit, base_max) in JOBS {
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
        // The upgrade runs as nekoserver-upgrade@<release> (or, after a restart, as the
        // resume service): its report says which, so a run at work is asked about.
        let named = upgrade_unit(&saved);
        let unit = if key == "upgrade" {
            named.as_str()
        } else {
            unit
        };
        let raw = text(&saved["status"]);
        // An update undone after the apps did not start well: the backup was made and the
        // apps are back as they were, so only the update is still to come (done in part).
        let undone = key == "maintenance" && rolled_back(&saved);
        let status = if undone {
            "partial"
        } else if ["running", "pass", "fail", "partial"].contains(&raw) {
            raw
        } else {
            "unknown"
        };
        // A success saved while the clock was ahead still counts (shown as now): hiding
        // it would read as "never succeeded" after the clock was put back.
        let success = common::shown_time(&saved["last_success"], now);
        let start = num(&saved["started_at"], 0.0, now + 60.0);
        // Never succeeded: due one interval (plus a day) after its schedule was set up.
        let installed = timer_installed_at(unit);
        let stale = if status == "running" {
            // A run with no start time can't be judged as fresh.
            start.as_f64().is_none_or(|t| now - t > max)
        } else {
            first_overdue(success.as_f64(), installed, now, max)
        };
        let state = overdue_state(status, stale);
        let (timer, known) = timer_of(unit);
        if let Some(why) = retired(cfg, key) {
            out.push(json!({"id":key,"name":name,"status":"disabled","last_success":success,"last_attempt":start,"timer":timer,"reason":why,"restore":[]}));
            continue;
        }
        // A worker that systemd could not even start writes no report: its service says so.
        let (_, service) = probe(
            &[
                "systemctl",
                "show",
                &format!("{unit}.service"),
                "--property=LoadState,ActiveState,Result,ExecMainStatus",
            ],
            8,
        );
        let unrun = if status == "fail" || undone {
            None
        } else if status == "running" && cut_off(&service, start.as_f64(), now) {
            Some("Stopped part-way: the PC restarted or the job was stopped. The next run starts it again.".to_string())
        } else {
            service_failure(unit, &service)
        };
        let state = if unrun.is_some() { "fail" } else { state };
        let stopped = if ["fail", "running"].contains(&state) {
            None
        } else {
            schedule_stopped(&timer, known && scheduled(key), !success.is_null())
        };
        let state = if stopped.is_some() { "stale" } else { state };
        let reason = if let Some(why) = unrun {
            Some(why)
        } else if let Some(why) = stopped {
            Some(why)
        } else if status == "fail" {
            Some(failure_reason(&saved))
        } else if undone {
            Some(undone_reason(&saved))
        } else if status == "partial" {
            Some(partial_reason(&saved["missing"]))
        } else if success.is_null() && stale {
            Some("No successful run since it was set up".to_string())
        } else if success.is_null() {
            Some("No recorded successful run".to_string())
        } else if stale {
            Some("Last success is overdue".to_string())
        } else {
            None
        };
        // The service that does the work, for the page's log commands (the weekly photo
        // schedule's, an upgrade's own instance).
        let mut row = json!({"id":key,"name":name,"status":state,"last_success":success,"last_attempt":start,"timer":timer,"reason":reason,"restore":restore_tests(cfg,key),"unit":format!("{unit}.service")});
        if undone {
            row["rolled_back"] = json!(true);
        }
        // Said to the page too: it kept its own rule, and listed "NekoServer upgrade
        // schedule is off" after every upgrade that worked.
        if !scheduled(key) {
            row["scheduled"] = json!(false);
        }
        if let Some(drive) = away {
            row["blocked"] = json!(format!(
                "The {drive} is missing, so this backup can't run until it is back."
            ));
        }
        out.push(row);
    }
    json!(out)
}
/// A job's state once it is overdue (`stale`): a backup that keeps failing stays a
/// failure (urgent), anything else is overdue.
fn overdue_state(status: &str, stale: bool) -> &str {
    if stale && status != "fail" {
        "stale"
    } else {
        status
    }
}
/// The service the upgrade's report names, when it is one of the upgrade's own: the
/// release's (nekoserver-upgrade@v0.45.8Beta) or the resume after a restart.
fn upgrade_unit(saved: &Value) -> String {
    let unit = text(&saved["unit"]);
    // A release however its tag is written (updates.rs release_version).
    let release = unit
        .strip_prefix("nekoserver-upgrade@")
        .is_some_and(|tag| crate::updates::release_version(tag).is_some());
    if release || unit == "nekoserver-upgrade-resume" {
        unit.to_string()
    } else {
        "nekoserver-upgrade".to_string()
    }
}
/// When this backup's schedule was set up (its timer file was written), if it was.
fn timer_installed_at(unit: &str) -> Option<f64> {
    let modified = fs::metadata(format!("/etc/systemd/system/{unit}.timer"))
        .ok()?
        .modified()
        .ok()?;
    Some(
        modified
            .duration_since(std::time::UNIX_EPOCH)
            .ok()?
            .as_secs_f64(),
    )
}
/// Overdue: the last success is older than the interval, or there never was one and a
/// day's grace after the first run was due (one interval after set-up) has passed.
fn first_overdue(success: Option<f64>, installed: Option<f64>, now: f64, max: f64) -> bool {
    match success {
        Some(t) => now - t > max,
        None => installed.is_some_and(|t| now - t > max + 86400.0),
    }
}
/// Why the backup service's latest run failed in systemd's own words, when it did. That
/// covers a worker that never started (missing or unrunnable program: exit 203), which
/// writes no report of its own and would otherwise look "unknown" for ever.
/// The report still says "running" but its service is not: the run was cut off (a power
/// cut or restart) before it could record how it ended. Runs started in the last two
/// minutes are left alone, so one that is just finishing isn't misread.
fn cut_off(show: &str, started: Option<f64>, now: f64) -> bool {
    let state = show
        .lines()
        .find_map(|l| l.strip_prefix("ActiveState="))
        .unwrap_or("");
    !state.is_empty()
        && !["active", "activating", "deactivating", "reloading"].contains(&state)
        && started.is_none_or(|t| now - t > 120.0)
}
fn service_failure(unit: &str, show: &str) -> Option<String> {
    let fields = show
        .lines()
        .filter_map(|s| s.split_once('='))
        .collect::<std::collections::BTreeMap<_, _>>();
    let result = fields.get("Result").copied().unwrap_or("");
    if fields.get("LoadState") != Some(&"loaded")
        || ["", "success"].contains(&result)
        || ["activating", "active", "reloading"]
            .contains(&fields.get("ActiveState").copied().unwrap_or(""))
    {
        return None;
    }
    let status = fields.get("ExecMainStatus").copied().unwrap_or("");
    Some(format!(
        "Its service could not run ({}{}); check  systemctl status {unit}.service",
        chars(result, 40),
        if status.is_empty() || status == "0" {
            String::new()
        } else {
            format!(", exit {}", chars(status, 10))
        }
    ))
}
/// The stage of the monthly run that starts the updated apps (maintenance.rs). A failure
/// there is rolled back: the apps get their previous version and data again, and are
/// checked to answer. When putting them back failed too, the reason goes on with "; then: ".
const UPDATED_APPS_STAGE: &str = "starting the updated apps";
fn rolled_back(saved: &Value) -> bool {
    // The monthly run says so itself since 0.40.2 ("rolled_back": true in its report);
    // a report from before that is read by its stage and reason.
    saved["status"] == "fail"
        && (saved["rolled_back"] == true
            || (saved["stage"] == UPDATED_APPS_STAGE
                && !text(&saved["reason"]).contains("; then: ")))
}
/// "The update was undone: …", in plain words, with why in short (the first sentence of
/// the reason; the advice for engineers after it is left to the guide).
fn undone_reason(saved: &Value) -> String {
    let said = text(&saved["reason"]);
    let why = said.split(". ").next().unwrap_or("").trim_end_matches('.');
    // A failed run that was set to try again says when (the next night, a few nights).
    let again = match saved["retry_at"].as_str() {
        Some(when) if !when.trim().is_empty() => format!("{}.", capitalised(&whole(when, 200))),
        _ => "It is tried again next month.".to_string(),
    };
    let mut out = format!(
        "The update was undone: your apps are back on their previous version and work as before. {again}"
    );
    if !why.trim().is_empty() {
        out += &format!(" Why: {}.", capitalised(&whole(why, 200)));
    }
    out
}
/// A reason the report cut at its length limit (common::sanitize keeps 300 characters)
/// ends at its last whole sentence, or else its last whole word and "…", never mid-word.
fn whole(said: &str, limit: usize) -> String {
    if said.chars().count() < limit {
        return said.to_string();
    }
    let cut: String = said.chars().take(limit - 1).collect();
    if let Some(at) = cut.rfind(". ").filter(|at| *at >= cut.len() / 2) {
        return cut[..=at].to_string();
    }
    match cut.rfind(' ') {
        Some(at) => format!("{}…", cut[..at].trim_end_matches([',', ';', ':'])),
        None => format!("{cut}…"),
    }
}
/// A reason that ends a whole sentence ends with its full stop: common::sanitize takes
/// one off (so "{reason}." never reads ".."), and a cut after a whole sentence reads
/// "sentence.…". A reason cut mid-sentence keeps its "…"; a command or file name at the
/// end gets nothing added, so it can be copied as it is.
fn full_stop(said: &str) -> String {
    let said = said.trim_end();
    if let Some(sentence) = said.strip_suffix(".…") {
        return format!("{sentence}.");
    }
    let last_word = said.rsplit(' ').next().unwrap_or("");
    let ends_plainly = said
        .chars()
        .last()
        .is_some_and(|c| c.is_alphanumeric() || c == ')');
    if ends_plainly && !last_word.contains(['/', '.', '_', '=']) {
        format!("{said}.")
    } else {
        said.to_string()
    }
}
/// How the repair advice (backup::SETTLE) begins, and the part of it to keep whole when
/// the reason it ends was cut: what to run, and that the monthly run waits until then.
const SETTLE_START: &str = "To settle it, run";
const SETTLE_SHORT: &str = "To settle it, run  sudo bash install.sh --recover  and choose \"Repair damaged backup copies\". Until then the monthly snapshot and updates wait.";
/// A saved reason as the page shows it: cut only at a whole sentence or word, with the
/// repair advice kept (in short) however long the rest is.
fn readable(said: &str) -> String {
    match said.find(SETTLE_START) {
        Some(at) => {
            let before = full_stop(&whole(said[..at].trim_end(), 300));
            format!("{before} {SETTLE_SHORT}").trim_start().to_string()
        }
        None => full_stop(&whole(said, 300)),
    }
}
/// "Failed while copying photos to the USB drive: cp exited with 1 (No space left on device)".
fn failure_reason(saved: &Value) -> String {
    let stage = chars(text(&saved["stage"]), 120);
    let reason = readable(&chars(text(&saved["reason"]), 300));
    match (stage.is_empty(), reason.is_empty()) {
        (true, true) => "Last run failed; inspect its service logs".to_string(),
        (false, true) => format!("Failed while {stage}"),
        (true, false) => format!("Failed: {reason}"),
        (false, false) => format!("Failed while {stage}: {reason}"),
    }
}
/// "3 files were not copied (2 in use, 1 with a name the USB drive cannot store; …)".
fn partial_reason(missing: &Value) -> String {
    // The monthly run says what it left for later as a list of sentences ("USB check:
    // the drive is full", "Immich release: …"): each is shown, the first five in full.
    if let Some(list) = missing.as_array() {
        let said: Vec<String> = list
            .iter()
            .filter_map(|v| v.as_str())
            .filter(|s| !s.trim().is_empty())
            .map(|s| capitalised(&chars(s, 300)))
            .collect();
        if !said.is_empty() {
            let mut text = said.iter().take(5).cloned().collect::<Vec<_>>().join(". ");
            if said.len() > 5 {
                text += &format!(" (and {} more)", said.len() - 5);
            }
            return text;
        }
    }
    let in_use = missing["in_use"].as_u64().unwrap_or(0);
    let names = missing["unsupported_names"].as_u64().unwrap_or(0);
    // Counts only: the status file can be read by every account on the server, so the
    // names of the files stay in the job's root-only report (reports/jobs on the SSD).
    let listed = missing["files"].as_array().is_some_and(|a| !a.is_empty());
    let mut parts = vec![];
    if in_use + names > 0 || listed {
        parts.push(format!(
            "{} not copied ({in_use} in use, {names} with names the USB drive cannot store; \
             their names are in the backup's report, reports/jobs on the application SSD)",
            plural(in_use + names, "file was", "files were")
        ));
    }
    // The USB copy of the Immich database (missing, stale or without its version note),
    // and a source that could not be copied at all (the Nextcloud folder missing).
    for key in ["database", "source"] {
        let problem = capitalised(&chars(text(&missing[key]), 300));
        if !problem.is_empty() {
            parts.push(problem);
        }
    }
    if parts.is_empty() {
        return "Incomplete; see the job's report".to_string();
    }
    parts.join(". ")
}
/// `text` with its first letter in capitals ("" stays "").
fn capitalised(text: &str) -> String {
    let mut letters = text.chars();
    letters
        .next()
        .map(|c| c.to_uppercase().collect::<String>() + letters.as_str())
        .unwrap_or_default()
}
/// Seconds since the computer started (None when /proc can't say).
fn uptime() -> Option<f64> {
    fs::read_to_string("/proc/uptime")
        .ok()?
        .split_whitespace()
        .next()?
        .parse()
        .ok()
}
/// The last check-in is overdue. Right after a start there is none yet: the first runs
/// 90 s after boot (nekoserver-heartbeat.timer OnBootSec) and may take up to 40 s, so
/// for the first 3 minutes "none yet" is not a missed check-in.
fn heartbeat_late(attempt: Option<f64>, now: f64, interval: u64, uptime: Option<f64>) -> bool {
    match attempt {
        Some(t) => now - t > interval as f64 + 90.0,
        None => uptime.is_none_or(|up| up > 180.0),
    }
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
    // A timer switched off by choice ("wanted": false) is not a missed check-in.
    let running = installed && fields.get("ActiveState") == Some(&"active");
    let interval = cfg["heartbeat"]["interval_seconds"]
        .as_u64()
        .filter(|v| (60..=3600).contains(v))
        .unwrap_or(300);
    let state = if running && heartbeat_late(attempt.as_f64(), now, interval, uptime()) {
        "stale"
    } else if row["format"] == "neko-heartbeat-v1"
        && ["pass", "fail"].contains(&row["status"].as_str().unwrap_or(""))
    {
        row["status"].as_str().unwrap()
    } else {
        "unknown"
    };
    json!({"status":state,"timer":{"installed":installed,"active":fields.get("ActiveState")==Some(&"active"),"next_run":fields.get("NextElapseUSecRealtime").filter(|s|!s.is_empty()).map(|s|chars(s,80))},"last_attempt":attempt,"last_success":common::shown_time(&row["last_success"],now),"provider_notifications_verified":false})
}

/// The latest app checks (media-health), with how current they are: "current",
/// "stale" (older than two check intervals, at least 2 hours) or "unknown". None when
/// monitoring is switched off.
struct MediaReport {
    saved: Value,
    age: Option<f64>,
    status: &'static str,
    /// Why the report is stale when its check could not run since it was written.
    reason: Option<String>,
}
fn media_report(cfg: &Value, now: f64, ok: bool) -> Option<MediaReport> {
    if cfg["monitoring"]["enabled"] == false {
        return None;
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
        .map(|t| now - t.timestamp_millis() as f64 / 1000.0)
        // Dated in the future (the clock was wrong when it was written): can't be judged.
        .filter(|age| *age >= -60.0)
        .map(|age| age.max(0.0));
    let limit = 7200.0_f64.max(
        cfg["monitoring"]["interval_minutes"]
            .as_f64()
            .unwrap_or(60.0)
            * 120.0,
    );
    let mut status = match age {
        Some(age) if age > limit => "stale",
        Some(_) => "current",
        None => "unknown",
    };
    // A check that could not run (a drive refused, its lock, broken settings) writes no
    // report, so the last PASS stayed "current" for up to two intervals while the check
    // failed every hour (the review of 0.47.9): a failed run since the report makes it
    // stale, and says why.
    let (_, show) = probe(
        &[
            "systemctl",
            "show",
            "nekoserver-media-health.service",
            "--property=LoadState,ActiveState,Result,ExecMainStatus,ExecMainExitTimestampMonotonic",
        ],
        8,
    );
    let reason = check_failed_since(&show, age, uptime());
    if reason.is_some() {
        status = "stale";
    }
    Some(MediaReport {
        saved,
        age,
        status,
        reason,
    })
}
/// The app check's failure to run, when its service's last run ended after the report
/// (`report_age`: seconds since the report; None: no report) and wrote none. Exit codes 2
/// and 3 are the check's own findings after a written report, not a failure to run.
/// `uptime`: seconds since boot, to read the exit time systemd gives in those.
fn check_failed_since(show: &str, report_age: Option<f64>, uptime: Option<f64>) -> Option<String> {
    let fields = show
        .lines()
        .filter_map(|s| s.split_once('='))
        .collect::<std::collections::BTreeMap<_, _>>();
    if ["2", "3"].contains(fields.get("ExecMainStatus").unwrap_or(&"")) {
        return None;
    }
    let why = service_failure("nekoserver-media-health", show)?;
    let ended = fields
        .get("ExecMainExitTimestampMonotonic")?
        .parse::<f64>()
        .ok()?
        / 1_000_000.0;
    let since_end = uptime? - ended;
    if since_end < 0.0 || report_age.is_some_and(|age| age < since_end) {
        return None; // the report is newer than the failure
    }
    Some(why)
}
/// Whether an app answered its last check over HTTP (true/false), or null when that
/// isn't known: no current check, or the app wasn't running. Processor use never
/// counts: video conversion can rightly keep every core busy.
fn responding(report: Option<&MediaReport>, app: &str, up_for: Option<f64>) -> Value {
    let Some(report) = report.filter(|r| r.status == "current") else {
        return Value::Null;
    };
    // A check made before the app last started says nothing about this start (the app
    // was restarted since, from the dashboard, say): not known until the next check.
    if let (Some(age), Some(up)) = (report.age, up_for) {
        if age > up {
            return Value::Null;
        }
    }
    let check = &report.saved["services"][app];
    match (check["container"].as_str(), &check["http_status"]) {
        (Some("running"), Value::Number(_)) => json!(true),
        (Some("running"), Value::Null) => json!(false),
        _ => Value::Null,
    }
}
/// {full container ID: when it last started (Unix seconds)} from `docker inspect
/// --format "{{.Id}} {{.State.StartedAt}}"`. A time Docker can't give is left out.
fn started_at(out: &str) -> Vec<(String, f64)> {
    out.lines()
        .filter_map(|line| {
            let (id, at) = line.trim().split_once(' ')?;
            let at = chrono::DateTime::parse_from_rfc3339(at).ok()?;
            let seconds = at.timestamp_millis() as f64 / 1000.0;
            // Docker writes year 1 for a container that never started.
            (seconds > 0.0).then(|| (id.to_string(), seconds))
        })
        .collect()
}
fn media(report: Option<&MediaReport>) -> Value {
    let Some(MediaReport {
        saved,
        age,
        status,
        reason,
    }) = report
    else {
        return json!({"status":"disabled","counts":{},"checks":[],"age_seconds":null});
    };
    let mut r = json!({"status":status,"age_seconds":age,"counts":{},"checks":[],"reason":reason});
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
/// The fingerprint of the settings an acceptance run must match: setup works it out
/// (acceptance_summary.scope_digest) and writes it into these settings; "" when they have
/// none, which no run matches. This also worked it out itself, a second copy of what it
/// covers (the review of 0.47.1).
fn scope(cfg: &Value) -> &str {
    cfg["acceptance_scope"]
        .as_str()
        .filter(|v| {
            v.len() == 64
                && v.bytes()
                    .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
        })
        .unwrap_or("")
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
        "cloud_restore" => Some("Nextcloud files restore"),
        "music_restore" => Some("Music library restore"),
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
    // The profiles are setup's (config.json), written into these settings: a list kept
    // here missed "accounts" (the review of 0.47.1).
    let known = cfg["acceptance_profiles"]
        .as_array()
        .is_some_and(|names| names.iter().any(|n| n.as_str() == Some(profile)));
    if saved["format"] != "neko-acceptance-v1"
        || !known
        || num(&saved["generated_at"], 0.0, now + 60.0).is_null()
    {
        return r;
    }
    r["profile"] = json!(profile);
    r["generated_at"] = saved["generated_at"].clone();
    if saved["version"] != VERSION
        || scope(cfg).is_empty()
        || text(&saved["dashboard_scope"]) != scope(cfg)
    {
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
                    .push(json!({"id":text(&row["id"]),"name":label,"status":state}));
            }
        }
        r[group] = counts;
    }
    // "stale" means complete but over a day old: the result still holds as far as anyone
    // knows, it is just not today's. A failed or unfinished check stays what it is.
    let old = now - saved["generated_at"].as_f64().unwrap() > 86400.0;
    r["status"] = json!(if !valid {
        "unknown"
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
        if old {
            "stale"
        } else {
            "complete"
        }
    } else {
        "pending"
    });
    r
}
/// The apps the Tailscale gateway set up routes for (gateway_tools.py writes its report).
fn remote_apps(report: &Path) -> Value {
    let saved = common::json(report, 64_000);
    if saved["format"] != "neko-gateway-v1" || saved["status"] != "configured" {
        return json!([]);
    }
    json!(saved["services"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .filter(|a| a.len() <= 40 && a.bytes().all(|b| b.is_ascii_lowercase() || b == b'_'))
        .take(20)
        .collect::<Vec<_>>())
}
/// What the System tab shows (system_updates.py writes it daily, as root): names,
/// versions, counts and reasons only, each shortened.
fn system_updates(path: &Path) -> Value {
    let saved = common::json(path, 4_000_000);
    if saved["format"] != "neko-system-check-v1" {
        return json!({});
    }
    let word = |v: &Value| json!(chars(&common::sanitize(text(v)), 120));
    let updates: Vec<Value> = saved["updates"]
        .as_array()
        .into_iter()
        .flatten()
        .take(300)
        .map(|u| {
            json!({"name": word(&u["name"]), "from": word(&u["from"]), "to": word(&u["to"]),
                "security": u["security"].as_bool(), "reboot": u["reboot"] == true,
                "restarts": u["restarts"].as_array().into_iter().flatten().take(4).map(&word).collect::<Vec<_>>()})
        })
        .collect();
    json!({
        "checked_at": saved["checked_at"].as_f64(),
        "system": word(&saved["system"]),
        "count": saved["count"].as_u64(),
        "updates": if saved["updates"].is_array() { json!(updates) } else { Value::Null },
        "note": saved["note"].as_str().map(|n| chars(&common::sanitize(n), 300)),
        // Why it is not known which updates fix a security issue (null when it is).
        "security_note": saved["security_note"].as_str().map(|n| chars(&common::sanitize(n), 300)),
        "restart_needed": saved["restart_needed"] == true,
        // "required", "not_required" or "unknown" (the check could not tell).
        "restart": saved["restart"]
            .as_str()
            .filter(|state| ["required", "not_required"].contains(state))
            .unwrap_or("unknown"),
        "restart_reasons": saved["restart_reasons"].as_array().into_iter().flatten().take(5).map(&word).collect::<Vec<_>>(),
    })
}
/// Major app updates the daily update check found but leaves for you to approve (its
/// notes on the SSD, written again at every check).
fn updates(cfg: &Value, data_ok: bool) -> Value {
    let run = Path::new(text(&cfg["data_mount"]))
        .join(crate::updates::FOLDER)
        .join("notes");
    if !data_ok {
        return json!([]);
    }
    let mut out = vec![];
    for (app, name) in [("immich", "Immich"), ("nextcloud", "Nextcloud")] {
        let pending = common::json(&run.join(format!("{app}-major-pending.json")), 64000);
        if pending["status"] == "manual_major_upgrade_required" {
            out.push(json!({"app":app,"name":name,"current":chars(text(&pending["current"]),40),"available":chars(text(&pending["available"]),40)}));
        }
    }
    json!(out)
}
/// What a nearly full drive stops, and how to free it.
fn space_advice(id: &str, full: bool) -> &'static str {
    match (id, full) {
        ("media_hdd", true) => "Downloads, the nightly backups and the monthly snapshot stop when it is full. Delete what you no longer watch, or old downloads, or add a bigger drive.",
        ("data_ssd", true) => "The apps keep their data, the music and their images here and can stop working when it is full. Free up space there, or move to a bigger SSD.",
        (_, true) => "Backups and uploads stop when it is full. Free up space there.",
        _ => "Review free space before uploads or backups.",
    }
}
/// backup_secrets.py's key: without it every monthly snapshot holds the app password
/// files in plain text (the monthly job says so in its log each run; the dashboard
/// says so all the time, so it can't go unnoticed).
const BACKUP_KEY_FILES: [&str; 2] = [
    "/etc/nekoserver-backups/secrets-key.p8",
    "/etc/nekoserver-backups/secrets-recipient.pem",
];
fn backup_key_ready() -> bool {
    BACKUP_KEY_FILES.iter().all(|p| Path::new(p).is_file())
}
/// Whether this server is still signed in to Tailscale, and when its sign-in runs out
/// (`days_left`; none when key expiry is switched off). Null without Tailscale.
fn tailscale_login(now: f64) -> Value {
    let (code, out) = probe(&["tailscale", "status", "--json"], 10);
    if code != 0 && out.trim().is_empty() {
        return Value::Null;
    }
    login_state(&serde_json::from_str(&out).unwrap_or(Value::Null), now)
}
fn login_state(status: &Value, now: f64) -> Value {
    let state = chars(status["BackendState"].as_str().unwrap_or("unknown"), 40);
    let days_left = status["Self"]["KeyExpiry"]
        .as_str()
        .and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok())
        .map(|t| ((t.timestamp() as f64 - now) / 86400.0).floor());
    // A server signed in before keeps its Tailscale name and addresses when the sign-in
    // runs out; one never signed in has neither.
    let known = status["Self"]["DNSName"]
        .as_str()
        .is_some_and(|n| !n.is_empty())
        || status["Self"]["TailscaleIPs"]
            .as_array()
            .is_some_and(|a| !a.is_empty());
    json!({"state": state, "days_left": days_left, "signed_in_before": known})
}
/// How hot the processor is, as an alert level. Current processors run at 95 °C (AMD)
/// or 100 °C (Intel) under full load by design, so one such reading is no alarm: urgent
/// at the chip's own critical limit or 105 °C at once, otherwise only after
/// five minutes above 95 °C; a warning after five minutes at 80 °C or more.
fn cpu_heat(s: &Value) -> Option<&'static str> {
    let temp = s["metrics"]["temperature"].as_f64()?;
    let crit = s["metrics"]["temperature_crit"].as_f64();
    if crit.is_some_and(|c| temp >= c) || temp >= 105.0 {
        return Some("critical");
    }
    let now = s["generated_at"].as_f64()?;
    if temp > 95.0 && held(&s["history"], now, |v| v > 95.0) {
        Some("critical")
    } else if temp >= 80.0 && held(&s["history"], now, |v| v >= 80.0) {
        Some("warning")
    } else {
        None
    }
}
/// Every reading of the last five minutes (and there is one from five minutes ago) is hot.
fn held(history: &Value, now: f64, hot: impl Fn(f64) -> bool) -> bool {
    let recent: Vec<(f64, Option<f64>)> = history
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|r| Some((now - r["at"].as_f64()?, r["temperature"].as_f64())))
        .filter(|(age, _)| (0.0..=330.0).contains(age))
        .collect();
    recent.iter().any(|(age, _)| *age >= 290.0) && recent.iter().all(|(_, t)| t.is_some_and(&hot))
}
/// A drive on the NAS stopped taking writes and the NAS check holds the apps.
fn nas_held(s: &Value) -> bool {
    s["storage"]
        .as_array()
        .into_iter()
        .flatten()
        .any(|d| d["held_by_nas_watch"] == true)
}
/// The apps that are stopped (exited, never started or dead) while the app SSD or the
/// media HDD is missing, or while the NAS check holds them: they are stopped on purpose,
/// and start again by themselves once the drive is back (or takes writes again).
fn held_apps(s: &Value) -> Vec<&str> {
    let away = s["storage"]
        .as_array()
        .into_iter()
        .flatten()
        .any(|d| d["id"] != "os" && d["status"] != "mounted");
    if !away && !nas_held(s) {
        return vec![];
    }
    s["applications"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|a| ["exited", "created", "dead"].contains(&text(&a["state"])))
        .map(|a| text(&a["name"]))
        .filter(|n| !n.is_empty())
        .collect()
}
/// Marks the apps stopped on purpose (`held`), so the page says the drive's one alert
/// instead of one "stopped" per app: decided here, once.
fn mark_held(s: &mut Value) {
    let held: Vec<String> = held_apps(s).into_iter().map(String::from).collect();
    if let Some(apps) = s["applications"].as_array_mut() {
        for a in apps {
            let on_hold = held.iter().any(|n| a["name"] == n.as_str());
            a["held"] = json!(on_hold);
        }
    }
}
pub(crate) fn alerts(s: &Value) -> Value {
    let mut out = vec![];
    let mut add = |id: String, level: &str, title: String, detail: &str| {
        out.push(json!({"id":id,"level":level,"title":title,"detail":detail}))
    };
    // Alerts that fail to reach anyone are still said on the page.
    if s["notifications"]["status"] == "fail" {
        add(
            "notifications:delivery".into(),
            "warning",
            "Phone alerts are not being delivered".into(),
            "The last alerts could not be sent to your ntfy topic: check its address and access token (setup can change them).",
        );
    }
    if ["fail", "stale"].contains(&text(&s["heartbeat"]["status"])) {
        add(
            "heartbeat:delivery".into(),
            "warning",
            "Outage alerts: the outside monitor is not reached".into(),
            "This server's check-ins to your outside monitor fail or have stopped, so it cannot warn you of an outage. Check the network and the monitor's ping address.",
        );
    }
    if s["tailscale"]["state"] == "NeedsLogin" && s["tailscale"]["signed_in_before"] == false {
        // Seen on a fresh Debian server: it was told its sign-in "ran out", as urgent,
        // before it had ever been signed in. That is a setup step still to do.
        add(
            "tailscale:login".into(),
            "warning",
            "Phone access is not set up yet".into(),
            "This server is not signed in to Tailscale yet, so your phone can't reach the apps away from home. Run  sudo bash install.sh  again: it walks you through it (or run  sudo tailscale up  and open the link it prints).",
        );
    } else if s["tailscale"]["state"] == "NeedsLogin" {
        add(
            "tailscale:login".into(),
            "critical",
            "Phone access is signed out of Tailscale".into(),
            "This server's Tailscale sign-in ran out, so the app addresses on your phone stop working. Sign in again: sudo tailscale up, then in the Tailscale admin console choose Disable key expiry for this server.",
        );
    } else if s["tailscale"]["days_left"]
        .as_f64()
        .is_some_and(|d| d <= 14.0)
    {
        add(
            "tailscale:expiry".into(),
            "warning",
            "Phone access stops soon: Tailscale sign-in runs out".into(),
            "In the Tailscale admin console (Machines), open this server's menu and choose Disable key expiry, so it stays signed in.",
        );
    }
    for unit in s["startup"].as_array().into_iter().flatten() {
        // Schedules can be off on purpose (the backup cards show those); the
        // services everything else depends on must start with the computer.
        let name = text(&unit["unit"]);
        let core = !name.ends_with(".timer") || name == "nekoserver-dashboard-collect.timer";
        // Enabled is not the same as healthy: a failed service is reported either way
        // (a finished one-shot, such as a resume service, is "inactive", not failed).
        if unit["active"] == "failed" {
            add(
                format!("startup:{name}:failed"),
                if core { "critical" } else { "warning" },
                format!("{} has stopped with an error", text(&unit["name"])),
                "Its last start or run failed. The guide shows how to read its log; setup can start it again.",
            );
        } else if stays_running(name) && unit["active"] == "inactive" {
            // Not failed, yet not running: Docker waiting for a drive that never
            // appeared, or a service someone stopped.
            add(
                format!("startup:{name}:failed"),
                "critical",
                format!("{} is not running", text(&unit["name"])),
                "It is stopped. If a drive is missing, connect it and restart the PC; otherwise the guide shows how to start it.",
            );
        }
        if core && unit["enabled"] == false {
            add(
                format!("startup:{}", text(&unit["unit"])),
                "warning",
                format!("{} won't start after a restart", text(&unit["name"])),
                "It is installed but not set to start when the computer boots. Run setup again to fix it.",
            );
        }
    }
    // Apps stopped on purpose while a drive is missing are named in that drive's alert,
    // not raised one by one (each with advice that can't help): one alert to act on.
    let held = held_apps(s);
    let mut named = false;
    if let Some(disks) = s["storage"].as_array() {
        for d in disks {
            let id = text(&d["id"]);
            let name = text(&d["name"]);
            if d["status"] != "mounted" {
                let mut detail = "The drive is not connected, or is not the one chosen at setup; the apps on it are stopped so nothing is written in its place. Reconnect it: once it is back, the apps start again by themselves within a minute. If they do not, restart the PC.".to_string();
                if id != "os" && !named && !held.is_empty() {
                    // Said first, so the page shows it before "Show more".
                    named = true;
                    detail = format!("Not connected, or not the drive chosen at setup. Stopped until it is back, so nothing is written in its place: {}. Reconnect it: the apps then start again by themselves within a minute. If they do not, restart the PC.", held.join(", "));
                }
                add(
                    format!("mount:{id}"),
                    "critical",
                    format!("{name} unavailable"),
                    &detail,
                );
            } else if d["fs_errors"].as_u64().is_some_and(|n| n > 0) && id != "os" {
                add(
                    format!("mount:{id}"),
                    "critical",
                    format!("{name} has filesystem errors"),
                    "Linux met errors writing to it (a loose cable, the network storage box away for long, a failing drive), so some changes may be missing and the drive may have turned read-only. Check it: sudo bash install.sh --recover, and choose \"Check a drive that turned read-only\" (it checks and repairs the filesystem, with your yes).",
                );
            } else if d["waiting"] == true {
                add(
                    format!("mount:{id}"),
                    "warning",
                    format!("{name} is waiting for the network storage box"),
                    "The NAS does not answer, so the apps' reads and writes to this drive wait for it (for up to ten minutes; nothing is lost while they wait). Check that the NAS is on and on the network.",
                );
            } else if d["read_only"] == true {
                add(
                    format!("mount:{id}"),
                    "critical",
                    format!("{name} turned read-only"),
                    if d["network"] == true {
                        "Linux stopped writing to it after errors (the connection to the network storage box was lost for a while, most likely), so the apps' changes fail. Nothing more is written; do not restart the apps yet. Check the NAS and the network, then in a terminal: sudo bash install.sh --recover, and choose \"Check a drive that turned read-only\"."
                    } else {
                        "Linux stopped writing to it after errors, so the apps' changes fail. Check its cable and health, then in a terminal: sudo bash install.sh --recover, and choose \"Check a drive that turned read-only\"."
                    },
                );
            } else if d["used_percent"].as_f64().unwrap_or(0.0) >= 90.0 {
                let full = d["used_percent"].as_f64().unwrap_or(0.0) >= 97.0;
                add(
                    format!("space:{id}"),
                    if full { "critical" } else { "warning" },
                    if full {
                        format!("{name} is almost full")
                    } else {
                        format!("{name} is filling up")
                    },
                    space_advice(id, full),
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
    for t in s["network_storage"]["targets"]
        .as_array()
        .into_iter()
        .flatten()
    {
        if t["state"] != "connected" {
            add(
                format!("nas:{}", text(&t["target"])),
                "critical",
                "The connection to the network storage box is down".into(),
                &format!(
                    "This PC has no working connection to {} on the NAS ({}), so the drives on it can't be read or written. Check that the NAS is on and on the network. Once it answers, the connection comes back by itself; if a drive then shows as read-only, follow its alert.",
                    text(&t["target"]),
                    text(&s["network_storage"]["portal"])
                ),
            );
        }
    }
    for a in s["arrays"].as_array().into_iter().flatten() {
        let id = text(&a["id"]);
        let names: Vec<&str> = a["holds_names"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .collect();
        let shown = if names.is_empty() {
            text(&a["name"]).to_string()
        } else {
            format!("{} ({})", text(&a["name"]), names.join(", "))
        };
        let worst_errors = a["members"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|m| m["errors"].as_u64())
            .max()
            .unwrap_or(0);
        match text(&a["status"]) {
            "degraded" => add(
                format!("array-lost:{id}"),
                "critical",
                format!("{shown} has lost a drive"),
                text(&a["reason"]),
            ),
            "rebuilding" => {
                add(
                    format!("array-lost:{id}"),
                    "critical",
                    format!("{shown} has one copy fewer"),
                    text(&a["reason"]),
                );
                let done = a["progress"]
                    .as_f64()
                    .map(|p| format!(" ({p} % done)"))
                    .unwrap_or_default();
                add(
                    format!("array-rebuild:{id}"),
                    "warning",
                    format!("{shown} is rebuilding{done}"),
                    "Keep the computer on and every drive connected until it finishes; the Drives page shows the progress.",
                );
            }
            "failing" => add(
                format!("array-errors:{id}"),
                "critical",
                format!("{shown}: a drive is failing"),
                text(&a["reason"]),
            ),
            "warning"
                if a["mismatches"].as_u64().unwrap_or(0) > 0
                    || worst_errors >= crate::array_health::ERRORS_ALERTED =>
            {
                add(
                    format!("array-errors:{id}"),
                    "warning",
                    format!("{shown} needs a look"),
                    text(&a["reason"]),
                )
            }
            _ => {}
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
    if let Some(level) = cpu_heat(s) {
        add(
            "temperature".into(),
            level,
            "CPU temperature is high".into(),
            "Check cooling and current workload.",
        );
    }
    if nas_held(s) {
        let stopped = if named || held.is_empty() {
            String::new()
        } else {
            format!(" Stopped: {}.", held.join(", "))
        };
        add(
            "nas:hold".into(),
            "critical",
            "The apps are held: a drive on the network storage box stopped taking writes".into(),
            &format!("They are stopped so nothing is written to a drive that fails.{stopped} They start again by themselves once every drive on the NAS takes writes; restarting them by hand is refused until then. If the drive was repaired and they stay stopped, look at  journalctl -u {} .", common::shared("nas_watch", "service")),
        );
    }
    if let Some(apps) = s["applications"].as_array() {
        for a in apps {
            if held.contains(&text(&a["name"])) {
                continue;
            }
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
                // Running but not answering its own page (no Docker health check to say
                // so): the dashboard shows it as not responding, the alert agrees.
                || (a["state"] == "running" && a["responding"] == false)
            {
                add(
                    format!("app:{}", text(&a["id"])),
                    if a["health"] == "unhealthy" || a["responding"] == false {
                        "critical"
                    } else {
                        "warning"
                    },
                    format!("{} needs attention", text(&a["name"])),
                    "On the dashboard's Your apps page, choose Restart app on it (on a phone, tap ⋯ on the app). If it keeps happening, the guide shows what else to try.",
                );
            }
        }
    }
    if let Some(jobs) = s["backups"].as_array() {
        for j in jobs {
            // Waiting for a missing drive: its alert says what to do.
            if j["blocked"].is_string() {
                continue;
            }
            if ["fail", "stale", "partial"].contains(&text(&j["status"])) {
                add(
                    format!("backup:{}", text(&j["id"])),
                    if j["status"] == "fail" {
                        "critical"
                    } else {
                        "warning"
                    },
                    if j["rolled_back"] == true {
                        format!(
                            "{}: the update was undone, your apps work",
                            text(&j["name"])
                        )
                    } else {
                        format!("{} needs attention", text(&j["name"]))
                    },
                    j["reason"]
                        .as_str()
                        .unwrap_or("Review the backup schedule."),
                );
            }
        }
    }
    // The monthly schedule exists, but no backup password was chosen: each snapshot
    // holds the app password files unencrypted until one is.
    let monthly_installed = s["backups"].as_array().is_some_and(|jobs| {
        jobs.iter()
            .any(|j| j["id"] == "maintenance" && j["timer"]["installed"] == true)
    });
    if monthly_installed && s["backup_key"] == false {
        add(
            "backup-key".into(),
            "warning",
            "Monthly backups keep the app passwords unencrypted".into(),
            "No backup password was chosen at setup, so each monthly snapshot on the HDD holds the app password files in plain text (readable by root only, but by anyone who takes the drive). In a terminal on the server, in the NekoServer folder, run  sudo python3 backup_secrets.py --setup  to choose one: snapshots made from then on keep them encrypted.",
        );
    }
    if let Some(rows) = s["integrity"].as_array() {
        for c in rows {
            let count = |k: &str| c[k].as_u64().unwrap_or(0);
            let bad =
                count("damaged") + count("both_damaged") + count("disagree") + count("missing");
            if bad > 0 {
                add(
                    format!("integrity:{}", text(&c["id"])),
                    if count("both_damaged") + count("damaged") + count("missing") > 0 { "critical" } else { "warning" },
                    format!(
                        "{}: {} damaged or missing",
                        text(&c["name"]),
                        plural(bad, "file is", "files are")
                    ),
                    "Run  sudo bash install.sh --recover  and choose \"Repair damaged backup copies\"; it lists the files.",
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
/// The phone alerts' last delivery check is overdue (over 3 minutes old, or dated ahead):
/// only while their timer runs. A timer switched off by choice ("wanted": false) is not
/// an overdue delivery check.
fn delivery_late(timer_active: bool, checked_at: Option<f64>, now: f64) -> bool {
    timer_active && checked_at.is_none_or(|t| t > now + 30.0 || now - t > 180.0)
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
    if delivery_late(timer["active"] == true, saved["generated_at"].as_f64(), now) {
        status = "stale";
    }
    json!({"status":status,"timer":timer,"last_success":common::shown_time(&saved["last_success"],now),"last_attempt":num(&saved["last_attempt"],0.0,now+30.0),"pending":num(&saved["pending"],0.0,100.0)})
}
/// The drive the backups wait for, when one is missing.
fn away_drive(data_ok: bool, media_ok: bool) -> Option<&'static str> {
    if !data_ok {
        Some("Application SSD")
    } else if !media_ok {
        Some("Media HDD")
    } else {
        None
    }
}
/// The backups', the outage monitor's and the phone alerts' states, worked out as the
/// dashboard shows them (the same functions), printed as one line of JSON. The acceptance
/// check asks this instead of judging them itself: it kept a copy of these rules, which
/// already disagreed (a worker systemd could not start, or a run cut off part-way, passed
/// there; the review of 0.47.1).
pub fn job_states(a: &Args) -> Result<()> {
    common::require_root()?;
    let cfg = common::json(&a.path("config")?, 2_000_000);
    let now = common::now();
    // A drive counts only when it passes the check every job makes (UUID, filesystem,
    // Btrfs subvolume, the NAS disk's WWN), and the two are two filesystems: no report is
    // read from a drive that is not the one setup chose (the review of 0.47.2: a drive
    // the acceptance check refused still gave it the backups' reports).
    let checked = |key: &str, mount: &str| -> Result<Option<String>> {
        let path = common::mount_path(text(&cfg[mount]))?;
        Ok(crate::backup::checked_drive(&cfg, key, &path).ok())
    };
    let (data, media) = (
        checked("data_ssd", "data_mount")?,
        checked("media_hdd", "media_mount")?,
    );
    let apart = !matches!((&data, &media), (Some(a), Some(b)) if a == b);
    let ok = data.is_some() && apart;
    let media_ok = media.is_some() && apart;
    let states = json!({
        "backups": backups(&cfg, now, ok, away_drive(ok, media_ok)),
        "heartbeat": heartbeat(&cfg, now),
        "notifications": notification_status(now),
    });
    println!("{states}");
    Ok(())
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
    let first = app_drive(
        &cfg,
        "data_ssd",
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
    let (disks, mounted, ok, media_ok) = storage(&cfg, &mut cache, now)?;
    let arrays = arrays_section(
        &mounted,
        &mut cache,
        now,
        cfg["dashboard"]["smart_interval_seconds"]
            .as_f64()
            .unwrap_or(1800.0),
    );
    let (host, cpu) = metrics(&previous["cpu"]);
    let mut history=previous["history"].as_array().map(|rows|rows.iter().rev().take(119).filter_map(|v|{let t=num(&v["at"],0.0,now).as_f64()?;if now-t>7200.0{return None}Some(json!({"at":v["at"],"cpu":num(&v["cpu"],0.0,100.0),"memory":num(&v["memory"],0.0,100.0),"temperature":num(&v["temperature"],-10.0,150.0)}))}).collect::<Vec<_>>()).unwrap_or_default();
    history.reverse();
    history.push(json!({"at":now,"cpu":host["cpu_percent"],"memory":host["memory_percent"],"temperature":host["temperature"]}));
    let report = media_report(&cfg, now, ok);
    let away = away_drive(ok, media_ok);
    let (apps, containers, services) = applications(&cfg, report.as_ref(), ok.then_some(data));
    let mut result = json!({"format":"neko-dashboard-v1","version":VERSION,"generated_at":now,"metrics":host,"history":history,"storage":disks,"applications":apps,"backups":backups(&cfg,now,ok,away),"media":media(report.as_ref()),"recovery":recovery(&cfg,ok,media_ok),"heartbeat":heartbeat(&cfg,now),"acceptance":acceptance(&if ok{common::json(&data.join("reports/acceptance/latest.json"),2_000_000)}else{empty()},&cfg,now)});
    result["notifications"] = notification_status(now);
    result["containers"] = containers;
    result["container_services"] = services;
    result["gpu_telemetry"] = gpu_telemetry();
    result["startup"] = startup();
    // How often readings are taken, and when they count as late: decided here once and
    // written into the readings, so the page, the acceptance check and the phone alerts
    // read the same answer (the review of 0.47.0: three rules, which disagreed).
    let interval = cfg["dashboard"]["interval_seconds"]
        .as_u64()
        .filter(|s| (30..=300).contains(s))
        .unwrap_or(60);
    result["collect_interval"] = json!(interval);
    result["late_after"] = json!(late_after(interval));
    result["updates"] = updates(&cfg, ok);
    // NekoServer's own release: this one, the newest offered, an upgrade at work.
    result["system_updates"] = if ok {
        system_updates(&data.join(crate::updates::FOLDER).join("system.json"))
    } else {
        json!({})
    };
    result["nekoserver_release"] = if ok {
        let mut shown = crate::updates::release_for_dashboard(data);
        shown["upgrading"] = json!(Path::new(*crate::maintenance::UPGRADE_JOURNAL).exists());
        shown
    } else {
        json!({})
    };
    // New app versions waiting to be installed, and when each installs by itself.
    result["waiting_updates"] = if ok {
        let mut shown = crate::updates::for_dashboard(data, crate::updates::settle_days(&cfg), now);
        if media_ok {
            let media = Path::new(text(&cfg["media_mount"]));
            for app in shown["apps"].as_array_mut().into_iter().flatten() {
                app["history"] = crate::maintenance::app_history(media, text(&app["app"]));
            }
        }
        shown
    } else {
        json!({"apps": []})
    };
    result["tv"] = tv(&cfg, ok.then_some(data));
    result["assistant"] = assistant(&cfg, ok.then_some(data));
    // The apps the Tailscale gateway really publishes (its own report): only those get an
    // "Open" link on a phone; the others' addresses lead nowhere.
    result["remote_apps"] = if ok {
        remote_apps(&data.join("reports/gateway-tailnet.json"))
    } else {
        json!([])
    };
    result["integrity"] = integrity(data, ok, away, text(&cfg["photos"]["external_backup_uuid"]));
    result["backup_key"] = json!(backup_key_ready());
    result["tailscale"] = tailscale_login(now);
    result["network_storage"] = network_storage(&cfg, Path::new("/sys/class/iscsi_session"));
    result["arrays"] = arrays;
    mark_held(&mut result);
    result["alerts"] = alerts(&result);
    // The readings are published even when the SSD can't keep the collector's own state
    // (unplugged since the readings began, or remounted read-only after errors): a
    // failed private write must never freeze the page on an older, healthier reading.
    let mut kept = Ok(());
    if ok {
        let check = app_drive(
            &cfg,
            "data_ssd",
            data,
            Some(text(&cfg["storage"]["data_ssd"]["uuid"])),
            text(&cfg["storage"]["data_ssd"]["fstype"]),
        );
        if check["status"] == "mounted" {
            kept = save_state(
                &internal,
                &json!({"cpu":cpu,"history":result["history"],"smart":cache}),
            );
            if let Err(e) = &kept {
                unwritable(&mut result, &e.to_string());
            }
        }
    }
    let mut b = serde_json::to_vec(&result)?;
    b.push(b'\n');
    common::atomic(&public, &b, 0o644)?;
    kept
}
fn save_state(internal: &Path, state: &Value) -> Result<()> {
    fs::create_dir_all(internal)?;
    fs::set_permissions(internal, fs::Permissions::from_mode(0o700))?;
    common::atomic_json(&internal.join("collector.json"), state)
}
/// The app SSD is mounted but can't be written: raised as its drive alert.
fn unwritable(result: &mut Value, why: &str) {
    let alert = json!({"id":"mount:data_ssd","level":"critical","title":"App SSD can't be written",
        "detail":format!("The apps can't save anything on it ({}). It may be full or failing: check it, then restart the PC.", chars(why, 160))});
    if let Some(list) = result["alerts"].as_array_mut() {
        list.retain(|a| a["id"] != "mount:data_ssd");
        list.insert(0, alert);
    }
}

#[cfg(test)]
mod os_tests {
    use super::*;
    #[test]
    fn readings_count_as_late_by_one_rule_written_into_them() {
        assert_eq!(late_after(60), 180);
        assert_eq!(late_after(30), 180); // never under 3 minutes
        assert_eq!(late_after(300), 630);
    }
    #[test]
    fn a_nas_check_stuck_on_its_test_write_still_counts_as_waiting() {
        let seen = json!({"at": 1000.0, "drives": {"media_hdd": "ok"}});
        assert_eq!(
            nas_verdict(&seen, "media_hdd", 1100.0, || false),
            Some("ok")
        );
        // Ten minutes on, the check still at work: its write hangs.
        assert_eq!(
            nas_verdict(&seen, "media_hdd", 1600.0, || true),
            Some("waits")
        );
        assert_eq!(nas_verdict(&seen, "media_hdd", 1600.0, || false), None);
    }
    #[test]
    fn only_the_apps_the_gateway_publishes_get_a_phone_link() {
        // The audit of 0.45.6: a phone got an "Open" link for every app with a Tailscale
        // port, also for apps the gateway does not publish; those links led nowhere.
        let dir = std::env::temp_dir().join(format!("neko-remote-apps-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let report = dir.join("gateway-tailnet.json");
        assert_eq!(remote_apps(&report), json!([]));
        fs::write(&report, r#"{"format":"neko-gateway-v1","status":"configured","services":["jellyfin","immich","../x"]}"#).unwrap();
        assert_eq!(remote_apps(&report), json!(["jellyfin", "immich"]));
        fs::write(
            &report,
            r#"{"format":"neko-gateway-v1","status":"pending","services":["jellyfin"]}"#,
        )
        .unwrap();
        assert_eq!(remote_apps(&report), json!([]));
        assert!(include_str!("../../dashboard/app.js")
            .contains("words(state.status?.remote_apps).includes(id)"));
        let _ = fs::remove_dir_all(dir);
    }
    #[test]
    fn a_hung_drive_never_holds_up_the_dashboard() {
        use std::time::{Duration, Instant};
        let key = std::env::temp_dir().join(format!("neko-hung-{}", std::process::id()));
        // A question that never comes back in time (statvfs on a hung drive).
        let start = Instant::now();
        let hung = answered_within(&key, Duration::from_millis(200), || {
            std::thread::sleep(Duration::from_secs(3));
            Some(1)
        });
        assert_eq!(hung, None);
        assert!(start.elapsed() < Duration::from_secs(2));
        // Still busy: not asked again (no pile of stuck threads), answered at once.
        let start = Instant::now();
        assert_eq!(
            answered_within(&key, Duration::from_secs(5), || Some(2)),
            None
        );
        assert!(start.elapsed() < Duration::from_secs(1));
        // A drive that answers is shown as usual.
        assert!(usage(Path::new("/")).is_some());
    }
    #[test]
    fn failing_alert_delivery_is_said_on_the_page() {
        let shown =
            alerts(&json!({"notifications":{"status":"fail"},"heartbeat":{"status":"stale"}}));
        let ids: Vec<&str> = shown
            .as_array()
            .unwrap()
            .iter()
            .map(|a| a["id"].as_str().unwrap())
            .collect();
        assert_eq!(ids, ["notifications:delivery", "heartbeat:delivery"]);
        assert_eq!(
            alerts(&json!({"notifications":{"status":"pass"},"heartbeat":{"status":"unknown"}})),
            json!([])
        );
    }
    #[test]
    fn a_tailscale_sign_in_that_runs_out_is_told() {
        let now = 1_792_900_000.0;
        let signed_out = login_state(
            &json!({"BackendState":"NeedsLogin","Self":{"DNSName":"neko.tail1.ts.net."}}),
            now,
        );
        let shown = alerts(&json!({"tailscale": signed_out}));
        assert_eq!(shown[0]["id"], "tailscale:login");
        assert_eq!(shown[0]["level"], "critical");
        // A server signed in before, whose sign-in ran out, is told so; one never signed in
        // is told it is a setup step still to do.
        let expired = login_state(
            &json!({"BackendState":"NeedsLogin","Self":{"DNSName":"neko.tail1.ts.net.","TailscaleIPs":["100.64.0.1"]}}),
            now,
        );
        let shown = alerts(&json!({"tailscale": expired}));
        assert_eq!(
            (shown[0]["level"].as_str(), shown[0]["title"].as_str()),
            (
                Some("critical"),
                Some("Phone access is signed out of Tailscale")
            )
        );
        let never = login_state(
            &json!({"BackendState":"NeedsLogin","Self":{"UserID":0,"DNSName":"","TailscaleIPs":null}}),
            now,
        );
        let shown = alerts(&json!({"tailscale": never}));
        assert_eq!(
            (shown[0]["level"].as_str(), shown[0]["title"].as_str()),
            (Some("warning"), Some("Phone access is not set up yet"))
        );
        let soon = login_state(
            &json!({"BackendState":"Running","Self":{"KeyExpiry":"2026-10-30T00:00:00Z"}}),
            now,
        );
        assert_eq!(
            alerts(&json!({"tailscale": soon}))[0]["id"],
            "tailscale:expiry"
        );
        // Expiry switched off (no KeyExpiry), or far away: nothing to say.
        for status in [
            json!({"BackendState":"Running","Self":{}}),
            json!({"BackendState":"Running","Self":{"KeyExpiry":"2027-06-01T00:00:00Z"}}),
        ] {
            assert_eq!(
                alerts(&json!({"tailscale": login_state(&status, now)})),
                json!([])
            );
        }
        assert_eq!(alerts(&json!({"tailscale": null})), json!([]));
    }
    #[test]
    fn each_backup_names_the_restore_tests_of_what_it_holds() {
        let cloud_only = json!({"photos":{"deploy_immich":false},"nextcloud":{"enabled":true}});
        assert_eq!(
            restore_tests(&cloud_only, "external"),
            json!(["cloud_restore"])
        );
        let both = json!({"photos":{"deploy_immich":true},"nextcloud":{"enabled":true}});
        assert_eq!(
            restore_tests(&both, "external"),
            json!(["independent_restore", "cloud_restore"])
        );
        assert_eq!(restore_tests(&both, "music"), json!(["music_restore"]));
        assert_eq!(restore_tests(&both, "maintenance"), json!([]));
        for key in [
            "independent_restore",
            "cloud_restore",
            "music_restore",
            "app_restore",
            "host_restore",
        ] {
            assert!(manual(key).is_some(), "{key}");
        }
    }
    #[test]
    fn jobs_of_a_switched_off_app_are_retired_not_overdue() {
        let both = json!({"photos":{"deploy_immich":true},"nextcloud":{"enabled":true}});
        for key in ["music", "archive", "database", "external", "host"] {
            assert_eq!(retired(&both, key), None, "{key}");
        }
        let cloud_only = json!({"photos":{"deploy_immich":false},"nextcloud":{"enabled":true}});
        assert!(retired(&cloud_only, "archive").is_some());
        assert!(retired(&cloud_only, "database").is_some());
        assert_eq!(retired(&cloud_only, "external"), None);
        let neither = json!({"photos":{"deploy_immich":false},"music":{"backup_enabled":false}});
        assert!(retired(&neither, "external").is_some());
        assert!(retired(&neither, "music").is_some());
        assert_eq!(retired(&neither, "applications"), None);
        // The media apps left out (0.50.0): their backups are off, the others expected.
        let no_media = json!({"media_apps":{"wanted":false},"photos":{"deploy_immich":true},
            "nextcloud":{"enabled":true}});
        for key in ["music", "applications", "archive", "database"] {
            assert!(retired(&no_media, key).is_some(), "{key}");
        }
        for key in ["external", "host"] {
            assert_eq!(retired(&no_media, key), None, "{key}");
        }
    }
    #[test]
    fn a_wanted_schedule_that_stopped_is_flagged() {
        let on = json!({"installed":true,"active":true});
        let off = json!({"installed":true,"active":false});
        let gone = json!({"installed":false,"active":false});
        assert_eq!(schedule_stopped(&on, true, true), None);
        assert!(schedule_stopped(&off, true, false).is_some());
        assert!(schedule_stopped(&gone, true, true).is_some());
        // Never set up yet, or systemd could not be asked: no evidence.
        assert_eq!(schedule_stopped(&gone, true, false), None);
        assert_eq!(schedule_stopped(&off, false, true), None);
        // The upgrade has no schedule to lose.
        assert!(scheduled("applications") && !scheduled("upgrade"));
        assert_eq!(schedule_stopped(&gone, scheduled("upgrade"), true), None);
    }
    #[test]
    fn a_run_cut_off_by_a_restart_is_not_running() {
        let now = 10_000.0;
        assert!(cut_off(
            "LoadState=loaded\nActiveState=inactive\n",
            Some(now - 600.0),
            now
        ));
        assert!(cut_off("ActiveState=failed\n", None, now));
        assert!(!cut_off("ActiveState=active\n", Some(now - 600.0), now));
        assert!(!cut_off("ActiveState=activating\n", Some(now - 600.0), now));
        // Just finished: its report is about to be rewritten.
        assert!(!cut_off("ActiveState=inactive\n", Some(now - 30.0), now));
        // systemctl could not be asked: no evidence either way.
        assert!(!cut_off("", Some(now - 600.0), now));
    }
    #[test]
    fn a_drive_full_for_the_apps_reads_full() {
        // 1000 blocks, 50 free but all of them kept for the system (ext4's reserve).
        let (total, used) = space(1000, 50, 0, 4096).unwrap();
        assert_eq!((total, used), (950 * 4096, 950 * 4096));
        // Half used, the reserve not counted as free space.
        assert_eq!(space(1000, 550, 500, 1).unwrap(), (950, 450));
        assert!(space(10, 20, 0, 1).is_none());
    }
    #[test]
    fn a_backup_worker_that_never_started_is_a_failure() {
        let unit = "nekoserver-app-backup";
        // systemd could not run the program at all: no report, but the service says so.
        let why = service_failure(
            unit,
            "LoadState=loaded\nActiveState=failed\nResult=exit-code\nExecMainStatus=203\n",
        )
        .unwrap();
        assert!(why.contains("exit-code, exit 203"), "{why}");
        assert!(why.contains("systemctl status nekoserver-app-backup.service"));
        assert!(service_failure(
            unit,
            "LoadState=loaded\nActiveState=inactive\nResult=start-limit-hit\nExecMainStatus=0\n"
        )
        .unwrap()
        .contains("start-limit-hit)"));
        // A clean or running service, or one that isn't installed, says nothing.
        for show in [
            "LoadState=loaded\nActiveState=inactive\nResult=success\nExecMainStatus=0\n",
            "LoadState=loaded\nActiveState=activating\nResult=exit-code\n",
            "LoadState=not-found\nActiveState=inactive\nResult=success\n",
            "",
        ] {
            assert!(service_failure(unit, show).is_none(), "{show}");
        }
    }
    #[test]
    fn a_backup_that_never_succeeded_becomes_overdue_after_a_grace_day() {
        let day = 86400.0;
        let now = 100.0 * day;
        // Set up yesterday with a daily schedule: not overdue yet.
        assert!(!first_overdue(None, Some(now - day), now, day));
        // Set up three days ago and never successful: overdue.
        assert!(first_overdue(None, Some(now - 3.0 * day), now, day));
        // No schedule on this server: nothing is due.
        assert!(!first_overdue(None, None, now, day));
        // With a success, only its age counts.
        assert!(!first_overdue(
            Some(now - 0.5 * day),
            Some(now - 30.0 * day),
            now,
            day
        ));
        assert!(first_overdue(Some(now - 2.0 * day), None, now, day));
    }
    #[test]
    fn a_check_that_could_not_run_since_the_last_report_makes_it_stale() {
        // The review of 0.47.9: the hourly check's early errors (a drive refused, its
        // lock, broken settings) wrote no report, so the last PASS stayed "current".
        let failed = "LoadState=loaded\nActiveState=failed\nResult=exit-code\nExecMainStatus=1\nExecMainExitTimestampMonotonic=1000000000\n";
        // The service ended 100 s ago (1000 s after boot, uptime 1100 s); the report is an hour old.
        let why = check_failed_since(failed, Some(3600.0), Some(1100.0)).unwrap();
        assert!(
            why.contains("Its service could not run (exit-code, exit 1)"),
            "{why}"
        );
        // No report at all: the failure still counts.
        assert!(check_failed_since(failed, None, Some(1100.0)).is_some());
        // A report written after the failure (50 s old): current.
        assert!(check_failed_since(failed, Some(50.0), Some(1100.0)).is_none());
        // Exit 2 and 3 come after a written report (fail / needs_setup): not a failure to run.
        let found = failed.replace("ExecMainStatus=1", "ExecMainStatus=2");
        assert!(check_failed_since(&found, Some(3600.0), Some(1100.0)).is_none());
        let found = failed.replace("ExecMainStatus=1", "ExecMainStatus=3");
        assert!(check_failed_since(&found, Some(3600.0), Some(1100.0)).is_none());
        // A run that worked, or one still at work: nothing.
        let fine = "LoadState=loaded\nActiveState=inactive\nResult=success\nExecMainStatus=0\nExecMainExitTimestampMonotonic=1000000000\n";
        assert!(check_failed_since(fine, Some(3600.0), Some(1100.0)).is_none());
        let busy = failed.replace("ActiveState=failed", "ActiveState=activating");
        assert!(check_failed_since(&busy, Some(3600.0), Some(1100.0)).is_none());
        // Without the uptime the exit time can't be placed: nothing is claimed.
        assert!(check_failed_since(failed, Some(3600.0), None).is_none());
        // The dashboard's media block carries the reason.
        let shown = media(Some(&MediaReport {
            saved: json!({}),
            age: Some(3600.0),
            status: "stale",
            reason: Some(why.clone()),
        }));
        assert_eq!(shown["status"], "stale");
        assert_eq!(shown["reason"], why);
    }
    #[test]
    fn an_app_check_older_than_the_apps_last_start_says_nothing() {
        let report = |age: f64, http: Value| MediaReport {
            saved: json!({"services": {"jellyfin": {"container": "running", "http_status": http}}}),
            age: Some(age),
            status: "current",
            reason: None,
        };
        // Checked 30 minutes ago and failed; the app has been up for 2 hours: that's it.
        assert_eq!(
            responding(Some(&report(1800.0, Value::Null)), "jellyfin", Some(7200.0)),
            json!(false)
        );
        assert_eq!(
            responding(Some(&report(1800.0, json!(200))), "jellyfin", Some(7200.0)),
            json!(true)
        );
        // Restarted 2 minutes ago (from the dashboard, say): the old failure no longer applies.
        assert_eq!(
            responding(Some(&report(1800.0, Value::Null)), "jellyfin", Some(120.0)),
            Value::Null
        );
        // Not running at the check, or no current check: unknown.
        assert_eq!(
            responding(Some(&report(60.0, Value::Null)), "immich", Some(7200.0)),
            Value::Null
        );
        assert_eq!(responding(None, "jellyfin", Some(7200.0)), Value::Null);
        // Up 5 min 59 s (Docker says "Up 5 minutes"); the failed check was 5 min 30 s ago,
        // after the start: it counts.
        assert_eq!(
            responding(Some(&report(330.0, Value::Null)), "jellyfin", Some(359.0)),
            json!(false)
        );
        let starts = started_at(
            "abc123 2026-10-22T12:00:01.123456789Z\nnever 0001-01-01T00:00:00Z\nbad not-a-time\n",
        );
        assert_eq!(starts, [("abc123".to_string(), 1792670401.123)]);
    }
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
    fn an_aliased_unit_is_listed_once() {
        // Debian: smb.service is a link to smbd.service, so both names answer as smbd.
        let block =
            "Id=smbd.service\nLoadState=loaded\nUnitFileState=enabled\nActiveState=inactive";
        let units = parse_startup(&format!("{block}\n\n{block}"));
        assert_eq!(units.len(), 1);
        assert_eq!(units[0]["unit"], "smbd.service");
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
        // The dashboard resting until its listening socket starts it is running; with its
        // socket stopped too, it is not.
        let resting = |socket: &str| {
            parse_startup(&format!("Id=nekoserver-dashboard.service\nLoadState=loaded\nUnitFileState=enabled\nActiveState=inactive\nTriggeredBy=nekoserver-dashboard.socket\n\nId=nekoserver-dashboard.socket\nLoadState=loaded\nUnitFileState=enabled\nActiveState={socket}"))
        };
        assert_eq!(resting("active")[0]["active"], "active");
        assert_eq!(
            resting("active").len(),
            1,
            "the socket itself is not listed"
        );
        let shown = alerts(&json!({ "startup": resting("inactive") }));
        assert!(shown
            .as_array()
            .unwrap()
            .iter()
            .any(|a| a["id"] == "startup:nekoserver-dashboard.service:failed"));
        // Enabled but failed is not healthy; a finished one-shot is.
        let units = parse_startup("Id=smb.service\nLoadState=loaded\nUnitFileState=enabled\nActiveState=failed\n\nId=nekoserver-app-resume.service\nLoadState=loaded\nUnitFileState=enabled\nActiveState=inactive");
        let shown = alerts(&json!({ "startup": units }));
        let ids = shown
            .as_array()
            .unwrap()
            .iter()
            .map(|a| a["id"].as_str().unwrap().to_string())
            .collect::<Vec<_>>();
        assert_eq!(ids, ["startup:smb.service:failed"]);
        assert_eq!(shown[0]["level"], "critical");
        assert_eq!(
            shown[0]["title"],
            "Shared folders (Samba) has stopped with an error"
        );
    }
    #[test]
    fn a_missing_backup_key_is_an_alert_once_the_monthly_schedule_exists() {
        let monthly = |installed: bool| json!([{"id":"maintenance","name":"Monthly maintenance","status":"pass","timer":{"installed":installed}}]);
        let shown = alerts(&json!({"backups": monthly(true), "backup_key": false}));
        let ids: Vec<&str> = shown
            .as_array()
            .unwrap()
            .iter()
            .map(|a| a["id"].as_str().unwrap())
            .collect();
        assert_eq!(ids, ["backup-key"]);
        assert!(shown[0]["detail"]
            .as_str()
            .unwrap()
            .contains("backup_secrets.py --setup"));
        assert!(
            alerts(&json!({"backups": monthly(true), "backup_key": true}))
                .as_array()
                .unwrap()
                .is_empty()
        );
        // No monthly schedule yet, or the key's state not reported: nothing.
        assert!(
            alerts(&json!({"backups": monthly(false), "backup_key": false}))
                .as_array()
                .unwrap()
                .is_empty()
        );
        assert!(alerts(&json!({"backups": monthly(true)}))
            .as_array()
            .unwrap()
            .is_empty());
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
        crate::backup::save_integrity(&data, "usb_photos", &report, crate::backup::Check::Full)
            .unwrap();
        crate::backup::save_integrity(
            &data,
            "archive",
            &Default::default(),
            crate::backup::Check::Full,
        )
        .unwrap();
        let rows = integrity(&data, true, None, "");
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
        assert!(!shown[0].to_string().contains("b.jpg"), "{}", shown[0]);
        assert!(text(&shown[0]["detail"]).contains("Repair damaged backup copies"));
        let _ = std::fs::remove_dir_all(data);
    }
    #[test]
    fn major_updates_found_by_the_daily_check_are_alerted() {
        let root = std::env::temp_dir().join(format!("neko-updates-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let notes = root.join(crate::updates::FOLDER).join("notes");
        fs::create_dir_all(&notes).unwrap();
        fs::write(
            notes.join("nextcloud-major-pending.json"),
            r#"{"status":"manual_major_upgrade_required","current":"31","available":"32"}"#,
        )
        .unwrap();
        // The check writes its notes again each time: a notice it no longer finds is gone.
        let cfg = json!({"data_mount": root.to_string_lossy()});
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
            "Failed while copying photos and videos to the USB drive: Storage operation failed: mount exited with 32 (wrong fs type)."
        );
        assert_eq!(
            failure_reason(&json!({})),
            "Last run failed; inspect its service logs"
        );
    }
    #[test]
    fn partial_backup_is_a_warning_without_its_file_names() {
        let reason = partial_reason(
            &json!({"in_use":1,"unsupported_names":1,"files":["alice/open.docx","bob/a:b.txt"]}),
        );
        assert!(reason.starts_with("2 files were not copied"));
        // The names stay in the root-only job report: the status file is readable by all.
        assert!(
            !reason.contains("alice") && !reason.contains("bob"),
            "{reason}"
        );
        assert!(reason.contains("reports/jobs"), "{reason}");
        let db = partial_reason(
            &json!({"in_use":0,"unsupported_names":0,"files":[],"database":"the newest Immich database copy is 10 days old"}),
        );
        assert_eq!(db, "The newest Immich database copy is 10 days old");
        let cloud = partial_reason(
            &json!({"in_use":0,"unsupported_names":0,"files":[],"source":"the Nextcloud files folder is missing, so Nextcloud was not copied"}),
        );
        assert_eq!(
            cloud,
            "The Nextcloud files folder is missing, so Nextcloud was not copied"
        );
        let shown = alerts(
            &json!({"backups":[{"id":"external","name":"USB backup","status":"partial","reason":reason}]}),
        );
        assert_eq!(shown[0]["level"], "warning");
        assert!(!text(&shown[0]["detail"]).contains("bob/a:b.txt"));
    }
    #[test]
    fn a_monthly_run_done_in_part_says_why() {
        // The monthly run lists what it left for later as sentences; the dashboard used to
        // show only "Incomplete; see the job's report" for them.
        let reason = partial_reason(&json!([
            "USB check: the USB drive is full",
            "Immich release: it could not be looked up"
        ]));
        assert_eq!(
            reason,
            "USB check: the USB drive is full. Immich release: it could not be looked up"
        );
        let night = partial_reason(&json!(["night window ended before the apps were stopped"]));
        assert_eq!(night, "Night window ended before the apps were stopped");
        let many: Vec<String> = (1..=7).map(|n| format!("part {n}")).collect();
        assert!(partial_reason(&json!(many)).ends_with("Part 5 (and 2 more)"));
        assert_eq!(
            partial_reason(&json!([])),
            "Incomplete; see the job's report"
        );
    }
    #[test]
    fn a_full_hdd_says_what_stops_and_how_to_free_it() {
        let drive = |percent: f64| json!({"storage":[{"id":"media_hdd","name":"Media HDD","status":"mounted","used_percent":percent}]});
        let full = alerts(&drive(100.0));
        let row = &full.as_array().unwrap()[0];
        assert_eq!(row["id"], "space:media_hdd");
        assert_eq!(row["level"], "critical");
        assert_eq!(row["title"], "Media HDD is almost full");
        assert!(text(&row["detail"]).contains("Downloads, the nightly backups"));
        let filling = alerts(&drive(92.0));
        let row = &filling.as_array().unwrap()[0];
        assert_eq!(
            (text(&row["level"]), text(&row["title"])),
            ("warning", "Media HDD is filling up")
        );
        assert!(alerts(&drive(50.0)).as_array().unwrap().is_empty());
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

#[cfg(test)]
mod integrity_tests {
    use super::*;
    use crate::backup::{archive_checked, save_integrity, Check};
    fn write_old(path: &Path, bytes: &[u8]) {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, bytes).unwrap();
        let past = libc::timespec {
            tv_sec: 1_000_000_000,
            tv_nsec: 0,
        };
        let c = std::ffi::CString::new(path.as_os_str().as_encoded_bytes()).unwrap();
        unsafe { libc::utimensat(libc::AT_FDCWD, c.as_ptr(), [past, past].as_ptr(), 0) };
    }
    /// The monthly full check finds a damaged copy of a photo Immich no longer has; the
    /// next weekly (quick) run must not make it look repaired.
    #[test]
    fn a_quick_run_keeps_the_damage_a_full_check_found() {
        let root = std::env::temp_dir().join(format!("neko-integrity-keep-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let (library, hdd, data) = (root.join("library"), root.join("hdd"), root.join("data"));
        write_old(&library.join("upload/u/b.jpg"), b"photo b");
        let (_, quick) = archive_checked(&library, &hdd, true, 60, Check::Quick).unwrap();
        save_integrity(&data, "archive", &quick, Check::Quick).unwrap();
        let row = &integrity(&data, true, None, "")[0];
        assert!(
            row["full_at"].is_null(),
            "a quick run is not a full check: {row}"
        );
        assert_eq!(row["check"], "quick");
        fs::remove_file(library.join("upload/u/b.jpg")).unwrap();
        fs::write(hdd.join("upload/u/b.jpg"), b"photo ?").unwrap();
        let (_, full) = archive_checked(&library, &hdd, true, 60, Check::Full).unwrap();
        save_integrity(&data, "archive", &full, Check::Full).unwrap();
        let found = integrity(&data, true, None, "")[0].clone();
        assert_eq!(found["damaged"], 1, "{found}");
        let full_at = found["full_at"].as_f64().unwrap();
        let (_, quick) = archive_checked(&library, &hdd, true, 60, Check::Quick).unwrap();
        save_integrity(&data, "archive", &quick, Check::Quick).unwrap();
        let after = integrity(&data, true, None, "")[0].clone();
        assert_eq!(after["damaged"], 1, "{after}");
        // The status file is readable by every account: counts, never the file names.
        assert!(after.get("files").is_none(), "{after}");
        assert!(!after.to_string().contains("b.jpg"), "{after}");
        assert_eq!(
            after["full_at"].as_f64(),
            Some(full_at),
            "the full check's date stays"
        );
        // The recovery menu reads the same list.
        let saved = common::json(&data.join("reports/integrity.json"), 1_000_000);
        assert_eq!(
            saved["sections"]["archive"]["damaged"],
            json!(["upload/u/b.jpg"])
        );
        // A later full check that finds nothing wrong replaces the list.
        fs::write(hdd.join("upload/u/b.jpg"), b"photo b").unwrap();
        let (_, full) = archive_checked(&library, &hdd, true, 60, Check::Full).unwrap();
        save_integrity(&data, "archive", &full, Check::Full).unwrap();
        assert_eq!(integrity(&data, true, None, "")[0]["damaged"], 0);
        let _ = fs::remove_dir_all(root);
    }
}

#[cfg(test)]
mod heat_tests {
    use super::*;
    fn level(
        temp: f64,
        crit: Option<f64>,
        minutes_hot: u32,
        history_temp: f64,
    ) -> Option<&'static str> {
        let now = 10_000.0;
        let history: Vec<Value> = (0..=minutes_hot)
            .map(|m| json!({"at": now - 60.0 * m as f64, "temperature": history_temp}))
            .collect();
        cpu_heat(&json!({"generated_at": now, "history": history,
            "metrics": {"temperature": temp, "temperature_crit": crit}}))
    }
    /// A Ryzen at its designed 95 °C, or one hot reading, is no alarm; five minutes above
    /// 95 °C, the chip's own limit or 105 °C are.
    #[test]
    fn processor_heat_is_judged_over_five_minutes_or_by_the_chips_own_limit() {
        assert_eq!(
            level(95.0, None, 10, 95.0),
            Some("warning"),
            "95 °C is not above 95 °C"
        );
        assert_eq!(level(99.0, None, 1, 99.0), None, "one minute is not five");
        assert_eq!(level(99.0, None, 5, 99.0), Some("critical"));
        assert_eq!(
            level(99.0, None, 5, 70.0),
            None,
            "it was cool a few minutes ago"
        );
        assert_eq!(
            level(100.0, Some(100.0), 0, 100.0),
            Some("critical"),
            "at the chip's limit"
        );
        assert_eq!(level(106.0, None, 0, 106.0), Some("critical"));
        assert_eq!(level(85.0, None, 5, 85.0), Some("warning"));
        assert_eq!(level(85.0, None, 2, 85.0), None);
        let raised = alerts(&json!({"generated_at": 10_000.0, "metrics": {"temperature": 99.0}}));
        assert!(raised.as_array().unwrap().is_empty(), "{raised}");
    }
}

#[cfg(test)]
mod heartbeat_tests {
    use super::*;
    #[test]
    fn no_check_in_yet_right_after_boot_is_not_a_missed_one() {
        assert!(
            !heartbeat_late(None, 1000.0, 300, Some(60.0)),
            "first check-in still to come"
        );
        assert!(heartbeat_late(None, 1000.0, 300, Some(600.0)));
        assert!(heartbeat_late(None, 1000.0, 300, None));
        assert!(!heartbeat_late(Some(900.0), 1000.0, 300, Some(60.0)));
        assert!(heartbeat_late(Some(500.0), 1000.0, 300, Some(6000.0)));
    }
}

#[cfg(test)]
mod acceptance_tests {
    use super::*;
    /// "stale" is a complete check over a day old; a failed or unfinished one stays so.
    #[test]
    fn only_a_complete_check_turns_stale() {
        let cfg = json!({"acceptance_scope": "ab".repeat(32), "acceptance_profiles": ["full", "accounts"]});
        let report = |automatic: &str, complete: bool| {
            json!({"format": "neko-acceptance-v1", "profile": "full", "version": VERSION,
                "generated_at": 1000.0, "dashboard_scope": scope(&cfg), "acceptance_complete": complete,
                "automatic": [{"status": automatic}],
                "manual": [{"id": "host_restore", "status": "pass", "evidence": "user_reported", "confirmed_at": 900.0}]})
        };
        let later = 1000.0 + 2.0 * 86400.0;
        assert_eq!(
            acceptance(&report("pass", true), &cfg, 1100.0)["status"],
            "complete"
        );
        assert_eq!(
            acceptance(&report("pass", true), &cfg, later)["status"],
            "stale"
        );
        assert_eq!(
            acceptance(&report("fail", false), &cfg, later)["status"],
            "failed"
        );
        assert_eq!(
            acceptance(&report("needs_setup", false), &cfg, later)["status"],
            "pending"
        );
    }
    /// The review of 0.47.1: the profiles and the scope are setup's, read from the
    /// settings. A list kept here missed "accounts", and the scope was worked out twice.
    #[test]
    fn the_profiles_and_the_scope_are_the_ones_setup_wrote() {
        let cfg = json!({"acceptance_scope": "cd".repeat(32), "acceptance_profiles": ["full", "accounts"]});
        let run = |profile: &str, scope: &str| {
            json!({"format": "neko-acceptance-v1", "profile": profile, "version": VERSION,
                "generated_at": 1000.0, "dashboard_scope": scope, "acceptance_complete": true,
                "automatic": [{"status": "pass"}],
                "manual": [{"id": "host_restore", "status": "pass", "evidence": "user_reported", "confirmed_at": 900.0}]})
        };
        let ours = "cd".repeat(32);
        assert_eq!(
            acceptance(&run("accounts", &ours), &cfg, 1100.0)["status"],
            "complete"
        );
        assert_eq!(
            acceptance(&run("tv", &ours), &cfg, 1100.0)["status"],
            "unknown"
        );
        assert_eq!(
            acceptance(&run("full", &"ef".repeat(32)), &cfg, 1100.0)["status"],
            "outdated"
        );
        // Settings without a fingerprint match no run.
        let none = json!({"acceptance_profiles": ["full"]});
        assert_eq!(
            acceptance(&run("full", ""), &none, 1100.0)["status"],
            "outdated"
        );
    }
}

#[cfg(test)]
mod user_panel_tests {
    use super::*;
    /// Ask Neko's page is shown only when the assistant was chosen and its model is set up.
    #[test]
    fn the_assistant_is_shown_only_when_set_up() {
        let root =
            std::env::temp_dir().join(format!("neko-collect-assistant-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("reports")).unwrap();
        let yes = json!({"assistant": {"wanted": true}});
        assert_eq!(assistant(&yes, Some(&root)), json!({"set_up": false}));
        fs::write(
            root.join("reports/assistant.json"),
            r#"{"format":"neko-assistant-v1","model":"qwen3.5:4b","memory":"5g","cpus":"2"}"#,
        )
        .unwrap();
        assert_eq!(
            assistant(&yes, Some(&root)),
            json!({"set_up": true, "model": "qwen3.5:4b", "memory": "5g", "cpus": "2"})
        );
        assert_eq!(
            assistant(&json!({"assistant": {"wanted": false}}), Some(&root)),
            json!({"set_up": false})
        );
        assert_eq!(assistant(&json!({}), Some(&root)), json!({"set_up": false}));
        fs::write(
            root.join("reports/assistant.json"),
            r#"{"model":null,"stopped":true}"#,
        )
        .unwrap();
        assert_eq!(assistant(&yes, Some(&root)), json!({"set_up": false}));
        // The SSD missing: not said, so the page keeps what it showed.
        assert_eq!(assistant(&yes, None), json!({"set_up": null}));
        assert_eq!(assistant(&json!({}), None), json!({"set_up": false}));
        let _ = fs::remove_dir_all(&root);
    }
    /// The HDD missing (user panel UP-03/UP-04): one drive alert naming the apps it
    /// stopped, no "check its container logs" for each, and no backup alerts of their own.
    #[test]
    fn a_missing_drive_is_one_alert_naming_the_apps_it_stopped() {
        let apps: Vec<Value> = ["Jellyfin", "Immich", "Nextcloud"]
            .iter()
            .map(|n| json!({"id": n.to_lowercase(), "name": n, "state": "exited"}))
            .chain([
                json!({"id": "seerr", "name": "Seerr", "state": "running", "health": "unhealthy"}),
            ])
            .collect();
        let s = json!({
            "storage": [{"id": "os", "name": "OS SSD", "status": "mounted"},
                        {"id": "media_hdd", "name": "Media HDD", "status": "missing"}],
            "applications": apps,
            "backups": [{"id": "archive", "name": "Photo archive", "status": "stale",
                         "blocked": "The Media HDD is missing, so this backup can't run until it is back."}]});
        let shown = alerts(&s);
        let ids: Vec<&str> = shown
            .as_array()
            .unwrap()
            .iter()
            .map(|a| text(&a["id"]))
            .collect();
        assert_eq!(ids, ["mount:media_hdd", "app:seerr"], "{shown}");
        assert!(text(&shown[0]["detail"]).starts_with("Not connected, or not the drive chosen at setup. Stopped until it is back, so nothing is written in its place: Jellyfin, Immich, Nextcloud. Reconnect it"));
        // An app that is not stopped by the drive still has its own alert, with advice
        // a household can follow.
        assert!(text(&shown[1]["detail"]).contains("Restart app"));
        assert!(!shown.to_string().contains("container"));
        // With the drive back, stopped apps are alerts again.
        let mut back = s.clone();
        back["storage"][1]["status"] = json!("mounted");
        back["backups"][0]
            .as_object_mut()
            .unwrap()
            .remove("blocked");
        let ids: Vec<String> = alerts(&back)
            .as_array()
            .unwrap()
            .iter()
            .map(|a| text(&a["id"]).to_string())
            .collect();
        assert!(ids.contains(&"app:jellyfin".to_string()), "{ids:?}");
        assert!(ids.contains(&"backup:archive".to_string()), "{ids:?}");
    }
    #[test]
    fn a_new_usb_drive_inherits_no_checksum_result_from_the_old_one() {
        let data = std::env::temp_dir().join(format!("neko-usb-integrity-{}", std::process::id()));
        let _ = fs::remove_dir_all(&data);
        fs::create_dir_all(&data).unwrap();
        let clean = crate::backup::Integrity::default();
        use crate::backup::{save_integrity_on, Check};
        save_integrity_on(&data, "usb_photos", &clean, Check::Full, "old-uuid").unwrap();
        let shown = |usb: &str| integrity(&data, true, None, usb);
        assert_eq!(shown("old-uuid")[0]["id"], "usb_photos");
        assert!(shown("old-uuid")[0]["full_at"].is_number());
        // A new drive in the settings: the old drive's check is not shown as its own.
        assert_eq!(shown("new-uuid"), json!([]));
        // Its first (quick) copy carries no full check over from the old drive.
        save_integrity_on(&data, "usb_photos", &clean, Check::Quick, "new-uuid").unwrap();
        let row = shown("new-uuid")[0].clone();
        assert_eq!(row["check"], "quick");
        assert!(row["full_at"].is_null(), "{row}");
        // A report from before drive ids were recorded (no "drive" at all) may be any
        // drive's: a new drive's quick copy carries nothing over from it either.
        let path = data.join("reports/integrity.json");
        let mut all = common::json(&path, 1 << 20);
        let mut legacy = all["sections"]["usb_photos"].clone();
        legacy.as_object_mut().unwrap().remove("drive");
        legacy["full_at"] = json!(1_700_000_000.0);
        legacy["damaged"] = json!(["old/photo.jpg"]);
        legacy["counts"]["damaged"] = json!(1);
        all["sections"]["usb_photos"] = legacy;
        common::atomic_json(&path, &all).unwrap();
        // The read side: until the new drive's own copy rewrites the entry, the legacy one
        // is not shown for it (the review of 0.47.10); with no USB drive set it still is.
        common::atomic_json(&path, &all).unwrap();
        assert_eq!(shown("third-uuid"), json!([]));
        assert_eq!(shown("")[0]["full_at"], json!(1_700_000_000.0));
        save_integrity_on(&data, "usb_photos", &clean, Check::Quick, "third-uuid").unwrap();
        let row = shown("third-uuid")[0].clone();
        assert!(row["full_at"].is_null(), "{row}");
        assert_eq!(row["damaged"], 0, "{row}");
        let _ = fs::remove_dir_all(&data);
    }
    #[test]
    fn the_app_drives_are_the_ones_every_job_checks_before_anything_is_read() {
        // The review of 0.47.2: the page read the reports of a drive every backup refused
        // (the right UUID and filesystem, another Btrfs subvolume or NAS disk).
        let source = include_str!("collect.rs");
        let body = |start: &str| {
            let at = source.find(start).unwrap();
            &source[at..at + source[at..].find("\n}\n").unwrap()]
        };
        assert!(body("fn app_drive(").contains("crate::backup::checked_drive(cfg, key, path)"));
        assert!(body("fn storage(").contains("app_drive(cfg, key, &path, expected, fs)"));
        let run = body("pub fn run(a: &Args)");
        assert_eq!(
            run.matches("app_drive(").count(),
            2,
            "reading and saving the state"
        );
        assert!(!run.contains("mount_info("));
        // The OS drive has no identity to check.
        assert_eq!(
            app_drive(&json!({}), "os", Path::new("/nonexistent-neko"), None, "")["status"],
            "missing"
        );
    }
    #[test]
    fn a_failing_job_stays_a_failure_and_a_switched_off_timer_is_never_late() {
        // Kept here once the acceptance check's copy of these rules went (0.47.2).
        assert_eq!(overdue_state("fail", true), "fail");
        assert_eq!(overdue_state("pass", true), "stale");
        assert_eq!(overdue_state("pass", false), "pass");
        assert_eq!(overdue_state("running", true), "stale");
        assert!(!delivery_late(false, None, 1000.0));
        assert!(delivery_late(true, None, 1000.0));
        assert!(delivery_late(true, Some(700.0), 1000.0));
        assert!(!delivery_late(true, Some(900.0), 1000.0));
        assert!(delivery_late(true, Some(1100.0), 1000.0)); // dated ahead
    }
    #[test]
    fn apps_held_by_the_nas_check_are_one_alert_and_marked_held() {
        // The 0.47.1 preview: each app the hold stopped raised "needs attention, choose
        // Restart app", which the start guard refuses, and the hold itself had no alert.
        let mut s = json!({
        "storage": [{"id": "media_hdd", "name": "Media HDD", "status": "mounted",
                     "network": true, "held_by_nas_watch": true}],
        "applications": [
            {"id": "jellyfin", "name": "Jellyfin", "state": "exited"},
            {"id": "immich", "name": "Immich", "state": "exited"},
            {"id": "seerr", "name": "Seerr", "state": "running", "health": "unhealthy"}
        ]});
        mark_held(&mut s);
        let held: Vec<bool> = s["applications"]
            .as_array()
            .unwrap()
            .iter()
            .map(|a| a["held"] == true)
            .collect();
        assert_eq!(held, [true, true, false]);
        let shown = alerts(&s);
        let ids: Vec<&str> = shown
            .as_array()
            .unwrap()
            .iter()
            .map(|a| text(&a["id"]))
            .collect();
        assert_eq!(ids, ["nas:hold", "app:seerr"], "{shown}");
        assert_eq!(shown[0]["level"], "critical");
        let detail = text(&shown[0]["detail"]);
        assert!(detail.contains("Stopped: Jellyfin, Immich."), "{detail}");
        assert!(detail.contains("nekoserver-nas-watch.service"), "{detail}");
        assert!(crate::notify::label_of("nas:hold").is_some_and(|l| l.contains("held")));
        // The hold lifted: stopped apps are alerts of their own again.
        s["storage"][0]["held_by_nas_watch"] = json!(false);
        mark_held(&mut s);
        assert_eq!(s["applications"][0]["held"], false);
        assert!(alerts(&s).to_string().contains("app:jellyfin"));
    }
    /// UP-03: with the HDD missing its archive's old "matches" is not shown as current.
    #[test]
    fn the_hdd_archive_check_waits_for_the_hdd() {
        let data = std::env::temp_dir().join(format!("neko-collect-away-{}", std::process::id()));
        let _ = fs::remove_dir_all(&data);
        for section in ["archive", "usb_photos"] {
            crate::backup::save_integrity(
                &data,
                section,
                &Default::default(),
                crate::backup::Check::Full,
            )
            .unwrap();
        }
        let rows = integrity(&data, true, Some("Media HDD"), "");
        let blocked = |id: &str| {
            rows.as_array()
                .unwrap()
                .iter()
                .find(|r| r["id"] == id)
                .unwrap()["blocked"]
                .clone()
        };
        assert_eq!(
            blocked("archive"),
            "The Media HDD is missing, so this copy can't be checked or kept up to date until it is back."
        );
        assert!(
            blocked("usb_photos").is_null(),
            "the USB copy is on its own drive"
        );
        assert!(integrity(&data, true, None, "")[0]["blocked"].is_null());
        let _ = fs::remove_dir_all(data);
    }
    /// UP-05: a monthly update undone after the updated apps did not start is said in
    /// plain words (apps back on their previous version, working), not as a failure; one
    /// whose undoing failed too stays a failure.
    #[test]
    fn a_rolled_back_update_says_the_apps_are_back_and_working() {
        let saved = json!({"status": "fail", "stage": "starting the updated apps",
            "reason": "jellyfin was not ready after 5 minutes (Container not healthy). If it was still updating its database, raise \"health_wait_seconds\" under \"maintenance\" (up to 1800) and run  sudo bash install.sh  again; the next monthly update then waits longer"});
        assert!(rolled_back(&saved));
        // The monthly run's own flag decides at any stage (0.40.2 reports).
        assert!(rolled_back(
            &json!({"status": "fail", "stage": "restoring the previous version", "rolled_back": true})
        ));
        assert!(!rolled_back(
            &json!({"status": "pass", "rolled_back": true})
        ));
        let said = undone_reason(&saved);
        assert_eq!(said, "The update was undone: your apps are back on their previous version and work as before. It is tried again next month. Why: Jellyfin was not ready after 5 minutes (Container not healthy).");
        assert!(!said.contains("health_wait_seconds"));
        // A run set to try again the next night says so instead of "next month".
        let mut soon = saved.clone();
        soon["retry_at"] =
            json!("the monthly update tries again on 2026-11-03 02:30:00 Europe/Lisbon");
        assert!(undone_reason(&soon).contains(
            "work as before. The monthly update tries again on 2026-11-03 02:30:00 Europe/Lisbon. Why:"
        ));
        let mut worse = saved.clone();
        worse["reason"] = json!("jellyfin was not ready; then: Rollback state incomplete");
        assert!(!rolled_back(&worse));
        let mut earlier = saved.clone();
        earlier["stage"] = json!("writing the snapshot");
        assert!(
            !rolled_back(&earlier),
            "no update was installed: the backup failed"
        );
        assert!(!rolled_back(
            &json!({"status": "pass", "stage": UPDATED_APPS_STAGE})
        ));
        // Its alert is a warning that says so, not an urgent failure.
        let shown = alerts(
            &json!({"backups": [{"id": "maintenance", "name": "Monthly backup and updates",
            "status": "partial", "rolled_back": true, "reason": said}]}),
        );
        assert_eq!(shown[0]["level"], "warning");
        assert_eq!(
            shown[0]["title"],
            "Monthly backup and updates: the update was undone, your apps work"
        );
    }
    /// UP-06: a reason the report cut at 300 characters ends at a whole sentence or word,
    /// and the repair advice that was cut off is kept, in short.
    #[test]
    fn long_reasons_are_cut_at_a_sentence_and_keep_the_repair_advice() {
        let full = format!("Backup copies failed their checksum check: 1 Nextcloud file no longer matches its recorded checksum (kept as it is): rivka/files/Clients/2024/Contract-Levi.pdf. {}", crate::backup::SETTLE);
        let saved = json!({"stage": "checking every copy on the USB drive by checksum",
            "reason": common::sanitize(&full)});
        // The report keeps at most 300 characters, cut at a sentence or word since 0.40.2.
        assert!(text(&saved["reason"]).chars().count() <= 300);
        let shown = failure_reason(&saved);
        assert_eq!(shown, format!("Failed while checking every copy on the USB drive by checksum: Backup copies failed their checksum check: 1 Nextcloud file no longer matches its recorded checksum (kept as it is): rivka/files/Clients/2024/Contract-Levi.pdf. {SETTLE_SHORT}"));
        assert!(crate::backup::SETTLE.starts_with(SETTLE_START));
        // Without the advice: the last whole sentence, or the last whole word and "…".
        let words = "word ".repeat(70);
        assert_eq!(whole(&words, 300).chars().last(), Some('…'));
        assert!(whole(&words, 300).ends_with("word…"));
        let sentences = format!("{}. {}", "a".repeat(200), "b ".repeat(60));
        assert_eq!(whole(&sentences, 300), format!("{}.", "a".repeat(200)));
        assert_eq!(whole("short.", 300), "short.");
    }
    /// UP-11: the next run is also sent as a time the page shows in local time.
    #[test]
    fn next_runs_are_sent_as_seconds() {
        assert_eq!(
            utc_seconds("Sun 2026-11-01 03:00:00 UTC"),
            Some(1_793_502_000.0)
        );
        assert_eq!(utc_seconds("Sun 2026-11-01 05:00:00 IST"), None);
        assert_eq!(utc_seconds(""), None);
        assert_eq!(utc_seconds("n/a"), None);
    }
    /// D10: an app setup never got to (no Compose file on the app SSD) is "not set up".
    #[test]
    fn apps_setup_never_reached_are_not_set_up() {
        let data = std::env::temp_dir().join(format!("neko-collect-setup-{}", std::process::id()));
        let _ = fs::remove_dir_all(&data);
        fs::create_dir_all(data.join("compose/jellyfin")).unwrap();
        assert!(!set_up(&data, "jellyfin"));
        fs::write(data.join("compose/jellyfin/compose.yaml"), "services: {}\n").unwrap();
        assert!(set_up(&data, "jellyfin"));
        assert!(!set_up(&data, "immich"));
        let _ = fs::remove_dir_all(data);
    }
}

#[cfg(test)]
mod round_two_tests {
    use super::*;
    /// QA after 0.40.2: a whole sentence keeps its full stop (common::sanitize takes the
    /// last one off, and a cut after a sentence reads "sentence.…"); a reason cut mid-
    /// sentence keeps "…", and a command or file name at the end gets nothing added.
    #[test]
    fn whole_sentences_keep_their_full_stop() {
        let said = |reason: &str| failure_reason(&json!({ "reason": reason }));
        assert_eq!(said("Free up space there"), "Failed: Free up space there.");
        assert_eq!(said("Free up space there."), "Failed: Free up space there.");
        assert_eq!(
            said("One thing went wrong.…"),
            "Failed: One thing went wrong."
        );
        assert_eq!(
            said("It stopped while copying…"),
            "Failed: It stopped while copying…"
        );
        assert_eq!(
            said("check  systemctl status nekoserver-app-backup.service"),
            "Failed: check  systemctl status nekoserver-app-backup.service"
        );
        assert_eq!(
            said("missing /mnt/nekoserver-media/photos"),
            "Failed: missing /mnt/nekoserver-media/photos"
        );
        // What the runtime's own sanitize leaves of a long reason.
        let long = format!("{}. {}", "First part".repeat(20), "more words ".repeat(30));
        let shown = said(&common::sanitize(&long));
        assert!(!shown.contains(".…") && !shown.ends_with(".."), "{shown}");
        assert!(shown.ends_with('.') || shown.ends_with('…'), "{shown}");
    }
    /// Seen on the rig (0.47.5): its virtual disks have no temperature sensor and say 0,
    /// which the Server health page showed as "0 °C".
    #[test]
    fn a_drive_with_no_temperature_sensor_shows_no_temperature() {
        let read = |current: i64| {
            smart_summary(
                &json!({"smart_status": {"passed": true}, "temperature": {"current": current, "drive_trip": 0}}),
                0,
            )
        };
        assert!(read(0)["temperature"].is_null());
        assert_eq!(read(0)["status"], "pass");
        assert_eq!(read(34)["temperature"], 34);
    }
    /// 0.45.8: the upgrade runs as nekoserver-upgrade@<release>; the dashboard asked the
    /// unit "nekoserver-upgrade", which does not exist, so a run at work read as cut off.
    #[test]
    fn the_upgrade_is_asked_about_by_its_own_service() {
        let unit = |u: &str| upgrade_unit(&json!({"unit": u}));
        assert_eq!(
            unit("nekoserver-upgrade@v0.45.8Beta"),
            "nekoserver-upgrade@v0.45.8Beta"
        );
        assert_eq!(
            unit("nekoserver-upgrade@v1.0.0"),
            "nekoserver-upgrade@v1.0.0"
        );
        // The audit of 0.46.2: a beta tagged "-beta" was asked about as the plain unit.
        assert_eq!(
            unit("nekoserver-upgrade@v0.46.3-beta"),
            "nekoserver-upgrade@v0.46.3-beta"
        );
        assert_eq!(
            unit("nekoserver-upgrade@0.47.4-beta"),
            "nekoserver-upgrade@0.47.4-beta"
        );
        assert_eq!(
            unit("nekoserver-upgrade-resume"),
            "nekoserver-upgrade-resume"
        );
        for odd in [
            "nekoserver-upgrade@v1.0",
            "ssh",
            "nekoserver-upgrade@v1.0.0;x",
            "nekoserver-upgrade@v..1",
        ] {
            assert_eq!(unit(odd), "nekoserver-upgrade", "{odd}");
        }
        assert_eq!(upgrade_unit(&json!({})), "nekoserver-upgrade");
    }
    /// 0.45.8: a two-drive Btrfs pool whose counters could not be read was left out, and
    /// its open alert was then dropped as if the pool had been taken out on purpose.
    #[test]
    fn a_pool_whose_counters_cannot_be_read_stays_listed() {
        assert!(shown_as_pool(0, 2, "unknown"));
        assert!(shown_as_pool(2, 0, "healthy"));
        assert!(!shown_as_pool(0, 1, "unknown"));
        assert!(!shown_as_pool(1, 1, "healthy"));
        assert!(shown_as_pool(1, 1, "warning"));
    }
    /// Seen on the rig: the collector's own sandbox (ProtectSystem=strict) shows every
    /// drive as a read-only mount, and both drives got the "turned read-only" alert. Only
    /// the filesystem's own "ro" counts (what Linux sets after write errors).
    #[test]
    fn a_drive_is_read_only_by_its_filesystem_not_by_this_services_view_of_it() {
        // As /proc/self/mountinfo reads inside the collector's service (Arch, 0.46.2).
        let sandboxed = "\
803 771 8:64 / /srv/nekoserver ro,relatime shared:1 - ext4 /dev/sde rw\n\
804 771 8:80 / /mnt/nekoserver-media ro,relatime shared:2 - ext4 /dev/sdf rw,errors=remount-ro\n\
805 803 8:64 /dashboard-state /srv/nekoserver/dashboard-state rw,relatime shared:1 - ext4 /dev/sde rw\n";
        let ssd = Path::new("/srv/nekoserver");
        let hdd = Path::new("/mnt/nekoserver-media");
        assert!(!filesystem_read_only(sandboxed, ssd));
        assert!(!filesystem_read_only(sandboxed, hdd));
        // After write errors Linux turns the filesystem itself read-only.
        let failed = "36 25 8:80 / /mnt/nekoserver-media rw,relatime shared:2 - ext4 /dev/sdf ro,errors=remount-ro\n";
        assert!(filesystem_read_only(failed, hdd));
        // A later mount on the same folder covers the earlier one; a folder that is not
        // a mount point, or a list that could not be read, says nothing.
        let covered = format!("{failed}90 25 8:96 / /mnt/nekoserver-media rw - ext4 /dev/sdg rw\n");
        assert!(!filesystem_read_only(&covered, hdd));
        assert!(!filesystem_read_only(
            failed,
            Path::new("/mnt/nekoserver-media/library")
        ));
        assert!(!filesystem_read_only("", hdd));
        // "ro" only as a whole option.
        let named = "36 25 8:80 / /mnt/x rw - ext4 /dev/sdf rw,rootcontext=robot\n";
        assert!(!filesystem_read_only(named, Path::new("/mnt/x")));
    }
    /// 0.46.0: drives on a network storage box. The NAS connection is shown from Linux's
    /// iSCSI sessions; a drive that turned read-only or waits for the NAS gets an alert;
    /// the SMART health of a NAS drive is the NAS's to watch.
    #[test]
    fn the_network_storage_box_is_shown_and_its_troubles_alerted() {
        let root = std::env::temp_dir().join(format!("neko-nas-sessions-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        for (n, target, state) in [
            (1, "iqn.2026-11.lab.nas:media", "LOGGED_IN"),
            (2, "iqn.2026-11.lab.nas:backup", "FAILED"),
        ] {
            let session = root.join(format!("session{n}"));
            fs::create_dir_all(&session).unwrap();
            fs::write(session.join("targetname"), format!("{target}\n")).unwrap();
            fs::write(session.join("state"), format!("{state}\n")).unwrap();
        }
        let cfg = json!({"network_storage": {"wanted": true, "portal": "10.0.2.2:3260",
            "targets": ["iqn.2026-11.lab.nas:media", "iqn.2026-11.lab.nas:backup", "iqn.2026-11.lab.nas:gone"]}});
        let shown = network_storage(&cfg, &root);
        let states: Vec<&str> = shown["targets"]
            .as_array()
            .unwrap()
            .iter()
            .map(|t| text(&t["state"]))
            .collect();
        assert_eq!(states, ["connected", "lost", "not_connected"]);
        assert_eq!(network_storage(&json!({}), &root), Value::Null);
        let raised = alerts(&json!({"network_storage": shown,
        "storage": [
            {"id": "media_hdd", "name": "Media HDD", "status": "mounted", "read_only": true, "network": true},
            {"id": "data_ssd", "name": "Application SSD", "status": "mounted", "waiting": true},
        ]}));
        let ids: Vec<&str> = raised
            .as_array()
            .unwrap()
            .iter()
            .map(|a| text(&a["id"]))
            .collect();
        assert!(
            ids.contains(&"nas:iqn.2026-11.lab.nas:backup")
                && ids.contains(&"nas:iqn.2026-11.lab.nas:gone"),
            "{ids:?}"
        );
        assert!(!ids.contains(&"nas:iqn.2026-11.lab.nas:media"));
        let read_only = raised
            .as_array()
            .unwrap()
            .iter()
            .find(|a| a["title"] == "Media HDD turned read-only")
            .unwrap();
        assert!(text(&read_only["detail"]).contains("Check a drive that turned read-only"));
        assert!(raised
            .as_array()
            .unwrap()
            .iter()
            .any(|a| a["title"] == "Application SSD is waiting for the network storage box"));
        // A NAS drive's health is the NAS's: never a SMART alert from here.
        assert!(on_nas(
            &json!({"storage": {"media_hdd": {"network": {"transport": "iscsi"}}}}),
            "media_hdd"
        ));
        assert!(!on_nas(&json!({"storage": {"media_hdd": {}}}), "media_hdd"));
        let _ = fs::remove_dir_all(root);
    }
    /// 0.46.1: a drive whose filesystem had errors (counted by Linux in /sys, read without
    /// waking the drive) is said, and not "back" until the count is reset by a repair.
    #[test]
    fn a_drive_with_filesystem_errors_is_said() {
        let root = std::env::temp_dir().join(format!("neko-ext4-errors-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("sys/sdb1")).unwrap();
        fs::write(root.join("sys/sdb1/errors_count"), "3\n").unwrap();
        fs::write(root.join("sdb1"), "").unwrap();
        let source = root.join("sdb1");
        assert_eq!(
            common::ext4_errors(source.to_str().unwrap(), &root.join("sys")),
            Some(3)
        );
        assert_eq!(common::ext4_errors("/dev/none", &root.join("sys")), None);
        let shown = alerts(
            &json!({"storage": [{"id": "media_hdd", "name": "Media HDD", "status": "mounted", "fs_errors": 3}]}),
        );
        let alert = &shown.as_array().unwrap()[0];
        assert_eq!(alert["title"], "Media HDD has filesystem errors");
        assert!(text(&alert["detail"]).contains("Check a drive that turned read-only"));
        let _ = fs::remove_dir_all(root);
    }
    /// QA after 0.40.2: "1 file", never "1 files".
    #[test]
    fn counts_agree_with_their_noun() {
        let row = |bad: u64| json!({"integrity": [{"id": "usb_photos", "name": "USB photo copy", "damaged": bad, "files": ["a.jpg"]}]});
        assert_eq!(
            alerts(&row(1))[0]["title"],
            "USB photo copy: 1 file is damaged or missing"
        );
        assert_eq!(
            alerts(&row(2))[0]["title"],
            "USB photo copy: 2 files are damaged or missing"
        );
        assert!(!alerts(&row(2))[0].to_string().contains("a.jpg"));
        assert!(partial_reason(&json!({"in_use": 1, "files": ["a.docx"]}))
            .starts_with("1 file was not copied"));
    }
    /// D10: the Steam Big Picture card only where the TV step set it up.
    #[test]
    fn the_tv_is_set_up_only_with_its_report() {
        let data = std::env::temp_dir().join(format!("neko-collect-tv-{}", std::process::id()));
        let _ = fs::remove_dir_all(&data);
        fs::create_dir_all(data.join("reports")).unwrap();
        // As the collector's settings carry it (modules/dashboard.py collector_settings):
        // "tv": {"wanted": ...}; null when setup never asked (the report then decides).
        let on = json!({"tv": {"wanted": true}});
        let off = json!({"tv": {"wanted": false}});
        let never_asked = json!({"tv": {"wanted": null}});
        assert_eq!(tv(&never_asked, Some(&data)), json!({"set_up": false}));
        assert_eq!(
            tv(&on, Some(&data)),
            json!({"set_up": false}),
            "setup stopped before it"
        );
        fs::write(data.join("reports/tv-setup.json"), "{}").unwrap();
        assert_eq!(tv(&on, Some(&data)), json!({"set_up": true}));
        assert_eq!(tv(&never_asked, Some(&data)), json!({"set_up": true}));
        assert_eq!(
            tv(&off, Some(&data)),
            json!({"set_up": false}),
            "switched off later"
        );
        assert_eq!(
            tv(&on, None),
            json!({"set_up": null}),
            "SSD missing: not known"
        );
        assert_eq!(tv(&off, None), json!({"set_up": false}));
        let _ = fs::remove_dir_all(data);
    }
}
