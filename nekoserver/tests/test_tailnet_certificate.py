"""S03 for the Tailscale addresses: routes in place and HTTPS switched on for the tailnet
do not mean the addresses open. Without the certificate Tailscale fetches from Let's
Encrypt, every https://<name>.ts.net:<port>/ hangs at the TLS handshake and the phone
shows a blank page (found on the 0.48.2 release gate, where Let's Encrypt refused the
certificate: five already issued that week). Setup and the acceptance check then say so,
with Tailscale's own reason when it gives one, instead of ending as if all were well.

The status and log shapes below are those of the NekoDebian VM (Tailscale 1.104.5) while
the certificate was refused."""

import datetime as dt
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gateway_tools as g  # noqa: E402
from modules import gateway_tailnet  # noqa: E402

SECURE = gateway_tailnet.secure_addresses  # the step's own, also where a test replaces it

NAME = 'nekodebian-1.tail4772a4.ts.net'
LINKS = {
    'jellyfin': 'https://' + NAME + ':8443/',
    'immich': 'https://' + NAME + ':8445/',
    'dashboard': 'https://' + NAME + ':8448/',
}
STATUS = {
    'BackendState': 'Running',
    'Health': [],
    'CurrentTailnet': {'MagicDNSSuffix': 'tail4772a4.ts.net', 'MagicDNSEnabled': True},
    'CertDomains': [NAME],
    'Self': {'DNSName': NAME + '.', 'TailscaleIPs': ['100.67.0.21', 'fd7a:115c:a1e0::433:17'], 'Online': True},
}
FETCHING = {**STATUS, 'Health': ['Fetching TLS certificate via ACME for: ' + NAME]}
# journalctl -u tailscaled.service -o cat, as on the VM (two refusals, the later one last).
LOG = '\n'.join(
    [
        'cert("' + NAME + '"): getCertPEM: 429 urn:ietf:params:acme:error:rateLimited: too many certificates (5) '
        'already issued for this exact set of identifiers in the last 168h0m0s, retry after 2026-11-10 17:30:28 UTC: '
        'see https://letsencrypt.org/docs/rate-limits/#new-certificates-per-exact-set-of-identifiers',
        'health(warnable=tls-cert-pending): ok',
        'health(warnable=tls-cert-pending): error: Fetching TLS certificate via ACME for: ' + NAME,
        'cert("' + NAME + '"): already had ACME account.',
        'cert("' + NAME + '"): acme: using dns-01: Funnel is not enabled for ' + NAME + ':443',
        'cert("' + NAME + '"): getCertPEM: 429 urn:ietf:params:acme:error:rateLimited: too many certificates (5) '
        'already issued for this exact set of identifiers in the last 168h0m0s, retry after 2026-11-10 17:54:58 UTC: '
        'see https://letsencrypt.org/docs/rate-limits/#new-certificates-per-exact-set-of-identifiers',
        'health(warnable=tls-cert-pending): ok',
    ]
)
NOW = dt.datetime(2026, 11, 9, 18, 0, tzinfo=dt.timezone.utc)
START = (
    'Tailscale has not got the HTTPS certificate for ' + NAME + ' yet, so the app addresses on the phone do not open. '
)
END = 'Run  sudo bash install.sh  again later; it checks again.'


