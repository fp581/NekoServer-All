#!/usr/bin/env python3
"""The dashboard's Media folders page, done as root (DASHBOARD.md, "Media folders").

After setup, a person can connect more folders of films, shows or music, take one away,
and choose where new downloads go, on the dashboard. The page asks the control service
(control.rs), which checks the request, writes it to a file in its own folder and starts
nekoserver-media-folders.service; that runs this file:

  sudo python3 media_locations.py run --request /run/nekoserver-control/media-folders/request.json
  sudo python3 media_locations.py look      what the page shows, in a terminal

Every change is made by the code setup uses for the same choice (existing_media.py and
existing_page.py): the folder is checked as the apps' account, saved in the settings
(both copies: the one on the SSD and the one setup was run from), bound into the
library, and the apps that see it are told (restarted, given the library, the root
folder). So a change here and a setup run give the same server. The person's files are
never changed, moved or deleted; taking a folder away only disconnects it.

The answer goes to result.json beside the request, which the page shows: what was done,
what each app said, and the next step when something needs the person.
"""

import argparse
import contextlib
import copy
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

import account_store
import existing_media
import existing_page
from neko_common import (
    SETTINGS_HELP,
    SETUP_LOCK,
    OPERATION_LOCK,
    SHARED,
    answered_within,
    atomic_json,
    checked_settings,
    default_settings,
    files_root,
    held_lock_reason,
    media_group,
    media_services,
    music_library,
    on_a_share,
    read_settings,
    verify_storage,
)

FACTS = SHARED['media_folders']
FORMAT = FACTS['format']
FOLDER = Path(FACTS['folder'])
KINDS = tuple(FACTS['kinds'])
CHANGES, HOW, USES, TARGETS = (tuple(FACTS[key]) for key in ('changes', 'how', 'uses', 'targets'))
LIBRARY, IN_PLACE, NEW = 'library', existing_media.IN_PLACE, existing_media.NEW
LONGEST_REQUEST = 8192
WORDS = {kind: words for kind, _, words in existing_page.KINDS}  # films, shows, music
GROUP = {kind: group for kind, group, _ in existing_page.KINDS}  # the group whose apps play it


class Refusal(RuntimeError):
    """A change that is not made, with why in the person's words. Nothing was changed."""


# ---- what is said --------------------------------------------------------------------------

BUSY = FACTS['busy']  # the control service says the same (media_folders.rs)
AWAY = FACTS['away']  # ... and this, when a share does not answer the folder picker
SETUP_RUNNING = (
    'Setup is running on the server right now and may change the same folders. Try again once it has finished.'
)
OPERATION_RUNNING = 'NekoServer is being upgraded or a drive is being moved. Try again once that has finished.'
BACKUP_RUNNING = (
    'A NekoServer backup or monthly update is running right now (the Backups page shows which one). Your folders '
    'were not changed. Try again once it has finished.'
)
NOT_ON = 'This server does not play {words} (switched off in setup), so there is nothing to connect them to.'
NO_DOWNLOADS = (
    'This server does not find and download new {words} (switched off in setup), so there is no place for new '
    'downloads to choose. Connect the folder without it.'
)
ALREADY = '{path} is already connected for your {words}.'
ONE_EACH = (
    'Your {words} already use {other} where it is. NekoServer connects one folder of {words} where it is, and one '
    'more for new downloads only. Take {other} away first, or choose "Copy its files into the library" for this one.'
)
BOTH_TAKEN = (
    'Your {words} already use {other} where it is and {new} for new downloads. Take one of them away first, or '
    'choose "Copy its files into the library".'
)
NOT_CONNECTED = 'That folder is not connected for your {words} (any more). Nothing was changed.'
KEPT = 'The files in {path} were not touched: nothing was deleted, moved or changed there.'
NEW_ONLY_LEAVES = (
    '{path} took only the new {words}, so it is disconnected now. The files in it stay there, untouched; they are '
    'no longer in your library while it is not connected.'
)
NOT_SAVED = 'The settings could not be saved ({why}), so nothing was changed.'
ONLY_SSD = 'The settings copy on the SSD was saved; {path} was not there to save in, so setup there has the old choice.'


