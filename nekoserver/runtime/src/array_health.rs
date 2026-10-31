//! Health of the software RAID arrays (mdadm) and Btrfs pools on this PC: whether every
//! drive of an array is in place, a rebuild and its progress, and the error counters
//! that show a drive going bad before it fails (corrected read errors on a RAID member,
//! the read/write/corruption counters of a Btrfs device). Read from `/sys/block/<md>/md`
//! and `btrfs device stats`; shown on the dashboard's Drives page and raised as alerts
//! (collect.rs), so a dying mirror is seen before the second drive goes too.
use serde_json::{json, Value};
use std::fs;
use std::path::Path;

/// A member's corrected read errors below this stay on the dashboard only; from here on
/// they are alerted (md itself fails the member around this count).
pub(crate) const ERRORS_ALERTED: u64 = 20;

fn line(path: &Path) -> Option<String> {
    fs::read_to_string(path)
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}
fn count(path: &Path) -> u64 {
    line(path).and_then(|s| s.parse().ok()).unwrap_or(0)
}

/// "12345 / 100000" (md's sync_completed) as a percentage, one decimal.
fn progress(text: Option<String>) -> Value {
    let Some(text) = text else { return Value::Null };
    let mut parts = text.split('/').map(str::trim);
    let done = parts.next().and_then(|v| v.parse::<f64>().ok());
    let whole = parts.next().and_then(|v| v.parse::<f64>().ok());
    match (done, whole) {
        (Some(done), Some(whole)) if whole > 0.0 => {
            json!(((1000.0 * done / whole).round() / 10.0).clamp(0.0, 100.0))
        }
        _ => Value::Null,
    }
}

/// The drives of one array: each one's own state and corrected-read-error count.
fn members(md: &Path) -> Vec<Value> {
    let mut rows = vec![];
    for entry in fs::read_dir(md).into_iter().flatten().flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        let Some(device) = name.strip_prefix("dev-") else {
            continue;
        };
        rows.push(json!({
            "device": device,
            "state": line(&entry.path().join("state")).unwrap_or_else(|| "unknown".into()),
            "errors": count(&entry.path().join("errors")),
        }));
    }
    rows.sort_by_key(|r| r["device"].as_str().unwrap_or("").to_string());
    rows
}

fn faulty(member: &Value) -> bool {
    member["state"]
        .as_str()
        .unwrap_or("")
        .split(',')
        .any(|s| s.trim() == "faulty")
}

/// What an array reports while everything is fine (md's array_state).
const FINE: [&str; 5] = [
    "clean",
    "active",
    "active-idle",
    "write-pending",
    "read-auto",
];

/// One mdadm array under /sys/block, as the dashboard shows it.
fn md_array(block: &Path, name: &str) -> Value {
    let md = block.join(name).join("md");
    let state = line(&md.join("array_state")).unwrap_or_else(|| "unknown".into());
    let level = line(&md.join("level")).unwrap_or_else(|| "raid".into());
    let missing = count(&md.join("degraded"));
    let action = line(&md.join("sync_action")).unwrap_or_else(|| "idle".into());
    let mismatches = count(&md.join("mismatch_cnt"));
    let members = members(&md);
    let failed: Vec<&str> = members
        .iter()
        .filter(|m| faulty(m))
        .filter_map(|m| m["device"].as_str())
        .collect();
    let worn: Vec<String> = members
        .iter()
        .filter(|m| m["errors"].as_u64().unwrap_or(0) > 0)
        .map(|m| {
            format!(
                "{} ({} corrected read errors)",
                m["device"].as_str().unwrap_or(""),
                m["errors"]
            )
        })
        .collect();
    let rebuilding = ["recover", "resync", "repair"].contains(&action.as_str());
    let named = if failed.is_empty() {
        "a drive is missing".to_string()
    } else {
        format!("the array marked {} as failed", failed.join(", "))
    };
    let (status, reason) = if missing > 0 || !failed.is_empty() {
        if rebuilding {
            ("rebuilding", format!("A drive is being brought back in ({named}); until it finishes, your data there has one copy fewer"))
        } else {
            ("degraded", format!("Your data there has no second copy: {named}. Replace or reconnect the drive and add it back; the array then rebuilds itself"))
        }
    } else if rebuilding {
        (
            "rebuilding",
            "The copies are being brought back in line".to_string(),
        )
    } else if !FINE.contains(&state.as_str()) {
        ("warning", format!("The array reports the state {state}"))
    } else if mismatches > 0 {
        ("warning", format!("{mismatches} blocks differ between the drives' copies (found by a consistency check): have md repair them, and check the drives' health"))
    } else if !worn.is_empty() {
        (
            "warning",
            format!(
                "A drive needed retries to be read, an early sign it may be failing: {}",
                worn.join(", ")
            ),
        )
    } else {
        ("healthy", "Every drive is in place".to_string())
    };
    let busy = rebuilding || action == "check";
    json!({
        "id": format!("md-{name}"),
        "kind": level,
        "name": format!("{} array {name}", level.to_uppercase()),
        "status": status,
        "reason": reason,
        "progress": if busy { progress(line(&md.join("sync_completed"))) } else { Value::Null },
        "mismatches": mismatches,
        "members": members,
        "holds": [],
    })
}

