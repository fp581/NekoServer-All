#!/usr/bin/env python3
"""Interactive, non-destructive source onboarding for NekoServer.

Collects media folder paths, remote share references, and indexer metadata.
Does not mount remote shares, add trackers, or handle secrets.
"""
import argparse
import json
import re
import urllib.parse
from pathlib import Path
from neko_common import safe_path, atomic_json, checked_settings, default_settings

KINDS = ('movies', 'tv', 'music')
# What the folder to give holds, per category: its folders are copied into the library as
# they are, each one becoming a film, a show or an artist there.
HOLDS = {'movies': 'your films (one folder per film)', 'tv': 'your shows (one folder per show)',
         'music': 'your artists (one folder per artist)'}
VIDEO = ('.mkv', '.mp4', '.avi', '.m4v', '.mov', '.wmv', '.ts', '.webm')
SEASON = re.compile(r'(season|series|staffel|saison|temporada)[ ._-]*\d+|s\d{1,2}', re.IGNORECASE)

def valid_local_path(value):
    p = Path(value).expanduser()
    if not p.is_absolute() or '..' in p.parts or str(p) == '/':
        raise ValueError('Local folder must be an absolute path other than /, without ..')
    return str(p)

def plain_url(uri):
    """A source address with a host and no credentials, query or fragment."""
    parsed = urllib.parse.urlsplit(uri)
    if not parsed.hostname or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError('Use a source URL without credentials, query parameters or fragments')
    return uri


def validate_source(source):
    if source.get('category') not in KINDS:
        raise ValueError('Invalid media category')
    kind = source.get('type')
    if kind == 'local':
        source['path'] = valid_local_path(source['path'])
    elif kind == 'remote':
        # Remote shares are REFERENCES ONLY until separately mounted and tested.
        uri = plain_url(source.get('uri', ''))
        if not (uri.startswith('smb://') or uri.startswith('nfs://')):
            raise ValueError('Remote reference must start smb:// or nfs://')
        if any(ch in uri for ch in '\r\n\x00') or '@' in uri.split('://', 1)[1].split('/')[0]:
            raise ValueError('Do not embed credentials in remote references')
        if source.get('mounted_path'):source['mounted_path']=valid_local_path(source['mounted_path'])
    elif kind == 'indexer':
        uri = plain_url(source.get('uri', ''))
        if not uri.startswith('https://') or any(ch in uri for ch in '\r\n\x00@'):
            raise ValueError('Indexer must have an HTTPS URL without embedded credentials')
    else:
        raise ValueError('Unknown source type')
    return source

def agreed(question, reader):
    """yes or y: True; no, n or Enter: False. Anything else is asked again (a typed
    "yes" must never count as no)."""
    while True:
        answer = reader(question + ' [yes/no, Enter = no]: ').strip().lower()
        if answer in ('yes', 'y'):
            return True
        if answer in ('', 'no', 'n'):
            return False
        print('Please type yes or no.')


def collect(existing=None, input_fn=None):
    """The source list with the ones added now; Ctrl+D stops adding and keeps them."""
    sources = list((existing or {}).get('sources', []))
    try:
        add_sources(sources, input if input_fn is None else input_fn)
    except EOFError:
        print()
    return {'schema_version': 1, 'sources': sources}


def add_sources(sources, reader):
    print('Source wizard: source references only; nothing will be downloaded or mounted.')
    while agreed('Add a media source?', reader):
        try:
            candidate = one_source(reader)
        except ValueError as exc:
            print('Not added:', exc)
            continue
        if candidate:
            sources.append(candidate)
            print('Added (configuration only).')


def level_warning(folder, category):
    """A warning when the folder looks like one level too deep: seasons at its top (a
    single show's folder), or video files lying loose in it. Its folders become the shows,
    films and artists of the library, so "Season 1" would become a show. None when it
    looks right, or can't be read now (a drive not plugged in yet)."""
    try:
        names = [entry.name for entry in Path(folder).iterdir()]
    except OSError:
        return None
    seasons = [n for n in names if SEASON.fullmatch(n.strip())]
    loose = [n for n in names if n.lower().endswith(VIDEO)] if category in ('tv', 'movies') else []
    if seasons:
        return ('This folder holds seasons (' + ', '.join(sorted(seasons)[:3]) + '): it looks like one show. '
                'Give the folder that contains your shows instead (the one above it), or each season would '
                'become a show of its own.')
    if loose:
        return ('This folder holds video files directly (' + ', '.join(sorted(loose)[:3]) + '). The library expects '
                + HOLDS.get(category, 'one folder per item') + '; loose files may not be recognised.')
    return None


def one_source(reader):
    """One source typed in, checked; None for an unusable name (asked again)."""
    def ask(prompt):
        return reader(prompt + ': ').strip()
    category = ask('Category (movies/tv/music)').lower()
    kind = ask('Type (local/remote/indexer)').lower()
    label = ask('Display name')
    if not label or any(ch in label for ch in '\r\n\x00'):
        print('Invalid name; try again')
        return None
    field = 'path' if kind == 'local' else 'uri'
    where = 'Folder that contains ' + HOLDS.get(category, 'your media') + ', as an absolute path'
    candidate = {'category': category, 'type': kind, 'name': label,
                 field: ask(where if kind == 'local' else 'Reference URL (no passwords)')}
    if kind == 'remote':
        mounted = ask('Already mounted local folder (blank to defer)')
        if mounted:
            candidate['mounted_path'] = mounted
    candidate = validate_source(candidate)
    folder = candidate.get('path') or candidate.get('mounted_path')
    warning = level_warning(folder, category) if folder else None
    if warning:
        print(warning)
        if not agreed('Use this folder anyway?', reader):
            return None
    if kind in ('local', 'remote'):
        candidate['copy_to_library'] = agreed('Copy this folder into its managed library (music on SSD, '
                                              'video on HDD) during onboarding?', reader)
    return candidate


def main():
    parser = argparse.ArgumentParser(description='NekoServer media source wizard')
    parser.add_argument('--config',type=checked_settings,default=default_settings(Path(__file__).parent))
    parser.add_argument('--output')
    parser.add_argument('--list', action='store_true', help='Show existing source references (no secrets)')
    args = parser.parse_args()
    settings=json.loads(safe_path(args.config).read_text())
    target = safe_path(args.output or str(Path(settings['data_mount'])/'config'/'media-sources.json'))
    existing = json.loads(target.read_text()) if target.exists() else {}
    if args.list:
        print(json.dumps(existing, indent=2))
        return
    config = collect(existing)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise SystemExit('Refusing symlink configuration')
    atomic_json(target,config)
    print('Saved references to', target)
    print('Local folders remain untouched. Mount remote shares separately and add indexers through Prowlarr.')
if __name__ == '__main__':
    main()