def settings_words(path):
    """A settings file as the person knows it."""
    return str(path) if path else 'the settings'


# ---- the request ---------------------------------------------------------------------------


def plain_path(value):
    """An absolute path as written: no '..', '.', empty parts, trailing slash, control
    characters or backslashes. The folder itself is checked by existing_media."""
    return (
        isinstance(value, str)
        and 1 < len(value) <= 4096
        and value.startswith('/')
        and os.path.normpath(value) == value
        and not value.startswith('//')
        and not any(ord(c) < 32 or c in '\\\x7f' for c in value)
    )


def checked_request(raw):
    """The request as the control service wrote it, every field checked again here (it is
    root's own file, but this is the side that acts on it). Refusal otherwise."""
    if not isinstance(raw, dict):
        raise Refusal('The request was not readable. Reload the page and try again.')
    change = raw.get('change')
    if change not in CHANGES:
        raise Refusal('That is not a change this page makes. Reload the page and try again.')
    asked = {'change': change, 'id': raw.get('id') if isinstance(raw.get('id'), str) else ''}
    if not re.fullmatch(r'[0-9a-f]{8,64}', asked['id']):
        raise Refusal('The request was not readable. Reload the page and try again.')
    if change == 'look':
        return asked
    if raw.get('kind') not in KINDS:
        raise Refusal('Choose films, shows or music.')
    asked['kind'] = raw['kind']
    if change == 'add':
        if not plain_path(raw.get('path')):
            raise Refusal('Type the folder as a full path, starting with / (for example /mnt/nas/films).')
        if raw.get('how') not in HOW or type(raw.get('downloads')) is not bool:
            raise Refusal('Choose whether to use the folder where it is or copy it, and where new downloads go.')
        asked.update(path=raw['path'], how=raw['how'], downloads=raw['downloads'])
        if asked['how'] == 'copy' and asked['downloads']:
            raise Refusal('A folder that is copied is not connected, so new downloads cannot go there.')
    elif change == 'remove':
        if raw.get('use') not in USES:
            raise Refusal('Which folder? Reload the page and try again.')
        asked['use'] = raw['use']
    elif change == 'target':
        if raw.get('target') not in TARGETS:
            raise Refusal('Where should new downloads go? Reload the page and try again.')
        asked['target'] = raw['target']
    return asked


def read_request(path):
    """The request file: root's own (written by the control service, closed to everyone
    else), not a link, small. Its fields are checked by checked_request."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        raise Refusal('The request was not there any more. Reload the page and try again.') from None
    with os.fdopen(fd, 'rb') as stream:
        seen = os.fstat(stream.fileno())
        # Written by the control service, which runs as root as this does.
        if not stat.S_ISREG(seen.st_mode) or seen.st_uid != os.geteuid() or seen.st_mode & 0o022:
            raise Refusal('The request was not written by the control service, so it was not used.')
        data = stream.read(LONGEST_REQUEST + 1)
    try:
        raw = json.loads(data) if len(data) <= LONGEST_REQUEST else None
    except ValueError:
        raw = None
    return checked_request(raw)


# ---- what the page shows -------------------------------------------------------------------


def library_folder(config, kind):
    """The folder NekoServer's own library of this kind is in on this PC."""
    if kind == 'music':
        return music_library(config)
    return files_root(config, 'movies') / 'library' / kind


def kind_state(config, kind, rows):
    """One kind's part of the page: NekoServer's library, the person's folders (with
    available()'s words for one that is not ready), where new downloads go, and the
    folders waiting to be copied."""
    mine = [row for row in rows if row['holds'] == kind]
    target = next((row for row in mine if row['new_files']), None)
    downloads = existing_page.downloads(config, kind)
    copies = [
        c['path']
        for c in config.get(existing_page.COPIES) or []
        if isinstance(c, dict) and c.get('holds') == kind and isinstance(c.get('path'), str)
    ]
    uses = {row['use'] for row in mine}
    return {
        'kind': kind,
        'words': WORDS[kind],
        'library': str(library_folder(config, kind)),
        'downloads': downloads,
        'target': (target['use'] if target else LIBRARY) if downloads else None,
        'folders': [
            {
                'use': row['use'],
                'path': row['path'],
                'new_files': row['new_files'],
                'ready': row['ready'],
                'reason': row['reason'],
            }
            for row in mine
        ],
        'copies': copies,
        # What another folder may still be added as (one used in place, one for new downloads).
        'room': {IN_PLACE: IN_PLACE not in uses, NEW: downloads and NEW not in uses},
    }


