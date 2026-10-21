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
const JOURNAL: &str = "/etc/nekoserver-backups/host-resume.json";
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
    let (code, _) = common::command(args, 15)?;
    if code != 1 {
        return Err(Error::new("Writer active or process probe failed"));
    }
    Ok(())
}
fn idle(cfg: &Value) -> Result<()> {
    if let Some(uid) = cfg["host_backup"]["steam_uid"].as_u64() {
        if !(1000..=u32::MAX as u64 - 1).contains(&uid) {
            return Err(Error::new("Invalid Steam user"));
        }
        probe_absent(&[
            "pgrep",
            "-u",
            &uid.to_string(),
            "-f",
            "(^|/)(steam|steamwebhelper|wineserver|wine64|wine)( |$)",
        ])?;
    }
    Ok(())
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
fn sources(cfg: &Value, media: &Path) -> Result<Vec<(String, PathBuf, bool)>> {
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
            || out.iter().any(|(s, _, _)| s == id)
        {
            return Err(Error::new("Invalid source ID"));
        }
        let source = common::safe(Path::new(row["path"].as_str().unwrap_or("")))?;
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
        out.push((id.to_string(), source, optional));
    }
    Ok(out)
}
type Inventory = (Vec<Value>, Vec<String>, Vec<Value>);
fn inventory(selected: &[(String, PathBuf, bool)]) -> Result<Inventory> {
    let mut files = vec![];
    let mut missing = vec![];
    let mut skipped = vec![];
    for (id, source, optional) in selected {
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
pub fn run(a: &Args) -> Result<()> {
    let job = backup::open_job(a, 128000)?;
    let selected = sources(&job.cfg, &job.media)?;
    let _lock = common::lock(Path::new(LOCK))?;
    let _recovery = backup::coordinate(&job.cfgpath, &job.data, &job.media)?;
    resume()?;
    backup::execute(Some(&job.data), "host", &job.cfgpath, || {
        snapshot(&job.cfgpath, &job.cfg, &job.data, &job.media, &selected)
    })
}
fn snapshot(
    cfgpath: &Path,
    cfg: &Value,
    data: &Path,
    media: &Path,
    selected: &[(String, PathBuf, bool)],
) -> Result<()> {
    idle(cfg)?;
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
        idle(cfg)?;
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
        if (stat.f_bavail as u128) * (stat.f_frsize as u128) < total as u128 + reserve as u128 {
            return Err(Error::new("Insufficient snapshot space"));
        }
        let stage = backup::stage_snapshot(&root)?;
        let payload = &stage.payload;
        for row in &mut files {
            let rel = Path::new(row["path"].as_str().unwrap());
            let (id, source, _) = selected
                .iter()
                .find(|(id, _, _)| rel.starts_with(id))
                .unwrap();
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
        idle(cfg)?;
        if samba {
            probe_absent(&["pgrep", "-x", "smbd|winbindd|nmbd|samba"])?;
        }
        let manifest = json!({"format":"neko-host-snapshot-v1","version":VERSION,"created_at":common::now(),"files":files,"sources":cfg["host_backup"]["sources"],"missing_optional":missing,"skipped":skipped,"smb_was_active":active,"links_are_inert":true,"application_consistency":"Steam closed; managed Samba stopped; other files checked for changes"});
        let keep = cfg["host_backup"]["keep_snapshots"].as_u64().unwrap_or(14);
        backup::publish_snapshot(
            &root,
            &stage,
            &files,
            || backup::guard(cfgpath, data, media),
            &manifest,
            keep,
        )
    })();
    let restarted = resume();
    result.and(restarted)
}
#[cfg(test)]
mod tests {
    use super::*;
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
    fn root_refused() {
        let cfg = json!({"host_backup":{"sources":[{"id":"source0","path":"/","optional":true}]}});
        assert!(sources(&cfg, Path::new("/media")).is_err());
    }
}
