"""Media you already have (0.50.0, the wizard: page P03).

The user's own case: "I gave it a remote ZFS folder and it could not access it once the
app was launched. it could only access it's own folders". A folder of films, shows or
music on this PC, on another drive or on a share that is mounted here, is used where it
is: setup checks it as the apps' account, mounts it into the apps at every start, and
makes a library for it in Jellyfin or Navidrome. Nothing in the folder is changed.

For each kind the person says, on the page:
  - the folder;
  - whether it is used where it is, or copied into the NekoServer library (the folder
    is kept as it is either way);
  - used where it is, on a server that finds and downloads: whether new ones are saved
    there too. A folder that cannot take them (read-only) says why, and another place is
    asked for: the NekoServer library, or another folder of the person's.

This is the page's side: the questions, and the places setup acts on the answers (before
the apps start, once the library folders are there, and when the libraries are made).
The checks, the mounting, the libraries and the copy are existing_media.py's.
"""

import existing_media
import placement
import questions
from neko_common import FINDING, media_group

# The kinds asked about, each with the group whose apps play it and its words.
KINDS = (
    ('movies', 'movies', 'films'),
    ('tv', 'movies', 'shows'),
    ('music', 'music', 'music'),
)
ASKED = 'existing_media_asked'  # true in the settings once the question was answered
# The folders to copy into the library, in the settings until they are copied:
# [{"holds": "movies", "path": "/mnt/tank/films"}].
COPIES = 'existing_media_copy'
IN_PLACE = 'They are used where they are; nothing is copied or changed.'
COPIED_LATER = 'They are copied into the NekoServer library once its drive is ready; your folder is kept as it is.'


def kinds(config):
    """The kinds this server plays: none with their group switched off."""
    return [(kind, words) for kind, group, words in KINDS if media_group(config, group)]


def downloads(config, kind):
    """This server finds and downloads new ones of that kind (the switch inside its card)."""
    group = next(group for name, group, _ in KINDS if name == kind)
    return media_group(config, group) and media_group(config, FINDING[group])


def keep_table(config):
    """The saved placement table brought up to date with the settings it is worked out
    from. A table is saved only because an existing folder is kept in it; where things
    are installed and kept still follows "storage" and "locations", so those rows are
    worked out again at every run and only the folders are carried over."""
    saved = config.get('placement')
    if not isinstance(saved, dict):
        return False
    table = placement.derived({key: value for key, value in config.items() if key != 'placement'})
    for row, entry in table.items():
        extra = saved.get(row, {}).get('extra') if isinstance(saved.get(row), dict) else None
        if extra:
            entry['extra'] = extra
    if table == saved:
        return False
    config['placement'] = table
    return True


def typed(key, prompt, **values):
    """One answer, as typed; '' when the question was left (Ctrl+D, the window's Skip)."""
    try:
        return questions.ask(key, prompt, **values).strip()
    except EOFError:
        return ''


def folder_found(config, kind, words, asking, key='existing_folder', new_files=False, tries=3):
    """A folder that passed the check for this kind (what existing_media.check returned),
    or None when there is none: Enter alone, the window's Skip, or `tries` folders that
    cannot be used. A folder that cannot be used says why, in existing_media's words, and
    is asked for again. `asking`: (prompt, output)."""
    prompt, output = asking
    for _ in range(tries):
        folder = typed(key, prompt, what=words)
        if not folder:
            return None
        try:
            return existing_media.check(config, kind, folder, new_files)
        except existing_media.Refused as refused:
            output(str(refused))
    output(f'No folder of {words} is used for now. Run setup again to give one.')
    return None


def another_place(config, kind, words, asking):
    """Where new ones go when the person's folder cannot take them: [] for the NekoServer
    library, or the entry of another folder of theirs, checked for writing."""
    _, output = asking
    place = None
    if typed('existing_new_place', asking[0], what=words) == '2':
        place = folder_found(config, kind, words, asking, key='existing_new_folder', new_files=True)
    if place and place['new_files']['ok']:
        output(place['new_files']['note'])
        return [existing_media.entry_for(kind, place, use=existing_media.NEW)]
    if place:
        output(place['new_files']['reason'])
    output(f'New {words} are saved in the NekoServer library.')
    return []


def with_new_files(config, kind, words, found, asking):
    """The entries for a folder used where it is when new ones are to be saved there
    too: the folder taking them, or the folder read-only with where they go instead."""
    _, output = asking
    checked = existing_media.check(config, kind, found['path'], new_files=True)
    answer = checked['new_files']
    if answer['ok']:
        output(answer['note'])
        return [existing_media.entry_for(kind, checked, new_files=True)]
    output(answer['reason'])
    return [existing_media.entry_for(kind, found), *another_place(config, kind, words, asking)]


