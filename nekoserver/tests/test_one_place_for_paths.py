"""The folders of the photos, the music and Nextcloud's files are named in one place,
shared.json "locations", and every path to them is built from it (neko_common.files_root,
music_library, cloud_files; the runtime's saved_path and shared_folder).

0.46.0 let setup put each on the SSD or the media drive, but three parts (the library
check, Lidarr's music folder and the music import) kept building "SSD + library/music"
themselves, so with music on the media drive connecting the apps never finished (seen on
a fresh Debian VM after 0.47.6). This test fails when a path to those folders is written
by hand again."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAMES = re.compile(r"library/music|library/downloads/music|photos/immich-library|cloud/files|['\"]immich-library['\"]")

# Places that may name them, and why. Each entry is (file, a piece of the line).
ALLOWED = {
    # The old Lidarr mounts, kept as text only to recognise and migrate an unedited file
    # written before 0.21.1.
    ('modules/media.py', '"${NEKO_DATA}/library/downloads/music:/data/downloads/music"'),
    ('modules/media.py', '"${NEKO_DATA}/library/music:/data/music"'),
    # The media drive's photo folder is reserved, so the photo archive can never be put
    # inside or around it, wherever the photos are kept now.
    ('installer.py', "managed = media / 'photos' / 'immich-library'"),
}
# The Compose templates: the drive is setup's choice (filled in from files_root), and the
# folder after it must be shared.json's.
COMPOSE_DRIVE = re.compile(r'\$\{NEKO_(MUSIC|PHOTOS|CLOUD)\}/(' + NAMES.pattern + ')')


def without_rust_tests(text):
    """The source with each #[cfg(test)] module left out: from that line to the module's
    closing brace, which rustfmt always puts alone at the start of a line (counting braces
    would be thrown off by the ones inside strings). Blank lines keep the line numbers."""
    lines, kept, skipping = text.splitlines(), [], False
    for line in lines:
        if line.startswith('#[cfg(test)]'):
            skipping = True
        kept.append('' if skipping else line)
        if skipping and line == '}':
            skipping = False
    return '\n'.join(kept)


def found():
    hits = []
    for path in sorted(ROOT.rglob('*')):
        if path.suffix not in ('.py', '.rs', '.yaml', '.js', '.sh') or {'tests', 'target'} & set(path.parts):
            continue
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(errors='replace')
        if path.suffix == '.rs':
            text = without_rust_tests(text)
        for number, line in enumerate(text.splitlines(), 1):
            if not NAMES.search(line):
                continue
            if rel.startswith('compose/') and COMPOSE_DRIVE.search(line):
                continue
            if any(rel == file and piece in line for file, piece in ALLOWED):
                continue
            hits.append(f'{rel}:{number}: {line.strip()}')
    return hits


class OnePlaceForPathsTests(unittest.TestCase):
    def test_no_path_to_the_photos_music_or_cloud_files_is_written_by_hand(self):
        self.assertEqual(found(), [], 'build these from neko_common.files_root / music_library / cloud_files')

    def test_the_names_are_shared_jsons(self):
        import neko_common

        names = {folder for _, folders in neko_common.LOCATIONS.values() for folder in folders}
        self.assertLessEqual(
            {'library/music', 'library/downloads/music', 'photos/immich-library', 'cloud/files'}, names
        )

    def test_the_guard_finds_a_path_written_by_hand(self):
        self.assertTrue(NAMES.search("music = root / 'library/music'"))
        self.assertIn(
            'fn real() {}', without_rust_tests('fn real() {}\n#[cfg(test)]\nmod t { fn x() { "library/music" } }\n')
        )
        self.assertNotIn(
            'library/music', without_rust_tests('#[cfg(test)]\nmod t { fn x() { "library/music" } }\nfn y() {}')
        )


if __name__ == '__main__':
    unittest.main()
