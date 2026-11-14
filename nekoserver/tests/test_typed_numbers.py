"""A number typed at a question (deep check plan 4).

str.isdigit() is true for "²" (a superscript two) and other digit-like characters that
int() refuses, so `answer.isdigit() and int(answer)` ended setup's questions, the recovery
menu and the uninstaller in a traceback. str.isdecimal() is true exactly for what int()
reads (also "١٢", Arabic-Indic twelve, which int() reads as 12)."""

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import drive_table  # noqa: E402
import recovery_wizard  # noqa: E402
import uninstaller  # noqa: E402

ODD_NUMBERS = ('²', '①', '¹⁰', '٣')  # superscripts and a circled one int() refuses; '٣' it reads as 3


def answers(*typed):
    given = iter(typed)
    return lambda question='': next(given)


class TypedNumberTests(unittest.TestCase):
    def test_the_recovery_menu_asks_again(self):
        said = []
        options = [('a', 'First'), ('b', 'Second'), ('c', 'Third')]
        chosen = recovery_wizard.choose('Pick one', options, answers(*ODD_NUMBERS[:3], '0'), said.append)
        self.assertIsNone(chosen)
        self.assertEqual(said.count('Please type one of the numbers shown.'), 3)
        # A decimal digit of another script is the number it is.
        self.assertEqual(recovery_wizard.choose('Pick one', options, answers('٣'), said.append), 'c')

    def test_the_uninstallers_checklist_says_not_a_choice(self):
        said = []
        uninstaller.choose(answers('² ①', ''), said.append)
        self.assertIn(' Not a choice: ²', said)
        self.assertIn(' Not a choice: ①', said)

    def test_the_drive_picker_takes_no_drive(self):
        table = ['first drive', 'second drive']
        for odd in ODD_NUMBERS[:3]:
            with self.subTest(answer=odd):
                self.assertIsNone(drive_table.numbered(table, odd))

    def test_no_typed_number_is_read_with_isdigit(self):
        """A guard over the sources: isdigit() followed by int() is the mistake above. The
        restore tool checks isascii() first, which makes isdigit() exact there."""
        allowed = {'neko_restore.py'}
        found = []
        for path in sorted(ROOT.glob('*.py')) + sorted((ROOT / 'modules').glob('*.py')):
            if path.name in allowed:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if isinstance(node, ast.Attribute) and node.attr == 'isdigit':
                    found.append(f'{path.relative_to(ROOT)}:{node.lineno}')
        self.assertEqual(found, [], 'use isdecimal(): it is true exactly for what int() reads')


if __name__ == '__main__':
    unittest.main()
