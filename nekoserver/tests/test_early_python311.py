"""Deep check 273: the suite on the oldest Python NekoServer supports. Python 3.11.0 to
3.11.2 (Debian 12 has 3.11.2) cannot close an HTTPError made without a body
(fp=None): it raises KeyError('file'), and when a test meets it the unittest runner
itself stops with a traceback, so none of the later tests run. A test's stand-in error
gets a body (io.BytesIO()) instead."""

import re
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
WITHOUT_BODY = re.compile(r'HTTPError\([^()]*\{\}, None\)')


class EarlyPython311Tests(unittest.TestCase):
    def test_no_stand_in_http_error_is_made_without_a_body(self):
        found = [
            f'{path.name}:{number}'
            for path in sorted(TESTS.glob('*.py'))
            for number, line in enumerate(path.read_text().splitlines(), 1)
            if WITHOUT_BODY.search(line)
        ]
        self.assertEqual(found, [])


if __name__ == '__main__':
    unittest.main()
