//! "Check this card" (0.52.4): every copy on an attached card read in full and hashed
//! against its record. A copy whose bytes changed (an SD card goes bad over the years),
//! whose payload is gone, or whose record can no longer be read is marked damaged in the
//! catalogue, so the next run copies that file again (run::decide: a version with no
//! healthy copy is copied anew). Nothing on the card is written. The marks are made only
//! once the whole card was read: a card taken out during the check changes nothing.
use super::catalogue::{Catalogue, CopyState};
use super::error::Res;
use super::format;
use super::rebuild::card_records;
use super::store::Store;
use super::volume::Volume;
use std::collections::HashMap;

/// What a check found.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Checked {
    /// Copies whose bytes match their record.
    pub fine: u64,
    /// The original paths of the files whose copy on the card is damaged.
    pub damaged: Vec<String>,
    /// Those of them with no healthy copy on any card: the next run copies them again.
    pub waiting: Vec<String>,
}

/// Check `vol`, an attached card of `store`'s set. `progress(done, total)` is told as it
/// goes.
pub fn check(store: &mut Store, vol: &Volume, progress: &mut dyn FnMut(u64, u64)) -> Res<Checked> {
    vol.confirm()?;
    let volume_id = vol.identity.volume_id.clone();
    // The copies the catalogue counts on this card, by record.
    let mut known: HashMap<String, String> = store
        .copies_on(&volume_id)?
        .into_iter()
        .filter(|c| matches!(c.state, CopyState::Committed | CopyState::Damaged))
        .map(|c| (c.record_id, c.version_id))
        .collect();
    let (records, _unreadable) = card_records(vol)?;
    let records: Vec<_> = records
        .into_iter()
        .filter(|r| known.remove(&r.txn().record_id).is_some())
        .collect();
    let total = records.len() as u64;
    let mut matches = Vec::with_capacity(records.len());
    for (done, r) in records.iter().enumerate() {
        progress(done as u64, total);
        // A copy that can't be read (a bad sector) is damaged, as long as the card is
        // still there; a card taken out stops the check.
        let ok = match vol.payload_matches(r.txn(), &r.meta.checksum, r.meta.byte_size) {
            Ok(ok) => ok,
            Err(e) => {
                vol.confirm().map_err(|_| e)?;
                false
            }
        };
        matches.push(ok);
    }
    progress(total, total);
    // Still the same card, still there: only now is anything marked.
    vol.confirm()?;
    let now = format::now();
    let mut checked = Checked::default();
    let mut damaged_versions = vec![];
    for (r, ok) in records.iter().zip(matches) {
        if ok {
            store.import_copy(r, CopyState::Committed, Some(&now))?;
            checked.fine += 1;
        } else {
            store.import_copy(r, CopyState::Damaged, None)?;
            checked.damaged.push(r.txn().original_relative_path.clone());
            damaged_versions.push(r.txn().version_id.clone());
        }
    }
    // Copies whose record is gone from the card, or can no longer be read.
    for (record_id, version_id) in known {
        store.copy_damaged(&record_id)?;
        let path = store.version(&version_id)?.map(|v| v.path);
        checked.damaged.push(path.unwrap_or(record_id));
        damaged_versions.push(version_id);
    }
    for version_id in damaged_versions {
        let healthy = store
            .copies_of(&version_id)?
            .iter()
            .any(|c| c.state.protects());
        if let (false, Some(v)) = (healthy, store.version(&version_id)?) {
            checked.waiting.push(v.path);
        }
    }
    checked.damaged.sort();
    checked.waiting.sort();
    let result = match checked.damaged.len() {
        0 => "ok".to_string(),
        n => format!("{n} damaged"),
    };
    store.rechecked(&volume_id, &now, &result)?;
    Ok(checked)
}
