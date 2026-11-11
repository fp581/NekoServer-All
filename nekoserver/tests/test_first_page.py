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
    def test_the_rows_and_their_words_are_the_specifications(self):
        rows = [(extra['part'], extra['title']) for extra in explanations.EXTRAS]
        self.assertEqual(
            rows,
            [
                ('apps', 'Photos and phone backup - Immich'),
                ('apps', 'Movies and TV shows - Jellyfin'),
                ('apps', 'Music - Navidrome'),
                ('apps', 'Personal files - Nextcloud'),
                ('downloads', 'Set up automatic media downloads'),
                ('extras', 'Use this PC with a TV'),
                ('extras', 'Send alerts to my phone'),
                ('extras', 'Warn me if the server stops checking in'),
                ('extras', 'Add a local AI assistant'),
                ('storage', 'My media and backup drives are in a network storage box (NAS)'),
            ],
        )
        self.assertEqual(explanations.PAGE_TITLE, 'What would you like this server to do?')
        # Statements the specification takes out.
        words = ' '.join(extra['explanation'] for extra in explanations.EXTRAS)
        for gone in ('never slows', 'slower', 'nothing you ask leaves', 'asked later'):
            self.assertNotIn(gone, words)

    def test_a_new_server_is_offered_the_apps_on_and_the_downloads_and_extras_off(self):
        self.assertEqual(
            explanations.states(settings()),
            {
                'media_photos': True,
                'media_movies': True,
                'media_music': True,
                'nextcloud': True,
                'media_downloads': False,
                'tv': False,
                'notifications': False,
                'heartbeat': False,
                'assistant': False,
                'network_storage': False,
            },
        )
        # A saved answer is what it says, on a new server too.
        chosen = settings(media_apps={'downloads': True}, tv={'wanted': True})
        self.assertTrue(explanations.states(chosen)['media_downloads'])
        self.assertTrue(explanations.states(chosen)['tv'])

    def test_the_downloads_need_films_or_music(self):
        downloads = by_id('media_downloads')
        self.assertEqual(downloads['needs'], ('media_movies', 'media_music'))
        photos_only = settings(media_apps={'movies': False, 'music': False, 'downloads': True})
        self.assertFalse(explanations.offered(photos_only, downloads))
        self.assertFalse(explanations.states(photos_only)['media_downloads'])
        self.assertTrue(explanations.offered(settings(media_apps={'movies': False}), downloads))  # music is on
        # Settled, it is saved off: there is nothing to download for.
        explanations.settle(photos_only)
        self.assertIs(photos_only['media_apps']['downloads'], False)

    def test_settling_gives_every_part_an_answer_so_no_step_asks(self):
        config = settings()
        explanations.settle(config)
        for extra in explanations.EXTRAS:
            self.assertIs(type(config[extra['section']][extra['key']]), bool, extra['id'])
        self.assertEqual(neko_common.media_services(config), ['jellyfin', 'navidrome'])  # playing, no downloading
        ctx = mock.Mock(config=config)
        refuse = mock.Mock(side_effect=AssertionError('asked'))
        with mock.patch.object(guided_setup, 'interactive', return_value=True):
            for section in ('tv', 'notifications', 'heartbeat', 'assistant'):
                self.assertIs(guided_setup.wanted(ctx, section, 'x', prompt=refuse, output=refuse), False)
            guided_setup.asked_media_apps(ctx, prompt=refuse, output=refuse)

    def test_the_review_lists_what_will_be_installed(self):
        config = settings(media_apps={'downloads': True, 'photos': False}, assistant={'wanted': True})
        review = dict(explanations.review(config))
        self.assertEqual(
            review['Apps to install'],
            [
                'Jellyfin: movies & TV shows',
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
            {key: now[key] for key in ('media_downloads', 'tv', 'notifications', 'heartbeat', 'assistant')},
            {'media_downloads': True, 'tv': True, 'notifications': True, 'heartbeat': True, 'assistant': False},
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
        started, asked, said, saved = self.run_page('yes')
        self.assertTrue(started)
        self.assertEqual(len(asked), 1)  # the one approval
        self.assertIn('=== What would you like this server to do? ===', said)
        self.assertIn(' [x]  2. Movies and TV shows - Jellyfin', said)
        self.assertIn(' [ ]  5. Set up automatic media downloads', said)
        self.assertIn('=== Ready to install NekoServer ===', said)
        self.assertIn('  - Jellyfin: movies & TV shows', said)
        # Yes saved every part with its answer.
        self.assertEqual(saved['media_apps'], {'photos': True, 'movies': True, 'music': True, 'downloads': False})
        self.assertEqual(saved['tv'], {'wanted': False})

    def test_a_number_switches_its_line_and_the_page_is_shown_again(self):
        started, asked, said, saved = self.run_page('5 7', 'yes')
        self.assertTrue(started)
        self.assertIn(' [x]  5. Set up automatic media downloads', said)
        self.assertIn(' [x]  7. Send alerts to my phone', said)
        self.assertIn('  - Sonarr: manage your TV library', said)
        self.assertIs(saved['media_apps']['downloads'], True)
        self.assertEqual(saved['notifications'], {'wanted': True})

    def test_no_stops_and_a_wrong_line_is_said(self):
        started, asked, said, saved = self.run_page('11', '', 'no')
        self.assertFalse(started)
        self.assertIn('Type yes, no, or the number of a line (1 to 10).', said)
        self.assertIn('Type yes, no, or the number of a line. Enter alone does nothing here.', said)
        self.assertEqual(saved, settings())  # nothing was saved
        # The downloads with neither films nor music: said, not switched.
        self.path.write_text(json.dumps(settings(media_apps={'movies': False, 'music': False})))
        started, asked, said, saved = self.run_page('5', 'no')
        self.assertIn(
            '"Set up automatic media downloads" needs one of these switched on first: '
            'Movies and TV shows - Jellyfin or Music - Navidrome.',
            said,
        )
        self.assertNotIn('downloads', saved['media_apps'])

    def test_yes_approves_only_what_was_shown(self):
        """A switch pressed in the setup window while the terminal waits: yes typed there
        approves nothing yet; the page is shown again as it stands."""
        answers, said = iter(['yes', 'yes']), []

        def prompt(question):
            if not said.count('=== Ready to install NekoServer ===') > 1:
                # The window saves a switch while the terminal's first question waits.
                changed = json.loads(self.path.read_text())
                changed.setdefault('assistant', {})['wanted'] = True
                self.path.write_text(json.dumps(changed))
            return next(answers)

        self.assertTrue(wizard.wait_to_start(self.path, prompt, said.append, None))
        self.assertIn('A choice was changed in the setup window meanwhile. Here they are as they stand now.', said)
        self.assertIn(' [x]  9. Add a local AI assistant', said)
        self.assertEqual(said.count('=== Ready to install NekoServer ==='), 2)

    def test_an_existing_server_is_asked_to_apply_changes(self):
        with mock.patch.object(neko_common, 'server_exists', return_value=True):
            started, asked, said, saved = self.run_page('no')
        self.assertIn('Apply these changes? Type yes to go on', asked[0])
        self.assertIn('=== Review your changes ===', said)


if __name__ == '__main__':
    unittest.main()
