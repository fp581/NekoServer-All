"""T1-17 (1.0.1 drill 7): after a move from the Drives page, a lost SSD, and setup run as
RECOVERY.md says, setup stopped at Drives: the my-config.json it was started with (where the
person unpacked a release) still said every kind was on the media drive. The jobs wrote the
new places only into the settings files they knew. Now the places a Drives job set are
recorded on the OS disk at the commit point, and every tool that reads settings takes them
from there when its file differs, brings the file up to date and says so in one line."""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import neko_common  # noqa: E402

OLD_SSD = {'uuid': 'old-ssd-uuid', 'fstype': 'ext4'}
NEW_SSD = {'uuid': 'new-ssd-uuid', 'fstype': 'ext4'}
D3 = {'uuid': 'd3-uuid', 'fstype': 'ext4', 'added': True}


def moved():
    """The places the Drives page left: backups, photos, films and Nextcloud on d3."""
    return {'photos': 'd3', 'movies': 'd3', 'cloud': 'd3', 'backups': 'd3'}


def stale(ssd=NEW_SSD):
    """The person's own file: a new SSD chosen, d3 added, the places never moved."""
    return {
        'data_mount': '/srv/nekoserver',
        'media_mount': '/mnt/nekoserver-media',
        'storage': {'data_ssd': dict(ssd), 'media_hdd': {'uuid': 'hdd-uuid'}, 'd3': dict(D3)},
    }


def server():
    """The settings a job wrote on the server: d3 and the places moved to it."""
    config = stale(OLD_SSD)
    config['locations'] = moved()
    return config


def settings_file(root, name='my-config.json', **config):
    path = root / 'release' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config))
    return path


def places_in(path):
    return json.loads(path.read_text())['locations']


class Folder(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__('shutil').rmtree, self.root, True)
        self.record = self.root / 'os/places.json'
        patch = mock.patch.object(neko_common, 'PLACES_RECORD', self.record)
        patch.start()
        self.addCleanup(patch.stop)


class RecordTests(Folder):
    def test_a_job_commit_writes_the_record_with_the_settings(self):
        import storage_jobs

        config = stale(OLD_SSD)
        settings = settings_file(self.root, **config)
        request = {'id': '0123456789abcdef', 'job': 'move', 'kind': 'photos', 'to': 'd3'}
        job = storage_jobs.Job(request, config, [settings], self.root / 'state', self.root / 'run', out=lambda *a: None)
        job.commit('switch', [['locations', 'photos', 'd3']], here='media', to='d3')
        record = json.loads((self.root / 'state' / 'places.json').read_text())
        self.assertEqual(record, {'storage': {'d3': D3}, 'locations': {'photos': 'd3'}})
        self.assertEqual(json.loads(settings.read_text())['locations'], {'photos': 'd3'})

    def test_the_record_holds_no_ssd_or_media_drive(self):
        neko_common.write_places(server())
        record = json.loads(self.record.read_text())
        self.assertEqual(set(record['storage']), {'d3'})
        self.assertEqual(record['locations'], moved())

    def test_a_stale_file_takes_the_records_places_and_keeps_its_own_new_ssd(self):
        neko_common.write_places(server())
        config, line = neko_common.current_places(stale())
        self.assertEqual(config['locations'], moved())
        self.assertEqual(config['storage']['data_ssd'], NEW_SSD)
        self.assertEqual(config['storage']['d3'], D3)
        self.assertIn("This server's data was moved from the dashboard; using its current places", line)
        self.assertIn('photos on Extra drive d3', line)

    def test_a_released_drive_goes_and_places_back_on_the_media_drive_go(self):
        config = stale(OLD_SSD)
        del config['storage']['d3']
        neko_common.write_places(config)
        before = server()
        fixed, line = neko_common.current_places(before)
        self.assertNotIn('d3', fixed['storage'])
        self.assertNotIn('locations', fixed)
        self.assertIsNotNone(line)

    def test_nothing_changes_without_a_record_or_when_they_agree(self):
        before = stale()
        self.assertEqual(neko_common.current_places(before), (before, None))
        neko_common.write_places(before)
        self.assertEqual(neko_common.current_places(before), (before, None))
        self.assertFalse(neko_common.take_places(before, out=self.fail))

    def test_a_damaged_record_is_not_used(self):
        self.record.parent.mkdir(parents=True)
        self.record.write_text('{"storage": [')
        before = stale()
        self.assertEqual(neko_common.current_places(before), (before, None))

    def test_take_places_brings_the_file_up_to_date_and_says_so_once(self):
        neko_common.write_places(server())
        path = settings_file(self.root, **stale())
        config, said = json.loads(path.read_text()), []
        self.assertTrue(neko_common.take_places(config, [path], out=said.append))
        self.assertEqual(len(said), 1)
        self.assertTrue(said[0].endswith(f'(from {self.record}). {path} brought up to date.'), said[0])
        saved = json.loads(path.read_text())
        self.assertEqual(saved['locations'], moved())
        self.assertEqual(saved['storage']['data_ssd'], NEW_SSD)
        self.assertEqual(config, saved)


