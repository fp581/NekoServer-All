//! Backup cards (0.52.0): SD cards and USB sticks that a household keeps somewhere else,
//! filled with copies of its photos and documents. The engine that writes a card safely
//! is `cardset/`; this file connects it to the server: what goes on the cards.
//!
//! The switch is setup's (explanations.EXTRAS "backup_cards"): while it is off, the
//! settings the runtime reads carry no "cards" section and nothing here runs.
use crate::cardset::source::Source;
use crate::cardset::store::{SourceRow, Store};
use crate::cardset::{format, source};
use crate::common::{self, Error, Result};
use serde_json::Value;
use std::fs;
use std::path::{Path, PathBuf};

/// The kinds of source archive format 1 knows (format.rs, inventory.rs).
const IMMICH: &str = "immich-originals";
const NEXTCLOUD: &str = "nextcloud-files";

/// One folder whose files go on the cards, before the catalogue gives it its ID.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Wanted {
    pub kind: &'static str,
    pub display_name: String,
    pub root: PathBuf,
}

/// What goes on the cards, as CARD-RECOVERY.md promises and nothing else: Immich's
/// original photos and videos (its `upload` and `library` folders; its thumbnails and
/// converted videos are not originals) and each Nextcloud user's own files (`<user>/files`:
/// not the trash, the old versions or Nextcloud's own folder). A folder that is not there
/// (Immich's library before its storage template moved anything) has nothing to copy.
pub fn wanted(cfg: &Value, data: &Path) -> Result<Vec<Wanted>> {
    let mut found = vec![];
    if cfg["cards"]["immich"] == true {
        let library = common::photo_library(cfg)?;
        for (folder, name) in [("upload", "Immich uploads"), ("library", "Immich library")] {
            let root = library.join(folder);
            if is_folder(&root) {
                found.push(Wanted {
                    kind: IMMICH,
                    display_name: name.to_string(),
                    root,
                });
            }
        }
    }
    if cfg["cards"]["nextcloud"] == true {
        let cloud = common::saved_path(cfg, "cloud")?;
        let internal = crate::backup::nextcloud_internal(data);
        let mut users: Vec<String> = match fs::read_dir(&cloud) {
            Ok(entries) => entries
                .flatten()
                .filter_map(|e| e.file_name().into_string().ok())
                .collect(),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => vec![],
            Err(e) => return Err(common::at(e, &cloud)),
        };
        users.sort();
        for user in users {
            let files = cloud.join(&user).join("files");
            let own = !crate::backup::is_nextcloud_internal(&user, internal.as_deref());
            // A user's name is at most 64 characters in Nextcloud, as a source's name
            // may be on a card (archive format 1).
            if own
                && user.chars().count() <= 64
                && is_folder(&cloud.join(&user))
                && is_folder(&files)
            {
                found.push(Wanted {
                    kind: NEXTCLOUD,
                    display_name: user,
                    root: files,
                });
            }
        }
    }
    Ok(found)
}

/// A real folder (not a link to one).
fn is_folder(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok_and(|m| m.is_dir())
}

/// The sources the run reads, each with the ID the catalogue keeps for it: one seen
/// before keeps its ID (found by its kind and name), a new one is added.
pub fn sources(store: &mut Store, wanted: &[Wanted]) -> Result<Vec<Source>> {
    let known = store.sources().map_err(card_error)?;
    let mut out = vec![];
    for w in wanted {
        let id = match known
            .iter()
            .find(|s| s.kind == w.kind && s.display_name == w.display_name)
        {
            Some(row) => row.source_id.clone(),
            None => {
                let row = SourceRow {
                    source_id: format::new_uuid().map_err(card_error)?,
                    display_name: w.display_name.clone(),
                    kind: w.kind.to_string(),
                    created_at: format::now(),
                    retired_at: None,
                };
                store.add_source(&row).map_err(card_error)?;
                row.source_id
            }
        };
        out.push(Source {
            source_id: id,
            display_name: w.display_name.clone(),
            root: w.root.clone(),
        });
    }
    Ok(out)
}

/// Every file of `sources` that goes on a card, and what was left out: a file Nextcloud is
/// still receiving waits for the next run, a link or a device is never copied, a name a
/// card cannot keep is counted.
pub fn candidates(sources: &[Source]) -> Result<(Vec<source::Candidate>, source::Skipped)> {
    let mut all = vec![];
    let mut skipped = source::Skipped::default();
    for s in sources {
        let scan = source::scan(s).map_err(card_error)?;
        all.extend(scan.candidates.into_iter().filter(|c| {
            let name = c.path.rsplit('/').next().unwrap_or("");
            !crate::backup::nextcloud_upload_in_progress(name)
        }));
        skipped.special.extend(scan.skipped.special);
        skipped.unsupported.extend(scan.skipped.unsupported);
        skipped.other_mount.extend(scan.skipped.other_mount);
    }
    Ok((all, skipped))
}

