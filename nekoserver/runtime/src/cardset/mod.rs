//! Removable-media backup sets (0.51.0): the card copy engine.
//!
//! The on-card format and the commit protocol are those of
//! `plans/0.51.0-archive-format-v1.md` ("archive format 1"); the step names D1 to D8 in
//! the comments are that document's. The card job, the Backup cards page and the status
//! the dashboard shows (`crate::cards`, 0.52.0) call it; the few parts nothing calls yet
//! (the rebuild of a lost catalogue) say so where they are.
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
//! - `store`: the catalogue in SQLite (decision Q2).
//! - `inventory`: the set inventory snapshot written onto each card at a checkpoint.
//! - `rebuild`: the catalogue rebuilt from the cards and their inventories.
//! - `lock`: one writer per backup set.

pub mod catalogue;
pub mod commit;
pub mod error;
pub mod format;
pub mod fsat;
pub mod inventory;
pub mod lock;
pub mod planner;
pub mod rebuild;
pub mod recheck;
pub mod reconcile;
pub mod run;
pub mod source;
pub mod store;
pub mod volume;

#[cfg(test)]
pub mod testkit;
#[cfg(test)]
mod tests_accept;
#[cfg(test)]
mod tests_buttons;
#[cfg(test)]
mod tests_catalogue;
#[cfg(test)]
mod tests_crash;
#[cfg(test)]
mod tests_format;
#[cfg(test)]
mod tests_growth;
#[cfg(test)]
mod tests_loop;
#[cfg(test)]
mod tests_restore_tool;
