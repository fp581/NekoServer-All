//! Incremental, immutable music objects plus a verified inventory per successful run.
use crate::{
    backup,
    common::{self, Error, Result},
    Args,
};
use serde_json::json;
use std::{fs, os::unix::fs::MetadataExt, path::Path};
/// A file changed in the last 10 minutes may still be being written (a download or an
/// import): it waits for the next run, and the rest of the library is backed up now.
const SETTLE: f64 = 600.0;
/// The files to back up (`rows`) and, in `waiting`, the ones changed too recently.
fn inventory(
    root: &Path,
    current: &Path,
    rows: &mut Vec<serde_json::Value>,
    waiting: &mut Vec<String>,
) -> Result<()> {
    common::safe(current)?;
    for entry in fs::read_dir(current)? {
        let path = entry?.path();
        common::safe(&path)?;
        let meta = fs::symlink_metadata(&path)?;
        if meta.is_dir() {
            inventory(root, &path, rows, waiting)?;
        } else if meta.is_file() {
            let rel = path
                .strip_prefix(root)
                .map_err(|_| Error::new("Invalid music path"))?;
            let name = rel
                .to_str()
                .ok_or_else(|| Error::new("Music path must be UTF-8"))?;
            // A file dated far ahead (a wrong clock) is backed up: its date may never come.
            let modified = meta.mtime() as f64 + meta.mtime_nsec() as f64 / 1e9;
            if common::settling(modified, SETTLE) {
                waiting.push(name.to_string());
                continue;
            }
            rows.push(json!({"path":name,"size":meta.len(),"sha256":common::digest(&path)?}));
        } else {
            return Err(Error::new("Music symlink or special file refused"));
        }
    }
    Ok(())
}
/// The library listed again after copying, for comparison with the first listing. A
/// file that waited then (and may have settled since) is left out of both; a file
/// changed during the run waits now, so it shows as changed.
fn listed_again(source: &Path, waited: &[String]) -> Result<Vec<serde_json::Value>> {
    let (mut after, mut waiting) = (vec![], vec![]);
    inventory(source, source, &mut after, &mut waiting)?;
    after.retain(|r| !waited.iter().any(|w| r["path"] == w.as_str()));
    after.sort_by(|a, b| a["path"].as_str().cmp(&b["path"].as_str()));
    Ok(after)
}
pub fn run(a: &Args) -> Result<()> {
    let backup::Job {
        cfgpath: config,
        data,
        media,
        ..
    } = backup::open_job(a, 64000)?;
    let _lock = backup::coordinate(&config, &data, &media)?;
    backup::execute(Some(&data), "music", &config, || {
        backup::no_unfinished_update()?;
        // A HDD that drops out mid-run stops it (see common::expect_drive).
        let _hdd = common::expect_drive(&media)?;
        let source = common::safe(&data.join("library/music"))?;
        let root = common::safe(&media.join("backups/music"))?;
        let (mut rows, mut waiting) = (vec![], vec![]);
        inventory(&source, &source, &mut rows, &mut waiting)?;
        rows.sort_by(|a, b| a["path"].as_str().cmp(&b["path"].as_str()));
        common::directory(&root, 0o700)?;
        for row in &rows {
            backup::guard(&config, &data, &media)?;
            let hash = row["sha256"]
                .as_str()
                .ok_or_else(|| Error::new("Hash missing"))?;
            let path = row["path"]
                .as_str()
                .ok_or_else(|| Error::new("Path missing"))?;
            let target = common::safe(&root.join("objects").join(&hash[..2]).join(hash))?;
            backup::verified_copy(&source.join(path), &target, true, 0o600)?;
            if common::digest(&target)? != hash
                || Some(fs::metadata(&target)?.len()) != row["size"].as_u64()
            {
                return Err(Error::new("Music changed or backup verification failed"));
            }
        }
        // Refuse a partial catalogue if a download/import changed the source during copying.
        if listed_again(&source, &waiting)? != rows {
            return Err(Error::new("Music library changed during backup"));
        }
        backup::guard(&config, &data, &media)?;
        let snapshots = root.join("snapshots");
        common::directory(&snapshots, 0o700)?;
        let target = snapshots.join(format!(
            "{}-{}.json",
            (common::now() * 1_000_000.0) as u64,
            std::process::id()
        ));
        if target.exists() {
            return Err(Error::new("Snapshot already exists"));
        }
        common::atomic_json(
            &target,
            &json!({"format":"neko-music-backup-v1","created_at":common::now(),"files":rows,"waiting":waiting}),
        )?;
        if !waiting.is_empty() {
            println!(
                "Music: {} files changed in the last 10 minutes (still being added?) wait for the next backup: {}",
                waiting.len(),
                waiting.iter().take(10).cloned().collect::<Vec<_>>().join(", ")
            );
        }
        Ok(())
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn a_file_still_being_added_waits_instead_of_failing_the_backup() {
        let root = std::env::temp_dir().join(format!("neko-music-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("Album")).unwrap();
        let day = std::time::Duration::from_secs(86400);
        let set = |name: &str, when: std::time::SystemTime| {
            let path = root.join(name);
            fs::write(&path, name).unwrap();
            fs::File::options()
                .write(true)
                .open(&path)
                .unwrap()
                .set_modified(when)
                .unwrap();
        };
        let now = std::time::SystemTime::now();
        set("Album/old.flac", now - day);
        set("Album/new.flac", now);
        // Dated a year ahead by a wrong clock: backed up, not left waiting for a year.
        set("Album/ahead.flac", now + day * 365);
        let (mut rows, mut waiting) = (vec![], vec![]);
        inventory(&root, &root, &mut rows, &mut waiting).unwrap();
        let mut names: Vec<&str> = rows.iter().map(|r| r["path"].as_str().unwrap()).collect();
        names.sort();
        assert_eq!(names, ["Album/ahead.flac", "Album/old.flac"]);
        assert_eq!(waiting, ["Album/new.flac"]);
        rows.sort_by(|a, b| a["path"].as_str().cmp(&b["path"].as_str()));
        // It settles during the run: not counted as a change.
        set("Album/new.flac", now - day);
        assert_eq!(listed_again(&root, &waiting).unwrap(), rows);
        // A file that was backed up and then changes during the run is a change.
        set("Album/old.flac", now);
        assert_ne!(listed_again(&root, &waiting).unwrap(), rows);
        fs::remove_dir_all(root).unwrap();
    }
}
