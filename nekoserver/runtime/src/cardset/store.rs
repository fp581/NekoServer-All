//! The server's catalogue in SQLite (decision Q2): what the cards hold, searchable, and
//! what the engine needs for D1 and D7 (the `Catalogue` contract).
//!
//! Each change is one SQLite transaction, so a killed process leaves either the state
//! before it or the state after it. The database runs in WAL mode with `synchronous =
//! FULL`: a committed change survives a power cut of the server. The file is the
//! server's own (mode 0600); it holds paths of the person's files but no credentials.
//! For the host backup, `export` writes a consistent copy (`VACUUM INTO`), never a copy
//! of the live file without its WAL.
//!
//! The cards' records stay the authority: the catalogue can be thrown away and rebuilt
//! from them (`rebuild.rs`). Every rebuild starts a new catalogue *epoch*; the inventory
//! snapshots written to cards carry the epoch and a revision that counts up inside it,
//! so that revisions of different histories are never compared.
use super::catalogue::{Catalogue, CopyRow, CopyState, OrphanRow, TxRow, VersionRow};
use super::error::{CardError, Kind, Res};
use super::format::{self, Identity, Record};
use rusqlite::{params, Connection, OptionalExtension, Row, Transaction};
use std::path::Path;

/// The layout of the tables; a database of another layout is not used.
pub const SCHEMA_VERSION: i64 = 1;

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS volumes (
    volume_id TEXT PRIMARY KEY,
    backup_set_id TEXT NOT NULL,
    sequence_number INTEGER NOT NULL,
    label TEXT NOT NULL,
    filesystem_type TEXT NOT NULL,
    filesystem_uuid TEXT NOT NULL,
    enrolled_at TEXT NOT NULL,
    state TEXT NOT NULL,
    state_since TEXT NOT NULL,
    last_seen_at TEXT,
    last_recheck_at TEXT,
    last_recheck_result TEXT,
    capacity_bytes INTEGER NOT NULL,
    observed INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    retired_at TEXT
);
CREATE TABLE IF NOT EXISTS versions (
    seq INTEGER PRIMARY KEY,
    version_id TEXT NOT NULL UNIQUE,
    source_id TEXT NOT NULL,
    path TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    ctime_ns INTEGER NOT NULL,
    checksum TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS versions_by_path ON versions (source_id, path);
CREATE TABLE IF NOT EXISTS copies (
    record_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES versions (version_id),
    volume_id TEXT NOT NULL,
    archive_path TEXT NOT NULL,
    state TEXT NOT NULL,
    verified_at TEXT
);
CREATE INDEX IF NOT EXISTS copies_by_version ON copies (version_id);
CREATE INDEX IF NOT EXISTS copies_by_volume ON copies (volume_id);
CREATE TABLE IF NOT EXISTS txs (
    transaction_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    volume_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orphans (
    volume_id TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    archive_path TEXT NOT NULL,
    PRIMARY KEY (volume_id, transaction_id)
);
CREATE TABLE IF NOT EXISTS conflicts (
    seq INTEGER PRIMARY KEY,
    found_at TEXT NOT NULL,
    what TEXT NOT NULL
);
";

/// A volume's state in the catalogue (archive format 1, section 6).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VolumeState {
    InUse,
    Unavailable,
    Lost,
    Retired,
    Damaged,
    IdentityConflict,
}

impl VolumeState {
    pub fn as_str(self) -> &'static str {
        match self {
            VolumeState::InUse => "in_use",
            VolumeState::Unavailable => "unavailable",
            VolumeState::Lost => "lost",
            VolumeState::Retired => "retired",
            VolumeState::Damaged => "damaged",
            VolumeState::IdentityConflict => "identity_conflict",
        }
    }
    pub fn parse(s: &str) -> Option<VolumeState> {
        [
            VolumeState::InUse,
            VolumeState::Unavailable,
            VolumeState::Lost,
            VolumeState::Retired,
            VolumeState::Damaged,
            VolumeState::IdentityConflict,
        ]
        .into_iter()
        .find(|v| v.as_str() == s)
    }
    /// A state a person or a check set: no snapshot may turn it back into a working one.
    pub fn is_final(self) -> bool {
        matches!(
            self,
            VolumeState::Lost | VolumeState::Retired | VolumeState::Damaged
        )
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VolumeRow {
    pub volume_id: String,
    pub backup_set_id: String,
    pub sequence_number: u64,
    pub label: String,
    pub filesystem_type: String,
    pub filesystem_uuid: String,
    pub enrolled_at: String,
    pub state: VolumeState,
    pub state_since: String,
    pub last_seen_at: Option<String>,
    pub last_recheck_at: Option<String>,
    pub last_recheck_result: Option<String>,
    pub capacity_bytes: u64,
    /// Seen attached by this catalogue; false when known only from an inventory snapshot.
    pub observed: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SourceRow {
    pub source_id: String,
    pub display_name: String,
    /// `immich-originals`, `nextcloud-files` or `other`.
    pub kind: String,
    pub created_at: String,
    pub retired_at: Option<String>,
}

/// One search result: a version and where its copies are.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Found {
    pub version: VersionRow,
    pub copies: Vec<(CopyRow, Option<String>)>,
}

pub fn db(what: &str, e: rusqlite::Error) -> CardError {
    CardError::new(Kind::Io, &format!("the catalogue: {what}: {e}"))
}

fn copy_state_str(state: CopyState) -> &'static str {
    match state {
        CopyState::Committed => "committed",
        CopyState::Damaged => "damaged",
        CopyState::Missing => "missing",
        CopyState::Lost => "lost",
        CopyState::Historical => "historical",
    }
}

pub fn copy_state(s: &str) -> Option<CopyState> {
    [
        CopyState::Committed,
        CopyState::Damaged,
        CopyState::Missing,
        CopyState::Lost,
        CopyState::Historical,
    ]
    .into_iter()
    .find(|c| copy_state_str(*c) == s)
}

pub fn copy_state_name(state: CopyState) -> &'static str {
    copy_state_str(state)
}

