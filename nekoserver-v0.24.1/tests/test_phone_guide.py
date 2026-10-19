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
USERS = {'jellyfin': {'username': 'neko_ab12'}, 'navidrome_listener': {'username': 'listener'},
         'immich': {'username': 'me@example.org', 'password': 'SECRET-PW'}}


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
        self.assertIn('sudo cat', text)
        # Navidrome uses the separate listener account on the phone.
        self.assertIn('listener', '\n'.join(phone_guide.instructions({}, 'navidrome', LINKS['navidrome'])))
        # Not provisioned yet: still tells you where to look.
        self.assertIn('see the password list', '\n'.join(phone_guide.instructions({}, 'seerr', LINKS['seerr'])))

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
            for bad in (root / 'sub/x.html', root / 'notes.txt', root / 'link.html',
                        root / 'missing.html', 'relative.html', Path('/etc/passwd')):
                with self.subTest(bad=bad):
                    self.assertFalse(guided_setup.valid_page(bad, root=root))


if __name__ == '__main__':
    unittest.main()
