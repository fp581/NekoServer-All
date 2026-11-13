"""Gate run B1: Enter alone takes the "(recommended)" answer, everywhere a choice has one."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import recovery_wizard  # noqa: E402


class EnterTakesTheRecommendedTests(unittest.TestCase):
    def test_the_recovery_choice_with_a_recommended_option(self):
        asked, said = [], []
        options = [('latest', 'Only the newest version (recommended)'), ('all', 'Every version')]

        def prompt(question):
            asked.append(question)
            return ''

        self.assertEqual(recovery_wizard.choose('Which versions?', options, prompt, said.append), 'latest')
        self.assertEqual(asked, ['Choose a number (Enter for 1): '])

    def test_a_choice_with_none_recommended_still_wants_a_number(self):
        typed = iter(['', '2'])
        said = []
        options = [('a', 'One'), ('b', 'Two')]
        self.assertEqual(recovery_wizard.choose('Which?', options, lambda q: next(typed), said.append), 'b')
        self.assertIn('Please type one of the numbers shown.', said)


if __name__ == '__main__':
    unittest.main()