fn number(n: i64) -> u64 {
    u64::try_from(n).unwrap_or(0)
}

fn signed(n: u64) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

fn version_row(row: &Row) -> rusqlite::Result<VersionRow> {
    Ok(VersionRow {
        version_id: row.get(0)?,
        source_id: row.get(1)?,
        path: row.get(2)?,
        byte_size: number(row.get(3)?),
        mtime_ns: row.get(4)?,
        ctime_ns: row.get(5)?,
        checksum: row.get(6)?,
    })
}

const VERSION_COLUMNS: &str =
    "version_id, source_id, path, byte_size, mtime_ns, ctime_ns, checksum";

fn copy_row(row: &Row) -> rusqlite::Result<CopyRow> {
    let state: String = row.get(3)?;
    Ok(CopyRow {
        record_id: row.get(0)?,
        version_id: row.get(1)?,
        volume_id: row.get(2)?,
        // An unknown word is never taken for a healthy copy.
        state: copy_state(&state).unwrap_or(CopyState::Missing),
        verified_at: row.get(4)?,
    })
}

const COPY_COLUMNS: &str = "record_id, version_id, volume_id, state, verified_at";

fn volume_row(row: &Row) -> rusqlite::Result<VolumeRow> {
    let state: String = row.get(7)?;
    Ok(VolumeRow {
        volume_id: row.get(0)?,
        backup_set_id: row.get(1)?,
        sequence_number: number(row.get(2)?),
        label: row.get(3)?,
        filesystem_type: row.get(4)?,
        filesystem_uuid: row.get(5)?,
        enrolled_at: row.get(6)?,
        // An unknown word is never taken for a working volume.
        state: VolumeState::parse(&state).unwrap_or(VolumeState::Unavailable),
        state_since: row.get(8)?,
        last_seen_at: row.get(9)?,
        last_recheck_at: row.get(10)?,
        last_recheck_result: row.get(11)?,
        capacity_bytes: number(row.get(12)?),
        observed: row.get::<_, i64>(13)? != 0,
    })
}

const VOLUME_COLUMNS: &str = "volume_id, backup_set_id, sequence_number, label, filesystem_type, \
     filesystem_uuid, enrolled_at, state, state_since, last_seen_at, last_recheck_at, \
     last_recheck_result, capacity_bytes, observed";

/// `text` for a LIKE pattern: its own `%`, `_` and `\` match only themselves.
fn like_pattern(text: &str) -> String {
    let mut out = String::from("%");
    for c in text.chars() {
        if matches!(c, '%' | '_' | '\\') {
            out.push('\\');
        }
        out.push(c);
    }
    out.push('%');
    out
}