def page_state(config, available=None):
    """Every kind this server plays, as the page shows them."""
    rows = (available or existing_media.available)(config)
    return [kind_state(config, kind, rows) for kind, _ in existing_page.kinds(config)]


# ---- the changes ---------------------------------------------------------------------------


def entries_of(config, kind):
    """{use: the saved entry} of this kind's folders."""
    return {entry['use']: entry for entry in existing_media.extras(config) if entry['holds'] == kind}


def answered(config, kind):
    """The kind counts as answered on setup's page: setup does not ask about it again (a
    folder chosen here would otherwise be asked for once more)."""
    done, _ = existing_page.answered_kinds(config)
    done.add(kind)
    config[existing_page.ANSWERED] = [name for name, _, _ in existing_page.KINDS if name in done]


def kept_flags(entry, old):
    """A remade entry keeps what was chosen for it before (the music backup's answer)."""
    if old and 'backup' in old:
        entry['backup'] = old['backup']
    return entry


LOOK_SECONDS = 15


def answering(path):
    """Refusal when `path` is on a share that does not answer within LOOK_SECONDS. setup's
    checks look at the folder in this process, and on a share that stopped answering
    such a look waits for as long as it is away. This run has no time limit (a copy takes
    hours) and holds the lock the backups wait for: one look that never ends stopped the
    nightly backups and every later change until the PC was restarted (seen with a
    share that stopped answering: the page said "Working on it" for good)."""
    if on_a_share(path) and answered_within(['test', '-d', path + '/.'], LOOK_SECONDS) is None:
        raise Refusal(AWAY)


def add(config, asked, out):
    """Connect a folder (or have it copied). Returns the kinds whose apps are to be told."""
    kind, words = asked['kind'], WORDS[asked['kind']]
    if not media_group(config, GROUP[kind]):
        raise Refusal(NOT_ON.format(words=words))
    if asked['downloads'] and not existing_page.downloads(config, kind):
        raise Refusal(NO_DOWNLOADS.format(words=words))
    answering(asked['path'])
    if asked['how'] == 'copy':
        return copy_in(config, kind, asked['path'], out)
    entries = entries_of(config, kind)
    real = os.path.realpath(asked['path'])
    if any(entry['path'] in (asked['path'], real) for entry in entries.values()):
        raise Refusal(ALREADY.format(path=real, words=words))
    try:
        if IN_PLACE not in entries:
            found = existing_media.check(config, kind, asked['path'], asked['downloads'])
            answer = found['new_files'] or {}
            if asked['downloads'] and not answer.get('ok'):
                # As setup does: the folder is still used (read-only), and why new downloads
                # can't go there is said; they go where they went before.
                out['notes'].append(answer.get('reason') or 'New downloads cannot be saved there.')
            elif asked['downloads']:
                out['notes'].append(answer['note'])
            entry = existing_media.entry_for(kind, found, new_files=bool(answer.get('ok')))
        elif asked['downloads'] and NEW not in entries:
            found = existing_media.check(config, kind, asked['path'], True)
            if not found['new_files']['ok']:
                raise Refusal(found['new_files']['reason'])
            out['notes'].append(found['new_files']['note'])
            entry = existing_media.entry_for(kind, found, use=NEW)
        elif NEW in entries:
            raise Refusal(BOTH_TAKEN.format(words=words, other=entries[IN_PLACE]['path'], new=entries[NEW]['path']))
        else:
            raise Refusal(ONE_EACH.format(words=words, other=entries[IN_PLACE]['path']))
    except existing_media.Refused as refused:
        raise Refusal(str(refused)) from None
    config['placement'] = existing_media.with_choice(config, entry)['placement']
    answered(config, kind)
    if entry['new_files'] and NEW in entries and entry['use'] == IN_PLACE:
        out['notes'].append(NEW_ONLY_LEAVES.format(path=entries[NEW]['path'], words=words))
    out['message'] = f'Your {words} in {entry["path"]} are connected, used where they are.'
    if entry['new_files']:
        out['message'] += f' New {words} are saved there from now on.'
    return [kind]


