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
/// Said when the drives came back under a running setup, which brings the apps back itself.
const LEFT_TO_SETUP: &str = "Drive return: setup (sudo bash install.sh) is running and starts the apps itself once the drives are in place; nothing to do here";
const HELD_FOR_NAS: &str = "Drive return: the drives are back, but a drive on the network storage box does not take writes, so the apps stay stopped (the NAS check starts them once it does; check it with  sudo bash install.sh --recover)";

pub(crate) const SERVICES: [&str; 4] = [
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
///   restart it is in the middle of. Only those installed (`installed`): on a server
///   without the dashboard, systemctl refused them ("not found") at every drive return,
///   and the journal showed two failures that were none.
fn refresh_commands(
    enabled: impl Fn(&str) -> bool,
    installed: impl Fn(&str) -> bool,
) -> Vec<Vec<&'static str>> {
    let mut commands = vec![];
    if enabled("nekoserver-hdd-policy.service") {
        commands.push(vec![
            "systemctl",
            "restart",
            "nekoserver-hdd-policy.service",
        ]);
    }
    for unit in ["nekoserver-dashboard.service", "nekoserver-control.service"] {
        if installed(unit) {
            commands.push(vec!["systemctl", "try-restart", "--no-block", unit]);
        }
    }
    commands
}

/// The recovery services of the jobs that stop apps (the monthly update, the nightly app
/// and host snapshots, the weekly Immich and Nextcloud pauses, a photo snapshot), in the order they are
/// finished: the monthly update's journal first (it decides whether apps roll back),
/// then the others'. Each finds its own journal and does nothing without one. Starting
/// Docker alone does not do this: containers a job stopped stay stopped under
/// `unless-stopped`.
const RESUMES: [&str; 6] = [
    "nekoserver-maintenance-resume.service",
    "nekoserver-app-resume.service",
    "nekoserver-host-resume.service",
    "nekoserver-immich-resume.service",
    "nekoserver-nextcloud-resume.service",
    "nekoserver-immich-snapshot-resume.service",
];

/// The device (MAJ:MIN) and source (/dev/sdb1, or /dev/sdb1[/@data] for a Btrfs
/// subvolume) of the mount in effect at `path`: with mounts stacked on one folder
/// findmnt lists them oldest first, and the last one is in use.
fn mounted(path: &Path) -> Result<Option<(String, String)>> {
    let p = path.to_string_lossy();
    let (code, out) = common::command(
        &["findmnt", "-n", "-o", "MAJ:MIN,SOURCE", "--mountpoint", &p],
        10,
    )?;
    Ok(if code == 0 {
        out.lines().rev().find_map(|l| {
            let mut fields = l.split_whitespace();
            let device = fields.next()?.to_string();
            Some((device, fields.next().unwrap_or("").to_string()))
        })
    } else {
        None
    })
}

/// Is the drive behind a mount still there? Most filesystems show the drive's own
/// device number, listed under /sys/dev/block while the drive is connected. Btrfs shows
/// a made-up one (major 0) that is never listed there, so a live Btrfs drive would look
/// dead and be unmounted at every start; for it the device in the source (/dev/sdb1,
/// which goes away when the drive does) is looked at instead.
fn alive(device: &str, source: &str, exists: impl Fn(&Path) -> bool) -> bool {
    if device.starts_with("0:") {
        let drive = source.split('[').next().unwrap_or("");
        // A folder a NAS shares (NFS, "192.168.1.20:/volume1/media") has no device that
        // could go away: while it is mounted its writes wait for the NAS, and the NAS
        // check watches it. Never released from here.
        if shared_folder_source(drive) {
            return true;
        }
        drive.starts_with("/dev/") && exists(Path::new(drive))
    } else {
        exists(&Path::new("/sys/dev/block").join(device))
    }
}

/// A mount's source is a shared folder's address (host:/folder), not a device.
fn shared_folder_source(source: &str) -> bool {
    !source.starts_with('/') && source.contains(":/")
}
/// The mount in effect is only systemd's automount (findmnt shows the source
/// "systemd-1"): the shared folder behind it is not mounted.
fn only_automount(found: Option<(String, String)>) -> Option<(String, String)> {
    found.filter(|(_, source)| source != "systemd-1")
}

