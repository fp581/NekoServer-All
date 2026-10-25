//! Which files were live at each weekly add-only copy. The add-only copies keep every
//! file ever seen (a deleted or renamed file stays), so a restore of everything brings
//! back what you had removed. Each run that finishes its copy writes the live set as
//! `.neko-inventory/<UTC>.json.gz` at the root of the backup tree: each file's path in
//! the tree (under its own name), the saved copy that held its content then, and that
//! content's SHA-256. The checksum is what a restore goes by: a saved copy can move
//! later (a file that became a folder is kept aside in neko-replaced, backup.rs
//! move_aside) and a new file can take its old place, so the name alone may point at
//! newer content. The
//! recovery menu (recovery_wizard.py) restores "as of the last backup" from it. A
//! missing or damaged inventory only means the restore offers everything, as before.
use crate::common::{self, Error, Result};
use crate::ledger::Ledger;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

/// Folder at a backup tree's root; never data (backup.rs tree_files leaves it out).
pub(crate) const FOLDER: &str = ".neko-inventory";
/// v2 (0.39.7) adds each copy's SHA-256; v1 lists (0.39.6) named the copy only.
const FORMAT: &str = "neko-inventory-v2";
/// Inventories kept per tree (half a year of weekly runs); older ones go.
pub(crate) const KEPT: usize = 26;

/// `<UTC stamp>.json.gz` (`<UTC stamp>-<n>.json.gz` for a second one in the same
/// second), the name of one inventory.
pub(crate) fn inventory_name(name: &str) -> bool {
    order(name).is_some()
}
/// The age order of an inventory's name: its stamp, then its number.
fn order(name: &str) -> Option<(String, u64)> {
    let found = regex::Regex::new(r"^([0-9]{8}T[0-9]{6}Z)(?:-([0-9]{1,9}))?\.json\.gz$")
        .unwrap()
        .captures(name)?;
    let n = found.get(2).map_or(Some(0), |m| m.as_str().parse().ok())?;
    Some((found[1].to_string(), n))
}
pub(crate) fn oldest_first(names: &mut [String]) {
    names.sort_by_key(|n| order(n));
}

#[derive(Default)]
pub(crate) struct Inventory {
    files: BTreeMap<String, String>,
}
impl Inventory {
    /// Note that the live file backed up at `target` (its own name in the tree under
    /// `root`) is current. Nothing is noted when the tree has no copy of it yet.
    pub(crate) fn add(&mut self, target: &Path, root: &Path) {
        let Some(key) = target
            .strip_prefix(root)
            .ok()
            .and_then(|p| p.to_str())
            .filter(|k| crate::ledger::valid_path(k))
        else {
            return;
        };
        if let Some(copy) = current_copy(target) {
            self.files.insert(key.to_string(), copy);
        }
    }
    pub(crate) fn len(&self) -> usize {
        self.files.len()
    }
}

/// The saved copy (in the same folder) that holds a file's content as the backup last
/// saw it: the one its current-version note names, else the file's own first copy.
fn current_copy(target: &Path) -> Option<String> {
    let folder = target.parent()?;
    let noted = fs::read(crate::backup::latest_marker(target))
        .ok()
        .and_then(|b| String::from_utf8(b).ok())
        .map(|s| s.trim().to_string())
        .filter(|n| !n.is_empty() && !n.contains('/') && n != "." && n != "..")
        .filter(|n| fs::symlink_metadata(folder.join(n)).is_ok_and(|m| m.is_file()));
    noted.or_else(|| {
        fs::symlink_metadata(target)
            .is_ok_and(|m| m.is_file())
            .then(|| target.file_name()?.to_str().map(str::to_string))?
    })
}

