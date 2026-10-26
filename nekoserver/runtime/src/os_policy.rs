//! Reapply only a verified internal SATA media HDD's legacy standby timer.
use crate::{
    backup,
    common::{self, Error, Result},
    startup, Args,
};
use std::path::{Component, Path, PathBuf};
fn output(args: &[&str]) -> Result<String> {
    let (code, text) = common::command(args, 20)?;
    if code != 0 {
        return Err(Error::new("Drive policy probe failed"));
    }
    Ok(text.trim().into())
}
/// The block device behind what findmnt reports: for a Btrfs subvolume that is
/// "/dev/sdb1[/@media]", and the device is the part before the bracket.
fn block_device(source: &str) -> &str {
    source.split('[').next().unwrap_or(source)
}
fn device(value: &str) -> Result<PathBuf> {
    let name = block_device(value);
    let name = name.strip_prefix("/dev/").unwrap_or(name);
    if name.is_empty() || !name.bytes().all(|b| b.is_ascii_alphanumeric()) {
        return Err(Error::new("Simple physical device path required"));
    }
    Ok(Path::new("/dev").join(name))
}
/// A /dev path made only of plain names (/dev/sdb1, /dev/mapper/luks-3f2a, /dev/dm-2).
fn plain_device(path: &Path) -> bool {
    path.strip_prefix("/dev").is_ok_and(|rest| {
        rest.components().next().is_some()
            && rest.components().all(|part| match part {
                Component::Normal(name) => name.to_str().is_some_and(|name| {
                    !name.starts_with(['-', '.'])
                        && name
                            .bytes()
                            .all(|b| b.is_ascii_alphanumeric() || b"-_.+:".contains(&b))
                }),
                _ => false,
            })
    })
}
/// The device findmnt reports for the HDD, resolved through its link (/dev/mapper/<name>
/// links to /dev/dm-N), for lsblk to say what it is. An encrypted (LUKS) or LVM drive is
/// such a device; it is only classified, never given to hdparm (`device` stays strict).
fn source_device(
    value: &str,
    resolve: impl Fn(&Path) -> std::io::Result<PathBuf>,
) -> Result<PathBuf> {
    let path = Path::new(block_device(value));
    if !plain_device(path) {
        return Err(Error::new("Simple device path required"));
    }
    let real = resolve(path).map_err(|_| Error::new("Media HDD device not found"))?;
    if !plain_device(&real) {
        return Err(Error::new("Simple device path required"));
    }
    Ok(real)
}
/// The devices of the HDD's filesystem: the one findmnt names (`source`) and, for a Btrfs
/// pool ("<uuid> btrfs" in `ids`), every other device of it, since findmnt names only the
/// one with the lowest device ID and the timer is for each drive of the pool.
fn with_members(
    source: &str,
    ids: &str,
    listed: impl FnOnce(&str) -> Option<Vec<String>>,
) -> Vec<String> {
    let mut all = vec![source.to_string()];
    if let [uuid, "btrfs"] = ids.split_whitespace().collect::<Vec<_>>()[..] {
        all.extend(
            listed(uuid)
                .unwrap_or_default()
                .into_iter()
                .filter(|d| d != source),
        );
    }
    all
}
/// The whole disk under a device of the HDD's filesystem, None for an encrypted, LVM or
/// RAID device (the disks under it are not set one by one).
fn disk_of(member: &str) -> Result<Option<PathBuf>> {
    let source = source_device(member, |path: &Path| std::fs::canonicalize(path))?;
    let text = source.to_string_lossy();
    Ok(
        match output(&["lsblk", "-dn", "-o", "TYPE", "--", &text])?.as_str() {
            "disk" => Some(device(&text)?),
            "part" => Some(device(&output(&[
                "lsblk", "-dn", "-o", "PKNAME", "--", &text,
            ])?)?),
            _ => None,
        },
    )
}
fn standby(cfg: &serde_json::Value) -> Result<u64> {
    match cfg["server_os"]["hdd_policy"]
        .as_str()
        .unwrap_or("keep_spinning")
    {
        "keep_spinning" => Ok(0),
        "idle" => {
            let minutes = cfg["server_os"]["hdd_idle_minutes"].as_u64().unwrap_or(0);
            if !(30..=330).contains(&minutes) || minutes % 30 != 0 {
                return Err(Error::new("Invalid HDD idle time"));
            }
            Ok(240 + minutes / 30)
        }
        _ => Err(Error::new("Unsupported HDD policy")),
    }
}
/// Why the standby timer is not for this drive, from lsblk's TYPE, ROTA and TRAN of the
/// whole disk; None for an internal spinning SATA disk, the one hdparm's timer is for.
/// The others keep their own power settings: a USB case may ignore the timer or pass it
/// on wrongly, and an SSD does not spin.
fn not_for_timer(shape: &str) -> Option<String> {
    match shape.split_whitespace().collect::<Vec<_>>().as_slice() {
        ["disk", "1", "sata"] => None,
        [_, _, "usb"] => Some("it is connected by USB".into()),
        [_, "0", ..] => Some("it is not a spinning disk".into()),
        [_, _, other] => Some(format!(
            "it is not an internal SATA disk (connected by {other})"
        )),
        _ => Some("its connection is not reported as SATA".into()),
    }
}
/// The line setup shows: what was set, or why the drive keeps its own settings.
fn left_to_drive(why: &str) -> String {
    format!("Drive power settings: left to the drive itself, as {why}")
}
pub fn run(a: &Args) -> Result<()> {
    let backup::Job {
        cfgpath: config,
        cfg,
        data,
        media,
    } = backup::open_job(a, 64000)?;
    standby(&cfg)?;
    let source = common::findmnt("SOURCE", "--mountpoint", &media, 20)?
        .ok_or_else(|| Error::new("Media HDD is not mounted"))?;
    if !source.starts_with("/dev/") {
        return Err(Error::new("Block-backed media HDD required"));
    }
    // A copy of the HDD left connected would be counted as a device of a pool.
    startup::copies(&cfg)?;
    let raw = block_device(&source).to_string();
    let ids = common::findmnt("UUID,FSTYPE", "--mountpoint", &media, 20)?.unwrap_or_default();
    let mut disks: Vec<PathBuf> = vec![];
    for member in with_members(&raw, &ids, startup::devices_with_uuid) {
        match disk_of(&member)? {
            Some(disk) if !disks.contains(&disk) => disks.push(disk),
            Some(_) => {}
            None => {
                println!(
                    "{}",
                    left_to_drive("it is an encrypted, LVM or RAID device")
                );
                return Ok(());
            }
        }
    }
    let several = disks.len() > 1;
    let mut timed = vec![];
    for disk in disks {
        let shape = output(&[
            "lsblk",
            "-dn",
            "-o",
            "TYPE,ROTA,TRAN",
            "--",
            &disk.to_string_lossy(),
        ])?;
        match not_for_timer(&shape) {
            Some(why) if several => {
                println!("{}", left_to_drive(&format!("{why} ({})", disk.display())))
            }
            Some(why) => println!("{}", left_to_drive(&why)),
            None => timed.push(disk),
        }
    }
    if timed.is_empty() {
        return Ok(());
    }
    // Resolve again immediately before mutation; never use a guessed /dev/sdX.
    backup::guard(&config, &data, &media)?;
    let again = output(&[
        "findmnt",
        "-n",
        "-o",
        "SOURCE",
        "--mountpoint",
        &media.to_string_lossy(),
    ])?;
    if block_device(&again) != raw {
        return Err(Error::new("Media device changed"));
    }
    let timer = standby(&cfg)?;
    for disk in timed {
        if common::command(
            &["hdparm", "-S", &timer.to_string(), &disk.to_string_lossy()],
            20,
        )?
        .0 != 0
        {
            return Err(Error::new("HDD standby command refused"));
        }
        println!(
            "Drive power settings: {} {}",
            disk.display(),
            if timer == 0 {
                "keeps spinning (its standby timer is off)".to_string()
            } else {
                format!(
                    "spins down after {} minutes without use",
                    (timer - 240) * 30
                )
            }
        );
    }
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn idle_timer_encoding_and_invalid_policies() {
        for (minutes, code) in [(30, 241), (60, 242), (330, 251)] {
            assert_eq!(standby(&serde_json::json!({"server_os":{"hdd_policy":"idle","hdd_idle_minutes":minutes}})).unwrap(), code);
        }
        for minutes in [0, 1, 29, 31, 331] {
            assert!(standby(
                &serde_json::json!({"server_os":{"hdd_policy":"idle","hdd_idle_minutes":minutes}})
            )
            .is_err());
        }
        assert!(standby(&serde_json::json!({"server_os":{"hdd_policy":"unchanged"}})).is_err());
    }
    #[test]
    fn only_an_internal_spinning_sata_disk_gets_the_timer() {
        assert_eq!(not_for_timer("disk 1 sata"), None);
        assert_eq!(not_for_timer(" disk   1 sata\n"), None);
        assert_eq!(
            not_for_timer("disk 1 usb").unwrap(),
            "it is connected by USB"
        );
        assert_eq!(
            not_for_timer("disk 0 nvme").unwrap(),
            "it is not a spinning disk"
        );
        assert_eq!(
            not_for_timer("disk 0 sata").unwrap(),
            "it is not a spinning disk"
        );
        assert!(not_for_timer("disk 1 sas").unwrap().contains("sas"));
        // A virtual disk reports no connection at all.
        assert!(not_for_timer("disk 1").unwrap().contains("not reported"));
        assert_eq!(
            left_to_drive("it is connected by USB"),
            "Drive power settings: left to the drive itself, as it is connected by USB"
        );
    }
    #[test]
    fn device_paths_are_narrow() {
        assert_eq!(device("sda").unwrap(), PathBuf::from("/dev/sda"));
        // What hdparm is given: a plain disk name only, never a mapper path.
        for name in ["../sda", "/dev/mapper/disk", "sda\n-S1", ""] {
            assert!(device(name).is_err());
        }
        // A Btrfs subvolume: the device is what comes before the bracket.
        assert_eq!(
            device("/dev/sdb1[/@media]").unwrap(),
            PathBuf::from("/dev/sdb1")
        );
        assert_eq!(block_device("/dev/sdb1[/media]"), "/dev/sdb1");
        assert_eq!(block_device("/dev/sdb1"), "/dev/sdb1");
        assert!(device("/dev/sdb1[/../x]").is_ok()); // only the device part is used
    }
    #[test]
    fn an_encrypted_or_lvm_hdd_is_classified_not_refused() {
        // /dev/mapper/<name> links to /dev/dm-N; lsblk then says crypt or lvm, and the
        // drive is left to itself instead of the service failing at every start.
        let links = |path: &Path| -> std::io::Result<PathBuf> {
            Ok(match path.to_str() {
                Some("/dev/mapper/luks-3f2a") => PathBuf::from("/dev/dm-0"),
                Some("/dev/mapper/vg-media") => PathBuf::from("/dev/dm-2"),
                Some("/dev/mapper/evil") => PathBuf::from("/etc/passwd"),
                _ => path.to_path_buf(),
            })
        };
        for (source, real) in [
            ("/dev/mapper/luks-3f2a", "/dev/dm-0"),
            ("/dev/mapper/vg-media[/@media]", "/dev/dm-2"),
            ("/dev/dm-2", "/dev/dm-2"),
            ("/dev/md127", "/dev/md127"),
            ("/dev/sdb1", "/dev/sdb1"),
        ] {
            assert_eq!(source_device(source, links).unwrap(), PathBuf::from(real));
        }
        for bad in [
            "/dev/mapper/evil",
            "/dev/../etc/passwd",
            "/dev/mapper/-S1",
            "/dev/",
            "/etc/x",
            "/dev/sd b",
        ] {
            assert!(source_device(bad, links).is_err(), "{bad}");
        }
        assert!(source_device("/dev/gone", |_: &Path| Err(std::io::Error::other("x"))).is_err());
    }
    #[test]
    fn every_device_of_a_btrfs_pool_gets_the_timer() {
        let listed = |uuid: &str| {
            assert_eq!(uuid, "pool-uuid");
            Some(vec!["/dev/sdb".to_string(), "/dev/sdc".to_string()])
        };
        assert_eq!(
            with_members("/dev/sdb", "pool-uuid btrfs", listed),
            ["/dev/sdb", "/dev/sdc"]
        );
        let unused = |_: &str| -> Option<Vec<String>> { panic!("only a Btrfs pool is listed") };
        assert_eq!(with_members("/dev/sdb1", "u ext4", unused), ["/dev/sdb1"]);
        assert_eq!(
            with_members("/dev/sdb", "pool-uuid btrfs", |_| None),
            ["/dev/sdb"]
        );
    }
}
