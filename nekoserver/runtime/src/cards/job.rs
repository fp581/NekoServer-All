//! `nekoserver-runtime cards`: the card job. Its timer starts it every night, an hour
//! after the other nightly jobs (modules/backup_cards.py), and "Write to the card now"
//! on the dashboard starts the same service. It writes only when a card of the set is
//! plugged in; with none, it only notes what waits for one and leaves its report as it
//! was. It holds the jobs' lock (the backups and the monthly update) and the set's own.
use super::drives::{self, Card};
use super::status::{self, Place, Seen};
use crate::backup;
use crate::cardset::commit;
use crate::cardset::error::{CardError, Kind};
use crate::cardset::format;
use crate::cardset::inventory;
use crate::cardset::lock::SetLock;
use crate::cardset::run::{self, Run, RunResult, RunState};
use crate::cardset::source::{Candidate, Skipped};
use crate::cardset::store::Store;
use crate::cardset::volume::Volume;

/// How the room on a card is found (cardset::run::card_space on a real card).
pub type Space<'a> =
    &'a mut dyn FnMut(&Volume) -> crate::cardset::error::Res<crate::cardset::planner::Space>;
use crate::common::{self, Error, Result};
use crate::Args;
use serde_json::{json, Value};

pub fn run(a: &Args) -> Result<()> {
    let backup::Job {
        cfgpath,
        cfg,
        data,
        media,
    } = backup::open_job(a, 64000)?;
    if !super::enabled(&cfg) {
        println!("Backup cards are switched off: nothing to do.");
        return Ok(());
    }
    let place = Place::on(&data);
    let Some(mut store) = place.existing()? else {
        println!("No backup card is set up yet: choose one on the dashboard's Backup cards page.");
        return Ok(());
    };
    let Some((set_id, _)) = store.backup_set().map_err(super::card_error)? else {
        println!("No backup card is set up yet: choose one on the dashboard's Backup cards page.");
        return Ok(());
    };
    let cards = drives::cards_of_set(&store, &set_id, &drives::plugged_in()?, &cfg);
    let sources = super::sources(&mut store, &super::wanted(&cfg, &data)?)?;
    let (cands, skipped) = super::candidates(&sources)?;
    if cards.is_empty() {
        // Only the status changes: the set's lock is enough (an enrolment holds it too).
        let _set = match SetLock::take(&place.folder, &set_id) {
            Err(e) if e.kind == Kind::Busy => {
                println!("A card is being set up on the dashboard: nothing to do now.");
                return Ok(());
            }
            taken => taken.map_err(super::card_error)?,
        };
        let counts = status::count(&store, &cands)?;
        status::save(
            &place,
            &store,
            &Seen {
                counts: Some(counts),
                ..Seen::default()
            },
        )?;
        println!(
            "No card of the set is plugged in: nothing was written. {} wait for the next card.",
            common::plural(counts.1, "file", "files")
        );
        return Ok(());
    }
    // The jobs' lock first (it may wait for the monthly update), then the set's.
    let _lock = backup::coordinate(&cfgpath, &data, &media)?;
    let _set = SetLock::take(&place.folder, &set_id).map_err(super::card_error)?;
    backup::execute_with(Some(&data), super::fact("job"), &cfgpath, || {
        let written = write(&mut store, &cards, &cands, &mut run::card_space)?;
        let counts = status::count(&store, &cands)?;
        let shown = status::save(&place, &store, &written.seen(counts))?;
        let missing = left_out(&written, &skipped, &shown);
        println!("{}", said(&written, counts));
        written.result()?;
        Ok(backup::Outcome {
            partial: (!missing.is_empty()).then(|| json!(missing)),
        })
    })
}

/// What the run did on each card it wrote to.
pub struct Written {
    pub runs: Vec<(u64, RunResult)>,
    pub plugged: Vec<u64>,
    /// The run on a card stopped (the card taken out, a card that could not be trusted).
    pub stopped: Option<(u64, CardError)>,
}

