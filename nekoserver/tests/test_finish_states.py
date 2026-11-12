"""0.50.0, page P09 ("ready / remaining tasks"): finish_states keeps apart, for each
chosen app, installed / login verified / address answering / tried by a person, and for
the server backup made / restore tried, each as done, failed, not tested or waiting with
a reason and a named next action. Wizard checks T26 (an empty library is neither a failure
nor a passed playback test), T37 (no single "all passed" over what nobody tested) and T38
(a task put off leaves the server ready and stays in the list)."""

import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import finish_states  # noqa: E402
from finish_states import ATTENTION, DONE, FAILED, LATER, NOT_TESTED, NOT_WORKING, PENDING, READY, SEEN, WAITING  # noqa: E402

TEMPLATE = json.loads((ROOT / 'config.json').read_text())
PLAN = TEMPLATE['profiles']['full']
APPS = ('jellyfin', 'navidrome', 'immich', 'nextcloud', 'sonarr')
PLANTED = 'planted-' + 'x' * 20  # stands for a password; must never come back


def facts(**changes):
    """A server where every program's check passed and no person has tried anything."""
    found = {
        'apps': [
            {'id': a, 'name': a.title(), 'url': f'https://{a}.example.ts.net/', 'local_url': f'http://127.0.0.1/{a}'}
            for a in APPS
        ],
        'plan': list(PLAN),
        # Every step of the full setup, and the music backup the music step brings with it.
        'steps': {step: 'success' for step in [*PLAN, *(step for step, _ in finish_states.BACKUPS)]},
        'containers': {a: ('running', 'healthy') for a in APPS},
        'logins': {a: 'verified' for a in APPS},
        'answers': {a: True for a in APPS},
        'library': {},
        'jobs': {report: 'pass' for _, report in finish_states.BACKUPS},
        'witnessed': {},
        'person': {},
    }
    for key, value in changes.items():
        found[key] = {**found[key], **value} if isinstance(value, dict) and isinstance(found[key], dict) else value
    return found


def states_of(result, app):
    return {name: s['state'] for name, s in next(a for a in result['apps'] if a['id'] == app)['states'].items()}