class Clock:
    """A mock clock: sleeping and each connection that times out move it on."""

    def __init__(self):
        self.now, self.slept = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class HttpsCheckTests(unittest.TestCase):
    def check(self, probe, status=STATUS, log='', clock=None, links=LINKS):
        clock = clock or Clock()
        with (
            mock.patch.object(g, 'tailscale_status', return_value=status),
            mock.patch.object(g, 'tailscale_log', return_value=log),
        ):
            return g.https_problem(links, sleep=clock.sleep, clock=clock, probe=probe, now=NOW), clock

    def test_an_address_that_answers_at_once_passes_without_waiting(self):
        probe = mock.Mock(return_value=True)
        problem, clock = self.check(probe)
        self.assertIsNone(problem)
        self.assertEqual(clock.slept, [])
        # The dashboard's route, over this PC's own Tailscale address (its DNS does not matter).
        probe.assert_called_once_with(NAME, g.DASHBOARD_PORT, '100.67.0.21')

    def test_without_the_dashboard_the_first_app_route_is_tried(self):
        probe = mock.Mock(return_value=True)
        self.check(probe, links={k: v for k, v in LINKS.items() if k != 'dashboard'})
        probe.assert_called_once_with(NAME, 8443, '100.67.0.21')

    def test_a_certificate_that_arrives_after_a_few_tries_passes(self):
        probe = mock.Mock(side_effect=[False, False, True])
        problem, clock = self.check(probe)
        self.assertIsNone(problem)
        self.assertEqual(clock.slept, [g.CERT_PAUSE, g.CERT_PAUSE])

    def test_a_certificate_that_never_arrives_gives_up_at_the_limit_and_says_why(self):
        clock = Clock()

        def times_out(name, port, address):
            clock.now += g.CERT_TRY
            return False

        problem, _ = self.check(times_out, log=LOG, clock=clock)
        self.assertEqual(
            problem,
            START + "Let's Encrypt gives a machine name at most 5 certificates a week; "
            'it allows the next one after 2026-11-10 17:54:58 UTC. ' + END,
        )
        # Bounded: tried until the wait was over, then no more (the last try may run its time).
        self.assertGreaterEqual(clock.now, g.CERT_WAIT)
        self.assertLessEqual(clock.now, g.CERT_WAIT + g.CERT_TRY)
        self.assertEqual(len(clock.slept), 4)

    def test_without_a_reason_from_tailscale_the_why_is_left_out(self):
        problem, _ = self.check(mock.Mock(return_value=False))
        self.assertEqual(problem, START + END)

    def test_tailscales_health_message_is_the_why_when_its_log_names_no_limit(self):
        problem, _ = self.check(mock.Mock(return_value=False), status=FETCHING)
        self.assertEqual(problem, START + 'Tailscale says: Fetching TLS certificate via ACME for: ' + NAME + '. ' + END)

    def test_a_limit_that_has_already_ended_is_not_named(self):
        later = NOW.replace(day=11)
        self.assertEqual(g.certificate_why(NAME, STATUS, LOG, later), '')
        # Another machine's refusal is not this one's.
        self.assertEqual(g.certificate_why('other.tail4772a4.ts.net', STATUS, LOG, NOW), '')
        # Health messages about something else are no reason either.
        self.assertEqual(g.certificate_why(NAME, {'Health': ['Tailscale could not connect to DERP']}, '', NOW), '')

    def test_an_address_nothing_listens_on_does_not_answer(self):
        with socket.socket() as spare:
            spare.bind(('127.0.0.1', 0))
            port = spare.getsockname()[1]
        self.assertFalse(g.https_answers('localhost', port, '127.0.0.1', timeout=2))

    def test_no_links_nothing_to_check(self):
        probe = mock.Mock()
        self.assertIsNone(self.check(probe, links={})[0])
        probe.assert_not_called()


