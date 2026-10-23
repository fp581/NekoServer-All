//! Reapply only a verified internal SATA media HDD's legacy standby timer.
use crate::{
    backup,
    common::{self, Error, Result},
    Args,
};
use std::path::{Path, PathBuf};
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
    let source = device(&source)?;
    let source_text = source.to_string_lossy();
    let kind = output(&["lsblk", "-dn", "-o", "TYPE", "--", &source_text])?;
    let disk = match kind.as_str() {
        "disk" => source.clone(),
        "part" => device(&output(&[
            "lsblk",
            "-dn",
            "-o",
            "PKNAME",
            "--",
            &source_text,
        ])?)?,
        _ => {
            // Encrypted, LVM or RAID: the disks under it are not set one by one.
            println!(
                "{}",
                left_to_drive("it is an encrypted, LVM or RAID device")
            );
            return Ok(());
        }
    };
    let shape = output(&[
        "lsblk",
        "-dn",
        "-o",
        "TYPE,ROTA,TRAN",
        "--",
        &disk.to_string_lossy(),
    ])?;
    if let Some(why) = not_for_timer(&shape) {
        println!("{}", left_to_drive(&why));
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
    if block_device(&again) != source_text {
        return Err(Error::new("Media device changed"));
    }
    let timer = standby(&cfg)?;
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
}
