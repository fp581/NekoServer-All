//! The weekly USB copy's copy of the app settings (1.0.0, row T1-R4). With the SSD and
//! the HDD both lost, the USB drive brought back the photos, Nextcloud and the password
//! list, but not the apps' own settings (Jellyfin's users and what was watched, Sonarr,
//! Radarr, Seerr...): their nightly backups are on the HDD only. The newest finished
//! nightly app backup now goes onto the USB drive too, after everything else.
//!
//! App settings hold the apps' API keys and password hashes, and the USB drive is the
//! backup that leaves the house. So the copy is sealed for the backup key, as the password
//! list on the drive is: only the backup password chosen during setup opens it (the
//! recovery menu's "App settings, from a nightly backup" asks for it).
//!
//! On the drive, `app-settings/<snapshot>/`:
//! - `settings.tar.enc`: the nightly snapshot (its manifest and its files) as one tar
//!   archive, encrypted (AES-256, `openssl enc`) with a random password made for this copy;
//! - `archive-key.cms`: that password, sealed for the backup certificate (`openssl cms`);
//! - the backup key and its certificate (the key locked by the backup password), so a new
//!   server opens the copy with the old backup password;
//! - `manifest.json`: which snapshot it is, when it was made, each file's checksum.
//!
//! A copy is made in `.<snapshot>.partial` and checked: the archive is read back from the
//! drive and decrypted, and must give the very bytes that were archived. Only then is it
//! put in place, and only then is the copy before it removed.
use crate::common::{self, Error, Result};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::fs;
use std::io::{Read, Write};
use std::path::Path;
use std::process::{Command, Stdio};

/// The folder on the USB drive.
pub(crate) const FOLDER: &str = "app-settings";
pub(crate) const FORMAT: &str = "neko-usb-app-settings-v1";
const ARCHIVE: &str = "settings.tar.enc";
const ARCHIVE_KEY: &str = "archive-key.cms";
/// The nightly app backups on the HDD.
const NIGHTLY: &str = "backups/media-applications";
/// Room kept free on the drive beyond the archive (its size is the snapshot's files and
/// tar's headers; encryption adds a few bytes).
const SPARE: u128 = 64 << 20;
/// How the archive's password makes its key (`openssl enc`): the same on both sides.
pub(crate) const ENC_ARGS: [&str; 5] = ["-aes-256-ctr", "-pbkdf2", "-iter", "100000", "-salt"];

/// Copy the newest finished nightly app backup on `media` onto the mounted USB drive
/// `usb`, sealed for the backup key in `key_dir`; `scratch` is a private folder on the
/// SSD for the archive's password while it is used. Some(words) when the copy is
/// incomplete for it: no backup password yet, no room, or a damaged nightly backup.
pub(crate) fn copy_newest(
    media: &Path,
    usb: &Path,
    key_dir: &Path,
    scratch: &Path,
) -> Result<Option<String>> {
    copy_newest_with(media, usb, key_dir, scratch, &common::free_bytes)
}

/// `copy_newest`, with `free` giving the bytes free on the drive that holds a folder.
fn copy_newest_with(
    media: &Path,
    usb: &Path,
    key_dir: &Path,
    scratch: &Path,
    free: &dyn Fn(&Path) -> Result<u128>,
) -> Result<Option<String>> {
    let nightly = media.join(NIGHTLY);
    let Some(name) = newest(&nightly)? else {
        println!("USB: no nightly backup of the app settings on the HDD yet; a later weekly copy takes it");
        return Ok(None);
    };
    let folder = usb.join(FOLDER);
    if copied(&folder.join(&name), &name) {
        println!("USB: app settings of {} (already on the drive)", day(&name));
        return Ok(None);
    }
    if !crate::maintenance::secrets_key_ready(key_dir) {
        return Ok(Some(NO_KEY.to_string()));
    }
    let snapshot = nightly.join(&name);
    let manifest = common::json(&snapshot.join("manifest.json"), 128_000_000);
    let size: u128 = manifest["files"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|r| r["size"].as_u64().unwrap_or(0) as u128 + 1024)
        .sum();
    common::directory(&folder, 0o700)?;
    clear_partials(&folder)?;
    let free = free(&folder)?;
    if free < size + SPARE {
        return Ok(Some(format!(
            "the app settings did not fit on the USB drive ({} needed, {} free): the photos and the other backups were copied first; make room on the drive or use a bigger one, and the next weekly copy takes them",
            size_said(size + SPARE),
            size_said(free)
        )));
    }
    let partial = folder.join(format!(".{name}.partial"));
    common::directory(&partial, 0o700)?;
    let made = seal(&snapshot, &name, &partial, key_dir, scratch)
        .and_then(|()| Ok(fs::rename(&partial, folder.join(&name))?));
    if let Err(e) = made {
        let _ = fs::remove_dir_all(&partial);
        return Err(e);
    }
    fs::File::open(&folder)?.sync_all()?;
    // Only now that the new copy is in place and checked: the one before it goes.
    for entry in fs::read_dir(&folder)? {
        let entry = entry?;
        let other = entry.file_name().to_string_lossy().to_string();
        if other != name && entry.file_type()?.is_dir() {
            fs::remove_dir_all(common::safe(&entry.path())?)?;
        }
    }
    println!(
        "USB: app settings of {} (encrypted; the recovery menu's \"App settings, from a nightly backup\" opens them with the backup password)",
        day(&name)
    );
    Ok(None)
}

