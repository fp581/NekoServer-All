//! The set inventory snapshot (archive format 1, section 6): what the catalogue knew
//! about the whole set, written new onto the card at each checkpoint (before a swap, at
//! the end of a run), so that a card can tell which other cards and files exist even
//! when the server's catalogue is gone.
//!
//! A snapshot is a small header, `inventory/<id>.json`, and a version list,
//! `inventory/<id>.versions.jsonl`, one JSON object a line, so that a large set is read
//! in bounded memory. The list is published first, then the header that names its size,
//! line count and checksum: a snapshot counts only with both, and only when they agree.
//! Nothing is ever overwritten; both go through `staging/` and a no-replace rename.
//!
//! A snapshot is historical knowledge, never proof that another card is healthy now.
use super::catalogue::{Catalogue, CopyState, VersionRow};
use super::error::{CardError, Kind, Res};
use super::format::{self, Fields};
use super::fsat::Dir;
use super::store::{self, SourceRow, Store, VolumeState};
use super::volume::Volume;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::io::{BufRead, BufReader, Read};

pub const HEADER_LIMIT: u64 = 1_048_576;
const HEADER_DEPTH: usize = 5;
pub const LINE_LIMIT: u64 = 16_384;
pub const MAX_LINES: u64 = 5_000_000;
const MAX_COPIES: usize = 16;
const MAX_SOURCES: usize = 32;
const MAX_VOLUMES: usize = 9999;
/// The most records one volume's entry may count.
const MAX_RECORDS: u64 = 1_000_000;
const VERSIONS_SUFFIX: &str = ".versions.jsonl";
const HEADER_SUFFIX: &str = ".json";

/// One volume as a snapshot knows it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VolumeEntry {
    pub volume_id: String,
    pub sequence_number: u64,
    pub state: VolumeState,
    pub state_since: String,
    pub enrolled_at: String,
    pub last_seen_at: Option<String>,
    pub last_recheck_at: Option<String>,
    pub last_recheck_result: Option<String>,
    pub record_count: u64,
    pub payload_bytes: u64,
    pub filesystem_type: String,
    pub capacity_bytes: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Header {
    pub snapshot_id: String,
    pub catalogue_epoch_id: String,
    pub revision: u64,
    pub exported_at: String,
    pub backup_set_id: String,
    pub set_display_name: String,
    pub written_to_volume_id: String,
    pub includes_transactions_through: String,
    pub sources: Vec<SourceRow>,
    pub volumes: Vec<VolumeEntry>,
    pub lines: u64,
    pub byte_size: u64,
    pub sha256: String,
}

/// A copy as a snapshot lists it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CopyEntry {
    pub record_id: String,
    pub volume_id: String,
    pub state: CopyState,
    pub last_verified_at: Option<String>,
}

/// One line of the version list. A snapshot carries no dates of the source file: a
/// version learnt from it has none (0), so a normal run looks at that file again.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VersionLine {
    pub version_id: String,
    pub source_id: String,
    pub path: String,
    pub byte_size: u64,
    pub sha256: String,
    pub copies: Vec<CopyEntry>,
}

impl VersionLine {
    pub fn version_row(&self) -> VersionRow {
        VersionRow {
            version_id: self.version_id.clone(),
            source_id: self.source_id.clone(),
            path: self.path.clone(),
            byte_size: self.byte_size,
            mtime_ns: 0,
            ctime_ns: 0,
            checksum: self.sha256.clone(),
        }
    }
}

/// What the inventory folder of one card holds.
#[derive(Debug, Default)]
pub struct Found {
    pub valid: Vec<Header>,
    /// Header names that were refused, with the reason.
    pub invalid: Vec<(String, String)>,
    /// Version lists without a header: an interrupted checkpoint, left in place.
    pub headless: Vec<String>,
    /// Names that do not fit the layout: reported, left alone.
    pub unknown: Vec<String>,
}

fn copy_word(state: CopyState) -> &'static str {
    // An unobserved copy is exported as the snapshot knew it: committed. A snapshot is
    // historical knowledge by definition, and whoever reads it treats it as that.
    match state {
        CopyState::Historical => "committed",
        other => store::copy_state_name(other),
    }
}

