"""Setup brings this server's own Nextcloud back from its backup on the HDD (the user's
design for blocker T1-R5 of the 1.0 round).

The case: only the SSD broke and was replaced. Nextcloud's settings, its program folder
and its database were on the SSD and are gone; its files are still on the HDD, and so is
its backup: the monthly snapshots (backups/monthly: appdata/nextcloud and
compose/nextcloud, the password files sealed for the backup key) and the weekly copies of
its database (backups/nextcloud-database), and since row C-01 of the 1.0 round the
nightly backup (backups/nextcloud-nightly: its program folder, settings and database, from
the first night on). The newest of them is used. Setup refused ("already holds files") and no
path was left for a first-time user.

Now setup, at the terminal or in the setup window only (never unattended: a dashboard
upgrade, a timer), says what it found and asks one yes/no question. The backup must be
this server's: the files folder keeps Nextcloud's own folder appdata_<instance id>, and
the backup must name that id (in its config.php, and in its database). Everything is put
together in a staging folder on the SSD first and switched into place only at the end;
a step that fails puts the SSD back as it was, and the files folder is never written
over, moved or deleted."""

import collections
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile
import time

import guided_setup
from neko_common import checked_run, cloud_files, compose_up, safe_path

MONTHLY_DIR = 'backups/monthly'  # on the HDD (recovery_wizard.MONTHLY_DIR)
# The nightly backup of Nextcloud (row C-01, made by the nightly app backup, app_snapshot.rs):
# snapshot-<time>-<number> folders (named as the nightly app snapshots), with its program folder and settings in
# data.tar (never the database's folder), its database as nextcloud.sql, and the password
# files sealed in secrets.tar.cms as a monthly snapshot's.
NIGHTLY_DIR = 'backups/nextcloud-nightly'
NIGHTLY = 'nextcloud-nightly'  # its manifest's kind
SQL = 'nextcloud.sql'
HTML = 'appdata/nextcloud/html'
POSTGRES = 'appdata/nextcloud/postgres'
MONTHLY = re.compile(r'\d+-\d+', re.ASCII)  # a monthly snapshot's folder: <time>-<number>
NIGHTLY_RUN = re.compile(r'snapshot-\d+-\d+', re.ASCII)  # a nightly backup's folder (app_snapshot.rs naming)
DUMPS_DIR = 'backups/nextcloud-database'  # on the HDD (backup.rs NEXTCLOUD_DUMPS)
PARTS = ('appdata/nextcloud', 'compose/nextcloud')  # what a monthly snapshot keeps of Nextcloud
CONFIG_PHP = 'appdata/nextcloud/html/config/config.php'
VERSION_PHP = 'appdata/nextcloud/html/version.php'
ENV = 'compose/nextcloud/.env'
MANAGED = 'compose/nextcloud/managed.json'
PINS = 'compose/nextcloud/maintenance-images.json'  # the monthly update's image IDs (sha256:...)
STAGING = '.nextcloud-restoring'  # under the data SSD: the restore is put together here
WWW_DATA = 33
# Nextcloud's own folder in its files folder, named by its instance id (backup.rs nextcloud_internal).
OWN_FOLDER = re.compile(r'appdata_([a-z0-9]{2,40})')
INSTANCE_ID = re.compile(rb"'instanceid'\s*=>\s*'([a-z0-9]{2,40})'")
DB_USER = re.compile(rb"'dbuser'\s*=>\s*'(\w{1,63})'")  # bytes: \w is [A-Za-z0-9_] only
DB_PASSWORD = re.compile(rb"'dbpassword'\s*=>\s*'((?:[^'\\]|\\.)*)'")  # PHP: \' and \\ inside
VERSION = re.compile(rb"\$OC_VersionString\s*=\s*'(\d+)\.\d+\.\d+'")
DUMP = re.compile(r'nextcloud-\d{8}T\d{6}Z\.sql', re.ASCII)  # ASCII: \d is [0-9] only
CHUNK = 1 << 20
LOOKED_AT = 3  # the newest monthly snapshots looked at; older ones are older than these anyway
READY_WAIT = 1800
STOP_ON_ERROR = 'ON_ERROR_STOP=1'  # psql -v: stop at the first error (and exit non-zero)

