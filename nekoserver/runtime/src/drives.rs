//! The Drives page's list of disks (/api/status "disks", 1.0.1 contract §6): every disk
//! this PC has, and what it is to NekoServer, from lsblk and /dev/disk/by-id. It says what
//! a disk is; whether one may be used or erased is the root tool's one refusal list
//! (storage_jobs.py, look.json), never decided here.
use crate::common::{self, SHARED};
use serde_json::{json, Value};
use std::{collections::BTreeMap, fs, path::Path};

/// What lsblk is asked (bytes, as JSON).
const COLUMNS: &str =
    "NAME,KNAME,TYPE,SIZE,MODEL,SERIAL,TRAN,ROTA,FSTYPE,LABEL,UUID,MOUNTPOINT,FSUSED,FSAVAIL";
/// Mount points that make a disk the system's.
const SYSTEM: [&str; 5] = ["/", "/boot", "/boot/efi", "/usr", "[SWAP]"];

fn words(role: &str) -> &'static str {
    SHARED["storage_jobs"]["role_words"][role]
        .as_str()
        .unwrap_or("")
}

/// A drive's name on the page: a role's words, or "Extra drive d3".
pub fn drive_words(key: &str) -> Option<String> {
    match key {
        "data_ssd" => Some(words("apps").to_string()),
        "media_hdd" => Some(words("media").to_string()),
        "ssd" => Some(words("apps").to_string()),
        "media" => Some(words("media").to_string()),
        k if extra(k) => Some(format!("{} {k}", words("extra"))),
        _ => None,
    }
}
/// An extra drive's key: "d" and a number from 3 to 99.
pub fn extra(key: &str) -> bool {
    key.strip_prefix('d')
        .and_then(|n| n.parse::<u8>().ok())
        .is_some_and(|n| (3..=99).contains(&n))
        && !key[1..].starts_with('0')
}

/// The disks as /api/status shows them.
pub fn disks(cfg: &Value) -> Vec<Value> {
    let (code, said) = common::probe(&["lsblk", "-J", "-b", "-o", COLUMNS], 8);
    if code != 0 {
        return Vec::new();
    }
    let tree: Value = serde_json::from_str(&said).unwrap_or(Value::Null);
    from_lsblk(&tree, &by_id(Path::new("/dev/disk/by-id")), cfg)
}

/// The stable name of each disk (kernel name -> by-id name), the most telling one when it
/// has several: the bus and serial ("ata-...", "nvme-...") before a world-wide number.
pub fn by_id(folder: &Path) -> BTreeMap<String, String> {
    let mut found: BTreeMap<String, String> = BTreeMap::new();
    let Ok(entries) = fs::read_dir(folder) else {
        return found;
    };
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        let Ok(target) = fs::read_link(entry.path()) else {
            continue;
        };
        let Some(kname) = target.file_name().map(|t| t.to_string_lossy().to_string()) else {
            continue;
        };
        if name.contains("-part") || !BY_ID_KINDS.iter().any(|kind| name.starts_with(kind)) {
            continue;
        }
        let better = found
            .get(&kname)
            .is_none_or(|had| (rank(&name), name.as_str()) < (rank(had), had.as_str()));
        if better {
            found.insert(kname, name);
        }
    }
    found
}
/// The kinds of stable name a disk is offered by (/dev/disk/by-id): the bus and its
/// serial, or a world-wide number. A kernel name (sdc) is none of these.
pub const BY_ID_KINDS: [&str; 8] = [
    "ata-",
    "nvme-",
    "scsi-",
    "usb-",
    "wwn-",
    "virtio-",
    "mmc-",
    "ieee1394-",
];
fn rank(name: &str) -> u8 {
    match name.split('-').next().unwrap_or("") {
        "ata" | "nvme" | "scsi" | "usb" if !name.starts_with("nvme-eui") => 0,
        "wwn" => 1,
        _ => 2,
    }
}

fn text(v: &Value) -> Option<String> {
    v.as_str()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
}
fn number(v: &Value) -> Option<u64> {
    v.as_u64()
        .or_else(|| v.as_str().and_then(|s| s.trim().parse().ok()))
}
fn flag(v: &Value) -> bool {
    v.as_bool()
        .unwrap_or_else(|| v.as_str().is_some_and(|s| s.trim() == "1"))
}
/// A node and everything under it (partitions, an array, an encrypted volume ...).
fn all(node: &Value) -> Vec<&Value> {
    let mut out = vec![node];
    for child in node["children"].as_array().into_iter().flatten() {
        out.extend(all(child));
    }
    out
}

