//! Recovery of every gap between two durable steps (format document, section 10).
//!
//! It runs when a card is attached, before any new transaction, under the set's lock. It
//! works from what is on the card, so it gives the same result with or without the
//! catalogue's rows, and a second run changes nothing. A staging file is never assumed
//! valid: a partial without a verified intent is removed, a verified payload is hashed
//! again before it is published or recorded, and a payload no trusted metadata names is
//! kept as an orphan. Only this engine's own files in `staging/` are ever removed.
use super::catalogue::{Catalogue, CopyState, OrphanRow};
use super::commit::{self, Start};
use super::error::{CardError, Kind, Res};
use super::format::{self, CopyMeta, Intent, Record, Txn};
use super::source::hash_reader;
use super::volume::Volume;
use std::collections::{BTreeMap, HashSet};

/// More files than this in `staging/` means something is wrong: no writes, report.
pub const STAGING_LIMIT: usize = 64;

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Report {
    /// Records written by recovery from a verified intent (no recopy).
    pub finished: Vec<String>,
    /// Records already on the card, now counted by the catalogue.
    pub imported: Vec<String>,
    /// Transactions whose staging files were removed.
    pub cleaned: Vec<String>,
    /// Source paths to copy again (nothing of theirs is committed).
    pub retry: Vec<String>,
    /// Transactions kept as evidence of an orphan payload.
    pub orphans: Vec<String>,
    /// Records whose payload is missing or has other bytes.
    pub damaged: Vec<String>,
    /// Staging files that did not parse (torn): they proved nothing.
    pub torn: Vec<String>,
    /// Names in `staging/` that do not fit the layout: reported, left alone.
    pub unknown: Vec<String>,
    pub notes: Vec<String>,
    /// exFAT can leave one file under two names: no more writes to this card.
    pub needs_filesystem_check: bool,
}

#[derive(Debug, Default, Clone, Copy)]
struct Staged {
    intent: bool,
    partial: bool,
    verified: bool,
    record_tmp: bool,
}

enum Name {
    Txn(String, fn(&mut Staged)),
    InventoryTemp,
    Unknown,
}

fn classify(name: &str) -> Name {
    let (Some(id), Some(rest)) = (name.get(..36), name.get(36..)) else {
        return Name::Unknown;
    };
    if !format::is_uuid(id) {
        return Name::Unknown;
    }
    let mark: fn(&mut Staged) = match rest {
        ".intent.json" => |s| s.intent = true,
        ".partial" => |s| s.partial = true,
        ".verified.json" => |s| s.verified = true,
        ".record.tmp" => |s| s.record_tmp = true,
        ".inventory.tmp" | ".versions.tmp" => return Name::InventoryTemp,
        _ => return Name::Unknown,
    };
    Name::Txn(id.to_string(), mark)
}

/// Reconcile the attached card with the catalogue.
pub fn reconcile(vol: &Volume, cat: &mut dyn Catalogue) -> Res<Report> {
    let names = vol.staging.names()?;
    if names.len() > STAGING_LIMIT {
        return Err(CardError::new(
            Kind::Invalid,
            "too many files in the card's staging folder: nothing is written",
        ));
    }
    let mut report = Report::default();
    let mut groups: BTreeMap<String, Staged> = BTreeMap::new();
    for name in names {
        match classify(&name) {
            Name::Txn(tx, mark) => mark(groups.entry(tx).or_default()),
            Name::InventoryTemp => {
                vol.staging.unlink(&name)?;
                report
                    .notes
                    .push(format!("removed an interrupted inventory file {name}"));
            }
            Name::Unknown => report.unknown.push(name),
        }
    }
    let evidence: HashSet<String> = cat
        .orphans(vol.volume_id())?
        .into_iter()
        .map(|o| o.transaction_id)
        .collect();
    for (tx, staged) in &groups {
        if !evidence.contains(tx) {
            Recovery {
                vol,
                cat: &mut *cat,
                report: &mut report,
            }
            .transaction(tx, *staged)?;
        }
    }
    rows_without_staging(vol, cat, &groups, &mut report)?;
    vol.staging.sync()?;
    Ok(report)
}

/// True when staging holds nothing but kept evidence: a new transaction may start.
pub fn staging_clear(vol: &Volume, cat: &dyn Catalogue) -> Res<bool> {
    let evidence: HashSet<String> = cat
        .orphans(vol.volume_id())?
        .into_iter()
        .map(|o| o.transaction_id)
        .collect();
    let names = vol.staging.names()?;
    Ok(names.iter().all(|n| match classify(n) {
        Name::Txn(tx, _) => evidence.contains(&tx),
        _ => false,
    }))
}

struct Recovery<'a> {
    vol: &'a Volume,
    cat: &'a mut dyn Catalogue,
    report: &'a mut Report,
}

