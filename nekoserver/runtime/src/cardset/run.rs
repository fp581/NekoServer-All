//! A run over the pending files on one attached card.
//!
//! Before anything: reconcile, and refuse a source root that lies on the card. Then each
//! candidate in order: a normal run trusts size, modification time and status-change time
//! against the newest archived version with a healthy copy (the user's decision of
//! 2026-11-11, Q5); a file that looks new or changed is hashed first, and copied only when
//! its content is not already archived with a healthy copy. Whole files only: a file that
//! does not fit now waits for the next card while smaller ones go on; one that no card
//! like this can hold is named as needing larger media.
use super::catalogue::Catalogue;
use super::commit::{self, Hook, Job, Outcome, Replace};
use super::error::{CardError, Kind, Res};
use super::fsat::Dir;
use super::planner::{self, Fit, Space};
use super::reconcile::{self, Report};
use super::source::{self, Candidate};
use super::volume::Volume;

/// A file whose source keeps changing is tried this many times, then deferred.
pub const ATTEMPTS: u32 = 3;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RunState {
    /// Every candidate is archived (or unchanged).
    Complete,
    /// Some files fit on a fresh card: ask for the next one.
    WaitingForNextCard,
    /// Nothing more for any card, but something is not protected (deferred, unsupported,
    /// too large for this kind of card).
    Partial,
    /// The run stopped: the card was removed or could not be trusted.
    Stopped,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Decision {
    /// Same size and dates as a version with a healthy copy: skipped, not read.
    Unchanged,
    /// Other dates, same content as that version: the catalogue notes the dates.
    SameContent(String),
    /// Copy it: a new version, or a replacement copy of a version without a healthy one.
    Copy(Option<Replace>),
}

#[derive(Debug)]
pub struct RunResult {
    pub state: RunState,
    pub reconcile: Report,
    pub committed: Vec<String>,
    pub unchanged: Vec<String>,
    pub same_content: Vec<String>,
    pub waiting: Vec<String>,
    pub needs_larger_media: Vec<String>,
    pub deferred: Vec<(String, CardError)>,
    pub stopped: Option<CardError>,
}

impl RunResult {
    fn new(reconcile: Report) -> RunResult {
        RunResult {
            state: RunState::Complete,
            reconcile,
            committed: Vec::new(),
            unchanged: Vec::new(),
            same_content: Vec::new(),
            waiting: Vec::new(),
            needs_larger_media: Vec::new(),
            deferred: Vec::new(),
            stopped: None,
        }
    }
    fn settle(&mut self) {
        self.state = if self.stopped.is_some() {
            RunState::Stopped
        } else if !self.waiting.is_empty() {
            RunState::WaitingForNextCard
        } else if self.deferred.is_empty() && self.needs_larger_media.is_empty() {
            RunState::Complete
        } else {
            RunState::Partial
        };
    }
}

/// The room on a real card.
pub fn card_space(vol: &Volume) -> Res<Space> {
    let room = vol.room()?;
    Ok(Space {
        free: room.free,
        capacity: room.capacity,
        unit: room.unit,
        max_file: planner::max_file_size(&vol.identity.filesystem_type),
    })
}

/// What a normal run does with one candidate.
pub fn decide(cat: &dyn Catalogue, cand: &Candidate) -> Res<Decision> {
    let Some(version) = cat.newest_version(&cand.source.source_id, &cand.path)? else {
        return Ok(Decision::Copy(None));
    };
    let copies = cat.copies_of(&version.version_id)?;
    let healthy = copies.iter().any(|c| c.state.protects());
    let same_dates = version.byte_size == cand.size
        && version.mtime_ns == cand.mtime_ns
        && version.ctime_ns == cand.ctime_ns;
    if same_dates && healthy {
        return Ok(Decision::Unchanged);
    }
    let (checksum, size) = source::hash_candidate(cand)?;
    if checksum != version.checksum || size != version.byte_size {
        return Ok(Decision::Copy(None));
    }
    if healthy {
        return Ok(Decision::SameContent(version.version_id));
    }
    Ok(Decision::Copy(Some(Replace {
        version_id: version.version_id,
        record_id: copies.first().map(|c| c.record_id.clone()),
        checksum,
    })))
}

pub struct Run<'a> {
    pub vol: &'a Volume,
    pub cat: &'a mut dyn Catalogue,
    pub run_id: &'a str,
    pub space: &'a mut dyn FnMut(&Volume) -> Res<Space>,
    pub hook: &'a mut dyn Hook,
}

enum Flow {
    Go,
    Stop,
}