NONE_YET = (
    'No backup of Nextcloud\'s settings exists yet, so this server\'s Nextcloud cannot be brought back: the '
    'nightly backup saves them first in the first night after setup, and the HDD holds none. Your files stay '
    'as they are in {files}: start a new, empty Nextcloud as said above, make its accounts again and copy your '
    'files into Nextcloud'
)
FOREIGN = (
    'The Nextcloud backup on the HDD ({date}) is not this server\'s: it does not name the folder '
    'appdata_{ids} that Nextcloud keeps in {files}, so it was not brought back'
)
DAMAGED = (
    'The Nextcloud backup on the HDD (of {dates}) is damaged: a file of it does not match its checksum, so it '
    'was not brought back'
)


class Backup:
    """This server's newest usable Nextcloud backup on the HDD."""

    def __init__(self, snapshot, manifest, version, config_php):
        self.snapshot, self.manifest, self.version, self.config_php = snapshot, manifest, version, config_php
        self.dump = None  # (path, note) of a newer database copy, when there is one

    @property
    def sealed(self):
        return self.manifest.get('password_files') == 'encrypted'

    @property
    def nightly(self):
        return self.manifest.get('kind') == NIGHTLY

    @property
    def made(self):
        """When what it brings back was saved: its database copy's time, else its own."""
        return self.dump[1]['created_at'] if self.dump else self.manifest['created_at']

    @property
    def date(self):
        return shown(self.made)


