//! A drive that comes back after it disappeared (unplugged, a loose cable, a USB case
//! that reset) leaves a dead mount behind, and Docker, bound to its drives, stays down
//! until someone steps in. systemd runs this whenever the SSD or HDD appears (and at
//! boot, when there is nothing to do): the dead mount is released, the drive mounted
//! again and checked, and the services NekoServer keeps on its drives started again.
use crate::{
    backup,
    common::{self, Error, Result},
    Args,
};
use std::{
    path::{Path, PathBuf},
    thread,
    time::{Duration, Instant},
};

/// What to do for one drive.
#[derive(Debug, PartialEq)]
enum Step {
    /// Mounted and alive.
    Nothing,
    /// Still not connected: wait for it to appear (this runs again when it does).
    Wait,
    /// Connected: mount it, after releasing the dead mount it left behind if any.
    Remount { release: bool },
}

/// `mounted`: the device (MAJ:MIN) of the mount in effect at the drive's folder;
/// `alive`: that device still exists; `present`: the drive (by its ID) is connected.
fn decide(mounted: Option<&str>, alive: bool, present: bool) -> Step {
    match (mounted, alive, present) {
        (Some(_), true, _) => Step::Nothing,
        (_, _, false) => Step::Wait,
        (Some(_), false, true) => Step::Remount { release: true },
        (None, _, true) => Step::Remount { release: false },
    }
}

/// The services NekoServer keeps on its drives; each is started again only when it is
/// switched on (enabled) and not running.
const SERVICES: [&str; 4] = [
    "docker.service",
    "smbd.service",
    "smb.service",
    "nekoserver-gateway-lan.service",
];

/// The device of the mount in effect at `path`: with mounts stacked on one folder
/// findmnt lists them oldest first, and the last one is in use.
fn mounted(path: &Path) -> Result<Option<String>> {
    let p = path.to_string_lossy();
    let (code, out) = common::command(&["findmnt", "-n", "-o", "MAJ:MIN", "--mountpoint", &p], 10)?;
    Ok(if code == 0 {
        out.lines()
            .map(str::trim)
            .rfind(|l| !l.is_empty())
            .map(str::to_string)
    } else {
        None
    })
}

fn run_ok(args: &[&str], timeout: u64) -> Result<String> {
    let (code, out) = common::command(args, timeout)?;
    if code != 0 {
        return Err(Error::new(&format!(
            "{} failed: {}",
            args.join(" "),
            out.lines().last().unwrap_or("no details")
        )));
    }
    Ok(out)
}

pub fn run(a: &Args) -> Result<()> {
    common::require_root()?;
    let cfgpath = a.path("config")?;
    let cfg = common::json(&cfgpath, 64000);
    let mut drives: Vec<(PathBuf, String)> = vec![];
    for (key, drive) in [("data_mount", "data_ssd"), ("media_mount", "media_hdd")] {
        let path = common::mount_path(cfg[key].as_str().unwrap_or(""))?;
        let uuid = cfg["storage"][drive]["uuid"].as_str().unwrap_or("");
        if uuid.is_empty() || !uuid.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-') {
            return Err(Error::new("Drive identity missing from the settings"));
        }
        drives.push((path, uuid.to_string()));
    }
    // systemd can start this before the drive is back: anything that asks for the drive
    // (Docker woken by a command, say) pulls this in at once. A drive still missing is
    // then waited for, so the services are started again once it returns. `recovering`
    // stays false at a normal boot, when both drives are already mounted.
    let deadline = Instant::now() + Duration::from_secs(600);
    let mut recovering = false;
    let mut told = false;
    loop {
        let mut waiting = false;
        for (path, uuid) in &drives {
            let present = Path::new("/dev/disk/by-uuid").join(uuid).exists();
            let device = mounted(path)?;
            let alive = device
                .as_deref()
                .is_some_and(|d| Path::new("/sys/dev/block").join(d).exists());
            let shown = path.to_string_lossy().into_owned();
            match decide(device.as_deref(), alive, present) {
                Step::Nothing => {}
                Step::Wait => {
                    if !told {
                        println!(
                            "Drive return: waiting for the drive for {shown} to be connected again"
                        );
                        told = true;
                    }
                    waiting = true;
                    recovering = true;
                }
                Step::Remount { release } => {
                    if release {
                        println!("Drive return: the drive for {shown} is back; releasing the mount it left behind");
                        run_ok(&["umount", "-l", &shown], 60)?;
                    }
                    let unit = run_ok(&["systemd-escape", "--path", "--suffix=mount", &shown], 10)?;
                    run_ok(&["systemctl", "start", unit.trim()], 120)?;
                    println!("Drive return: mounted {shown} again");
                    recovering = true;
                }
            }
        }
        if !waiting {
            break;
        }
        if Instant::now() >= deadline {
            println!("Drive return: a drive is still not connected after 10 minutes; this runs again as soon as it is");
            return Ok(());
        }
        thread::sleep(Duration::from_secs(5));
    }
    if !recovering {
        return Ok(());
    }
    let paths: Vec<PathBuf> = drives.into_iter().map(|(p, _)| p).collect();
    // Anything is started again only with both drives in place and the right ones.
    backup::guard(&cfgpath, &paths[0], &paths[1])?;
    for unit in SERVICES {
        let enabled = common::command(&["systemctl", "is-enabled", "--quiet", unit], 15)?.0 == 0;
        let active = common::command(&["systemctl", "is-active", "--quiet", unit], 15)?.0 == 0;
        if enabled && !active {
            let _ = common::command(&["systemctl", "reset-failed", unit], 15);
            run_ok(&["systemctl", "start", unit], 300)?;
            println!("Drive return: started {unit} again");
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn a_dead_mount_is_released_only_once_the_drive_is_back() {
        // A live mount: nothing to do (also the case at every boot).
        assert_eq!(decide(Some("8:112"), true, true), Step::Nothing);
        // The drive is gone and its mount is dead: wait for it; nothing is touched.
        assert_eq!(decide(Some("8:112"), false, false), Step::Wait);
        // Back again (as another device): release the dead mount, mount it again.
        assert_eq!(
            decide(Some("8:112"), false, true),
            Step::Remount { release: true }
        );
        // Missing at boot and connected later: just mount it.
        assert_eq!(decide(None, false, true), Step::Remount { release: false });
        assert_eq!(decide(None, false, false), Step::Wait);
    }
}
