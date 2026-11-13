//! Archive format 1: identity.json, the intent, the verified intent and the record,
//! written and read with the bounds of sections 1, 3, 4 and 5 of the format document.
//! A reader refuses a missing field, a wrong type and a value out of bounds; unknown
//! fields are ignored.
use super::error::{CardError, Kind, Res};
use serde_json::{json, Map, Value};
use std::fs::File;
use std::io::Read;

pub const ROOT: &str = "NekoBackup";
pub const STAGING: &str = "staging";
pub const OBJECTS: &str = "objects";
pub const RECORDS: &str = "records";
pub const INVENTORY: &str = "inventory";
pub const IDENTITY: &str = "identity.json";
pub const README: &str = "README.txt";
/// identity.json, an intent, a verified intent and a record are at most this large.
pub const SMALL_FILE: u64 = 16_384;
const SMALL_DEPTH: usize = 4;
pub const WRITTEN_BY: &str = concat!("nekoserver ", env!("CARGO_PKG_VERSION"));

// ---- ids and times ---------------------------------------------------------------

/// A random version 4 UUID from the kernel's random source.
pub fn new_uuid() -> Res<String> {
    let mut b = [0u8; 16];
    File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut b))
        .map_err(|e| CardError::io("random source", &e))?;
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    let h: String = b.iter().map(|x| format!("{x:02x}")).collect();
    Ok(format!(
        "{}-{}-{}-{}-{}",
        &h[0..8],
        &h[8..12],
        &h[12..16],
        &h[16..20],
        &h[20..32]
    ))
}

pub fn now() -> String {
    chrono::Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

pub fn is_uuid(s: &str) -> bool {
    let b = s.as_bytes();
    if b.len() != 36 {
        return false;
    }
    let hex = |c: u8| c.is_ascii_digit() || (b'a'..=b'f').contains(&c);
    let shape = b.iter().enumerate().all(|(i, &c)| match i {
        8 | 13 | 18 | 23 => c == b'-',
        _ => hex(c),
    });
    shape && b[14] == b'4' && matches!(b[19], b'8' | b'9' | b'a' | b'b')
}

pub fn is_time(s: &str) -> bool {
    s.len() == 20 && chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%dT%H:%M:%SZ").is_ok()
}

pub fn is_sha256(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}

fn parse_ns(s: &str) -> Option<i64> {
    if s.is_empty() || s.len() > 20 {
        return None;
    }
    let digits = s.strip_prefix('-').unwrap_or(s);
    if digits.is_empty() || !digits.bytes().all(|c| c.is_ascii_digit()) {
        return None;
    }
    s.parse().ok()
}

/// The safe extension of a file name: after the last dot, lower-cased, 1 to 8 of a-z 0-9.
pub fn safe_extension(file_name: &str) -> String {
    let Some((stem, ext)) = file_name.rsplit_once('.') else {
        return String::new();
    };
    let ext = ext.to_ascii_lowercase();
    let fits = !stem.is_empty()
        && (1..=8).contains(&ext.len())
        && ext
            .bytes()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit());
    if fits {
        format!(".{ext}")
    } else {
        String::new()
    }
}

/// `objects/<hh>/<record_id><ext>`, relative to NekoBackup.
pub fn archive_path(record_id: &str, ext: &str) -> String {
    format!("{OBJECTS}/{}/{record_id}{ext}", &record_id[..2])
}

/// A path below a source root: parts split at `/`, none empty, `.` or `..`, no NUL,
/// each at most 255 bytes and the whole at most 4096.
pub fn check_relative_path(path: &str) -> Res<()> {
    if path.is_empty() || path.len() > 4096 {
        return Err(CardError::new(Kind::Invalid, "path empty or too long"));
    }
    for part in path.split('/') {
        super::fsat::check_part(part)?;
    }
    Ok(())
}

// ---- reading with bounds ---------------------------------------------------------

fn depth(v: &Value) -> usize {
    match v {
        Value::Object(m) => 1 + m.values().map(depth).max().unwrap_or(0),
        Value::Array(a) => 1 + a.iter().map(depth).max().unwrap_or(0),
        _ => 0,
    }
}

/// Parse one small metadata file: UTF-8 JSON, no byte-order mark, an object, bounded.
pub fn parse_small(bytes: &[u8]) -> Res<Map<String, Value>> {
    if bytes.len() as u64 > SMALL_FILE {
        return Err(CardError::new(Kind::Invalid, "metadata file too large"));
    }
    if bytes.starts_with(&[0xef, 0xbb, 0xbf]) {
        return Err(CardError::new(
            Kind::Invalid,
            "metadata file has a byte-order mark",
        ));
    }
    let value: Value = serde_json::from_slice(bytes)
        .map_err(|_| CardError::new(Kind::Invalid, "metadata file is not JSON"))?;
    if depth(&value) > SMALL_DEPTH {
        return Err(CardError::new(
            Kind::Invalid,
            "metadata file nested too deep",
        ));
    }
    match value {
        Value::Object(map) => Ok(map),
        _ => Err(CardError::new(
            Kind::Invalid,
            "metadata file is not an object",
        )),
    }
}