def copy_in(config, kind, path, out):
    """ "Copy its files into the library": checked and sized first (copy_preview), then
    put on setup's list of folders to copy, which the copy step works through."""
    words = WORDS[kind]
    try:
        shown = existing_media.copy_preview(config, kind, path)
    except existing_media.Refused as refused:
        raise Refusal(str(refused)) from None
    except existing_media.source_import.IncompleteSource as exc:
        raise Refusal(f'Part of {path} cannot be read ({exc}), so nothing was copied.') from None
    if not shown['fits']:
        raise Refusal(
            existing_media.NO_ROOM.format(
                source=shown['source'],
                need=existing_media.size_words(shown['bytes']),
                destination=shown['destination'],
                free=existing_media.size_words(shown['free']),
            )
        )
    wanted = {'holds': kind, 'path': shown['source']}
    listed = [c for c in config.get(existing_page.COPIES) or [] if c != wanted]
    config[existing_page.COPIES] = [*listed, wanted]
    answered(config, kind)
    out['copy'] = existing_page.copy_said(shown)
    out['message'] = f'Your {words} in {shown["source"]} are copied into the library. Your folder is not changed.'
    return [kind]


def remove(config, asked, out):
    """Take a folder away: out of the settings, so setup's own code disconnects it. Its
    files are never touched."""
    kind, words = asked['kind'], WORDS[asked['kind']]
    entry = entries_of(config, kind).get(asked['use'])
    if not entry:
        raise Refusal(NOT_CONNECTED.format(words=words))
    config.update(existing_media.without_choice(config, kind, asked['use']))
    out['message'] = f'{entry["path"]} is taken away from your {words}. ' + KEPT.format(path=entry['path'])
    if entry['new_files']:
        out['notes'].append(f'New {words} are saved in the NekoServer library ({library_folder(config, kind)}) now.')
    return [kind]


def target(config, asked, out):
    """Where new downloads of a kind go: NekoServer's library, the folder used where it
    is (checked for writing as the apps' account first), or the folder for new ones."""
    kind, words = asked['kind'], WORDS[asked['kind']]
    if not existing_page.downloads(config, kind):
        raise Refusal(NO_DOWNLOADS.format(words=words))
    entries = entries_of(config, kind)
    now = next((use for use, entry in entries.items() if entry['new_files']), LIBRARY)
    wanted = asked['target']
    if wanted == now:
        out['message'] = f'New {words} already go there; nothing was changed.'
        return []
    if wanted != LIBRARY and wanted not in entries:
        raise Refusal(NOT_CONNECTED.format(words=words))
    if wanted == NEW:
        raise Refusal(NOT_CONNECTED.format(words=words))  # a folder for new ones always takes them
    leaving = entries.get(NEW)
    if wanted == LIBRARY:
        changed = existing_media.without_choice(config, kind, NEW)
        if IN_PLACE in entries:
            changed = existing_media.with_choice(changed, {**entries[IN_PLACE], 'new_files': False})
        config.update(changed)
        out['message'] = f'New {words} are saved in the NekoServer library ({library_folder(config, kind)}) now.'
    else:
        old = entries[IN_PLACE]
        answering(old['path'])
        try:
            found = existing_media.check(config, kind, old['path'], True)
        except existing_media.Refused as refused:
            raise Refusal(str(refused)) from None
        if not found['new_files']['ok']:
            raise Refusal(found['new_files']['reason'])
        out['notes'].append(found['new_files']['note'])
        entry = kept_flags(existing_media.entry_for(kind, found, new_files=True), old)
        config['placement'] = existing_media.with_choice(config, entry)['placement']
        out['message'] = f'New {words} are saved in {entry["path"]} now.'
    if leaving:
        out['notes'].append(NEW_ONLY_LEAVES.format(path=leaving['path'], words=words))
    return [kind]