class ToolTests(Folder):
    """Each tool, started from a stale my-config.json beside a moved server."""

    def setUp(self):
        super().setUp()
        neko_common.write_places(server())
        self.path = settings_file(self.root, **stale())

    def test_setup(self):
        import installer

        with mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            config = installer.settings_for_run(self.path, apply=True)
        self.assertEqual((config)['locations'], moved())
        self.assertEqual(places_in(self.path), moved())
        self.assertIn('moved from the dashboard', out.getvalue())

    def test_setup_without_apply_changes_no_file(self):
        import installer

        with mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            config = installer.settings_for_run(self.path, apply=False)
        self.assertEqual(config['locations'], moved())
        self.assertNotIn('locations', json.loads(self.path.read_text()))
        self.assertIn('moved from the dashboard', out.getvalue())
        self.assertNotIn('brought up to date', out.getvalue())  # nothing was written

    def test_a_file_that_can_not_be_written_is_said_in_the_same_line(self):
        said = []
        config = json.loads(self.path.read_text())
        with mock.patch.object(neko_common, 'atomic_json', side_effect=OSError('read-only')):
            neko_common.take_places(config, [self.path], out=said.append)
        self.assertEqual(len(said), 1)
        self.assertIn(f'{self.path} could not be (read-only): the places above are used anyway.', said[0])

    def test_setup_keeps_the_record_in_step_and_makes_none(self):
        import installer

        config = installer.settings_for_run(self.path, apply=True, out=lambda line: None)
        config['locations']['music'] = 'd3'
        ctx = installer.Context(config, lambda *a: None, apply=True, config_path=self.path)
        ctx.persist_config()
        self.assertEqual(json.loads(self.record.read_text())['locations']['music'], 'd3')
        self.record.unlink()
        ctx.persist_config()
        self.assertFalse(self.record.exists())

    def test_recovery(self):
        import recover

        with mock.patch('sys.stdout', new_callable=io.StringIO):
            config, problem = recover.read_settings(str(self.path))
        self.assertIsNone(problem)
        self.assertEqual((config)['locations'], moved())
        self.assertEqual(places_in(self.path), moved())

    def test_recovery_menu(self):
        import recovery_wizard

        said = []
        config = recovery_wizard.settings(str(self.path), said.append)
        self.assertEqual((config)['locations'], moved())
        self.assertEqual(places_in(self.path), moved())
        self.assertTrue(any('moved from the dashboard' in line for line in said))

    def test_migrate(self):
        import migrate

        with mock.patch('sys.stdout', new_callable=io.StringIO):
            _, config = migrate.load_settings(str(self.path))
        self.assertEqual((config)['locations'], moved())
        self.assertEqual(places_in(self.path), moved())

    def test_upgrade(self):
        import upgrade

        with mock.patch('sys.stdout', new_callable=io.StringIO):
            config = upgrade.settings_for_run(str(self.path))
        self.assertEqual((config)['locations'], moved())
        self.assertEqual(places_in(self.path), moved())

    def test_drives_job(self):
        import storage_jobs

        with (
            mock.patch.object(storage_jobs, 'resume', return_value=0) as resume,
            mock.patch('sys.stdout', new_callable=io.StringIO),
        ):
            storage_jobs.main(['resume', '--config', str(self.path)])
        self.assertEqual(resume.call_args.args[0]['locations'], moved())
        self.assertEqual((json.loads(self.path.read_text()))['locations'], moved())
        self.assertEqual(places_in(self.path), moved())

    def test_t1_17_setup_on_a_new_ssd_keeps_the_backups_bind(self):
        """Drill 7: the bind of the backups onto d3 is one setup wants again, so its Drives
        step does not stop at "holds the settings of an app that is installed on another
        drive"."""
        import installer

        config = installer.settings_for_run(self.path, apply=True, out=lambda line: None)
        self.assertEqual(config['storage']['data_ssd'], NEW_SSD)
        self.assertEqual(neko_common.drive_folder(config, 'd3'), neko_common.EXTRA_MOUNTS / 'd3')
        self.assertEqual(config['locations']['backups'], 'd3')


