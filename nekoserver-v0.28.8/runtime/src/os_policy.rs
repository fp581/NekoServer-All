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
pub fn run(a: &Args) -> Result<()> {
    let backup::Job {
        cfgpath: config,
        cfg,
        data,
        media,
    } = backup::open_job(a, 64000)?;
    standby(&cfg)?;
    let source = output(&[
        "findmnt",
        "-n",
        "-o",
        "SOURCE",
        "--mountpoint",
        &media.to_string_lossy(),
    ])?;
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
            return Err(Error::new(
                "Mapped/RAID device policy requires manual setup",
            ))
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
    if shape.split_whitespace().collect::<Vec<_>>() != vec!["disk", "1", "sata"] {
        return Err(Error::new(
            "Only an internal rotating SATA HDD is supported",
        ));
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
    let timer = standby(&cfg)?.to_string();
    if common::command(&["hdparm", "-S", &timer, &disk.to_string_lossy()], 20)?.0 != 0 {
        return Err(Error::new("HDD standby command refused"));
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
