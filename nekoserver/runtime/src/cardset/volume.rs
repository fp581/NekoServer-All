//! Attaching a card: it must be the mounted, approved card of the set before anything is
//! written, and it is checked again before every publication (format document,
//! sections 3 and 9; integration report C1, C12, C13).
//!
//! "Never write into an unmounted mount folder": the card's root folder is opened once,
//! after these checks. Every later call is relative to that open folder, so a card that
//! is pulled makes the calls fail instead of reaching the empty folder underneath, and
//! `confirm` notices that the path no longer leads to the folder that was opened.
use super::error::{CardError, Kind, Res};
use super::format::{self, Identity, Txn};
use super::fsat::{Dir, Room, Stat};
use super::source::hash_reader;
use std::path::{Path, PathBuf};

/// How the card's folder must relate to its parent.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MountRule {
    /// The folder is a mount point: its device differs from its parent's.
    MountPoint,
    /// Unit tests only: a plain folder stands for the mounted card (the device must still
    /// be the expected one). It does not exist in a build that is installed.
    #[cfg(test)]
    TestFolder,
}

/// What the catalogue knows about the volume, checked against what is attached.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Expect {
    /// The device number the mounted filesystem must have (the approved block device).
    pub device: u64,
    pub backup_set_id: String,
    pub volume_id: String,
    /// The filesystem's UUID as the system reads it now (blkid), compared with the one
    /// identity.json was enrolled on.
    pub filesystem_uuid: String,
    pub rule: MountRule,
}

/// What enrolment writes into identity.json.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Enrolment {
    pub backup_set_id: String,
    pub volume_id: String,
    pub sequence_number: u64,
    pub set_display_name: String,
    pub filesystem_type: String,
    pub filesystem_uuid: String,
    pub privacy_acknowledged_at: String,
    /// The standalone restore tool (the release's neko_restore.py), written onto the card
    /// as NekoBackup/tools/neko-restore.py so that the card can be read without NekoServer.
    pub restore_tool: Vec<u8>,
}

#[derive(Debug)]
pub struct Volume {
    path: PathBuf,
    root: Dir,
    root_stat: Stat,
    rule: MountRule,
    pub nb: Dir,
    pub staging: Dir,
    pub objects: Dir,
    pub records: Dir,
    pub identity: Identity,
}

fn open_root(path: &Path, device: u64, rule: MountRule) -> Res<(Dir, Stat)> {
    let root = Dir::open_path(path)?;
    let st = root.stat()?;
    if rule == MountRule::MountPoint {
        let parent = path
            .parent()
            .ok_or_else(|| CardError::new(Kind::NotMounted, "the card's folder has no parent"))?;
        if Dir::open_path(parent)?.stat()?.dev == st.dev {
            return Err(CardError::new(
                Kind::NotMounted,
                "the card is not mounted on its folder: nothing is written there",
            ));
        }
    }
    if st.dev != device {
        return Err(CardError::new(
            Kind::WrongVolume,
            "the folder is on another device than the approved card",
        ));
    }
    Ok((root, st))
}

fn not_enrolled(err: CardError) -> CardError {
    if err.errno == Some(libc::ENOENT) {
        return CardError::new(
            Kind::WrongVolume,
            "this card has no NekoBackup folder: it must be enrolled first",
        );
    }
    err
}

fn check_identity(identity: &Identity, expect: &Expect) -> Res<()> {
    if identity.backup_set_id != expect.backup_set_id {
        return Err(CardError::new(
            Kind::WrongVolume,
            "this card belongs to another backup set",
        ));
    }
    if identity.volume_id != expect.volume_id {
        return Err(CardError::new(
            Kind::WrongVolume,
            "this is another card of the set than the one expected",
        ));
    }
    if identity.filesystem_uuid != expect.filesystem_uuid {
        return Err(CardError::new(
            Kind::WrongVolume,
            "identity conflict: this card's NekoBackup was made on another filesystem",
        ));
    }
    Ok(())
}

/// Where the standalone restore tool goes on a card (archive format 1, section 2).
pub const TOOLS: &str = "tools";
pub const RESTORE_TOOL: &str = "neko-restore.py";

const README_TEXT: &str = "\
This folder is a NekoBackup volume: one card of a numbered backup set.

The files are NOT encrypted. Anyone holding this card can read them.

To get files back, on any Linux computer, with no NekoServer and no network
(Python 3, which almost every Linux has, runs the tool; nothing to install):

  python3 NekoBackup/tools/neko-restore.py info
  python3 NekoBackup/tools/neko-restore.py list --search 2021
  python3 NekoBackup/tools/neko-restore.py verify
  python3 NekoBackup/tools/neko-restore.py restore /home/me/restored --search 2021
  python3 NekoBackup/tools/neko-restore.py where beach

Run them from the card's top folder (or give the card with --card). restore never
overwrites a file of yours: one whose place is taken is skipped, or put under
_renamed/ with --rename, and the tool says where each file went. Of several
versions of one file, the newest gets its name. Each file is checked against
its record before it gets its name. 'where' tells, from this card's inventory,
which card of the set held a file when this card was last written.

