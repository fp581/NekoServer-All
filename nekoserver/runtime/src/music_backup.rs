//! Incremental, immutable music objects plus a verified inventory per successful run.
use crate::{
    backup,
    common::{self, Error, Result},
    Args,
};
use serde_json::json;
use std::{
    fs,
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
};
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
fn listed_again(
    sources: &[(String, PathBuf)],
    waited: &[String],
) -> Result<Vec<serde_json::Value>> {
    let (mut after, _waiting) = listing(sources)?;
    after.retain(|r| !waited.iter().any(|w| r["path"] == w.as_str()));
    Ok(after)
}
/// Where the person's own music folders are listed in a snapshot (G7, 0.50.0): under
/// this folder and the folder's name, apart from the library's own files.
const OWN: &str = "your-own";
/// What is copied: the library (no prefix), then each of the person's own music folders
/// on a network share that they asked to back up ("music_extra" in the jobs' settings:
/// [{"slot": "music", "path": <where setup mounts it>}]), each under OWN/<slot>.
fn sources(cfg: &serde_json::Value, library: PathBuf) -> Result<Vec<(String, PathBuf)>> {
    let mut found = vec![(String::new(), library)];
    for extra in cfg["music_extra"].as_array().into_iter().flatten() {
        let slot = extra["slot"].as_str().unwrap_or("");
        let path = common::safe(Path::new(extra["path"].as_str().unwrap_or("")))?;
        // Only a folder setup mounts under library/existing (existing_media.mount_point).
        let under_existing = path
            .parent()
            .is_some_and(|p| p.ends_with("library/existing"))
            && path.file_name().and_then(|n| n.to_str()) == Some(slot);
        if !(slot == "music" || slot == "music-new") || !under_existing {
            return Err(Error::new("Invalid music folder to back up"));
        }
        found.push((format!("{OWN}/{slot}/"), path));
    }
    Ok(found)
}
/// Each of the person's own music folders is there now (`there`: mounted where setup puts
/// it, and answering a listing within a time limit). With its share away the folder is the
/// empty one left in its place: backing that up would save a "successful" inventory
/// without their music, so the run stops instead and the last good one stays the newest.
fn own_folders_there(sources: &[(String, PathBuf)], there: &dyn Fn(&Path) -> bool) -> Result<()> {
    for (_, path) in sources.iter().filter(|(prefix, _)| !prefix.is_empty()) {
        if !there(path) {
            return Err(Error::new(&format!(
                "Your own music folder (shown at {}) did not answer: its network share is off or not connected. The music backup waits for it; the last backup stays the newest one",
                path.display()
            )));
        }
    }
    Ok(())
}
/// A folder setup mounts is mounted (findmnt names it), and can be listed within 30
/// seconds: a share that stopped answering never holds the run up for hours.
fn mounted_and_answering(path: &Path) -> bool {
    let mounted = matches!(
        common::findmnt("TARGET", "--mountpoint", path, 10),
        Ok(Some(_))
    );
    let shown = path.to_string_lossy();
    mounted && common::command(&["ls", "-1", "--", &shown], 30).is_ok_and(|(code, _)| code == 0)
}
/// Every source listed: (rows sorted by path, files still being added).
fn listing(sources: &[(String, PathBuf)]) -> Result<(Vec<serde_json::Value>, Vec<String>)> {
    let (mut rows, mut waiting) = (vec![], vec![]);
    for (prefix, root) in sources {
        let (mut found, mut later) = (vec![], vec![]);
        inventory(root, root, &mut found, &mut later)?;
        for mut row in found {
            let name = row["path"].as_str().unwrap_or("").to_string();
            if prefix.is_empty() && name.starts_with(&format!("{OWN}/")) {
                return Err(Error::new(
                    "A music folder named your-own is in the library",
                ));
            }
            row["path"] = json!(format!("{prefix}{name}"));
            rows.push(row);
        }
        waiting.extend(later.into_iter().map(|name| format!("{prefix}{name}")));
    }
    rows.sort_by(|a, b| a["path"].as_str().cmp(&b["path"].as_str()));
    Ok((rows, waiting))
}
/// The file a row names, in the source it was listed from.
fn file_of(sources: &[(String, PathBuf)], path: &str) -> Result<PathBuf> {
    let (prefix, root) = sources
        .iter()
        .rev()
        .find(|(prefix, _)| !prefix.is_empty() && path.starts_with(prefix.as_str()))
        .or_else(|| sources.first())
        .ok_or_else(|| Error::new("Music source missing"))?;
    Ok(root.join(&path[prefix.len()..]))
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
        // Where setup put the music (the SSD or the media drive, "locations").
        let cfg = common::json(&config, 64000);
        let source = common::safe(&common::music_library(&cfg)?)?;
        let from = sources(&cfg, source)?;
        own_folders_there(&from, &mounted_and_answering)?;
        let root = common::safe(&media.join("backups/music"))?;
        let (rows, waiting) = listing(&from)?;
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
            backup::verified_copy(&file_of(&from, path)?, &target, true, 0o600)?;
            if common::digest(&target)? != hash
                || Some(fs::metadata(&target)?.len()) != row["size"].as_u64()
            {
                return Err(Error::new("Music changed or backup verification failed"));
            }
        }
        // Refuse a partial catalogue if a download/import changed the source during copying.
        if listed_again(&from, &waiting)? != rows {
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
    fn the_persons_own_music_folder_is_listed_apart_and_copied_from_where_it_is() {
        let top = std::env::temp_dir().join(format!("neko-music-own-{}", std::process::id()));
        let _ = fs::remove_dir_all(&top);
        let library = top.join("library/music");
        let own = top.join("library/existing/music");
        fs::create_dir_all(library.join("Album")).unwrap();
        fs::create_dir_all(own.join("Mine")).unwrap();
        let old = std::time::SystemTime::now() - std::time::Duration::from_secs(86400);
        for path in [library.join("Album/a.flac"), own.join("Mine/b.flac")] {
            fs::write(&path, b"x").unwrap();
            fs::File::options()
                .write(true)
                .open(&path)
                .unwrap()
                .set_modified(old)
                .unwrap();
        }
        let cfg = json!({"music_extra": [{"slot": "music", "path": own}]});
        let from = sources(&cfg, library.clone()).unwrap();
        let (rows, waiting) = listing(&from).unwrap();
        let names: Vec<_> = rows
            .iter()
            .map(|r| r["path"].as_str().unwrap().to_string())
            .collect();
        assert_eq!(names, ["Album/a.flac", "your-own/music/Mine/b.flac"]);
        assert!(waiting.is_empty());
        assert_eq!(
            file_of(&from, "your-own/music/Mine/b.flac").unwrap(),
            own.join("Mine/b.flac")
        );
        assert_eq!(
            file_of(&from, "Album/a.flac").unwrap(),
            library.join("Album/a.flac")
        );
        // Only a folder under library/existing, named as its slot.
        let elsewhere =
            json!({"music_extra": [{"slot": "music", "path": top.join("library/music")}]});
        assert!(sources(&elsewhere, library.clone()).is_err());
        // With its share away, the run stops before anything is listed or saved.
        assert!(own_folders_there(&from, &|_| true).is_ok());
        let said = own_folders_there(&from, &|path| path != own.as_path())
            .unwrap_err()
            .to_string();
        assert!(said.contains("Your own music folder"), "{said}");
        assert!(
            said.contains("the last backup stays the newest one"),
            "{said}"
        );
        // The library alone needs no share: never checked.
        let alone = sources(&json!({}), library.clone()).unwrap();
        assert!(own_folders_there(&alone, &|_| false).is_ok());
        let _ = fs::remove_dir_all(&top);
    }
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
        assert_eq!(
            listed_again(&[(String::new(), root.clone())], &waiting).unwrap(),
            rows
        );
        // A file that was backed up and then changes during the run is a change.
        set("Album/old.flac", now);
        assert_ne!(
            listed_again(&[(String::new(), root.clone())], &waiting).unwrap(),
            rows
        );
        fs::remove_dir_all(root).unwrap();
    }
}