/// D7 inside an open transaction: the version once, the copy keyed by its record.
fn insert_copy(
    tx: &Transaction,
    record: &Record,
    state: CopyState,
    verified_at: Option<&str>,
) -> rusqlite::Result<()> {
    let m = &record.meta;
    let t = &m.txn;
    tx.execute(
        "INSERT OR IGNORE INTO versions (version_id, source_id, path, byte_size, mtime_ns, \
         ctime_ns, checksum) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        params![
            t.version_id,
            t.source_id,
            t.original_relative_path,
            signed(m.byte_size),
            t.source_mtime_ns,
            t.source_identity.ctime_ns,
            m.checksum
        ],
    )?;
    tx.execute(
        "INSERT INTO copies (record_id, version_id, volume_id, archive_path, state, verified_at) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6) ON CONFLICT (record_id) DO UPDATE SET \
         state = excluded.state, verified_at = excluded.verified_at",
        params![
            t.record_id,
            t.version_id,
            t.volume_id,
            t.archive_relative_path,
            copy_state_str(state),
            verified_at
        ],
    )?;
    tx.execute(
        "DELETE FROM txs WHERE transaction_id = ?1",
        params![t.transaction_id],
    )?;
    Ok(())
}

/// A new, empty file at `path` that only its owner can read (0600); Exists if one is there.
fn private_file(path: &Path) -> Res<()> {
    use std::os::unix::fs::OpenOptionsExt;
    match std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)
    {
        Ok(_) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => Err(CardError::new(
            Kind::Exists,
            "the catalogue's file exists already",
        )),
        Err(e) => Err(CardError::io("the catalogue's file", &e)),
    }
}

pub struct Store {
    conn: Connection,
}

impl Store {
    /// Open (or make) the catalogue at `path`. A new file is the server's own (0600).
    pub fn open(path: &Path) -> Res<Store> {
        if !path.exists() {
            // Made the server's own before SQLite writes a byte into it (an empty file is a
            // new database to SQLite): made by SQLite, it was readable by others for a moment.
            match private_file(path) {
                Ok(()) => {}
                Err(e) if e.kind == Kind::Exists => {}
                Err(e) => return Err(e),
            }
        }
        let conn = Connection::open(path).map_err(|e| db("open", e))?;
        Store::prepare(conn)
    }

    /// A catalogue in memory (tests, and the rebuild's dry run).
    pub fn in_memory() -> Res<Store> {
        Store::prepare(Connection::open_in_memory().map_err(|e| db("open", e))?)
    }