/// The disks of lsblk's tree, each with its role.
pub fn from_lsblk(tree: &Value, ids: &BTreeMap<String, String>, cfg: &Value) -> Vec<Value> {
    let drives: Vec<(String, String)> = cfg["storage"]
        .as_object()
        .into_iter()
        .flatten()
        .filter_map(|(key, d)| Some((key.clone(), text(&d["uuid"])?)))
        .collect();
    let backup = text(&cfg["photos"]["external_backup_uuid"]);
    // A Btrfs filesystem on more than one disk is a mirror of its own.
    let mut btrfs: BTreeMap<String, usize> = BTreeMap::new();
    for disk in tree["blockdevices"].as_array().into_iter().flatten() {
        let mut seen = std::collections::BTreeSet::new();
        for node in all(disk) {
            if node["fstype"] == "btrfs" {
                if let Some(uuid) = text(&node["uuid"]) {
                    seen.insert(uuid);
                }
            }
        }
        for uuid in seen {
            *btrfs.entry(uuid).or_default() += 1;
        }
    }
    let mut out = Vec::new();
    for disk in tree["blockdevices"].as_array().into_iter().flatten() {
        if disk["type"] != "disk" {
            continue;
        }
        let nodes = all(disk);
        let kname = text(&disk["kname"])
            .or_else(|| text(&disk["name"]))
            .unwrap_or_default();
        let uuids: Vec<String> = nodes.iter().filter_map(|n| text(&n["uuid"])).collect();
        let mounts: Vec<String> = nodes
            .iter()
            .filter_map(|n| text(&n["mountpoint"]))
            .collect();
        let drive = drives
            .iter()
            .find(|(_, uuid)| uuids.contains(uuid))
            .map(|(key, _)| key.clone());
        let md = nodes
            .iter()
            .find(|n| text(&n["type"]).is_some_and(|t| t.starts_with("raid")))
            .and_then(|n| text(&n["kname"]).or_else(|| text(&n["name"])));
        let pool = nodes
            .iter()
            .filter(|n| n["fstype"] == "btrfs")
            .filter_map(|n| text(&n["uuid"]))
            .find(|u| btrfs.get(u).copied().unwrap_or(0) > 1);
        let array = md
            .map(|m| format!("md-{m}"))
            .or(pool.map(|u| format!("btrfs-{u}")));
        let role = if mounts.iter().any(|m| SYSTEM.contains(&m.as_str())) {
            "system"
        } else if array.is_some() {
            "mirror_member"
        } else if let Some(key) = &drive {
            match key.as_str() {
                "data_ssd" => "apps",
                "media_hdd" => "media",
                _ => "extra",
            }
        } else if backup.as_ref().is_some_and(|b| uuids.contains(b)) {
            "offline_backup"
        } else {
            "none"
        };
        // The filesystem shown: the one that is mounted, else the first one found.
        let shown = nodes
            .iter()
            .find(|n| text(&n["mountpoint"]).is_some() && text(&n["fstype"]).is_some())
            .or_else(|| nodes.iter().find(|n| text(&n["fstype"]).is_some()));
        let sum = |key: &str| -> Option<u64> {
            let found: Vec<u64> = nodes
                .iter()
                .filter(|n| text(&n["mountpoint"]).is_some())
                .filter_map(|n| number(&n[key]))
                .collect();
            (!found.is_empty()).then(|| found.iter().sum())
        };
        out.push(json!({
            "by_id": ids.get(&kname),
            "dev": kname,
            "model": text(&disk["model"]),
            "serial": text(&disk["serial"]),
            "size_bytes": number(&disk["size"]),
            "transport": text(&disk["tran"]),
            "rotational": flag(&disk["rota"]),
            "role": role,
            "role_words": words(role),
            "drive_words": drive.as_deref().and_then(drive_words),
            "drive": drive,
            "array": array,
            "mounted_at": mounts.iter().find(|m| m.starts_with('/')),
            "fstype": shown.and_then(|n| text(&n["fstype"])),
            "label": shown.and_then(|n| text(&n["label"])),
            "used_bytes": sum("fsused"),
            "free_bytes": sum("fsavail"),
        }));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg() -> Value {
        json!({"storage": {"data_ssd": {"uuid": "u-ssd"}, "media_hdd": {"uuid": "u-hdd"},
            "d3": {"uuid": "u-d3"}}, "photos": {"external_backup_uuid": "u-usb"}})
    }
    fn part(name: &str, fstype: &str, uuid: &str, mount: Option<&str>) -> Value {
        json!({"name": name, "kname": name, "type": "part", "fstype": fstype, "uuid": uuid,
            "mountpoint": mount, "fsused": mount.map(|_| "100"), "fsavail": mount.map(|_| 900)})
    }
    fn disk(name: &str, children: Vec<Value>) -> Value {
        json!({"name": name, "kname": name, "type": "disk", "size": 4_000_000_000_000u64,
            "model": "WDC WD40EFRX ", "serial": format!("S-{name}"), "tran": "sata",
            "rota": "1", "children": children})
    }
    fn roles(tree: &Value) -> Vec<(String, String, Value)> {
        from_lsblk(tree, &BTreeMap::new(), &cfg())
            .into_iter()
            .map(|d| {
                (
                    d["dev"].as_str().unwrap().to_string(),
                    d["role"].as_str().unwrap().to_string(),
                    d["drive"].clone(),
                )
            })
            .collect()
    }

    /// Each disk says what it is to NekoServer: the system's, a role's, the offline
    /// backup, a mirror's member, or not in use.
    #[test]
    fn every_disk_says_what_it_is() {
        let tree = json!({"blockdevices": [
            disk("nvme0n1", vec![part("nvme0n1p1", "vfat", "e", Some("/boot/efi")), part("nvme0n1p2", "ext4", "r", Some("/"))]),
            disk("sda", vec![part("sda1", "ext4", "u-ssd", Some("/srv/nekoserver"))]),
            disk("sdb", vec![part("sdb1", "xfs", "u-hdd", Some("/mnt/nekoserver-media"))]),
            disk("sdc", vec![part("sdc1", "ext4", "u-d3", Some("/mnt/nekoserver-drives/d3"))]),
            disk("sdd", vec![part("sdd1", "exfat", "u-usb", None)]),
            disk("sde", vec![part("sde1", "ntfs", "x", None)]),
            disk("sdf", vec![]),
            {"name": "loop0", "kname": "loop0", "type": "loop"},
        ]});
        assert_eq!(
            roles(&tree),
            vec![
                ("nvme0n1".into(), "system".into(), Value::Null),
                ("sda".into(), "apps".into(), json!("data_ssd")),
                ("sdb".into(), "media".into(), json!("media_hdd")),
                ("sdc".into(), "extra".into(), json!("d3")),
                ("sdd".into(), "offline_backup".into(), Value::Null),
                ("sde".into(), "none".into(), Value::Null),
                ("sdf".into(), "none".into(), Value::Null),
            ]
        );
        let all = from_lsblk(&tree, &BTreeMap::new(), &cfg());
        assert_eq!(all[1]["role_words"], "Apps drive");
        assert_eq!(all[1]["drive_words"], "Apps drive");
        assert_eq!(all[3]["drive_words"], "Extra drive d3");
        assert_eq!(all[5]["drive_words"], Value::Null);
        assert_eq!(all[5]["role_words"], "Not in use");
        assert_eq!(all[1]["model"], "WDC WD40EFRX");
        assert_eq!(all[1]["rotational"], true);
        assert_eq!(all[1]["used_bytes"], 100);
        assert_eq!(all[1]["free_bytes"], 900);
        assert_eq!(all[5]["fstype"], "ntfs");
        assert_eq!(all[5]["used_bytes"], Value::Null);
    }

    /// An mdadm or multi-disk Btrfs mirror's disks are its members, with the array's id
    /// as /api/status "arrays" names it; the drive it holds is said too.
    #[test]
    fn a_mirrors_disks_are_its_members() {
        let md = json!({"name": "md0", "kname": "md0", "type": "raid1", "fstype": "ext4",
            "uuid": "u-hdd", "mountpoint": "/mnt/nekoserver-media"});
        let a = disk(
            "sdb",
            vec![json!({"name": "sdb1", "kname": "sdb1", "type": "part",
            "fstype": "linux_raid_member", "uuid": "m", "children": [md.clone()]})],
        );
        let b = disk(
            "sdc",
            vec![json!({"name": "sdc1", "kname": "sdc1", "type": "part",
            "fstype": "linux_raid_member", "uuid": "m", "children": [md]})],
        );
        let pool = |n: &str| disk(n, vec![part(&format!("{n}1"), "btrfs", "u-pool", None)]);
        let tree = json!({"blockdevices": [a, b, pool("sdd"), pool("sde"),
            disk("sdf", vec![part("sdf1", "btrfs", "u-alone", None)])]});
        let all = from_lsblk(&tree, &BTreeMap::new(), &cfg());
        assert_eq!(all[0]["role"], "mirror_member");
        assert_eq!(all[0]["array"], "md-md0");
        assert_eq!(all[0]["drive"], "media_hdd");
        assert_eq!(all[1]["array"], "md-md0");
        assert_eq!(all[2]["array"], "btrfs-u-pool");
        assert_eq!(all[3]["role"], "mirror_member");
        assert_eq!(all[4]["role"], "none", "a Btrfs disk alone is no mirror");
    }

    /// Disks are named by their stable name, never by sdX alone: bus and serial first.
    #[test]
    fn the_stable_name_prefers_bus_and_serial() {
        let dir = std::env::temp_dir().join(format!("neko-by-id-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        for (name, to) in [
            ("wwn-0x50014ee2b5", "../../sda"),
            ("ata-WDC_WD40EFRX_S1", "../../sda"),
            ("ata-WDC_WD40EFRX_S1-part1", "../../sda1"),
            ("nvme-eui.0025", "../../nvme0n1"),
            ("nvme-Samsung_980_S2", "../../nvme0n1"),
            ("lvm-pv-uuid-abc", "../../sdb"),
        ] {
            std::os::unix::fs::symlink(to, dir.join(name)).unwrap();
        }
        let ids = by_id(&dir);
        assert_eq!(ids["sda"], "ata-WDC_WD40EFRX_S1");
        assert_eq!(ids["nvme0n1"], "nvme-Samsung_980_S2");
        assert!(!ids.contains_key("sda1"));
        assert!(!ids.contains_key("sdb"), "an LVM name is not the disk's");
        let _ = fs::remove_dir_all(&dir);
    }

    /// The rank decides, not the alphabet: a world-wide number before an other bus's
    /// name, and an NVMe's model and serial before its EUI, whichever is read first.
    #[test]
    fn the_rank_decides_before_the_alphabet() {
        let dir = std::env::temp_dir().join(format!("neko-by-id-rank-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        for (name, to) in [
            ("ieee1394-0x0001", "../../sdc"),
            ("wwn-0x5000c500a1", "../../sdc"),
            ("mmc-SD32G_0x1", "../../sdd"),
            ("wwn-0x5000c500a2", "../../sdd"),
            ("nvme-eui.0025385", "../../nvme1n1"),
            ("nvme-foo_980_S3", "../../nvme1n1"),
            ("usb-Kingston_S4", "../../sde"),
            ("ata-Kingston_S4", "../../sde"),
            ("wwn-0x5000c500a3", "../../sde"),
            ("virtio-vd1", "../../vda"),
        ] {
            std::os::unix::fs::symlink(to, dir.join(name)).unwrap();
        }
        let ids = by_id(&dir);
        assert_eq!(ids["sdc"], "wwn-0x5000c500a1");
        assert_eq!(ids["sdd"], "wwn-0x5000c500a2");
        assert_eq!(ids["nvme1n1"], "nvme-foo_980_S3");
        assert_eq!(
            ids["sde"], "ata-Kingston_S4",
            "two of one rank: the first by name"
        );
        assert_eq!(
            ids["vda"], "virtio-vd1",
            "a name of the last rank is still a name"
        );
        let _ = fs::remove_dir_all(&dir);
    }

    /// The names of the drives, the role drives by either key, and none for a key that
    /// is no drive's.
    #[test]
    fn each_drive_has_its_words() {
        assert_eq!(drive_words("ssd").as_deref(), Some("Apps drive"));
        assert_eq!(drive_words("data_ssd").as_deref(), Some("Apps drive"));
        assert_eq!(
            drive_words("media").as_deref(),
            Some("Media and backups drive")
        );
        assert_eq!(
            drive_words("media_hdd").as_deref(),
            Some("Media and backups drive")
        );
        assert_eq!(drive_words("d42").as_deref(), Some("Extra drive d42"));
        for key in ["d2", "d03", "d100", "photos", ""] {
            assert_eq!(drive_words(key), None, "{key}");
        }
    }

    /// A spinning disk or not, however lsblk says it; the filesystem shown is the
    /// mounted one, not the first one found.
    #[test]
    fn rotational_and_the_filesystem_shown() {
        let mut ssd = disk(
            "sdg",
            vec![
                part("sdg1", "ext4", "old", None),
                part("sdg2", "xfs", "now", Some("/mnt/x")),
            ],
        );
        let shown = |d: &Value| from_lsblk(&json!({"blockdevices": [d]}), &BTreeMap::new(), &cfg());
        assert_eq!(shown(&ssd)[0]["fstype"], "xfs");
        assert_eq!(shown(&ssd)[0]["rotational"], true);
        for (rota, spins) in [
            (json!("0"), false),
            (json!(false), false),
            (json!(true), true),
            (json!(null), false),
        ] {
            ssd["rota"] = rota.clone();
            assert_eq!(shown(&ssd)[0]["rotational"], spins, "{rota}");
        }
        // Nothing mounted: the first filesystem found.
        let unmounted = disk(
            "sdh",
            vec![part("sdh1", "", "", None), part("sdh2", "ntfs", "n", None)],
        );
        assert_eq!(shown(&unmounted)[0]["fstype"], "ntfs");
    }
}
