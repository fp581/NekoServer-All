//! What the engine needs from the server's catalogue: the transaction row of D1, the
//! all-or-nothing import of D7, and the lookups a normal run makes.
//!
//! The decided store is SQLite inside the runtime (Q2); it is built with the rest of the
//! catalogue (inventory, rebuild, search) in the next step. `Memory` keeps the same
//! contract in memory: every method is one all-or-nothing change, as one SQLite
//! transaction will be. The cards' records stay the authority either way.
use super::error::Res;
use super::format::Record;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TxRow {
    pub transaction_id: String,
    pub run_id: String,
    pub record_id: String,
    pub version_id: String,
    pub volume_id: String,
    pub source_id: String,
    pub path: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CopyState {
    /// Committed on its card, its payload hashed against the record.
    Committed,
    /// The record is there but the payload is missing or has other bytes.
    Damaged,
    /// The catalogue knew it, the card no longer has it.
    Missing,
    /// The person marked the card lost.
    Lost,
}

impl CopyState {
    pub fn protects(self) -> bool {
        self == CopyState::Committed
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VersionRow {
    pub version_id: String,
    pub source_id: String,
    pub path: String,
    pub byte_size: u64,
    pub mtime_ns: i64,
    pub ctime_ns: i64,
    pub checksum: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CopyRow {
    pub record_id: String,
    pub version_id: String,
    pub volume_id: String,
    pub state: CopyState,
    pub verified_at: Option<String>,
}

/// A payload on a card that no trusted record names (format document, section 10).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OrphanRow {
    pub volume_id: String,
    pub transaction_id: String,
    pub archive_path: String,
}

pub trait Catalogue {
    /// D1: the transaction row, durable before anything is written on the card.
    fn begin(&mut self, row: &TxRow) -> Res<()>;
    /// Forget a transaction that left nothing committed.
    fn drop_tx(&mut self, transaction_id: &str) -> Res<()>;
    /// D7: version, copy and "verified now" in one change, the transaction row removed.
    /// Keyed by the record ID: repeating it changes nothing.
    fn commit_copy(&mut self, record: &Record, verified_at: Option<&str>) -> Res<()>;
    /// A copy found damaged or missing, or marked lost.
    fn set_copy_state(&mut self, record_id: &str, state: CopyState) -> Res<()>;
    /// A payload kept as an orphan, with its transaction as the evidence.
    fn add_orphan(&mut self, row: &OrphanRow) -> Res<()>;
    /// An unchanged file seen again with another date: the version notes the new one.
    fn note_dates(&mut self, version_id: &str, mtime_ns: i64, ctime_ns: i64) -> Res<()>;

    fn open_txs(&self, volume_id: &str) -> Vec<TxRow>;
    fn newest_version(&self, source_id: &str, path: &str) -> Option<VersionRow>;
    fn copies_of(&self, version_id: &str) -> Vec<CopyRow>;
    fn copy(&self, record_id: &str) -> Option<CopyRow>;
    fn orphans(&self, volume_id: &str) -> Vec<OrphanRow>;
    fn version_count(&self) -> u64;
}

/// The catalogue contract kept in memory.
#[derive(Debug, Default, Clone)]
pub struct Memory {
    pub txs: Vec<TxRow>,
    pub versions: Vec<VersionRow>,
    pub copies: Vec<CopyRow>,
    pub orphan_rows: Vec<OrphanRow>,
}

impl Catalogue for Memory {
    fn begin(&mut self, row: &TxRow) -> Res<()> {
        self.txs.retain(|t| t.transaction_id != row.transaction_id);
        self.txs.push(row.clone());
        Ok(())
    }
    fn drop_tx(&mut self, transaction_id: &str) -> Res<()> {
        self.txs.retain(|t| t.transaction_id != transaction_id);
        Ok(())
    }
    fn commit_copy(&mut self, record: &Record, verified_at: Option<&str>) -> Res<()> {
        let m = &record.meta;
        let t = &m.txn;
        if !self.versions.iter().any(|v| v.version_id == t.version_id) {
            self.versions.push(VersionRow {
                version_id: t.version_id.clone(),
                source_id: t.source_id.clone(),
                path: t.original_relative_path.clone(),
                byte_size: m.byte_size,
                mtime_ns: t.source_mtime_ns,
                ctime_ns: t.source_identity.ctime_ns,
                checksum: m.checksum.clone(),
            });
        }
        self.copies.retain(|c| c.record_id != t.record_id);
        self.copies.push(CopyRow {
            record_id: t.record_id.clone(),
            version_id: t.version_id.clone(),
            volume_id: t.volume_id.clone(),
            state: CopyState::Committed,
            verified_at: verified_at.map(str::to_string),
        });
        self.txs.retain(|r| r.transaction_id != t.transaction_id);
        Ok(())
    }
    fn set_copy_state(&mut self, record_id: &str, state: CopyState) -> Res<()> {
        for c in self.copies.iter_mut().filter(|c| c.record_id == record_id) {
            c.state = state;
        }
        Ok(())
    }
    fn add_orphan(&mut self, row: &OrphanRow) -> Res<()> {
        if !self.orphan_rows.contains(row) {
            self.orphan_rows.push(row.clone());
        }
        Ok(())
    }
    fn note_dates(&mut self, version_id: &str, mtime_ns: i64, ctime_ns: i64) -> Res<()> {
        for v in self
            .versions
            .iter_mut()
            .filter(|v| v.version_id == version_id)
        {
            v.mtime_ns = mtime_ns;
            v.ctime_ns = ctime_ns;
        }
        Ok(())
    }
    fn open_txs(&self, volume_id: &str) -> Vec<TxRow> {
        self.txs
            .iter()
            .filter(|t| t.volume_id == volume_id)
            .cloned()
            .collect()
    }
    fn newest_version(&self, source_id: &str, path: &str) -> Option<VersionRow> {
        self.versions
            .iter()
            .rev()
            .find(|v| v.source_id == source_id && v.path == path)
            .cloned()
    }
    fn copies_of(&self, version_id: &str) -> Vec<CopyRow> {
        self.copies
            .iter()
            .filter(|c| c.version_id == version_id)
            .cloned()
            .collect()
    }
    fn copy(&self, record_id: &str) -> Option<CopyRow> {
        self.copies
            .iter()
            .find(|c| c.record_id == record_id)
            .cloned()
    }
    fn orphans(&self, volume_id: &str) -> Vec<OrphanRow> {
        self.orphan_rows
            .iter()
            .filter(|o| o.volume_id == volume_id)
            .cloned()
            .collect()
    }
    fn version_count(&self) -> u64 {
        self.versions.len() as u64
    }
}
