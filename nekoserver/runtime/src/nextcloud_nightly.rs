//! 1.0.0 (C-01): the nightly backup saves Nextcloud to the HDD from the first night, with
//! or without a USB drive: what a bring-back needs besides the people's own files (which
//! stay where they are; the weekly USB copy covers those).
//!
//! `<media>/backups/nextcloud-nightly/snapshot-<micros>-<pid>/`, named and kept as the
//! nightly app snapshots are (the same count, the same pruning and room-making):
//! - `nextcloud.sql`  a pg_dump of its database, made with Nextcloud paused;
//! - `data.tar`       its html tree without `data/` (the people's files) and its Compose
//!   folder, written in the same pause, so the apps listed in the database are there;
//! - its `.env` and `config.php` (passwords and its secret) kept as the monthly snapshot
//!   keeps the app password list: sealed for the backup key in `secrets.tar.cms` (the
//!   key and certificate beside it) when there is a key, else inside `data.tar`,
//!   readable by root only;
//! - `manifest.json`  every file's SHA-256, read back from the drive.
//!
//! Nextcloud's web app and its cron job are paused (the database stays up) for the dump
//! and the copy; how long is in the manifest and the job's log.
use crate::{
    backup::{self, Containers},
    common::{self, Error, Result},
    maintenance, VERSION,
};
use serde_json::json;
use std::{
    fs,
    os::unix::fs::{MetadataExt, PermissionsExt},
    path::{Path, PathBuf},
    time::Instant,
};

pub(crate) const FOLDER: &str = "backups/nextcloud-nightly";
pub(crate) const FORMAT: &str = "neko-maintenance-v1";
pub(crate) const KIND: &str = "nextcloud-nightly";
/// What data.tar holds, from the SSD's top folder.
pub(crate) const PATHS: [&str; 2] = ["appdata/nextcloud/html", "compose/nextcloud"];
/// The people's files: only a mount point here, never copied by this backup.
const FILES: &str = "appdata/nextcloud/html/data";
/// The files holding passwords or secret keys in plain text.
pub(crate) const SEALED: [&str; 2] = [
    "compose/nextcloud/.env",
    "appdata/nextcloud/html/config/config.php",
];
const DUMP: &str = "nextcloud.sql";
/// Space kept free on the HDD after it, as the nightly app backup keeps.
const RESERVE: u128 = 2 << 30;

/// One night's Nextcloud backup that was made.
#[derive(Debug)]
pub(crate) struct Made {
    pub name: String,
    pub paused_seconds: f64,
    pub bytes: u64,
}

/// The words a night without a Nextcloud backup is reported with (the other apps'
/// backup goes on, and the job is "done in part").
pub(crate) fn not_saved(e: &Error) -> String {
    format!("Nextcloud was not backed up tonight: {e}. The other apps' backup is not affected; the next night tries again")
}

/// The nightly run's Nextcloud part, with the real drives and Docker. Ok(None): Nextcloud
/// is not installed (yet), so there is nothing to save.
pub(crate) fn run(cfgpath: &Path, data: &Path, media: &Path) -> Result<Option<Made>> {
    let compose = data.join("compose/nextcloud/compose.yaml");
    if !compose.is_file() {
        println!("Nextcloud: not installed yet, so there is nothing of it to back up");
        return Ok(None);
    }
    common::stage("backing up Nextcloud");
    backup::guard(cfgpath, data, media)?;
    // A HDD that drops out mid-run stops it (see common::expect_drive).
    let _hdd = common::expect_drive(media)?;
    let root = media.join(FOLDER);
    private_root(&root)?;
    let settings = common::json(cfgpath, 64000);
    let (keep, refused) = backup::keep_count(
        &settings["app_backup"]["keep_snapshots"],
        &settings["app_backup"]["earlier"],
        "app_backup",
    );
    if let Some(why) = refused {
        println!("Nextcloud backup: {why}");
    }
    common::clear_leftovers(&root, true)?;
    let needed = needed(data, &root);
    let removed = backup::make_room(&root, needed, &|have| short_of_space(media, have, needed))?;
    let env = data.join("compose/nextcloud/.env");
    let dump = |dest: &Path| {
        backup::database_of("nextcloud", &compose, &env, dest, true)?
            .ok_or_else(|| Error::new("Database dump was not made"))
    };
    let docker = backup::AppDocker(&backup::NEXTCLOUD);
    let made = make(
        data,
        &root,
        Path::new(maintenance::SECRETS_KEY_DIR),
        keep,
        &docker,
        Path::new(backup::NEXTCLOUD.journal()),
        &dump,
    )
    .map_err(|error| backup::failed_after_room(&root, &removed, error))?;
    println!(
        "Nextcloud: nightly backup {} made ({:.1} MB; Nextcloud was paused for {})",
        made.name,
        made.bytes as f64 / 1e6,
        common::plural(made.paused_seconds.round(), "second", "seconds")
    );
    Ok(Some(made))
}