pub(crate) const NO_KEY: &str = "the app settings were not copied to the USB drive: there is no backup password yet to protect them (they hold the apps' keys); run  sudo bash install.sh  to choose one";

/// A size in plain words.
fn size_said(bytes: u128) -> String {
    if bytes < 1_000_000_000 {
        format!("{} MB", bytes.div_ceil(1_000_000))
    } else {
        format!("{:.1} GB", bytes as f64 / 1e9)
    }
}

/// The newest complete nightly snapshot's name (never one still being written: those
/// are `.snapshot-….partial` until they are finished).
fn newest(nightly: &Path) -> Result<Option<String>> {
    // Sorted oldest first; one made while the clock was ahead sorts as the oldest.
    Ok(common::complete_snapshots(nightly)?
        .into_iter()
        .map(|(_, name)| name)
        .next_back())
}

/// The day a snapshot `snapshot-<microseconds>-<pid>` was made, for the report.
fn day(name: &str) -> String {
    name.split('-')
        .nth(1)
        .and_then(|us| us.parse::<i64>().ok())
        .and_then(chrono::DateTime::from_timestamp_micros)
        .map(|d| d.format("%Y-%m-%d").to_string())
        .unwrap_or_else(|| name.to_string())
}

/// `copy` is a finished copy of snapshot `name` whose files match their checksums.
fn copied(copy: &Path, name: &str) -> bool {
    let manifest = common::json(&copy.join("manifest.json"), 64000);
    manifest["format"] == FORMAT
        && manifest["snapshot"] == name
        && files().iter().all(|(key, file)| {
            manifest["sha256"][key]
                .as_str()
                .is_some_and(|sum| common::digest(&copy.join(file)).is_ok_and(|d| d == sum))
        })
}

/// The files of a copy, by the key their checksum has in its manifest.
fn files() -> [(&'static str, &'static str); 4] {
    [
        ("archive", ARCHIVE),
        ("archive_key", ARCHIVE_KEY),
        ("secrets_key", crate::maintenance::SECRETS_KEY),
        ("secrets_cert", crate::maintenance::SECRETS_CERT),
    ]
}

/// Half-made copies of runs that were cut off.
fn clear_partials(folder: &Path) -> Result<()> {
    for entry in fs::read_dir(folder)? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().to_string();
        if name.starts_with('.') && name.ends_with(".partial") {
            fs::remove_dir_all(common::safe(&entry.path())?)?;
        }
    }
    Ok(())
}