class SummaryTests(unittest.TestCase):
    def test_what_programs_checked_and_what_people_tried_are_kept_apart(self):
        result = finish_states.summary(facts())
        self.assertEqual((result['outcome'], result['ready']), (READY, True))
        self.assertEqual(
            states_of(result, 'jellyfin'), {'installed': DONE, 'login': DONE, 'address': DONE, 'tried': NOT_TESTED}
        )
        # Every program's check passed, and still nothing says a person tried anything.
        self.assertEqual(result['checked'][DONE], 3 * len(APPS) + len(finish_states.BACKUPS))
        self.assertEqual(result['tried'][DONE], 0)
        self.assertEqual(result['tried'][NOT_TESTED], len(result['tests']))
        self.assertGreater(len(result['tests']), 4)
        self.assertEqual(result['server']['backup']['state'], DONE)
        self.assertEqual(result['server']['restore']['state'], NOT_TESTED)
        # Nothing in the answer sums the two kinds up as one "all passed".
        self.assertFalse([key for key in result if 'all' in key or 'pass' in key])

    def test_an_app_without_a_test_of_its_own_has_no_tried_state(self):
        result = finish_states.summary(facts())
        self.assertNotIn('tried', states_of(result, 'sonarr'))
        self.assertEqual(
            {t['app'] for t in result['tests'] if 'app' in t}, {'jellyfin', 'navidrome', 'immich', 'nextcloud'}
        )

    def test_an_empty_library_is_neither_a_failure_nor_a_passed_test(self):
        result = finish_states.summary(facts(library={'jellyfin': 0, 'navidrome': 0}))
        self.assertEqual((result['outcome'], result['ready']), (READY, True))
        for app in ('jellyfin', 'navidrome'):
            tried = next(a for a in result['apps'] if a['id'] == app)['states']['tried']
            self.assertEqual((tried['state'], tried['reason']), (NOT_TESTED, 'empty_library'))
            self.assertEqual(tried['action']['do'], 'add_media')
            self.assertIn('test_later', tried['action']['also'])
            self.assertEqual(states_of(result, app)['installed'], DONE)
        self.assertEqual(result['attention'], [])
        # A library with something in it is simply not tried yet.
        full = finish_states.summary(facts(library={'jellyfin': 12}))
        tried = next(a for a in full['apps'] if a['id'] == 'jellyfin')['states']['tried']
        self.assertEqual(
            (tried['state'], tried['reason'], tried['action']['do']), (NOT_TESTED, 'nobody_tried_yet', 'try')
        )

    def test_only_a_persons_word_makes_a_test_done_or_failed(self):
        result = finish_states.summary(
            facts(person={'play_video': SEEN, 'play_song': NOT_WORKING, 'phone_photo': LATER, 'tv_sound': SEEN})
        )
        self.assertEqual(states_of(result, 'jellyfin')['tried'], DONE)
        self.assertEqual(states_of(result, 'navidrome')['tried'], FAILED)
        self.assertEqual(states_of(result, 'immich')['tried'], WAITING)
        self.assertEqual(states_of(result, 'nextcloud')['tried'], NOT_TESTED)
        # The TV: sound was seen; picture, controller and Steam were not, each on its own.
        self.assertEqual({t['id']: t['state'] for t in result['tv']}['tv_sound'], DONE)
        self.assertEqual({t['id']: t['state'] for t in result['tv']}['tv_picture'], NOT_TESTED)
        self.assertEqual(result['tried'][DONE], 2)
        # A test a person failed, or put off, does not make the server unready.
        self.assertEqual((result['outcome'], result['ready']), (READY, True))

    def test_a_test_passed_in_the_acceptance_checklist_counts_as_tried(self):
        result = finish_states.summary(facts(witnessed={'media_workflow': 'pass', 'cloud_client': 'fail'}))
        self.assertEqual(states_of(result, 'jellyfin')['tried'], DONE)
        self.assertEqual(states_of(result, 'nextcloud')['tried'], FAILED)

    def test_a_backup_that_exists_is_not_a_restore_that_was_tried(self):
        result = finish_states.summary(facts())
        self.assertEqual(result['server']['backup']['state'], DONE)
        self.assertEqual(result['server']['restore']['state'], NOT_TESTED)
        self.assertEqual({r['state'] for r in result['restores']}, {NOT_TESTED})
        self.assertEqual({r['action']['do'] for r in result['restores']}, {'try_restore'})
        every = {test: 'pass' for test, _ in finish_states.RESTORES}
        self.assertEqual(finish_states.summary(facts(witnessed=every))['server']['restore']['state'], DONE)
        one_failed = {**every, 'app_restore': 'fail'}
        self.assertEqual(finish_states.summary(facts(witnessed=one_failed))['server']['restore']['state'], FAILED)
        some = {'app_restore': 'pass'}
        self.assertEqual(finish_states.summary(facts(witnessed=some))['server']['restore']['state'], NOT_TESTED)

    def test_each_backup_says_whether_it_ran(self):
        result = finish_states.summary(facts(jobs={'music': None, 'host': 'running'}))
        by_id = {b['id']: b for b in result['backups']}
        self.assertEqual((by_id['music']['state'], by_id['music']['action']['do']), (NOT_TESTED, 'run_backup'))
        self.assertEqual(by_id['host']['state'], WAITING)
        self.assertEqual(result['server']['backup']['state'], NOT_TESTED)
        self.assertEqual((result['outcome'], result['ready']), (PENDING, True))
        failed = finish_states.summary(facts(jobs={'applications': 'fail'}))
        self.assertEqual(failed['server']['backup']['state'], FAILED)
        self.assertEqual((failed['outcome'], failed['ready']), (ATTENTION, False))
        partial = finish_states.summary(facts(jobs={'external': 'partial'}))
        self.assertEqual({b['id']: b['reason'] for b in partial['backups']}['external'], 'left_something_out')
        self.assertEqual(partial['outcome'], ATTENTION)

    def test_a_task_put_off_leaves_the_server_ready_and_stays_in_the_list(self):
        result = finish_states.summary(
            facts(steps={'notifications': 'needs_setup', 'photo_backup': 'needs_setup', 'assistant': 'failed'})
        )
        self.assertEqual((result['outcome'], result['ready']), (PENDING, True))
        self.assertEqual(result['attention'], [])
        ids = {item['id']: item for item in result['pending']}
        for step in ('notifications', 'photo_backup', 'assistant'):
            self.assertIn('step:' + step, ids)
        self.assertEqual(ids['step:notifications']['action'], {'do': 'finish_step', 'step': 'notifications'})
        self.assertEqual(ids['step:assistant']['action'], {'do': 'retry_step', 'step': 'assistant'})
        # The offline backup that waits for a drive is listed as waiting, not as failed.
        self.assertEqual({b['id']: b['state'] for b in result['backups']}['external'], WAITING)
        self.assertEqual(result['server']['backup']['state'], DONE)

    def test_a_core_step_that_failed_needs_attention(self):
        result = finish_states.summary(facts(steps={'media': 'failed'}))
        self.assertEqual((result['outcome'], result['ready']), (ATTENTION, False))
        jellyfin = next(a for a in result['apps'] if a['id'] == 'jellyfin')['states']
        self.assertEqual(jellyfin['installed']['action'], {'do': 'retry_step', 'step': 'media'})
        # What depends on it waits; it is not counted as failed a second time.
        self.assertEqual(
            (jellyfin['login']['state'], jellyfin['address']['state'], jellyfin['tried']['state']), (WAITING,) * 3
        )
        self.assertEqual(states_of(result, 'immich')['installed'], DONE)
        self.assertIn('step:media', {item['id'] for item in result['attention']})
        missing = finish_states.summary(facts(steps={'docker': 'blocked'}))
        self.assertEqual(missing['outcome'], ATTENTION)
        never_ran = facts()
        del never_ran['steps']['nextcloud']
        self.assertEqual(finish_states.summary(never_ran)['outcome'], ATTENTION)

    def test_a_container_tells_installed_apart_from_running(self):
        cases = (
            (('exited', 'unknown'), FAILED, 'restart_app'),
            (('missing', 'unknown'), FAILED, 'retry_step'),
            (('running', 'unhealthy'), FAILED, 'restart_app'),
            (('running', 'starting'), WAITING, None),
            (('running', 'not_defined'), DONE, None),
        )
        for container, state, action in cases:
            result = finish_states.summary(facts(containers={'navidrome': container}))
            found = next(a for a in result['apps'] if a['id'] == 'navidrome')['states']['installed']
            self.assertEqual(found['state'], state, container)
            self.assertEqual((found['action'] or {}).get('do'), action, container)
            self.assertEqual(result['ready'], state != FAILED, container)
        unread = finish_states.summary(facts(containers=None))
        self.assertEqual(states_of(unread, 'jellyfin')['installed'], NOT_TESTED)
        self.assertEqual(unread['checked'][DONE], len(finish_states.BACKUPS))

    def test_login_and_address_are_states_of_their_own(self):
        result = finish_states.summary(
            facts(
                logins={'jellyfin': 'needs_setup', 'immich': 'started', 'sonarr': None},
                answers={'navidrome': False, 'nextcloud': None},
            )
        )
        self.assertEqual(
            states_of(result, 'jellyfin'), {'installed': DONE, 'login': FAILED, 'address': DONE, 'tried': NOT_TESTED}
        )
        self.assertEqual(states_of(result, 'immich')['login'], WAITING)
        self.assertEqual(states_of(result, 'sonarr')['login'], NOT_TESTED)
        self.assertEqual(states_of(result, 'navidrome')['address'], FAILED)
        self.assertEqual(states_of(result, 'nextcloud')['address'], NOT_TESTED)
        # An app that does not answer needs attention; a login to enter again is a task.
        self.assertEqual(result['outcome'], ATTENTION)
        self.assertEqual([item['id'] for item in result['attention']], ['address:navidrome'])
        self.assertIn('login:jellyfin', {item['id'] for item in result['pending']})
        login_only = finish_states.summary(facts(logins={'jellyfin': 'needs_setup'}))
        self.assertEqual((login_only['outcome'], login_only['ready']), (PENDING, True))

    def test_a_server_without_a_tv_or_a_backup_has_no_rows_for_them(self):
        result = finish_states.summary(facts(steps={'tv': 'unavailable', 'music_backup': 'unavailable'}))
        self.assertEqual(result['tv'], [])
        self.assertNotIn('music', {b['id'] for b in result['backups']})
        self.assertNotIn('music_restore', {r['id'] for r in result['restores']})
        none = finish_states.summary(facts(steps={step: 'unavailable' for step, _ in finish_states.BACKUPS}))
        self.assertEqual(
            (none['server']['backup']['state'], none['server']['backup']['reason']), (NOT_TESTED, 'no_backup_set_up')
        )