fn run_ok(args: &[&str], timeout: u64) -> Result<String> {
    let (code, out) = common::command(args, timeout)?;
    if code != 0 {
        return Err(Error::new(&format!(
            "{} failed: {}",
            args.join(" "),
            failure_detail(code, &common::last_stderr(), &out)
        )));
    }
    Ok(out)
}
/// Why a command failed: what it said on stderr (systemctl says it there: "Unit … not
/// found", "Job for … failed. See …"), else its last line of output, else its exit code.
fn failure_detail(code: i32, stderr: &str, out: &str) -> String {
    let said = stderr.trim();
    if !said.is_empty() {
        return said.to_string();
    }
    out.lines()
        .map(str::trim)
        .rfind(|l| !l.is_empty())
        .map_or_else(|| format!("exit code {code}"), str::to_string)
}

/// A Btrfs pool over several drives with one of them gone refuses an ordinary mount
/// (only `-o degraded` opens it on the drives left), so at boot its fstab line fails and
/// the drive-return service finds nothing mounted. `btrfs filesystem show <uuid>` lists
/// the pool's drives and ends with "*** Some devices missing" when one is gone.
fn pool_lacks_a_drive(shown: &str) -> bool {
    shown
        .lines()
        .any(|line| line.trim().starts_with("***") && line.contains("missing"))
}

/// What to say once a pool was opened on the drives left: the data there has one copy
/// fewer until the missing drive is replaced (RECOVERY.md shows how).
fn degraded_notice(path: &str) -> String {
    format!(
        "Drive return: a drive of the Btrfs pool for {path} is missing, so the pool was mounted on the drives left (degraded). Your data there has one copy fewer until the drive is replaced; the dashboard's Drives page shows the pool, and RECOVERY.md how to replace the drive"
    )
}

/// The mount unit started; a Btrfs pool missing a drive, which the unit cannot open, is
/// mounted on its remaining drives instead (fstab's options plus `degraded`), so the apps
/// and the backups keep running on the mirror's other half.
fn mount_again(path: &str, uuid: &str, fstype: &str) -> Result<()> {
    let unit = run_ok(&["systemd-escape", "--path", "--suffix=mount", path], 10)?;
    match run_ok(&["systemctl", "start", unit.trim()], 120) {
        Ok(_) => println!("Drive return: mounted {path} again"),
        Err(e) => {
            let (_, shown) = common::probe(&["btrfs", "filesystem", "show", uuid], 30);
            if fstype != "btrfs" || !pool_lacks_a_drive(&shown) {
                return Err(e);
            }
            run_ok(&["mount", "-o", "degraded", path], 120)?;
            println!("{}", degraded_notice(path));
        }
    }
    Ok(())
}