/// The sealed copy of `snapshot` in `into`, checked.
fn seal(snapshot: &Path, name: &str, into: &Path, key_dir: &Path, scratch: &Path) -> Result<()> {
    let (key, cert) = crate::maintenance::key_pair(
        &key_dir.join(crate::maintenance::SECRETS_KEY),
        &key_dir.join(crate::maintenance::SECRETS_CERT),
    )?;
    common::atomic(&into.join(crate::maintenance::SECRETS_KEY), &key, 0o600)?;
    common::atomic(&into.join(crate::maintenance::SECRETS_CERT), &cert, 0o600)?;
    // The archive's password: 32 random bytes, written as hex for openssl, kept on the SSD
    // only while it is used.
    let mut random = [0u8; 32];
    fs::File::open("/dev/urandom")?.read_exact(&mut random)?;
    let password: String = random.iter().map(|b| format!("{b:02x}")).collect();
    common::directory(scratch, 0o700)?;
    let pass_file = scratch.join(format!("usb-app-settings-{}.pass", std::process::id()));
    common::atomic(&pass_file, password.as_bytes(), 0o600)?;
    let result = (|| {
        sealed_for(
            &password,
            &into.join(crate::maintenance::SECRETS_CERT),
            &into.join(ARCHIVE_KEY),
        )?;
        let archived = encrypt(snapshot, &into.join(ARCHIVE), &pass_file)?;
        let mut sums = json!({});
        for (key, file) in files() {
            sums[key] = json!(common::digest_landed(&into.join(file))?);
        }
        // Read back from the drive and decrypted: the bytes that were archived.
        if decrypted_digest(&into.join(ARCHIVE), &pass_file)? != archived {
            return Err(Error::new(
                "the app settings' copy on the USB drive did not read back as written (a failing drive or cable?); it was not kept",
            ));
        }
        let made = common::json(&snapshot.join("manifest.json"), 128_000_000)["created_at"].clone();
        common::atomic_json(
            &into.join("manifest.json"),
            &json!({"format": FORMAT, "snapshot": name, "created_at": made, "sha256": sums}),
        )?;
        fs::File::open(into)?.sync_all()?;
        Ok(())
    })();
    let _ = fs::remove_file(&pass_file);
    result
}

/// `password` sealed for the certificate `cert` into `output` (openssl cms).
fn sealed_for(password: &str, cert: &Path, output: &Path) -> Result<()> {
    let mut child = Command::new("openssl")
        .args([
            "cms", "-encrypt", "-binary", "-aes256", "-outform", "DER", "-out",
        ])
        .arg(output)
        .arg(cert)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| common::not_started(e, "openssl"))?;
    child
        .stdin
        .take()
        .ok_or_else(|| Error::new("openssl took no input"))?
        .write_all(password.as_bytes())?;
    if !child.wait()?.success() {
        return Err(Error::new(
            "Could not seal the app settings' copy for the backup key",
        ));
    }
    Ok(())
}

/// The tar archive of `snapshot` (its manifest and payload), encrypted into `output`;
/// returns the archive's checksum before encryption.
fn encrypt(snapshot: &Path, output: &Path, pass_file: &Path) -> Result<String> {
    let mut tar = Command::new("tar")
        .args(["--create", "--file", "-", "--numeric-owner", "-C"])
        .arg(snapshot)
        .args(["--", "manifest.json", "payload"])
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| common::not_started(e, "tar"))?;
    let mut enc = Command::new("openssl")
        .arg("enc")
        .args(ENC_ARGS)
        .arg("-pass")
        .arg(format!("file:{}", pass_file.display()))
        .arg("-out")
        .arg(output)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| common::not_started(e, "openssl"))?;
    let mut from = tar
        .stdout
        .take()
        .ok_or_else(|| Error::new("tar gave no output"))?;
    let mut to = enc
        .stdin
        .take()
        .ok_or_else(|| Error::new("openssl took no input"))?;
    let mut hash = Sha256::new();
    let mut buffer = vec![0u8; 1 << 20];
    let streamed = (|| -> Result<()> {
        loop {
            let n = from.read(&mut buffer)?;
            if n == 0 {
                return Ok(());
            }
            hash.update(&buffer[..n]);
            to.write_all(&buffer[..n])?;
        }
    })();
    drop(to);
    let (archived, encrypted) = (tar.wait()?, enc.wait()?);
    streamed?;
    if !archived.success() || !encrypted.success() {
        return Err(Error::new(
            "Could not write the app settings' copy to the USB drive",
        ));
    }
    fs::File::open(output)?.sync_all()?;
    Ok(format!("{:x}", hash.finalize()))
}

