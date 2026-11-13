//! The sources: reading a file through descriptors, noticing a change, and listing the
//! candidates of a source root (V2 section 7, format document section 7).
use super::error::{CardError, Kind, Res};
use super::format::check_relative_path;
use super::fsat::{self, Dir, Stat};
use sha2::{Digest, Sha256};
use std::fs::File;
use std::io::Read;
use std::os::unix::ffi::OsStrExt;
use std::path::PathBuf;

/// One source root, as the server defines it (Immich's originals, Nextcloud's files).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Source {
    pub source_id: String,
    pub display_name: String,
    pub root: PathBuf,
}

/// One file of a source, as the scan saw it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Candidate {
    pub source: Source,
    pub path: String,
    pub size: u64,
    pub mtime_ns: i64,
    pub ctime_ns: i64,
}

/// What a scan found that is not a candidate.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Skipped {
    /// Links, devices, sockets, FIFOs: never copied.
    pub special: Vec<String>,
    /// Names format 1 cannot keep losslessly (not UTF-8, path too long): not protected.
    pub unsupported: Vec<String>,
    /// Folders on another mount inside the source: a source error.
    pub other_mount: Vec<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Scan {
    pub candidates: Vec<Candidate>,
    pub skipped: Skipped,
}

/// The open source file and the folder it is in, with what was seen when it was opened.
pub struct Opened {
    pub file: File,
    pub parent: Dir,
    pub name: String,
    pub before: Stat,
}

/// Open the source root itself; a missing root is an error, never an empty source.
pub fn open_root(source: &Source) -> Res<Dir> {
    Dir::open_path(&source.root).map_err(|e| {
        CardError::new(
            Kind::SourceMissing,
            &format!("source {} is not available: {e}", source.display_name),
        )
    })
}

/// Open `cand` through its folders one by one: no link, no other mount on the way.
pub fn open(cand: &Candidate) -> Res<Opened> {
    check_relative_path(&cand.path)?;
    let mut dir = open_root(&cand.source)?;
    let mut parts: Vec<&str> = cand.path.split('/').collect();
    let name = parts.pop().unwrap_or_default().to_string();
    for part in parts {
        dir = dir.open_dir(part).map_err(gone)?;
    }
    let file = dir.open_file(&name).map_err(gone)?;
    let before = fsat::stat_file(&file)?;
    Ok(Opened {
        file,
        parent: dir,
        name,
        before,
    })
}

fn gone(err: CardError) -> CardError {
    CardError::new(
        Kind::SourceChanged,
        &format!("the source file moved or vanished: {err}"),
    )
}

fn same(a: &Stat, b: &Stat) -> bool {
    a.dev == b.dev
        && a.ino == b.ino
        && a.size == b.size
        && a.mtime_ns == b.mtime_ns
        && a.ctime_ns == b.ctime_ns
}

impl Opened {
    /// After reading: the still-open file and the path both show what was seen before.
    pub fn unchanged(&self) -> Res<()> {
        let now_open = fsat::stat_file(&self.file)?;
        let now_path = self.parent.stat_at(&self.name)?;
        let path_same = now_path.is_some_and(|p| same(&p, &self.before));
        if !same(&now_open, &self.before) || !path_same {
            return Err(CardError::new(
                Kind::SourceChanged,
                "the source file changed while it was copied",
            ));
        }
        Ok(())
    }
}

/// The SHA-256 of an open file from its current position, and the number of bytes.
pub fn hash_reader(mut r: impl Read) -> Res<(String, u64)> {
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 1 << 16];
    let mut total = 0u64;
    loop {
        let n = r.read(&mut buf).map_err(|e| CardError::io("read", &e))?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
        total += n as u64;
    }
    Ok((format!("{:x}", hasher.finalize()), total))
}

/// The SHA-256 of a source file as it is now, read through descriptors, and whether it
/// stayed the same while it was read.
pub fn hash_candidate(cand: &Candidate) -> Res<(String, u64)> {
    let opened = open(cand)?;
    let result = hash_reader(&opened.file)?;
    opened.unchanged()?;
    Ok(result)
}

/// List the regular files below a source root, sorted by path, without following a link
/// or crossing into another mount.
pub fn scan(source: &Source) -> Res<Scan> {
    let root = open_root(source)?;
    let device = root.stat()?.dev;
    let mut out = Scan::default();
    let mut stack = vec![(root, String::new())];
    while let Some((dir, prefix)) = stack.pop() {
        for raw in dir.raw_names()? {
            let entry = Entry::read(&dir, &prefix, raw.as_bytes())?;
            entry.sort_into(source, device, &dir, &mut out, &mut stack)?;
        }
    }
    out.candidates.sort_by(|a, b| a.path.cmp(&b.path));
    Ok(out)
}

struct Entry {
    name: Option<String>,
    shown: String,
    stat: Option<Stat>,
}

impl Entry {
    fn read(dir: &Dir, prefix: &str, raw: &[u8]) -> Res<Entry> {
        let name = std::str::from_utf8(raw).ok().map(str::to_string);
        let shown = format!("{prefix}{}", String::from_utf8_lossy(raw));
        let stat = match &name {
            Some(n) => dir.stat_at(n)?,
            None => None,
        };
        Ok(Entry { name, shown, stat })
    }

    fn sort_into(
        self,
        source: &Source,
        device: u64,
        dir: &Dir,
        out: &mut Scan,
        stack: &mut Vec<(Dir, String)>,
    ) -> Res<()> {
        let (Some(name), Some(st)) = (self.name, self.stat) else {
            out.skipped.unsupported.push(self.shown);
            return Ok(());
        };
        if st.is_dir() && st.dev != device {
            out.skipped.other_mount.push(self.shown);
        } else if st.is_dir() {
            stack.push((dir.open_dir(&name)?, format!("{}/", self.shown)));
        } else if !st.is_file() {
            out.skipped.special.push(self.shown);
        } else if check_relative_path(&self.shown).is_err() {
            out.skipped.unsupported.push(self.shown);
        } else {
            out.candidates.push(Candidate {
                source: source.clone(),
                path: self.shown,
                size: st.size,
                mtime_ns: st.mtime_ns,
                ctime_ns: st.ctime_ns,
            });
        }
        Ok(())
    }
}
