//! Folder-relative file operations (conflict C1 of the integration report).
//!
//! A `Dir` is an open folder. Every open, create, rename, unlink and fsync the engine
//! does is relative to one, one name at a time, with the kernel's `openat2` refusing a
//! symbolic link, a step outside the folder and a step onto another mount. A check
//! followed by an open of a path is never used: if the card is pulled, the open folder
//! belongs to a filesystem that is gone and every call fails; nothing can reach the
//! empty folder underneath. `openat2` needs Linux 5.6 or newer; there is no weaker
//! fallback for card writes.
use super::error::{CardError, Kind, Res};
use std::ffi::CString;
use std::fs::File;
use std::io::Write;
use std::os::fd::{AsRawFd, FromRawFd, IntoRawFd, OwnedFd, RawFd};
use std::path::Path;

/// The status of a file or folder, as far as the engine uses it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Stat {
    pub dev: u64,
    pub ino: u64,
    pub size: u64,
    pub mtime_ns: i64,
    pub ctime_ns: i64,
    pub mode: u32,
}

impl Stat {
    fn from(st: &libc::stat) -> Stat {
        Stat {
            dev: st.st_dev,
            ino: st.st_ino,
            size: st.st_size as u64,
            mtime_ns: st.st_mtime * 1_000_000_000 + st.st_mtime_nsec,
            ctime_ns: st.st_ctime * 1_000_000_000 + st.st_ctime_nsec,
            mode: st.st_mode,
        }
    }
    pub fn is_file(&self) -> bool {
        self.mode & libc::S_IFMT == libc::S_IFREG
    }
    pub fn is_dir(&self) -> bool {
        self.mode & libc::S_IFMT == libc::S_IFDIR
    }
}

/// Free space of the filesystem an open folder is on.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Room {
    pub free: u64,
    pub capacity: u64,
    pub unit: u64,
    pub read_only: bool,
}

/// One part of a path: not empty, not `.` or `..`, no `/` and no NUL.
pub fn check_part(name: &str) -> Res<()> {
    let bad = name.is_empty()
        || name == "."
        || name == ".."
        || name.contains('/')
        || name.contains('\0')
        || name.len() > 255;
    if bad {
        return Err(CardError::new(Kind::Invalid, "a path part is not allowed"));
    }
    Ok(())
}

fn c_name(name: &str) -> Res<CString> {
    check_part(name)?;
    CString::new(name).map_err(|_| CardError::new(Kind::Invalid, "a path part holds NUL"))
}

fn openat2(dirfd: RawFd, name: &CString, flags: i32, mode: u32, resolve: u64) -> Res<OwnedFd> {
    // SAFETY: open_how is plain data; zeroed is its documented starting value.
    let mut how: libc::open_how = unsafe { std::mem::zeroed() };
    how.flags = (flags | libc::O_CLOEXEC | libc::O_NOFOLLOW) as u64;
    how.mode = if flags & libc::O_CREAT != 0 {
        mode as u64
    } else {
        0
    };
    how.resolve = resolve;
    // SAFETY: a NUL-terminated name, a valid open_how and its size, as openat2 takes them.
    let fd = unsafe {
        libc::syscall(
            libc::SYS_openat2,
            dirfd,
            name.as_ptr(),
            &how as *const libc::open_how,
            std::mem::size_of::<libc::open_how>(),
        )
    };
    if fd < 0 {
        let err = std::io::Error::last_os_error();
        if err.raw_os_error() == Some(libc::ENOSYS) {
            return Err(CardError::new(
                Kind::Unsupported,
                "card writes need Linux 5.6 or newer (openat2)",
            ));
        }
        return Err(CardError::io(
            &format!("open {}", name.to_string_lossy()),
            &err,
        ));
    }
    // SAFETY: openat2 returned a new descriptor that nothing else owns.
    Ok(unsafe { OwnedFd::from_raw_fd(fd as RawFd) })
}

const BENEATH: u64 = libc::RESOLVE_BENEATH | libc::RESOLVE_NO_SYMLINKS | libc::RESOLVE_NO_XDEV;

fn fstat(fd: RawFd) -> Res<Stat> {
    // SAFETY: stat is plain data filled in by fstat.
    let mut st: libc::stat = unsafe { std::mem::zeroed() };
    // SAFETY: a descriptor this process holds open and a stat to fill.
    if unsafe { libc::fstat(fd, &mut st) } != 0 {
        return Err(CardError::last("status of an open file"));
    }
    Ok(Stat::from(&st))
}