fn version_json(v: &VersionRow, copies: &[super::catalogue::CopyRow]) -> Value {
    let copies: Vec<Value> = copies
        .iter()
        .map(|c| {
            json!({
                "record_id": c.record_id,
                "volume_id": c.volume_id,
                "state": copy_word(c.state),
                "last_verified_at": c.verified_at,
            })
        })
        .collect();
    json!({
        "version_id": v.version_id,
        "source_id": v.source_id,
        "path": v.path,
        "byte_size": v.byte_size,
        "sha256": v.checksum,
        "copies": copies,
    })
}

/// The version list of the catalogue, one line per version.
fn version_lines(store: &Store) -> Res<(Vec<u8>, u64)> {
    let mut bytes = Vec::new();
    let mut lines = 0u64;
    for v in store.all_versions()? {
        let copies = store.copies_of(&v.version_id)?;
        if copies.len() > MAX_COPIES {
            return Err(CardError::new(
                Kind::Unsupported,
                "a version has more copies than an inventory line can list",
            ));
        }
        let line = serde_json::to_vec(&version_json(&v, &copies))
            .map_err(|_| CardError::new(Kind::Invalid, "a version line"))?;
        if line.len() as u64 > LINE_LIMIT {
            return Err(CardError::new(
                Kind::Unsupported,
                "a version is too long for an inventory line",
            ));
        }
        bytes.extend_from_slice(&line);
        bytes.push(b'\n');
        lines += 1;
    }
    Ok((bytes, lines))
}

fn volume_entries(store: &Store) -> Res<Vec<Value>> {
    let sizes: std::collections::HashMap<String, u64> = store
        .all_versions()?
        .into_iter()
        .map(|v| (v.version_id, v.byte_size))
        .collect();
    let mut out = Vec::new();
    for v in store.volumes()? {
        let held: Vec<_> = store
            .copies_on(&v.volume_id)?
            .into_iter()
            .filter(|c| matches!(c.state, CopyState::Committed | CopyState::Historical))
            .collect();
        let payload: u64 = held
            .iter()
            .map(|c| sizes.get(&c.version_id).copied().unwrap_or(0))
            .sum();
        out.push(json!({
            "volume_id": v.volume_id,
            "sequence_number": v.sequence_number,
            "state": v.state.as_str(),
            "state_since": v.state_since,
            "enrolled_at": v.enrolled_at,
            "last_seen_at": v.last_seen_at,
            "last_recheck_at": v.last_recheck_at,
            "last_recheck_result": v.last_recheck_result,
            "record_count": held.len(),
            "payload_bytes": payload,
            "filesystem_type": v.filesystem_type,
            "capacity_bytes": v.capacity_bytes,
        }));
    }
    Ok(out)
}

/// Publish `bytes` as `inventory/<name>` through `staging/<temp>`: written and fsynced
/// in staging, the card's identity checked again, renamed without replacing anything,
/// both folders fsynced.
fn publish(vol: &Volume, inventory: &Dir, temp: &str, name: &str, bytes: &[u8]) -> Res<()> {
    vol.staging.write_new(temp, bytes)?;
    vol.confirm()?;
    vol.staging.rename_noreplace(temp, inventory, name)?;
    inventory.sync()?;
    vol.staging.sync()
}

/// A snapshot within the bounds `read` takes, before anything is written: one past them
/// was written in full and then refused by every reader, on every later checkpoint too.
fn within_reading_bounds(lines: u64, volumes: &[Value], sources: usize) -> Res<()> {
    let records = volumes
        .iter()
        .map(|v| v["record_count"].as_u64().unwrap_or(u64::MAX))
        .max()
        .unwrap_or(0);
    let what = if lines > MAX_LINES {
        "the set has more versions than an inventory can list"
    } else if volumes.len() > MAX_VOLUMES {
        "the set has more cards than an inventory can list"
    } else if sources > MAX_SOURCES {
        "the set has more sources than an inventory can list"
    } else if records > MAX_RECORDS {
        "a card holds more records than an inventory can count"
    } else {
        return Ok(());
    };
    Err(CardError::new(Kind::Unsupported, what))
}

