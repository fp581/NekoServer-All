import itertools
import string
import unittest
from unittest import mock

import account_store

SAFE_FIRST = set(string.ascii_letters + string.digits)


class GeneratedPasswordStartTests(unittest.TestCase):
    """A made-up password never starts with '-' or '_' (T1-R12: a command line reads a
    value starting with '-' as an option, and Nextcloud's install stopped on it)."""

    def test_the_first_character_is_never_drawn_from_dash_or_underscore(self):
        fill = itertools.cycle('aA1')

        def choice(seq):
            # The worst draw: '-' whenever it can be picked first, then letters and digits.
            if '-' in seq and not getattr(choice, 'used', False):
                choice.used = True
                return '-'
            return next(fill)

        with mock.patch.object(account_store.secrets, 'choice', choice):
            _, password = account_store.generate('media')
        self.assertIn(password[0], SAFE_FIRST, password)
        self.assertEqual(len(password), 32)

    def test_many_real_passwords_start_with_a_letter_or_digit_and_keep_their_rules(self):
        for _ in range(3000):
            _, password = account_store.generate('nextcloud')
            self.assertIn(password[0], SAFE_FIRST, password)
            self.assertEqual(len(password), 32)
            self.assertTrue(set(password) <= SAFE_FIRST | {'-', '_'}, password)
            for group in (string.ascii_lowercase, string.ascii_uppercase, string.digits):
                self.assertTrue(any(ch in group for ch in password), password)


if __name__ == '__main__':
    unittest.main()