pub fn run(a: &Args) -> Result<()> {
    common::require_root()?;
    let cfgpath = a.path("config")?;
    let cfg = common::json(&cfgpath, 64000);
    let mut drives: Vec<(PathBuf, String, String)> = vec![];
    // The media drive when it is a folder the NAS shares (NFS): its address.
    let export = common::nfs_export(&cfg["storage"]["media_hdd"]);
    for (key, drive) in [("data_mount", "data_ssd"), ("media_mount", "media_hdd")] {
        let path = common::mount_path(cfg[key].as_str().unwrap_or(""))?;
        let uuid = cfg["storage"][drive]["uuid"].as_str().unwrap_or("");
        if uuid.is_empty() || !uuid.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-') {
            return Err(Error::new("Drive identity missing from the settings"));
        }
        let fstype = cfg["storage"][drive]["fstype"].as_str().unwrap_or("");
        drives.push((path, uuid.to_string(), fstype.to_string()));
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
        for (path, uuid, fstype) in &drives {
            // A shared folder is "connected" when its NAS answers.
            let present = match export.filter(|_| common::is_nfs_identity(uuid)) {
                Some(export) => common::nfs_answers(export),
                None => Path::new("/dev/disk/by-uuid").join(uuid).exists(),
            };
            let found = only_automount(mounted(path)?);
            let alive = found
                .as_ref()
                .is_some_and(|(device, source)| alive(device, source, Path::exists));
            let shown = path.to_string_lossy().into_owned();
            match decide(found.as_ref().map(|(d, _)| d.as_str()), alive, present) {
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
                    mount_again(&shown, uuid, fstype)?;
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
    // A drive that "returns" while setup runs is setup's doing (a replaced HDD given its
    // fstab line, say): setup starts Docker, the gateway and the rest itself, with their
    // records brought up to date first. Started from here in between, the gateway found
    // its record naming the old drive and refused, and this run ended as a failure.
    if common::setup_running() {
        println!("{}", LEFT_TO_SETUP);
        return Ok(());
    }
    // The NAS check stopped the apps for a NAS drive that does not take writes: they stay
    // stopped (the 0.46.0 review: plugging the SSD in again started them). That check
    // starts them once every drive takes writes.
    // A NAS drive that fails writes (not one only waiting for the NAS) and that the NAS
    // check hasn't seen yet: held here, so that check starts the apps once it takes writes
    // again (the audit of 0.46.2: refused without a hold, nothing ever started them).
    if crate::nas_watch::held() || crate::nas_watch::hold_if_failing(&cfg)? {
        println!("{HELD_FOR_NAS}");
        return Ok(());
    }
    let paths: Vec<PathBuf> = drives.into_iter().map(|(p, _, _)| p).collect();
    start_again(&cfgpath, &paths[0], &paths[1], "Drive return")
}

/// True when every one of SERVICES that is switched on runs (the apps' services, started
/// again by start_again).
/// The apps run: Docker, which runs them, is up (when switched on). Samba or the gateway
/// failing to start is said, but is no reason to keep the apps held: they don't hold the
/// apps' data, and holding for them stopped every resume for good (the review of 0.47.0).
pub(crate) fn apps_up() -> bool {
    apps_up_with(&|args| common::command(args, 15).map(|(code, _)| code))
}
fn apps_up_with(systemctl: &dyn Fn(&[&str]) -> Result<i32>) -> bool {
    let unit = SERVICES[0];
    let enabled = systemctl(&["systemctl", "is-enabled", "--quiet", unit]).is_ok_and(|c| c == 0);
    !enabled || systemctl(&["systemctl", "is-active", "--quiet", unit]).is_ok_and(|c| c == 0)
}
/// Each service switched on and not running is started; every one is tried. Returns the
/// ones that did not start (the review of 0.47.0: the first failure stopped the rest and
/// every resume after them).
fn start_services(who: &str, systemctl: &dyn Fn(&[&str], u64) -> Result<i32>) -> Vec<&'static str> {
    let mut failed = vec![];
    for unit in SERVICES {
        let enabled =
            systemctl(&["systemctl", "is-enabled", "--quiet", unit], 15).is_ok_and(|c| c == 0);
        let active =
            systemctl(&["systemctl", "is-active", "--quiet", unit], 15).is_ok_and(|c| c == 0);
        if enabled && !active {
            let _ = systemctl(&["systemctl", "reset-failed", unit], 15);
            if systemctl(&["systemctl", "start", unit], 300).is_ok_and(|c| c == 0) {
                println!("{who}: started {unit} again");
            } else {
                println!("{who}: {unit} did not start (journalctl -u {unit})");
                failed.push(unit);
            }
        }
    }
    failed
}
/// The services NekoServer keeps on its drives started again (those switched on), their
/// views of the drives refreshed, then each job a missing drive cut off finished. Only
/// with both drives in place and the right ones. `who` begins each line said.
pub(crate) fn start_again(cfgpath: &Path, data: &Path, media: &Path, who: &str) -> Result<()> {
    backup::guard(cfgpath, data, media)?;
    let failed = start_services(who, &|args, wait| {
        common::command(args, wait).map(|(code, _)| code)
    });
    if failed.contains(&SERVICES[0]) {
        // Without Docker no app and no resume can run: tried again later.
        return Err(Error::new(
            "Docker did not start (journalctl -u docker.service)",
        ));
    }
    let enabled = |unit: &str| {
        common::command(&["systemctl", "is-enabled", "--quiet", unit], 15)
            .is_ok_and(|(code, _)| code == 0)
    };
    let installed = |unit: &str| {
        common::command(&["systemctl", "cat", unit], 15).is_ok_and(|(code, _)| code == 0)
    };
    for args in refresh_commands(enabled, installed) {
        // Not being able to set the drive's power, say, never stops the apps coming back.
        match run_ok(&args, 240) {
            Ok(_) => println!("{who}: {} {}", args[1], args[args.len() - 1]),
            Err(e) => println!("{who}: {e}"),
        }
    }
    let resumed = finish_interrupted_jobs(installed, |unit| {
        run_ok(&["systemctl", "start", unit], 3 * 3600).map(|_| ())
    });
    match failed.first() {
        Some(unit) => Err(Error::new(&format!(
            "{unit} did not start (journalctl -u {unit})"
        ))),
        None => resumed,
    }
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
    fn the_apps_count_as_up_when_docker_runs() {
        // The review of 0.46.3: Docker failing to start lifted the NAS hold all the same.
        let docker_down = |args: &[&str]| -> Result<i32> {
            Ok(if args[1] == "is-active" && args[3] == SERVICES[0] {
                3
            } else {
                0
            })
        };
        assert!(!apps_up_with(&docker_down));
        assert!(apps_up_with(&|_args: &[&str]| -> Result<i32> { Ok(0) }));
        // The review of 0.47.0: a broken gateway (or Samba) kept the hold on for good.
        let gateway_down = |args: &[&str]| -> Result<i32> {
            Ok(if args[1] == "is-active" && args[3] == SERVICES[3] {
                3
            } else {
                0
            })
        };
        assert!(apps_up_with(&gateway_down));
    }
    #[test]
    fn every_service_is_tried_even_when_one_does_not_start() {
        use std::cell::RefCell;
        let started = RefCell::new(vec![]);
        let systemctl = |args: &[&str], _wait: u64| -> Result<i32> {
            Ok(match args[1] {
                "is-enabled" => 0,
                "is-active" => 3,
                "start" => {
                    started.borrow_mut().push(args[2].to_string());
                    i32::from(args[2] == SERVICES[1]) // Samba refuses
                }
                _ => 0,
            })
        };
        assert_eq!(start_services("test", &systemctl), vec![SERVICES[1]]);
        assert_eq!(*started.borrow(), SERVICES.map(str::to_string).to_vec());
    }
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
                "nekoserver-host-resume.service",
                "nekoserver-immich-resume.service",
                "nekoserver-nextcloud-resume.service"
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
        assert_eq!(
            started.borrow().len(),
            RESUMES.len(),
            "the others are still tried"
        );
    }
    #[test]
    fn the_hdd_power_settings_and_the_dashboard_are_renewed_after_a_remount() {
        let with_policy = refresh_commands(|u| u == "nekoserver-hdd-policy.service", |_| true);
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
        let without = refresh_commands(|_| false, |_| true);
        assert!(without.iter().all(|c| c[1] == "try-restart"));
    }
    #[test]
    fn services_that_are_not_installed_are_not_restarted_nor_reported_as_failed() {
        // A server without the dashboard: its two services are not there at all.
        let none = refresh_commands(
            |_| true,
            |u| !u.starts_with("nekoserver-dashboard") && !u.starts_with("nekoserver-control"),
        );
        assert_eq!(
            none,
            vec![vec![
                "systemctl",
                "restart",
                "nekoserver-hdd-policy.service"
            ]]
        );
    }
    #[test]
    fn a_failed_command_keeps_systemctl_s_own_reason() {
        // systemctl says why on stderr, with nothing on stdout: that is what is shown.
        assert_eq!(
            failure_detail(5, "Unit srv-nekoserver.mount not found.", ""),
            "Unit srv-nekoserver.mount not found."
        );
        assert_eq!(failure_detail(1, "", "first\nlast line\n\n"), "last line");
        assert_eq!(failure_detail(4, "", ""), "exit code 4");
        assert!(!failure_detail(5, "", "").contains("no details"));
    }
    #[test]
    fn immich_is_started_again_once_a_drive_is_back() {
        // The weekly Immich pause leaves a journal when the HDD dropped mid-run; the
        // resume it names is run here with the others (QA-01).
        assert!(RESUMES.contains(&"nekoserver-immich-resume.service"));
        assert!(include_str!("../../modules/photo_preservation.py")
            .contains("RESUME = 'nekoserver-immich-resume'"));
    }
    #[test]
    fn a_live_btrfs_drive_is_not_taken_for_a_dead_one() {
        let here = |p: &Path| p == Path::new("/dev/sdb1") || p == Path::new("/sys/dev/block/8:17");
        // Btrfs: a made-up device number; the drive in the source decides.
        assert!(alive("0:47", "/dev/sdb1[/@data]", here));
        assert!(alive("0:47", "/dev/sdb1", here));
        assert!(!alive("0:47", "/dev/sdc1[/@data]", here));
        // Something else mounted there (no drive at all) still counts as dead.
        assert!(!alive("0:47", "tmpfs", here));
        // ext4 and XFS: the drive's own device number, as before.
        assert!(alive("8:17", "/dev/sdb1", here));
        assert!(!alive("8:33", "/dev/sdb1", here));
    }
    #[test]
    fn a_mounted_shared_folder_is_never_released_and_an_automount_is_no_mount() {
        // 0.50.0: a folder the NAS shares over NFS has no device that could go away.
        let nothing = |_: &Path| false;
        assert!(alive("0:51", "192.168.1.20:/volume1/media", nothing));
        assert!(alive("0:51", "nas.home:/export/media", nothing));
        assert!(!alive("0:51", "//nas/share", nothing)); // SMB: never a NekoServer drive
        assert!(!alive("0:51", "systemd-1", nothing));
        // Before its first use only systemd's automount holds the folder: nothing is
        // mounted yet, so it is mounted once the NAS answers (Remount, nothing released).
        let waiting = Some(("0:50".to_string(), "systemd-1".to_string()));
        assert_eq!(only_automount(waiting), None);
        let there = Some((
            "0:51".to_string(),
            "192.168.1.20:/volume1/media".to_string(),
        ));
        assert_eq!(only_automount(there.clone()), there);
        assert_eq!(decide(None, false, true), Step::Remount { release: false });
        assert_eq!(decide(None, false, false), Step::Wait);
        assert_eq!(decide(Some("0:51"), true, false), Step::Nothing);
    }
    #[test]
    fn a_btrfs_pool_missing_a_drive_is_told_apart_from_a_whole_one() {
        let whole = "Label: none  uuid: 6d1e-aaaa\n\tTotal devices 2 FS bytes used 1.00GiB\n\
                     \tdevid    1 size 50.00GiB used 3.00GiB path /dev/sdb1\n\
                     \tdevid    2 size 50.00GiB used 3.00GiB path /dev/sdc1\n";
        assert!(!pool_lacks_a_drive(whole));
        let short = "Label: none  uuid: 6d1e-aaaa\n\tTotal devices 2 FS bytes used 1.00GiB\n\
                     \tdevid    1 size 50.00GiB used 3.00GiB path /dev/sdb1\n\
                     \t*** Some devices missing\n";
        assert!(pool_lacks_a_drive(short));
        // A pool the tool cannot find at all (nothing said) is not mounted degraded.
        assert!(!pool_lacks_a_drive(""));
        let said = degraded_notice("/mnt/nekoserver-media");
        assert!(said.contains("mounted on the drives left (degraded)"));
        assert!(said.contains("one copy fewer"));
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