def one_kind(config, kind, words, asking):
    """One kind's answers, put into the settings: its folder, how it is used, and where
    new ones are saved."""
    prompt, output = asking
    found = folder_found(config, kind, words, asking)
    if found is None:
        return
    where = f" ({found['share']})" if found.get('share') and found['share'] != found['path'] else ''
    if typed('existing_use', prompt, what=words) == '2':
        wanted = {'holds': kind, 'path': found['path']}
        config[COPIES] = [*[c for c in config.get(COPIES) or [] if c != wanted], wanted]
        output(f"Your {words}: {found['path']}{where}. {COPIED_LATER}")
        return
    output(f"Your {words}: {found['path']}{where}. {IN_PLACE}")
    entries = [existing_media.entry_for(kind, found)]
    if downloads(config, kind) and questions.yes_no('existing_new_files', prompt, output, what=words):
        entries = with_new_files(config, kind, words, found, asking)
    if kind == 'music' and found.get('share'):
        # On a network share (G7, the user's decision): asked once whether the music
        # backup copies it too; yes unless "No". Films and shows have no NekoServer backup
        # job, so they are not asked about and stay listed as not backed up.
        backed_up = typed('existing_backup', prompt, what=words) != '2'
        entries = [{**entry, 'backup': backed_up} for entry in entries]
        output(f'Your {words}: ' + ('copied each night with the music backup.' if backed_up else 'not backed up by NekoServer.'))
    for entry in entries:
        config['placement'] = existing_media.with_choice(config, entry)['placement']


def ask(ctx, prompt=None, output=print):
    """The page, asked once: whether there are films, shows or music already, then each
    kind's answers (one_kind). They are saved in the placement table
    (existing_media.with_choice); a folder to copy is kept until it is copied (COPIES).
    Returns True when the settings changed."""
    config = ctx.config
    changed = keep_table(config)
    asked = kinds(config)
    if not asked or config.get(ASKED) is True:
        return changed
    answer = questions.yes_no('existing_media', prompt, output)
    if answer is None:
        return changed  # left: asked again next time
    config[ASKED] = True
    if answer:
        for kind, words in asked:
            one_kind(config, kind, words, (prompt, output))
    return True


def mount(ctx):
    """Before the apps start: every folder used in place is mounted where the apps see
    it, now and at every start. A share that is not permanent yet gets its line (the one
    the check offers). A folder that is away is said and left for the next start; it
    never fails the step."""
    config = ctx.config
    wanted = existing_media.extras(config)
    if not wanted:
        return []
    permanent = []
    for entry in wanted:
        if not existing_media.folder_answers(entry['path']):
            continue  # away now (its share does not answer): use_in_place says so, never waiting for it
        try:
            line = existing_media.check(config, entry['holds'], entry['path']).get('add_to_fstab')
        except existing_media.Refused:
            continue  # away now: use_in_place says so
        if line:
            permanent.append(line)
    rows = existing_media.use_in_place(config, permanent, log=ctx.log)
    for row in rows:
        ctx.log(
            f"Your own folder ({row['holds']}): {row['path']} is "
            + ('used where it is' if row.get('mounted') else 'not there now: ' + str(row.get('reason') or 'it is away'))
        )
    return rows


def copy_said(shown):
    """What a copy will do, before it starts: the files, their size, the room, and the
    files the library already has differently (left as they are)."""
    said = (
        f"Copying {shown['source']} to {shown['destination']}: {shown['files']} files, "
        f"{existing_media.size_words(shown['bytes'])}; {existing_media.size_words(shown['free'])} free. "
        'Your folder is not changed.'
    )
    if shown['conflicts']:
        said += (
            f" {len(shown['conflicts'])} files are already in the library with other content and are left as "
            'they are: ' + ', '.join(str(name) for name in shown['conflicts'][:5])
        )
    return said


def copy(ctx):
    """Once the library folders are there: every folder the person chose to copy is
    copied into the library (existing_media.copy_now: add-only, their folder kept), after
    saying what the copy will do. One that cannot be copied now (it is away, it does not
    fit, part of it cannot be read) says why and stays on the list for the next run; it
    never fails the step. Returns the folders still to copy."""
    config = ctx.config
    wanted = config.get(COPIES)
    if not isinstance(wanted, list) or not wanted:
        return []
    left = []
    for folder in wanted:
        # The settings file is not trusted: copy_preview checks the kind and the folder again.
        kind, path = (folder.get('holds'), folder.get('path')) if isinstance(folder, dict) else (None, None)
        try:
            ctx.log(copy_said(existing_media.copy_preview(config, kind, path)))
            row = existing_media.copy_now(config, kind, path)
        except (RuntimeError, OSError, TypeError) as problem:
            ctx.log(f'Your own folder {path} was not copied: {problem}. Setup tries again at its next run.')
            left.append(folder)
            continue
        ctx.log(
            f"Copied into the library: {row.get('copied', 0)} files from {path}"
            + (f" ({row['reason']})" if row.get('reason') else '')
        )
    if left:
        config[COPIES] = left
    else:
        config.pop(COPIES, None)
    if hasattr(ctx, 'persist_config'):
        ctx.persist_config()
    return left


def libraries(config, apply=False):
    """What media_libraries.configure calls with Jellyfin's signed-in client: a library
    for each folder of films and shows used in place. None with no such folder."""
    if not existing_media.extras(config):
        return None
    return lambda client: existing_media.jellyfin_libraries(client, config, apply)


def music_libraries(config, apply=False):
    """Navidrome's library for the person's music folder, as rows for the connecting
    step's report; [] with no such folder."""
    return existing_media.music_libraries(config, apply)
