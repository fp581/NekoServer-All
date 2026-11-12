"""Setup's first page, "What would you like this server to do?", and the review after it
(the wizard of 0.50.0): every part chosen once, the downloads a switch of their own, one
approval. The same in the window (setup_gui) and in the terminal (wizard)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import explanations  # noqa: E402
import guided_setup  # noqa: E402
import neko_common  # noqa: E402
import wizard  # noqa: E402

SERVICES = ['jellyfin', 'navidrome', 'sonarr', 'radarr', 'lidarr', 'prowlarr', 'seerr', 'qbittorrent']


def settings(**more):
    return {'media': {'services': list(SERVICES)}, 'photos': {'deploy_immich': True}, **more}


def by_id(ident):
    return next(extra for extra in explanations.EXTRAS if extra['id'] == ident)


class Base(unittest.TestCase):
    exists = False

    def setUp(self):
        patcher = mock.patch.object(neko_common, 'server_exists', return_value=self.exists)
        patcher.start()
        self.addCleanup(patcher.stop)


class PageTests(Base):
    def test_the_rows_and_their_words(self):
        rows = [(extra['part'], extra['title']) for extra in explanations.shown()]
        self.assertEqual(
            rows,
            [
                ('apps', 'Photos and phone backup - Immich'),
                ('apps', 'Movies and shows - Jellyfin'),
                ('apps', 'Also find and download new ones'),
                ('apps', 'Music - Navidrome'),
                ('apps', 'Also find and download new ones'),
                ('apps', 'Personal files - Nextcloud'),
                ('extras', 'A TV connected to this PC'),
                ('extras', 'Send alerts to my phone'),
                ('extras', 'Warn me if the server stops checking in'),
                ('extras', 'Add a local AI assistant'),
                ('storage', 'My media and backup drives are in a network storage box (NAS)'),
            ],
        )
        self.assertEqual(explanations.PAGE_TITLE, 'What would you like this server to do?')
        # The cards say what the person gets; the app names are small print.
        movies = by_id('media_movies')
        self.assertEqual(
            movies['explanation'],
            'Watch your films and shows on any device, and ask for new ones to be found and added.',
        )
        self.assertEqual(
            movies['small'], 'Jellyfin. With finding and downloading: Seerr, Sonarr, Radarr, Prowlarr and qBittorrent.'
        )
        self.assertIn('Lidarr', by_id('media_music')['small'])
        # Statements the specification takes out, and a file name that says nothing to the person.
        words = ' '.join(extra['explanation'] for extra in explanations.EXTRAS)
        for gone in ('never slows', 'slower', 'nothing you ask leaves', 'asked later', 'ASSISTANT.md'):
            self.assertNotIn(gone, words)
        # The word TV is the TV connected to this PC, nothing else.
        self.assertEqual([e['id'] for e in explanations.EXTRAS if 'TV' in e['title']], ['tv'])

    def test_a_new_server_is_offered_the_apps_on_and_the_extras_off(self):
        self.assertEqual(
            explanations.states(settings()),
            {
                'media_photos': True,
                'media_movies': True,
                'media_music': True,
                'nextcloud': True,
                'movies_downloads': True,
                'music_downloads': True,
                'tv': False,
                'notifications': False,
                'heartbeat': False,
                'assistant': False,
                'network_storage': False,
            },
        )
        # A saved answer is what it says, on a new server too.
        chosen = settings(media_apps={'movies_downloads': False}, tv={'wanted': True})
        self.assertFalse(explanations.states(chosen)['movies_downloads'])
        self.assertTrue(explanations.states(chosen)['tv'])

    def test_finding_and_downloading_is_a_switch_inside_its_card(self):
        for group in ('movies', 'music'):
            switch = by_id(group + '_downloads')
            self.assertEqual((switch['under'], switch['needs']), ('media_' + group, ('media_' + group,)))
            self.assertEqual(switch['title'], 'Also find and download new ones')
        finding = by_id('movies_downloads')
        self.assertIn('Finds and downloads films and shows you are entitled to', finding['explanation'])
        # Offered while its card is on.
        no_films = settings(media_apps={'movies': False})
        self.assertFalse(explanations.offered(no_films, finding))
        self.assertTrue(explanations.offered(no_films, by_id('music_downloads')))
        # Off: Jellyfin alone; music keeps its own.
        playing = settings(media_apps={'movies_downloads': False})
        self.assertEqual(
            neko_common.media_services(playing), ['jellyfin', 'navidrome', 'lidarr', 'prowlarr', 'qbittorrent']
        )
        # A card switched off keeps its switch's answer for when it comes back.
        explanations.settle(no_films)
        self.assertEqual(
            no_films['media_apps'],
            {'movies': False, 'photos': True, 'music': True, 'movies_downloads': True, 'music_downloads': True},
        )

    def test_settling_gives_every_part_an_answer_so_no_step_asks(self):
        config = settings()
        explanations.settle(config)
        for extra in explanations.EXTRAS:
            self.assertIs(type(config[extra['section']][extra['key']]), bool, extra['id'])
        self.assertEqual(neko_common.media_services(config), SERVICES)
        ctx = mock.Mock(config=config)
        refuse = mock.Mock(side_effect=AssertionError('asked'))
        with mock.patch.object(guided_setup, 'interactive', return_value=True):
            for section in ('tv', 'notifications', 'heartbeat', 'assistant'):
                self.assertIs(guided_setup.wanted(ctx, section, 'x', prompt=refuse, output=refuse), False)
            guided_setup.asked_media_apps(ctx, prompt=refuse, output=refuse)

    def test_the_review_lists_what_will_be_installed(self):
        config = settings(media_apps={'photos': False}, assistant={'wanted': True})
        review = dict(explanations.review(config))
        self.assertEqual(
            review['Apps to install'],
            [
                'Jellyfin: movies & shows',
                'Navidrome: your music, everywhere',
                'Sonarr: manage your TV library',
                'Radarr: manage your movie library',
                'Lidarr: manage your music library',
                'Prowlarr: manage search providers',
                'Seerr: find something to watch',
                'qBittorrent: check your downloads',
                'Nextcloud: your files on your own devices',
            ],
        )
        self.assertEqual(review['Extras'], ['Add a local AI assistant'])
        self.assertEqual(
            review['Left out'], ['Photos and phone backup - Immich (stopped if this server has it; its data is kept)']
        )
        self.assertIn('It never formats a drive and never deletes your files.', review['What happens next'])
        # What it still stops for is said, and only for the parts chosen.
        asks = review['Setup stops to ask you']
        self.assertIn('Which AI model to use', asks)
        self.assertNotIn('The address of your alerts topic', asks)
        self.assertIn('Signing in to Tailscale, and setting up your phone', asks)
        # Nothing chosen is a server like any other.
        none = settings(media_apps={'photos': False, 'movies': False, 'music': False}, nextcloud={'enabled': False})
        self.assertEqual(dict(explanations.review(none))['Apps to install'], ['None: only what is always set up'])


class ExistingServerTests(Base):
    exists = True

    def test_a_server_set_up_before_keeps_what_it_has_where_nothing_was_saved(self):
        # No answers at all (every server before 0.50.0): the download apps, the TV, the
        # alerts and the outage warning are what it has; the assistant was always an opt-in.
        now = explanations.states(settings())
        self.assertEqual(
            {key: now[key] for key in ('movies_downloads', 'tv', 'notifications', 'heartbeat', 'assistant')},
            {'movies_downloads': True, 'tv': True, 'notifications': True, 'heartbeat': True, 'assistant': False},
        )
        config = settings()
        explanations.settle(config)
        self.assertEqual(neko_common.media_services(config), SERVICES)  # an upgrade stops nothing


class TerminalTests(Base):
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'my-config.json'
        self.path.write_text(json.dumps(settings()))

    def run_page(self, *typed):
        answers, asked, said = iter(typed), [], []

        def prompt(question):
            asked.append(question)
            return next(answers)

        started = wizard.wait_to_start(self.path, prompt, said.append, None)
        return started, asked, said, json.loads(self.path.read_text())

    def test_the_same_page_and_review_then_one_approval(self):
        started, asked, said, saved = self.run_page('', 'yes')
        self.assertTrue(started)
        self.assertEqual(len(asked), 2)  # Enter to go on from the first page, then the one approval
        self.assertIn('Nothing is installed yet.', asked[0])
        self.assertIn('Install selected apps? Type yes to go on, back to change your choices, or no to stop', asked[1])
        self.assertIn('=== What would you like this server to do? ===', said)
        self.assertIn(' [x]  2. Movies and shows - Jellyfin', said)
        self.assertIn('      [x]  3. Also find and download new ones', said)  # set in, under its card
        self.assertIn(' [ ]  7. A TV connected to this PC', said)
        self.assertIn('=== Ready to install NekoServer ===', said)
        self.assertIn('  - Jellyfin: movies & shows', said)
        # Yes saved every part with its answer.
        self.assertEqual(
            saved['media_apps'],
            {'photos': True, 'movies': True, 'music': True, 'movies_downloads': True, 'music_downloads': True},
        )
        self.assertEqual(saved['tv'], {'wanted': False})

    def test_a_number_switches_its_line_and_the_page_is_shown_again(self):
        started, asked, said, saved = self.run_page('3 8', '', 'yes')
        self.assertTrue(started)
        self.assertIn('      [ ]  3. Also find and download new ones', said)
        self.assertIn(' [x]  8. Send alerts to my phone', said)
        # Jellyfin alone now: the review, which comes after the page, has no Sonarr.
        self.assertIn('  - Jellyfin: movies & shows', said)
        self.assertNotIn('  - Sonarr: manage your TV library', said)
        self.assertIs(saved['media_apps']['movies_downloads'], False)
        self.assertEqual(saved['notifications'], {'wanted': True})

    def test_no_stops_and_a_wrong_line_is_said(self):
        started, asked, said, saved = self.run_page('12', 'no')
        self.assertFalse(started)
        self.assertIn('Type the number of a line (1 to 11), press Enter to go on, or type no to stop.', said)
        self.assertNotIn('=== Ready to install NekoServer ===', said)
        self.assertEqual(saved, settings())  # nothing was saved
        # On the review, Enter alone approves nothing, and no stops with nothing saved.
        started, asked, said, saved = self.run_page('', '', 'no')
        self.assertFalse(started)
        self.assertIn('Type yes, back or no. Enter alone does nothing here.', said)
        self.assertEqual(saved, settings())
        # A card's own switch with the card off: said, not switched.
        self.path.write_text(json.dumps(settings(media_apps={'movies': False})))
        started, asked, said, saved = self.run_page('3', 'no')
        self.assertIn(
            '"Also find and download new ones" needs one of these switched on first: Movies and shows - Jellyfin.',
            said,
        )
        self.assertNotIn('movies_downloads', saved['media_apps'])

    def test_yes_approves_only_what_was_shown(self):
        """A switch changed in another window while the review waits: yes typed there
        approves nothing yet; the pages are shown again as they stand."""
        answers, said = iter(['', 'yes', '', 'yes']), []

        def prompt(question):
            if 'Type yes to go on' in question and said.count('=== Ready to install NekoServer ===') == 1:
                # A switch is saved while the review's question waits.
                changed = json.loads(self.path.read_text())
                changed.setdefault('assistant', {})['wanted'] = True
                self.path.write_text(json.dumps(changed))
            return next(answers)

        self.assertTrue(wizard.wait_to_start(self.path, prompt, said.append, None))
        self.assertIn('A choice was changed in the setup window meanwhile. Here they are as they stand now.', said)
        self.assertIn(' [x] 10. Add a local AI assistant', said)
        self.assertEqual(said.count('=== Ready to install NekoServer ==='), 2)

    def test_back_on_the_review_shows_the_first_page_again(self):
        started, asked, said, saved = self.run_page('', 'back', '8', '', 'yes')
        self.assertTrue(started)
        self.assertEqual(said.count('=== What would you like this server to do? ==='), 3)
        self.assertEqual(said.count('=== Ready to install NekoServer ==='), 2)
        self.assertEqual(saved['notifications'], {'wanted': True})

    def test_the_drives_are_chosen_between_the_page_and_the_review(self):
        """The storage page comes after the first page and before the approval; what it
        chose is in the review, and is written only once the person approves."""
        chosen = {
            'settings': {'storage': ({'data_ssd': {'uuid': 'u1', 'fstype': 'ext4'}}, [])},
            'places': {},
            'lines': ['NekoServer and your apps: drive 2 (sdb1: 466 GB SSD)'],
        }
        order = []

        def storage(path, output):
            order.append('drives')
            return chosen

        def run(*typed):
            answers, said = iter(typed), []

            def prompt(question):
                order.append('approval' if 'Type yes to go on' in question else 'page')
                return next(answers)

            started = wizard.wait_to_start(self.path, prompt, said.append, None, storage=storage)
            return started, said, json.loads(self.path.read_text())

        started, said, saved = run('', 'no')
        self.assertEqual(order, ['page', 'drives', 'approval'])
        self.assertIn('Where your apps and files are kept:', said)
        self.assertIn('  - NekoServer and your apps: drive 2 (sdb1: 466 GB SSD)', said)
        self.assertFalse(started)
        self.assertEqual(saved, settings())  # no: nothing of it was saved
        started, said, saved = run('', 'yes')
        self.assertTrue(started)
        self.assertEqual(saved['storage']['data_ssd'], {'uuid': 'u1', 'fstype': 'ext4'})
        # A page that cannot go on (no drive that can be used) stops setup before the review.
        self.path.write_text(json.dumps(settings()))
        said = []
        self.assertFalse(
            wizard.wait_to_start(self.path, lambda question: '', said.append, None, storage=lambda path, output: None)
        )
        self.assertNotIn('=== Ready to install NekoServer ===', said)

    def test_an_existing_server_is_asked_to_apply_changes(self):
        with mock.patch.object(neko_common, 'server_exists', return_value=True):
            started, asked, said, saved = self.run_page('', 'no')
        self.assertIn('Apply these changes? Type yes to go on', asked[1])
        self.assertIn('=== Review your changes ===', said)


if __name__ == '__main__':
    unittest.main()
