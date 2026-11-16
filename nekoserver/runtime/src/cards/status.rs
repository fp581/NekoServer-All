//! What the dashboard shows of the set (cards/status.json on the app SSD): its cards,
//! which are plugged in, which is full, how many files are on the cards and how many
//! wait for one, and when a card was last written. Written by the card job and by the
//! Backup cards page after an enrolment; read by the collector (collect.rs).
//!
//! Counts and card names only, no file names: the collector's readings can be read by
//! every account on the server.
use crate::cardset::catalogue::Catalogue;
use crate::cardset::source::Candidate;
use crate::cardset::store::Store;
use crate::common::{self, Result};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

pub const FORMAT: &str = "neko-cards-v1";

/// Where the server keeps what it knows of the set: `<app SSD>/cards`, root's only.
#[derive(Debug, Clone)]
pub struct Place {
    pub folder: PathBuf,
}

impl Place {
    pub fn on(data: &Path) -> Place {
        Place {
            folder: data.join(super::fact("folder")),
        }
    }
    pub fn catalogue(&self) -> PathBuf {
        self.folder.join(super::fact("catalogue"))
    }
    pub fn status(&self) -> PathBuf {
        self.folder.join(super::fact("status"))
    }
    /// The catalogue, made (with its folder) when there is none yet.
    pub fn open(&self) -> Result<Store> {
        common::directory(&self.folder, 0o700)?;
        Store::open(&self.catalogue()).map_err(super::card_error)
    }
    /// The catalogue when there is one; None before the first card was enrolled.
    pub fn existing(&self) -> Result<Option<Store>> {
        if !self.catalogue().exists() {
            return Ok(None);
        }
        self.open().map(Some)
    }
    pub fn previous(&self) -> Value {
        let v = common::json(&self.status(), common::REPORT_LIMIT);
        if v["format"] == FORMAT {
            v
        } else {
            json!({})
        }
    }
}

/// (files on the cards, files waiting for one): a file is on the cards when its newest
/// version there has its size and dates, with a healthy copy. Nothing is read but the
/// catalogue: the scan already has each file's size and dates.
pub fn count(cat: &dyn Catalogue, cands: &[Candidate]) -> Result<(u64, u64)> {
    let (mut on, mut waiting) = (0, 0);
    for c in cands {
        let version = cat
            .newest_version(&c.source.source_id, &c.path)
            .map_err(super::card_error)?;
        let kept = match version {
            Some(v)
                if v.byte_size == c.size
                    && v.mtime_ns == c.mtime_ns
                    && v.ctime_ns == c.ctime_ns =>
            {
                cat.copies_of(&v.version_id)
                    .map_err(super::card_error)?
                    .iter()
                    .any(|copy| copy.state.protects())
            }
            _ => false,
        };
        if kept {
            on += 1;
        } else {
            waiting += 1;
        }
    }
    Ok((on, waiting))
}

/// What this run saw and did, for the status.
#[derive(Debug, Default)]
pub struct Seen {
    /// The numbers of the cards of the set plugged in.
    pub plugged: Vec<u64>,
    /// Cards this run filled up (it stopped at their reserve), and cards it wrote that
    /// took everything they were given.
    pub full: Vec<u64>,
    pub not_full: Vec<u64>,
    /// A card was written to (its run ended without being stopped).
    pub wrote: bool,
    /// (files on the cards, files waiting), when the sources were looked at.
    pub counts: Option<(u64, u64)>,
}

/// The status as it is now, saved where the collector reads it, and returned.
pub fn save(place: &Place, store: &Store, seen: &Seen) -> Result<Value> {
    let old = place.previous();
    let (_, name) = store
        .backup_set()
        .map_err(super::card_error)?
        .unwrap_or_default();
    let was_full = |n: u64| {
        old["cards"]
            .as_array()
            .into_iter()
            .flatten()
            .any(|c| c["number"] == n && c["full"] == true)
    };
    let mut cards = vec![];
    let mut newest = 0;
    let mut newest_full = false;
    for v in store.volumes().map_err(super::card_error)? {
        let n = v.sequence_number;
        let files = store
            .copies_on(&v.volume_id)
            .map_err(super::card_error)?
            .iter()
            .filter(|c| c.state.protects())
            .count();
        let full = seen.full.contains(&n) || (was_full(n) && !seen.not_full.contains(&n));
        if n >= newest {
            newest = n;
            newest_full = full;
        }
        cards.push(
            json!({"number": n, "label": v.label, "state": v.state.as_str(),
            "plugged": seen.plugged.contains(&n), "files": files, "full": full,
            "capacity_bytes": v.capacity_bytes, "last_seen_at": v.last_seen_at}),
        );
    }
    let (on, waiting) = match seen.counts {
        Some((on, waiting)) => (json!(on), json!(waiting)),
        None => (old["files_on_cards"].clone(), old["files_waiting"].clone()),
    };
    let now = common::now();
    let status = json!({
        "format": FORMAT,
        "updated_at": now,
        "set_name": name,
        "cards": cards,
        "files_on_cards": on,
        "files_waiting": waiting,
        "last_write_at": if seen.wrote { json!(now) } else { old["last_write_at"].clone() },
        // The newest card of the set is full: the next one to put in.
        "full_card": if newest_full { json!(newest) } else { Value::Null },
        "next_number": newest + 1,
    });
    common::atomic_json(&place.status(), &status)?;
    Ok(status)
}

/// The dashboard's reading of the set (collect.rs "cards"), only while backup cards are
/// switched on: the status as the job and the page last saved it (empty while the app
/// SSD is away, or before the first card).
pub fn reading(cfg: &Value, data: &Path, ok: bool) -> Option<Value> {
    if !super::enabled(cfg) {
        return None;
    }
    let set = if ok {
        Place::on(data).previous()
    } else {
        json!({})
    };
    Some(json!({"enabled": true, "set": set}))
}
