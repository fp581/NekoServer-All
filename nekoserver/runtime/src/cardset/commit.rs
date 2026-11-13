//! One file through the commit protocol D1 to D8 (format document, section 9).
//!
//! A copy call returning is never success: the bytes are read back and hashed, the
//! source is checked to be the file that was opened, and only a published record next to
//! a published payload is a committed copy; the catalogue counts it after that (D7).
//!
//! `Hook` is called after every durable step and once in the middle of the payload. The
//! running job passes `no_hook`; the crash tests stop there (a `Kind::Interrupted` error:
//! return at once, clean up nothing, as a killed process would) or inject an error.
use super::catalogue::{Catalogue, TxRow};
use super::error::{CardError, Kind, Res};
use super::format::{self, CopyMeta, Intent, Record, SourceIdentity, Txn};
use super::fsat::{close_checked, Dir};
use super::source::{self, Candidate, Opened};
use super::volume::Volume;
use sha2::{Digest, Sha256};
use std::io::{Read, Write};

/// The points between the steps, in order.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Point {
    /// D1 done: the server's transaction row.
    Begun,
    /// D2 done: the intent on the card.
    IntentWritten,
    /// D3 under way: part of the payload written.
    PayloadStarted,
    /// D3 done: the payload fsynced in staging.
    PayloadWritten,
    /// The readback and source check passed.
    Checked,
    /// D4 done: the verified intent.
    VerifiedWritten,
    /// D5 done: the payload under its final name.
    Published,
    /// D6 under way: the record's temporary file written, not published.
    RecordTempWritten,
    /// D6 done: the record published. The copy is committed on the card.
    RecordPublished,
    /// D7 done: the catalogue counts the copy.
    CatalogueCommitted,
    /// D8 done: the staging files removed.
    CleanedUp,
}

pub trait Hook {
    fn at(&mut self, point: Point) -> Res<()>;
}

impl<F: FnMut(Point) -> Res<()>> Hook for F {
    fn at(&mut self, point: Point) -> Res<()> {
        self(point)
    }
}

/// The hook of a real run: nothing happens between the steps.
pub fn no_hook(_: Point) -> Res<()> {
    Ok(())
}

pub fn intent_name(tx: &str) -> String {
    format!("{tx}.intent.json")
}
pub fn partial_name(tx: &str) -> String {
    format!("{tx}.partial")
}
pub fn verified_name(tx: &str) -> String {
    format!("{tx}.verified.json")
}
pub fn record_tmp_name(tx: &str) -> String {
    format!("{tx}.record.tmp")
}

/// A new copy of a version that already exists (its other copies are lost or damaged):
/// the bytes must equal that version's checksum, and the record keeps its version ID.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Replace {
    pub version_id: String,
    pub record_id: Option<String>,
    pub checksum: String,
}

pub struct Job<'a> {
    pub cand: &'a Candidate,
    pub run_id: &'a str,
    pub replace: Option<&'a Replace>,
}

#[derive(Debug)]
pub enum Outcome {
    /// Committed on the card and counted by the catalogue. A failed D8 is reported here;
    /// it does not undo the commit.
    Committed {
        record: Box<Record>,
        cleanup_failed: Option<CardError>,
    },
    /// Nothing committed and this transaction's staging files removed: the file is
    /// still pending (the source changed, the readback differed, the card is full ...).
    Deferred(CardError),
}

/// Copy one candidate onto the card. `Err` means the card can no longer be trusted for
/// this run (removed, publication refused, a crash): what is on it is left for
/// reconciliation at the next attach.
pub fn commit_file(
    vol: &Volume,
    cat: &mut dyn Catalogue,
    job: &Job,
    hook: &mut dyn Hook,
) -> Res<Outcome> {
    let opened = match source::open(job.cand) {
        Ok(o) => o,
        Err(e) => return Ok(Outcome::Deferred(e)),
    };
    vol.ready_to_write()?;
    let intent = new_intent(vol, job, &opened)?;
    let txn = &intent.txn;
    cat.begin(&TxRow {
        transaction_id: txn.transaction_id.clone(),
        run_id: txn.run_id.clone(),
        record_id: txn.record_id.clone(),
        version_id: txn.version_id.clone(),
        volume_id: txn.volume_id.clone(),
        source_id: txn.source_id.clone(),
        path: txn.original_relative_path.clone(),
    })?;
    hook.at(Point::Begun)?;
    match stage(vol, &intent, &opened, &job.cand.source.display_name, hook) {
        Ok(meta) => finish(vol, cat, &meta, Start::Staging, hook),
        Err(e) if e.is_crash() || e.kind == Kind::MountChanged => Err(e),
        Err(e) => {
            discard(vol, cat, txn)?;
            Ok(Outcome::Deferred(e))
        }
    }
}