/// Write a new snapshot of what `store` knows onto the attached card. Returns its ID.
/// `through`: every record committed up to this time is in the catalogue.
pub fn write(vol: &Volume, store: &mut Store, through: &str) -> Res<String> {
    vol.ready_to_write()?;
    let (versions, lines) = version_lines(store)?;
    let volumes = volume_entries(store)?;
    let sources: Vec<Value> = store
        .sources()?
        .into_iter()
        .map(|s| {
            json!({"source_id": s.source_id, "display_name": s.display_name, "kind": s.kind,
                   "created_at": s.created_at, "retired_at": s.retired_at})
        })
        .collect();
    within_reading_bounds(lines, &volumes, sources.len())?;
    let room = vol.room()?;
    let needed = versions.len() as u64 + HEADER_LIMIT;
    if room.free < needed.saturating_add(room.unit.saturating_mul(2)) {
        return Err(CardError::new(
            Kind::NoRoom,
            "no room left on the card for the set's inventory",
        ));
    }
    let (epoch, revision) = store.take_revision()?;
    let id = format::new_uuid()?;
    let inventory = vol.nb.ensure_dir(format::INVENTORY)?;
    let versions_name = format!("{id}{VERSIONS_SUFFIX}");
    publish(
        vol,
        &inventory,
        &format!("{id}.versions.tmp"),
        &versions_name,
        &versions,
    )?;
    let header = json!({
        "schema_version": 1,
        "snapshot_id": id,
        "catalogue_epoch_id": epoch,
        "revision": revision,
        "exported_at": format::now(),
        "backup_set_id": vol.identity.backup_set_id,
        "set_display_name": vol.identity.set_display_name,
        "written_to_volume_id": vol.identity.volume_id,
        "includes_transactions_through": through,
        "sources": sources,
        "volumes": volumes,
        "versions_file": {
            "name": versions_name,
            "lines": lines,
            "byte_size": versions.len(),
            "sha256": format!("{:x}", Sha256::digest(&versions)),
        },
    });
    let bytes = format::to_line(&header);
    if bytes.len() as u64 > HEADER_LIMIT {
        return Err(CardError::new(
            Kind::Unsupported,
            "the set's inventory header is too large",
        ));
    }
    publish(
        vol,
        &inventory,
        &format!("{id}.inventory.tmp"),
        &format!("{id}{HEADER_SUFFIX}"),
        &bytes,
    )?;
    Ok(id)
}

fn opt_time(f: &Fields, name: &str) -> Res<Option<String>> {
    match f.0.get(name) {
        Some(Value::Null) => Ok(None),
        Some(_) => f.time(name).map(Some),
        None => Err(format::bad(name)),
    }
}

fn objects<'a>(
    map: &'a Map<String, Value>,
    name: &str,
    most: usize,
) -> Res<Vec<&'a Map<String, Value>>> {
    let list = map
        .get(name)
        .and_then(Value::as_array)
        .filter(|a| a.len() <= most)
        .ok_or_else(|| format::bad(name))?;
    list.iter()
        .map(|v| v.as_object().ok_or_else(|| format::bad(name)))
        .collect()
}

fn source_entry(m: &Map<String, Value>) -> Res<SourceRow> {
    let f = Fields(m);
    let kind = f.str("kind")?;
    if !matches!(kind, "immich-originals" | "nextcloud-files" | "other") {
        return Err(format::bad("kind"));
    }
    Ok(SourceRow {
        source_id: f.uuid("source_id")?,
        display_name: f.text("display_name", 64)?,
        kind: kind.to_string(),
        created_at: f.time("created_at")?,
        retired_at: opt_time(&f, "retired_at")?,
    })
}

