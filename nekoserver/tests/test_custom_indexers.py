"""Torrentio as a download source NekoServer adds to Prowlarr's catalogue itself
(prowlarr-definitions/torrentio.yml, custom_indexers.py).

Prowlarr is not run here. The definition is read as YAML, and its paths and fields are
worked out with the few rules of Prowlarr's Cardigann engine it uses (flat "if ... else
... end", "or", plain variables; the "split" and "regexp" filters; a magnet link made
from the info hash of a public definition), so a mistake in the templates shows here."""

import hashlib
import importlib.util
import json
import re
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import custom_indexers  # noqa: E402

try:
    import yaml  # only these tests and test_review_fixes read YAML; CI installs it
except ImportError:  # pragma: no cover
    yaml = None

DEFINITION = ROOT / 'prowlarr-definitions' / 'torrentio.yml'
SITE = 'https://torrentio.strem.fun/'


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- the few rules of Prowlarr's templates the definition uses ----------------------------


def value(name, variables):
    """A variable's value; None and "" both count as not set (as in Prowlarr)."""
    return variables.get(name) or None


def condition(text, variables):
    """`.Name` or `or .A .B`: whether it is set."""
    parts = text.split()
    if parts[0] == 'or':
        return any(value(name, variables) for name in parts[1:])
    if parts[0] == 'and':
        return all(value(name, variables) for name in parts[1:])
    return value(parts[0], variables) is not None


IF_BLOCK = re.compile(r'{{\s*if\s+(.+?)\s*}}(.*?){{\s*else\s*}}(.*?){{\s*end\s*}}')
VARIABLE = re.compile(r'{{\s*(\.[A-Za-z.]+)\s*}}')


def render(template, variables):
    """A template as Prowlarr's engine makes it: each flat if/else/end block, then the
    plain variables. A nested "if" would be cut wrongly here, as it is in Prowlarr."""
    text = IF_BLOCK.sub(lambda m: m[2] if condition(m[1], variables) else m[3], str(template))
    if '{{' in VARIABLE.sub('', text):
        raise AssertionError('Something in the template Prowlarr would not understand: ' + text)
    return VARIABLE.sub(lambda m: value(m[1], variables) or '', text)


def query(imdb=None, season=None, ep=None, keywords='', categories=()):
    """The variables Prowlarr gives a search, and the categories it asks for."""
    variables = {
        '.Query.IMDBID': imdb,
        '.Query.Season': season,
        '.Query.Ep': ep,
        '.Query.Keywords': keywords,
        '.Keywords': keywords,
        '.Config.sitelink': SITE,
        '.True': 'True',
        '.False': None,
    }
    return variables, set(categories)


def requested(definition, variables, categories):
    """The addresses Prowlarr asks for one search: every path, unless the path names
    categories and the search asks for none of them."""
    names = {m['id']: m['cat'] for m in definition['caps']['categorymappings']}
    urls = []
    for path in definition['search']['paths']:
        wanted = {names[c] for c in path.get('categories', [])}
        if wanted and categories and not wanted & categories:
            continue
        urls.append(SITE + render(path['path'], variables))
    return urls


def run_filter(text, step):
    if step['name'] == 'split':
        separator, index = step['args']
        return text.split(separator[0])[int(index)]
    if step['name'] == 'regexp':
        found = re.search(step['args'], text)
        return found[1] if found else ''
    raise AssertionError('A filter these tests do not know: ' + step['name'])


def releases(definition, answer, variables):
    """The releases Prowlarr would make of one answer of Torrentio's."""
    found = []
    rows = answer[definition['search']['rows']['selector']]
    for row in rows:
        result = dict(variables)
        release = {}
        for name, field in definition['search']['fields'].items():
            text = row.get(field['selector'], '') if 'selector' in field else render(field['text'], result)
            for step in field.get('filters', []):
                text = run_filter(text, step)
            release[name] = text
            result['.Result.' + name] = text
        # Prowlarr: a public definition's release with an info hash and no link of its own
        # gets a magnet link made from the hash and the title.
        if definition['type'] == 'public' and release.get('infohash') and 'magnet' not in release:
            release['magnet'] = (
                'magnet:?xt=urn:btih:' + release['infohash'] + '&dn=' + urllib.parse.quote(release['title'])
            )
        found.append(release)
    return found


def stream(title, seeders, size, source='EZTV'):
    """A stream as Torrentio gives it; its info hash made up from its title."""
    return {
        'name': 'Torrentio\n1080p',
        'title': f'{title}\n\U0001f464 {seeders} \U0001f4be {size} ⚙️ {source}',
        'infoHash': hashlib.sha1(title.encode()).hexdigest(),
        'fileIdx': 0,
        'behaviorHints': {'bingeGroup': 'torrentio|1080p'},
        'sources': ['tracker:udp://tracker.example:1337/announce'],
    }


