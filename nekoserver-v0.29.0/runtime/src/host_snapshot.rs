//! Private host/user snapshots. Links are stored as inert text, never followed.
use crate::{
    app_snapshot, backup,
    common::{self, Error, Result},
    Args, VERSION,
};
use serde_json::{json, Value};
use std::{
    fs,
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
};
pub(crate) const JOURNAL: &str = "/etc/nekoserver-backups/host-resume.json";
const LOCK: &str = "/run/nekoserver-host-snapshot.lock";
fn command(args: &[&str]) -> Result<String> {
    let (code, text) = common::command(args, 90)?;
    if code != 0 {
        return Err(Error::new("Host service operation failed"));
    }
    Ok(text.trim().into())
}
/// Samba's service: `smb.service` on Arch, `smbd.service` on Debian and Ubuntu.
const SAMBA_UNITS: [&str; 2] = ["smb.service", "smbd.service"];
/// The installed Samba unit ("" when Samba is absent) and whether it runs.
fn samba_state() -> Result<(&'static str, String)> {
    let text = command(&[
        "systemctl",
        "show",
        SAMBA_UNITS[0],
        SAMBA_UNITS[1],
        "--property=Id,LoadState,ActiveState",
    ])?;
    parse_samba(&text)
}
fn parse_samba(text: &str) -> Result<(&'static str, String)> {
    for block in text.split("\n\n") {
        let fields = block
            .lines()
            .filter_map(|s| s.split_once('='))
            .collect::<std::collections::BTreeMap<_, _>>();
        let Some(unit) = SAMBA_UNITS.iter().find(|u| fields.get("Id") == Some(u)) else {
            continue;
        };
        if fields.get("LoadState") != Some(&"loaded") {
            continue;
        }
        return match fields.get("ActiveState").copied() {
            Some("active") => Ok((unit, "active".into())),
            Some("inactive" | "failed") => Ok((unit, "inactive".into())),
            _ => Err(Error::new("Samba transitioning or unknown")),
        };
    }
    Ok(("", "absent".into()))
}
fn resume() -> Result<()> {
    let path = Path::new(JOURNAL);
    if !path.exists() {
        return Ok(());
    }
    let row = common::json(path, 64000);
    if row["format"] != "neko-host-resume-v1" || !row["smb_was_active"].is_boolean() {
        return Err(Error::new("Invalid host resume journal"));
    }
    if row["smb_was_active"] == true {
        let (unit, _) = samba_state()?;
        if unit.is_empty() {
            return Err(Error::new("Samba restart not verified"));
        }
        command(&["systemctl", "start", unit])?;
        if samba_state()?.1 != "active" {
            return Err(Error::new("Samba restart not verified"));
        }
    }
    backup::finish_journal(path)
}
pub fn resume_command(a: &Args) -> Result<()> {
    let job = backup::open_job(a, 128000)?;
    let _lock = common::lock(Path::new(LOCK))?;
    let _recovery = backup::coordinate(&job.cfgpath, &job.data, &job.media)?;
    resume()?;
    backup::mark_interrupted(&job.data.join("reports/jobs/host.json"))
}
fn probe_absent(args: &[&str]) -> Result<()> {
    let (code, found) = common::command(args, 15)?;
    if code != 1 {
        // The name of what is running, so the reason can be seen (Debian's samba package
        // starts winbind, say); pgrep -a would give the command line, pgrep alone the pid.
        let names = found
            .split_whitespace()
            .filter(|w| w.parse::<u32>().is_err())
            .collect::<Vec<_>>()
            .join(" ");
        return Err(Error::new(&format!(
            "A Samba program this setup does not manage is running ({}): the settings backup only works beside NekoServer's own standalone Samba. Stop and disable it (sudo systemctl disable --now winbind nmbd), then run setup again",
            if names.is_empty() { args.last().copied().unwrap_or("?") } else { &names }
        )));
    }
    Ok(())
}
/// Steam (or a game it started through Wine) is running for the Steam user. Its files
/// change while it runs, so they are only copied while it is closed.
fn steam_open(cfg: &Value) -> Result<bool> {
    let Some(uid) = cfg["host_backup"]["steam_uid"].as_u64() else {
        return Ok(false);
    };
    if !(1000..=u32::MAX as u64 - 1).contains(&uid) {
        return Err(Error::new("Invalid Steam user"));
    }
    let (code, _) = common::command(
        &[
            "pgrep",
            "-u",
            &uid.to_string(),
            "-f",
            "(^|/)(steam|steamwebhelper|wineserver|wine64|wine)( |$)",
        ],
        15,
    )?;
    match code {
        0 => Ok(true),
        1 => Ok(false),
        _ => Err(Error::new("Process probe failed")),
    }
}
/// Steam's files are in this snapshot: Steam must still be closed.
fn steam_still_closed(cfg: &Value, steam_included: bool) -> Result<()> {
    if steam_included && steam_open(cfg)? {
        return Err(Error::new("Steam was started during the snapshot"));
    }
    Ok(())
}
/// The newest snapshot that left nothing out (so it holds Steam's files): kept while
/// later runs, made with Steam open, don't have them.
fn newest_complete(root: &Path) -> Option<String> {
    let pattern = regex::Regex::new(r"^snapshot-([0-9]+)-([0-9]+)$").ok()?;
    let mut found: Vec<((u128, u128), String)> = fs::read_dir(root)
        .ok()?
        .flatten()
        .filter_map(|entry| {
            let name = entry.file_name().to_string_lossy().to_string();
            let parts = pattern.captures(&name)?;
            let order: (u128, u128) =
                (parts[1].parse().unwrap_or(0), parts[2].parse().unwrap_or(0));
            let manifest = common::json(&entry.path().join("manifest.json"), 64_000_000);
            let complete = manifest["format"] == "neko-host-snapshot-v1"
                && manifest["left_out"].as_array().is_none_or(Vec::is_empty);
            complete.then(|| (order, name.clone()))
        })
        .collect();
    found.sort();
    found.pop().map(|(_, name)| name)
}
/// What the dashboard and the job report say when Steam's files were left out.
const STEAM_LEFT_OUT: &str = "Steam was open, so its settings and saves were not backed up this time; everything else was. They are backed up on a night Steam is closed (it is never closed for you)";
/// The sources to copy now: Steam's are left out while Steam is open (never closed for
/// you: the PC may be streaming a game), so Samba and the other settings are still saved.
fn copied_now(selected: &[Source], open: bool) -> (Vec<Source>, Vec<String>) {
    let left_out = selected
        .iter()
        .filter(|s| open && s.3)
        .map(|s| s.0.clone())
        .collect();
    let now = selected
        .iter()
        .filter(|s| !(open && s.3))
        .cloned()
        .collect();
    (now, left_out)
}
/// Sockets, FIFOs, devices and nested filesystems are skipped and listed rather than
/// failing the backup; hard-linked files are copied as independent files.
fn walk(
    source: &Path,
    relative: &Path,
    device: u64,
    out: &mut Vec<Value>,
    skipped: &mut Vec<Value>,
) -> Result<()> {
    common::safe(
        source
            .parent()
            .ok_or_else(|| Error::new("Invalid source"))?,
    )?;
    let m = fs::symlink_metadata(source)?;
    let reason = if m.dev() != device && !m.file_type().is_symlink() {
        Some("nested filesystem")
    } else if !(m.is_dir() || m.is_file() || m.file_type().is_symlink()) {
        Some("special file")
    } else {
        None
    };
    if let Some(reason) = reason {
        if relative.components().count() <= 1 {
            return Err(Error::new(
                "Backup source is not a regular directory or file",
            ));
        }
        skipped.push(json!({"path":relative.to_str().unwrap_or("?"),"reason":reason}));
        return Ok(());
    }
    if m.is_file() && m.mode() & 0o6000 != 0 {
        return Err(Error::new("Privileged file metadata refused"));
    }
    let kind = if m.is_dir() {
        "directory"
    } else if m.is_file() {
        "file"
    } else {
        "symlink"
    };
    let mut row = json!({"path":relative.to_str().ok_or_else(||Error::new("Non-UTF8 path"))?,"kind":kind,"mode":m.mode()&0o7777,"uid":m.uid(),"gid":m.gid(),"size":if m.is_file(){m.len()}else{0},"mtime":m.mtime(),"mtime_nsec":m.mtime_nsec()});
    if kind == "symlink" {
        row["link_target"] = json!(fs::read_link(source)?
            .to_str()
            .ok_or_else(|| Error::new("Non-UTF8 link"))?);
    }
    out.push(row);
    if out.len() > 200000 {
        return Err(Error::new("Host inventory limit exceeded"));
    }
    if m.is_dir() {
        let mut children = fs::read_dir(source)?.collect::<std::io::Result<Vec<_>>>()?;
        children.sort_by_key(|v| v.file_name());
        for child in children {
            walk(
                &child.path(),
                &relative.join(child.file_name()),
                device,
                out,
                skipped,
            )?;
        }
    }
    Ok(())
}
/// (id, path, optional, Steam's own)
type Source = (String, PathBuf, bool, bool);
fn sources(cfg: &Value, media: &Path) -> Result<Vec<Source>> {
    let rows = cfg["host_backup"]["sources"]
        .as_array()
        .ok_or_else(|| Error::new("Sources required"))?;
    if rows.is_empty() || rows.len() > 100 {
        return Err(Error::new("Invalid source count"));
    }
    let mut out = vec![];
    for row in rows {
        let id = row["id"].as_str().unwrap_or("");
        if id.is_empty()
            || !id.bytes().all(|b| b.is_ascii_alphanumeric())
            || out.iter().any(|s: &Source| s.0 == id)
        {
            return Err(Error::new("Invalid source ID"));
        }
        // The source itself may be a link (Debian's /etc/vconsole.conf and
        // /etc/default/locale are): it is kept as a link, like a link inside a source.
        // Its parents must not be links, and the path must not climb.
        let source = Path::new(row["path"].as_str().unwrap_or("")).to_path_buf();
        common::safe(
            source
                .parent()
                .ok_or_else(|| Error::new("Invalid source"))?,
        )?;
        if source
            .components()
            .any(|c| matches!(c, std::path::Component::ParentDir))
        {
            return Err(Error::new("Parent traversal refused"));
        }
        let backup = media.join("backups");
        if !source.is_absolute()
            || source == Path::new("/")
            || source.starts_with(&backup)
            || backup.starts_with(&source)
        {
            return Err(Error::new("Recursive or invalid source"));
        }
        let optional = row["optional"]
            .as_bool()
            .ok_or_else(|| Error::new("Invalid source policy"))?;
        let steam = row["steam"].as_bool().unwrap_or(false);
        out.push((id.to_string(), source, optional, steam));
    }
    Ok(out)
}
type Inventory = (Vec<Value>, Vec<String>, Vec<Value>);
fn inventory(selected: &[Source]) -> Result<Inventory> {
    let mut files = vec![];
    let mut missing = vec![];
    let mut skipped = vec![];
    for (id, source, optional, _) in selected {
        match fs::symlink_metadata(source) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound && *optional => {
                missing.push(id.clone());
                continue;
            }
            Err(e) => return Err(e.into()),
            Ok(m) => walk(source, Path::new(id), m.dev(), &mut files, &mut skipped)?,
        }
    }
    if files.is_empty() {
        return Err(Error::new("No host files to preserve"));
    }
    Ok((files, missing, skipped))
}
/// The settings backup's message when the HDD is too full: the drive, what is free and
/// what is needed, in the GB of the `host_backup.minimum_free_gb` setting.
fn short_of_space(media: &Path, have: u128, needed: u128, reserve: u64) -> String {
    let gb = |b: u128| {
        let g = b as f64 / (1u64 << 30) as f64;
        if g < 10.0 {
            format!("{g:.1}")
        } else {
            format!("{g:.0}")
        }
    };
    format!(
        "Not enough space on the HDD ({}) for the nightly settings backup: {} GB free, about {} GB needed (the settings files plus the {} GB reserve from host_backup.minimum_free_gb). Free up space there",
        media.display(),
        gb(have),
        gb(needed),
        reserve >> 30
    )
}
pub fn run(a: &Args) -> Result<()> {
    let job = backup::open_job(a, 128000)?;
    let selected = sources(&job.cfg, &job.media)?;
    let _lock = common::lock(Path::new(LOCK))?;
    let _recovery = backup::coordinate(&job.cfgpath, &job.data, &job.media)?;
    resume()?;
    backup::execute_with(Some(&job.data), "host", &job.cfgpath, || {
        snapshot(&job.cfgpath, &job.cfg, &job.data, &job.media, &selected)
    })
}
fn snapshot(
    cfgpath: &Path,
    cfg: &Value,
    data: &Path,
    media: &Path,
    all: &[Source],
) -> Result<backup::Outcome> {
    let (chosen, left_out) = copied_now(all, steam_open(cfg)?);
    let selected = &chosen[..];
    let steam_included = selected.iter().any(|s| s.3);
    let samba = cfg["host_backup"]["samba"]
        .as_bool()
        .ok_or_else(|| Error::new("Samba policy required"))?;
    let mut samba_unit = "";
    let active = if samba {
        // This installer supports a standalone Samba server, not AD/DC or winbind.
        probe_absent(&["pgrep", "-x", "winbindd|nmbd|samba"])?;
        let (unit, state) = samba_state()?;
        samba_unit = unit;
        if state != "absent"
            && common::bytes(Path::new("/etc/samba/smb.conf"), 64000)?
                != b"[global]\n   include = /etc/samba/nekoserver.conf\n"
        {
            return Err(Error::new("Unmanaged Samba refused"));
        }
        if state != "absent" {
            for parameter in ["state directory", "private dir"] {
                let value = command(&["testparm", "-s", &format!("--parameter-name={parameter}")])?;
                let path = common::safe(Path::new(&value))?;
                if !path.starts_with("/etc/samba") && !path.starts_with("/var/lib/samba") {
                    return Err(Error::new("Unsupported Samba database location"));
                }
            }
            if command(&["testparm", "-s", "--parameter-name=passdb backend"])? != "tdbsam" {
                return Err(Error::new("Unsupported Samba authentication backend"));
            }
        }
        state == "active"
    } else {
        false
    };
    common::directory(Path::new("/etc/nekoserver-backups"), 0o700)?;
    common::atomic_json(
        Path::new(JOURNAL),
        &json!({"format":"neko-host-resume-v1","smb_was_active":active}),
    )?;
    let result = (|| {
        if active {
            command(&["systemctl", "stop", samba_unit])?;
        }
        if samba {
            probe_absent(&["pgrep", "-x", "smbd|winbindd|nmbd|samba"])?;
        }
        steam_still_closed(cfg, steam_included)?;
        backup::guard(cfgpath, data, media)?;
        let (mut files, missing, skipped) = inventory(selected)?;
        let root = media.join("backups/host-user");
        common::directory(&root, 0o700)?;
        let m = fs::metadata(&root)?;
        if m.uid() != 0 || m.mode() & 0o077 != 0 {
            return Err(Error::new("Private host backup root required"));
        }
        let total = files.iter().try_fold(0u64, |n, r| {
            n.checked_add(r["size"].as_u64().unwrap_or(0))
                .ok_or_else(|| Error::new("Snapshot too large"))
        })?;
        let reserve = cfg["host_backup"]["minimum_free_bytes"]
            .as_u64()
            .ok_or_else(|| Error::new("Free-space reserve required"))?;
        if !(1024 * 1024 * 1024..=1024 * 1024 * 1024 * 1024).contains(&reserve) {
            return Err(Error::new("Invalid free-space reserve"));
        }
        let c = std::ffi::CString::new(root.as_os_str().as_encoded_bytes())
            .map_err(|_| Error::new("Invalid backup path"))?;
        let mut stat = std::mem::MaybeUninit::<libc::statvfs>::uninit();
        if unsafe { libc::statvfs(c.as_ptr(), stat.as_mut_ptr()) } != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        let stat = unsafe { stat.assume_init() };
        let have = (stat.f_bavail as u128) * (stat.f_frsize as u128);
        if have < total as u128 + reserve as u128 {
            return Err(Error::new(&short_of_space(
                media,
                have,
                total as u128 + reserve as u128,
                reserve,
            )));
        }
        let stage = backup::stage_snapshot(&root)?;
        let payload = &stage.payload;
        for row in &mut files {
            let rel = Path::new(row["path"].as_str().unwrap());
            let (id, source, _, _) = selected.iter().find(|s| rel.starts_with(&s.0)).unwrap();
            let suffix = rel.strip_prefix(id).unwrap();
            let original = if suffix.as_os_str().is_empty() {
                source.clone()
            } else {
                source.join(suffix)
            };
            let target = payload.join(rel);
            common::directory(target.parent().unwrap(), 0o700)?;
            if row["kind"] == "directory" {
                common::directory(&target, 0o700)?;
            } else if row["kind"] == "symlink" {
                common::atomic(
                    &target,
                    row["link_target"].as_str().unwrap().as_bytes(),
                    0o600,
                )?;
                row["size"] = json!(fs::metadata(&target)?.len());
                row["sha256"] = json!(common::digest(&target)?);
            } else {
                row["sha256"] = json!(app_snapshot::copy_file(&original, &target)?);
            }
        }
        let (after, after_missing, _) = inventory(selected)?;
        let mut before = files.clone();
        for row in &mut before {
            row.as_object_mut().unwrap().remove("sha256");
            if row["kind"] == "symlink" {
                row["size"] = json!(0);
            }
        }
        if before != after || missing != after_missing {
            return Err(Error::new("Host inventory changed during snapshot"));
        }
        steam_still_closed(cfg, steam_included)?;
        if samba {
            probe_absent(&["pgrep", "-x", "smbd|winbindd|nmbd|samba"])?;
        }
        let manifest = json!({"format":"neko-host-snapshot-v1","version":VERSION,"created_at":common::now(),"files":files,"sources":cfg["host_backup"]["sources"],"missing_optional":missing,"left_out":left_out,"skipped":skipped,"smb_was_active":active,"links_are_inert":true,"application_consistency":if left_out.is_empty() {"Steam closed; managed Samba stopped; other files checked for changes"} else {"Steam open: its files left out; managed Samba stopped; other files checked for changes"}});
        let keep = cfg["host_backup"]["keep_snapshots"].as_u64().unwrap_or(7);
        // A run that left Steam out never retires the last snapshot that has it.
        let protected = if left_out.is_empty() {
            None
        } else {
            newest_complete(&root)
        };
        backup::publish_snapshot(
            &root,
            &stage,
            &files,
            || backup::guard(cfgpath, data, media),
            &manifest,
            keep,
            protected.as_deref(),
        )
    })();
    let restarted = resume();
    result.and(restarted)?;
    Ok(backup::Outcome {
        partial: (!left_out.is_empty())
            .then(|| json!({"source": STEAM_LEFT_OUT, "left_out": left_out})),
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn a_full_hdd_names_the_drive_and_the_space() {
        let said = short_of_space(
            Path::new("/mnt/nekoserver-media"),
            64 << 20,
            (2 << 30) + (10 << 20),
            2 << 30,
        );
        assert!(said.contains("HDD (/mnt/nekoserver-media)"), "{said}");
        assert!(said.contains("0.1 GB free, about 2.0 GB needed"), "{said}");
        assert!(
            said.contains("2 GB reserve from host_backup.minimum_free_gb"),
            "{said}"
        );
    }
    #[test]
    fn recursion_refused() {
        let cfg =
            json!({"host_backup":{"sources":[{"id":"source0","path":"/media","optional":true}]}});
        assert!(sources(&cfg, Path::new("/media")).is_err());
    }
    #[test]
    fn duplicate_id_refused() {
        let cfg = json!({"host_backup":{"sources":[{"id":"source0","path":"/etc/hosts","optional":true},{"id":"source0","path":"/etc/fstab","optional":true}]}});
        assert!(sources(&cfg, Path::new("/media")).is_err());
    }
    #[test]
    fn samba_unit_on_each_family() {
        let arch = "Id=smb.service\nLoadState=loaded\nActiveState=active\n\nId=smbd.service\nLoadState=not-found\nActiveState=inactive";
        assert_eq!(parse_samba(arch).unwrap(), ("smb.service", "active".into()));
        let debian = "Id=smb.service\nLoadState=not-found\nActiveState=inactive\n\nId=smbd.service\nLoadState=loaded\nActiveState=failed";
        assert_eq!(
            parse_samba(debian).unwrap(),
            ("smbd.service", "inactive".into())
        );
        let none = "Id=smb.service\nLoadState=not-found\nActiveState=inactive\n\nId=smbd.service\nLoadState=not-found\nActiveState=inactive";
        assert_eq!(parse_samba(none).unwrap(), ("", "absent".into()));
        let starting = "Id=smbd.service\nLoadState=loaded\nActiveState=activating";
        assert!(parse_samba(starting).is_err());
    }
    #[test]
    fn steam_files_wait_while_steam_is_open() {
        let all: Vec<Source> = vec![
            ("source0".into(), "/etc/hostname".into(), true, false),
            (
                "source1".into(),
                "/home/u/.local/share/Steam/config".into(),
                true,
                true,
            ),
            ("source2".into(), "/etc/samba".into(), true, false),
        ];
        let (now, left_out) = copied_now(&all, true);
        assert_eq!(
            now.iter().map(|s| s.0.as_str()).collect::<Vec<_>>(),
            ["source0", "source2"]
        );
        assert_eq!(left_out, ["source1"]);
        let (now, left_out) = copied_now(&all, false);
        assert_eq!(now.len(), 3);
        assert!(left_out.is_empty());
        let cfg = json!({"host_backup":{"sources":[{"id":"source0","path":"/etc/hosts","optional":true,"steam":true}]}});
        assert!(sources(&cfg, Path::new("/media")).unwrap()[0].3);
    }
    #[test]
    fn the_last_snapshot_with_steam_outlives_the_ones_without() {
        let root = std::env::temp_dir().join(format!("neko-host-keep-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let snap = |name: &str, left_out: &[&str]| {
            let dir = root.join(name);
            fs::create_dir_all(&dir).unwrap();
            fs::set_permissions(&dir, std::os::unix::fs::PermissionsExt::from_mode(0o700)).unwrap();
            fs::write(
                dir.join("manifest.json"),
                json!({"format":"neko-host-snapshot-v1","left_out":left_out}).to_string(),
            )
            .unwrap();
        };
        fs::create_dir_all(&root).unwrap();
        snap("snapshot-100-1", &[]); // Steam closed: has the saves
        snap("snapshot-200-1", &["source1"]);
        assert_eq!(newest_complete(&root).as_deref(), Some("snapshot-100-1"));
        snap("snapshot-300-1", &["source1"]);
        // Keep 2: 300 (just made) and 200, plus 100 because it is the only one with Steam.
        let removed =
            common::prune_snapshots(&root, 2, Some("snapshot-300-1"), Some("snapshot-100-1"))
                .unwrap();
        assert!(removed.is_empty());
        snap("snapshot-400-1", &["source1"]);
        let removed = common::prune_snapshots(
            &root,
            2,
            Some("snapshot-400-1"),
            newest_complete(&root).as_deref(),
        )
        .unwrap();
        assert_eq!(removed, ["snapshot-200-1"]);
        assert!(root.join("snapshot-100-1").exists());
        // A complete run again: the old one goes as usual.
        snap("snapshot-500-1", &[]);
        let removed = common::prune_snapshots(&root, 2, Some("snapshot-500-1"), None).unwrap();
        assert_eq!(removed, ["snapshot-100-1", "snapshot-300-1"]);
        fs::remove_dir_all(root).unwrap();
    }
    /// Debian's /etc/vconsole.conf and /etc/default/locale are links: a linked source is
    /// kept as a link (its parents must still be real folders, and no path may climb).
    #[test]
    fn a_linked_source_is_kept_as_a_link() {
        let root = std::env::temp_dir().join(format!("neko-host-link-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("etc/default")).unwrap();
        fs::write(
            root.join("etc/default/keyboard"),
            "KEYMAP=us
",
        )
        .unwrap();
        std::os::unix::fs::symlink("default/keyboard", root.join("etc/vconsole.conf")).unwrap();
        let link = root
            .join("etc/vconsole.conf")
            .to_string_lossy()
            .into_owned();
        let cfg = json!({"host_backup":{"sources":[{"id":"source0","path":link,"optional":true}]}});
        let selected = sources(&cfg, Path::new("/media")).unwrap();
        let (files, missing, skipped) = inventory(&selected).unwrap();
        assert!(missing.is_empty() && skipped.is_empty());
        assert_eq!(files[0]["kind"], "symlink");
        assert_eq!(files[0]["link_target"], "default/keyboard");
        // A link higher up the path is still refused.
        std::os::unix::fs::symlink("etc", root.join("conf")).unwrap();
        let through = root
            .join("conf/default/keyboard")
            .to_string_lossy()
            .into_owned();
        let cfg =
            json!({"host_backup":{"sources":[{"id":"source0","path":through,"optional":true}]}});
        assert!(sources(&cfg, Path::new("/media")).is_err());
        let cfg = json!({"host_backup":{"sources":[{"id":"source0","path":"/etc/../etc/hosts","optional":true}]}});
        assert!(sources(&cfg, Path::new("/media")).is_err());
        let _ = fs::remove_dir_all(&root);
    }
    #[test]
    fn root_refused() {
        let cfg = json!({"host_backup":{"sources":[{"id":"source0","path":"/","optional":true}]}});
        assert!(sources(&cfg, Path::new("/media")).is_err());
    }
}