class SetupStepTests(unittest.TestCase):
    """The Tailscale step itself: NEEDS SETUP with the reason, nothing else changed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        (self.data / 'reports').mkdir()
        self.ctx = mock.Mock(config={'data_mount': str(self.data)}, apply=True)
        self.routes = {'format': 'neko-gateway-v1', 'status': 'configured', 'links': LINKS}

    def secure(self, problem, interactive):
        def apply(config):
            # As _apply_tailnet does: the report is written with the routes.
            g.atomic_json(gateway_tailnet.report_path(config), {**self.routes, 'client_acceptance': 'needs_setup'})
            return dict(self.routes)

        with (
            mock.patch.object(gateway_tailnet.guided_setup, 'interactive', return_value=interactive),
            mock.patch.object(g, 'https_ready', return_value=True),
            mock.patch.object(g, 'apply_tailnet', side_effect=apply),
            mock.patch.object(g, 'https_problem', return_value=problem) as check,
        ):
            return SECURE(self.ctx), check

    def test_a_missing_certificate_ends_the_step_as_needs_setup_with_the_reason(self):
        for interactive in (True, False):
            with self.subTest(interactive=interactive):
                self.ctx.log.reset_mock()
                result, check = self.secure(START + END, interactive)
                self.assertEqual(result, {'status': 'needs_setup', 'reason': START + END})
                check.assert_called_once_with(LINKS)
                # Not "already on; secure addresses updated" as if all were well.
                self.ctx.log.assert_not_called()

    def test_answering_addresses_keep_the_step_as_it_was(self):
        result, _ = self.secure(None, True)
        self.assertEqual(result, self.routes)
        self.ctx.log.assert_called_once_with('Tailscale: MagicDNS and HTTPS already on; secure addresses updated')

    def test_a_phone_confirmed_before_stays_confirmed_while_the_certificate_is_awaited(self):
        g.atomic_json(
            gateway_tailnet.report_path(self.ctx.config), {**self.routes, 'client_acceptance': 'user_confirmed'}
        )
        with (
            mock.patch.object(g, 'selected'),
            mock.patch.object(g, 'configure', return_value=True),
            mock.patch.object(
                gateway_tailnet, 'secure_addresses', side_effect=lambda ctx: self.secure(START + END, False)[0]
            ),
        ):
            result = gateway_tailnet.run(self.ctx)
        self.assertEqual(result, {'status': 'needs_setup', 'reason': START + END})
        self.assertEqual(gateway_tailnet.confirmed_links(self.ctx.config), LINKS)


class AcceptanceTests(unittest.TestCase):
    def test_matching_routes_whose_addresses_do_not_answer_are_not_a_pass(self):
        import gateway_checks

        cfg = {'data_mount': '/nonexistent'}
        routes = g.desired(NAME, ['jellyfin'])
        journal = {'format': 'neko-tailnet-owned-v1', 'host': NAME, 'status': 'configured', 'owned': routes}
        for problem, status, detail in (
            (START + END, 'needs_setup', START + END),
            (None, 'pass', 'Private owned HTTPS routes must match selected apps; Funnel is forbidden.'),
        ):
            with (
                self.subTest(status=status),
                mock.patch.object(g, 'tailnode', return_value=NAME),
                mock.patch.object(g, 'tailnet_routes', return_value=routes),
                mock.patch.object(g, 'serve_state', return_value=routes),
                mock.patch.object(gateway_checks.health, 'read_json', return_value=journal),
                mock.patch.object(g, 'https_problem', return_value=problem) as check,
            ):
                row = gateway_checks.check(cfg, 'tailnet')[0]
                self.assertEqual((row['status'], row['detail']), (status, detail))
                # The same check as setup's, one try (no wait) on the routes' addresses.
                check.assert_called_once_with({'8443': 'https://' + NAME + ':8443/'}, wait=0)

    def test_routes_that_do_not_match_fail_without_a_connection(self):
        import gateway_checks

        routes = g.desired(NAME, ['jellyfin'])
        journal = {'format': 'neko-tailnet-owned-v1', 'host': NAME, 'status': 'configured', 'owned': routes}
        with (
            mock.patch.object(g, 'tailnode', return_value=NAME),
            mock.patch.object(g, 'tailnet_routes', return_value=routes),
            mock.patch.object(g, 'serve_state', return_value={'TCP': {}, 'Web': {}}),
            mock.patch.object(gateway_checks.health, 'read_json', return_value=journal),
            mock.patch.object(g, 'https_problem') as check,
        ):
            self.assertEqual(gateway_checks.check({'data_mount': '/nonexistent'}, 'tailnet')[0]['status'], 'fail')
        check.assert_not_called()


if __name__ == '__main__':
    unittest.main()
