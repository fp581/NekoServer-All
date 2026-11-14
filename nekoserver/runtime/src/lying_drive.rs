//! For the loop-device tests: a drive that does not keep what it was given. A file's first
//! bytes are changed in the drive's image file, under the mounted filesystem, the way a
//! fake-capacity or failing drive changes them. Linux's copy in memory still holds the
//! bytes that were written, so only a read from the drive itself sees the change.
use std::io::{Seek, SeekFrom, Write};
use std::path::Path;

/// Where `file` starts on its filesystem, in 4 KiB blocks, as `filefrag` says.
fn first_block(file: &Path) -> u64 {
    let out = std::process::Command::new("filefrag")
        .args(["-v", "-b4096"])
        .arg(file)
        .output()
        .expect("filefrag");
    let text = String::from_utf8_lossy(&out.stdout);
    // "   0:   0..  511:   34816..  35327:  512:  last,eof": the physical range's start.
    let line = text
        .lines()
        .find(|l| l.trim_start().starts_with("0:"))
        .unwrap_or_else(|| panic!("no extent for {file:?}: {text}"));
    let physical = line.split(':').nth(2).unwrap();
    physical.split("..").next().unwrap().trim().parse().unwrap()
}

/// Change the first bytes of `file` in the drive image `image` (the file must be written
/// out to the drive first: fsynced).
pub fn change_on_disk(image: &str, file: &Path) {
    let mut img = std::fs::OpenOptions::new().write(true).open(image).unwrap();
    img.seek(SeekFrom::Start(first_block(file) * 4096)).unwrap();
    img.write_all(b"EVIL").unwrap();
    img.sync_all().unwrap();
}