/// Close a file and report the error of `close` itself, which `drop` would hide.
pub fn close_checked(file: File) -> Res<()> {
    let fd = file.into_raw_fd();
    // SAFETY: the descriptor was taken from the File, so it is closed exactly once here.
    if unsafe { libc::close(fd) } != 0 {
        return Err(CardError::last("close"));
    }
    Ok(())
}

/// The status of an open file.
pub fn stat_file(file: &File) -> Res<Stat> {
    fstat(file.as_raw_fd())
}

#[derive(Debug)]
pub struct Dir {
    fd: OwnedFd,
}

impl Dir {
    /// Open a folder by its absolute path, refusing a symbolic link in any part of it.
    pub fn open_path(path: &Path) -> Res<Dir> {
        let name = CString::new(path.as_os_str().as_encoded_bytes())
            .map_err(|_| CardError::new(Kind::Invalid, "a path holds NUL"))?;
        let fd = openat2(
            libc::AT_FDCWD,
            &name,
            libc::O_RDONLY | libc::O_DIRECTORY,
            0,
            libc::RESOLVE_NO_SYMLINKS,
        )?;
        Ok(Dir { fd })
    }

    /// Open the folder `name` inside this one (no link, no other mount).
    pub fn open_dir(&self, name: &str) -> Res<Dir> {
        let fd = openat2(
            self.fd.as_raw_fd(),
            &c_name(name)?,
            libc::O_RDONLY | libc::O_DIRECTORY,
            0,
            BENEATH,
        )?;
        Ok(Dir { fd })
    }

    /// Open `name` inside this folder, making it first when it is missing. When it was
    /// made, this folder is fsynced so that the new name is durable.
    pub fn ensure_dir(&self, name: &str) -> Res<Dir> {
        let c = c_name(name)?;
        // SAFETY: an open folder descriptor and a NUL-terminated single name.
        let made = unsafe { libc::mkdirat(self.fd.as_raw_fd(), c.as_ptr(), 0o755) } == 0;
        if !made {
            let err = std::io::Error::last_os_error();
            if err.raw_os_error() != Some(libc::EEXIST) {
                return Err(CardError::io(&format!("make folder {name}"), &err));
            }
        }
        let dir = self.open_dir(name)?;
        if made {
            self.sync()?;
        }
        Ok(dir)
    }

    /// Create `name`, which must not exist yet, for writing.
    pub fn create_new(&self, name: &str) -> Res<File> {
        let fd = openat2(
            self.fd.as_raw_fd(),
            &c_name(name)?,
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL,
            0o644,
            BENEATH,
        )?;
        Ok(File::from(fd))
    }

    /// Open the regular file `name` to read.
    pub fn open_file(&self, name: &str) -> Res<File> {
        let fd = openat2(
            self.fd.as_raw_fd(),
            &c_name(name)?,
            libc::O_RDONLY | libc::O_NONBLOCK,
            0,
            BENEATH,
        )?;
        let file = File::from(fd);
        if !stat_file(&file)?.is_file() {
            return Err(CardError::new(Kind::Invalid, "not a regular file"));
        }
        Ok(file)
    }

    /// The status of `name` in this folder, without following a link. `None` when absent.
    pub fn stat_at(&self, name: &str) -> Res<Option<Stat>> {
        let c = c_name(name)?;
        // SAFETY: stat is plain data filled in by fstatat.
        let mut st: libc::stat = unsafe { std::mem::zeroed() };
        // SAFETY: an open folder, a NUL-terminated single name and a stat to fill.
        let rc = unsafe {
            libc::fstatat(
                self.fd.as_raw_fd(),
                c.as_ptr(),
                &mut st,
                libc::AT_SYMLINK_NOFOLLOW,
            )
        };
        if rc == 0 {
            return Ok(Some(Stat::from(&st)));
        }
        let err = std::io::Error::last_os_error();
        if err.raw_os_error() == Some(libc::ENOENT) {
            return Ok(None);
        }
        Err(CardError::io(&format!("status of {name}"), &err))
    }

    pub fn exists(&self, name: &str) -> Res<bool> {
        Ok(self.stat_at(name)?.is_some())
    }

    /// Move `from` in this folder to `to` in `target`, never replacing anything there.
    pub fn rename_noreplace(&self, from: &str, target: &Dir, to: &str) -> Res<()> {
        let (a, b) = (c_name(from)?, c_name(to)?);
        // SAFETY: two open folders and two NUL-terminated single names that outlive the call.
        let rc = unsafe {
            libc::syscall(
                libc::SYS_renameat2,
                self.fd.as_raw_fd(),
                a.as_ptr(),
                target.fd.as_raw_fd(),
                b.as_ptr(),
                libc::RENAME_NOREPLACE,
            )
        };
        if rc != 0 {
            let err = std::io::Error::last_os_error();
            if err.raw_os_error() == Some(libc::EINVAL) {
                return Err(CardError::new(
                    Kind::Unsupported,
                    "this filesystem cannot rename without replacing",
                ));
            }
            return Err(CardError::io(&format!("publish {to}"), &err));
        }
        Ok(())
    }