class Opener:
    """Answers for the apps' addresses: a number is an answer, an error is no connection."""

    def __init__(self, answers):
        self.answers = answers

    def open(self, url, timeout=None):
        answer = self.answers[url]
        if isinstance(answer, Exception):
            raise answer
        if answer >= 400:
            raise urllib.error.HTTPError(url, answer, 'no', {}, None)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class CollectTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data = Path(folder.name) / 'ssd'
        self.data.mkdir()
        self.config = {**json.loads(json.dumps(TEMPLATE)), 'data_mount': str(self.data)}
        self.config['nextcloud'] = {**self.config.get('nextcloud', {}), 'enabled': True}
        self.read = {
            'containers': lambda config, apps: {a['id']: ('running', 'healthy') for a in apps},
            'logins': lambda config, apps: {a['id']: 'verified' for a in apps},
            'answers': lambda config, apps: {a['id']: True for a in apps},
        }

    def write(self, parts, value):
        path = self.data.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def test_the_facts_are_read_from_the_servers_own_records(self):
        self.write(
            ('installer', 'state', 'state.json'), {'media': {'status': 'success', 'reason': PLANTED}, 'tv': 'odd'}
        )
        self.write(('reports', 'jobs', 'applications.json'), {'status': 'pass', 'reason': PLANTED})
        self.write(('reports', 'jobs', 'music.json'), {'status': 'something else'})
        self.write(
            ('reports', 'acceptance', 'latest.json'),
            {
                'manual': [
                    {'id': 'app_restore', 'status': 'pass', 'evidence': 'user_reported'},
                    {'id': 'host_restore', 'status': 'pass'},  # not a person's word
                    {'id': 'music_restore', 'status': 'needs_setup', 'evidence': 'user_reported'},
                ]
            },
        )
        self.write(('reports', 'gateway-tailnet.json'), {'links': {'jellyfin': 'https://jellyfin.example.ts.net/'}})
        found = finish_states.collect(self.config, PLAN, self.read)
        self.assertEqual(found['steps'], {'media': 'success'})
        self.assertEqual(
            (found['jobs']['applications'], found['jobs']['music'], found['jobs']['host']), ('pass', None, None)
        )
        self.assertEqual(found['witnessed'], {'app_restore': 'pass'})
        by_id = {a['id']: a for a in found['apps']}
        self.assertIn('nextcloud', by_id)
        self.assertEqual(by_id['jellyfin']['url'], 'https://jellyfin.example.ts.net/')
        self.assertTrue(by_id['nextcloud']['url'].startswith('http://127.0.0.1:'))
        self.assertNotIn(PLANTED, json.dumps(found))
        self.assertNotIn(PLANTED, json.dumps(finish_states.summary(found)))

    def test_what_a_person_said_is_kept_and_read_back(self):
        kept = finish_states.record_test(self.config, 'play_video', SEEN, now=123.0)
        self.assertEqual(kept, {'said': SEEN, 'at': 123.0})
        finish_states.record_test(self.config, 'phone_photo', LATER)
        finish_states.record_test(self.config, 'play_video', NOT_WORKING)  # they tried again
        found = finish_states.collect(self.config, PLAN, self.read)
        self.assertEqual(found['person'], {'play_video': NOT_WORKING, 'phone_photo': LATER})
        for test, word in (('not_a_test', SEEN), ('play_video', 'pass'), ('play_video', PLANTED)):
            with self.assertRaises(ValueError):
                finish_states.record_test(self.config, test, word)
        self.assertEqual(finish_states.collect(self.config, PLAN, self.read)['person']['play_video'], NOT_WORKING)

    def test_an_answer_is_not_kept_outside_the_app_drive(self):
        outside = self.data.parent / 'elsewhere'
        outside.mkdir()
        (self.data / 'reports').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            finish_states.record_test(self.config, 'play_video', SEEN)
        self.assertEqual(list(outside.iterdir()), [])
        gone = {**self.config, 'data_mount': str(self.data.parent / 'unplugged')}
        with self.assertRaises(RuntimeError):
            finish_states.record_test(gone, 'play_video', SEEN)

    def test_an_app_answers_when_anything_of_it_answers(self):
        apps = [{'id': a, 'local_url': 'http://127.0.0.1/' + a} for a in ('up', 'login_page', 'broken', 'down')]
        opener = Opener(
            {
                'http://127.0.0.1/up': 200,
                'http://127.0.0.1/login_page': 401,
                'http://127.0.0.1/broken': 502,
                'http://127.0.0.1/down': ConnectionRefusedError(),
            }
        )
        self.assertEqual(
            finish_states.answering(apps, opener), {'up': True, 'login_page': True, 'broken': False, 'down': False}
        )

    def test_the_saved_logins_come_back_as_states_only(self):
        import account_store

        stored = {'apps': {'jellyfin': {'username': 'me', 'password': PLANTED, 'state': 'verified'}}}
        real, account_store.load = account_store.load, lambda config: stored
        self.addCleanup(setattr, account_store, 'load', real)
        self.assertEqual(finish_states.login_states(self.config), {'jellyfin': 'verified'})

        def refused(config):
            raise RuntimeError('Invalid private account store; contents preserved')

        account_store.load = refused
        self.assertEqual(finish_states.login_states(self.config), {})


if __name__ == '__main__':
    unittest.main()
