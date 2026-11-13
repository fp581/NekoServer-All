//! Rebuilding the catalogue from the cards (V2 section 10, A28 to A31): the records of
//! the cards that are attached, each payload hashed against its record, then what the
//! inventory snapshots on those cards know about the others.
//!
//! What a card holds is observed; what a snapshot says is historical knowledge. A copy
//! known only from a snapshot is `Historical`: shown, never counted as protection, until
//! its card is attached and its record read. Nothing is decided by a date: a version or
//! a copy that two sources describe differently is kept as a conflict for a person, and
//! a volume any snapshot or the catalogue calls lost, retired or damaged stays so. After
//! a rebuild the catalogue starts a new epoch, so its revisions are never compared with
//! those of the history that was lost.
//!
//! The report says plainly what cannot be known: a card enrolled, or a file copied, after
//! the newest snapshot that was read is unknown to this catalogue.
use super::catalogue::{CopyRow, CopyState};
use super::error::{CardError, Kind, Res};
use super::format::{self, Record};
use super::inventory::{self, Header, VersionLine, VolumeEntry};
use super::store::{Store, VolumeRow, VolumeState};
use super::volume::Volume;
use std::collections::{BTreeMap, HashMap, HashSet};

/// What one rebuild did and what it could not know.
#[derive(Debug, Default, Clone)]
pub struct RebuildReport {
    /// Records imported, per attached volume, with their payload hashed and matching.
    pub imported: BTreeMap<String, usize>,
    /// Records whose payload is missing or has other bytes: kept, marked damaged.
    pub damaged: Vec<String>,
    /// Record files that did not parse or contradict their card: left in place, ignored.
    pub quarantined: Vec<String>,
    /// Volumes known only from a snapshot (not attached), by ID.
    pub unobserved_volumes: Vec<String>,
    /// Copies learnt from snapshots only: not healthy.
    pub historical_copies: usize,
    /// Copies a snapshot listed on an attached card that the card does not have.
    pub missing: Vec<String>,
    /// Disagreements kept for review.
    pub conflicts: Vec<String>,
    /// Snapshots refused, and version lists without a header.
    pub unusable_snapshots: Vec<String>,
    /// The newest snapshot read in each epoch: (epoch, revision, exported at, card).
    pub newest: Vec<(String, u64, String, String)>,
    /// The new epoch the rebuilt catalogue starts.
    pub epoch: String,
}

impl RebuildReport {
    /// The plain statement of what the rebuilt catalogue cannot know.
    pub fn unknown_coverage(&self) -> String {
        if self.newest.is_empty() {
            return "No inventory was found on the attached cards: only their own files are known. \
                    Cards that are not attached, and the files on them, are unknown."
                .to_string();
        }
        let latest = self
            .newest
            .iter()
            .map(|(_, rev, at, card)| format!("revision {rev} of {at} on card {card}"))
            .collect::<Vec<_>>()
            .join("; ");
        format!(
            "Known from the cards attached and their inventories ({latest}). A card enrolled, or a \
             file copied, after the newest of these is not known; copies on cards that are not \
             attached are not counted as protection."
        )
    }
}

/// The records of one attached card: (valid records, quarantined file names).
fn card_records(vol: &Volume) -> Res<(Vec<Record>, Vec<String>)> {
    let mut good = Vec::new();
    let mut bad = Vec::new();
    for hh in vol.records.names()? {
        let Ok(dir) = vol.records.open_dir(&hh) else {
            bad.push(format!("records/{hh}"));
            continue;
        };
        for name in dir.names()? {
            let parsed = dir
                .read_limited(&name, format::SMALL_FILE)
                .and_then(|b| Record::parse(&b))
                .ok()
                .filter(|r| {
                    let t = r.txn();
                    t.record_name() == name
                        && t.hh() == hh
                        && t.volume_id == vol.identity.volume_id
                        && t.backup_set_id == vol.identity.backup_set_id
                });
            match parsed {
                Some(r) => good.push(r),
                None => bad.push(format!("records/{hh}/{name}")),
            }
        }
    }
    Ok((good, bad))
}

/// The four values that make a version (format 1, section 4).
fn version_key(source: &str, path: &str, size: u64, checksum: &str) -> String {
    format!("{source}\u{0}{path}\u{0}{size}\u{0}{checksum}")
}