/// The backup folder, made private, and refused when it is not (never chmod'ed quietly).
fn private_root(root: &Path) -> Result<()> {
    common::directory(root, 0o700)?;
    let meta = fs::metadata(root)?;
    if meta.mode() & 0o077 != 0 || meta.uid() != unsafe { libc::geteuid() } {
        return Err(Error::new("Private Nextcloud backup directory required"));
    }
    Ok(())
}

/// Room for one more: the html tree and Compose folder, the last night's dump as a guess
/// of this one's size, and the reserve.
fn needed(data: &Path, root: &Path) -> u128 {
    let html = data.join(PATHS[0]);
    let tree: u128 = fs::read_dir(&html)
        .into_iter()
        .flatten()
        .flatten()
        .filter(|e| e.file_name() != "data")
        .map(|e| backup::tree_bytes(&e.path()))
        .sum::<u128>()
        + backup::tree_bytes(&data.join(PATHS[1]));
    let last_dump = common::complete_snapshots(root)
        .ok()
        .and_then(|found| found.last().map(|(_, name)| root.join(name).join(DUMP)))
        .and_then(|p| fs::metadata(p).ok())
        .map_or(0, |m| m.len() as u128);
    tree + last_dump + RESERVE
}

fn short_of_space(media: &Path, have: u128, needed: u128) -> String {
    let gb = |b: u128| format!("{:.1}", b as f64 / (1u64 << 30) as f64);
    format!(
        "Not enough space on the HDD ({}) for the nightly Nextcloud backup: {} GB free, about {} GB needed (its database copy and settings plus 2 GB kept free)",
        media.display(),
        gb(have),
        gb(needed)
    )
}

/// The password files there are, in the order they are sealed.
fn sealed_members(data: &Path) -> Vec<String> {
    SEALED
        .iter()
        .filter(|m| data.join(m).is_file())
        .map(|m| m.to_string())
        .collect()
}

/// Make one night's backup in `root` (the HDD's folder) from `data` (the SSD), keep the
/// newest `keep`. `ctl` and `journal` pause Nextcloud; `dump` writes the database copy
/// into the folder it is given and returns its path.
pub(crate) fn make(
    data: &Path,
    root: &Path,
    key_dir: &Path,
    keep: u64,
    ctl: &dyn Containers,
    journal: &Path,
    dump: &dyn Fn(&Path) -> Result<PathBuf>,
) -> Result<Made> {
    private_root(root)?;
    let version = backup::nextcloud_version(data).ok_or_else(|| {
        Error::new("its version could not be read from html/version.php, so a bring-back could not check it; run  sudo bash install.sh  to set Nextcloud up again")
    })?;
    // Decided once, as the monthly snapshot does (maintenance::seal_as_decided): data.tar
    // leaves the password files out only when they are sealed beside it.
    let sealing = maintenance::secrets_key_ready(key_dir);
    let members = sealed_members(data);
    let stage = backup::stage_snapshot(root)?;
    // The files sit in the folder itself (the layout the bring-back reads), not in payload/.
    fs::remove_dir(&stage.payload)?;
    let mut exclude = vec![FILES];
    if sealing {
        exclude.extend(members.iter().map(String::as_str));
    }
    let paths: Vec<String> = PATHS.iter().map(|p| p.to_string()).collect();
    let started = Instant::now();
    let password_files = paused(ctl, journal, || {
        let copy = dump(&stage.dir)?;
        fs::rename(&copy, stage.dir.join(DUMP))?;
        fs::set_permissions(stage.dir.join(DUMP), fs::Permissions::from_mode(0o600))?;
        let archive = stage.dir.join("data.tar");
        maintenance::tar(data, &paths, &archive, &exclude)?;
        // Root's alone whatever the umask (it may hold the password files: no backup key).
        fs::set_permissions(&archive, fs::Permissions::from_mode(0o600))?;
        seal(data, &stage.dir, key_dir, sealing, &members)
    })?;
    let paused_seconds = started.elapsed().as_secs_f64();
    if password_files == "unencrypted_in_data_tar" {
        println!("Nextcloud backup: there is no backup key, so its .env and config.php are kept unencrypted inside data.tar (readable by root only). To encrypt them from now on: sudo python3 backup_secrets.py --setup");
    }
    let mut sums = json!({});
    let mut bytes = 0;
    let mut files = vec![
        ("data", "data.tar".to_string()),
        ("database", DUMP.to_string()),
    ];
    if password_files == "encrypted" {
        files.extend([
            ("secrets", maintenance::SECRETS_ARCHIVE.to_string()),
            ("secrets_key", maintenance::SECRETS_KEY.to_string()),
            ("secrets_cert", maintenance::SECRETS_CERT.to_string()),
        ]);
    }
    for (key, name) in &files {
        let path = stage.dir.join(name);
        bytes += fs::metadata(&path)?.len();
        // Read back from the drive, not from memory (common::digest_landed).
        sums[key] = json!(common::digest_landed(&path)?);
    }
    let manifest = json!({
        "format": FORMAT,
        "kind": KIND,
        "apps": ["nextcloud"],
        "version": VERSION,
        "created_at": common::now(),
        "nextcloud_version": version,
        "paused_seconds": (paused_seconds * 10.0).round() / 10.0,
        "paths": {"data": PATHS, "media": []},
        "sha256": sums,
        "password_files": password_files,
    });
    common::atomic_json(&stage.dir.join("manifest.json"), &manifest)?;
    fs::File::open(&stage.dir)?.sync_all()?;
    common::on_drive(root)?;
    common::rename_noreplace(&stage.dir, &root.join(&stage.name))?;
    fs::File::open(root)?.sync_all()?;
    common::prune_snapshots(root, keep, Some(&stage.name), None)?;
    Ok(Made {
        name: stage.name.clone(),
        paused_seconds,
        bytes,
    })
}