fn bad(field: &str) -> CardError {
    CardError::new(
        Kind::Invalid,
        &format!("field {field} missing or out of bounds"),
    )
}

struct Fields<'a>(&'a Map<String, Value>);

impl Fields<'_> {
    fn str(&self, f: &str) -> Res<&str> {
        self.0.get(f).and_then(Value::as_str).ok_or_else(|| bad(f))
    }
    fn checked(&self, f: &str, ok: fn(&str) -> bool) -> Res<String> {
        let s = self.str(f)?;
        if !ok(s) {
            return Err(bad(f));
        }
        Ok(s.to_string())
    }
    fn uuid(&self, f: &str) -> Res<String> {
        self.checked(f, is_uuid)
    }
    fn opt_uuid(&self, f: &str) -> Res<Option<String>> {
        match self.0.get(f) {
            Some(Value::Null) => Ok(None),
            Some(_) => self.uuid(f).map(Some),
            None => Err(bad(f)),
        }
    }
    fn opt_sha(&self, f: &str) -> Res<Option<String>> {
        match self.0.get(f) {
            Some(Value::Null) => Ok(None),
            Some(_) => self.checked(f, is_sha256).map(Some),
            None => Err(bad(f)),
        }
    }
    fn time(&self, f: &str) -> Res<String> {
        self.checked(f, is_time)
    }
    fn text(&self, f: &str, max: usize) -> Res<String> {
        let s = self.str(f)?;
        if s.len() > max || s.contains('\0') {
            return Err(bad(f));
        }
        Ok(s.to_string())
    }
    fn ns(&self, f: &str) -> Res<i64> {
        parse_ns(self.str(f)?).ok_or_else(|| bad(f))
    }
    fn bytes(&self, f: &str) -> Res<u64> {
        let n = self
            .0
            .get(f)
            .and_then(Value::as_u64)
            .ok_or_else(|| bad(f))?;
        if n > 9_007_199_254_740_991 {
            return Err(bad(f));
        }
        Ok(n)
    }
    fn int(&self, f: &str, lo: u64, hi: u64) -> Res<u64> {
        let n = self
            .0
            .get(f)
            .and_then(Value::as_u64)
            .ok_or_else(|| bad(f))?;
        if n < lo || n > hi {
            return Err(bad(f));
        }
        Ok(n)
    }
    fn exact(&self, f: &str, want: &str) -> Res<()> {
        if self.str(f)? != want {
            return Err(bad(f));
        }
        Ok(())
    }
    fn source_identity(&self, f: &str) -> Res<SourceIdentity> {
        let map = self
            .0
            .get(f)
            .and_then(Value::as_object)
            .ok_or_else(|| bad(f))?;
        let inner = Fields(map);
        let unsigned = |name: &str| -> Res<u64> {
            let s = inner.str(name)?;
            if s.is_empty() || s.len() > 20 || !s.bytes().all(|c| c.is_ascii_digit()) {
                return Err(bad(name));
            }
            s.parse().map_err(|_| bad(name))
        };
        Ok(SourceIdentity {
            device: unsigned("device")?,
            inode: unsigned("inode")?,
            ctime_ns: inner.ns("ctime_ns")?,
        })
    }
}

// ---- identity.json ---------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Identity {
    pub backup_set_id: String,
    pub volume_id: String,
    pub sequence_number: u64,
    pub set_display_name: String,
    pub created_at: String,
    pub filesystem_type: String,
    pub filesystem_uuid: String,
    pub privacy_acknowledged_at: String,
}