fn new_intent(vol: &Volume, job: &Job, opened: &Opened) -> Res<Intent> {
    let record_id = format::new_uuid()?;
    let version_id = match job.replace {
        Some(r) => r.version_id.clone(),
        None => format::new_uuid()?,
    };
    let ext = format::safe_extension(job.cand.path.rsplit('/').next().unwrap_or(""));
    let txn = Txn {
        transaction_id: format::new_uuid()?,
        run_id: job.run_id.to_string(),
        archive_relative_path: format::archive_path(&record_id, &ext),
        record_id,
        version_id,
        replaces_record_id: job.replace.and_then(|r| r.record_id.clone()),
        backup_set_id: vol.identity.backup_set_id.clone(),
        volume_id: vol.identity.volume_id.clone(),
        source_id: job.cand.source.source_id.clone(),
        original_relative_path: job.cand.path.clone(),
        source_mtime_ns: opened.before.mtime_ns,
        source_identity: SourceIdentity {
            device: opened.before.dev,
            inode: opened.before.ino,
            ctime_ns: opened.before.ctime_ns,
        },
    };
    Ok(Intent {
        txn,
        expected_checksum: job.replace.map(|r| r.checksum.clone()),
        observed_byte_size: opened.before.size,
        created_at: format::now(),
    })
}

/// D2, D3, the check and D4. Any error leaves only this transaction's staging files.
fn stage(
    vol: &Volume,
    intent: &Intent,
    opened: &Opened,
    display_name: &str,
    hook: &mut dyn Hook,
) -> Res<CopyMeta> {
    let txn = &intent.txn;
    let tx = &txn.transaction_id;
    let observed_at = format::now();
    vol.staging
        .write_new(&intent_name(tx), &intent.to_bytes())?;
    hook.at(Point::IntentWritten)?;
    vol.objects_dir(txn.hh())?;
    vol.records_dir(txn.hh())?;
    let (checksum, size) = write_partial(&vol.staging, &partial_name(tx), &opened.file, hook)?;
    hook.at(Point::PayloadWritten)?;
    check(vol, intent, opened, &checksum, size)?;
    hook.at(Point::Checked)?;
    let meta = CopyMeta {
        txn: txn.clone(),
        source_display_name: display_name.to_string(),
        byte_size: size,
        checksum,
        source_observed_at: observed_at,
        copy_verified_at: format::now(),
    };
    vol.staging
        .write_new(&verified_name(tx), &meta.verified_bytes())?;
    hook.at(Point::VerifiedWritten)?;
    Ok(meta)
}

/// D3: stream the source into a new `.partial` while hashing; fsync it and staging.
fn write_partial(
    staging: &Dir,
    name: &str,
    mut from: &std::fs::File,
    hook: &mut dyn Hook,
) -> Res<(String, u64)> {
    let mut out = staging.create_new(name)?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 1 << 16];
    let mut total = 0u64;
    loop {
        let n = from
            .read(&mut buf)
            .map_err(|e| CardError::io("read the source", &e))?;
        if n == 0 {
            break;
        }
        out.write_all(&buf[..n])
            .map_err(|e| CardError::io("write the payload", &e))?;
        hasher.update(&buf[..n]);
        if total == 0 {
            hook.at(Point::PayloadStarted)?;
        }
        total += n as u64;
    }
    out.sync_all()
        .map_err(|e| CardError::io("fsync the payload", &e))?;
    close_checked(out)?;
    staging.sync()?;
    Ok((format!("{:x}", hasher.finalize()), total))
}