    fn prepare(conn: Connection) -> Res<Store> {
        conn.pragma_update(None, "journal_mode", "WAL")
            .map_err(|e| db("journal mode", e))?;
        conn.pragma_update(None, "synchronous", "FULL")
            .map_err(|e| db("synchronous", e))?;
        conn.pragma_update(None, "foreign_keys", "ON")
            .map_err(|e| db("foreign keys", e))?;
        conn.busy_timeout(std::time::Duration::from_secs(10))
            .map_err(|e| db("busy timeout", e))?;
        let mut store = Store { conn };
        let tx = store.begin_tx()?;
        tx.execute_batch(SCHEMA).map_err(|e| db("schema", e))?;
        let found: Option<String> = tx
            .query_row(
                "SELECT value FROM meta WHERE key = 'schema_version'",
                [],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| db("schema version", e))?;
        match found {
            None => {
                tx.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?1), \
                     ('epoch_id', ?2), ('revision', '0')",
                    params![SCHEMA_VERSION.to_string(), format::new_uuid()?],
                )
                .map_err(|e| db("first epoch", e))?;
            }
            Some(v) if v == SCHEMA_VERSION.to_string() => {}
            Some(v) => {
                return Err(CardError::new(
                    Kind::Unsupported,
                    &format!(
                        "the catalogue has layout {v}; this version reads layout {SCHEMA_VERSION}"
                    ),
                ))
            }
        }
        tx.commit().map_err(|e| db("commit", e))?;
        Ok(store)
    }

    fn begin_tx(&mut self) -> Res<Transaction<'_>> {
        self.conn.transaction().map_err(|e| db("begin", e))
    }

    fn meta(&self, key: &str) -> Res<String> {
        self.conn
            .query_row("SELECT value FROM meta WHERE key = ?1", params![key], |r| {
                r.get(0)
            })
            .map_err(|e| db(key, e))
    }

    /// The catalogue's current epoch.
    pub fn epoch(&self) -> Res<String> {
        self.meta("epoch_id")
    }

    /// The next inventory revision of this epoch, kept before it is used: a revision is
    /// never written twice, even when the snapshot it was taken for is never finished.
    pub fn take_revision(&mut self) -> Res<(String, u64)> {
        let tx = self.begin_tx()?;
        tx.execute(
            "UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'revision'",
            [],
        )
        .map_err(|e| db("revision", e))?;
        let (epoch, revision): (String, String) = tx
            .query_row(
                "SELECT (SELECT value FROM meta WHERE key = 'epoch_id'), \
                 (SELECT value FROM meta WHERE key = 'revision')",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .map_err(|e| db("revision", e))?;
        tx.commit().map_err(|e| db("commit", e))?;
        let revision = revision.parse().map_err(|_| {
            CardError::new(Kind::Invalid, "the catalogue's revision is not a number")
        })?;
        Ok((epoch, revision))
    }

    /// A rebuilt catalogue is another history: a new epoch, revisions from 1 again.
    pub fn start_epoch(&mut self) -> Res<String> {
        let epoch = format::new_uuid()?;
        let tx = self.begin_tx()?;
        tx.execute(
            "UPDATE meta SET value = ?1 WHERE key = 'epoch_id'",
            params![epoch],
        )
        .and_then(|_| tx.execute("UPDATE meta SET value = '0' WHERE key = 'revision'", []))
        .map_err(|e| db("epoch", e))?;
        tx.commit().map_err(|e| db("commit", e))?;
        Ok(epoch)
    }

    /// A card seen attached: added on its first sight, its last sighting noted after.
    /// A volume the person marked lost, retired or damaged keeps that state.
    pub fn saw_volume(&mut self, identity: &Identity, capacity: u64, now: &str) -> Res<()> {
        self.conn
            .execute(
                &format!(
                    "INSERT INTO volumes ({VOLUME_COLUMNS}) VALUES \
                     (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'in_use', ?8, ?8, NULL, NULL, ?9, 1) \
                     ON CONFLICT (volume_id) DO UPDATE SET last_seen_at = excluded.last_seen_at, \
                     capacity_bytes = excluded.capacity_bytes, observed = 1, \
                     state = CASE WHEN state IN ('lost', 'retired', 'damaged', 'identity_conflict') \
                     THEN state ELSE 'in_use' END"
                ),
                params![
                    identity.volume_id,
                    identity.backup_set_id,
                    signed(identity.sequence_number),
                    identity.label(),
                    identity.filesystem_type,
                    identity.filesystem_uuid,
                    identity.created_at,
                    now,
                    signed(capacity)
                ],
            )
            .map_err(|e| db("volume", e))?;
        Ok(())
    }

    /// A volume known only from an inventory snapshot (not attached): added if new, never
    /// changed if the catalogue knows it already.
    pub fn heard_of_volume(&mut self, v: &VolumeRow) -> Res<()> {
        self.conn
            .execute(
                &format!(
                    "INSERT OR IGNORE INTO volumes ({VOLUME_COLUMNS}) VALUES \
                     (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, 0)"
                ),
                params![
                    v.volume_id,
                    v.backup_set_id,
                    signed(v.sequence_number),
                    v.label,
                    v.filesystem_type,
                    v.filesystem_uuid,
                    v.enrolled_at,
                    v.state.as_str(),
                    v.state_since,
                    v.last_seen_at,
                    v.last_recheck_at,
                    v.last_recheck_result,
                    signed(v.capacity_bytes)
                ],
            )
            .map_err(|e| db("volume from an inventory", e))?;
        Ok(())
    }

    /// A person's or a check's decision about a volume. Its copies follow: a lost or
    /// retired volume's copies are lost, a damaged one's damaged; none of them protects.
    pub fn set_volume_state(&mut self, volume_id: &str, state: VolumeState, now: &str) -> Res<()> {
        let copies = match state {
            VolumeState::Lost | VolumeState::Retired => Some(CopyState::Lost),
            VolumeState::Damaged => Some(CopyState::Damaged),
            _ => None,
        };
        let tx = self.begin_tx()?;
        let changed = tx
            .execute(
                "UPDATE volumes SET state = ?2, state_since = ?3 WHERE volume_id = ?1",
                params![volume_id, state.as_str(), now],
            )
            .map_err(|e| db("volume state", e))?;
        if changed == 0 {
            return Err(CardError::new(
                Kind::Invalid,
                "no such volume in the catalogue",
            ));
        }
        if let Some(c) = copies {
            tx.execute(
                "UPDATE copies SET state = ?2 WHERE volume_id = ?1",
                params![volume_id, copy_state_str(c)],
            )
            .map_err(|e| db("copies of a volume", e))?;
        }
        tx.commit().map_err(|e| db("commit", e))
    }

    pub fn volume(&self, volume_id: &str) -> Res<Option<VolumeRow>> {
        self.conn
            .query_row(
                &format!("SELECT {VOLUME_COLUMNS} FROM volumes WHERE volume_id = ?1"),
                params![volume_id],
                volume_row,
            )
            .optional()
            .map_err(|e| db("volume", e))
    }

    pub fn volumes(&self) -> Res<Vec<VolumeRow>> {
        self.rows(
            &format!("SELECT {VOLUME_COLUMNS} FROM volumes ORDER BY sequence_number"),
            [],
            volume_row,
        )
    }

    pub fn add_source(&mut self, s: &SourceRow) -> Res<()> {
        self.conn
            .execute(
                "INSERT OR IGNORE INTO sources (source_id, display_name, kind, created_at, retired_at) \
                 VALUES (?1, ?2, ?3, ?4, ?5)",
                params![s.source_id, s.display_name, s.kind, s.created_at, s.retired_at],
            )
            .map_err(|e| db("source", e))?;
        Ok(())
    }

    pub fn sources(&self) -> Res<Vec<SourceRow>> {
        self.rows(
            "SELECT source_id, display_name, kind, created_at, retired_at FROM sources \
             ORDER BY created_at, source_id",
            [],
            |r| {
                Ok(SourceRow {
                    source_id: r.get(0)?,
                    display_name: r.get(1)?,
                    kind: r.get(2)?,
                    created_at: r.get(3)?,
                    retired_at: r.get(4)?,
                })
            },
        )
    }

    /// Every version, oldest first, for the inventory snapshot.
    pub fn all_versions(&self) -> Res<Vec<VersionRow>> {
        self.rows(
            &format!("SELECT {VERSION_COLUMNS} FROM versions ORDER BY seq"),
            [],
            version_row,
        )
    }

    pub fn copies_on(&self, volume_id: &str) -> Res<Vec<CopyRow>> {
        self.rows(
            &format!("SELECT {COPY_COLUMNS} FROM copies WHERE volume_id = ?1 ORDER BY record_id"),
            params![volume_id],
            copy_row,
        )
    }

    /// A copy known from an inventory snapshot only (its volume's content not observed),
    /// or as the snapshot says it is (lost, damaged, missing). Never over a copy the
    /// catalogue observed itself.
    pub fn heard_of_copy(
        &mut self,
        version: &VersionRow,
        copy: &CopyRow,
        archive_path: &str,
    ) -> Res<()> {
        let tx = self.begin_tx()?;
        tx.execute(
            "INSERT OR IGNORE INTO versions (version_id, source_id, path, byte_size, mtime_ns, \
             ctime_ns, checksum) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![
                version.version_id,
                version.source_id,
                version.path,
                signed(version.byte_size),
                version.mtime_ns,
                version.ctime_ns,
                version.checksum
            ],
        )
        .and_then(|_| {
            tx.execute(
                "INSERT OR IGNORE INTO copies (record_id, version_id, volume_id, archive_path, \
                 state, verified_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                params![
                    copy.record_id,
                    copy.version_id,
                    copy.volume_id,
                    archive_path,
                    copy_state_str(copy.state),
                    copy.verified_at
                ],
            )
        })
        .map_err(|e| db("copy from an inventory", e))?;
        tx.commit().map_err(|e| db("commit", e))
    }

    /// The version of this ID, if the catalogue has it.
    pub fn version(&self, version_id: &str) -> Res<Option<VersionRow>> {
        self.conn
            .query_row(
                &format!("SELECT {VERSION_COLUMNS} FROM versions WHERE version_id = ?1"),
                params![version_id],
                version_row,
            )
            .optional()
            .map_err(|e| db("version", e))
    }

    /// A disagreement kept for a person to review (never resolved by a date).
    pub fn add_conflict(&mut self, what: &str, now: &str) -> Res<()> {
        self.conn
            .execute(
                "INSERT INTO conflicts (found_at, what) VALUES (?1, ?2)",
                params![now, what],
            )
            .map_err(|e| db("conflict", e))?;
        Ok(())
    }

    pub fn conflicts(&self) -> Res<Vec<String>> {
        self.rows("SELECT what FROM conflicts ORDER BY seq", [], |r| r.get(0))
    }

    /// A rebuild's own import of a record observed on a card: the copy keyed by its
    /// record, in `state`.
    pub fn import_copy(
        &mut self,
        record: &Record,
        state: CopyState,
        verified_at: Option<&str>,
    ) -> Res<()> {
        let tx = self.begin_tx()?;
        insert_copy(&tx, record, state, verified_at).map_err(|e| db("import", e))?;
        tx.commit().map_err(|e| db("commit", e))
    }

    /// Versions whose path holds `text` (as typed, any part of the path), newest first,
    /// each with its copies and the label of the volume that holds each.
    pub fn search(&self, text: &str, limit: u32) -> Res<Vec<Found>> {
        let versions = self.rows(
            &format!(
                "SELECT {VERSION_COLUMNS} FROM versions WHERE path LIKE ?1 ESCAPE '\\' \
                 ORDER BY seq DESC LIMIT ?2"
            ),
            params![like_pattern(text), limit],
            version_row,
        )?;
        let mut found = Vec::new();
        for version in versions {
            let copies = self.rows(
                "SELECT c.record_id, c.version_id, c.volume_id, c.state, c.verified_at, v.label \
                 FROM copies c LEFT JOIN volumes v ON v.volume_id = c.volume_id \
                 WHERE c.version_id = ?1 ORDER BY v.sequence_number, c.record_id",
                params![version.version_id],
                |r| Ok((copy_row(r)?, r.get(5)?)),
            )?;
            found.push(Found { version, copies });
        }
        Ok(found)
    }

    /// Versions with no copy that protects them. `true` beside one: no copy is even on
    /// an offline or unobserved volume (lost or damaged everywhere): it cannot be got
    /// back from any known copy.
    pub fn unprotected(&self) -> Res<Vec<(VersionRow, bool)>> {
        let versions = self.rows(
            &format!(
                "SELECT {VERSION_COLUMNS} FROM versions v WHERE NOT EXISTS (SELECT 1 FROM copies c \
                 WHERE c.version_id = v.version_id AND c.state = 'committed') ORDER BY seq"
            ),
            [],
            version_row,
        )?;
        let mut out = Vec::new();
        for v in versions {
            let unrecoverable = self.copies_of(&v.version_id)?.iter().all(|c| {
                matches!(
                    c.state,
                    CopyState::Lost | CopyState::Damaged | CopyState::Missing
                )
            });
            out.push((v, unrecoverable));
        }
        Ok(out)
    }

    /// A consistent copy of the catalogue for the host backup, at `target` (which must not
    /// exist yet): SQLite writes it from one read transaction, WAL included.
    pub fn export(&self, target: &Path) -> Res<()> {
        let path = target
            .to_str()
            .ok_or_else(|| CardError::new(Kind::Invalid, "the export's path is not UTF-8"))?;
        // The server's own, like the catalogue (VACUUM INTO fills an empty file): left to
        // the umask, the paths of the person's files were readable by every user.
        private_file(target)?;
        if let Err(e) = self.conn.execute("VACUUM INTO ?1", params![path]) {
            let _ = std::fs::remove_file(target);
            return Err(db("export", e));
        }
        Ok(())
    }

    fn rows<T, P: rusqlite::Params>(
        &self,
        sql: &str,
        p: P,
        f: impl FnMut(&Row) -> rusqlite::Result<T>,
    ) -> Res<Vec<T>> {
        let mut stmt = self.conn.prepare(sql).map_err(|e| db("query", e))?;
        let rows = stmt.query_map(p, f).map_err(|e| db("query", e))?;
        rows.collect::<rusqlite::Result<Vec<T>>>()
            .map_err(|e| db("row", e))
    }
}

