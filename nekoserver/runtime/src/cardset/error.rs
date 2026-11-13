//! The card engine's error: a kind the run can act on, and the words for a person.
use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    /// The folder is not the mounted card: nothing may be written through it.
    NotMounted,
    /// The card's folder, device or identity changed after it was attached.
    MountChanged,
    /// Another set's card, an unknown card, or one whose identity disagrees.
    WrongVolume,
    /// Something this kernel, filesystem or format cannot do safely.
    Unsupported,
    /// The card has no room left for this file and its metadata.
    NoRoom,
    /// The source file changed, was replaced or vanished while it was read.
    SourceChanged,
    /// A source root is missing or is not the expected folder.
    SourceMissing,
    /// The bytes read back are not the bytes written.
    VerifyFailed,
    /// A metadata file is malformed, oversized or contradicts the card.
    Invalid,
    /// Another writer holds the set.
    Busy,
    /// A name that must be new already exists.
    Exists,
    /// A simulated crash (tests only produce it): stop at once, clean up nothing.
    Interrupted,
    /// Any other input or output error, with the system's error number.
    Io,
}

#[derive(Debug, Clone)]
pub struct CardError {
    pub kind: Kind,
    pub what: String,
    pub errno: Option<i32>,
}

impl CardError {
    pub fn new(kind: Kind, what: &str) -> Self {
        CardError {
            kind,
            what: what.to_string(),
            errno: None,
        }
    }
    /// The error of an input or output call, named by what was being done.
    pub fn io(what: &str, err: &std::io::Error) -> Self {
        let errno = err.raw_os_error();
        let kind = match errno {
            Some(libc::ENOSPC) | Some(libc::EDQUOT) => Kind::NoRoom,
            Some(libc::EEXIST) => Kind::Exists,
            _ => Kind::Io,
        };
        CardError {
            kind,
            what: format!("{what}: {err}"),
            errno,
        }
    }
    /// The last error of a system call.
    pub fn last(what: &str) -> Self {
        Self::io(what, &std::io::Error::last_os_error())
    }
    pub fn is_crash(&self) -> bool {
        self.kind == Kind::Interrupted
    }
}

impl fmt::Display for CardError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.what)
    }
}

pub type Res<T> = std::result::Result<T, CardError>;
