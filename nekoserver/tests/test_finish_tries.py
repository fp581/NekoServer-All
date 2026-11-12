"""Gate run B2 (VM2, photos only): the finishing screen's "You can try these" listed the
TV's picture, sound, controller and Steam, and "Music restore", and it said "Playback
verification pending", with Movies and Music off and no player installed. The steps that
are left out finish too, so a finished step was taken for a set-up part. Only the tries for
what this server has are listed now."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import finish_states  # noqa: E402
import wizard  # noqa: E402

TEMPLATE = json.loads((ROOT / 'config.json').read_text())
PLAN = TEMPLATE['profiles']['full']
TV_TITLES = [wizard.TEST_TITLES[test] for test in finish_states.TV_TESTS]
READ = {
    'containers': lambda config, apps: {a['id']: ('running', 'healthy') for a in apps},
    'logins': lambda config, apps: {a['id']: 'verified' for a in apps},
    'answers': lambda config, apps: {a['id']: True for a in apps},
}


class FinishTriesTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data = Path(folder.name) / 'ssd'
        self.data.mkdir()
        # Every step finished, the left-out ones too (each returns at once with nothing to do).
        steps = [*PLAN, *(step for step, _ in finish_states.BACKUPS)]
        self.write(('installer', 'state', 'state.json'), {step: {'status': 'success'} for step in steps})

    def write(self, parts, value):
        path = self.data.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def settings(self, **changes):
        config = {**json.loads(json.dumps(TEMPLATE)), 'data_mount': str(self.data)}
        config.update(changes)
        return config

    def photos_only(self):
        return self.settings(
            media_apps={'photos': True, 'movies': False, 'music': False},
            photos={**TEMPLATE.get('photos', {}), 'deploy_immich': True, 'external_backup_wanted': False},
            nextcloud={'enabled': False},
        )

    def tries(self, config):
        summary = finish_states.summary(finish_states.collect(config, PLAN, READ))
        lines = wizard.states_lines(summary)
        start = lines.index('You can try these whenever you like; nothing waits for them:')
        return summary, lines[start + 1 :]

    def test_photos_only_lists_only_the_photos_and_the_server_s_own_restores(self):
        summary, tries = self.tries(self.photos_only())
        self.assertEqual(
            [test['id'] for test in summary['tests']], ['phone_photo', 'host_restore', 'independent_restore']
        )
        said = '\n'.join(tries)
        for title in [*TV_TITLES, 'Music restore', 'App restore', 'Cloud restore', 'Play a video', 'Play a song']:
            self.assertNotIn(title, said)
        self.assertIn('A photo taken on the phone appears in Immich', said)
        # The backups that are switched off are not waited for either.
        self.assertEqual([b['id'] for b in summary['backups']], ['host', 'database'])

    def test_photos_only_says_no_playback_is_pending(self):
        config = self.photos_only()
        out = []
        wizard.finishing_screen(config, {'onboarding': {'status': 'success'}}, output=out.append, ran=['onboarding'])
        self.assertNotIn('Playback verification pending', '\n'.join(out))
        # With Jellyfin, still said until a playback is confirmed.
        config['media_apps'] = {'photos': True, 'movies': True, 'music': False}
        config['media'] = {**config.get('media', {}), 'services': ['jellyfin']}
        out = []
        wizard.finishing_screen(config, {'onboarding': {'status': 'success'}}, output=out.append, ran=['onboarding'])
        self.assertIn('Playback verification pending', '\n'.join(out))

    def test_the_tv_tries_come_only_with_a_tv_that_was_set_up(self):
        with_movies = self.settings(media_apps={'photos': True, 'movies': True, 'music': True})
        _, tries = self.tries(with_movies)
        self.assertFalse([line for line in tries if any(title in line for title in TV_TITLES)])  # no TV report
        self.write(('reports', 'tv-setup.json'), {'results': []})
        _, tries = self.tries(with_movies)
        self.assertEqual([t for t in TV_TITLES if any(t in line for line in tries)], TV_TITLES)
        # Turned down later, or Movies switched off: an old report says nothing.
        _, tries = self.tries({**with_movies, 'tv': {'wanted': False}})
        self.assertFalse([line for line in tries if any(title in line for title in TV_TITLES)])
        _, tries = self.tries({**with_movies, 'media_apps': {'photos': True, 'movies': False, 'music': True}})
        self.assertFalse([line for line in tries if any(title in line for title in TV_TITLES)])

    def test_music_restore_comes_with_music_and_its_backup(self):
        with_music = self.settings(
            media_apps={'photos': True, 'movies': False, 'music': True},
            music={**TEMPLATE.get('music', {}), 'backup_enabled': True},
        )
        summary, _ = self.tries(with_music)
        self.assertIn('music_restore', [test['id'] for test in summary['tests']])
        self.assertIn('app_restore', [test['id'] for test in summary['tests']])
        # The music backup turned off in the settings (the template's default): no restore to try.
        summary, _ = self.tries({**with_music, 'music': {**with_music['music'], 'backup_enabled': False}})
        self.assertNotIn('music_restore', [test['id'] for test in summary['tests']])


if __name__ == '__main__':
    unittest.main()
