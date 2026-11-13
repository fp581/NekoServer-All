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

from neko_common import atomic_write

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


def ours(path):
    """The file there is one NekoServer wrote (it starts with the mark)."""
    try:
        with open(path, encoding='utf-8') as stream:
            return stream.readline() == MARK
    except (OSError, UnicodeError):
        return False


def to_write(target, name, wanted, log):
    """The definition is to be written: it is not there, or it is NekoServer's and differs.
    One someone else put there is left as it is, and said."""
    if not target.exists():
        return True
    if not ours(target):
        log(f"Prowlarr: {target} was put there by someone else; NekoServer's {name} is not installed")
        return False
    return target.read_text(encoding='utf-8') != wanted


def made_for(folder, uid, gid):
    """The custom folder and its parent, made for Prowlarr's account when they are not there."""
    for part in (folder.parent, folder):
        if not part.exists():
            part.mkdir(mode=0o755)
            os.chown(part, uid, gid, follow_symlinks=False)


def install(data_root, uid, gid, log=print):
    """Copy every shipped definition into Prowlarr's custom folder, owned by the account
    Prowlarr runs as. Returns the names written (new or changed): Prowlarr reads the folder
    only when it starts, so it must be restarted when this is not empty."""
    folder = custom_folder(data_root)
    written = []
    for name in DEFINITIONS:
        target = folder / name
        if target.is_symlink():
            raise RuntimeError(f'Refusing symlink definition: {target}')
        wanted = shipped_text(name)
        if not to_write(target, name, wanted, log):
            continue
        made_for(folder, uid, gid)
        atomic_write(target, wanted, 0o644)
        os.chown(target, uid, gid, follow_symlinks=False)
        written.append(name)
        log(f"Prowlarr: the download source {DEFINITIONS[name][0]} added to its catalogue ({target})")
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
        path.unlink()
        removed.append(path)
        log(f'Removed {path}')
    return removed


def note(schema):
    """The picker's one line for a definition NekoServer ships, or None for any other."""
    names = {str(schema.get(key, '')).casefold() for key in ('definitionName', 'name')}
    for _file, (definition, said) in DEFINITIONS.items():
        if definition in names:
            return said
    return None
