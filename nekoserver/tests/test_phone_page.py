"""0.50.0, the wizard (P08): the phone page shows what setup checked itself (this server
signed in, the private HTTPS addresses, a phone in the account, each app) above the steps.
A row is passed, not yet, or not known, and not known is never shown as passed. A phone
seen in the account ticks the "Tailscale on the phone" step without anyone's word; that
an app opened with Wi-Fi off stays the person's own observation.

The checks themselves are phone_status's (tests/test_phone_status.py)."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import guided_setup  # noqa: E402
import phone_status  # noqa: E402
from modules import gateway_tailnet  # noqa: E402

LINKS = {'jellyfin': 'https://neko.example.ts.net:8443/'}


def row(ident, status, reason, next_step='', **more):
    return {'id': ident, 'status': status, 'reason': reason, 'next': next_step, **more}


CHECKED = [
    row('server', 'pass', 'This server is signed in to Tailscale.'),
    row('https', 'unknown', 'No app address is switched on yet, so no certificate could be checked.', 'Check again.'),
    row('phone', 'fail', 'No phone is signed in to your Tailscale account yet.', 'Install Tailscale on the phone.'),
    row('app:jellyfin', 'pass', 'Jellyfin: login verified, address on and answering.', title='Jellyfin'),
]


class PhonePageTests(unittest.TestCase):
    def steps(self, checked, part=None, done=()):
        logs, calls = [], []

        def recorded(ctx, title, steps, question, **more):
            calls.append((title, [step['id'] for step in steps], more))
            return part(steps) if part else set()

        ctx = SimpleNamespace(config={'data_mount': '/srv/nekoserver'}, log=logs.append)
        with (
            mock.patch.object(phone_status, 'check', return_value={'rows': checked}),
            mock.patch.object(gateway_tailnet.phone_guide, 'write_page', return_value=Path('/tmp/page.html')),
            mock.patch.object(gateway_tailnet.phone_guide, 'instructions', return_value=['Open the app.']),
            mock.patch.object(gateway_tailnet.phone_guide, 'login_of', return_value='jellyfin'),
            mock.patch.object(guided_setup, 'part', recorded),
            mock.patch.object(gateway_tailnet.gateway_tools, 'selected', return_value=['jellyfin']),
            mock.patch.object(phone_status, 'record_observation') as kept,
        ):
            confirmed = gateway_tailnet.phone_steps(ctx, {'links': LINKS}, done)
        return confirmed, calls[0], logs, kept

    def test_the_rows_setup_checked_are_shown_above_the_steps(self):
        _, (title, ids, more), _, _ = self.steps(CHECKED)
        self.assertEqual(title, 'Use your apps away from home')
        self.assertEqual(ids, ['tailscale', 'jellyfin', 'away_from_home'])
        status = more['status']
        self.assertEqual(
            [(line['title'], line['status']) for line in status],
            [
                ('This server is signed in to Tailscale', 'pass'),
                ('Private HTTPS addresses', 'unknown'),
                ('A phone in your Tailscale account', 'fail'),
                ('Jellyfin', 'pass'),
            ],
        )
        # The terminal lists the same rows; not known is said as not known, never as OK.
        said = more['intro']
        self.assertIn('What setup checked itself:', said)
        self.assertIn('  [OK] This server is signed in to Tailscale: This server is signed in to Tailscale.', said)
        self.assertTrue(any(line.startswith('  [NOT KNOWN] Private HTTPS addresses:') for line in said))
        self.assertIn('        Install Tailscale on the phone.', said)
        # No phone seen: its step is still to do.
        self.assertEqual(list(more['done']), [])

    def test_a_phone_seen_in_the_account_ticks_its_step_and_nobody_is_asked(self):
        seen = [
            *CHECKED[:2],
            row('phone', 'pass', 'A phone is signed in.', shows='It does not show that the phone opened an app.'),
        ]
        _, (_, _, more), logs, _ = self.steps(seen)
        self.assertEqual(list(more['done']), ['tailscale'])
        self.assertTrue(any('seen by setup, not asked' in line for line in logs))
        # What that row does and does not show is under it.
        self.assertIn('        It does not show that the phone opened an app.', more['intro'])

    def test_away_from_home_is_the_persons_own_word_and_is_kept_as_that(self):
        confirmed, _, _, kept = self.steps(CHECKED, part=lambda steps: {step['id'] for step in steps})
        self.assertIn('away_from_home', confirmed)
        kept.assert_called_once()
        self.assertEqual(kept.call_args.args[1:], (True, LINKS))
        # Not confirmed: nothing is recorded as observed.
        _, _, _, kept = self.steps(CHECKED, part=lambda steps: {'tailscale'})
        kept.assert_not_called()
        # The step says setup cannot see it, and makes no promise about "anywhere".
        source = (ROOT / 'modules/gateway_tailnet.py').read_text()
        self.assertIn('Setup cannot see this for you: only you can say that it worked.', source)
        self.assertNotIn('your apps work anywhere', source)

    def test_a_check_that_cannot_run_never_fails_the_step(self):
        logs = []
        ctx = SimpleNamespace(config={}, log=logs.append)
        with mock.patch.object(phone_status, 'check', side_effect=OSError('no reports folder')):
            self.assertEqual(gateway_tailnet.phone_rows(ctx), [])
        self.assertTrue(any('could not check the connection itself' in line for line in logs))
        self.assertEqual(gateway_tailnet.status_lines([]), [])


if __name__ == '__main__':
    unittest.main()