/// Every mdadm array on this PC, from /sys/block (nothing to run, works on every kernel).
pub(crate) fn md_arrays(block: &Path) -> Vec<Value> {
    let mut names: Vec<String> = fs::read_dir(block)
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.file_name().to_string_lossy().to_string())
        .filter(|n| block.join(n).join("md").is_dir())
        .collect();
    names.sort();
    names.iter().map(|n| md_array(block, n)).collect()
}

/// The per-device counter rows of `btrfs device stats` output, such as
/// `[/dev/sdb1].corruption_errs  3`.
fn btrfs_members(stats: &str) -> Vec<Value> {
    let mut by_device: Vec<(String, Value)> = vec![];
    for row in stats.lines() {
        let Some((device, rest)) = row
            .trim()
            .strip_prefix('[')
            .and_then(|r| r.split_once("]."))
        else {
            continue;
        };
        let mut parts = rest.split_whitespace();
        let (Some(counter), Some(value)) = (parts.next(), parts.next()) else {
            continue;
        };
        let Ok(value) = value.parse::<u64>() else {
            continue;
        };
        let device = device.strip_prefix("/dev/").unwrap_or(device).to_string();
        if !by_device.iter().any(|(d, _)| *d == device) {
            by_device.push((
                device.clone(),
                json!({"device": device, "state": "in_sync"}),
            ));
        }
        let row = &mut by_device.iter_mut().find(|(d, _)| *d == device).unwrap().1;
        row[counter] = json!(value);
    }
    by_device.into_iter().map(|(_, v)| v).collect()
}

fn counter(member: &Value, keys: &[&str]) -> u64 {
    keys.iter().map(|k| member[*k].as_u64().unwrap_or(0)).sum()
}