struct Rebuild<'a> {
    store: &'a mut Store,
    report: RebuildReport,
    /// version ID -> its four values, as first learnt.
    versions: HashMap<String, String>,
    now: String,
}

impl Rebuild<'_> {
    /// False, with a conflict kept, when this version ID was learnt with other values.
    fn agrees(&mut self, version_id: &str, key: String, from: &str) -> Res<bool> {
        match self.versions.get(version_id) {
            None => {
                self.versions.insert(version_id.to_string(), key);
                Ok(true)
            }
            Some(known) if *known == key => Ok(true),
            Some(_) => {
                let what = format!("version {version_id} is described differently by {from}");
                self.store.add_conflict(&what, &self.now)?;
                self.report.conflicts.push(what);
                Ok(false)
            }
        }
    }

    /// The records of the attached cards, oldest observation first, so that the newest
    /// version of a path is the last one imported.
    fn import_records(&mut self, cards: &[&Volume]) -> Res<HashSet<String>> {
        let mut all = Vec::new();
        for vol in cards {
            vol.confirm()?;
            let room = vol.room()?;
            self.store
                .saw_volume(&vol.identity, room.capacity, &self.now)?;
            let (records, bad) = card_records(vol)?;
            self.report.quarantined.extend(
                bad.into_iter()
                    .map(|n| format!("{}: {n}", vol.identity.label())),
            );
            for r in records {
                let ok = vol.payload_matches(r.txn(), &r.meta.checksum, r.meta.byte_size)?;
                all.push((r, ok));
            }
        }
        all.sort_by(|(a, _), (b, _)| {
            (
                &a.meta.source_observed_at,
                &a.committed_at,
                &a.txn().record_id,
            )
                .cmp(&(
                    &b.meta.source_observed_at,
                    &b.committed_at,
                    &b.txn().record_id,
                ))
        });
        let mut seen = HashSet::new();
        for (r, ok) in all {
            let t = r.txn();
            let key = version_key(
                &t.source_id,
                &t.original_relative_path,
                r.meta.byte_size,
                &r.meta.checksum,
            );
            if !self.agrees(&t.version_id, key, &format!("record {}", t.record_id))? {
                continue;
            }
            if ok {
                self.store
                    .import_copy(&r, CopyState::Committed, Some(&self.now))?;
                *self.report.imported.entry(t.volume_id.clone()).or_default() += 1;
            } else {
                self.store.import_copy(&r, CopyState::Damaged, None)?;
                self.report.damaged.push(t.record_id.clone());
            }
            seen.insert(t.record_id.clone());
        }
        Ok(seen)
    }

    /// A volume a snapshot knows: added if new; its state is the worst any snapshot
    /// gives it (a lost card is never brought back by an older snapshot).
    fn learn_volume(
        &mut self,
        header: &Header,
        v: &VolumeEntry,
        worst: &mut HashMap<String, VolumeState>,
    ) -> Res<()> {
        let rank = |s: VolumeState| match s {
            VolumeState::Lost => 5,
            VolumeState::Retired => 4,
            VolumeState::Damaged => 3,
            VolumeState::IdentityConflict => 2,
            VolumeState::Unavailable => 1,
            VolumeState::InUse => 0,
        };
        let entry = worst.entry(v.volume_id.clone()).or_insert(v.state);
        if rank(v.state) > rank(*entry) {
            *entry = v.state;
        }
        self.store.heard_of_volume(&VolumeRow {
            volume_id: v.volume_id.clone(),
            backup_set_id: header.backup_set_id.clone(),
            sequence_number: v.sequence_number,
            label: format!("{} - {:03}", header.set_display_name, v.sequence_number),
            filesystem_type: v.filesystem_type.clone(),
            filesystem_uuid: String::new(),
            enrolled_at: v.enrolled_at.clone(),
            state: VolumeState::Unavailable,
            state_since: self.now.clone(),
            last_seen_at: v.last_seen_at.clone(),
            last_recheck_at: v.last_recheck_at.clone(),
            last_recheck_result: v.last_recheck_result.clone(),
            capacity_bytes: v.capacity_bytes,
            observed: false,
        })
    }

    /// One version line of a snapshot: copies on cards not attached become historical;
    /// a committed copy the attached card does not have is missing there.
    fn learn_version(
        &mut self,
        line: &VersionLine,
        attached: &HashSet<String>,
        seen: &HashSet<String>,
        from: &str,
    ) -> Res<()> {
        let key = version_key(&line.source_id, &line.path, line.byte_size, &line.sha256);
        if !self.agrees(&line.version_id, key, from)? {
            return Ok(());
        }
        let version = line.version_row();
        for c in &line.copies {
            if seen.contains(&c.record_id) {
                continue; // observed on its card: the card is the authority
            }
            let state = if attached.contains(&c.volume_id) {
                if c.state == CopyState::Committed {
                    self.report.missing.push(c.record_id.clone());
                }
                if c.state == CopyState::Committed {
                    CopyState::Missing
                } else {
                    c.state
                }
            } else if c.state == CopyState::Committed {
                self.report.historical_copies += 1;
                CopyState::Historical
            } else {
                c.state
            };
            let row = CopyRow {
                record_id: c.record_id.clone(),
                version_id: line.version_id.clone(),
                volume_id: c.volume_id.clone(),
                state,
                verified_at: c.last_verified_at.clone(),
            };
            self.store.heard_of_copy(&version, &row, "")?;
        }
        Ok(())
    }
}