def shown(seconds):
    """A backup's time as the recovery menu shows it (its local time)."""
    try:
        return dt.datetime.fromtimestamp(float(seconds), dt.timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M %Z')
    except (ValueError, OverflowError, OSError):
        return 'unknown date'


def monthly_snapshots(folder, nightly=False):
    """(path, manifest) of the monthly snapshots or nightly backups (<time>-<number>, snapshot-<time>-<number>
    folders) in `folder` that keep Nextcloud whole, newest first. Setup reads them itself:
    the recovery menu is never started by setup (tests/test_recovery_sections.py)."""
    try:
        named = NIGHTLY_RUN if nightly else MONTHLY
        paths = [Path(folder) / name for name in os.listdir(folder) if named.fullmatch(name)]
    except OSError:
        return []
    found = [
        (path, manifest)
        for path in paths
        if path.is_dir() and not path.is_symlink()
        for manifest in [snapshot_manifest(path)]
        if manifest and (manifest.get('kind') == NIGHTLY) == nightly
    ]
    return sorted(found, key=lambda run: run[1]['created_at'], reverse=True)


def own_ids(files):
    """The instance ids of the Nextcloud folders (appdata_<id>) in the files folder."""
    try:
        names = [entry.name for entry in os.scandir(files) if entry.is_dir(follow_symlinks=False)]
    except OSError:
        return set()
    return {match[1] for match in map(OWN_FOLDER.fullmatch, names) if match}


def needles(ids):
    return [f'appdata_{i}'.encode() for i in sorted(ids)]


def holds(stream, wanted):
    """Whether one of the `wanted` byte strings is in the rest of `stream`."""
    keep = max(map(len, wanted)) - 1
    tail = b''
    for chunk in iter(lambda: stream.read(CHUNK), b''):
        window = tail + chunk
        if any(w in window for w in wanted):
            return True
        tail = window[-keep:] if keep else b''
    return False


class Hashing:
    """A file read through tarfile with every byte counted into a checksum."""

    def __init__(self, raw):
        self.raw, self.digest = raw, hashlib.sha256()

    def read(self, size=-1):
        data = self.raw.read(size)
        self.digest.update(data)
        return data


def snapshot_manifest(path):
    """A monthly snapshot's or a nightly backup's manifest when it keeps Nextcloud whole,
    else None."""
    try:
        manifest = json.loads((path / 'manifest.json').read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict) or manifest.get('format') != 'neko-maintenance-v1':
        return None
    sums = manifest.get('sha256') if isinstance(manifest.get('sha256'), dict) else {}
    if not isinstance(sums.get('data'), str) or not isinstance(manifest.get('created_at'), (int, float)):
        return None
    if manifest.get('password_files') == 'encrypted' and not (path / 'secrets.tar.cms').is_file():
        return None
    return manifest if keeps_nextcloud(path, manifest, sums) else None


def keeps_nextcloud(path, manifest, sums):
    """Whether a manifest lists all of Nextcloud: a nightly backup its program folder,
    settings and database copy, a monthly snapshot appdata/nextcloud and its settings."""
    data = (manifest.get('paths') or {}).get('data') or []
    nightly = manifest.get('kind') == NIGHTLY
    parts = (HTML, PARTS[1]) if nightly else PARTS
    if 'nextcloud' not in (manifest.get('apps') or []) or not all(p in data for p in parts):
        return False
    return not nightly or (isinstance(sums.get('database'), str) and (path / SQL).is_file())


def read_rest(stream):
    """The rest of `stream` read and dropped (a Hashing stream counts it into its checksum)."""
    collections.deque(iter(lambda: stream.read(CHUNK), b''), maxlen=0)


def read_checked(path, expected, reader):
    """`reader` over every byte of the file at `path`, each byte also counted into its
    checksum; what `reader` returned, or None when the file does not match `expected`."""
    with open(path, 'rb') as raw:
        counted = Hashing(raw)
        result = reader(counted)
        read_rest(counted)
    return result if counted.digest.hexdigest() == expected else None


def member_facts(tar, wanted):
    """(names one of `wanted` in its database, Nextcloud's major version, config.php) of
    the members of a snapshot's data.tar."""
    named, version, config_php = False, None, None
    for member in tar:
        if not member.isfile():
            continue
        name = member.name.removeprefix('./')
        stream = tar.extractfile(member)
        if name == CONFIG_PHP:
            config_php = stream.read(1_000_000)
        elif name == VERSION_PHP:
            found = VERSION.search(stream.read(1_000_000))
            version = found[1].decode() if found else None
        elif not named and name.startswith('appdata/nextcloud/postgres/'):
            named = holds(stream, wanted)
    return named, version, config_php


def looked_at(snapshot, manifest, ids):
    """(this server's, Nextcloud's major version, config.php) of a monthly snapshot or a
    nightly backup, read in one pass over its data.tar (and its nextcloud.sql); (None, None,
    None) when one does not match its checksum.
    "This server's": its config.php names one of `ids` (a snapshot made without a backup
    key keeps it in data.tar), or, with config.php sealed, its database names the folder
    appdata_<id>."""

    def facts(stream):
        with tarfile.open(fileobj=stream, mode='r|') as tar:
            return member_facts(tar, needles(ids))

    found = read_checked(snapshot / 'data.tar', manifest['sha256']['data'], facts)
    if found is None:
        return None, None, None
    named, version, config_php = found
    if manifest.get('kind') == NIGHTLY:
        # A nightly backup keeps its database as nextcloud.sql, not as the database's folder.
        named = read_checked(snapshot / SQL, manifest['sha256']['database'], lambda s: holds(s, needles(ids)))
        if named is None:
            return None, None, None
    if config_php is not None:
        instance = INSTANCE_ID.search(config_php)
        named = bool(instance) and instance[1].decode() in ids
    return named, version, config_php


def dump_notes(folder):
    """(path, note) of the database copies in `folder` that have a note with their time,
    newest first (they are named by their time)."""
    try:
        names = sorted((n for n in os.listdir(folder) if DUMP.fullmatch(n)), reverse=True)
    except OSError:
        return []
    found = []
    for name in names:
        try:
            note = json.loads((folder / f'{name}.json').read_text())
        except (OSError, ValueError):
            continue
        if isinstance(note, dict) and isinstance(note.get('created_at'), (int, float)):
            found.append((folder / name, note))
    return found


def same_major(path, note, version, log):
    """Whether a database copy was made by the major Nextcloud version the snapshot holds."""
    major = str(note.get('nextcloud_version') or '').split('.')[0]
    if not version or major == version:
        return True
    log(
        f'Nextcloud: the database copy {path.name} was made by Nextcloud {note.get("nextcloud_version")}, '
        f'the snapshot holds Nextcloud {version}: not used'
    )
    return False


def newer_dump(media, after, version, ids, log):
    """The newest database copy made after the snapshot (`after`, its time) by the same
    major Nextcloud version, that matches its checksum and names this server's folder:
    (path, note), or None."""
    for path, note in dump_notes(media / DUMPS_DIR):
        if note['created_at'] <= after:
            break  # the rest are older still
        if same_major(path, note, version, log) and read_checked(
            path, note.get('sha256'), lambda stream: holds(stream, needles(ids))
        ):
            return path, note
    return None


def own_sql(backup):
    """A nightly backup's own database copy, as (path, note) like a weekly copy's."""
    sums = backup.manifest['sha256']
    note = {k: backup.manifest.get(k) for k in ('created_at', 'nextcloud_version')}
    return backup.snapshot / SQL, {**note, 'sha256': sums['database']}


def own_backup(media, runs, ids, log, damaged):
    """This server's newest backup among `runs`, with the newest database copy for it (a
    newer weekly copy, else a nightly backup's own); None when none is. The dates of the
    damaged ones are added to `damaged`."""
    for path, manifest in runs:
        mine, version, config_php = looked_at(path, manifest, ids)
        if mine:
            backup = Backup(path, manifest, version, config_php)
            backup.dump = newer_dump(media, manifest['created_at'], version, ids, log)
            if backup.dump is None and backup.nightly:
                backup.dump = own_sql(backup)
            return backup
        if mine is None:
            damaged.append(shown(manifest['created_at']))
    return None


def could_be_newer(media, monthly, made):
    """Whether a monthly snapshot or a weekly database copy is newer than `made`, so the
    monthly snapshots are looked at too (reading one takes minutes)."""
    copies = dump_notes(media / DUMPS_DIR)[:1]
    return any(m['created_at'] > made for _, m in monthly) or any(n['created_at'] > made for _, n in copies)


def found_backup(config, ids, log):
    """(Backup, None) for this server's newest Nextcloud backup on the HDD: its newest
    nightly backup, or a monthly snapshot (with a newer weekly database copy) when that
    is newer; or (None, what was found instead)."""
    media = Path(config['media_mount'])
    nightly = monthly_snapshots(media / NIGHTLY_DIR, nightly=True)[:LOOKED_AT]
    monthly = monthly_snapshots(media / MONTHLY_DIR)[:LOOKED_AT]
    files = cloud_files(config)
    if not nightly and not monthly:
        return None, NONE_YET.format(files=files)
    log('Nextcloud: looking at its backup on the HDD (this can take a few minutes)')
    damaged = []
    newest = own_backup(media, nightly, ids, log, damaged)
    if newest is None or could_be_newer(media, monthly, newest.made):
        other = own_backup(media, monthly, ids, log, damaged)
        if other and (newest is None or other.made > newest.made):
            newest = other
    if newest:
        return newest, None
    if len(damaged) == len(nightly) + len(monthly):
        return None, DAMAGED.format(dates=', '.join(damaged))
    latest = max(m['created_at'] for _, m in nightly + monthly)
    return None, FOREIGN.format(date=shown(latest), ids=' or appdata_'.join(sorted(ids)), files=files)


def question(files, backup):
    lines = [
        f'Nextcloud is not set up on this SSD yet, but {files} holds this server\'s Nextcloud files, and the HDD '
        'holds this server\'s own Nextcloud backup:',
        f'  settings and program: the {"nightly backup" if backup.nightly else "monthly snapshot"} of '
        f'{shown(backup.manifest["created_at"])}',
    ]
    if backup.dump and backup.made != backup.manifest['created_at']:
        lines.append(f'  database (accounts, shares, calendars, contacts): the newer copy of {backup.date}')
    else:
        lines.append('  database (accounts, shares, calendars, contacts): from the same backup')
    lines.append(
        'Setup can bring Nextcloud back from it, then let it find the files again. The files folder itself is not '
        'changed. Answer no to keep it as it is and leave Nextcloud not set up.'
    )
    if backup.sealed:
        lines.append(
            'It asks for the backup password of the server that made this backup (on a rebuilt server, the old '
            'password, not a new one chosen since).'
        )
    return lines, f'Bring Nextcloud back from the backup of {backup.date}?'


def offered(ctx, app, ask=None, interactive=None, run=None):
    """Setup's offer for the state above: (True, None) when Nextcloud was brought back, or
    (False, the line the refusal adds: what was found), (False, None) when that state is
    not this one. `app`: the Nextcloud step's own pieces (compose_base, status,
    settings_problem)."""
    config = ctx.config
    data = Path(config['data_mount'])
    files = cloud_files(config)
    if not files.is_dir() or not any(files.iterdir()) or not untouched(data):
        return False, None
    ids = own_ids(files)
    if not ids:
        return False, None
    backup, said = found_backup(config, ids, ctx.log)
    if backup is None:
        return False, said
    if not (guided_setup.interactive(ctx) if interactive is None else interactive):
        return False, (
            f'This server\'s Nextcloud backup of {backup.date} is on the HDD; setup brings it back only when you '
            'run it yourself (sudo bash install.sh), never unattended'
        )
    lines, asked = question(files, backup)
    for line in lines:
        ctx.log(line)
    if not (ask or guided_setup.yes_no)(asked):
        return False, f'This server\'s Nextcloud backup of {backup.date} is on the HDD; you chose not to bring it back'
    try:
        restore(ctx, backup, ids, app, run or subprocess.run)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        return False, (
            f'Bringing Nextcloud back from the backup of {backup.date} did not work ({exc}); the SSD was put back '
            'as it was'
        )
    return True, None


def untouched(data):
    """Nothing of Nextcloud on the SSD yet but empty folders (and no settings: a fresh step)."""
    for part in PARTS:
        live = data / part
        for _, _, names in os.walk(live) if live.is_dir() else []:
            if names:
                return False
    return True


def ours(member):
    """Whether a member of data.tar is Nextcloud's (the snapshot keeps the other apps too)."""
    name = member.name.removeprefix('./').rstrip('/')
    return any(name == part or name.startswith(part + '/') for part in PARTS)


def within(root, path):
    """Whether `path`, every link on its way followed as it is on the disk now, is in `root`."""
    real = os.path.realpath(path)
    return real == root or real.startswith(root + os.sep)


def by_hand(member, root):
    """What tarfile's 'tar' filter does, and more, on every Python (3.11.0 to 3.11.3 have no
    filter at all). Checked as extractall takes each member, against the links already
    unpacked (a chain of links each "inside" as text climbs out): where the member goes, and
    where a link leads, must be in `root` (the real path of the folder it is unpacked
    into). A device or the like is refused; set-user-ID, set-group-ID, sticky and
    group/other write bits are cleared. Owners are kept."""
    name = member.name
    places = [os.path.join(root, name)]
    if member.issym():
        places.append(os.path.join(root, os.path.dirname(name), member.linkname))
    elif member.islnk():
        places.append(os.path.join(root, member.linkname))
    elif not (member.isfile() or member.isdir()):
        raise tarfile.TarError(f'{name} is not a file, folder or link')
    if os.path.isabs(name) or not all(within(root, place) for place in places):
        raise tarfile.TarError(f'{name} leads out of its folder')
    member.mode &= ~(stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX | stat.S_IWGRP | stat.S_IWOTH)
    return member


def made_empty(hidden):
    """The two folders a nightly backup leaves out, made empty as setup makes them: the
    database's (its database comes back from nextcloud.sql into a new one) and data,
    where the files folder on the HDD is mounted."""
    (hidden / POSTGRES).mkdir(mode=0o700)
    (hidden / HTML / 'data').mkdir(mode=0o750, exist_ok=True)
    os.chown(hidden / HTML / 'data', WWW_DATA, WWW_DATA, follow_symlinks=False)


def unpacked(backup, stage):
    """Nextcloud's folders out of the snapshot's data.tar into `stage`, from the same bytes
    that are checked against its checksum (checked first and unpacked afterwards, a file
    changed in between would come back unchecked): unpacked into a hidden folder, moved
    into place only when the whole archive matched. Owners and modes are kept (the
    database's folder is the database's own); nothing lands outside the folder."""
    hidden = stage / '.unpacking'
    hidden.mkdir(mode=0o700)
    # Every member checked by hand (by_hand): the 'tar' filter lets a link lead out of the
    # folder, and Python 3.11.0 to 3.11.3 have no filter at all (Debian 12 has 3.11.2;
    # upgrade.py unpack has the same). Where it exists, the filter is used as well.
    filtered = {'filter': 'tar'} if hasattr(tarfile, 'data_filter') else {}
    with open(backup.snapshot / 'data.tar', 'rb') as raw:
        counted = Hashing(raw)
        try:
            with tarfile.open(fileobj=counted, mode='r|') as tar:
                root = os.path.realpath(hidden)
                members = (by_hand(m, root) for m in tar if ours(m))
                tar.extractall(hidden, members=members, numeric_owner=True, **filtered)
        except tarfile.TarError as exc:
            raise RuntimeError(f'its data.tar holds something setup never unpacks ({exc})') from None
        read_rest(counted)
    if counted.digest.hexdigest() != backup.manifest['sha256']['data']:
        raise RuntimeError('the snapshot\'s data.tar changed since it was looked at (checksum mismatch)')
    for part in PARTS:
        if not (hidden / part).is_dir():
            raise RuntimeError(f'the backup holds no {part}')
    if not (hidden / VERSION_PHP).is_file():
        raise RuntimeError(f'the backup holds no {VERSION_PHP}')  # never a bare config.php
    if backup.nightly:
        made_empty(hidden)
    for top in {part.split('/')[0] for part in PARTS}:
        os.rename(hidden / top, stage / top)
    os.rmdir(hidden)


def take_out(backup, stage, ask_password):
    """The snapshot's Nextcloud folders, checked against its checksum, into `stage`, and
    its .env and config.php (from the sealed password files when it has a backup key)."""
    unpacked(backup, stage)
    if backup.sealed:
        opened = stage / 'password-files'
        ask_password(backup.snapshot, opened)
        for part in (ENV, CONFIG_PHP):
            if not (opened / part).is_file():
                raise RuntimeError(f'the backup\'s password files hold no {part}')
            (stage / part).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(opened / part, stage / part)
        shutil.rmtree(opened)
    for part in (ENV, CONFIG_PHP, MANAGED):
        if not (stage / part).is_file():
            raise RuntimeError(f'the backup holds no {part}')


def checked_copy(source, target, expected):
    """`source` copied to `target`, the copy's own bytes checked against `expected`."""
    digest = hashlib.sha256()
    with open(source, 'rb') as read, open(target, 'xb') as write:
        for chunk in iter(lambda: read.read(CHUNK), b''):
            digest.update(chunk)
            write.write(chunk)
    if digest.hexdigest() != expected:
        raise RuntimeError(f'the database copy {source.name} changed while it was read (checksum mismatch)')


def prepared(backup, ids, stage, app):
    """The staged restore checked and made ready: this server's (its config.php names the
    folder in the files folder), owned as Nextcloud needs, its settings those of
    my-config.json. Returns (database user, the staged database copy or None, admin)."""
    config_php = (stage / CONFIG_PHP).read_bytes()
    found = INSTANCE_ID.search(config_php)
    if not found or found[1].decode() not in ids:
        raise RuntimeError('its config.php is not this server\'s (another instance id)')
    user = DB_USER.search(config_php)
    if not user:
        raise RuntimeError('its config.php names no database user')
    owned = json.loads((stage / MANAGED).read_text())
    problem, admin = app.settings_problem(owned)
    if problem:
        raise RuntimeError(f'its settings differ from my-config.json ({problem})')
    # Nextcloud (www-data) writes its own config.php: root-owned, it said "Cannot write
    # into config directory". The .env holds passwords: root's alone.
    os.chown(stage / CONFIG_PHP, WWW_DATA, WWW_DATA, follow_symlinks=False)
    os.chmod(stage / CONFIG_PHP, 0o640)
    os.chown(stage / ENV, 0, 0, follow_symlinks=False)
    os.chmod(stage / ENV, 0o600)
    dump = None
    if backup.dump:
        dump = stage / backup.dump[0].name
        checked_copy(backup.dump[0], dump, backup.dump[1]['sha256'])
        role_sql(stage, user[1].decode(), config_php)
    return user[1].decode(), dump, admin


def role_sql(stage, user, config_php):
    """stage/role.sql: Nextcloud's own database user made with its password from
    config.php when the database has no such user (a nightly backup's database comes back
    into a new, empty one; a monthly snapshot's database has it already). The password
    goes to psql on its standard input, never on a command line."""
    found = DB_PASSWORD.search(config_php)
    if not found:
        raise RuntimeError('its config.php names no database password')
    password = re.sub(rb"\\([\\'])", rb'\1', found[1]).decode().replace("'", "''")
    if '$neko$' in password:
        raise RuntimeError('its database password cannot be given to psql as it is')
    text = (
        f"DO $neko$BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{user}') THEN "
        f"CREATE ROLE \"{user}\" LOGIN PASSWORD '{password}'; END IF; END$neko$;\n"
    )
    with open(stage / 'role.sql', 'x', encoding='utf-8', opener=lambda p, f: os.open(p, f, 0o600)) as out:
        out.write(text)


def switched(data, stage):
    """The staged folders put in place of the empty ones setup may have made; the empty
    ones kept in the staging folder, to be put back if a later step fails."""
    for part in PARTS:
        live, before = data / part, stage / 'before' / part
        before.parent.mkdir(parents=True, exist_ok=True)
        if live.exists():
            os.rename(safe_path(live), before)
        live.parent.mkdir(parents=True, exist_ok=True)
        os.rename(stage / part, live)


def put_back(data, stage, base, run, made):
    """A step after the switch failed: Nextcloud's containers removed and the SSD put back
    as it was (the restored copies go; the backup on the HDD stays). `made`: the folders
    the switch made (appdata, compose on an empty SSD), removed again."""
    if base:
        try:
            run(base + ['down'], capture_output=True, text=True, timeout=600, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    for part in PARTS:
        live, before = data / part, stage / 'before' / part
        if live.exists():
            failed = stage / 'failed' / part
            failed.parent.mkdir(parents=True, exist_ok=True)
            os.rename(live, failed)
        if before.exists():
            os.rename(before, live)
    for folder in made:
        with contextlib.suppress(OSError):
            os.rmdir(folder)


def pinned_images(pins):
    """The images maintenance-images.json pins, or None when it cannot be read."""
    try:
        services = json.loads(pins.read_text()).get('services')
        return [str((service or {}).get('image') or '') for service in services.values()]
    except (OSError, ValueError, AttributeError):
        return None


def pins_kept(data, base, run, log):
    """The monthly update's pins (maintenance-images.json) kept only when Docker on this PC
    has every image they name. Docker keeps its images on the SSD: on a new SSD they went
    with the old one, and Docker cannot download an image by its ID ("pull access denied
    for sha256"), so Nextcloud never started (T1-R9). The pins are taken out then: the
    versions compose.yaml names are downloaded, and the monthly update pins them again
    before its next download, as it pins every app that runs unpinned. Returns whether
    the pins are kept."""
    pins = data / PINS
    if not pins.is_file():
        return False
    docker = base[: base.index('compose')] if 'compose' in base else ['docker']
    images = pinned_images(pins)
    if images and all(
        run([*docker, 'image', 'inspect', image], capture_output=True, timeout=60, check=False).returncode == 0
        for image in images
    ):
        return True
    pins.unlink()
    log(
        'Nextcloud: the app versions its monthly update had pinned are not on this PC (Docker kept them on the old '
        'SSD); the versions its settings name are downloaded instead, and the monthly update pins them again'
    )
    return False


def database_loaded(base, user, dump, run):
    """The database copy loaded as Nextcloud's own database user (made first when the
    database has none: role_sql) into a new database, which takes the place of the one
    there only once it loaded without an error."""
    exec_db = base + ['exec', '-T', 'database']
    steps = [
        (
            exec_db + ['psql', '-q', '-U', 'nextcloud', '-d', 'postgres', '-v', STOP_ON_ERROR],
            dump.parent / 'role.sql',
        ),
        (
            exec_db
            + [
                'sh',
                '-c',
                'dropdb --if-exists -U nextcloud nextcloud_restoring && createdb -U nextcloud -O "$1" nextcloud_restoring',
                'sh',
                user,
            ],
            None,
        ),
        (
            exec_db
            + ['psql', '-q', '-U', user, '-d', 'nextcloud_restoring', '--single-transaction', '-v', STOP_ON_ERROR],
            dump,
        ),
        (
            exec_db
            + [
                'psql',
                '-q',
                '-U',
                'nextcloud',
                '-d',
                'postgres',
                '-v',
                STOP_ON_ERROR,
                '-c',
                'ALTER DATABASE nextcloud RENAME TO nextcloud_before',
                '-c',
                'ALTER DATABASE nextcloud_restoring RENAME TO nextcloud',
                '-c',
                'DROP DATABASE nextcloud_before',
            ],
            None,
        ),
    ]
    for argv, stdin in steps:
        with open(stdin, 'rb') if stdin else open(os.devnull, 'rb') as given:
            done = run(argv, stdin=given, capture_output=True, timeout=6 * 3600, check=False)
        if done.returncode:
            said = (done.stderr or b'').decode(errors='replace').strip().splitlines()[-1:] or ['no details']
            raise RuntimeError(f'its database copy did not load: {said[0]}')


def ready(base, status, wait=None):
    """Wait until Nextcloud answers as installed, up to date and out of maintenance."""
    wait = READY_WAIT if wait is None else wait
    deadline = time.monotonic() + wait
    sleep, now = time.sleep, time.monotonic
    while True:
        found = status(base)
        if found and found.get('installed') and not found.get('maintenance') and not found.get('needsDbUpgrade'):
            return
        if now() > deadline:
            raise RuntimeError(f'Nextcloud did not answer as ready within {wait // 60} minutes')
        sleep(10)


def scanned_all(base, run):
    done = run(
        base + ['exec', '-T', '-u', 'www-data', 'nextcloud', 'php', 'occ', 'files:scan', '--all'],
        capture_output=True,
        text=True,
        timeout=6 * 3600,
        check=False,
    )
    if done.returncode:
        raise RuntimeError(f'its file scan ended with error {done.returncode}')


def restore(ctx, backup, ids, app, run, ask_password=None):
    """Bring Nextcloud back from `backup`: staged and checked first, switched into place,
    then started (the newer database copy loaded before Nextcloud itself starts), and its
    file list refreshed. Any failure (Ctrl+C too) puts the SSD back as it was."""
    import backup_secrets

    data = Path(ctx.config['data_mount'])
    stage = safe_path(data / STAGING)
    if stage.exists():
        shutil.rmtree(stage)  # an earlier try cut off: only ever setup's own copy
    stage.mkdir(mode=0o700)
    base = None
    made = [folder for folder in (data / 'appdata', data / 'compose') if not folder.exists()]
    try:
        take_out(
            backup, stage, ask_password or (lambda run_, into: backup_secrets.asked_restore(run_, into, out=ctx.log))
        )
        user, dump, admin = prepared(backup, ids, stage, app)
        switched(data, stage)
        base = app.compose_base(data / 'compose/nextcloud/compose.yaml', data / ENV)
        if not pins_kept(data, base, run, ctx.log):
            base = app.compose_base(data / 'compose/nextcloud/compose.yaml', data / ENV)  # without the pins
        ctx.log('Nextcloud: its settings, program and database are back on the SSD; starting it')
        checked_run(base + ['up', '-d', '--wait', 'database'], 1800, 'Nextcloud\'s database (docker compose up)')
        if dump:
            database_loaded(base, user, dump, run)
        compose_up(base, 'Nextcloud', ctx.log, 1800)
        ready(base, app.status)
        ctx.log('Nextcloud: finding its files again (occ files:scan --all; this can take a while)')
        scanned_all(base, run)
    except BaseException:
        put_back(data, stage, base, run, made)
        shutil.rmtree(stage, ignore_errors=True)
        raise
    shutil.rmtree(stage, ignore_errors=True)
    app.adopt(admin)
    ctx.log(
        f'Nextcloud: brought back from this server\'s backup of {backup.date}. Sign in with the Nextcloud login '
        'you had before (the list of app passwords has it: sudo bash install.sh --recover, then "The list of app '
        'passwords"); setup\'s accounts step then asks you to choose c at Nextcloud to take it.'
    )