/// `work` with Nextcloud's web app and cron job stopped; started again however it ends
/// (a cut-off run: the journal, by the job's ExecStopPost and at boot).
fn paused<T>(ctl: &dyn Containers, journal: &Path, work: impl FnOnce() -> Result<T>) -> Result<T> {
    let stopped = match backup::pause_app(&backup::NEXTCLOUD, ctl, journal) {
        Ok(stopped) => stopped,
        Err(e) => {
            let resumed = backup::resume_app(&backup::NEXTCLOUD, ctl, journal);
            return common::first_error(Err(e), resumed);
        }
    };
    backup::say_paused(&backup::NEXTCLOUD, &stopped);
    let done = work();
    let resumed = backup::resume_app(&backup::NEXTCLOUD, ctl, journal);
    common::first_error(done, resumed)
}

/// The password files kept as decided when the backup began (`sealing`).
fn seal(
    data: &Path,
    folder: &Path,
    key_dir: &Path,
    sealing: bool,
    members: &[String],
) -> Result<&'static str> {
    if members.is_empty() {
        return Ok("none");
    }
    if !sealing {
        return Ok("unencrypted_in_data_tar");
    }
    match maintenance::encrypt_members_with(data, folder, key_dir, members)? {
        "unencrypted_in_data_tar" => Err(Error::new(
            "the backup key was there when this backup began and is gone now, so its .env and config.php would be in no part of it. Make the key again with  sudo python3 backup_secrets.py --setup",
        )),
        kept => Ok(kept),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;
    use std::cell::RefCell;
    use std::os::unix::fs::DirBuilderExt;

    struct Fake {
        running: RefCell<Vec<(String, String)>>,
        done: RefCell<Vec<String>>,
    }
    impl Fake {
        fn up() -> Fake {
            Fake {
                running: RefCell::new(vec![
                    ("nextcloud".into(), "a".repeat(64)),
                    ("cron".into(), "b".repeat(64)),
                ]),
                done: RefCell::new(vec![]),
            }
        }
    }
    impl Containers for Fake {
        fn running_writers(&self) -> Result<Vec<(String, String)>> {
            Ok(self.running.borrow().clone())
        }
        fn stop(&self, id: &str) -> Result<()> {
            self.done.borrow_mut().push(format!("stop {}", &id[..1]));
            self.running.borrow_mut().retain(|(_, i)| i != id);
            Ok(())
        }
        fn start(&self, service: &str, id: &str) -> Result<()> {
            self.done.borrow_mut().push(format!("start {}", &id[..1]));
            self.running
                .borrow_mut()
                .push((service.to_string(), id.to_string()));
            Ok(())
        }
    }
    fn scratch(name: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!("neko-ncn-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&root)
            .unwrap();
        root
    }
    fn write(path: &Path, body: &[u8]) {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, body).unwrap();
    }
    /// A small Nextcloud on the SSD: its html tree with a person's file under data/.
    fn ssd(root: &Path) -> PathBuf {
        let data = root.join("ssd");
        write(
            &data.join("appdata/nextcloud/html/version.php"),
            b"<?php $OC_VersionString = '31.0.5';\n",
        );
        write(
            &data.join("appdata/nextcloud/html/index.php"),
            b"<?php // nc\n",
        );
        write(
            &data.join("appdata/nextcloud/html/custom_apps/notes/appinfo/info.xml"),
            b"<info/>",
        );
        write(
            &data.join("appdata/nextcloud/html/config/config.php"),
            b"<?php $CONFIG = ['dbpassword' => 'db-secret-123'];",
        );
        write(
            &data.join("appdata/nextcloud/html/data/alice/files/private.txt"),
            b"alice's own file",
        );
        write(
            &data.join("compose/nextcloud/compose.yaml"),
            b"services: {}\n",
        );
        write(
            &data.join("compose/nextcloud/.env"),
            b"POSTGRES_PASSWORD=env-secret-456\n",
        );
        write(
            &data.join("compose/immich/.env"),
            b"DB_PASSWORD=immich-secret\n",
        );
        data
    }
    /// The database copy, made as the real one is (into the folder it is given), noting
    /// whether Nextcloud was paused while it was made.
    fn dumper<'a>(ctl: &'a Fake) -> impl Fn(&Path) -> Result<PathBuf> + 'a {
        move |dest: &Path| {
            let paused = ctl.running.borrow().is_empty();
            let out = dest.join("nextcloud-20261123T010000Z.sql");
            fs::write(
                &out,
                format!("-- paused={paused}\n-- PostgreSQL database dump complete\n"),
            )?;
            Ok(out)
        }
    }
    fn listed(tar: &Path) -> Vec<String> {
        let out = std::process::Command::new("tar")
            .arg("--list")
            .arg("--file")
            .arg(tar)
            .output()
            .unwrap();
        String::from_utf8_lossy(&out.stdout)
            .lines()
            .map(|l| l.trim_end_matches('/').to_string())
            .collect()
    }

    #[test]
    fn a_night_saves_the_database_settings_and_html_but_not_the_files() {
        let root = scratch("night");
        let data = ssd(&root);
        let hdd = root.join("hdd").join(FOLDER);
        let ctl = Fake::up();
        let journal = root.join("nextcloud-resume.json");
        let made = make(
            &data,
            &hdd,
            &root.join("no-key"),
            3,
            &ctl,
            &journal,
            &dumper(&ctl),
        )
        .unwrap();
        let folder = hdd.join(&made.name);
        assert!(made.name.starts_with("snapshot-"), "{}", made.name);
        // Paused for the dump and started again; the journal is gone.
        assert_eq!(
            *ctl.done.borrow(),
            ["stop a", "stop b", "start a", "start b"]
        );
        assert!(!journal.exists());
        assert!(fs::read_to_string(folder.join(DUMP))
            .unwrap()
            .contains("paused=true"));
        let names = listed(&folder.join("data.tar"));
        for wanted in [
            "appdata/nextcloud/html/version.php",
            "appdata/nextcloud/html/custom_apps/notes/appinfo/info.xml",
            "compose/nextcloud/compose.yaml",
        ] {
            assert!(names.contains(&wanted.to_string()), "{wanted} in {names:?}");
        }
        assert!(
            !names
                .iter()
                .any(|n| n.starts_with("appdata/nextcloud/html/data")),
            "the people's files are not in it: {names:?}"
        );
        assert!(
            !names.iter().any(|n| n.starts_with("compose/immich")),
            "{names:?}"
        );
        let manifest: Value =
            serde_json::from_slice(&fs::read(folder.join("manifest.json")).unwrap()).unwrap();
        assert_eq!(manifest["format"], FORMAT);
        assert_eq!(manifest["kind"], KIND);
        assert_eq!(manifest["apps"], json!(["nextcloud"]));
        assert_eq!(manifest["nextcloud_version"], "31.0.5");
        assert_eq!(manifest["paths"]["data"], json!(PATHS));
        assert!(manifest["paused_seconds"].as_f64().is_some());
        for (key, name) in [("data", "data.tar"), ("database", DUMP)] {
            assert_eq!(
                manifest["sha256"][key],
                common::digest(&folder.join(name)).unwrap(),
                "{key}"
            );
        }
        // No key: the password files are kept in data.tar, which only root reads.
        assert_eq!(manifest["password_files"], "unencrypted_in_data_tar");
        assert!(names.contains(&SEALED[0].to_string()) && names.contains(&SEALED[1].to_string()));
        assert_eq!(fs::metadata(&folder).unwrap().mode() & 0o777, 0o700);
        for name in ["data.tar", DUMP, "manifest.json"] {
            let mode = fs::metadata(folder.join(name)).unwrap().mode() & 0o777;
            assert_eq!(mode, 0o600, "{name}");
        }
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn with_a_backup_key_its_passwords_are_sealed_and_left_out_of_data_tar() {
        let root = scratch("sealed");
        let data = ssd(&root);
        let keys = root.join("keys");
        fs::create_dir(&keys).unwrap();
        maintenance::tests::backup_key(&keys, "correct horse");
        let hdd = root.join("hdd").join(FOLDER);
        let ctl = Fake::up();
        let made = make(
            &data,
            &hdd,
            &keys,
            3,
            &ctl,
            &root.join("j.json"),
            &dumper(&ctl),
        )
        .unwrap();
        let folder = hdd.join(&made.name);
        let manifest: Value =
            serde_json::from_slice(&fs::read(folder.join("manifest.json")).unwrap()).unwrap();
        assert_eq!(manifest["password_files"], "encrypted");
        for key in ["secrets", "secrets_key", "secrets_cert"] {
            assert!(manifest["sha256"][key].is_string(), "{key}");
        }
        let names = listed(&folder.join("data.tar"));
        assert!(
            !names.iter().any(|n| SEALED.contains(&n.as_str())),
            "{names:?}"
        );
        for entry in fs::read_dir(&folder).unwrap().flatten() {
            let body = fs::read(entry.path()).unwrap();
            for secret in [&b"db-secret-123"[..], b"env-secret-456"] {
                assert!(
                    !body.windows(secret.len()).any(|w| w == secret),
                    "{:?} holds a password in plain text",
                    entry.file_name()
                );
            }
        }
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn a_failed_dump_starts_nextcloud_again_and_leaves_nothing_half_made() {
        let root = scratch("failed");
        let data = ssd(&root);
        let hdd = root.join("hdd").join(FOLDER);
        let ctl = Fake::up();
        let journal = root.join("j.json");
        let failing = |_: &Path| -> Result<PathBuf> { Err(Error::new("Database dump failed")) };
        let e = make(
            &data,
            &hdd,
            &root.join("no-key"),
            3,
            &ctl,
            &journal,
            &failing,
        )
        .unwrap_err();
        assert_eq!(e.to_string(), "Database dump failed");
        assert_eq!(ctl.running.borrow().len(), 2, "started again");
        assert!(!journal.exists());
        assert_eq!(
            fs::read_dir(&hdd).unwrap().count(),
            0,
            "nothing left in {hdd:?}"
        );
        assert!(
            not_saved(&e).starts_with("Nextcloud was not backed up tonight: Database dump failed.")
        );
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn the_nightly_count_is_kept() {
        let root = scratch("kept");
        let data = ssd(&root);
        let hdd = root.join("hdd").join(FOLDER);
        let ctl = Fake::up();
        let mut names = vec![];
        for _ in 0..4 {
            let made = make(
                &data,
                &hdd,
                &root.join("no-key"),
                2,
                &ctl,
                &root.join("j.json"),
                &dumper(&ctl),
            )
            .unwrap();
            names.push(made.name);
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
        let mut left: Vec<String> = fs::read_dir(&hdd)
            .unwrap()
            .flatten()
            .map(|e| e.file_name().to_string_lossy().to_string())
            .collect();
        left.sort();
        assert_eq!(left, names[2..].to_vec());
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn a_version_that_cannot_be_read_is_said_and_nothing_is_paused() {
        let root = scratch("version");
        let data = ssd(&root);
        fs::remove_file(data.join("appdata/nextcloud/html/version.php")).unwrap();
        let ctl = Fake::up();
        let e = make(
            &data,
            &root.join("hdd").join(FOLDER),
            &root.join("no-key"),
            3,
            &ctl,
            &root.join("j.json"),
            &dumper(&ctl),
        )
        .unwrap_err();
        assert!(
            e.to_string().starts_with("its version could not be read"),
            "{e}"
        );
        assert!(ctl.done.borrow().is_empty());
        let _ = fs::remove_dir_all(&root);
    }
}
