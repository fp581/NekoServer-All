"""T1-16 (drill 10, 1.0.1): the uninstaller did not know the dashboard's Extra drives. Its
plan never named d3 or d4, nor what of NekoServer's was on them; choosing to delete the
backups named only the HDD's folder (only the mount of the backups on d3); the old copies
moves leave were listed as not NekoServer's; and d4's /etc/fstab line stayed. Now each
Extra drive is named with what is on it and its size, each choice acts on the kind's real
place and says which drive, the old copies are a choice of their own, every drive under
/mnt/nekoserver-drives goes with the mounts, and no drive is ever formatted or wiped."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import uninstaller  # noqa: E402
from test_uninstaller import SETTINGS, FakeSystem  # noqa: E402

D3, D4, D9 = '/mnt/nekoserver-drives/d3', '/mnt/nekoserver-drives/d4', '/mnt/nekoserver-drives/d9'
MEDIA = '/mnt/nekoserver-media'
OLD_MUSIC = D3 + '/nekoserver-old-music-0123456789abcdef'
OLD_PHOTOS = MEDIA + '/nekoserver-old-photos-00000000000000aa'
# As drill 10 left it: backups, photos, films and Nextcloud's files moved to d3, music
# moved back to the HDD; d4 added on the Drives page, holding a folder of the person's.
MOVED = {
    **SETTINGS,
    'storage': {
        **SETTINGS['storage'],
        'd3': {'uuid': 'eeee-5555', 'fstype': 'ext4', 'added': True},
        'd4': {'uuid': 'f4449405-4444', 'fstype': 'ext4', 'added': True},
    },
    'locations': {'backups': 'd3', 'photos': 'd3', 'movies': 'd3', 'cloud': 'd3', 'music': 'media'},
}
ALL = {key for key, *_ in uninstaller.CHOICES}
NUMBER = {key: number for number, (key, *_) in enumerate(uninstaller.CHOICES, 1)}


class ExtraDriveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.system = FakeSystem(tmp.name).populate()
        self.system.drives[D3] = 'eeee-5555'
        self.system.drives[D4] = 'f4449405-4444'
        w = self.system.write
        for path in (
            '/backups/apps/nextcloud.tar',
            '/photos/archive/2024/a.jpg',
            '/photos/immich-library/upload/b.jpg',
            '/library/movies/film.mkv',
            '/cloud/files/neko/note.txt',
            '/nekoserver-old-music-0123456789abcdef/library/music/song.mp3',
        ):
            w(D3 + path)
        w(D4 + '/films/mine.mkv')
        w(D9 + '/something.txt')  # a drive under the folder the settings do not name
        w(OLD_PHOTOS + '/photos/immich-library/c.jpg')
        w(MEDIA + '/backups/.closed')  # the mount point of d3's backups (storage.backup_binds)
        fstab = self.system.root / 'etc/fstab'
        fstab.write_text(
            fstab.read_text()
            + f'UUID=eeee-5555 {D3} ext4 defaults,nofail 0 2\n'
            + f'UUID=f4449405-4444 {D4} ext4 nofail 0 2\n'
            + f'UUID=9999-0000 {D9} ext4 nofail 0 2\n'
            + 'UUID=1234-mine /mnt/mine ext4 defaults 0 2\n'
        )
        self.plan = self.system.plan(settings=MOVED)
        self.shown = []

    def there(self, path):
        return (self.system.root / path.lstrip('/')).exists()

    def at(self, path):
        return str(self.system.root) + path

    def remove(self, chosen):
        with mock.patch.object(uninstaller.shutil, 'which', return_value='/usr/bin/x'):
            return uninstaller.remove(
                self.plan,
                chosen,
                run=self.system.run,
                out=self.shown.append,
                root=self.system.root,
                call=self.system.call,
                sleep=lambda seconds: None,
            )

    def test_the_plan_names_each_extra_drive_with_what_is_on_it(self):
        places = '\n'.join(self.plan.places())
        self.assertIn(
            D3 + " (a drive of its own: the backups, Nextcloud's files, Movies and shows's files, Photos's files)",
            places,
        )
        for path, key in (('/backups', 'backups'), ('/photos', 'photos'), ('/library', 'media'), ('/cloud', 'cloud')):
            self.assertRegex(places, self.at(D3 + path) + r' \(\S+\): kept unless you choose ' + str(NUMBER[key]))
        self.assertIn(
            D4 + " (a drive of its own: nothing of NekoServer's is placed there; what is on it stays)", places
        )
        self.assertIn(D9 + ' (a drive the settings do not name: nothing on it is removed', places)
        # The person's folder on d4 stays, whatever is chosen.
        stays = self.plan.describe(ALL).split('== NekoServer itself')[0]
        self.assertIn(self.at(D4 + '/films'), stays.split("== Not NekoServer's")[1])

    def test_old_copies_are_named_under_their_drive_and_kept_unless_chosen(self):
        places = self.plan.places()
        old = ' from a move on the Drives page; kept unless you choose ' + str(NUMBER['old'])
        d3_line = next(i for i, line in enumerate(places) if line.startswith('   - ' + D3 + ' '))
        media_line = next(i for i, line in enumerate(places) if line.startswith('   - ' + MEDIA + ' '))
        music = next(i for i, line in enumerate(places) if self.at(OLD_MUSIC) in line)
        photos = next(i for i, line in enumerate(places) if self.at(OLD_PHOTOS) in line)
        self.assertTrue(places[music].endswith('): the old copy of your music' + old))
        self.assertTrue(places[photos].endswith('): the old copy of your photos' + old))
        self.assertGreater(music, d3_line)
        self.assertGreater(photos, media_line)
        self.assertLess(photos, d3_line)
        # Not "not NekoServer's": they are its copies of your files.
        self.assertNotIn(
            'nekoserver-old-', self.plan.describe(ALL).split('== NekoServer itself')[0].split("Not Neko")[-1]
        )
        self.remove(ALL - {'mounts', 'old'})
        self.assertTrue(self.there(OLD_MUSIC + '/library/music/song.mp3'))
        self.assertTrue(self.there(OLD_PHOTOS + '/photos/immich-library/c.jpg'))

    def test_old_copies_go_only_when_chosen(self):
        self.assertIn('old copies', self.plan.phrase({'server', 'old'}))
        said = self.plan.describe({'server', 'old'})
        self.assertIn('remove ' + self.at(OLD_MUSIC) + '  (', said)
        self.assertIn(', on Extra drive d3)', said.split('remove ' + self.at(OLD_MUSIC))[1].splitlines()[0])
        self.remove({'server', 'old'})
        self.assertFalse(self.there(OLD_MUSIC))
        self.assertFalse(self.there(OLD_PHOTOS))
        self.assertTrue(self.there(D3 + '/library/movies/film.mkv'))  # only the old copies

    def test_each_delete_choice_acts_on_the_real_place_and_names_the_drive(self):
        said = self.plan.describe(ALL)
        backups = said.split('== The backups')[1].split('\n== ')[0]
        self.assertIn('remove ' + self.at(D3 + '/backups') + '  (', backups)
        self.assertIn(', on Extra drive d3)', backups)
        self.assertNotIn(self.at(MEDIA + '/backups') + ' ', backups + '\n')  # only the mount of d3's backups
        self.assertIn(
            'The photo archive in ' + D3 + '/photos/archive goes with your photos (choice ' + str(NUMBER['photos']),
            backups,
        )
        films = said.split('== Your movies')[1].split('\n== ')[0]
        self.assertIn('remove ' + self.at(D3 + '/library') + '  (', films)
        self.remove({'server', 'backups'})
        self.assertFalse(self.there(D3 + '/backups'))
        self.assertTrue(self.there(MEDIA + '/backups/.closed'), 'the mount point on the HDD is not the backups')
        self.assertTrue(self.there(D3 + '/library/movies/film.mkv'))
        self.assertTrue(self.there(D3 + '/photos/archive/2024/a.jpg'), 'it goes with the photos, not chosen')

    def test_everything_chosen_leaves_the_person_s_files_and_nothing_of_neko_s_on_d3(self):
        self.remove(ALL - {'mounts'})
        for gone in ('/backups', '/photos', '/library', '/cloud', '/nekoserver-old-music-0123456789abcdef'):
            self.assertFalse(self.there(D3 + gone), gone)
        self.assertTrue(self.there(D4 + '/films/mine.mkv'))
        self.assertTrue(self.there(D9 + '/something.txt'))

    def test_the_mounts_take_every_extra_drive_s_line_and_never_format_a_drive(self):
        lines = self.plan.fstab_lines()
        for folder in (D3, D4, D9):
            self.assertTrue(any(line.split()[1] == folder for line in lines), folder)
        self.assertFalse(any('/mnt/mine' in line for line in lines))
        notes = ' '.join(self.plan.groups['mounts']['notes'])
        self.assertIn('Unmount ' + D4 + ' (a drive of its own)', notes)
        self.assertIn('Unmount ' + D9 + ' (a drive the settings do not name)', notes)
        self.remove(ALL)
        fstab = (self.system.root / 'etc/fstab').read_text()
        for gone in ('eeee-5555', 'f4449405-4444', '9999-0000'):
            self.assertNotIn(gone, fstab)
        self.assertIn('UUID=1234-mine /mnt/mine ext4 defaults 0 2', fstab)
        self.assertTrue(list(self.system.root.glob('etc/fstab.*')), 'the file as it was is kept')
        self.assertTrue(self.there(D4 + '/films/mine.mkv'))
        for call in self.system.calls:
            self.assertFalse(
                any(tool in ' '.join(map(str, call)) for tool in ('mkfs', 'wipefs', 'sgdisk', 'blkdiscard', ' dd ')),
                call,
            )

    def test_neko_s_binds_from_an_extra_drive_go_with_the_mounts_and_the_person_s_stay(self):
        # T5, review of T1-16: drill 10's binds mount elsewhere (on the HDD, the SSD), so
        # their mount point alone did not find them. Here they are outside setup's blocks.
        waits = 'bind,nofail,x-systemd.requires-mounts-for=' + D3
        neko = [
            f'{D3}/backups {MEDIA}/backups none {waits} 0 0',
            f'{D3}/photos/archive {MEDIA}/photos/archive none {waits} 0 0',
            f'{D3}/nekoserver/appdata/immich /srv/nekoserver/appdata/immich none {waits},'
            'x-systemd.requires-mounts-for=/srv/nekoserver 0 0',
            # Found only by the drive it waits for: its source is not on d3.
            f'/srv/nekoserver/appdata/shared /srv/nekoserver/appdata/radarr/shared none {waits} 0 0',
        ]
        mine = f'{D4}/films {MEDIA}/library/extra/films none bind,ro,nofail 0 0'
        fstab = self.system.root / 'etc/fstab'
        fstab.write_text(fstab.read_text() + '\n'.join(neko + [mine]) + '\n')
        settings = {
            **MOVED,
            'placement': {'movies': {'extra': [{'path': D4 + '/films', 'holds': 'movies', 'use': 'in_place'}]}},
        }
        self.plan = self.system.plan(settings=settings)
        lines = self.plan.fstab_lines()
        for line in neko:
            self.assertIn(line, lines)
        self.assertNotIn(mine, lines)
        mounted = {self.at(MEDIA + '/backups'), self.at(MEDIA + '/photos/archive')}
        real = uninstaller.os.path.ismount
        with mock.patch.object(uninstaller.os.path, 'ismount', side_effect=lambda p: str(p) in mounted or real(p)):
            self.remove({'server', 'mounts'})
        text = fstab.read_text()
        for line in neko:
            self.assertNotIn(line, text)
        self.assertIn(mine, text)
        umounts = [call[1] for call in self.system.calls if call[:1] == ['umount']]
        self.assertEqual(umounts[:2], [MEDIA + '/photos/archive', MEDIA + '/backups'])  # the deepest first
        self.assertTrue(self.there(D3 + '/backups/apps/nextcloud.tar'))

    def test_the_person_s_folder_with_a_space_keeps_its_bind(self):
        # T5, review of 0443aec: fstab writes "T1 Films" as T1\040Films (drill 9's folder).
        own = D4 + '/T1 Films'
        target = MEDIA + '/library/extra/T1 Films'
        mine = f'{D4}/T1\\040Films {MEDIA}/library/extra/T1\\040Films none bind,ro,nofail 0 0'
        neko = f'{D3}/my\\040backups {MEDIA}/my\\040backups none bind,nofail,x-systemd.requires-mounts-for={D3} 0 0'
        fstab = self.system.root / 'etc/fstab'
        fstab.write_text(fstab.read_text() + mine + '\n' + neko + '\n')
        settings = {**MOVED, 'placement': {'movies': {'extra': [{'path': own, 'holds': 'movies', 'use': 'in_place'}]}}}
        self.plan = self.system.plan(settings=settings)
        self.assertNotIn(mine, self.plan.fstab_lines())
        self.assertIn(neko, self.plan.fstab_lines())
        mounted = {self.at(target), self.at(MEDIA + '/my backups')}
        real = uninstaller.os.path.ismount
        with mock.patch.object(uninstaller.os.path, 'ismount', side_effect=lambda p: str(p) in mounted or real(p)):
            self.remove({'server', 'mounts'})
        self.assertIn(mine, fstab.read_text())
        umounts = [call[1] for call in self.system.calls if call[:1] == ['umount']]
        self.assertNotIn(target, umounts)
        self.assertIn(MEDIA + '/my backups', umounts)  # unmounted by the path, not its fstab spelling


class StaleSettingsTests(unittest.TestCase):
    """T1-16 with T1-17's rule: uninstall.sh run from a folder whose my-config.json is from
    before the moves (even with a fresh time, as unpacking gives it) uses the places the
    Drives page last wrote (/etc/nekoserver-os/places.json), says so, and writes nothing."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.system = FakeSystem(tmp.name).populate()
        self.system.drives[D3] = 'eeee-5555'
        self.system.write(D3 + '/backups/apps/nextcloud.tar')
        self.system.write(D3 + '/library/movies/film.mkv')
        self.folder = Path(tmp.name) / 'download'
        self.folder.mkdir()
        self.stale = dict(SETTINGS)  # from before the moves: no d3, no locations
        self.mine = self.folder / 'my-config.json'
        self.mine.write_text(json.dumps(self.stale))
        # Only the Extra drives are the record's: the SSD's and the HDD's entries stay the file's.
        extra = {k: MOVED['storage'][k] for k in ('d3', 'd4')}
        self.record = self.system.write(
            '/' + uninstaller.PLACES, json.dumps({'storage': extra, 'locations': MOVED['locations']})
        )
        os.utime(self.record, (1, 1))  # older than the stale file: the time never decides

    def test_the_places_the_drives_page_wrote_win_and_are_said(self):
        settings, source = uninstaller.find_settings(self.folder, self.system.root)
        self.assertEqual(settings['locations'], MOVED['locations'])
        self.assertEqual(settings['storage']['d3']['uuid'], 'eeee-5555')
        self.assertEqual(settings['storage']['data_ssd'], SETTINGS['storage']['data_ssd'])
        self.assertTrue(source.startswith(str(self.mine) + ". This server's data was moved from the dashboard"))
        self.assertEqual(json.loads(self.mine.read_text()), self.stale, 'the uninstaller writes no settings')
        self.assertNotIn('brought up to date', source)  # and says no file was
        self.assertTrue(source.endswith('(from ' + str(self.record) + ').'), source)
        plan = self.system.plan(settings=settings)
        said = plan.describe({'server', 'backups', 'media'})
        self.assertIn('remove ' + str(self.system.root) + D3 + '/backups  (', said)
        self.assertIn('remove ' + str(self.system.root) + D3 + '/library  (', said)

    def test_without_a_record_or_when_it_agrees_the_file_is_used_as_before(self):
        self.record.unlink()
        settings, source = uninstaller.find_settings(self.folder, self.system.root)
        self.assertEqual((settings, source), (self.stale, str(self.mine)))
        self.mine.write_text(json.dumps(MOVED))
        self.system.write(
            '/' + uninstaller.PLACES,
            json.dumps({'storage': {k: MOVED['storage'][k] for k in ('d3', 'd4')}, 'locations': MOVED['locations']}),
        )
        settings, source = uninstaller.find_settings(self.folder, self.system.root)
        self.assertEqual((settings, source), (MOVED, str(self.mine)))

    def test_the_record_also_corrects_the_drives_setup_recorded(self):
        self.mine.unlink()
        self.system.write(
            '/etc/nekoserver-os/drives.json',
            json.dumps({k: SETTINGS[k] for k in ('data_mount', 'media_mount', 'storage')}),
        )
        settings, source = uninstaller.find_settings(self.folder, self.system.root)
        self.assertEqual(settings['locations'], MOVED['locations'])
        self.assertIn("(the drives setup recorded", source)
        self.assertIn("This server's data was moved from the dashboard", source)


if __name__ == '__main__':
    unittest.main()
