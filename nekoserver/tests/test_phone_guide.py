"""Phone setup steps and the QR page."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import guided_setup  # noqa: E402
import phone_guide  # noqa: E402

LINKS = {
    'jellyfin': 'https://neko.tail1234.ts.net:8443/',
    'seerr': 'https://neko.tail1234.ts.net:8444/',
    'immich': 'https://neko.tail1234.ts.net:8445/',
    'navidrome': 'https://neko.tail1234.ts.net:8446/',
    'nextcloud': 'https://neko.tail1234.ts.net:8447/',
}
USERS = {
    'jellyfin': {'username': 'neko_ab12'},
    'navidrome_listener': {'username': 'listener'},
    'immich': {'username': 'me@example.org', 'password': 'SECRET-PW'},
}


def fake_get(config, app, verified=False):
    return USERS.get(app)


class PhoneGuideTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch('account_store.get', fake_get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_everyday_app_has_phone_apps_and_steps(self):
        self.assertEqual(set(phone_guide.APPS), set(LINKS) | {'dashboard'})
        for app, info in phone_guide.APPS.items():
            with self.subTest(app=app):
                self.assertTrue(info['iphone'] and info['android'] and info['steps'])

    def test_dashboard_needs_no_login(self):
        url = 'https://neko.tail1234.ts.net:8448/'
        text = '\n'.join(phone_guide.instructions({}, 'dashboard', url))
        self.assertIn(url, text)
        self.assertNotIn('Username', text)
        self.assertNotIn('Password', text)

    def test_instructions_show_address_and_username_never_password(self):
        steps = phone_guide.instructions({}, 'immich', LINKS['immich'])
        text = '\n'.join(steps)
        self.assertIn(LINKS['immich'], text)
        self.assertIn('me@example.org', text)
        self.assertIn('Immich (App Store)', text)
        self.assertNotIn('SECRET-PW', text)
        self.assertIn('account-setup.py show immich', text)
        # Navidrome uses the separate listener account on the phone, and says what it is.
        music = '\n'.join(phone_guide.instructions({}, 'navidrome', LINKS['navidrome']))
        self.assertIn('show navidrome_listener', music)
        self.assertIn('made for playing music', music)
        # Not provisioned yet: still tells you how to see it.
        self.assertIn('account-setup.py show seerr', '\n'.join(phone_guide.instructions({}, 'seerr', LINKS['seerr'])))

    def test_the_login_comes_before_the_sign_in_step(self):
        """D16: the username and how to see the password were the last lines, after
        "Sign in with your ... password"; they now come just before it."""
        for app in phone_guide.LOGIN:
            with self.subTest(app=app):
                steps = phone_guide.instructions({}, app, LINKS[app])
                login = next(n for n, s in enumerate(steps) if 'account-setup.py show' in s)
                sign_in = next(n for n, s in enumerate(steps) if 'sign in' in s.lower())
                self.assertEqual(login + 1, sign_in)
                self.assertFalse(any('sudo cat' in s for s in steps))

    def test_the_show_command_is_the_copy_kept_on_the_ssd(self):
        """It must still work after the downloaded folder is deleted, and a folder name
        with a space must stay one argument when pasted."""
        command = phone_guide.show_command({'data_mount': '/srv/my ssd'}, 'jellyfin')
        self.assertEqual(
            command,
            "sudo python3 '/srv/my ssd/installer/releases/v" + phone_guide.VERSION + "/account-setup.py' show jellyfin",
        )
        self.assertTrue((ROOT / phone_guide.SHOW_TOOL).is_file())

    def test_page_lists_phone_apps_apart_from_the_numbered_steps(self):
        page = phone_guide.page_html({}, {'jellyfin': LINKS['jellyfin']}, qr=lambda text: '')
        self.assertNotIn('<li>iPhone:', page)
        self.assertIn('<p>Phone app: iPhone: Jellyfin (App Store)', page)
        self.assertIn('Username: neko_ab12', page)
        self.assertIn('account-setup.py show jellyfin', page)
        self.assertNotIn('sudo cat', page)

    def test_page_has_tailscale_first_then_each_app_with_qr(self):
        page = phone_guide.page_html({}, LINKS, qr=lambda text: '<svg data-for="' + text + '"></svg>')
        self.assertLess(page.index('Tailscale'), page.index('Jellyfin'))
        for url in [phone_guide.TAILSCALE_DOWNLOAD, *LINKS.values()]:
            self.assertIn('data-for="' + url + '"', page)
        self.assertNotIn('SECRET-PW', page)
        self.assertIn('name="viewport"', page)

    def test_page_works_without_qrencode(self):
        page = phone_guide.page_html({}, {'jellyfin': LINKS['jellyfin']}, qr=lambda text: '')
        self.assertIn(LINKS['jellyfin'], page)
        self.assertNotIn('class="qr"', page)

    def test_page_escapes_addresses(self):
        page = phone_guide.page_html({}, {'jellyfin': 'https://x/"><script>'}, qr=lambda text: '')
        self.assertNotIn('<script>', page)

    def test_write_page_is_readable_by_the_desktop_user(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(phone_guide, 'ensure_qrencode'):
            root = Path(tmp) / 'pages'
            path = phone_guide.write_page({}, LINKS, root=root)
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            self.assertEqual(root.stat().st_mode & 0o777, 0o755)
            self.assertTrue(guided_setup.valid_page(path, root=root))


class LocalPageRuleTests(unittest.TestCase):
    def test_only_our_own_html_pages_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = root / 'phone-setup.html'
            good.write_text('<p>ok</p>')
            (root / 'sub').mkdir()
            (root / 'sub/x.html').write_text('x')
            (root / 'notes.txt').write_text('x')
            (root / 'link.html').symlink_to(good)
            self.assertTrue(guided_setup.valid_page(good, root=root))
            for bad in (
                root / 'sub/x.html',
                root / 'notes.txt',
                root / 'link.html',
                root / 'missing.html',
                'relative.html',
                Path('/etc/passwd'),
            ):
                with self.subTest(bad=bad):
                    self.assertFalse(guided_setup.valid_page(bad, root=root))


if __name__ == '__main__':
    unittest.main()