fn volume_entry(m: &Map<String, Value>) -> Res<VolumeEntry> {
    let f = Fields(m);
    let result = match m.get("last_recheck_result") {
        Some(Value::Null) => None,
        Some(Value::String(s)) if s == "ok" || s == "damaged" => Some(s.clone()),
        _ => return Err(format::bad("last_recheck_result")),
    };
    let fs_type = f.str("filesystem_type")?;
    if fs_type != "ext4" && fs_type != "exfat" {
        return Err(format::bad("filesystem_type"));
    }
    Ok(VolumeEntry {
        volume_id: f.uuid("volume_id")?,
        sequence_number: f.int("sequence_number", 1, 9999)?,
        state: VolumeState::parse(f.str("state")?).ok_or_else(|| format::bad("state"))?,
        state_since: f.time("state_since")?,
        enrolled_at: f.time("enrolled_at")?,
        last_seen_at: opt_time(&f, "last_seen_at")?,
        last_recheck_at: opt_time(&f, "last_recheck_at")?,
        last_recheck_result: result,
        record_count: f.int("record_count", 0, MAX_RECORDS)?,
        payload_bytes: f.bytes("payload_bytes")?,
        filesystem_type: fs_type.to_string(),
        capacity_bytes: f.bytes("capacity_bytes")?,
    })
}

/// A header's fields, checked against its bounds and against the card it is on.
fn parse_header(bytes: &[u8], vol: &Volume, stem: &str) -> Res<Header> {
    let map = format::parse_bounded(bytes, HEADER_LIMIT, HEADER_DEPTH)?;
    let f = Fields(&map);
    f.int("schema_version", 1, 1)?;
    let file = map
        .get("versions_file")
        .and_then(Value::as_object)
        .ok_or_else(|| format::bad("versions_file"))?;
    let vf = Fields(file);
    let header = Header {
        snapshot_id: f.uuid("snapshot_id")?,
        catalogue_epoch_id: f.uuid("catalogue_epoch_id")?,
        revision: f.int("revision", 1, 9_007_199_254_740_991)?,
        exported_at: f.time("exported_at")?,
        backup_set_id: f.uuid("backup_set_id")?,
        set_display_name: f.text("set_display_name", 64)?,
        written_to_volume_id: f.uuid("written_to_volume_id")?,
        includes_transactions_through: f.time("includes_transactions_through")?,
        sources: objects(&map, "sources", MAX_SOURCES)?
            .into_iter()
            .map(source_entry)
            .collect::<Res<_>>()?,
        volumes: objects(&map, "volumes", MAX_VOLUMES)?
            .into_iter()
            .map(volume_entry)
            .collect::<Res<_>>()?,
        lines: vf.int("lines", 0, MAX_LINES)?,
        byte_size: vf.bytes("byte_size")?,
        sha256: vf.checked("sha256", format::is_sha256)?,
    };
    if header.snapshot_id != stem || vf.str("name")? != format!("{stem}{VERSIONS_SUFFIX}") {
        return Err(CardError::new(
            Kind::Invalid,
            "its name does not match its snapshot ID",
        ));
    }
    if header.written_to_volume_id != vol.identity.volume_id
        || header.backup_set_id != vol.identity.backup_set_id
    {
        return Err(CardError::new(
            Kind::Invalid,
            "it names another card or set than the one it is on",
        ));
    }
    Ok(header)
}

/// The version list's size, line count and checksum as the header names them: read
/// once in full before any line is believed.
fn check_versions(inventory: &Dir, header: &Header) -> Res<()> {
    let name = format!("{}{VERSIONS_SUFFIX}", header.snapshot_id);
    let mut file = inventory.open_file(&name)?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 1 << 16];
    let (mut size, mut lines) = (0u64, 0u64);
    loop {
        let n = file
            .read(&mut buf)
            .map_err(|e| CardError::io("read an inventory", &e))?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
        size += n as u64;
        lines += buf[..n].iter().filter(|&&b| b == b'\n').count() as u64;
        if size > header.byte_size || lines > MAX_LINES {
            break;
        }
    }
    if size != header.byte_size
        || lines != header.lines
        || format!("{:x}", hasher.finalize()) != header.sha256
    {
        return Err(CardError::new(
            Kind::Invalid,
            "its version list does not match its header",
        ));
    }
    Ok(())
}

