#!/usr/bin/env python3
"""Interactive, non-destructive source onboarding for NekoServer.

Collects media folder paths, remote share references, and indexer metadata.
Does not mount remote shares, add trackers, or handle secrets.
"""
import argparse
import json
import os
import urllib.parse
from pathlib import Path
from neko_common import safe_path, atomic_json

KINDS = ('movies', 'tv', 'music')

def valid_local_path(value):
    p = Path(value).expanduser()
    if not p.is_absolute() or '..' in p.parts or str(p) == '/':
        raise ValueError('Local folder must be an absolute path other than /, without ..')
    return str(p)

def validate_source(source):
    if source.get('category') not in KINDS:
        raise ValueError('Invalid media category')
    kind = source.get('type')
    if kind == 'local':
        source['path'] = valid_local_path(source['path'])
    elif kind == 'remote':
        # Remote shares are REFERENCES ONLY until separately mounted and tested.
        uri = source.get('uri', '')
        parsed=urllib.parse.urlsplit(uri)
        if not parsed.hostname or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError('Use a source URL without credentials, query parameters or fragments')
        if not (uri.startswith('smb://') or uri.startswith('nfs://')):
            raise ValueError('Remote reference must start smb:// or nfs://')
        if any(ch in uri for ch in '\r\n\x00') or '@' in uri.split('://', 1)[1].split('/')[0]:
            raise ValueError('Do not embed credentials in remote references')
        if source.get('mounted_path'):source['mounted_path']=valid_local_path(source['mounted_path'])
    elif kind == 'indexer':
        uri = source.get('uri', '')
        parsed=urllib.parse.urlsplit(uri)
        if not parsed.hostname or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError('Use a source URL without credentials, query parameters or fragments')
        if not uri.startswith('https://') or any(ch in uri for ch in '\r\n\x00@'):
            raise ValueError('Indexer must have an HTTPS URL without embedded credentials')
    else:
        raise ValueError('Unknown source type')
    return source

def ask(prompt, default=''):
    tail = f' [{default}]' if default else ''
    answer = input(f'{prompt}{tail}: ').strip()
    return answer or default

def collect(existing=None, input_fn=None):
    # Keep injected input local to this invocation; do not mutate global input.
    reader=input if input_fn is None else input_fn
    def ask(prompt,default=''):
        tail=f' [{default}]' if default else ''
        return reader(f'{prompt}{tail}: ').strip() or default
    sources = list((existing or {}).get('sources', []))
    print('Source wizard: source references only; nothing will be downloaded or mounted.')
    while ask('Add source? (y/n)', 'n').lower() == 'y':
        category = ask('Category (movies/tv/music)').lower()
        kind = ask('Type (local/remote/indexer)').lower()
        label = ask('Display name')
        if not label or any(ch in label for ch in '\r\n\x00'):
            print('Invalid name; try again')
            continue
        field = 'path' if kind == 'local' else 'uri'
        value = ask('Absolute folder path' if kind == 'local' else 'Reference URL (no passwords)')
        try:
            candidate={'category': category, 'type': kind, 'name': label, field: value}
            if kind=='remote':
                mounted=ask('Already mounted local folder (blank to defer)')
                if mounted:candidate['mounted_path']=mounted
            candidate = validate_source(candidate)
            if kind in ('local','remote'):
                candidate['copy_to_library']=ask('Copy this folder into its managed library (music on SSD, video on HDD) during onboarding? (y/n)','n').lower()=='y'
            sources.append(candidate)
            print('Added (configuration only).')
        except ValueError as exc:
            print('Not added:', exc)
    return {'schema_version': 1, 'sources': sources}

def main():
    parser = argparse.ArgumentParser(description='NekoServer media source wizard')
    parser.add_argument('--config',default=str(Path(__file__).with_name('config.json')))
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