@unittest.skipUnless(yaml, 'PyYAML is not installed')
class DefinitionTests(unittest.TestCase):
    def setUp(self):
        self.definition = yaml.safe_load(DEFINITION.read_text(encoding='utf-8'))

    def test_it_is_a_public_definition_searched_by_imdb_number(self):
        d = self.definition
        self.assertEqual((d['id'], d['name'], d['type']), ('torrentio', 'Torrentio', 'public'))
        self.assertEqual(d['links'], [SITE])
        self.assertEqual(d['settings'], [], 'no account, no key')
        self.assertGreaterEqual(d['requestDelay'], 1)
        modes = d['caps']['modes']
        self.assertEqual(modes['search'], ['q'])
        # IMDb numbers only, so Sonarr asks with imdbid (it prefers tvdbid when offered).
        self.assertIn('imdbid', modes['tv-search'])
        self.assertNotIn('tvdbid', modes['tv-search'])
        self.assertIn('imdbid', modes['movie-search'])
        self.assertEqual({m['cat'] for m in d['caps']['categorymappings']}, {'Movies', 'TV'})
        # The note the catalogue shows is the one the picker gives.
        self.assertIn('never offers new releases on its own', d['description'])
        # Prowlarr makes the magnet link itself: an info hash, and no link of its own.
        fields = d['search']['fields']
        self.assertEqual(fields['infohash'], {'selector': 'infoHash'})
        self.assertNotIn('magnet', fields)
        self.assertNotIn('download', fields)

    def test_a_film_is_asked_for_by_its_imdb_number(self):
        variables, categories = query('tt0133093', categories=['Movies'])
        self.assertEqual(
            requested(self.definition, variables, categories),
            [SITE + 'stream/movie/tt0133093.json', SITE + 'stream/series/none.json'],
        )

    def test_an_episode_and_a_season_are_asked_for_as_torrentio_names_them(self):
        variables, categories = query('tt0903747', season='2', ep='5', categories=['TV'])
        self.assertEqual(
            requested(self.definition, variables, categories),
            [SITE + 'stream/series/tt0903747:2:5.json', SITE + 'stream/series/none.json'],
        )
        # A whole season: episode 1, whose answer also lists the season's packs.
        variables, categories = query('tt0903747', season='2', categories=['TV'])
        self.assertIn(SITE + 'stream/series/tt0903747:2:1.json', requested(self.definition, variables, categories))

    def test_with_no_categories_every_path_still_asks_only_for_what_applies(self):
        # Torrentio answers {"streams": []} for "none", "tt...-none" and "::1".
        variables, categories = query('tt0133093')
        self.assertEqual(
            requested(self.definition, variables, categories),
            [SITE + 'stream/movie/tt0133093.json', SITE + 'stream/series/::1.json', SITE + 'stream/series/none.json'],
        )
        variables, categories = query('tt0903747', season='1', ep='1')
        self.assertEqual(
            requested(self.definition, variables, categories)[:2],
            [SITE + 'stream/movie/tt0903747-none.json', SITE + 'stream/series/tt0903747:1:1.json'],
        )

    def test_a_search_by_name_alone_finds_nothing_and_never_guesses_an_id(self):
        # Torrentio answers ANY "tt" number with streams, so none may be made up.
        variables, categories = query(keywords='the matrix')
        urls = requested(self.definition, variables, categories)
        self.assertEqual(
            urls, [SITE + 'stream/movie/none.json', SITE + 'stream/series/::1.json', SITE + 'stream/series/none.json']
        )
        self.assertFalse(any('tt' in url.rsplit('/', 1)[1] for url in urls))

    def test_prowlarrs_empty_test_search_gets_one_known_episode(self):
        # Prowlarr refuses an indexer whose empty test search returns nothing.
        variables, categories = query()
        self.assertIn(SITE + 'stream/series/tt0903747:1:1.json', requested(self.definition, variables, categories))

    def test_magnet_links_are_made_from_the_streams(self):
        answer = {
            'streams': [
                stream('Some.Show.S02E05.1080p.WEB.h264', 352, '1.45 GB'),
                stream('Some Show Season 2 Complete 720p', 12, '9.8 GB', 'TorrentGalaxy'),
            ]
        }
        variables, _ = query('tt0903747', season='2', ep='5')
        first, second = releases(self.definition, answer, variables)
        self.assertEqual(first['title'], 'Some.Show.S02E05.1080p.WEB.h264')
        self.assertEqual((first['size'], first['seeders']), ('1.45 GB', '352'))
        self.assertEqual((second['size'], second['seeders']), ('9.8 GB', '12'))
        self.assertEqual(first['category'], 'series')
        self.assertEqual(first['imdbid'], 'tt0903747')
        for release, row in zip((first, second), answer['streams']):
            self.assertEqual(release['infohash'], row['infoHash'])
            self.assertTrue(release['magnet'].startswith('magnet:?xt=urn:btih:' + row['infoHash'] + '&dn='))
            # Each release is told apart by its hash (Prowlarr's guid is the details link).
            self.assertEqual(release['details'], SITE + '?btih=' + row['infoHash'])
        self.assertIn('dn=Some%20Show%20Season%202%20Complete%20720p', second['magnet'])
        variables, _ = query('tt0133093')
        (film,) = releases(self.definition, {'streams': [stream('A.Film.1999.2160p', 7, '18.45 GB')]}, variables)
        self.assertEqual(film['category'], 'movie')

    def test_the_file_reads_as_one_document_with_nekoservers_mark_first(self):
        text = custom_indexers.shipped_text('torrentio.yml')
        self.assertTrue(text.startswith(custom_indexers.MARK))
        self.assertEqual(yaml.safe_load(text), self.definition)


class InstallTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        (self.data / 'appdata' / 'prowlarr').mkdir(parents=True)
        self.custom = self.data / 'appdata' / 'prowlarr' / 'Definitions' / 'Custom'
        self.target = self.custom / 'torrentio.yml'
        self.owners = []
        noted = lambda path, uid, gid, follow_symlinks=True: self.owners.append((path, uid, gid, follow_symlinks))  # noqa: E731
        chown = mock.patch.object(custom_indexers.os, 'chown', noted)
        chown.start()
        self.addCleanup(chown.stop)
        self.said = []

    def install(self):
        return custom_indexers.install(self.data, 1000, 1001, self.said.append)

    def test_the_definition_goes_into_prowlarrs_custom_folder_owned_by_prowlarrs_account(self):
        self.assertEqual(self.install(), ['torrentio.yml'])
        self.assertEqual(self.target.read_text(encoding='utf-8'), custom_indexers.shipped_text('torrentio.yml'))
        self.assertIn((self.target, 1000, 1001, False), self.owners)  # never through a link
        self.assertIn((self.custom, 1000, 1001, False), self.owners)
        self.assertIn('Torrentio'.lower(), self.said[0])
        # Again: nothing written, so Prowlarr is not restarted for nothing.
        self.assertEqual(self.install(), [])
        # A newer NekoServer ships a changed definition: it replaces its own old one.
        self.target.write_text(custom_indexers.MARK + 'id: torrentio\n', encoding='utf-8')
        self.assertEqual(self.install(), ['torrentio.yml'])

    def test_a_definition_someone_put_there_is_never_replaced_or_removed(self):
        self.custom.mkdir(parents=True)
        self.target.write_text('id: torrentio\nname: My own Torrentio\n', encoding='utf-8')
        self.assertEqual(self.install(), [])
        self.assertIn('put there by someone else', self.said[-1])
        self.assertEqual(custom_indexers.remove(self.data, self.said.append), [])
        self.assertIn('My own Torrentio', self.target.read_text(encoding='utf-8'))

    def test_a_link_is_never_followed(self):
        self.custom.mkdir(parents=True)
        outside = self.data / 'elsewhere.yml'
        outside.write_text('x', encoding='utf-8')
        self.target.symlink_to(outside)
        with self.assertRaises(RuntimeError):
            self.install()
        self.assertEqual(outside.read_text(encoding='utf-8'), 'x')
        # Prowlarr's folder itself a link out of the app SSD: refused.
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        (self.data / 'appdata' / 'prowlarr').rename(self.data / 'appdata' / 'old')
        (self.data / 'appdata' / 'prowlarr').symlink_to(other.name)
        with self.assertRaises(RuntimeError):
            custom_indexers.custom_folder(self.data)

    def test_remove_takes_out_only_nekoservers_file(self):
        self.install()
        mine = self.custom / 'my-tracker.yml'
        mine.write_text('id: mine\n', encoding='utf-8')
        self.assertEqual(custom_indexers.remove(self.data, self.said.append), [self.target])
        self.assertFalse(self.target.exists())
        self.assertTrue(mine.exists())
        self.assertEqual(custom_indexers.remove(self.data, self.said.append), [])