/// One Btrfs filesystem (a pool when it spans several drives) as the dashboard shows it:
/// its devices' error counters (from `btrfs device stats` output), and the devices its
/// kernel view lists as missing. Without the btrfs tool the counters are unknown.
pub(crate) fn btrfs_pool(uuid: &str, stats: Option<&str>, missing: &[String]) -> Value {
    let members = stats.map(btrfs_members).unwrap_or_default();
    let corrupted: Vec<&str> = members
        .iter()
        .filter(|m| counter(m, &["corruption_errs", "generation_errs"]) > 0)
        .filter_map(|m| m["device"].as_str())
        .collect();
    let erring: Vec<String> = members
        .iter()
        .filter(|m| counter(m, &["read_io_errs", "write_io_errs", "flush_io_errs"]) > 0)
        .map(|m| {
            format!(
                "{} ({} read, {} write)",
                m["device"].as_str().unwrap_or(""),
                m["read_io_errs"].as_u64().unwrap_or(0),
                m["write_io_errs"].as_u64().unwrap_or(0)
            )
        })
        .collect();
    let (status, reason) = if !missing.is_empty() {
        ("degraded", format!("Your data there has one copy fewer: {} missing. Reconnect or replace the drive, then have btrfs bring the copies back in line", missing.join(", ")))
    } else if !corrupted.is_empty() {
        ("failing", format!("Btrfs found data that reads back wrong on {}: check the drive's health and its cable. Damaged backup copies can be repaired from the recovery menu", corrupted.join(", ")))
    } else if !erring.is_empty() {
        (
            "warning",
            format!(
                "Btrfs counts failed reads or writes, an early sign a drive may be failing: {}",
                erring.join(", ")
            ),
        )
    } else if stats.is_none() {
        (
            "unknown",
            "The btrfs tool is not installed, so the drives' error counters can't be read"
                .to_string(),
        )
    } else {
        ("healthy", "No errors counted".to_string())
    };
    let short = uuid.get(..8).unwrap_or(uuid);
    json!({
        "id": format!("btrfs-{short}"),
        "kind": "btrfs",
        "name": "Btrfs drives",
        "status": status,
        "reason": reason,
        "progress": Value::Null,
        "mismatches": 0,
        "uuid": uuid,
        "members": members,
        "holds": [],
    })
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use std::path::PathBuf;

    fn scratch(name: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!("neko-arrays-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        root
    }
    fn write(path: &Path, text: &str) {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, text).unwrap();
    }
    /// A fake /sys/block with one RAID1 of sda1+sdb1, as the tests shape it.
    pub(crate) fn fake_md(root: &Path, entries: &[(&str, &str)]) {
        let md = root.join("md0/md");
        for (file, value) in [
            ("array_state", "clean"),
            ("level", "raid1"),
            ("degraded", "0"),
            ("sync_action", "idle"),
            ("mismatch_cnt", "0"),
            ("dev-sda1/state", "in_sync"),
            ("dev-sda1/errors", "0"),
            ("dev-sdb1/state", "in_sync"),
            ("dev-sdb1/errors", "0"),
        ] {
            write(&md.join(file), value);
        }
        for (file, value) in entries {
            write(&md.join(file), value);
        }
        // Drives that are not arrays are passed over without a sound.
        fs::create_dir_all(root.join("sda")).unwrap();
    }

    #[test]
    fn a_whole_array_is_healthy_and_names_its_drives() {
        let root = scratch("healthy");
        fake_md(&root, &[]);
        let rows = md_arrays(&root);
        assert_eq!(rows.len(), 1);
        let a = &rows[0];
        assert_eq!(
            (a["id"].as_str(), a["status"].as_str(), a["kind"].as_str()),
            (Some("md-md0"), Some("healthy"), Some("raid1"))
        );
        assert_eq!(a["name"], "RAID1 array md0");
        assert_eq!(a["members"][0]["device"], "sda1");
        assert_eq!(a["members"][1]["device"], "sdb1");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn a_lost_drive_is_degraded_and_rebuilding_once_it_is_replaced() {
        let root = scratch("degraded");
        fake_md(
            &root,
            &[
                ("array_state", "clean"),
                ("degraded", "1"),
                ("dev-sdb1/state", "faulty"),
            ],
        );
        let a = &md_arrays(&root)[0];
        assert_eq!(a["status"], "degraded");
        let reason = a["reason"].as_str().unwrap();
        assert!(
            reason.contains("sdb1") && reason.contains("no second copy"),
            "{reason}"
        );
        // The replacement is being filled: rebuilding, with its progress.
        fake_md(
            &root,
            &[
                ("degraded", "1"),
                ("sync_action", "recover"),
                ("sync_completed", "250 / 1000"),
            ],
        );
        let a = &md_arrays(&root)[0];
        assert_eq!(
            (a["status"].as_str(), a["progress"].as_f64()),
            (Some("rebuilding"), Some(25.0))
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn mismatches_and_read_errors_are_a_warning_with_the_drive_named() {
        let root = scratch("worn");
        fake_md(&root, &[("mismatch_cnt", "128")]);
        let a = &md_arrays(&root)[0];
        assert_eq!(a["status"], "warning");
        assert!(a["reason"].as_str().unwrap().contains("128 blocks differ"));
        fake_md(&root, &[("dev-sda1/errors", "7")]);
        let a = &md_arrays(&root)[0];
        assert_eq!(a["status"], "warning");
        assert!(a["reason"]
            .as_str()
            .unwrap()
            .contains("sda1 (7 corrected read errors)"));
        assert_eq!(a["members"][0]["errors"], 7);
        // A monthly consistency check on a whole array is not an alarm.
        fake_md(
            &root,
            &[("sync_action", "check"), ("sync_completed", "1 / 2")],
        );
        let a = &md_arrays(&root)[0];
        assert_eq!(
            (a["status"].as_str(), a["progress"].as_f64()),
            (Some("healthy"), Some(50.0))
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn btrfs_counters_name_the_failing_drive() {
        let stats = "[/dev/sdb1].write_io_errs   0\n[/dev/sdb1].read_io_errs    0\n\
                     [/dev/sdb1].flush_io_errs   0\n[/dev/sdb1].corruption_errs 0\n\
                     [/dev/sdb1].generation_errs 0\n[/dev/sdc1].write_io_errs   0\n\
                     [/dev/sdc1].read_io_errs    4\n[/dev/sdc1].flush_io_errs   0\n\
                     [/dev/sdc1].corruption_errs 0\n[/dev/sdc1].generation_errs 0\n";
        let pool = btrfs_pool("aabbccdd-1111", Some(stats), &[]);
        assert_eq!(
            (pool["id"].as_str(), pool["status"].as_str()),
            (Some("btrfs-aabbccdd"), Some("warning"))
        );
        assert!(pool["reason"]
            .as_str()
            .unwrap()
            .contains("sdc1 (4 read, 0 write)"));
        assert_eq!(pool["members"].as_array().unwrap().len(), 2);
        let bad = stats.replace(
            "[/dev/sdb1].corruption_errs 0",
            "[/dev/sdb1].corruption_errs 2",
        );
        let pool = btrfs_pool("aabbccdd-1111", Some(&bad), &[]);
        assert_eq!(pool["status"], "failing");
        assert!(pool["reason"].as_str().unwrap().contains("sdb1"));
        let gone = btrfs_pool("aabbccdd-1111", Some(stats), &["device 2".into()]);
        assert_eq!(gone["status"], "degraded");
        let blind = btrfs_pool("aabbccdd-1111", None, &[]);
        assert_eq!(blind["status"], "unknown");
        assert!(blind["reason"].as_str().unwrap().contains("btrfs tool"));
    }

    #[test]
    fn rebuild_progress_is_read_defensively() {
        assert_eq!(progress(Some("250 / 1000".into())).as_f64(), Some(25.0));
        assert_eq!(progress(Some("none".into())), Value::Null);
        assert_eq!(progress(Some("3 / 0".into())), Value::Null);
        assert_eq!(progress(None), Value::Null);
        assert_eq!(progress(Some("7777 / 7".into())).as_f64(), Some(100.0));
    }
}