/// Rebuild `store` (empty, or the catalogue as it is) from the attached `cards`.
pub fn rebuild(store: &mut Store, cards: &[&Volume]) -> Res<RebuildReport> {
    let now = format::now();
    let mut ids = HashSet::new();
    for vol in cards {
        if !ids.insert(vol.identity.volume_id.clone()) {
            return Err(CardError::new(
                Kind::WrongVolume,
                "two attached cards carry the same volume ID: an identity conflict, nothing is rebuilt",
            ));
        }
    }
    let mut work = Rebuild {
        store,
        report: RebuildReport::default(),
        versions: HashMap::new(),
        now,
    };
    for v in work.store.all_versions()? {
        let key = version_key(&v.source_id, &v.path, v.byte_size, &v.checksum);
        work.versions.insert(v.version_id, key);
    }
    let seen = work.import_records(cards)?;
    let attached: HashSet<String> = cards.iter().map(|v| v.identity.volume_id.clone()).collect();
    let mut worst: HashMap<String, VolumeState> = HashMap::new();
    let mut newest: BTreeMap<String, (u64, String, String)> = BTreeMap::new();
    for vol in cards {
        let found = inventory::read(vol)?;
        let label = vol.identity.label();
        work.report.unusable_snapshots.extend(
            found
                .invalid
                .iter()
                .map(|(n, why)| format!("{label}: {n}: {why}"))
                .chain(
                    found
                        .headless
                        .iter()
                        .map(|n| format!("{label}: {n}: no header")),
                ),
        );
        for header in &found.valid {
            for s in &header.sources {
                work.store.add_source(s)?;
            }
            for v in &header.volumes {
                work.learn_volume(header, v, &mut worst)?;
            }
            let from = format!("the inventory {} on {label}", header.snapshot_id);
            inventory::each_version(vol, header, |line| {
                work.learn_version(&line, &attached, &seen, &from)
            })?;
            let best = newest.entry(header.catalogue_epoch_id.clone()).or_insert((
                0,
                String::new(),
                String::new(),
            ));
            if header.revision > best.0 {
                *best = (header.revision, header.exported_at.clone(), label.clone());
            }
        }
    }
    for (volume_id, state) in &worst {
        let known = work.store.volume(volume_id)?;
        if let Some(v) = known {
            let keep = v.state.is_final() && !state.is_final();
            if !keep && (state.is_final() || *state == VolumeState::IdentityConflict) {
                work.store.set_volume_state(volume_id, *state, &work.now)?;
            }
            if !v.observed {
                work.report.unobserved_volumes.push(volume_id.clone());
            }
        }
    }
    work.report.newest = newest
        .into_iter()
        .map(|(epoch, (rev, at, card))| (epoch, rev, at, card))
        .collect();
    work.report.epoch = work.store.start_epoch()?;
    Ok(work.report)
}
