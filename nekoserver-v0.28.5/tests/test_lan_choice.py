"""LAN folder sharing (Samba) and the LAN HTTPS gateway share one home-network choice,
so a fresh setup with automatic LAN settings finishes Samba on the first pass."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import network_access  # noqa: E402
from modules import samba  # noqa: E402

ROWS = [{'interface': 'enp3s0', 'ip': '192.168.1.20', 'cidr': '192.168.1.0/24'},
        {'interface': 'wlan0', 'ip': '10.0.0.5', 'cidr': '10.0.0.0/24'}]


class LanChoiceTests(unittest.TestCase):
    def ctx(self, **gateway):
        saved = []
        ctx = SimpleNamespace(config={'gateway': dict(gateway)}, log=print, apply=True)
        ctx.persist_config = lambda: saved.append(dict(ctx.config['gateway']))
        return ctx, saved

    def test_the_choice_is_asked_once_and_saved(self):
        ctx, saved = self.ctx(lan_ip='auto', lan_cidr='auto')
        with mock.patch.object(network_access, 'candidates', return_value=ROWS):
            self.assertTrue(network_access.choose_lan(ctx, ask=lambda _: '1', interactive=True))
        self.assertEqual(ctx.config['gateway'], {'lan_ip': '192.168.1.20', 'lan_cidr': '192.168.1.0/24'})
        self.assertEqual(saved, [ctx.config['gateway']])
        # The next step (the gateway after Samba, say) uses the answer without asking.
        never = mock.Mock(side_effect=AssertionError('asked twice'))
        self.assertTrue(network_access.choose_lan(ctx, ask=never, interactive=True))

    def test_without_anyone_to_choose_nothing_is_guessed(self):
        ctx, saved = self.ctx(lan_ip='auto', lan_cidr='auto')
        self.assertFalse(network_access.choose_lan(ctx, interactive=False))
        self.assertEqual((ctx.config['gateway']['lan_ip'], saved), ('auto', []))
        ctx, _ = self.ctx(lan_ip='auto', lan_cidr='auto')
        with mock.patch.object(network_access, 'candidates', return_value=ROWS), \
                self.assertRaisesRegex(RuntimeError, 'Invalid LAN selection'):
            network_access.choose_lan(ctx, ask=lambda _: '7', interactive=True)

    def test_samba_chooses_the_lan_itself_instead_of_waiting_for_the_gateway(self):
        ctx, _ = self.ctx(lan_ip='auto', lan_cidr='auto')
        ctx.config.update(networking={'share_mode': 'lan', 'shares': {'Media': '.'}}, media_mount='/nonexistent')
        asked = []

        def choose(context):
            asked.append(context)
            context.config['gateway'].update(lan_ip='192.168.1.20', lan_cidr='192.168.1.0/24')
            return True
        with mock.patch.object(samba.os, 'geteuid', return_value=0), \
                mock.patch.object(samba.host, 'current'), \
                mock.patch.object(samba.network_access, 'choose_lan', side_effect=choose), \
                mock.patch.object(samba.network_access, 'verify_lan') as verified, \
                mock.patch.dict(samba.os.environ, {'SUDO_USER': 'root'}), \
                self.assertRaisesRegex(RuntimeError, 'real desktop account'):
            samba.run(ctx)   # gets past the LAN step, to the next check
        self.assertEqual(asked, [ctx])
        verified.assert_called_once_with({'gateway': {'share_mode': 'lan', 'shares': {'Media': '.'},
                                                      'lan_ip': '192.168.1.20', 'lan_cidr': '192.168.1.0/24'}})
        # With nobody to choose, Samba says how to finish instead of pointing at the gateway.
        ctx, _ = self.ctx(lan_ip='auto', lan_cidr='auto')
        ctx.config.update(networking={'share_mode': 'lan', 'shares': {'Media': '.'}})
        with mock.patch.object(samba.os, 'geteuid', return_value=0), \
                mock.patch.object(samba.host, 'current'), \
                mock.patch.object(samba.network_access, 'choose_lan', return_value=False):
            result = samba.run(ctx)
        self.assertEqual(result['status'], 'needs_setup')
        self.assertNotIn('gateway_lan', result['reason'])


if __name__ == '__main__':
    unittest.main()