impl Recovery<'_> {
    fn transaction(&mut self, tx: &str, staged: Staged) -> Res<()> {
        if staged.record_tmp {
            self.vol.staging.unlink(&commit::record_tmp_name(tx))?;
        }
        let intent = self.read_intent(tx, staged.intent);
        match self.read_verified(tx, staged.verified) {
            Some(meta) => self.verified(tx, staged, &meta),
            None => self.unverified(tx, intent.as_ref()),
        }
    }

    fn belongs(&self, txn: &Txn, tx: &str) -> bool {
        let id = &self.vol.identity;
        txn.transaction_id == tx
            && txn.volume_id == id.volume_id
            && txn.backup_set_id == id.backup_set_id
    }

    fn read_intent(&mut self, tx: &str, present: bool) -> Option<Intent> {
        if !present {
            return None;
        }
        let name = commit::intent_name(tx);
        let parsed = self
            .vol
            .staging
            .read_limited(&name, format::SMALL_FILE)
            .and_then(|b| Intent::parse(&b))
            .ok()
            .filter(|i| self.belongs(&i.txn, tx));
        if parsed.is_none() {
            self.report.torn.push(name);
        }
        parsed
    }

    fn read_verified(&mut self, tx: &str, present: bool) -> Option<CopyMeta> {
        if !present {
            return None;
        }
        let name = commit::verified_name(tx);
        let parsed = self
            .vol
            .staging
            .read_limited(&name, format::SMALL_FILE)
            .and_then(|b| CopyMeta::parse_verified(&b))
            .ok()
            .filter(|m| self.belongs(&m.txn, tx));
        if parsed.is_none() {
            self.report.torn.push(name);
        }
        parsed
    }

    fn payload_present(&self, txn: &Txn) -> Res<bool> {
        match self.vol.existing(&self.vol.objects, txn.hh())? {
            Some(dir) => dir.exists(txn.payload_name()),
            None => Ok(false),
        }
    }

    fn record_present(&self, txn: &Txn) -> Res<bool> {
        match self.vol.existing(&self.vol.records, txn.hh())? {
            Some(dir) => dir.exists(&txn.record_name()),
            None => Ok(false),
        }
    }

    /// The payload's checksum and size equal `checksum` and `size`.
    fn payload_matches(&self, txn: &Txn, checksum: &str, size: u64) -> Res<bool> {
        self.vol.payload_matches(txn, checksum, size)
    }

    fn partial_matches(&self, meta: &CopyMeta) -> Res<bool> {
        let file = self
            .vol
            .staging
            .open_file(&commit::partial_name(&meta.txn.transaction_id))?;
        let (sum, n) = hash_reader(file)?;
        Ok(sum == meta.checksum && n == meta.byte_size)
    }

    fn remove_all(&mut self, tx: &str) -> Res<()> {
        for name in [
            commit::verified_name(tx),
            commit::partial_name(tx),
            commit::intent_name(tx),
        ] {
            self.vol.staging.unlink_if_present(&name)?;
        }
        self.vol.staging.sync()?;
        self.cat.drop_tx(tx)?;
        self.report.cleaned.push(tx.to_string());
        Ok(())
    }

    /// Gaps D1 to D4: nothing verified, so nothing is trusted.
    fn unverified(&mut self, tx: &str, intent: Option<&Intent>) -> Res<()> {
        if let Some(i) = intent {
            if self.payload_present(&i.txn)? {
                return self.orphan(&i.txn, "a payload with no verified intent");
            }
            self.report.retry.push(i.txn.original_relative_path.clone());
        }
        self.remove_all(tx)
    }

    /// Gaps D4 to D8: the verified intent says what the bytes must be.
    fn verified(&mut self, tx: &str, staged: Staged, meta: &CopyMeta) -> Res<()> {
        let txn = &meta.txn;
        let payload = self.payload_present(txn)?;
        if self.record_present(txn)? {
            return self.after_record(tx, meta);
        }
        if staged.partial && payload {
            self.report.needs_filesystem_check = true;
            self.report.notes.push(format!(
                "transaction {tx}: the payload is under two names; check the card on another computer"
            ));
            return Ok(());
        }
        if payload {
            return self.after_publish(meta);
        }
        if staged.partial {
            return self.after_verified(tx, meta);
        }
        self.report.notes.push(format!(
            "transaction {tx}: the payload is gone; the card may have lost space and should be checked"
        ));
        self.report.retry.push(txn.original_relative_path.clone());
        self.remove_all(tx)
    }

    /// D4 to D5: hash the partial again before it is published.
    fn after_verified(&mut self, tx: &str, meta: &CopyMeta) -> Res<()> {
        if !self.partial_matches(meta)? {
            self.report.notes.push(format!(
                "transaction {tx}: verification failed after an interruption"
            ));
            self.report
                .retry
                .push(meta.txn.original_relative_path.clone());
            return self.remove_all(tx);
        }
        let elsewhere = self
            .cat
            .copies_of(&meta.txn.version_id)?
            .iter()
            .any(|c| c.state.protects());
        if elsewhere {
            return self.remove_all(tx);
        }
        commit::finish(
            self.vol,
            self.cat,
            meta,
            Start::Staging,
            &mut commit::no_hook,
        )?;
        self.report.finished.push(meta.txn.record_id.clone());
        Ok(())
    }

    /// D5 to D6: the payload is final; write the record only if its bytes match.
    fn after_publish(&mut self, meta: &CopyMeta) -> Res<()> {
        if !self.payload_matches(&meta.txn, &meta.checksum, meta.byte_size)? {
            return self.orphan(&meta.txn, "the payload does not match its verified intent");
        }
        commit::finish(
            self.vol,
            self.cat,
            meta,
            Start::Published,
            &mut commit::no_hook,
        )?;
        self.report.finished.push(meta.txn.record_id.clone());
        Ok(())
    }

    /// D6 to D8: the record is committed; import it (once) and clean up.
    fn after_record(&mut self, tx: &str, meta: &CopyMeta) -> Res<()> {
        let Some(record) = self.read_record(&meta.txn)? else {
            return self.orphan(
                &meta.txn,
                "its record is unreadable or contradicts the card",
            );
        };
        if record.meta.txn != meta.txn {
            return self.orphan(&meta.txn, "its record contradicts the verified intent");
        }
        if self.cat.copy(&meta.txn.record_id)?.is_none() {
            self.import(&record)?;
        }
        commit::clean_up(self.vol, tx)?;
        self.report.cleaned.push(tx.to_string());
        Ok(())
    }

    fn read_record(&mut self, txn: &Txn) -> Res<Option<Record>> {
        let dir = self.vol.records_dir(txn.hh())?;
        let parsed = dir
            .read_limited(&txn.record_name(), format::SMALL_FILE)
            .and_then(|b| Record::parse(&b))
            .ok()
            .filter(|r| r.txn().record_id == txn.record_id);
        if parsed.is_none() {
            self.report
                .torn
                .push(format!("records/{}/{}", txn.hh(), txn.record_name()));
        }
        Ok(parsed)
    }

    /// Count a committed record, healthy only when its payload hashes to the record.
    fn import(&mut self, record: &Record) -> Res<()> {
        let m = &record.meta;
        let healthy = self.payload_matches(&m.txn, &m.checksum, m.byte_size)?;
        let now = format::now();
        self.cat
            .commit_copy(record, healthy.then_some(now.as_str()))?;
        if healthy {
            self.report.imported.push(m.txn.record_id.clone());
            return Ok(());
        }
        self.cat
            .set_copy_state(&m.txn.record_id, CopyState::Damaged)?;
        self.report.damaged.push(m.txn.record_id.clone());
        self.report.retry.push(m.txn.original_relative_path.clone());
        Ok(())
    }

    /// Keep the payload where it is, keep the staging files as evidence, copy the
    /// source again under a new record.
    fn orphan(&mut self, txn: &Txn, why: &str) -> Res<()> {
        self.cat.add_orphan(&OrphanRow {
            volume_id: txn.volume_id.clone(),
            transaction_id: txn.transaction_id.clone(),
            archive_path: txn.archive_relative_path.clone(),
        })?;
        self.cat.drop_tx(&txn.transaction_id)?;
        self.report.orphans.push(txn.transaction_id.clone());
        self.report.retry.push(txn.original_relative_path.clone());
        self.report
            .notes
            .push(format!("orphan {}: {why}", txn.archive_relative_path));
        Ok(())
    }
}

/// Server rows of this card with no staging file: an intent that never became durable
/// (D1 to D2), or a record whose staging was already cleaned.
fn rows_without_staging(
    vol: &Volume,
    cat: &mut dyn Catalogue,
    groups: &BTreeMap<String, Staged>,
    report: &mut Report,
) -> Res<()> {
    for row in cat.open_txs(vol.volume_id())? {
        if groups.contains_key(&row.transaction_id) {
            continue;
        }
        let hh = &row.record_id[..2];
        let record = match vol.existing(&vol.records, hh)? {
            Some(dir) => dir
                .read_limited(&format!("{}.json", row.record_id), format::SMALL_FILE)
                .and_then(|b| Record::parse(&b))
                .ok(),
            None => None,
        };
        match record {
            Some(r) => Recovery {
                vol,
                cat: &mut *cat,
                report: &mut *report,
            }
            .import(&r)?,
            None => {
                cat.drop_tx(&row.transaction_id)?;
                report.retry.push(row.path.clone());
                report.cleaned.push(row.transaction_id.clone());
            }
        }
    }
    Ok(())
}