impl Written {
    fn seen(&self, counts: (u64, u64)) -> Seen {
        let ended = |s: RunState| {
            self.runs
                .iter()
                .filter(move |(_, r)| r.state == s)
                .map(|(n, _)| *n)
        };
        Seen {
            plugged: self.plugged.clone(),
            full: ended(RunState::WaitingForNextCard).collect(),
            not_full: ended(RunState::Complete)
                .chain(ended(RunState::Partial))
                .collect(),
            wrote: self.runs.iter().any(|(_, r)| r.state != RunState::Stopped),
            counts: Some(counts),
        }
    }
    /// A run that stopped is the job's failure, in words for the person.
    fn result(&self) -> Result<()> {
        match &self.stopped {
            None => Ok(()),
            Some((n, e)) => Err(Error::new(&stopped_words(*n, e))),
        }
    }
}

/// Why the run on card `n` stopped, and what to do.
pub fn stopped_words(n: u64, e: &CardError) -> String {
    match e.kind {
        Kind::NotMounted | Kind::MountChanged => format!(
            "Card {n} was taken out (or stopped answering) while it was written. Nothing on it is lost: plug it in again; the next run goes on where this one stopped"
        ),
        Kind::WrongVolume => format!(
            "Card {n} is not the card the server enrolled ({}): nothing was written to it",
            e.what
        ),
        Kind::Unsupported | Kind::Invalid => format!(
            "Card {n} could not be written ({}). Nothing was changed on it. Check it on another computer (fsck for ext4, chkdsk for exFAT), then plug it in again",
            e.what
        ),
        Kind::Busy => "Another write to the backup cards is running: this one waits for the next night".to_string(),
        _ => format!(
            "Card {n} could not be written: {}. Nothing on it is lost; the next run tries again",
            e.what
        ),
    }
}

/// Write onto each plugged-in card in turn, while files wait for room: a card that
/// fills up hands the rest to the next one plugged in. Every card's run starts with the
/// recovery of what a run before it left unfinished (cardset::reconcile).
/// `space`: the room on a card (run::card_space: what its filesystem says).
pub fn write(
    store: &mut Store,
    cards: &[Card],
    cands: &[Candidate],
    space: Space,
) -> Result<Written> {
    let mut written = Written {
        runs: vec![],
        plugged: cards.iter().map(|c| c.row.sequence_number).collect(),
        stopped: None,
    };
    let keep = common::SHARED["cards"]["keep_inventories"]
        .as_u64()
        .unwrap_or(3) as usize;
    for card in cards {
        let n = card.row.sequence_number;
        match one_card(store, card, cands, keep, &mut *space) {
            Ok(result) => {
                // The next plugged-in card takes over what this one could not: the rest
                // when it is full, and files too big for it (a bigger card may hold them).
                let more = result.state == RunState::WaitingForNextCard
                    || !result.needs_larger_media.is_empty();
                if let Some(e) = result.stopped.clone() {
                    written.stopped = Some((n, e));
                }
                written.runs.push((n, result));
                if !more || written.stopped.is_some() {
                    break;
                }
            }
            Err(e) => {
                written.stopped = Some((n, e));
                break;
            }
        }
    }
    Ok(written)
}

fn one_card(
    store: &mut Store,
    card: &Card,
    cands: &[Candidate],
    keep: usize,
    space: Space,
) -> std::result::Result<RunResult, CardError> {
    let vol = Volume::attach(&card.mount, &card.expect)?;
    vol.ready_to_write()?;
    store.saw_volume(&vol.identity, vol.room()?.capacity, &format::now())?;
    let run_id = format::new_uuid()?;
    let result = Run {
        vol: &vol,
        cat: store,
        run_id: &run_id,
        space,
        hook: &mut commit::no_hook,
    }
    .go(cands)?;
    if result.state != RunState::Stopped {
        // The set's inventory on the card, for a restore without the server.
        inventory::checkpoint(&vol, store, &format::now(), keep)?;
    }
    Ok(result)
}