Without the tool: every file under objects/ is an ordinary copy of one original
photo, video or document and can be opened or copied directly. Its original name
and folder are in the file of the same name (ending in .json) under records/,
with its size and its SHA-256 checksum (sha256sum shows the file's own).

This card holds only its own files; the other cards of the set hold the rest.
These are original media files, not Immich's accounts, albums or sharing.
An ext4 card needs a computer that reads ext4 (any Linux does).
";

impl Volume {
    /// Attach an enrolled card that must be `expect`.
    pub fn attach(path: &Path, expect: &Expect) -> Res<Volume> {
        let (root, root_stat) = open_root(path, expect.device, expect.rule)?;
        let nb = root.open_dir(format::ROOT).map_err(not_enrolled)?;
        let identity = Identity::parse(&nb.read_limited(format::IDENTITY, format::SMALL_FILE)?)?;
        check_identity(&identity, expect)?;
        Ok(Volume {
            path: path.to_path_buf(),
            staging: nb.open_dir(format::STAGING)?,
            objects: nb.open_dir(format::OBJECTS)?,
            records: nb.open_dir(format::RECORDS)?,
            root,
            root_stat,
            rule: expect.rule,
            nb,
            identity,
        })
    }

    /// Enrol a card: make NekoBackup with identity.json and its folders. Refused when
    /// any folder of that name (ignoring case) is already there; it is never adopted.
    pub fn enrol(path: &Path, expect: &Expect, e: &Enrolment) -> Res<Volume> {
        let (root, _) = open_root(path, expect.device, expect.rule)?;
        let taken = root
            .names()?
            .iter()
            .any(|n| n.eq_ignore_ascii_case(format::ROOT));
        if taken {
            return Err(CardError::new(
                Kind::Exists,
                "a NekoBackup folder is already on this card: it is not adopted",
            ));
        }
        if e.restore_tool.is_empty() {
            return Err(CardError::new(
                Kind::Invalid,
                "the restore tool is missing: a card is never enrolled without it",
            ));
        }
        let identity = Identity {
            backup_set_id: e.backup_set_id.clone(),
            volume_id: e.volume_id.clone(),
            sequence_number: e.sequence_number,
            set_display_name: e.set_display_name.clone(),
            created_at: format::now(),
            filesystem_type: e.filesystem_type.clone(),
            filesystem_uuid: e.filesystem_uuid.clone(),
            privacy_acknowledged_at: e.privacy_acknowledged_at.clone(),
        };
        let nb = root.ensure_dir(format::ROOT)?;
        for name in [
            format::STAGING,
            format::OBJECTS,
            format::RECORDS,
            format::INVENTORY,
        ] {
            nb.ensure_dir(name)?;
        }
        nb.ensure_dir(TOOLS)?
            .write_new(RESTORE_TOOL, &e.restore_tool)?;
        nb.write_new(format::README, README_TEXT.as_bytes())?;
        nb.write_new(format::IDENTITY, &identity.to_bytes())?;
        root.sync()?;
        Volume::attach(path, expect)
    }

    /// The check of "before any transaction", repeated before D5 and D6: the path still
    /// leads to the folder that was opened, and identity.json is still the same.
    pub fn confirm(&self) -> Res<()> {
        let changed = |what: &str| CardError::new(Kind::MountChanged, what);
        let now = Dir::open_path(&self.path)
            .and_then(|d| d.stat())
            .map_err(|_| changed("the card's folder is gone: the card was removed"))?;
        if now.dev != self.root_stat.dev || now.ino != self.root_stat.ino {
            return Err(changed(
                "the card's folder now leads somewhere else: the card was removed",
            ));
        }
        let bytes = self
            .nb
            .read_limited(format::IDENTITY, format::SMALL_FILE)
            .map_err(|_| changed("identity.json can no longer be read"))?;
        if Identity::parse(&bytes).ok().as_ref() != Some(&self.identity) {
            return Err(changed("identity.json changed while the card was attached"));
        }
        Ok(())
    }

    /// The card can be written: not read-only, and the folder is still the card.
    pub fn ready_to_write(&self) -> Res<()> {
        self.confirm()?;
        if self.room()?.read_only {
            return Err(CardError::new(Kind::Io, "the card is mounted read-only"));
        }
        Ok(())
    }

    pub fn room(&self) -> Res<Room> {
        self.root.room()
    }

    pub fn device(&self) -> u64 {
        self.root_stat.dev
    }

    /// A source root that is on the card, or that holds the card's folder, overlaps it.
    /// On a mounted card the device decides; a test folder shares its device with
    /// everything else, so there only the paths do.
    pub fn overlaps(&self, source_root: &Path, source_device: u64) -> bool {
        let nested = source_root.starts_with(&self.path) || self.path.starts_with(source_root);
        nested || (self.rule == MountRule::MountPoint && source_device == self.root_stat.dev)
    }

    pub fn volume_id(&self) -> &str {
        &self.identity.volume_id
    }

    /// `objects/<hh>/`, made when first needed (D3).
    pub fn objects_dir(&self, hh: &str) -> Res<Dir> {
        self.objects.ensure_dir(hh)
    }

    /// `records/<hh>/`, made when first needed (D3).
    pub fn records_dir(&self, hh: &str) -> Res<Dir> {
        self.records.ensure_dir(hh)
    }

    /// The payload of `txn` is on the card with this checksum and size (read in full).
    pub fn payload_matches(&self, txn: &Txn, checksum: &str, size: u64) -> Res<bool> {
        let Some(dir) = self.existing(&self.objects, txn.hh())? else {
            return Ok(false);
        };
        let file = match dir.open_from_card(txn.payload_name()) {
            Ok(f) => f,
            Err(e) if e.errno == Some(libc::ENOENT) => return Ok(false),
            Err(e) => return Err(e),
        };
        let (sum, n) = hash_reader(file)?;
        Ok(sum == checksum && n == size)
    }

    /// `objects/<hh>/` or `records/<hh>/` when it exists, without making it.
    pub fn existing(&self, top: &Dir, hh: &str) -> Res<Option<Dir>> {
        if !top.exists(hh)? {
            return Ok(None);
        }
        top.open_dir(hh).map(Some)
    }
}