/// Read the payload back, hash every byte, and require the source to be unchanged.
fn check(vol: &Volume, intent: &Intent, opened: &Opened, checksum: &str, size: u64) -> Res<()> {
    let tx = &intent.txn.transaction_id;
    let (again, n) = source::hash_reader(vol.staging.open_from_card(&partial_name(tx))?)?;
    if again != checksum || n != size {
        return Err(CardError::new(
            Kind::VerifyFailed,
            "the bytes read back from the card are not the bytes written",
        ));
    }
    opened.unchanged()?;
    if size != intent.observed_byte_size {
        return Err(CardError::new(
            Kind::SourceChanged,
            "the source file's size changed while it was copied",
        ));
    }
    if intent
        .expected_checksum
        .as_deref()
        .is_some_and(|c| c != checksum)
    {
        return Err(CardError::new(
            Kind::VerifyFailed,
            "the file's content is not the version it should replace",
        ));
    }
    Ok(())
}

/// Where `finish` starts.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Start {
    /// The payload is still `staging/T.partial` (after D4).
    Staging,
    /// The payload is already at its final name (after D5).
    Published,
}

/// D5 (when needed), D6, D7 and D8. Reconciliation continues here too.
pub fn finish(
    vol: &Volume,
    cat: &mut dyn Catalogue,
    meta: &CopyMeta,
    from: Start,
    hook: &mut dyn Hook,
) -> Res<Outcome> {
    let txn = &meta.txn;
    if from == Start::Staging {
        vol.confirm()?;
        let objects = vol.objects_dir(txn.hh())?;
        let tx = &txn.transaction_id;
        vol.staging
            .rename_noreplace(&partial_name(tx), &objects, txn.payload_name())?;
        objects.sync()?;
        vol.staging.sync()?;
        hook.at(Point::Published)?;
    }
    vol.confirm()?;
    let record = Record {
        meta: meta.clone(),
        committed_at: format::now(),
    };
    publish_record(vol, &record, hook)?;
    cat.commit_copy(&record, Some(&meta.copy_verified_at))?;
    hook.at(Point::CatalogueCommitted)?;
    let cleanup_failed = clean_up(vol, &txn.transaction_id).err();
    hook.at(Point::CleanedUp)?;
    Ok(Outcome::Committed {
        record: Box::new(record),
        cleanup_failed,
    })
}

/// D6: the record through a temporary file, published without replacing anything.
fn publish_record(vol: &Volume, record: &Record, hook: &mut dyn Hook) -> Res<()> {
    let txn = record.txn();
    let tmp = record_tmp_name(&txn.transaction_id);
    let mut file = vol.staging.create_new(&tmp)?;
    file.write_all(&record.to_bytes())
        .map_err(|e| CardError::io("write the record", &e))?;
    file.sync_all()
        .map_err(|e| CardError::io("fsync the record", &e))?;
    close_checked(file)?;
    hook.at(Point::RecordTempWritten)?;
    let records = vol.records_dir(txn.hh())?;
    vol.staging
        .rename_noreplace(&tmp, &records, &txn.record_name())?;
    records.sync()?;
    vol.staging.sync()?;
    hook.at(Point::RecordPublished)
}

/// D8: remove this transaction's verified intent and intent, only these two names.
pub fn clean_up(vol: &Volume, tx: &str) -> Res<()> {
    vol.staging.unlink_if_present(&verified_name(tx))?;
    vol.staging.unlink_if_present(&intent_name(tx))?;
    vol.staging.sync()
}

/// Undo a transaction that committed nothing: its staging files and its server row.
pub fn discard(vol: &Volume, cat: &mut dyn Catalogue, txn: &Txn) -> Res<()> {
    let tx = &txn.transaction_id;
    for name in [
        record_tmp_name(tx),
        verified_name(tx),
        partial_name(tx),
        intent_name(tx),
    ] {
        vol.staging.unlink_if_present(&name)?;
    }
    vol.staging.sync()?;
    cat.drop_tx(tx)
}
