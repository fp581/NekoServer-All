//! The drives plugged into the server, as lsblk lists them, and which of them are cards
//! of the set. A card is written only when all of this holds: it is removable, mounted,
//! not one of NekoServer's own drives nor the disk Linux runs from, the filesystem
//! mounted on its folder is that drive's (device numbers), its NekoBackup/identity.json
//! names this set, and the catalogue enrolled it on that very filesystem (UUID).
use crate::cardset::format::{self, Identity};
use crate::cardset::fsat::Dir;
use crate::cardset::store::{Store, VolumeRow};
use crate::cardset::volume::{Expect, MountRule};
use crate::common::{self, Error, Result};
use serde_json::Value;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};

/// One filesystem (a partition, or a whole card without partitions).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Drive {
    pub path: String,
    pub uuid: String,
    pub fstype: String,
    pub label: String,
    pub size: u64,
    /// Where it is mounted, if it is.
    pub mount: Option<PathBuf>,
    /// The drive says it can be taken out (an SD card, a USB stick or drive).
    pub removable: bool,
    /// Its disk holds a filesystem Linux runs from (/, /boot, /usr, /var or swap).
    pub system: bool,
    /// "major:minor" of the block device, to match the mounted filesystem to it.
    pub device: String,
}

/// Folders a disk Linux runs from has one of its filesystems on.
const SYSTEM_MOUNTS: [&str; 7] = ["/", "/boot", "/boot/efi", "/efi", "/usr", "/var", "[SWAP]"];

/// What lsblk said, read the same from old and new versions (some write true/false, some
/// "1"/"0"; some sizes as text).
fn flag(v: &Value) -> bool {
    v == true || v == "1" || v.as_u64() == Some(1)
}
fn size(v: &Value) -> u64 {
    v.as_u64()
        .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
        .unwrap_or(0)
}
fn text(v: &Value) -> String {
    v.as_str().unwrap_or("").to_string()
}

/// Every filesystem of `lsblk -J -b -o` (LSBLK's columns), with what its disk says: a
/// partition of a removable disk is removable, and every filesystem of a disk Linux runs
/// from belongs to the system disk.
pub fn parse(lsblk: &Value) -> Vec<Drive> {
    let mut out = vec![];
    for disk in lsblk["blockdevices"].as_array().into_iter().flatten() {
        let mut rows = vec![];
        collect(disk, false, &mut rows);
        let system = rows
            .iter()
            .any(|(row, _)| SYSTEM_MOUNTS.contains(&text(&row["mountpoint"]).as_str()));
        for (row, removable) in rows {
            let fstype = text(&row["fstype"]);
            if fstype.is_empty() {
                continue;
            }
            let mount = row["mountpoint"]
                .as_str()
                .filter(|m| m.starts_with('/'))
                .map(PathBuf::from);
            out.push(Drive {
                path: text(&row["path"]),
                uuid: text(&row["uuid"]),
                fstype,
                label: text(&row["label"]),
                size: size(&row["size"]),
                mount,
                removable,
                system,
                device: text(&row["maj:min"]),
            });
        }
    }
    out
}

fn collect<'a>(row: &'a Value, above: bool, out: &mut Vec<(&'a Value, bool)>) {
    let removable = above || flag(&row["rm"]) || flag(&row["hotplug"]);
    out.push((row, removable));
    for child in row["children"].as_array().into_iter().flatten() {
        collect(child, removable, out);
    }
}

/// The columns asked of lsblk (MOUNTPOINT, not the newer MOUNTPOINTS: Debian 11's lsblk
/// has only the first).
const LSBLK: &str = "PATH,UUID,FSTYPE,LABEL,SIZE,MOUNTPOINT,RM,HOTPLUG,MAJ:MIN";

/// The drives plugged in now.
pub fn plugged_in() -> Result<Vec<Drive>> {
    let (code, said) = common::command(&["lsblk", "-J", "-b", "-o", LSBLK], 20)?;
    if code != 0 {
        return Err(Error::new("The list of drives (lsblk) could not be read"));
    }
    let listed: Value = serde_json::from_str(&said)
        .map_err(|_| Error::new("The list of drives (lsblk) could not be read"))?;
    Ok(parse(&listed))
}