DO = {'add': add, 'remove': remove, 'target': target}


# ---- telling the apps ----------------------------------------------------------------------
# The same steps setup takes after these choices: the folders bound (existing_page.mount),
# the apps that see them restarted (media.restart_for_own_folders), the copy made
# (existing_page.copy), Jellyfin's and Navidrome's libraries (existing_page), the root
# folders of Sonarr, Radarr and Lidarr (media-connect.py --root-folders) and Seerr's
# folder for requests (seerr_providers).

FILMS_AND_SHOWS = ('movies', 'tv')
ARR = {'movies': 'radarr', 'tv': 'sonarr', 'music': 'lidarr'}


class Context:
    """What setup's steps are given: the settings, a log, and (for the copy) a way to save."""

    def __init__(self, config, log, persist):
        self.config, self.log, self.apply = config, log, True
        self.persist_config = persist


def manifests(config):
    """{app: (its Compose file, the file's text)} of the media apps set up here, as
    modules/media.py hands them to restart_for_own_folders."""
    found = {}
    for name in media_services(config):
        dest = Path(config['data_mount']) / 'compose' / name / 'compose.yaml'
        with contextlib.suppress(OSError):
            if dest.is_file() and not dest.is_symlink():
                found[name] = (dest, dest.read_text(encoding='utf-8'))
    return found


def app_rows(rows, out, label):
    """Rows of setup's library and provider steps, said on the page: a step left for the
    person goes to the next steps, a failure is said as it is."""
    for row in rows:
        status, reason = row.get('status'), row.get('reason')
        if status == 'needs_setup' and reason:
            out['next'].append(f'{label}: {reason}')
        elif status == 'fail':
            out['notes'].append(f'{label}: {reason or "it did not answer"}. Try again later, or run setup again.')


def jellyfin(config, out):
    import media_libraries

    root = Path(config['data_mount'])
    if not account_store.get(config, 'jellyfin', verified=True):
        out['next'].append('Jellyfin: its administrator login is not saved yet; run setup again to finish Jellyfin.')
        return

    def no_question(_):
        raise EOFError  # the saved login is used; nothing is asked here

    rows = media_libraries.configure(
        root,
        files_root(config, 'movies'),
        True,
        ('jellyfin',),
        username=no_question,
        password=no_question,
        more=existing_page.libraries(config, True),
    )
    app_rows([r for r in rows if r['check'].startswith('jellyfin:existing') or r['status'] != 'pass'], out, 'Jellyfin')


def navidrome(config, out):
    app_rows(existing_page.music_libraries(config, True), out, 'Navidrome')


