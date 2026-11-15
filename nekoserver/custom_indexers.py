"""Download sources NekoServer adds to Prowlarr's catalogue itself (custom definitions).

Prowlarr reads extra indexer definitions from its Definitions/Custom folder when it
starts. NekoServer ships one: Torrentio (prowlarr-definitions/torrentio.yml). Setup copies
it there before Prowlarr starts (and restarts Prowlarr when the file is new or changed),
so it shows in the download-source picker like any other entry of the catalogue: not
ticked and not recommended. The uninstaller takes the file out again.

Only files NekoServer wrote are ever replaced or removed: a definition of the same name
that someone put there by hand is left as it is.
"""

import os
from pathlib import Path

from neko_common import atomic_write_at, open_folder_below

HERE = Path(__file__).resolve().parent
SHIPPED = HERE / 'prowlarr-definitions'
# Each definition NekoServer ships: its file name, and what the picker says about it.
DEFINITIONS = {
    'torrentio.yml': (
        'torrentio',
        'Finds only the films and shows Sonarr and Radarr search for; it never offers new releases on its own',
    ),
}
# The first line of every file NekoServer writes there, so it knows its own.
MARK = '# Installed by NekoServer (custom_indexers.py); setup replaces this file, edits are lost.\n'


def custom_folder(data_root):
    """Prowlarr's Definitions/Custom folder on the app SSD (its /config is
    appdata/prowlarr), checked to be inside the app SSD."""
    root = os.path.realpath(data_root)
    folder = os.path.realpath(os.path.join(root, 'appdata', 'prowlarr', 'Definitions', 'Custom'))
    if not folder.startswith(root + os.sep):
        raise RuntimeError("Prowlarr's custom definitions folder would be outside the app SSD")
    return Path(folder)


def shipped_text(name):
    """A definition as NekoServer installs it: the mark, then the shipped file."""
    return MARK + (SHIPPED / name).read_text(encoding='utf-8')


# Prowlarr's custom folder below the app SSD. Prowlarr's account owns these folders, so
# root reaches them from the SSD one by one, never through a link (open_folder_below).
CUSTOM_PARTS = ('appdata', 'prowlarr', 'Definitions', 'Custom')


def read_in(folder, name):
    """The text of `name` in the open folder, None when it is not there (never a link)."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=folder)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(f'Refusing symlink definition: {name} ({exc.strerror})') from None
    with os.fdopen(fd, encoding='utf-8', errors='replace') as stream:
        return stream.read()


def ours(path):
    """The file there is one NekoServer wrote (it starts with the mark)."""
    try:
        with open(path, encoding='utf-8') as stream:
            return stream.readline() == MARK
    except (OSError, UnicodeError):
        return False


def to_write(found, target, name, wanted, log):
    """The definition is to be written: it is not there (`found` None), or it is
    NekoServer's and differs. One someone else put there is left as it is, and said."""
    if found is None:
        return True
    if not found.startswith(MARK):
        log(f"Prowlarr: {target} was put there by someone else; NekoServer's {name} is not installed")
        return False
    return found != wanted


def install(data_root, uid, gid, log=print):
    """Copy every shipped definition into Prowlarr's custom folder, owned by the account
    Prowlarr runs as. Returns the names written (new or changed): Prowlarr reads the folder
    only when it starts, so it must be restarted when this is not empty."""
    shown = custom_folder(data_root)
    try:
        folder = open_folder_below(data_root, CUSTOM_PARTS, make=2, owner=(uid, gid))
    except OSError as exc:
        raise RuntimeError(
            f"Prowlarr's custom definitions folder {shown}, or a folder above it, is a link or not a folder now "
            f'({exc.strerror}), so nothing was written there: NekoServer never writes through a link. '
            'Put it back as a plain folder (or remove it), then run setup again.'
        ) from None
    written = []
    try:
        for name in DEFINITIONS:
            target = shown / name
            wanted = shipped_text(name)
            if not to_write(read_in(folder, name), target, name, wanted, log):
                continue
            atomic_write_at(folder, name, wanted, 0o644, owner=(uid, gid))
            written.append(name)
            log(f"Prowlarr: the download source {DEFINITIONS[name][0]} added to its catalogue ({target})")
    finally:
        os.close(folder)
    return written


def installed(data_root):
    """The definitions NekoServer installed that are there now (their paths)."""
    try:
        folder = custom_folder(data_root)
    except (RuntimeError, OSError):
        return []
    return [folder / name for name in DEFINITIONS if (folder / name).is_file() and ours(folder / name)]


def remove(data_root, log=print):
    """Take NekoServer's definitions out of Prowlarr's custom folder (the uninstaller).
    Returns the paths removed."""
    removed = []
    for path in installed(data_root):
        # Removed from the folder opened without links, not by path (see install).
        folder = open_folder_below(data_root, CUSTOM_PARTS)
        try:
            if (read_in(folder, path.name) or '').startswith(MARK):
                os.unlink(path.name, dir_fd=folder)
                removed.append(path)
                log(f'Removed {path}')
        finally:
            os.close(folder)
    return removed


def note(schema):
    """The picker's one line for a definition NekoServer ships, or None for any other."""
    names = {str(schema.get(key, '')).casefold() for key in ('definitionName', 'name')}
    for _file, (definition, said) in DEFINITIONS.items():
        if definition in names:
            return said
    return None