/// An engine error as the runtime's own, with its words.
pub fn card_error(e: crate::cardset::error::CardError) -> Error {
    Error::new(&e.what)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn scratch(name: &str) -> PathBuf {
        let p = std::env::temp_dir().join(format!("neko-cards-job-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&p);
        fs::create_dir_all(&p).unwrap();
        p
    }

    fn put(path: &Path, bytes: &[u8]) {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, bytes).unwrap();
    }

    fn settings(data: &Path, immich: bool, nextcloud: bool) -> Value {
        json!({"data_mount": data, "media_mount": data.join("media"),
               "paths": {"photos": data.join("photos/immich-library"), "cloud": data.join("cloud/files")},
               "cards": {"enabled": true, "immich": immich, "nextcloud": nextcloud}})
    }

    #[test]
    fn the_cards_get_immichs_originals_and_each_nextcloud_users_files_only() {
        let data = scratch("sources");
        let photos = data.join("photos/immich-library");
        put(&photos.join("upload/u1/a.jpg"), b"photo");
        put(&photos.join("library/u1/2001/b.jpg"), b"moved photo");
        put(&photos.join("thumbs/u1/a.webp"), b"thumbnail");
        put(&photos.join("encoded-video/u1/c.mp4"), b"converted");
        let cloud = data.join("cloud/files");
        put(&cloud.join("alice/files/Documents/tax.pdf"), b"pdf");
        put(
            &cloud.join("alice/files/big.iso.ocTransferId123.part"),
            b"half",
        );
        put(
            &cloud.join("alice/files_trashbin/files/old.txt"),
            b"deleted",
        );
        put(&cloud.join("alice/files_versions/tax.pdf.v1"), b"older");
        put(
            &cloud.join("appdata_oc1234567890/preview/p.png"),
            b"preview",
        );
        put(&cloud.join("nextcloud.log"), b"log");
        let cfg = settings(&data, true, true);
        let found = wanted(&cfg, &data).unwrap();
        let names: Vec<_> = found
            .iter()
            .map(|w| (w.kind, w.display_name.as_str()))
            .collect();
        assert_eq!(
            names,
            [
                (IMMICH, "Immich uploads"),
                (IMMICH, "Immich library"),
                (NEXTCLOUD, "alice")
            ]
        );
        let mut store = Store::in_memory().unwrap();
        let srcs = sources(&mut store, &found).unwrap();
        let (cands, _) = candidates(&srcs).unwrap();
        let mut seen: Vec<_> = cands
            .iter()
            .map(|c| format!("{}:{}", c.source.display_name, c.path))
            .collect();
        seen.sort();
        assert_eq!(
            seen,
            [
                "Immich library:u1/2001/b.jpg",
                "Immich uploads:u1/a.jpg",
                "alice:Documents/tax.pdf"
            ]
        );
        // The next run finds the same sources under the same IDs, and adds none.
        let again = sources(&mut store, &found).unwrap();
        assert_eq!(again, srcs);
        assert_eq!(store.sources().unwrap().len(), 3);
        // Only what the settings say: Nextcloud off, no Nextcloud files.
        let photos_only = wanted(&settings(&data, true, false), &data).unwrap();
        assert!(photos_only.iter().all(|w| w.kind == IMMICH));
        let _ = fs::remove_dir_all(&data);
    }

    #[test]
    fn the_sources_are_only_read() {
        // The engine opens every source file read-only through descriptors; the
        // originals are the same, with their dates, after a scan and a hash.
        let data = scratch("readonly");
        let file = data.join("photos/immich-library/upload/u1/a.jpg");
        put(&file, b"photo");
        let before = fs::metadata(&file).unwrap();
        let found = wanted(&settings(&data, true, false), &data).unwrap();
        let srcs = sources(&mut Store::in_memory().unwrap(), &found).unwrap();
        let (cands, _) = candidates(&srcs).unwrap();
        source::hash_candidate(&cands[0]).unwrap();
        let after = fs::metadata(&file).unwrap();
        assert_eq!(fs::read(&file).unwrap(), b"photo");
        assert_eq!(before.modified().unwrap(), after.modified().unwrap());
        let _ = fs::remove_dir_all(&data);
    }
}