class UninstallerTests(unittest.TestCase):
    def setUp(self):
        self.uninstaller = load('uninstaller_torrentio', 'uninstaller.py')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data = self.root / 'srv' / 'nekoserver'
        (self.data / 'appdata' / 'prowlarr').mkdir(parents=True)
        with mock.patch.object(custom_indexers.os, 'chown'):
            custom_indexers.install(self.data, 1000, 1000, lambda line: None)
        self.file = self.data / 'appdata/prowlarr/Definitions/Custom/torrentio.yml'
        self.plan = SimpleNamespace(
            data='/srv/nekoserver',
            identified={'data': True},
            at=lambda path: self.root / str(path).lstrip('/'),
        )

    def test_it_goes_with_the_apps_even_when_their_data_stays(self):
        said = []
        self.uninstaller._remove_definitions(self.plan, {'server', 'apps'}, said.append)
        self.assertFalse(self.file.exists())
        self.assertEqual(said, ['  Removed ' + str(self.file)])

    def test_with_the_apps_data_it_goes_with_that_folder_and_on_another_drive_nothing_is_touched(self):
        self.uninstaller._remove_definitions(self.plan, {'apps', 'data'}, print)
        self.assertTrue(self.file.exists(), 'removed with appdata by the data choice')
        self.plan.identified = {'data': False}
        self.uninstaller._remove_definitions(self.plan, {'apps'}, print)
        self.assertTrue(self.file.exists())

    def test_the_removal_is_part_of_taking_out_the_apps(self):
        calls = []
        with (
            mock.patch.object(self.uninstaller, '_remove_containers', lambda *a: calls.append('containers')),
            mock.patch.object(self.uninstaller, '_remove_definitions', lambda *a: calls.append('definitions')),
        ):
            self.plan.docker_root = lambda: None
            self.uninstaller._remove_docker_parts(self.plan, {'apps'}, '/', None, print)
            self.uninstaller._remove_docker_parts(self.plan, {'server'}, '/', None, print)
        self.assertEqual(calls, ['containers', 'definitions'])


class PickerTests(unittest.TestCase):
    def setUp(self):
        self.connect = load('media_connect_torrentio', 'media-connect.py')
        self.schemas = [
            {'name': 'The Pirate Bay', 'definitionName': 'thepiratebay', 'privacy': 'public', 'protocol': 'torrent'},
            {
                'name': 'Torrentio',
                'definitionName': 'torrentio',
                'privacy': 'public',
                'protocol': 'torrent',
                'language': 'en-US',
                'description': 'from the file',
            },
        ]

    def test_torrentio_is_offered_with_its_one_line_and_not_ticked(self):
        self.assertEqual(
            self.connect.indexer_note(self.schemas[1]),
            'Finds only the films and shows Sonarr and Radarr search for; it never offers new releases on its own',
        )
        self.assertEqual(self.connect.indexer_note(self.schemas[0]), 'public, torrent')
        # Not recommended: the recommended file does not name it, so it is never ticked.
        names = self.connect.recommended_names()
        self.assertNotIn('torrentio', names)
        self.assertEqual(self.connect.recommended_among(self.schemas, names), [1])

    def test_after_the_install_enter_takes_the_recommended_ones_without_torrentio(self):
        # 0.50.0's picker comes after the install with the recommended ones ticked: Enter
        # alone adds those, and Torrentio only when it is typed (or Prowlarr has it already).
        recommended = self.connect.recommended_among(self.schemas, {'thepiratebay'})
        ticked = self.connect.ticked_ones(self.schemas, recommended, existing=())
        self.assertEqual(ticked, [1])
        typed = iter(['', '1 2'])
        with mock.patch('builtins.print'):
            enter = self.connect.picked_indexers(self.schemas, recommended, ticked, lambda q: next(typed), 3)
            both = self.connect.picked_indexers(self.schemas, recommended, ticked, lambda q: next(typed), 3)
        self.assertEqual([schema['name'] for schema in enter], ['The Pirate Bay'])
        self.assertEqual([schema['name'] for schema in both], ['The Pirate Bay', 'Torrentio'])
        there = [{**schema, 'implementation': 'Cardigann'} for schema in self.schemas]
        added = [{'implementation': 'Cardigann', 'name': 'Torrentio'}]
        self.assertEqual(self.connect.ticked_ones(there, recommended, existing=added), [1, 2])

    def test_the_definition_is_shipped_with_every_release(self):
        recovery = load('recovery_torrentio', 'modules/recovery.py')
        self.assertIn(DEFINITION, recovery.bundle_files(ROOT))


class MediaStepTests(unittest.TestCase):
    def test_prowlarr_is_restarted_only_when_a_definition_was_written(self):
        text = (ROOT / 'modules' / 'media.py').read_text(encoding='utf-8')
        install = text.index("custom_indexers.install(data, uid, gid, context.log) if 'prowlarr' in selected")
        start = text.index('compose_up(base, name, context.log)')
        restart = text.index("if name == 'prowlarr' and new_definitions:")
        self.assertLess(install, start, 'the file is there before Prowlarr first starts')
        self.assertLess(start, restart)
        self.assertIn("run_cmd([*base, 'restart'], timeout=120)", text[restart : restart + 300])


if __name__ == '__main__':
    unittest.main()
