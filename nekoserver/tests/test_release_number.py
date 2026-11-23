"""The release number is the same in every place it is kept.

An upgrade checks that the download says the version it was asked for. Twice a release
was tagged while the code still said the release before (v0.52.0-beta said 0.51.3, and
0.52.2 said 0.52.1 until the round of tests found it): every server's upgrade to it would
have stopped with "The download says it is ..., not ...". The newest changelog entry is
the release being made, so the code must say that number too.
"""

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from neko_common import VERSION  # noqa: E402


def newest_changelog_version():
    """The number of the first "## vX.Y.Z" heading of CHANGELOG.md."""
    text = (ROOT.parent / 'CHANGELOG.md').read_text(encoding='utf-8')
    found = re.search(r'^## v(\d+\.\d+\.\d+)\b', text, re.MULTILINE)
    return found.group(1) if found else None


def cargo_version():
    """The runtime package's version in runtime/Cargo.toml."""
    text = (ROOT / 'runtime/Cargo.toml').read_text(encoding='utf-8')
    package = text.split('[package]', 1)[1].split('\n[', 1)[0]
    return re.search(r'^version = "([^"]+)"', package, re.MULTILINE).group(1)


def config_version():
    """The number in config.json's project name ("NekoServer v0.52.2")."""
    name = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))['project_name']
    return re.search(r'v(\d+\.\d+\.\d+)', name).group(1)


class ReleaseNumberTests(unittest.TestCase):
    def test_the_code_says_the_release_the_changelog_is_at(self):
        newest = newest_changelog_version()
        self.assertIsNotNone(newest, 'CHANGELOG.md has no "## vX.Y.Z" heading')
        kept = {
            'neko_common.VERSION': VERSION,
            'runtime/Cargo.toml': cargo_version(),
            'config.json project_name': config_version(),
        }
        for place, number in kept.items():
            with self.subTest(place=place):
                self.assertEqual(number, newest, f'{place} says {number}, the changelog is at {newest}')


if __name__ == '__main__':
    unittest.main()
