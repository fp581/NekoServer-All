//! One writer per backup set (A36). The lock is an flock on a file in a folder only root
//! can write (`/run/nekoserver-cardset/` when the job exists); it belongs to the open
//! file, so it ends with the process, also when the process is killed.
use super::error::{CardError, Kind, Res};
use super::format::is_uuid;
use std::fs::{File, OpenOptions};
use std::os::fd::AsRawFd;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;

#[derive(Debug)]
pub struct SetLock {
    _file: File,
}

impl SetLock {
    /// Take the lock of set `set_id`; `Busy` when another writer has it.
    pub fn take(folder: &Path, set_id: &str) -> Res<SetLock> {
        if !is_uuid(set_id) {
            return Err(CardError::new(Kind::Invalid, "not a backup set ID"));
        }
        let file = OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(folder.join(format!("set-{set_id}.lock")))
            .map_err(|e| CardError::io("open the set's lock", &e))?;
        // SAFETY: flock on a descriptor this function owns; released when it is closed.
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return Err(CardError::new(
                Kind::Busy,
                "another backup to this set is running",
            ));
        }
        Ok(SetLock { _file: file })
    }
}