/// The checksum of what `archive` decrypts to.
fn decrypted_digest(archive: &Path, pass_file: &Path) -> Result<String> {
    let mut dec = Command::new("openssl")
        .args(["enc", "-d"])
        .args(ENC_ARGS)
        .arg("-pass")
        .arg(format!("file:{}", pass_file.display()))
        .arg("-in")
        .arg(archive)
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| common::not_started(e, "openssl"))?;
    let mut from = dec
        .stdout
        .take()
        .ok_or_else(|| Error::new("openssl gave no output"))?;
    let mut hash = Sha256::new();
    let mut buffer = vec![0u8; 1 << 20];
    loop {
        let n = from.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        hash.update(&buffer[..n]);
    }
    if !dec.wait()?.success() {
        return Ok(String::new());
    }
    Ok(format!("{:x}", hash.finalize()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{DirBuilderExt, PermissionsExt};

    const PASSWORD: &str = "the backup password";

    fn scratch(name: &str) -> std::path::PathBuf {
        let root =
            std::env::temp_dir().join(format!("neko-usb-apps-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        root
    }
    fn private(dir: &Path) {
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(dir)
            .unwrap();
        fs::set_permissions(dir, fs::Permissions::from_mode(0o700)).unwrap();
    }
    fn write(path: &Path, body: &[u8]) {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, body).unwrap();
    }
    fn names(folder: &Path) -> Vec<String> {
        let mut names: Vec<String> = fs::read_dir(folder)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().to_string())
            .collect();
        names.sort();
        names
    }
    /// A finished nightly snapshot `name` on the HDD under `media`, whose Jellyfin
    /// settings hold `secret`.
    fn nightly(media: &Path, name: &str, secret: &str) -> std::path::PathBuf {
        let snap = media.join(NIGHTLY).join(name);
        private(&snap);
        let config = format!("<ApiKey>{secret}</ApiKey>");
        write(
            &snap.join("payload/appdata/jellyfin/config/system.xml"),
            config.as_bytes(),
        );
        write(
            &snap.join("payload/compose/docker-compose.yml"),
            b"services: {}\n",
        );
        let manifest = json!({"format": "neko-media-snapshot-v1", "created_at": 1_700_000_000.0,
            "files": [{"path": "appdata/jellyfin/config/system.xml", "kind": "file", "size": config.len()}]});
        write(&snap.join("manifest.json"), manifest.to_string().as_bytes());
        snap
    }
    fn keys(root: &Path) -> std::path::PathBuf {
        let keys = root.join("keys");
        private(&keys);
        crate::maintenance::tests::backup_key(&keys, PASSWORD);
        keys
    }
    fn plenty(_: &Path) -> Result<u128> {
        Ok(1 << 40)
    }
    fn run(cmd: &mut Command) -> Vec<u8> {
        let out = cmd.output().unwrap();
        assert!(
            out.status.success(),
            "{cmd:?}: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        out.stdout
    }
    /// What the copy `copy` opens to with the backup password, unpacked in `into`, as a
    /// new server would: only the files on the USB drive and the password are used.
    fn opened(copy: &Path, into: &Path) {
        let password = run(Command::new("openssl")
            .args(["cms", "-decrypt", "-binary", "-inform", "DER", "-in"])
            .arg(copy.join(ARCHIVE_KEY))
            .arg("-inkey")
            .arg(copy.join(crate::maintenance::SECRETS_KEY))
            .args(["-passin", &format!("pass:{PASSWORD}")]));
        let pass_file = into.with_extension("pass");
        fs::write(&pass_file, &password).unwrap();
        fs::create_dir_all(into).unwrap();
        let tar = into.with_extension("tar");
        run(Command::new("openssl")
            .args(["enc", "-d"])
            .args(ENC_ARGS)
            .arg("-pass")
            .arg(format!("file:{}", pass_file.display()))
            .arg("-in")
            .arg(copy.join(ARCHIVE))
            .arg("-out")
            .arg(&tar));
        run(Command::new("tar").arg("-xf").arg(&tar).arg("-C").arg(into));
    }

    /// The newest finished nightly backup goes onto the drive, sealed: the apps' keys are
    /// not readable on it, and the backup password opens the very files of the snapshot.
    /// One still being written is never taken.
    #[test]
    fn the_newest_app_backup_goes_onto_the_usb_drive_sealed_for_the_backup_key() {
        let root = scratch("sealed");
        let (media, usb, work) = (root.join("hdd"), root.join("usb"), root.join("work"));
        let keys = keys(&root);
        nightly(&media, "snapshot-1700000000000000-1", "old-key");
        let newest = nightly(
            &media,
            "snapshot-1700086400000000-2",
            "jellyfin-api-key-123",
        );
        // Newer, but still being written.
        write(
            &media
                .join(NIGHTLY)
                .join(".snapshot-1700172800000000-3.partial/payload/x"),
            b"half",
        );
        assert_eq!(
            copy_newest_with(&media, &usb, &keys, &work, &plenty).unwrap(),
            None
        );
        let copy = usb.join(FOLDER).join("snapshot-1700086400000000-2");
        assert_eq!(names(&usb.join(FOLDER)), ["snapshot-1700086400000000-2"]);
        for entry in fs::read_dir(&copy).unwrap() {
            let body = fs::read(entry.unwrap().path()).unwrap();
            assert!(!String::from_utf8_lossy(&body).contains("jellyfin-api-key-123"));
        }
        let manifest = common::json(&copy.join("manifest.json"), 64000);
        assert_eq!(manifest["format"], FORMAT);
        assert_eq!(manifest["snapshot"], "snapshot-1700086400000000-2");
        assert!(copied(&copy, "snapshot-1700086400000000-2"));
        let back = root.join("opened");
        opened(&copy, &back);
        for file in [
            "manifest.json",
            "payload/appdata/jellyfin/config/system.xml",
            "payload/compose/docker-compose.yml",
        ] {
            assert_eq!(
                fs::read(back.join(file)).unwrap(),
                fs::read(newest.join(file)).unwrap(),
                "{file}"
            );
        }
        // Nothing of the archive's password is left behind on the SSD.
        assert_eq!(names(&work), Vec::<String>::new());
        // The next week, the same backup: nothing is written again.
        let before = fs::metadata(copy.join(ARCHIVE))
            .unwrap()
            .modified()
            .unwrap();
        assert_eq!(
            copy_newest_with(&media, &usb, &keys, &work, &plenty).unwrap(),
            None
        );
        assert_eq!(
            fs::metadata(copy.join(ARCHIVE))
                .unwrap()
                .modified()
                .unwrap(),
            before
        );
        let _ = fs::remove_dir_all(root);
    }

    /// The copy before is removed only once the new one is in place and checked: a copy
    /// that fails leaves the old one whole, and no half-made folder.
    #[test]
    fn the_previous_copy_stays_until_the_new_one_is_checked() {
        let root = scratch("replace");
        let (media, usb, work) = (root.join("hdd"), root.join("usb"), root.join("work"));
        let keys = keys(&root);
        nightly(&media, "snapshot-1700000000000000-1", "first");
        copy_newest_with(&media, &usb, &keys, &work, &plenty).unwrap();
        let first = usb.join(FOLDER).join("snapshot-1700000000000000-1");
        assert!(copied(&first, "snapshot-1700000000000000-1"));
        nightly(&media, "snapshot-1700086400000000-2", "second");
        // A certificate that can't seal anything: the new copy fails.
        let cert = keys.join(crate::maintenance::SECRETS_CERT);
        let good = fs::read(&cert).unwrap();
        fs::write(&cert, b"not a certificate").unwrap();
        assert!(copy_newest_with(&media, &usb, &keys, &work, &plenty).is_err());
        assert_eq!(names(&usb.join(FOLDER)), ["snapshot-1700000000000000-1"]);
        assert!(copied(&first, "snapshot-1700000000000000-1"));
        fs::write(&cert, good).unwrap();
        copy_newest_with(&media, &usb, &keys, &work, &plenty).unwrap();
        assert_eq!(names(&usb.join(FOLDER)), ["snapshot-1700086400000000-2"]);
        let _ = fs::remove_dir_all(root);
    }

    /// Without a backup password the settings are not put on the drive unprotected, and
    /// without room they are left off: each is said in plain words, and nothing is written.
    #[test]
    fn app_settings_without_a_key_or_room_are_said_and_not_written() {
        let root = scratch("refused");
        let (media, usb, work) = (root.join("hdd"), root.join("usb"), root.join("work"));
        let no_keys = root.join("no-keys");
        private(&no_keys);
        nightly(&media, "snapshot-1700000000000000-1", "secret");
        let said = copy_newest_with(&media, &usb, &no_keys, &work, &plenty)
            .unwrap()
            .unwrap();
        assert_eq!(said, NO_KEY);
        assert!(!usb.join(FOLDER).exists());
        let keys = keys(&root);
        let small = |_: &Path| -> Result<u128> { Ok(3_000_000) };
        let said = copy_newest_with(&media, &usb, &keys, &work, &small)
            .unwrap()
            .unwrap();
        assert!(
            said.starts_with("the app settings did not fit on the USB drive ("),
            "{said}"
        );
        assert!(said.contains("3 MB free"), "{said}");
        assert!(
            said.contains("the photos and the other backups were copied first"),
            "{said}"
        );
        assert_eq!(names(&usb.join(FOLDER)), Vec::<String>::new());
        // Before the first nightly backup: nothing to copy, and that is not a problem.
        let empty = root.join("empty-hdd");
        assert_eq!(
            copy_newest_with(&empty, &usb, &keys, &work, &plenty).unwrap(),
            None
        );
        let _ = fs::remove_dir_all(root);
    }
}