/// The inventory as gzip-compressed JSON (by the system's gzip, which every supported
/// Linux has).
fn compressed(value: &Value) -> Result<Vec<u8>> {
    let text = serde_json::to_vec(value)?;
    let mut child = Command::new("gzip")
        .args(["-n", "-c"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()?;
    let mut stdin = child
        .stdin
        .take()
        .ok_or_else(|| Error::new("gzip did not start"))?;
    // Written from its own thread: gzip's output is read meanwhile, so a big list never
    // fills both pipes.
    let writer = std::thread::spawn(move || stdin.write_all(&text));
    let output = child.wait_with_output()?;
    writer
        .join()
        .map_err(|_| Error::new("gzip input failed"))??;
    if !output.status.success() || output.stdout.is_empty() {
        return Err(Error::new("gzip failed"));
    }
    Ok(output.stdout)
}

/// Each listed file with its saved copy and that copy's SHA-256: the one the checksum
/// list (`ledger`) records for it, else read from the copy now. A copy that can be read
/// neither way is left out (a restore then names it missing, never guesses).
fn with_checksums(root: &Path, inventory: &Inventory, ledger: &Ledger) -> BTreeMap<String, Value> {
    let mut out = BTreeMap::new();
    for (key, copy) in &inventory.files {
        let saved = match key.rsplit_once('/') {
            Some((folder, _)) => format!("{folder}/{copy}"),
            None => copy.clone(),
        };
        let sum = ledger
            .get(&saved)
            .map(|e| e.sha256.clone())
            .or_else(|| common::digest(&root.join(&saved)).ok());
        if let Some(sha256) = sum {
            out.insert(key.clone(), json!({"copy": copy, "sha256": sha256}));
        }
    }
    out
}

/// The name for a new inventory in `folder`, made at `now` (a UTC stamp): it always sorts
/// after every inventory already there, since the restore takes the last one as "as of
/// the last backup". A clock that was set back (a flat clock battery corrected later,
/// say) would otherwise give the newest list an older name than the one before it; then
/// it takes the newest name's stamp with the next number instead.
fn next_name(folder: &Path, now: &str) -> Result<PathBuf> {
    let newest = fs::read_dir(common::safe(folder)?)?
        .flatten()
        .filter_map(|e| order(&e.file_name().to_string_lossy()))
        .max();
    let (stamp, mut n) = match newest {
        Some((stamp, n)) if stamp.as_str() >= now => (stamp, n + 1),
        _ => (now.to_string(), 0),
    };
    loop {
        let name = if n == 0 {
            format!("{stamp}.json.gz")
        } else {
            format!("{stamp}-{n}.json.gz")
        };
        let path = folder.join(name);
        if fs::symlink_metadata(&path).is_err() {
            return Ok(path);
        }
        n += 1;
    }
}

/// Write `inventory` for the tree at `root`, then keep the newest KEPT. Returns its path.
pub(crate) fn write(root: &Path, inventory: &Inventory, ledger: &Ledger) -> Result<PathBuf> {
    let folder = root.join(FOLDER);
    common::directory(&folder, 0o700)?;
    let now = chrono::DateTime::from_timestamp(common::now() as i64, 0)
        .map(|d| d.format("%Y%m%dT%H%M%SZ").to_string())
        .unwrap_or_default();
    let path = next_name(&folder, &now)?;
    let files = with_checksums(root, inventory, ledger);
    let value = json!({"format": FORMAT, "made_at": now, "files": files});
    common::atomic(&path, &compressed(&value)?, 0o600)?;
    let name = path.file_name().unwrap_or_default().to_string_lossy();
    prune(&folder, KEPT, &name)?;
    Ok(path)
}

/// Keep the newest `keep` inventories in `folder`; `newest` (the one just written) is
/// never removed, even when a wrong clock made another name sort after it.
pub(crate) fn prune(folder: &Path, keep: usize, newest: &str) -> Result<()> {
    let mut found: Vec<String> = fs::read_dir(common::safe(folder)?)?
        .flatten()
        .filter(|e| e.file_type().is_ok_and(|t| t.is_file()))
        .map(|e| e.file_name().to_string_lossy().to_string())
        .filter(|n| inventory_name(n) && n != newest)
        .collect();
    oldest_first(&mut found);
    let aside = usize::from(folder.join(newest).is_file());
    let excess = (found.len() + aside).saturating_sub(keep.max(1));
    for name in found.into_iter().take(excess) {
        fs::remove_file(folder.join(name))?;
    }
    Ok(())
}

/// Bring the inventories of the HDD photo archive onto its USB copy (the files they name
/// are mirrored under the same paths), keeping the newest KEPT there too.
pub(crate) fn mirror(source: &Path, dest: &Path) -> Result<()> {
    let from = source.join(FOLDER);
    if !from.is_dir() {
        return Ok(());
    }
    let mut names: Vec<String> = fs::read_dir(common::safe(&from)?)?
        .flatten()
        .filter(|e| e.file_type().is_ok_and(|t| t.is_file()))
        .map(|e| e.file_name().to_string_lossy().to_string())
        .filter(|n| inventory_name(n))
        .collect();
    oldest_first(&mut names);
    let Some(newest) = names.last().cloned() else {
        return Ok(());
    };
    let to = dest.join(FOLDER);
    common::directory(&to, 0o700)?;
    for name in names.iter().rev().take(KEPT) {
        let target = to.join(name);
        if fs::symlink_metadata(&target).is_err() {
            crate::backup::verified_copy(&from.join(name), &target, true, 0o600)?;
        }
    }
    prune(&to, KEPT, &newest)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn inventory_names_are_dated() {
        assert!(inventory_name("20261025T030000Z.json.gz"));
        assert!(inventory_name("20261025T030000Z-1.json.gz"));
        assert!(!inventory_name("notes.json.gz"));
        assert!(!inventory_name("20261025T030000Z.json"));
        let mut names: Vec<String> = [
            "20261025T030000Z-1.json.gz",
            "20261101T030000Z.json.gz",
            "20261025T030000Z.json.gz",
        ]
        .map(String::from)
        .to_vec();
        oldest_first(&mut names);
        assert_eq!(
            names,
            [
                "20261025T030000Z.json.gz",
                "20261025T030000Z-1.json.gz",
                "20261101T030000Z.json.gz"
            ]
        );
    }
    #[test]
    fn a_new_inventory_sorts_last_even_after_the_clock_was_set_back() {
        let folder =
            std::env::temp_dir().join(format!("neko-inventory-names-{}", std::process::id()));
        let _ = fs::remove_dir_all(&folder);
        fs::create_dir_all(&folder).unwrap();
        let name = |now: &str| {
            let path = next_name(&folder, now).unwrap();
            fs::write(&path, b"").unwrap();
            path.file_name().unwrap().to_string_lossy().to_string()
        };
        assert_eq!(name("20261025T030000Z"), "20261025T030000Z.json.gz");
        assert_eq!(name("20261025T030000Z"), "20261025T030000Z-1.json.gz");
        // The clock went back a week: the next list still comes after the newest one.
        assert_eq!(name("20261018T030000Z"), "20261025T030000Z-2.json.gz");
        assert_eq!(name("20261101T030000Z"), "20261101T030000Z.json.gz");
        let mut names: Vec<String> = fs::read_dir(&folder)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().to_string())
            .collect();
        oldest_first(&mut names);
        assert_eq!(names.last().unwrap(), "20261101T030000Z.json.gz");
        assert_eq!(names[2], "20261025T030000Z-2.json.gz");
        fs::remove_dir_all(folder).unwrap();
    }
}