fn version_line(bytes: &[u8]) -> Res<VersionLine> {
    let map = format::parse_bounded(bytes, LINE_LIMIT, 3)?;
    let f = Fields(&map);
    let path = f.text("path", 4096)?;
    format::check_relative_path(&path)?;
    let copies = objects(&map, "copies", MAX_COPIES)?
        .into_iter()
        .map(|c| {
            let cf = Fields(c);
            let state = match cf.str("state")? {
                "committed" => CopyState::Committed,
                "damaged" => CopyState::Damaged,
                "missing" => CopyState::Missing,
                "lost" => CopyState::Lost,
                _ => return Err(format::bad("state")),
            };
            Ok(CopyEntry {
                record_id: cf.uuid("record_id")?,
                volume_id: cf.uuid("volume_id")?,
                state,
                last_verified_at: opt_time(&cf, "last_verified_at")?,
            })
        })
        .collect::<Res<_>>()?;
    Ok(VersionLine {
        version_id: f.uuid("version_id")?,
        source_id: f.uuid("source_id")?,
        path,
        byte_size: f.bytes("byte_size")?,
        sha256: f.checked("sha256", format::is_sha256)?,
        copies,
    })
}

/// Every snapshot on the attached card, checked; nothing on the card is changed.
pub fn read(vol: &Volume) -> Res<Found> {
    let mut found = Found::default();
    let Some(inventory) = vol.existing(&vol.nb, format::INVENTORY)? else {
        return Ok(found);
    };
    let names = inventory.names()?;
    for name in &names {
        if let Some(stem) = name.strip_suffix(VERSIONS_SUFFIX) {
            if !(format::is_uuid(stem) && names.contains(&format!("{stem}{HEADER_SUFFIX}"))) {
                found.headless.push(name.clone());
            }
            continue;
        }
        let Some(stem) = name.strip_suffix(HEADER_SUFFIX) else {
            found.unknown.push(name.clone());
            continue;
        };
        if !format::is_uuid(stem) {
            found.unknown.push(name.clone());
            continue;
        }
        let checked = inventory
            .read_limited(name, HEADER_LIMIT)
            .and_then(|b| parse_header(&b, vol, stem))
            .and_then(|h| check_versions(&inventory, &h).map(|_| h));
        match checked {
            Ok(h) => found.valid.push(h),
            Err(e) => found.invalid.push((name.clone(), e.what)),
        }
    }
    Ok(found)
}

/// Each line of a snapshot's version list, handed to `each`. The list was checked
/// against its header by `read`; a line that does not parse now stops the reading.
pub fn each_version(
    vol: &Volume,
    header: &Header,
    mut each: impl FnMut(VersionLine) -> Res<()>,
) -> Res<()> {
    let inventory = vol.nb.open_dir(format::INVENTORY)?;
    let file = inventory.open_file(&format!("{}{VERSIONS_SUFFIX}", header.snapshot_id))?;
    let mut reader = BufReader::new(file.take(header.byte_size));
    let mut line = Vec::new();
    loop {
        line.clear();
        let n = reader
            .by_ref()
            .take(LINE_LIMIT + 1)
            .read_until(b'\n', &mut line)
            .map_err(|e| CardError::io("read an inventory", &e))?;
        if n == 0 {
            return Ok(());
        }
        if line.last() != Some(&b'\n') {
            return Err(CardError::new(
                Kind::Invalid,
                "an inventory line is too long or unfinished",
            ));
        }
        line.pop();
        each(version_line(&line)?)?;
    }
}

#[cfg(test)]
mod bounds {
    use super::*;

    #[test]
    fn a_snapshot_past_what_a_reader_takes_is_not_written() {
        // The review of 0.51.0: the writer checked none of the bounds the reader keeps.
        let card = |records: u64| json!({ "record_count": records });
        assert!(within_reading_bounds(MAX_LINES, &[card(MAX_RECORDS)], MAX_SOURCES).is_ok());
        for (lines, volumes, sources) in [
            (MAX_LINES + 1, vec![card(1)], 1),
            (1, vec![card(MAX_RECORDS + 1)], 1),
            (1, vec![card(1); MAX_VOLUMES + 1], 1),
            (1, vec![card(1)], MAX_SOURCES + 1),
        ] {
            let e = within_reading_bounds(lines, &volumes, sources).unwrap_err();
            assert_eq!(e.kind, Kind::Unsupported, "{}", e.what);
        }
    }
}