impl Run<'_> {
    /// Archive `cands` onto the attached card.
    pub fn go(&mut self, cands: &[Candidate]) -> Res<RunResult> {
        let report = reconcile::reconcile(self.vol, self.cat)?;
        let mut result = RunResult::new(report);
        if let Err(e) = self.may_start(&result.reconcile, cands) {
            result.stopped = Some(e);
            result.settle();
            return Ok(result);
        }
        let mut full = false;
        for cand in cands {
            if let Flow::Stop = self.one(cand, &mut full, &mut result) {
                break;
            }
        }
        result.settle();
        Ok(result)
    }

    fn may_start(&self, report: &Report, cands: &[Candidate]) -> Res<()> {
        if report.needs_filesystem_check {
            return Err(CardError::new(
                Kind::Unsupported,
                "this card needs a filesystem check on another computer before more writes",
            ));
        }
        if !reconcile::staging_clear(self.vol, self.cat)? {
            return Err(CardError::new(
                Kind::Invalid,
                "the card's staging folder holds files recovery could not explain",
            ));
        }
        let mut roots: Vec<&std::path::Path> =
            cands.iter().map(|c| c.source.root.as_path()).collect();
        roots.sort();
        roots.dedup();
        for root in roots {
            let device = Dir::open_path(root)
                .and_then(|d| d.stat())
                .map_err(|e| {
                    CardError::new(
                        Kind::SourceMissing,
                        &format!("a source is not available: {e}"),
                    )
                })?
                .dev;
            if self.vol.overlaps(root, device) {
                return Err(CardError::new(
                    Kind::Invalid,
                    "a source folder and the card overlap: nothing is copied",
                ));
            }
        }
        Ok(())
    }

    fn one(&mut self, cand: &Candidate, full: &mut bool, result: &mut RunResult) -> Flow {
        match decide(self.cat, cand) {
            Ok(Decision::Unchanged) => result.unchanged.push(cand.path.clone()),
            Ok(Decision::SameContent(version_id)) => {
                if let Err(e) = self
                    .cat
                    .note_dates(&version_id, cand.mtime_ns, cand.ctime_ns)
                {
                    result.stopped = Some(e);
                    return Flow::Stop;
                }
                result.same_content.push(cand.path.clone());
            }
            Ok(Decision::Copy(replace)) => return self.place(cand, replace.as_ref(), full, result),
            Err(e) => result.deferred.push((cand.path.clone(), e)),
        }
        Flow::Go
    }

    fn place(
        &mut self,
        cand: &Candidate,
        replace: Option<&Replace>,
        full: &mut bool,
        result: &mut RunResult,
    ) -> Flow {
        if *full {
            result.waiting.push(cand.path.clone());
            return Flow::Go;
        }
        let counted = (self.space)(self.vol).and_then(|s| Ok((s, self.cat.version_count()?)));
        let (space, versions) = match counted {
            Ok(found) => found,
            Err(e) => {
                result.stopped = Some(e);
                return Flow::Stop;
            }
        };
        let reserve = planner::reserve(&space, versions, 2);
        match planner::fit(cand.size, &space, reserve) {
            Fit::NeedsLargerMedia => result.needs_larger_media.push(cand.path.clone()),
            Fit::NotNow => result.waiting.push(cand.path.clone()),
            Fit::Fits => return self.attempt(cand, replace, full, result),
        }
        Flow::Go
    }

    fn attempt(
        &mut self,
        cand: &Candidate,
        replace: Option<&Replace>,
        full: &mut bool,
        result: &mut RunResult,
    ) -> Flow {
        let job = Job {
            cand,
            run_id: self.run_id,
            replace,
        };
        for attempt in 1..=ATTEMPTS {
            match commit::commit_file(self.vol, self.cat, &job, self.hook) {
                Ok(Outcome::Committed { record, .. }) => {
                    result.committed.push(record.txn().record_id.clone());
                    return Flow::Go;
                }
                Ok(Outcome::Deferred(e)) if e.kind == Kind::SourceChanged && attempt < ATTEMPTS => {
                }
                Ok(Outcome::Deferred(e)) if e.kind == Kind::NoRoom => {
                    *full = true;
                    result.waiting.push(cand.path.clone());
                    return Flow::Go;
                }
                Ok(Outcome::Deferred(e)) => {
                    result.deferred.push((cand.path.clone(), e));
                    return Flow::Go;
                }
                Err(e) => {
                    result.stopped = Some(e);
                    return Flow::Stop;
                }
            }
        }
        Flow::Go
    }
}