    /// Remove the file `name` from this folder.
    pub fn unlink(&self, name: &str) -> Res<()> {
        let c = c_name(name)?;
        // SAFETY: an open folder and a NUL-terminated single name.
        if unsafe { libc::unlinkat(self.fd.as_raw_fd(), c.as_ptr(), 0) } != 0 {
            return Err(CardError::last(&format!("remove {name}")));
        }
        Ok(())
    }

    /// Remove `name` when it is there; true when something was removed.
    pub fn unlink_if_present(&self, name: &str) -> Res<bool> {
        if !self.exists(name)? {
            return Ok(false);
        }
        self.unlink(name)?;
        Ok(true)
    }

    /// Make this folder's entries durable.
    pub fn sync(&self) -> Res<()> {
        // SAFETY: a descriptor this Dir owns.
        if unsafe { libc::fsync(self.fd.as_raw_fd()) } != 0 {
            return Err(CardError::last("fsync of a folder"));
        }
        Ok(())
    }

    pub fn stat(&self) -> Res<Stat> {
        fstat(self.fd.as_raw_fd())
    }

    /// Free space and capacity of the filesystem this folder is on.
    pub fn room(&self) -> Res<Room> {
        // SAFETY: statvfs is plain data filled in by fstatvfs.
        let mut sv: libc::statvfs = unsafe { std::mem::zeroed() };
        // SAFETY: a descriptor this Dir owns and a statvfs to fill.
        if unsafe { libc::fstatvfs(self.fd.as_raw_fd(), &mut sv) } != 0 {
            return Err(CardError::last("free space of the card"));
        }
        let unit = sv.f_frsize.max(1);
        Ok(Room {
            free: sv.f_bavail.saturating_mul(unit),
            capacity: sv.f_blocks.saturating_mul(unit),
            unit,
            read_only: sv.f_flag & libc::ST_RDONLY != 0,
        })
    }

    /// The names in this folder, sorted. Names that are not UTF-8 come back escaped.
    pub fn names(&self) -> Res<Vec<String>> {
        let at = format!("/proc/self/fd/{}", self.fd.as_raw_fd());
        let entries = std::fs::read_dir(&at).map_err(|e| CardError::io("list a folder", &e))?;
        let mut names = Vec::new();
        for entry in entries {
            let entry = entry.map_err(|e| CardError::io("list a folder", &e))?;
            names.push(entry.file_name().to_string_lossy().into_owned());
        }
        names.sort();
        Ok(names)
    }

    /// The raw names in this folder, sorted, without changing any byte.
    pub fn raw_names(&self) -> Res<Vec<std::ffi::OsString>> {
        let at = format!("/proc/self/fd/{}", self.fd.as_raw_fd());
        let entries = std::fs::read_dir(&at).map_err(|e| CardError::io("list a folder", &e))?;
        let mut names = Vec::new();
        for entry in entries {
            names.push(
                entry
                    .map_err(|e| CardError::io("list a folder", &e))?
                    .file_name(),
            );
        }
        names.sort();
        Ok(names)
    }

    /// Write `bytes` into the new file `name`, fsync it, close it checked, fsync this
    /// folder. Used for the intent, the verified intent and identity.json.
    pub fn write_new(&self, name: &str, bytes: &[u8]) -> Res<()> {
        let mut file = self.create_new(name)?;
        file.write_all(bytes)
            .map_err(|e| CardError::io(&format!("write {name}"), &e))?;
        file.sync_all()
            .map_err(|e| CardError::io(&format!("fsync {name}"), &e))?;
        close_checked(file)?;
        self.sync()
    }

    /// Read the file `name`, refusing one larger than `limit` bytes.
    pub fn read_limited(&self, name: &str, limit: u64) -> Res<Vec<u8>> {
        use std::io::Read;
        let file = self.open_file(name)?;
        let mut bytes = Vec::new();
        file.take(limit + 1)
            .read_to_end(&mut bytes)
            .map_err(|e| CardError::io(&format!("read {name}"), &e))?;
        if bytes.len() as u64 > limit {
            return Err(CardError::new(
                Kind::Invalid,
                &format!("{name} is too large"),
            ));
        }
        Ok(bytes)
    }
}