/// What the run left out, as sentences the Backups page shows (the report's "missing").
pub fn left_out(w: &Written, skipped: &Skipped, shown: &Value) -> Vec<String> {
    let mut said = vec![];
    let last_card = w.runs.last();
    if let Some(n) = shown["full_card"].as_u64() {
        said.push(full_words(n));
    } else if let Some((n, _)) = last_card.filter(|(_, r)| r.state == RunState::WaitingForNextCard)
    {
        // Only an older card of the set was plugged in, and it is full: the newest card
        // has room, but it was not here, so what is new waits for it.
        let newest = shown["next_number"].as_u64().unwrap_or(n + 2) - 1;
        let waiting = shown["files_waiting"].as_u64().unwrap_or(0);
        if waiting > 0 {
            said.push(format!(
                "Card {n} is full: {} for card {newest}. Plug in card {newest}",
                common::plural(waiting, "file waits", "files wait")
            ));
        }
    }
    let last = last_card.map(|(_, r)| r);
    let deferred = last.map_or(0, |r| r.deferred.len());
    if deferred > 0 {
        said.push(format!(
            "{} changed while {} copied: {} tried again at the next run",
            common::plural(deferred, "file", "files"),
            if deferred == 1 { "it was" } else { "they were" },
            if deferred == 1 { "it is" } else { "they are" }
        ));
    }
    let larger = last.map_or(0, |r| r.needs_larger_media.len());
    if larger > 0 {
        said.push(format!(
            "{} larger than a card of this size can hold: use a bigger card for {}",
            common::plural(larger, "file is", "files are"),
            if larger == 1 { "it" } else { "them" }
        ));
    }
    if !skipped.unsupported.is_empty() {
        said.push(format!(
            "{} names a backup card cannot keep (not readable as text, or a path too long): not on the cards",
            common::plural(skipped.unsupported.len(), "file has", "files have")
        ));
    }
    if !skipped.other_mount.is_empty() {
        said.push(format!(
            "{} on another drive inside a folder that is backed up: not copied",
            common::plural(skipped.other_mount.len(), "folder is", "folders are")
        ));
    }
    said
}

/// The card full message (and the phone alert's words, collect.rs).
pub fn full_words(n: u64) -> String {
    format!(
        "Card {n} is full: put in a new card (it becomes card {})",
        n + 1
    )
}