impl Catalogue for Store {
    fn begin(&mut self, row: &TxRow) -> Res<()> {
        self.conn
            .execute(
                "INSERT OR REPLACE INTO txs (transaction_id, run_id, record_id, version_id, \
                 volume_id, source_id, path) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
                params![
                    row.transaction_id,
                    row.run_id,
                    row.record_id,
                    row.version_id,
                    row.volume_id,
                    row.source_id,
                    row.path
                ],
            )
            .map_err(|e| db("transaction row", e))?;
        Ok(())
    }
    fn drop_tx(&mut self, transaction_id: &str) -> Res<()> {
        self.conn
            .execute(
                "DELETE FROM txs WHERE transaction_id = ?1",
                params![transaction_id],
            )
            .map_err(|e| db("transaction row", e))?;
        Ok(())
    }
    fn commit_copy(&mut self, record: &Record, verified_at: Option<&str>) -> Res<()> {
        self.import_copy(record, CopyState::Committed, verified_at)
    }
    fn set_copy_state(&mut self, record_id: &str, state: CopyState) -> Res<()> {
        self.conn
            .execute(
                "UPDATE copies SET state = ?2 WHERE record_id = ?1",
                params![record_id, copy_state_str(state)],
            )
            .map_err(|e| db("copy state", e))?;
        Ok(())
    }
    fn add_orphan(&mut self, row: &OrphanRow) -> Res<()> {
        self.conn
            .execute(
                "INSERT OR IGNORE INTO orphans (volume_id, transaction_id, archive_path) \
                 VALUES (?1, ?2, ?3)",
                params![row.volume_id, row.transaction_id, row.archive_path],
            )
            .map_err(|e| db("orphan", e))?;
        Ok(())
    }
    fn note_dates(&mut self, version_id: &str, mtime_ns: i64, ctime_ns: i64) -> Res<()> {
        self.conn
            .execute(
                "UPDATE versions SET mtime_ns = ?2, ctime_ns = ?3 WHERE version_id = ?1",
                params![version_id, mtime_ns, ctime_ns],
            )
            .map_err(|e| db("dates", e))?;
        Ok(())
    }
    fn open_txs(&self, volume_id: &str) -> Res<Vec<TxRow>> {
        self.rows(
            "SELECT transaction_id, run_id, record_id, version_id, volume_id, source_id, path \
             FROM txs WHERE volume_id = ?1 ORDER BY transaction_id",
            params![volume_id],
            |r| {
                Ok(TxRow {
                    transaction_id: r.get(0)?,
                    run_id: r.get(1)?,
                    record_id: r.get(2)?,
                    version_id: r.get(3)?,
                    volume_id: r.get(4)?,
                    source_id: r.get(5)?,
                    path: r.get(6)?,
                })
            },
        )
    }
    fn newest_version(&self, source_id: &str, path: &str) -> Res<Option<VersionRow>> {
        self.conn
            .query_row(
                &format!(
                    "SELECT {VERSION_COLUMNS} FROM versions WHERE source_id = ?1 AND path = ?2 \
                     ORDER BY seq DESC LIMIT 1"
                ),
                params![source_id, path],
                version_row,
            )
            .optional()
            .map_err(|e| db("newest version", e))
    }
    fn copies_of(&self, version_id: &str) -> Res<Vec<CopyRow>> {
        self.rows(
            &format!("SELECT {COPY_COLUMNS} FROM copies WHERE version_id = ?1 ORDER BY record_id"),
            params![version_id],
            copy_row,
        )
    }
    fn copy(&self, record_id: &str) -> Res<Option<CopyRow>> {
        self.conn
            .query_row(
                &format!("SELECT {COPY_COLUMNS} FROM copies WHERE record_id = ?1"),
                params![record_id],
                copy_row,
            )
            .optional()
            .map_err(|e| db("copy", e))
    }
    fn orphans(&self, volume_id: &str) -> Res<Vec<OrphanRow>> {
        self.rows(
            "SELECT volume_id, transaction_id, archive_path FROM orphans WHERE volume_id = ?1 \
             ORDER BY transaction_id",
            params![volume_id],
            |r| {
                Ok(OrphanRow {
                    volume_id: r.get(0)?,
                    transaction_id: r.get(1)?,
                    archive_path: r.get(2)?,
                })
            },
        )
    }
    fn version_count(&self) -> Res<u64> {
        self.conn
            .query_row("SELECT COUNT(*) FROM versions", [], |r| r.get::<_, i64>(0))
            .map(number)
            .map_err(|e| db("count", e))
    }
}