impl Identity {
    pub fn label(&self) -> String {
        format!("{} - {:03}", self.set_display_name, self.sequence_number)
    }
    pub fn to_bytes(&self) -> Vec<u8> {
        let v = json!({
            "format": "neko-backup-volume",
            "archive_format_version": 1,
            "format_minor": 0,
            "backup_set_id": self.backup_set_id,
            "volume_id": self.volume_id,
            "sequence_number": self.sequence_number,
            "set_display_name": self.set_display_name,
            "volume_label": self.label(),
            "created_at": self.created_at,
            "filesystem_type": self.filesystem_type,
            "filesystem_uuid": self.filesystem_uuid,
            "encrypted": false,
            "privacy_acknowledged_at": self.privacy_acknowledged_at,
            "written_by": WRITTEN_BY,
        });
        to_line(&v)
    }
    pub fn parse(bytes: &[u8]) -> Res<Identity> {
        let map = parse_small(bytes)?;
        let f = Fields(&map);
        f.exact("format", "neko-backup-volume")?;
        let version = map.get("archive_format_version").and_then(Value::as_u64);
        if version != Some(1) {
            return Err(CardError::new(
                Kind::Unsupported,
                &format!("archive format {version:?} is not format 1: read only"),
            ));
        }
        f.int("format_minor", 0, 999)?;
        if map.get("encrypted") != Some(&Value::Bool(false)) {
            return Err(bad("encrypted"));
        }
        let fs_type = f.str("filesystem_type")?;
        if fs_type != "ext4" && fs_type != "exfat" {
            return Err(bad("filesystem_type"));
        }
        let identity = Identity {
            backup_set_id: f.uuid("backup_set_id")?,
            volume_id: f.uuid("volume_id")?,
            sequence_number: f.int("sequence_number", 1, 9999)?,
            set_display_name: f.text("set_display_name", 64)?,
            created_at: f.time("created_at")?,
            filesystem_type: fs_type.to_string(),
            filesystem_uuid: f.text("filesystem_uuid", 64)?,
            privacy_acknowledged_at: f.time("privacy_acknowledged_at")?,
        };
        if identity.set_display_name.is_empty() || f.str("volume_label")? != identity.label() {
            return Err(bad("volume_label"));
        }
        Ok(identity)
    }
}

// ---- intent, verified intent, record --------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SourceIdentity {
    pub device: u64,
    pub inode: u64,
    pub ctime_ns: i64,
}

impl SourceIdentity {
    fn to_value(self) -> Value {
        json!({
            "device": self.device.to_string(),
            "inode": self.inode.to_string(),
            "ctime_ns": self.ctime_ns.to_string(),
        })
    }
}

/// What every staging file and the record of one transaction share.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Txn {
    pub transaction_id: String,
    pub run_id: String,
    pub record_id: String,
    pub version_id: String,
    pub replaces_record_id: Option<String>,
    pub backup_set_id: String,
    pub volume_id: String,
    pub source_id: String,
    pub original_relative_path: String,
    pub archive_relative_path: String,
    pub source_mtime_ns: i64,
    pub source_identity: SourceIdentity,
}

impl Txn {
    fn put(&self, m: &mut Map<String, Value>) {
        let pairs = [
            ("transaction_id", json!(self.transaction_id)),
            ("run_id", json!(self.run_id)),
            ("record_id", json!(self.record_id)),
            ("version_id", json!(self.version_id)),
            ("replaces_record_id", json!(self.replaces_record_id)),
            ("backup_set_id", json!(self.backup_set_id)),
            ("volume_id", json!(self.volume_id)),
            ("source_id", json!(self.source_id)),
            ("original_relative_path", json!(self.original_relative_path)),
            ("archive_relative_path", json!(self.archive_relative_path)),
            ("source_mtime_ns", json!(self.source_mtime_ns.to_string())),
            ("source_identity", self.source_identity.to_value()),
        ];
        for (k, v) in pairs {
            m.insert(k.to_string(), v);
        }
    }
    fn read(f: &Fields) -> Res<Txn> {
        let txn = Txn {
            transaction_id: f.uuid("transaction_id")?,
            run_id: f.uuid("run_id")?,
            record_id: f.uuid("record_id")?,
            version_id: f.uuid("version_id")?,
            replaces_record_id: f.opt_uuid("replaces_record_id")?,
            backup_set_id: f.uuid("backup_set_id")?,
            volume_id: f.uuid("volume_id")?,
            source_id: f.uuid("source_id")?,
            original_relative_path: f.text("original_relative_path", 4096)?,
            archive_relative_path: f.text("archive_relative_path", 64)?,
            source_mtime_ns: f.ns("source_mtime_ns")?,
            source_identity: f.source_identity("source_identity")?,
        };
        check_relative_path(&txn.original_relative_path)?;
        txn.check_archive_path()?;
        Ok(txn)
    }
    /// The archive path must be `objects/<hh>/<record_id>[.ext]` for this record's ID.
    fn check_archive_path(&self) -> Res<()> {
        let prefix = format!("{OBJECTS}/{}/{}", &self.record_id[..2], self.record_id);
        let ext = self.archive_relative_path.strip_prefix(&prefix);
        let ok = match ext {
            Some("") => true,
            Some(e) => {
                let body = e.strip_prefix('.').unwrap_or("");
                (1..=8).contains(&body.len())
                    && body
                        .bytes()
                        .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
            }
            None => false,
        };
        if !ok {
            return Err(bad("archive_relative_path"));
        }
        Ok(())
    }
    /// The payload's file name in `objects/<hh>/`.
    pub fn payload_name(&self) -> &str {
        self.archive_relative_path.rsplit('/').next().unwrap_or("")
    }
    pub fn hh(&self) -> &str {
        &self.record_id[..2]
    }
    pub fn record_name(&self) -> String {
        format!("{}.json", self.record_id)
    }
    pub fn file_name(&self) -> &str {
        self.original_relative_path.rsplit('/').next().unwrap_or("")
    }
}

