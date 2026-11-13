//! Removable-media backup sets (0.51.0): the card copy engine.
//!
//! The on-card format and the commit protocol are those of
//! `plans/0.51.0-archive-format-v1.md` ("archive format 1"); the step names D1 to D8 in
//! the comments are that document's. This step builds the engine and its crash tests
//! only: nothing calls it yet (the job, the control operations and the dashboard come
//! later), hence the `dead_code` allowance below.
//!
//! Parts:
//! - `error`: the engine's own error, with a kind the job can act on.
//! - `fsat`: open, create, rename, unlink and fsync relative to an open folder, never
//!   through a path the engine did not open itself.
//! - `volume`: attaching a card (it must be the mounted, approved card), enrolment,
//!   free space, and the identity check repeated before every publication.
//! - `format`: `identity.json`, intents and records, written and read with their bounds.
//! - `catalogue`: what the server's catalogue must do for D1 and D7.
//! - `source`: reading a source file through descriptors and noticing a change.
//! - `planner`: the reserve, and which pending file fits (whole files only).
//! - `commit`: one file through D1 to D8.
//! - `reconcile`: the recovery of every gap between two durable steps.
//! - `run`: a run over the pending files on one card.
//! - `lock`: one writer per backup set.
#![allow(dead_code)]

pub mod catalogue;
pub mod commit;
pub mod error;
pub mod format;
pub mod fsat;
pub mod lock;
pub mod planner;
pub mod reconcile;
pub mod run;
pub mod source;
pub mod volume;

#[cfg(test)]
mod testkit;
#[cfg(test)]
mod tests_accept;
#[cfg(test)]
mod tests_crash;
#[cfg(test)]
mod tests_format;
#[cfg(test)]
mod tests_loop;
