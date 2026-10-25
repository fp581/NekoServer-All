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

/// Started again once the drives are back, after the services above:
/// - the HDD's power settings (a oneshot that applies them): the drive keeps its own
///   until then, after it was missing at boot or lost them when it reset;
/// - the dashboard's web and control services, when they run: started before the SSD was
///   mounted, their view of it (the folders systemd opened or hid for them) is stale.
///   Without `--no-block`, a restart of the control service would wait for an app
///   restart it is in the middle of.
fn refresh_commands(enabled: impl Fn(&str) -> bool) -> Vec<Vec<&'static str>> {
    let mut commands = vec![];
    if enabled("nekoserver-hdd-policy.service") {
        commands.push(vec![
            "systemctl",
            "restart",
            "nekoserver-hdd-policy.service",
        ]);
    }
    for unit in ["nekoserver-dashboard.service", "nekoserver-control.service"] {
        commands.push(vec!["systemctl", "try-restart", "--no-block", unit]);
    }
    commands
}

/// The recovery services of the jobs that stop apps (the monthly update, the nightly app
/// and host snapshots, a photo snapshot), in the order they are finished: the monthly
/// update's journal first (it decides whether apps roll back), then the snapshots'.
/// Each finds its own journal and does nothing without one. Starting Docker alone does
/// not do this: containers a job stopped stay stopped under `unless-stopped`.
const RESUMES: [&str; 4] = [
    "nekoserver-maintenance-resume.service",
    "nekoserver-app-resume.service",
    "nekoserver-host-resume.service",
    "nekoserver-immich-snapshot-resume.service",
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
    let enabled = |unit: &str| {
        common::command(&["systemctl", "is-enabled", "--quiet", unit], 15)
            .is_ok_and(|(code, _)| code == 0)
    };
    for args in refresh_commands(enabled) {
        // Not being able to set the drive's power, say, never stops the apps coming back.
        match run_ok(&args, 240) {
            Ok(_) => println!("Drive return: {} {}", args[1], args[args.len() - 1]),
            Err(e) => println!("Drive return: {e}"),
        }
    }
    finish_interrupted_jobs(
        |unit| common::command(&["systemctl", "cat", unit], 15).is_ok_and(|(code, _)| code == 0),
        |unit| run_ok(&["systemctl", "start", unit], 3 * 3600).map(|_| ()),
    )
}

/// A job a drive cut off part-way (the apps it stopped, its journal) is finished now that
/// both drives are back: each installed recovery service runs, in RESUMES order. Every
/// one is tried; the first failure is returned, so the run shows as failed.
fn finish_interrupted_jobs(
    installed: impl Fn(&str) -> bool,
    start: impl Fn(&str) -> Result<()>,
) -> Result<()> {
    let mut first = None;
    for unit in RESUMES.iter().filter(|u| installed(u)) {
        match start(unit) {
            Ok(()) => println!("Drive return: {unit} finished what a cut-off job left"),
            Err(e) => {
                println!("Drive return: {unit} failed ({e})");
                first.get_or_insert(e);
            }
        }
    }
    first.map_or(Ok(()), Err)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn interrupted_jobs_are_finished_in_order_and_a_failure_is_reported() {
        use std::cell::RefCell;
        let started = RefCell::new(vec![]);
        let installed = |u: &str| u != "nekoserver-immich-snapshot-resume.service";
        finish_interrupted_jobs(installed, |u| {
            started.borrow_mut().push(u.to_string());
            Ok(())
        })
        .unwrap();
        assert_eq!(
            *started.borrow(),
            [
                "nekoserver-maintenance-resume.service",
                "nekoserver-app-resume.service",
                "nekoserver-host-resume.service"
            ]
        );
        started.borrow_mut().clear();
        let failed = finish_interrupted_jobs(
            |_| true,
            |u| {
                started.borrow_mut().push(u.to_string());
                if u == "nekoserver-app-resume.service" {
                    Err(Error::new("apps not back"))
                } else {
                    Ok(())
                }
            },
        );
        assert!(failed.unwrap_err().to_string().contains("apps not back"));
        assert_eq!(started.borrow().len(), 4, "the others are still tried");
    }
    #[test]
    fn the_hdd_power_settings_and_the_dashboard_are_renewed_after_a_remount() {
        let with_policy = refresh_commands(|u| u == "nekoserver-hdd-policy.service");
        assert_eq!(
            with_policy[0],
            ["systemctl", "restart", "nekoserver-hdd-policy.service"]
        );
        assert!(with_policy.contains(&vec![
            "systemctl",
            "try-restart",
            "--no-block",
            "nekoserver-control.service"
        ]));
        assert!(with_policy.contains(&vec![
            "systemctl",
            "try-restart",
            "--no-block",
            "nekoserver-dashboard.service"
        ]));
        // Policy "unchanged" (the unit is switched off): no HDD command.
        let without = refresh_commands(|_| false);
        assert!(without.iter().all(|c| c[1] == "try-restart"));
    }
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