/// `staging/<transaction_id>.intent.json`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Intent {
    pub txn: Txn,
    pub expected_checksum: Option<String>,
    pub observed_byte_size: u64,
    pub created_at: String,
}

impl Intent {
    pub fn to_bytes(&self) -> Vec<u8> {
        let mut m = Map::new();
        m.insert("schema_version".into(), json!(1));
        m.insert("kind".into(), json!("intent"));
        self.txn.put(&mut m);
        m.insert("expected_checksum".into(), json!(self.expected_checksum));
        m.insert("observed_byte_size".into(), json!(self.observed_byte_size));
        m.insert("created_at".into(), json!(self.created_at));
        to_line(&Value::Object(m))
    }
    pub fn parse(bytes: &[u8]) -> Res<Intent> {
        let map = parse_small(bytes)?;
        let f = Fields(&map);
        f.int("schema_version", 1, 1)?;
        f.exact("kind", "intent")?;
        Ok(Intent {
            txn: Txn::read(&f)?,
            expected_checksum: f.opt_sha("expected_checksum")?,
            observed_byte_size: f.bytes("observed_byte_size")?,
            created_at: f.time("created_at")?,
        })
    }
}

/// Every field of the record except `committed_at` and `completion_state`: the
/// verified intent holds exactly this, and the record adds the two.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CopyMeta {
    pub txn: Txn,
    pub source_display_name: String,
    pub byte_size: u64,
    pub checksum: String,
    pub source_observed_at: String,
    pub copy_verified_at: String,
}

impl CopyMeta {
    fn map(&self, kind: Option<&str>) -> Map<String, Value> {
        let mut m = Map::new();
        m.insert("schema_version".into(), json!(1));
        if let Some(k) = kind {
            m.insert("kind".into(), json!(k));
        }
        self.txn.put(&mut m);
        let pairs = [
            ("source_display_name", json!(self.source_display_name)),
            ("original_file_name", json!(self.txn.file_name())),
            ("byte_size", json!(self.byte_size)),
            ("checksum_algorithm", json!("sha256")),
            ("checksum", json!(self.checksum)),
            ("source_observed_at", json!(self.source_observed_at)),
            ("copy_verified_at", json!(self.copy_verified_at)),
            (
                "consistency",
                json!({"mode": "live-file-observation", "snapshot": null}),
            ),
            ("written_by", json!(WRITTEN_BY)),
        ];
        for (k, v) in pairs {
            m.insert(k.to_string(), v);
        }
        m
    }
    fn read(f: &Fields) -> Res<CopyMeta> {
        f.int("schema_version", 1, 1)?;
        f.exact("checksum_algorithm", "sha256")?;
        let meta = CopyMeta {
            txn: Txn::read(f)?,
            source_display_name: f.text("source_display_name", 64)?,
            byte_size: f.bytes("byte_size")?,
            checksum: f.checked("checksum", is_sha256)?,
            source_observed_at: f.time("source_observed_at")?,
            copy_verified_at: f.time("copy_verified_at")?,
        };
        if f.text("original_file_name", 255)? != meta.txn.file_name() {
            return Err(bad("original_file_name"));
        }
        Ok(meta)
    }
    pub fn verified_bytes(&self) -> Vec<u8> {
        to_line(&Value::Object(self.map(Some("verified-intent"))))
    }
    pub fn parse_verified(bytes: &[u8]) -> Res<CopyMeta> {
        let map = parse_small(bytes)?;
        let f = Fields(&map);
        f.exact("kind", "verified-intent")?;
        CopyMeta::read(&f)
    }
}

/// `records/<hh>/<record_id>.json`: one committed copy.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Record {
    pub meta: CopyMeta,
    pub committed_at: String,
}

impl Record {
    pub fn to_bytes(&self) -> Vec<u8> {
        let mut m = self.meta.map(None);
        m.insert("committed_at".into(), json!(self.committed_at));
        m.insert("completion_state".into(), json!("committed"));
        to_line(&Value::Object(m))
    }
    pub fn parse(bytes: &[u8]) -> Res<Record> {
        let map = parse_small(bytes)?;
        let f = Fields(&map);
        f.exact("completion_state", "committed")?;
        Ok(Record {
            meta: CopyMeta::read(&f)?,
            committed_at: f.time("committed_at")?,
        })
    }
    pub fn txn(&self) -> &Txn {
        &self.meta.txn
    }
}

fn to_line(v: &Value) -> Vec<u8> {
    let mut bytes = serde_json::to_vec_pretty(v).unwrap_or_default();
    bytes.push(b'\n');
    bytes
}
