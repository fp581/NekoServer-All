"""0.50.0: what setup's review page knows before the long install starts (the wizard
specification's P07; its check T08: an unsupported layout or too little room stops
before many packages are installed).

before_install.check(config, look) only looks. Every case here stands in for this PC
(its drives, its memory, the download places) with a fake `look`.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import before_install  # noqa: E402
from modules import assistant, capacity  # noqa: E402

GB = 1024**3
SSD_ID, HDD_ID, USB_ID = 'ssd-0001', 'hdd-0001', 'usb-0001'


class FakeLook:
    """This PC as a test describes it. Counts what was asked; has nothing that could change anything."""

    def __init__(self, drives=None, memory=16.0, silent=()):
        self.found = {
            SSD_ID: {'fstype': 'ext4', 'size': 500 * GB, 'free': 400 * GB, 'mount': '/srv/nekoserver'},
            HDD_ID: {'fstype': 'ext4', 'size': 4000 * GB, 'free': 3000 * GB, 'mount': '/mnt/nekoserver-media'},
        }
        self.found.update(drives or {})
        self.found = {uuid: facts for uuid, facts in self.found.items() if facts is not None}
        self.total, self.silent, self.asked = memory, set(silent), []

    def drives(self):
        return self.found

    def folder_free(self, folder):
        return 900 * GB

    def reaches(self, host):
        self.asked.append(host)
        return host not in self.silent

    def memory(self):
        return self.total


def settings(**changes):
    """A new server's settings with its two drives chosen."""
    config = json.loads((ROOT / 'config.json').read_text())
    config['storage']['data_ssd'] = {'uuid': SSD_ID, 'fstype': 'ext4'}
    config['storage']['media_hdd'] = {'uuid': HDD_ID, 'fstype': 'ext4'}
    config.update(changes)
    return config


def problems(found):
    return [row['problem'] for row in found]