# Each place in the code that writes "locations" or a drive under "storage", and why the
# record stays right with it (T5 on bc4efb8): current_places replaces the file's Extra
# drives and places as a whole, so a writer that skips the record is undone by the next run.
WRITERS = {
    ('acceptance_summary.py', "out['locations'] = moved"): 'a report, not the settings',
    ('migrate.py', "config['storage'][drive] = record_"): 'the SSD or the media drive only, never in the record',
    ('modules/dashboard.py', "clean['locations'] = places"): "the dashboard's own copy for the collector",
    (
        'modules/network_storage.py',
        "kept = config.setdefault('storage', {}).setdefault('media_hdd', {})",
    ): 'the media drive',
    (
        'modules/storage.py',
        "configured = config.setdefault('storage', {})",
    ): 'setup: saved by persist_config, which records',
    (
        'modules/storage.py',
        "chosen = config.setdefault('locations', {})",
    ): 'setup: saved by persist_config, which records',
    (
        'modules/storage.py',
        "storage = config.setdefault('storage', {})",
    ): 'setup: saved by persist_config, which records',
    ('native_runtime.py', "found['locations'] = config['locations']"): "the runtime's copy (drives.json)",
    ('neko_common.py', "new['locations'] = json.loads(json.dumps(saved['locations']))"): 'current_places itself',
}


class WritersTests(unittest.TestCase):
    def test_every_writer_of_the_places_keeps_the_record_right(self):
        import re

        pattern = re.compile(
            r"""\[['"]locations['"]\]\s*=[^=]|setdefault\(['"]locations|\[['"]storage['"]\]\[[^]]+\]\s*=[^=]"""
            r"""|\[['"]storage['"]\]\.update|setdefault\(['"]storage['"]"""
        )
        root = Path(__file__).resolve().parents[1]
        found = set()
        for path in sorted(root.rglob('*.py')):
            relative = path.relative_to(root).as_posix()
            if relative.startswith(('tests/', 'lab/')) or '/tests/' in relative:
                continue
            for line in path.read_text(encoding='utf-8').splitlines():
                if pattern.search(line):
                    found.add((relative, line.strip()))
        new = sorted(found - set(WRITERS))
        self.assertEqual(
            new,
            [],
            'A new writer of "locations" or a drive under "storage": save it through Job.save_settings or '
            "installer's persist_config (both keep /etc/nekoserver-os/places.json right), then add it here",
        )


D4 = {'uuid': 'd4-uuid', 'fstype': 'ext4', 'added': True}


class DrivesTests(Folder):
    """T1-20 (drill 5): the upgrade's setup took a my-config.json without d4 and dropped d4
    from the settings; its mount stayed, and "Move to d4" was refused ("d4" is not a drive
    under "storage"). The record holds the Extra drives too, and no tool drops one from it
    because a file lacks it."""

    def setUp(self):
        super().setUp()
        config = server()
        config['storage']['d4'] = dict(D4)
        neko_common.write_places(config)
        self.path = settings_file(self.root, **stale())  # d3 only

    def test_a_file_without_an_added_drive_takes_it_from_the_record_and_says_so(self):
        config, line = neko_common.current_places(json.loads(self.path.read_text()))
        self.assertEqual(config['storage']['d4'], D4)
        self.assertIn('Extra drives d3 and d4', line)

    def test_the_upgrade_keeps_the_drive(self):
        import upgrade

        with mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            config = upgrade.settings_for_run(str(self.path))
        self.assertEqual(config['storage']['d4'], D4)
        self.assertEqual(json.loads(self.path.read_text())['storage']['d4'], D4)
        self.assertIn('d4', out.getvalue())

    def test_a_drive_setup_gives_up_leaves_the_record(self):
        """Review of 9e390ba: setup's own "Is this drive permanently unavailable?" -> yes
        takes the drive out; the record must not bring it back at the next run."""
        import copy

        import installer

        config = installer.settings_for_run(self.path, apply=True, out=lambda line: None)
        ctx = installer.Context(config, lambda *a: None, apply=True, config_path=self.path, saved=copy.deepcopy(config))
        del ctx.config['storage']['d3']
        ctx.persist_config()
        self.assertNotIn('d3', json.loads(self.record.read_text())['storage'])
        again = json.loads(self.path.read_text())
        self.assertFalse(
            neko_common.take_places(again, [self.path], out=lambda line: None) and 'd3' in again['storage']
        )
        self.assertNotIn('d3', again['storage'])

    def test_setup_saving_a_file_without_the_drive_never_drops_it_from_the_record(self):
        import installer

        config = json.loads(self.path.read_text())  # as a tool that did not take the record
        ctx = installer.Context(config, lambda *a: None, apply=True, config_path=self.path)
        ctx.persist_config()
        self.assertEqual(json.loads(self.record.read_text())['storage']['d4'], D4)


if __name__ == '__main__':
    unittest.main()