def root_folders(config, settings, out, run=subprocess.run):
    """Sonarr's, Radarr's and Lidarr's folders, as setup's connecting step makes them."""
    script = Path(__file__).with_name('media-connect.py')
    argv = [sys.executable, '-Es', str(script), '--config', str(settings), '--root-folders', '--apply']
    try:
        done = run(argv, capture_output=True, text=True, timeout=600, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        out['notes'].append(f'Sonarr, Radarr and Lidarr were not told ({exc}). Run setup again.')
        return
    if done.returncode:
        said = [line for line in (done.stdout or '').splitlines() if 'incomplete' in line or 'skipped' in line]
        out['notes'].append(
            'Sonarr, Radarr or Lidarr did not take the folder: '
            + ('; '.join(said[-3:]) or 'see the service log')
            + '. Try again once they run, or run setup again.'
        )


def seerr(config, out):
    import seerr_providers

    apps = media_services(config)
    rows = seerr_providers.configure(
        Path(config['data_mount']), True, apps, folders=seerr_providers.request_folders(config)
    )
    app_rows(rows, out, 'Seerr')


def tell_apps(ctx, kinds, settings, out, steps=None):
    """After the settings changed: the folders bound as they say, the apps restarted
    when a folder was connected, taken away or switched between read-only and not, the
    copy made, and each app given its library and folders. Each step's trouble is said,
    and never stops the next one."""
    config, apps = ctx.config, set(media_services(ctx.config))
    steps = steps or STEPS
    logged = len(out['log'])
    rows = steps['mount'](ctx)
    for row in rows:
        if not row.get('mounted') and row.get('reason'):
            out['notes'].append(str(row['reason']))
    # A folder an app holds open stays connected until a restart (existing_media says so
    # in its log): that is the person's next step, never only a line in the log.
    for line in out['log'][logged:]:
        if 'still connected' in line and line not in out['next']:
            out['next'].append(line)
    out['connected'] = [row['path'] for row in rows if row.get('mounted')]
    if getattr(ctx, 'existing_mounts_changed', False):
        out['restarted'] = steps['restart'](manifests(config), [], ctx.log)
    if config.get(existing_page.COPIES):
        left = steps['copy'](ctx)
        if left:
            out['next'].append('The copy is not finished: ' + ' '.join(out['log'][-2:]))
    wanted = [
        ('jellyfin', set(kinds) & set(FILMS_AND_SHOWS) and 'jellyfin' in apps, lambda: steps['jellyfin'](config, out)),
        ('navidrome', 'music' in kinds and 'navidrome' in apps, lambda: steps['navidrome'](config, out)),
        (
            'root folders',
            any(ARR[k] in apps for k in kinds),
            lambda: steps['root_folders'](config, settings, out),
        ),
        (
            'seerr',
            set(kinds) & set(FILMS_AND_SHOWS) and 'seerr' in apps and apps & {'sonarr', 'radarr'},
            lambda: steps['seerr'](config, out),
        ),
    ]
    for name, needed, step in wanted:
        if not needed:
            continue
        try:
            step()
        except Exception as exc:  # one app's trouble never hides what the others said
            out['notes'].append(f'{name}: not told ({exc}). Run setup again.')


def restart(found, failed, log):
    from modules import media

    return media.restart_for_own_folders(found, failed, log)


STEPS = {
    'mount': existing_page.mount,
    'restart': restart,
    'copy': existing_page.copy,
    'jellyfin': jellyfin,
    'navidrome': navidrome,
    'root_folders': root_folders,
    'seerr': seerr,
}


# ---- one run -------------------------------------------------------------------------------


def write_result(folder, answer):
    atomic_json(Path(folder) / FACTS['result'], {'format': FORMAT, 'at': time.time(), **answer})


@contextlib.contextmanager
def one_at_a_time(folder):
    """This page's own lock: one change at a time (the control service starts one too,
    but a run from a terminal could meet one from the page)."""
    Path(folder).mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(Path(folder) / FACTS['lock'], 'a') as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refusal(BUSY) from None
        yield


def not_now():
    """Why no change may be made now: setup, an upgrade or a drive move at work."""
    return held_lock_reason([(SETUP_LOCK, SETUP_RUNNING), (OPERATION_LOCK, OPERATION_RUNNING)])


def apps_lock(config):
    """The lock backups and monthly updates hold: the apps are not restarted under them."""
    from recovery_tools import lock

    return lock(config, patient=False)


class Settings:
    """The settings, read from the copy on the SSD and saved into both copies (the one
    setup was run from too, `also`), the way setup saves what a run changed."""

    def __init__(self, path, also=None):
        import installer

        self.path, self.also = Path(path), Path(also) if also else None
        config = read_settings(self.path)
        self.context = installer.Context(config, print, apply=True, config_path=self.path, saved=config)
        self.context.original_config_path = self.also if self.also and self.also.is_file() else None
        self.missed = self.also if self.also and not self.also.is_file() else None

    @property
    def config(self):
        return self.context.config

    def save(self):
        try:
            self.context.persist_config()
        except (OSError, RuntimeError) as exc:
            raise Refusal(NOT_SAVED.format(why=exc)) from None


def change(settings, asked, folder, out, steps=None, lock=apps_lock):
    """One change, made and told to the apps. `out`: the answer, filled in."""
    config = settings.config
    if asked['change'] == 'look':
        out['message'] = 'Your folders as they are now.'
        return
    verify_storage(config)
    before = copy.deepcopy(config)
    with lock(config):
        kinds = DO[asked['change']](config, asked, out)
        if config == before:
            return
        write_result(folder, {**out, 'state': 'working', 'message': out['message'] + ' Telling the apps…'})
        settings.save()
        if settings.missed:
            out['notes'].append(ONLY_SSD.format(path=settings.missed))
        ctx = Context(config, lambda line: out['log'].append(str(line)), settings.save)
        tell_apps(ctx, kinds, settings.path, out, steps)


def run(settings_path, request, also=None, steps=None, lock=apps_lock, available=None):
    """The unit's run: the request read, the change made, the answer written. Returns
    the exit code (0 also for a refusal: it is an answer, not a failure of the unit)."""
    # Every file this run touches is beside the request: its path (from the command
    # line) is taken only as the real path of a .json file, and the folder from it.
    real = os.path.realpath(request)
    if not (real.startswith(os.sep) and real.endswith('.json')):
        raise SystemExit(f'The request must be a .json file: {request}')
    request, folder = real, Path(os.path.dirname(real))
    out = {'id': '', 'state': 'done', 'message': '', 'notes': [], 'next': [], 'log': []}
    try:
        with one_at_a_time(folder):
            asked = read_request(request)
            out['id'], out['asked'] = asked['id'], asked
            write_result(folder, {**out, 'state': 'working', 'message': 'Working on it…'})
            with contextlib.suppress(FileNotFoundError):
                os.unlink(request)  # taken: the control service may accept the next one once this ends
            reason = not_now()
            if reason and asked['change'] != 'look':
                raise Refusal(reason)
            settings = Settings(settings_path, also)
            try:
                change(settings, asked, folder, out, steps, lock)
            except RuntimeError as exc:
                if isinstance(exc, Refusal):
                    raise
                raise Refusal(
                    BACKUP_RUNNING if 'backup or monthly update is running' in str(exc) else str(exc)
                ) from None
            out['kinds'] = page_state(settings.config, available)
    except Refusal as refused:
        out.update(state='refused', message=str(refused))
        if str(refused) == BUSY:
            return 0  # the run at work writes the answer; this one leaves it alone
        with contextlib.suppress(RuntimeError, OSError):
            out['kinds'] = page_state(read_settings(settings_path), available)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        out.update(state='failed', message=f'Something went wrong: {exc}. Nothing more was changed. Run setup again.')
    out['log'] = out['log'][-40:]
    write_result(folder, out)
    print(f'Media folders: {out["state"]}: {out["message"]}')
    return 0


def look(settings_path):
    """The page's view, in a terminal."""
    for kind in page_state(read_settings(settings_path)):
        print(f'{kind["words"].capitalize()}: the NekoServer library, {kind["library"]}')
        for folder in kind['folders']:
            role = 'used where it is' if folder['use'] == IN_PLACE else 'for new downloads only'
            print(f'  {folder["path"]} ({role}): ' + ('ready' if folder['ready'] else folder['reason']))
        if kind['downloads']:
            where = next((f['path'] for f in kind['folders'] if f['new_files']), kind['library'])
            print(f'  New downloads go to {where}')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('run', 'look'))
    parser.add_argument(
        '--config',
        type=lambda v: Path(checked_settings(v)),
        default=default_settings(Path(__file__).parent),
        help=SETTINGS_HELP,
    )
    parser.add_argument('--also', help='the settings file setup was run from, saved too')
    parser.add_argument('--request', default=str(FOLDER / FACTS['request']))
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error('Run it with sudo')
    if args.action == 'look':
        try:
            return look(args.config)
        except RuntimeError as exc:
            print(f'Media folders: {exc}', file=sys.stderr)
            return 1
    return run(args.config, args.request, args.also)


if __name__ == '__main__':
    sys.exit(main())