class CheckTests(unittest.TestCase):
    def check(self, config=None, **look):
        self.look = FakeLook(**look)
        return before_install.check(config or settings(), self.look)

    def test_a_pc_with_room_memory_and_a_connection_has_nothing_to_say(self):
        found = self.check()
        self.assertEqual((found['blocking'], found['warnings']), ([], []))
        self.assertEqual([row['name'] for row in found['drives']], ['ssd', 'media'])
        ssd, media = found['drives']
        self.assertEqual(capacity.SSD, (ssd['least_gb'], ssd['recommended_gb']))
        self.assertEqual(capacity.HDD, (media['least_gb'], media['recommended_gb']))
        self.assertEqual(ssd['holds'], ['the apps, their settings, photos and music'])
        self.assertEqual(self.look.asked, ['download.docker.com', 'registry-1.docker.io', 'ghcr.io'])

    def test_every_finding_says_what_is_wrong_and_what_to_do(self):
        found = self.check(
            settings(assistant={'wanted': True, 'model': 'auto'}),
            drives={SSD_ID: {'fstype': 'ext4', 'size': 500 * GB, 'free': 10 * GB, 'mount': '/srv/nekoserver'}},
            memory=4.0,
            silent=['ghcr.io'],
        )
        rows = found['blocking'] + found['warnings']
        self.assertGreaterEqual(len(rows), 4)
        for row in rows:
            self.assertEqual(sorted(row), ['about', 'do', 'problem'])
            self.assertIn(row['about'], ('drive', 'space', 'downloads', 'memory'))
            self.assertTrue(row['problem'].endswith('.') and row['do'].endswith('.'), row)

    # ---- each chosen drive is there and of a usable kind

    def test_a_chosen_drive_that_is_not_connected_blocks(self):
        found = self.check(drives={HDD_ID: None})
        self.assertEqual(
            problems(found['blocking']),
            ['The drive chosen as "media" is not connected to this PC (or is switched off).'],
        )
        self.assertEqual(found['blocking'][0]['do'], 'Connect it and check its cable and power, then look again.')

    def test_a_drive_formatted_as_something_else_blocks_and_is_never_formatted(self):
        for kind in ('ntfs', 'exfat', 'vfat', ''):
            found = self.check(drives={HDD_ID: {'fstype': kind, 'size': 4000 * GB, 'free': None, 'mount': None}})
            said = found['blocking'][0]
            self.assertIn(f'is formatted as {kind or "nothing NekoServer knows"}.', said['problem'])
            self.assertIn('only on drives formatted as ext4, xfs or btrfs', said['problem'])
            self.assertIn('setup never formats a drive', said['do'])
        for kind in ('xfs', 'btrfs'):
            config = settings()
            config['storage']['media_hdd']['fstype'] = kind
            found = self.check(config, drives={HDD_ID: {'fstype': kind, 'size': 1, 'free': 3000 * GB, 'mount': '/m'}})
            self.assertEqual(found['blocking'], [])

    def test_a_drive_formatted_again_since_it_was_chosen_blocks(self):
        found = self.check(drives={SSD_ID: {'fstype': 'xfs', 'size': 500 * GB, 'free': 400 * GB, 'mount': '/x'}})
        self.assertIn('is formatted as xfs now, but it was ext4 when it was chosen', found['blocking'][0]['problem'])

    def test_a_drive_the_table_names_but_the_settings_do_not_have_blocks(self):
        config = settings()
        config['storage']['media_hdd'] = {}
        found = self.check(config)
        self.assertEqual(
            problems(found['blocking']),
            ['"media" is chosen in the table of drives, but no such drive is in the settings.'],
        )

    def test_the_removable_backup_drive_missing_is_a_warning_not_a_stop(self):
        config = settings()
        config['photos']['external_backup_uuid'] = USB_ID
        found = self.check(config)
        self.assertEqual(found['blocking'], [])
        self.assertIn('It is the removable backup drive, so setup goes on without it', found['warnings'][0]['problem'])
        self.assertIn('have no copy outside this PC', found['warnings'][0]['do'])
        # Plugged in: nothing to say, and no room is asked of it here.
        plugged = {USB_ID: {'fstype': 'ext4', 'size': 64 * GB, 'free': 1 * GB, 'mount': None}}
        found = self.check(config, drives=plugged)
        self.assertEqual((found['blocking'], found['warnings']), ([], []))
        self.assertEqual(found['drives'][-1]['holds'], ['a copy of your photos, files and password lists'])

    # ---- free space, summed over what is placed on each drive

    def test_too_little_room_blocks_with_the_exact_numbers(self):
        found = self.check(drives={SSD_ID: {'fstype': 'ext4', 'size': 120 * GB, 'free': 12 * GB, 'mount': '/s'}})
        self.assertEqual(
            found['blocking'][0],
            {
                'about': 'space',
                'problem': 'The drive "ssd" has 12 GB free. What you placed on it (the apps, their settings, photos '
                'and music) needs at least 20 GB.',
                'do': 'Free up space on it, choose a bigger drive, or place some of it on another drive.',
            },
        )
        # Never rounded up to the number it is short of.
        found = self.check(drives={SSD_ID: {'fstype': 'ext4', 'size': 1, 'free': int(19.6 * GB), 'mount': '/s'}})
        self.assertIn('has 19.6 GB free.', found['blocking'][0]['problem'])

    def test_less_than_recommended_warns_with_both_numbers(self):
        found = self.check(drives={SSD_ID: {'fstype': 'ext4', 'size': 64 * GB, 'free': 40 * GB, 'mount': '/s'}})
        self.assertEqual(found['blocking'], [])
        self.assertEqual(
            problems(found['warnings']),
            [
                'The drive "ssd" has 40 GB free. What you placed on it (the apps, their settings, photos and music) '
                'needs at least 20 GB, and 100 GB or more is recommended.'
            ],
        )
        self.assertIn('You can go on', found['warnings'][0]['do'])
        # An empty drive sold as the recommended size is not warned about (a "500 GB" drive has 450 free).
        sold = {HDD_ID: {'fstype': 'ext4', 'size': 465 * GB, 'free': 452 * GB, 'mount': '/m'}}
        self.assertEqual(self.check(drives=sold)['warnings'], [])

    def test_what_is_placed_on_one_drive_is_added_up(self):
        """Photos kept on the media drive stay on the apps' side of the numbers; a drive
        that holds both sides needs both, as the drive space step counts a shared disk."""
        both = settings()
        both['drives'] = {'ssd': {'uuid': SSD_ID, 'fstype': 'ext4'}}
        both['placement'] = {
            'core': {'install': 'ssd'},
            'photos': {'install': 'ssd', 'data': 'ssd'},
            'movies': {'install': 'ssd', 'data': 'ssd'},
            'music': {'install': 'ssd', 'data': 'ssd'},
            'cloud': {'install': 'ssd', 'data': 'ssd'},
            'backup': {'data': 'ssd'},
            'offline': {'data': None},
        }
        needs = before_install.needed(both)
        self.assertEqual(list(needs), ['ssd'])
        self.assertEqual((needs['ssd']['least'], needs['ssd']['recommended']), (70, 600))
        found = self.check(both, drives={SSD_ID: {'fstype': 'ext4', 'size': 1, 'free': 60 * GB, 'mount': '/s'}})
        self.assertIn('has 60 GB free.', found['blocking'][0]['problem'])
        self.assertIn('needs at least 70 GB.', found['blocking'][0]['problem'])
        self.assertIn(
            'photos and music, films, shows, Nextcloud files and the backups', found['blocking'][0]['problem']
        )

    def test_the_ai_model_is_counted_on_the_drive_nekoserver_is_on(self):
        sizes = [size for _name, size, _memory, _words in assistant.MODELS]
        auto = before_install.needed(settings(assistant={'wanted': True, 'model': 'auto'}))['ssd']
        self.assertEqual(auto['least'], 20 + assistant.IMAGES_GB + min(sizes))
        self.assertEqual(auto['recommended'], 100 + assistant.IMAGES_GB + max(sizes))
        self.assertEqual(auto['holds'][-1], 'the AI assistant and its model')
        named = before_install.needed(settings(assistant={'wanted': True, 'model': 'qwen3.5:9b'}))['ssd']
        self.assertEqual(named['least'], 20 + assistant.IMAGES_GB + 6.6)
        own = before_install.needed(settings(assistant={'wanted': True, 'model': 'gemma4:12b'}))['ssd']
        self.assertEqual(own['least'], 20 + assistant.IMAGES_GB + assistant.UNKNOWN_MODEL_GB)
        # Not chosen: not counted, and its download place is not asked.
        self.assertEqual(before_install.needed(settings())['ssd']['least'], 20)
        self.assertEqual(before_install.needed(settings(assistant={'wanted': False}))['ssd']['least'], 20)
        # 25 GB free is enough without it and too little with the biggest model.
        tight = {SSD_ID: {'fstype': 'ext4', 'size': 1, 'free': 25 * GB, 'mount': '/s'}}
        self.assertEqual(self.check(drives=tight)['blocking'], [])
        found = self.check(settings(assistant={'wanted': True, 'model': 'qwen3.5:9b'}), drives=tight)
        self.assertIn('needs at least 31.6 GB.', found['blocking'][0]['problem'])
        self.assertIn('registry.ollama.ai', self.look.asked)

    def test_a_drive_not_connected_to_a_folder_yet_is_judged_by_its_size(self):
        small = {HDD_ID: {'fstype': 'ext4', 'size': 32 * GB, 'free': None, 'mount': None}}
        found = self.check(drives=small)
        self.assertIn('The drive "media" is 32 GB in all.', found['blocking'][0]['problem'])
        self.assertIn('needs at least 50 GB free', found['blocking'][0]['problem'])
        big = {HDD_ID: {'fstype': 'ext4', 'size': 4000 * GB, 'free': None, 'mount': None}}
        found = self.check(drives=big)
        self.assertEqual(found['blocking'], [])
        self.assertIn('cannot be read before setup connects it', found['warnings'][0]['problem'])
        self.assertIn('stops there if it is too little', found['warnings'][0]['do'])

    def test_a_shared_folder_of_a_storage_box_is_measured_where_it_is(self):
        config = settings()
        config['storage']['media_hdd'] = {'uuid': 'nas:/volume1/media', 'fstype': 'nfs4', 'network': {'kind': 'nfs'}}
        found = self.check(config, drives={HDD_ID: None})
        self.assertEqual((found['blocking'], found['warnings']), ([], []))
        self.assertEqual((found['drives'][1]['shared'], found['drives'][1]['free']), (True, 900 * GB))

    # ---- the places setup downloads from

    def test_no_connection_at_all_is_said_once(self):
        found = self.check(silent=['download.docker.com', 'registry-1.docker.io', 'ghcr.io'])
        self.assertEqual(
            found['blocking'],
            [
                {
                    'about': 'downloads',
                    'problem': 'This PC cannot reach the internet places setup downloads from (none of them answered).',
                    'do': 'Check the network cable or Wi-Fi and that other websites open on this PC, then look again.',
                }
            ],
        )

    def test_one_place_that_does_not_answer_is_named_with_what_comes_from_it(self):
        found = self.check(silent=['ghcr.io'])
        self.assertEqual(
            problems(found['blocking']), ['ghcr.io does not answer. Setup downloads the rest of the apps from there.']
        )

    # ---- memory

    def test_little_memory_warns_and_never_blocks(self):
        found = self.check(memory=3.7)
        self.assertEqual(found['blocking'], [])
        self.assertIn('This PC has 3.7 GB of memory.', found['warnings'][0]['problem'])
        self.assertIn('with less than 8 GB those can be stopped', found['warnings'][0]['problem'])
        self.assertEqual(self.check(memory=7.6)['warnings'], [])
        self.assertEqual(self.check(memory=0)['warnings'], [])  # not readable: nothing is made up

    def test_the_ai_assistant_says_when_no_model_fits_in_memory(self):
        chosen = settings(assistant={'wanted': True, 'model': 'auto'})
        found = self.check(chosen, memory=4.0)
        said = [row for row in found['warnings'] if 'AI assistant' in row['problem']][0]
        self.assertIn('needs 3 GB of memory for its smallest model', said['problem'])
        self.assertIn('This PC has 4.0 GB, which leaves 2.0 GB for it.', said['problem'])
        self.assertIn('Setup goes on without the assistant', said['do'])
        self.assertEqual(self.check(chosen, memory=16.0)['warnings'], [])

    # ---- how long

    def test_the_time_is_a_rough_range_that_grows_with_what_is_chosen(self):
        everything = before_install.time_needed(settings(assistant={'wanted': True, 'model': 'auto'}))
        few = settings(media_apps={'photos': True, 'movies': False, 'music': False, 'downloads': False})
        few['nextcloud']['enabled'] = False
        little = before_install.time_needed(few)
        self.assertEqual((little['low'], little['high']), (25, 50))
        self.assertGreater(everything['low'], little['low'])
        self.assertEqual(everything['high'], 2 * everything['low'])
        self.assertIn(f'about {little["low"]} to {little["high"]} minutes on a usual home connection', little['words'])
        self.assertEqual(self.check(few)['time'], little)

    # ---- looking only

    def test_the_real_look_runs_one_listing_command_and_nothing_that_changes_anything(self):
        from unittest import mock

        listing = {
            'blockdevices': [
                {
                    'uuid': None,
                    'fstype': None,
                    'size': 500 * GB,
                    'fsavail': None,
                    'mountpoint': None,
                    'children': [
                        {'uuid': SSD_ID, 'fstype': 'ext4', 'size': '499', 'fsavail': '123', 'mountpoint': '/srv/x'},
                        {'uuid': HDD_ID, 'fstype': 'ntfs', 'size': 7, 'fsavail': None, 'mountpoint': None},
                    ],
                }
            ]
        }
        ran = []

        def run(argv, **kwargs):
            ran.append(argv)
            return mock.Mock(returncode=0, stdout=json.dumps(listing))

        with mock.patch.object(before_install.subprocess, 'run', side_effect=run):
            found = before_install.Look().drives()
        self.assertEqual(ran, [['lsblk', '-J', '-b', '-o', 'UUID,FSTYPE,SIZE,FSAVAIL,MOUNTPOINT']])
        self.assertEqual(found[SSD_ID], {'fstype': 'ext4', 'size': 499, 'free': 123, 'mount': '/srv/x'})
        self.assertEqual(found[HDD_ID], {'fstype': 'ntfs', 'size': 7, 'free': None, 'mount': None})
        with mock.patch.object(before_install.subprocess, 'run', side_effect=OSError):
            self.assertEqual(before_install.Look().drives(), {})
        # The module can run nothing else, and writes nowhere: no other command, no open for writing.
        source = (ROOT / 'before_install.py').read_text()
        self.assertEqual(source.count('subprocess.run('), 1)
        for call in ('.write_text(', '.mkdir(', '.unlink(', 'os.remove(', 'atomic_json(', 'persist_config('):
            self.assertNotIn(call, source)


if __name__ == '__main__':
    unittest.main()
