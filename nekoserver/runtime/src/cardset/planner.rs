//! Room on a card: the reserve, and whether a whole file fits (format document section 9,
//! V2 section 8 "Capacity"). A file is never split. A file that does not fit now waits
//! for the next card; one that cannot fit on any card like this one is named as needing
//! larger media, and no card is asked for because of it alone.
use super::format::SMALL_FILE;

const MIB: u64 = 1 << 20;

/// The room on the card as the planner sees it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Space {
    pub free: u64,
    pub capacity: u64,
    /// The filesystem's allocation unit.
    pub unit: u64,
    /// The largest file the filesystem can hold.
    pub max_file: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Fit {
    Fits,
    /// Not on what is left of this card; a fresh card of the same size would take it.
    NotNow,
    /// Larger than this kind of card can ever take (capacity or the filesystem's limit).
    NeedsLargerMedia,
}

/// The largest file of each supported filesystem: ext4 with 4 KiB blocks, exFAT.
pub fn max_file_size(filesystem_type: &str) -> u64 {
    match filesystem_type {
        "ext4" => 16 << 40,
        _ => u64::MAX,
    }
}

pub fn round_up(bytes: u64, unit: u64) -> u64 {
    let unit = unit.max(1);
    bytes.div_ceil(unit).saturating_mul(unit)
}

/// The space kept free: the larger of 256 MiB and 1 % of the capacity, plus three small
/// metadata files, the inventory budget (64 KiB and 512 bytes a version) and one unit
/// for each folder the file may need, each rounded up to the allocation unit.
pub fn reserve(space: &Space, versions_in_set: u64, new_folders: u64) -> u64 {
    let base = (256 * MIB).max(space.capacity / 100);
    let metadata = 3 * round_up(SMALL_FILE, space.unit);
    let inventory = round_up(64 * 1024 + 512 * (versions_in_set + 1), space.unit);
    let folders = new_folders * space.unit;
    round_up(base, space.unit) + metadata + inventory + folders
}

/// Whether a whole file of `size` bytes fits with the reserve left untouched.
pub fn fit(size: u64, space: &Space, reserve: u64) -> Fit {
    let needed = round_up(size, space.unit).saturating_add(reserve);
    if size > space.max_file || needed > space.capacity {
        return Fit::NeedsLargerMedia;
    }
    if needed > space.free {
        return Fit::NotNow;
    }
    Fit::Fits
}
