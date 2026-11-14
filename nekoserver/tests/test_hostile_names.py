"""Names that come from outside (a drive's label and model, a folder's name) never carry
a terminal escape or a right-to-left mark onto the terminal as they are (deep check 228)."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import drive_table
import neko_common

# What a USB stick can be given as its label or model: clear the screen, retitle the
# window, and a right-to-left mark that makes "gpj.exe" read as "exe.jpg".
HOSTILE = 'pics\x1b[2J\x1b]0;pwned\x07‮gpj.exe\x9b'


def raw(text):
    return any(c in text for c in '\x1b\x07‮\x9b')


class DriveNameTests(unittest.TestCase):
    def test_a_drive_label_and_model_are_printed_escaped(self):
        inventory = {
            'blockdevices': [
                {
                    'name': 'sdb',
                    'size': '1T',
                    'type': 'disk',
                    'model': 'Disk' + HOSTILE,
                    'tran': 'usb',
                    'children': [
                        {
                            'name': 'sdb1',
                            'size': '1T',
                            'type': 'part',
                            'fstype': 'ext4',
                            'uuid': '1111-2222',
                            'label': HOSTILE,
                            'mountpoint': None,
                        }
                    ],
                }
            ]
        }
        config = json.loads((ROOT / 'config.json').read_text())
        table = drive_table.rows(inventory, config, items=[], look=lambda *a: (None, None))
        lines = '\n'.join(drive_table.shown(table, config))
        self.assertFalse(raw(lines), lines)
        self.assertIn('named "pics\\x1b[2J\\x1b]0;pwned\\x07\\u202egpj.exe\\x9b"', lines)

    def test_printable_keeps_every_ordinary_letter(self):
        self.assertEqual(neko_common.printable('Fotos é 日本 🐱 ok'), 'Fotos é 日本 🐱 ok')
        self.assertFalse(raw(neko_common.printable(HOSTILE)))


if __name__ == '__main__':
    unittest.main()
