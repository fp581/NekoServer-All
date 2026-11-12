"""Media you already have (0.50.0, the wizard: page P03).

The user's own case: "I gave it a remote ZFS folder and it could not access it once the
app was launched. it could only access it's own folders". A folder of films or shows on
this PC, on another drive or on a share that is mounted here, is used where it is: setup
checks it as the apps' account, mounts it into the apps at every start, and makes a
library for it in Jellyfin. Nothing is copied and nothing in the folder is changed.

This is the page's side: the question, the folders, and the two places setup acts on the
answer (before the apps start, and when Jellyfin's libraries are made). The checks, the
mounting and the library are existing_media.py's.

Not here yet: music (Navidrome needs a library of its own made through its API), saving
new films and shows into the same folder (the download apps' own folders must follow),
and copying instead (the sources question of the connecting step does that, as before).
"""

import existing_media
import placement
import questions
from neko_common import media_group

# The kinds asked about, each with the group whose apps play it and its words.
KINDS = (
    ('movies', 'movies', 'films'),
    ('tv', 'movies', 'shows'),
)
ASKED = 'existing_media_asked'  # true in the settings once the question was answered


def kinds(config):
    """The kinds this server plays: none with Movies and shows switched off."""
    return [(kind, words) for kind, group, words in KINDS if media_group(config, group)]


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


def folder_asked(config, kind, words, prompt, output, tries=3):
    """One kind's folder: the entry for the settings, or None when there is none (Enter
    alone, the window's Skip, or `tries` folders that cannot be used). A folder that
    cannot be used says why, in existing_media's words, and is asked for again."""
    for _ in range(tries):
        try:
            folder = questions.ask('existing_folder', prompt, what=words).strip()
        except EOFError:
            return None
        if not folder:
            return None
        try:
            found = existing_media.check(config, kind, folder)
        except existing_media.Refused as refused:
            output(str(refused))
            continue
        where = f" ({found['share']})" if found.get('share') and found['share'] != found['path'] else ''
        output(f"Your {words}: {found['path']}{where}. They are used where they are; nothing is copied or changed.")
        return existing_media.entry_for(kind, found)
    output(f'No folder of {words} is used for now. Run setup again to give one.')
    return None


def ask(ctx, prompt=None, output=print):
    """The page, asked once: whether there are films or shows already, then the folder
    of each. The answers are saved in the placement table (existing_media.with_choice).
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
            entry = folder_asked(config, kind, words, prompt, output)
            if entry:
                config['placement'] = existing_media.with_choice(config, entry)['placement']
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


def libraries(config, apply=False):
    """What media_libraries.configure calls with Jellyfin's signed-in client: a library
    for each folder of films and shows used in place. None with no such folder."""
    if not existing_media.extras(config):
        return None
    return lambda client: existing_media.jellyfin_libraries(client, config, apply)
