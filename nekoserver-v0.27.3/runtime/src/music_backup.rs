//! Incremental, immutable music objects plus a verified inventory per successful run.
use crate::{
    backup,
    common::{self, Error, Result},
    Args,
};
use serde_json::json;
use std::{fs, os::unix::fs::MetadataExt, path::Path};
fn inventory(root: &Path, current: &Path, rows: &mut Vec<serde_json::Value>) -> Result<()> {
    common::safe(current)?;
    for entry in fs::read_dir(current)? {
        let path = entry?.path();
        common::safe(&path)?;
        let meta = fs::symlink_metadata(&path)?;
        if meta.is_dir() {
            inventory(root, &path, rows)?;
        } else if meta.is_file() {
            if common::now() - (meta.mtime() as f64) < 600.0 {
                return Err(Error::new("Music file still settling; retry next backup"));
            }
            let rel = path
                .strip_prefix(root)
                .map_err(|_| Error::new("Invalid music path"))?;
            let name = rel
                .to_str()
                .ok_or_else(|| Error::new("Music path must be UTF-8"))?;
            rows.push(json!({"path":name,"size":meta.len(),"sha256":common::digest(&path)?}));
        } else {
            return Err(Error::new("Music symlink or special file refused"));
        }
    }
    Ok(())
}
pub fn run(a: &Args) -> Result<()> {
    common::require_root()?;
    let config = a.path("config")?;
    let cfg = common::json(&config, 64000);
    let data = common::mount_path(cfg["data_mount"].as_str().unwrap_or(""))?;
    let media = common::mount_path(cfg["media_mount"].as_str().unwrap_or(""))?;
    let _lock = backup::coordinate(&config, &data, &media)?;
    backup::execute(Some(&data), "music", &config, || {
        let source = common::safe(&data.join("library/music"))?;
        let root = common::safe(&media.join("backups/music"))?;
        let mut rows = vec![];
        inventory(&source, &source, &mut rows)?;
        rows.sort_by(|a, b| a["path"].as_str().cmp(&b["path"].as_str()));
        common::directory(&root, 0o700)?;
        for row in &rows {
            backup::guard(&config, &data, &media)?;
            let hash = row["sha256"]
                .as_str()
                .ok_or_else(|| Error::new("Hash missing"))?;
            let target = common::safe(&root.join("objects").join(&hash[..2]).join(hash))?;
            backup::verified_copy(
                &source.join(row["path"].as_str().unwrap()),
                &target,
                true,
                0o600,
            )?;
            if common::digest(&target)? != hash
                || fs::metadata(&target)?.len() != row["size"].as_u64().unwrap()
            {
                return Err(Error::new("Music changed or backup verification failed"));
            }
        }
        // Refuse a partial catalogue if a download/import changed the source during copying.
        let mut after = vec![];
        inventory(&source, &source, &mut after)?;
        after.sort_by(|a, b| a["path"].as_str().cmp(&b["path"].as_str()));
        if after != rows {
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
            &json!({"format":"neko-music-backup-v1","created_at":common::now(),"files":rows}),
        )?;
        Ok(())
    })
}