/// The job's last line in its log.
fn said(w: &Written, (on, waiting): (u64, u64)) -> String {
    let copied: usize = w.runs.iter().map(|(_, r)| r.committed.len()).sum();
    let cards: Vec<String> = w.runs.iter().map(|(n, _)| n.to_string()).collect();
    format!(
        "Backup cards: {} copied onto card {}; {} on the cards, {} waiting for a card.",
        common::plural(copied, "file", "files"),
        if cards.is_empty() {
            "-".to_string()
        } else {
            cards.join(" and ")
        },
        on,
        waiting
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cardset::planner;
    use crate::cardset::testkit::{self, bytes, put, source, Scratch, MIB, RESERVE_FLOOR};

    /// A card of the set as `cards_of_set` hands it over, in a folder of the scratch.
    fn card(s: &Scratch, store: &mut Store, seq: u64) -> Card {
        let (path, vol) = testkit::card(s, seq);
        store
            .saw_volume(&vol.identity, RESERVE_FLOOR + 3 * MIB, &format::now())
            .unwrap();
        Card {
            mount: path.clone(),
            row: store.volume(&vol.identity.volume_id).unwrap().unwrap(),
            expect: testkit::expect(&path, seq),
        }
    }

    /// Cards of 3 MiB above the reserve, each its own: two files of 1.2 MiB fit on one.
    fn room(cards: &[Card]) -> impl FnMut(&Volume) -> crate::cardset::error::Res<planner::Space> {
        let mut each: Vec<_> = cards
            .iter()
            .map(|c| {
                (
                    c.row.volume_id.clone(),
                    testkit::space_of(c.mount.clone(), 3 * MIB),
                )
            })
            .collect();
        move |vol| {
            let (_, f) = each
                .iter_mut()
                .find(|(id, _)| id == vol.volume_id())
                .unwrap();
            f(vol)
        }
    }

    fn files(s: &Scratch, n: u8) -> Vec<Candidate> {
        let src = source(s, "photos", 1);
        for i in 0..n {
            put(&src, &format!("2001/{i}.jpg"), &bytes(i, 1_200_000));
        }
        testkit::cands(&src)
    }

    fn place(s: &Scratch) -> Place {
        Place {
            folder: s.dir("server"),
        }
    }

    #[test]
    fn a_full_card_hands_the_rest_to_the_next_one_and_the_newest_full_one_is_named() {
        let s = Scratch::new("job-full");
        let mut store = Store::in_memory().unwrap();
        store.start_set("Example set").unwrap();
        let cards = vec![card(&s, &mut store, 1), card(&s, &mut store, 2)];
        let cands = files(&s, 6);
        let w = write(&mut store, &cards, &cands, &mut room(&cards)).unwrap();
        assert_eq!(
            w.runs
                .iter()
                .map(|(n, r)| (*n, r.state))
                .collect::<Vec<_>>(),
            [
                (1, RunState::WaitingForNextCard),
                (2, RunState::WaitingForNextCard)
            ]
        );
        assert_eq!(w.runs[0].1.committed.len() + w.runs[1].1.committed.len(), 4);
        assert!(w.result().is_ok());
        let counts = status::count(&store, &cands).unwrap();
        assert_eq!(counts, (4, 2));
        let shown = status::save(&place(&s), &store, &w.seen(counts)).unwrap();
        assert_eq!(shown["full_card"], 2);
        assert_eq!(shown["next_number"], 3);
        assert_eq!(shown["files_waiting"], 2);
        let said = left_out(&w, &Skipped::default(), &shown);
        assert_eq!(
            said,
            ["Card 2 is full: put in a new card (it becomes card 3)"]
        );
        // Each card has the set's inventory, for a restore without the server.
        for c in &cards {
            assert!(
                inventory::read(&testkit::attach(&c.mount, c.row.sequence_number))
                    .unwrap()
                    .valid
                    .len()
                    == 1
            );
        }
    }

    #[test]
    fn a_file_too_big_for_one_card_goes_onto_a_bigger_card_plugged_in_beside_it() {
        let s = Scratch::new("job-bigger");
        let mut store = Store::in_memory().unwrap();
        store.start_set("Example set").unwrap();
        // Card 1 holds 3 MiB; card 2, plugged in beside it, 8 MiB.
        let small = card(&s, &mut store, 1);
        let (path, vol) = testkit::card(&s, 2);
        store
            .saw_volume(&vol.identity, RESERVE_FLOOR + 8 * MIB, &format::now())
            .unwrap();
        let big = Card {
            mount: path.clone(),
            row: store.volume(&vol.identity.volume_id).unwrap().unwrap(),
            expect: testkit::expect(&path, 2),
        };
        let src = source(&s, "photos", 1);
        put(&src, "2001/film.mp4", &bytes(1, 5 * 1024 * 1024));
        put(&src, "2001/photo.jpg", &bytes(2, 500_000));
        let cands = testkit::cands(&src);
        let mut sizes = [
            (
                small.row.volume_id.clone(),
                testkit::space_of(small.mount.clone(), 3 * MIB),
            ),
            (
                big.row.volume_id.clone(),
                testkit::space_of(big.mount.clone(), 8 * MIB),
            ),
        ];
        let mut space = |v: &Volume| {
            let (_, f) = sizes
                .iter_mut()
                .find(|(id, _)| id == v.volume_id())
                .unwrap();
            f(v)
        };
        let w = write(&mut store, &[small, big], &cands, &mut space).unwrap();
        assert_eq!(w.runs.len(), 2, "card 2 is tried too");
        assert_eq!(w.runs[0].1.needs_larger_media, ["2001/film.mp4"]);
        assert_eq!(w.runs[1].1.committed.len(), 1, "the film is on card 2");
        let counts = status::count(&store, &cands).unwrap();
        assert_eq!(counts, (2, 0));
        let shown = status::save(&place(&s), &store, &w.seen(counts)).unwrap();
        assert!(left_out(&w, &Skipped::default(), &shown).is_empty());
    }

    #[test]
    fn an_older_full_card_alone_is_not_a_finished_run() {
        let s = Scratch::new("job-older-full");
        let mut store = Store::in_memory().unwrap();
        store.start_set("Example set").unwrap();
        let place = place(&s);
        // Card 1 fills up; card 2, the newest, takes the rest and has room left.
        let first = vec![card(&s, &mut store, 1)];
        let cands = files(&s, 3);
        write(&mut store, &first, &cands, &mut room(&first)).unwrap();
        let second = vec![card(&s, &mut store, 2)];
        write(&mut store, &second, &cands, &mut room(&second)).unwrap();
        // New photos, and only the old full card plugged in tonight.
        let cands = files(&s, 5);
        let w = write(&mut store, &first, &cands, &mut room(&first)).unwrap();
        assert_eq!(w.runs[0].1.state, RunState::WaitingForNextCard);
        let counts = status::count(&store, &cands).unwrap();
        assert_eq!(counts.1, 2, "the two new photos are on no card");
        let shown = status::save(&place, &store, &w.seen(counts)).unwrap();
        assert_eq!(
            shown["full_card"],
            serde_json::Value::Null,
            "card 2 has room"
        );
        assert_eq!(
            left_out(&w, &Skipped::default(), &shown),
            ["Card 1 is full: 2 files wait for card 2. Plug in card 2"]
        );
    }

    #[test]
    fn a_card_with_room_takes_everything_and_is_no_longer_full() {
        let s = Scratch::new("job-room");
        let mut store = Store::in_memory().unwrap();
        store.start_set("Example set").unwrap();
        let first = vec![card(&s, &mut store, 1)];
        let cands = files(&s, 3);
        let place = place(&s);
        let w = write(&mut store, &first, &cands, &mut room(&first)).unwrap();
        let shown = status::save(
            &place,
            &store,
            &w.seen(status::count(&store, &cands).unwrap()),
        )
        .unwrap();
        assert_eq!(shown["full_card"], 1);
        // Card 2, the new one, takes the file left.
        let second = vec![card(&s, &mut store, 2)];
        let w = write(&mut store, &second, &cands, &mut room(&second)).unwrap();
        assert_eq!(w.runs[0].1.state, RunState::Complete);
        let shown = status::save(
            &place,
            &store,
            &w.seen(status::count(&store, &cands).unwrap()),
        )
        .unwrap();
        assert_eq!(shown["full_card"], serde_json::Value::Null, "{shown}");
        assert_eq!(
            (
                shown["files_on_cards"].as_u64(),
                shown["files_waiting"].as_u64()
            ),
            (Some(3), Some(0))
        );
        let numbers: Vec<_> = shown["cards"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| {
                (
                    c["number"].as_u64(),
                    c["full"].as_bool(),
                    c["plugged"].as_bool(),
                )
            })
            .collect();
        assert_eq!(
            numbers,
            [
                (Some(1), Some(true), Some(false)),
                (Some(2), Some(false), Some(true))
            ]
        );
        assert!(shown["last_write_at"].is_number());
        assert!(left_out(&w, &Skipped::default(), &shown).is_empty());
        // Run again: nothing new, nothing copied.
        let w = write(&mut store, &second, &cands, &mut room(&second)).unwrap();
        assert!(w.runs[0].1.committed.is_empty());
        assert_eq!(w.runs[0].1.unchanged.len(), 3);
    }

    #[test]
    fn a_card_taken_out_is_said_with_what_to_do() {
        let s = Scratch::new("job-out");
        let mut store = Store::in_memory().unwrap();
        store.start_set("Example set").unwrap();
        let cards = vec![card(&s, &mut store, 1)];
        std::fs::remove_dir_all(&cards[0].mount).unwrap();
        let w = write(&mut store, &cards, &files(&s, 1), &mut room(&cards)).unwrap();
        let e = w.result().unwrap_err().to_string();
        assert!(e.starts_with("Card 1 could not be written"), "{e}");
        let pulled = CardError::new(Kind::MountChanged, "the card's folder is gone");
        assert!(stopped_words(2, &pulled).contains("Card 2 was taken out"));
        assert!(stopped_words(2, &pulled).contains("plug it in again"));
    }
}