/// The drives NekoServer itself uses (the settings name them by UUID): the app SSD, the
/// media drive, the USB backup drive, the other drives something is placed on.
pub fn own_uuids(cfg: &Value) -> Vec<String> {
    let mut found: Vec<String> = cfg["storage"]
        .as_object()
        .into_iter()
        .flatten()
        .filter_map(|(_, d)| d["uuid"].as_str().map(str::to_string))
        .collect();
    if let Some(usb) = cfg["photos"]["external_backup_uuid"].as_str() {
        found.push(usb.to_string());
    }
    found.retain(|u| !u.is_empty());
    found
}

/// Why `drive` is never a backup card, in words for the person; None when it may be one.
pub fn refused(drive: &Drive, cfg: &Value) -> Option<String> {
    if drive.system {
        return Some("This is the disk Linux runs from: it is never used as a backup card.".into());
    }
    if own_uuids(cfg).contains(&drive.uuid) {
        return Some(
            "This is one of NekoServer's own drives (it holds the apps, your files or their backups): it is never used as a backup card."
                .into(),
        );
    }
    if !drive.removable {
        return Some(
            "This drive is built into the PC, not removable: backup cards are SD cards or USB sticks you can take somewhere else."
                .into(),
        );
    }
    let kinds = common::shared_list(&["cards", "filesystems"]);
    if !kinds.contains(&drive.fstype.as_str()) {
        return Some(format!(
            "This card is formatted {}, which backup cards are not: they are ext4 or exFAT. NekoServer never formats a card; format it on a computer first (that erases it).",
            drive.fstype
        ));
    }
    if drive.uuid.is_empty() {
        return Some("This card's filesystem has no ID (UUID), so it could not be told apart from another one.".into());
    }
    None
}

/// The device number of the filesystem mounted at `mount`, as "major:minor".
pub fn mounted_device(mount: &Path) -> Option<String> {
    let dev = std::fs::metadata(mount).ok()?.dev();
    Some(format!("{}:{}", libc::major(dev), libc::minor(dev)))
}

/// The identity.json on the card mounted at `mount`, read through descriptors; None
/// when it has no NekoBackup folder.
pub fn identity_at(mount: &Path) -> Result<Option<Identity>> {
    let root = Dir::open_path(mount).map_err(super::card_error)?;
    let names = root.names().map_err(super::card_error)?;
    if !names.iter().any(|n| n.eq_ignore_ascii_case(format::ROOT)) {
        return Ok(None);
    }
    let bytes = root
        .open_dir(format::ROOT)
        .and_then(|nb| nb.read_limited(format::IDENTITY, format::SMALL_FILE))
        .map_err(super::card_error)?;
    Identity::parse(&bytes).map(Some).map_err(super::card_error)
}

/// A card of the set, plugged in, mounted and checked: what attaching it must find.
#[derive(Debug, Clone)]
pub struct Card {
    pub mount: PathBuf,
    pub row: VolumeRow,
    pub expect: Expect,
}

/// What the volume check expects of a mounted card: this device, this set, this card,
/// this filesystem.
pub fn expect(mount: &Path, set_id: &str, volume_id: &str, uuid: &str) -> Result<Expect> {
    let device = std::fs::metadata(mount)
        .map_err(|e| common::at(e, mount))?
        .dev();
    Ok(Expect {
        device,
        backup_set_id: set_id.to_string(),
        volume_id: volume_id.to_string(),
        filesystem_uuid: uuid.to_string(),
        rule: MountRule::MountPoint,
    })
}

