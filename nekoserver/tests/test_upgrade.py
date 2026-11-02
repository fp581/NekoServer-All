"""upgrade.py: NekoServer upgraded from the dashboard. It downloads the release, checks it
is that release, runs its setup without questions and without a system update, checks
the result, and puts the previous release back when anything fails."""

import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import upgrade  # noqa: E402
from neko_common import VERSION  # noqa: E402

NEWER = 'v{}.{}.{}Beta'.format(*[int(n) + (1 if i == 1 else 0) for i, n in enumerate(VERSION.split('.'))])


def release(tag, version=None, extra=None):
    """A release archive as GitHub serves it: one top folder with nekoserver/ inside."""
    buffer = io.BytesIO()
    top = 'NekoServer-All-' + tag.lstrip('v')
    version = version or '.'.join(map(str, upgrade.numbers(tag)))
    files = {
        f'{top}/nekoserver/install.sh': b'#!/bin/bash\n',
        f'{top}/nekoserver/neko_common.py': f"VERSION = '{version}'\n".encode(),
        **(extra or {}),
    }
    with tarfile.open(fileobj=buffer, mode='w:gz') as bundle:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.data = self.dir / 'ssd'
        self.previous = self.data / 'installer/releases' / ('v' + VERSION)
        self.previous.mkdir(parents=True)
        (self.previous / 'install.sh').write_text('#!/bin/bash\n')
        (self.data / 'installer/state').mkdir(parents=True)
        self.state({'storage': 'complete', 'photos': 'complete', 'tailscale': 'needs_setup'})
        self.settings = self.dir / 'my-config.json'
        self.settings.write_text(json.dumps({'data_mount': str(self.data)}))
        self.journal = self.dir / 'etc/upgrade.json'
        self.said = []
        self.ran = []
        for target, value in (
            ('settings_path', lambda p: Path(p)),
            ('owner', lambda config: 'neko'),
            ('MAINTENANCE_JOURNAL', str(self.dir / 'none.json')),
        ):
            patcher = mock.patch.object(upgrade, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def state(self, steps):
        (self.data / 'installer/state/state.json').write_text(
            json.dumps({name: {'status': status} for name, status in steps.items()})
        )

    def setup(self, *codes):
        """Stand-in setup runs, answering with `codes` in turn (the new release, then the old)."""
        answers = iter(codes)

        def run(project, settings, account, log_file):
            self.ran.append((Path(project).name, account, self.journal_phase()))
            return next(answers)

        return run

    def journal_phase(self):
        return json.loads(self.journal.read_text())['phase'] if self.journal.exists() else None

    def upgrade(self, setup, check=lambda: None, payload=None, tag=NEWER):
        return upgrade.upgrade(
            tag,
            self.settings,
            out=self.said.append,
            get=lambda url, what, limit, timeout: payload or release(tag),
            setup=setup,
            check=check,
            journal=self.journal,
            lock=self.dir / 'setup.lock',
        )

    def test_a_good_release_is_set_up_and_checked(self):
        self.assertEqual(self.upgrade(self.setup(1)), 0)  # 1: a step still waits for you
        self.assertEqual(self.ran, [('nekoserver', 'neko', 'installing')])
        self.assertFalse(self.journal.exists())
        self.assertIn(f'NekoServer {NEWER} is installed and checked', self.said)

    def test_a_failed_setup_or_check_puts_the_release_you_had_back(self):
        self.assertEqual(self.upgrade(self.setup(2, 0)), 1)
        self.assertEqual([r[0] for r in self.ran], ['nekoserver', 'v' + VERSION])
        self.assertEqual(self.ran[1][2], 'going_back')
        self.assertFalse(self.journal.exists())
        self.ran.clear()
        self.assertEqual(self.upgrade(self.setup(0, 0), check=lambda: 'the dashboard does not answer'), 1)
        self.assertIn('the dashboard does not answer', ' '.join(self.said))
        # Putting it back failing too: it says what to run, and keeps the record.
        self.ran.clear()
        self.assertEqual(self.upgrade(self.setup(2, 2)), 2)
        self.assertTrue(self.journal.exists())
        self.assertIn('sudo bash install.sh  in ' + str(self.previous), self.said[-1])

    def test_a_download_that_is_not_that_release_changes_nothing(self):
        for payload in (release(NEWER, version='0.0.1'), b'not an archive', release('v0.0.1Beta')):
            with self.subTest(payload=payload[:20]):
                self.said.clear()
                self.assertEqual(self.upgrade(self.setup(), payload=payload), 1)
                self.assertFalse(self.journal.exists())
                self.assertIn('this server stays on ' + VERSION, self.said[-1])
        self.assertEqual(self.ran, [])
        # A link out of the folder is refused by the unpacking itself.
        bad = io.BytesIO()
        with tarfile.open(fileobj=bad, mode='w:gz') as bundle:
            info = tarfile.TarInfo('NekoServer-All-x/escape')
            info.type, info.linkname = tarfile.SYMTYPE, '/etc/passwd'
            bundle.addfile(info)
        self.assertEqual(self.upgrade(self.setup(), payload=bad.getvalue()), 1)

    def test_nothing_starts_while_something_else_is_at_work_or_for_an_older_release(self):
        self.journal.parent.mkdir(parents=True)
        self.journal.write_text('{}')
        self.assertEqual(self.upgrade(self.setup()), 2)
        self.assertIn('already at work', self.said[-1])
        self.journal.unlink()
        (self.dir / 'none.json').write_text('{}')  # an app update not finished
        self.assertEqual(self.upgrade(self.setup()), 2)
        (self.dir / 'none.json').unlink()
        self.assertEqual(self.upgrade(self.setup(), tag='v0.0.1Beta'), 2)
        self.assertEqual(self.upgrade(self.setup(), tag='v9.9.9Bata'), 2)
        self.assertEqual(self.ran, [])

    def test_the_check_finds_a_step_that_stopped_working(self):
        before = upgrade.steps_done({'data_mount': str(self.data)})
        self.assertEqual(before, {'storage', 'photos'})
        self.state({'storage': 'complete', 'photos': 'failed', 'tailscale': 'needs_setup'})
        wanted = '.'.join(map(str, upgrade.numbers(NEWER)))
        found = upgrade.works_on({'data_mount': str(self.data)}, NEWER, before, lambda: wanted, lambda v: True)
        self.assertIn('photos', found)
        self.state({'storage': 'complete', 'photos': 'complete'})
        config = {'data_mount': str(self.data)}
        self.assertIsNone(upgrade.works_on(config, NEWER, before, lambda: wanted, lambda v: True))
        self.assertIn('dashboard', upgrade.works_on(config, NEWER, before, lambda: VERSION, lambda v: True))
        self.assertIn('services', upgrade.works_on(config, NEWER, before, lambda: wanted, lambda v: False))

    def test_a_cut_off_upgrade_is_put_right_at_the_next_start(self):
        upgrade.record('downloading', self.journal, to=NEWER, previous=str(self.previous))
        self.assertEqual(upgrade.resume(self.settings, self.said.append, self.setup(), self.journal), 0)
        self.assertFalse(self.journal.exists())
        upgrade.record('installing', self.journal, to=NEWER, previous=str(self.previous))
        self.assertEqual(upgrade.resume(self.settings, self.said.append, self.setup(0), self.journal), 1)
        self.assertEqual(self.ran[-1][0], 'v' + VERSION)
        self.assertFalse(self.journal.exists())
        self.assertEqual(upgrade.resume(self.settings, self.said.append, self.setup(), self.journal), 0)

    def test_setup_runs_without_questions_and_without_a_system_update(self):
        project = self.dir / 'project'
        project.mkdir()
        seen = {}

        def run(argv, **kwargs):
            seen.update(argv=argv, **kwargs)
            return mock.Mock(returncode=0)

        upgrade.run_setup(project, self.settings, 'neko', self.dir / 'log', run=run)
        self.assertEqual(seen['argv'][2:], ['--profile', 'full', '--apply'])
        self.assertIs(seen['stdin'], upgrade.subprocess.DEVNULL)
        self.assertEqual(seen['env']['NEKOSERVER_KEEP_SYSTEM'], '1')
        self.assertEqual(seen['env']['SUDO_USER'], 'neko')
        self.assertEqual((project / 'my-config.json').read_text(), self.settings.read_text())
        # install.sh and host.py honour it: no "pacman -Syu", no new package on Arch.
        script = (ROOT / 'install.sh').read_text()
        self.assertIn('elif [[ -n "${NEKOSERVER_KEEP_SYSTEM:-}" ]]; then', script)
        self.assertLess(script.index('NEKOSERVER_KEEP_SYSTEM'), script.index('pacman -Syu --noconfirm'))
        import host

        arch = host.Arch()
        with mock.patch.dict(upgrade.os.environ, {'NEKOSERVER_KEEP_SYSTEM': '1'}):
            with self.assertRaisesRegex(RuntimeError, 'full system update'):
                arch._install(['samba'], 60)


if __name__ == '__main__':
    unittest.main()