/// The cards of set `set_id` among `drives`, in their order in the set. A card the person
/// marked lost, retired or damaged is never written to.
pub fn cards_of_set(store: &Store, set_id: &str, drives: &[Drive], cfg: &Value) -> Vec<Card> {
    let mut found = vec![];
    for drive in drives {
        let Some(mount) = drive.mount.clone() else {
            continue;
        };
        if refused(drive, cfg).is_some() || mounted_device(&mount).as_deref() != Some(&drive.device)
        {
            continue;
        }
        let Ok(Some(identity)) = identity_at(&mount) else {
            continue;
        };
        let Ok(Some(row)) = store.volume(&identity.volume_id) else {
            continue;
        };
        let same = identity.backup_set_id == set_id
            && row.backup_set_id == set_id
            && row.filesystem_uuid == drive.uuid
            && identity.filesystem_uuid == drive.uuid;
        if !same || row.state.is_final() {
            continue;
        }
        if let Ok(expect) = expect(&mount, set_id, &row.volume_id, &drive.uuid) {
            found.push(Card { mount, row, expect });
        }
    }
    found.sort_by_key(|c| c.row.sequence_number);
    found
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn listing() -> Value {
        json!({"blockdevices": [
            {"path": "/dev/nvme0n1", "uuid": null, "fstype": null, "rm": false, "hotplug": false,
             "size": 512_000_000_000u64, "maj:min": "259:0", "children": [
                {"path": "/dev/nvme0n1p1", "uuid": "AAAA-1111", "fstype": "vfat", "mountpoint": "/boot/efi",
                 "rm": false, "hotplug": false, "size": "536870912", "maj:min": "259:1"},
                {"path": "/dev/nvme0n1p2", "uuid": "root-uuid", "fstype": "ext4", "mountpoint": "/",
                 "rm": false, "hotplug": false, "size": 500_000_000_000u64, "maj:min": "259:2"}]},
            {"path": "/dev/sda", "uuid": "ssd-uuid", "fstype": "ext4", "mountpoint": "/srv/nekoserver",
             "rm": "0", "hotplug": "0", "size": 1_000_000_000_000u64, "maj:min": "8:0"},
            {"path": "/dev/sdb", "uuid": null, "fstype": null, "rm": "1", "hotplug": "1",
             "size": 64_000_000_000u64, "maj:min": "8:16", "children": [
                {"path": "/dev/sdb1", "uuid": "1234-ABCD", "fstype": "exfat", "label": "CARD",
                 "mountpoint": "/run/media/amy/CARD", "rm": "1", "hotplug": "1", "size": "63999000000",
                 "maj:min": "8:17"}]},
            {"path": "/dev/sdc", "uuid": "c-uuid", "fstype": "ntfs", "mountpoint": "/media/amy/WIN",
             "rm": true, "hotplug": true, "size": 32_000_000_000u64, "maj:min": "8:32"},
            {"path": "/dev/sdd", "uuid": "d-uuid", "fstype": "ext4", "mountpoint": null,
             "rm": false, "hotplug": true, "size": 32_000_000_000u64, "maj:min": "8:48"}
        ]})
    }

    #[test]
    fn lsblk_is_read_with_each_partition_knowing_its_disk() {
        let found = parse(&listing());
        let by = |p: &str| found.iter().find(|d| d.path == p).unwrap().clone();
        assert!(by("/dev/nvme0n1p1").system && by("/dev/nvme0n1p2").system);
        assert!(!by("/dev/nvme0n1p2").removable);
        let card = by("/dev/sdb1");
        assert!(card.removable && !card.system);
        assert_eq!(
            (card.size, card.label.as_str(), card.device.as_str()),
            (63_999_000_000, "CARD", "8:17")
        );
        assert_eq!(
            card.mount.as_deref(),
            Some(Path::new("/run/media/amy/CARD"))
        );
        assert!(by("/dev/sdd").removable, "a USB drive says hotplug");
        assert_eq!(by("/dev/sdd").mount, None);
        assert!(
            !found.iter().any(|d| d.path == "/dev/sdb"),
            "a disk with partitions has no filesystem"
        );
    }

    #[test]
    fn the_system_disk_nekoservers_drives_and_fixed_drives_are_never_cards() {
        let cfg = json!({"storage": {"data_ssd": {"uuid": "ssd-uuid"}, "media_hdd": {"uuid": "hdd-uuid"}},
                         "photos": {"external_backup_uuid": "1234-ABCD"}});
        let found = parse(&listing());
        let why = |p: &str| refused(found.iter().find(|d| d.path == p).unwrap(), &cfg);
        assert!(why("/dev/nvme0n1p2")
            .unwrap()
            .contains("the disk Linux runs from"));
        assert!(why("/dev/sda").unwrap().contains("NekoServer's own drives"));
        // The USB backup drive of the weekly copy is NekoServer's too, removable or not.
        assert!(why("/dev/sdb1")
            .unwrap()
            .contains("NekoServer's own drives"));
        assert!(why("/dev/sdc").unwrap().contains("formatted ntfs"));
        assert_eq!(why("/dev/sdd"), None);
        let fixed = Drive {
            removable: false,
            ..found.iter().find(|d| d.path == "/dev/sdd").unwrap().clone()
        };
        assert!(refused(&fixed, &cfg).unwrap().contains("not removable"));
        assert_eq!(
            refused(&found[3], &json!({})),
            None,
            "the exFAT card, for another server"
        );
    }
}
