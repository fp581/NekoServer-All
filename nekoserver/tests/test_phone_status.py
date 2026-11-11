"""The status rows of the "connect your phone" page (wizard specification P08; checks
T19, T22, T23, T24): each is pass, fail or unknown from what Tailscale itself says, and
unknown is never shown as pass. Tailscale is a fake here: its answers are made up below,
no tailnet is joined and no certificate is asked for.
"""

import contextlib
import io
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import account_store  # noqa: E402
import gateway_tools  # noqa: E402
import phone_status  # noqa: E402
from modules import gateway_tailnet  # noqa: E402

# A made-up server: its name and addresses are what must never reach a log or a reason.
NAME = 'nekotest.tail' + 'a1b2c3' + '.ts.net'
OWN_ADDRESS = '100.' + '101.102.103'
APPS = ['jellyfin', 'immich']
PORT = {app: str(gateway_tools.APPS[app][2]) for app in APPS}


def tailscale(state='Running', magic=True, certificates=True, peers=None, **own):
    """What `tailscale status --json` says, as a dict."""
    status = {
        'BackendState': state,
        'Self': {'DNSName': NAME + '.', 'TailscaleIPs': [OWN_ADDRESS], 'UserID': 7, 'Online': True, **own},
        'CurrentTailnet': {'Name': 'someone@example.org', 'MagicDNSEnabled': magic},
        'CertDomains': [NAME] if certificates else None,
        'Health': [],
    }
    if peers is not None:
        status['Peer'] = peers
    return status


def phone(system='android', online=True, user=7):
    return {'HostName': 'private-phone-name', 'OS': system, 'Online': online, 'UserID': user}


def serve(ports):
    """What `tailscale serve status --json` says for private HTTPS routes on these ports."""
    return {
        'TCP': {port: {'HTTPS': True} for port in ports},
        'Web': {f'{NAME}:{port}': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:1'}}} for port in ports},
    }


class FakeTailscale:
    """Stands in for the tailscale program: answers the two commands that only read, and
    fails the test at any other."""

    def __init__(self, status, routes=None):
        self.status, self.routes, self.asked = status, routes, []

    def __call__(self, args, timeout=30):
        self.asked.append(args)
        answer = {
            ('tailscale', 'status', '--json'): self.status,
            ('tailscale', 'serve', 'status', '--json'): self.routes,
        }[tuple(args)]
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, str) else json.dumps(answer)


def status_of(text_or_dict):
    return phone_status.read_status(FakeTailscale(text_or_dict))


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.config = {'data_mount': folder.name}
        self.logins = {app: {'state': 'verified'} for app in APPS}
        self.accounts_on = True
        for patch in (
            mock.patch.object(gateway_tools, 'selected', lambda config: list(APPS)),
            mock.patch.object(account_store, 'enabled', lambda config: self.accounts_on),
            mock.patch.object(account_store, 'get', lambda config, app, verified=False: self.logins.get(app)),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def addresses(self):
        return {app: f'https://{NAME}:{PORT[app]}/' for app in APPS}


class ReadingTailscaleTests(Base):
    def test_only_a_proper_answer_is_taken(self):
        self.assertEqual(status_of(tailscale())['BackendState'], 'Running')
        for bad in ('', 'not json', '[]', '"Running"', '{}', '{"BackendState": 5}', RuntimeError('no tailscale')):
            self.assertIsNone(status_of(bad), bad)

    def test_the_routes_are_read_and_a_funnel_or_garbage_is_not_trusted(self):
        read = lambda answer: phone_status.read_routes(FakeTailscale(tailscale(), answer))  # noqa: E731
        self.assertEqual(read(serve(['8443', '8444'])), {'8443', '8444'})
        self.assertEqual(read({}), set())
        funnel = {**serve(['8443']), 'AllowFunnel': {NAME + ':8443': True}}
        for bad in ('nonsense', '[]', funnel, RuntimeError('refused'), {'TCP': 'x', 'Web': {}}):
            self.assertIsNone(read(bad), bad)

    def test_the_machine_name_is_only_ever_a_tailscale_name(self):
        self.assertEqual(phone_status.machine_name(tailscale()), NAME)
        for bad in ('', 'localhost.', 'evil.example.org.', 5, None, NAME + '/x'):
            self.assertEqual(phone_status.machine_name(tailscale(DNSName=bad)), '', bad)
        self.assertEqual(phone_status.machine_name({'BackendState': 'Running', 'Self': 'x'}), '')


class ServerRowTests(Base):
    def test_each_state_of_tailscale_has_its_own_row(self):
        for state, expected, said in (
            ('Running', 'pass', 'is signed in'),
            ('NeedsLogin', 'fail', 'not signed in'),
            ('NeedsMachineAuth', 'fail', 'approve'),
            ('Stopped', 'fail', 'switched off'),
            ('Starting', 'unknown', 'still starting'),
            ('NoState', 'unknown', 'still starting'),
            ('SomethingNew', 'unknown', 'does not know'),
        ):
            found = phone_status.server_signed_in(status_of(tailscale(state)))
            self.assertEqual((found['id'], found['status']), ('server', expected), state)
            self.assertIn(said, found['reason'] + found['next'], state)
            self.assertEqual(bool(found['next']), expected != 'pass', state)  # a way on for every other row

    def test_no_answer_from_tailscale_is_unknown_and_never_pass(self):
        for bad in ('garbage', RuntimeError('not installed'), '{}'):
            found = phone_status.server_signed_in(status_of(bad))
            self.assertEqual(found['status'], 'unknown')
            self.assertIn('could not ask Tailscale', found['reason'])
        running_without_self = {'BackendState': 'Running'}
        self.assertEqual(phone_status.server_signed_in(status_of(running_without_self))['status'], 'unknown')


class HttpsRowTests(Base):
    def check(self, status, routes=('all',), answers=True, why=lambda: ''):
        routes = set(PORT.values()) if routes == ('all',) else routes
        self.probed = []

        def probe(name, port, address, timeout):
            self.probed.append((name, port, address))
            return answers if isinstance(answers, bool) else answers[str(port)]

        return phone_status.private_https(status_of(status), self.addresses(), routes, probe, why)

    def test_everything_in_place_and_answering_is_pass(self):
        found = self.check(tailscale())
        self.assertEqual(found['status'], 'pass')
        self.assertEqual(
            (found['name_service'], found['certificates_allowed'], found['certificate']), ('pass', 'pass', 'pass')
        )
        self.assertEqual(found['answers'], {'jellyfin': 'pass', 'immich': 'pass'})
        # Each address is asked through this PC's own Tailscale address, by its real name.
        self.assertEqual(self.probed, [(NAME, int(PORT[app]), OWN_ADDRESS) for app in APPS])

    def test_not_signed_in_is_unknown_and_nothing_is_tried(self):
        for status in (tailscale('NeedsLogin'), 'garbage'):
            found = self.check(status)
            self.assertEqual((found['status'], found['answers'], self.probed), ('unknown', {}, []))

    def test_magicdns_off_and_https_off_each_say_what_to_switch_on(self):
        off = self.check(tailscale(magic=False))
        self.assertEqual((off['status'], off['name_service']), ('fail', 'fail'))
        self.assertIn('Enable MagicDNS', off['next'])
        unsaid = self.check(tailscale(magic=None))
        self.assertEqual((unsaid['status'], unsaid['name_service']), ('unknown', 'unknown'))
        no_https = self.check(tailscale(certificates=False))
        self.assertEqual((no_https['status'], no_https['certificates_allowed']), ('fail', 'fail'))
        self.assertIn('Enable HTTPS', no_https['next'])
        self.assertIn('public list of certificates', no_https['next'])  # said before it is switched on
        self.assertEqual(self.probed, [])  # a box ticked there is not a certificate: nothing claimed, nothing tried

    def test_switched_on_in_the_account_is_not_a_certificate(self):
        found = self.check(tailscale(), answers=False, why=lambda: 'Tailscale says: five this week.')
        self.assertEqual(
            (found['status'], found['certificates_allowed'], found['certificate']), ('fail', 'pass', 'fail')
        )
        self.assertIn('has not got the HTTPS certificate', found['reason'])
        self.assertIn('five this week', found['reason'])

    def test_one_app_that_does_not_answer_is_named(self):
        found = self.check(tailscale(), answers={PORT['jellyfin']: True, PORT['immich']: False})
        self.assertEqual((found['status'], found['certificate']), ('fail', 'pass'))
        self.assertEqual(found['answers'], {'jellyfin': 'pass', 'immich': 'fail'})
        self.assertIn(gateway_tools.APPS['immich'][0], found['reason'])

    def test_an_address_without_a_route_is_never_tried_and_never_pass(self):
        none_on = self.check(tailscale(), routes=set())
        self.assertEqual((none_on['status'], none_on['certificate'], self.probed), ('unknown', 'unknown', []))
        unreadable = self.check(tailscale(), routes=None)
        self.assertEqual((unreadable['status'], self.probed), ('unknown', []))
        one_on = self.check(tailscale(), routes={PORT['jellyfin']})
        self.assertEqual(one_on['status'], 'unknown')
        self.assertEqual(one_on['answers'], {'jellyfin': 'pass', 'immich': 'unknown'})
        self.assertEqual([port for _, port, _ in self.probed], [int(PORT['jellyfin'])])

    def test_the_addresses_are_the_ones_the_routes_step_writes(self):
        found = phone_status.app_addresses(self.config, status_of(tailscale()))
        self.assertEqual(found, self.addresses())
        routes = gateway_tools.desired(NAME, APPS)
        self.assertEqual(sorted(found.values()), sorted(gateway_tools.tailnet_links(NAME, routes).values()))
        self.assertEqual(phone_status.app_addresses(self.config, status_of(tailscale(DNSName='x'))), {})


class PhoneRowTests(Base):
    def row(self, peers):
        return phone_status.phone_in_tailnet(status_of(tailscale(peers=peers)))

    def test_a_phone_signed_in_is_seen_connected_or_not(self):
        on = self.row({'k1': phone('android'), 'k2': {'OS': 'linux', 'Online': True, 'UserID': 7}})
        self.assertEqual((on['status'], on['phones'], on['connected_now'], on['same_account']), ('pass', 1, 1, 1))
        self.assertIn('connected right now', on['reason'])
        off = self.row({'k1': phone('iOS', online=False)})
        self.assertEqual((off['status'], off['phones'], off['connected_now']), ('pass', 1, 0))
        self.assertIn('not connected at this moment', off['reason'])
        self.assertIn('switch Tailscale on', off['next'])
        other = self.row({'k1': phone('android', user=9)})
        self.assertEqual((other['status'], other['same_account']), ('pass', 0))
        self.assertIn('another person', other['next'])

    def test_no_phone_is_fail_and_no_answer_is_unknown(self):
        for peers in ({}, None, {'k': {'OS': 'windows', 'Online': True}}, {'k': 'x'}, {'k': {'OS': 5}}):
            found = self.row(peers)
            self.assertEqual((found['status'], found['phones']), ('fail', 0), peers)
            self.assertIn('SAME account', found['next'])
        self.assertEqual(self.row(['a list'])['status'], 'unknown')
        for status in (tailscale('NeedsLogin', peers={'k': phone()}), 'garbage'):
            self.assertEqual(phone_status.phone_in_tailnet(status_of(status))['status'], 'unknown')

    def test_it_never_claims_the_phone_opened_an_app_or_was_on_mobile_data(self):
        """T24."""
        for found in (self.row({'k': phone()}), self.row({'k': phone(online=False)}), self.row({})):
            self.assertIn('does not show that the phone opened an app', found['shows'])
            self.assertIn('cannot see whether a phone is on Wi-Fi or on mobile data', found['shows'])
            for claim in ('mobile data', 'Wi-Fi', 'away from home', 'opened'):
                self.assertNotIn(claim, found['reason'] + found['next'])


class AppRowTests(Base):
    def rows(self, routes=('all',), answers=None, status=None):
        routes = set(PORT.values()) if routes == ('all',) else routes
        found = phone_status.app_rows(self.config, status_of(status or tailscale()), routes, answers)
        return {one['app']: one for one in found}

    def test_verified_login_and_route_on_is_pass_with_its_address(self):
        found = self.rows(answers={'jellyfin': 'pass', 'immich': 'pass'})
        self.assertEqual(list(found), APPS)
        for app in APPS:
            one = found[app]
            self.assertEqual(
                (one['id'], one['status'], one['login'], one['route']), ('app:' + app, 'pass', 'pass', 'on')
            )
            self.assertEqual((one['address'], one['route_reason']), (self.addresses()[app], ''))
            self.assertEqual(one['title'], gateway_tools.APPS[app][0])
            self.assertTrue(phone_status.route_may_open(one))

    def test_a_route_stays_off_with_the_reason_while_the_login_is_not_verified(self):
        """T19: unknown and failed are both not safe."""
        self.logins['immich'] = {'state': 'failed'}
        failed = self.rows(routes={PORT['jellyfin']})['immich']
        self.assertEqual((failed['status'], failed['login'], failed['route']), ('fail', 'fail', 'off'))
        self.assertIn('could not sign in', failed['route_reason'])
        self.assertIn('stays off until its login is verified', failed['route_reason'])
        self.assertFalse(phone_status.route_may_open(failed))
        del self.logins['immich']
        missing = self.rows(routes={PORT['jellyfin']})['immich']
        self.assertEqual((missing['status'], missing['login']), ('unknown', 'unknown'))
        self.assertFalse(phone_status.route_may_open(missing))
        self.accounts_on = False
        for one in self.rows(routes=set()).values():
            self.assertEqual((one['status'], one['login'], one['route']), ('unknown', 'unknown', 'off'))
            self.assertIn('does not keep the logins', one['route_reason'])
            self.assertFalse(phone_status.route_may_open(one))

    def test_a_login_that_cannot_be_read_is_unknown(self):
        with mock.patch.object(account_store, 'get', side_effect=RuntimeError('store damaged')):
            for one in self.rows().values():
                self.assertEqual((one['status'], one['login']), ('unknown', 'unknown'))

    def test_an_unverified_app_whose_route_is_on_is_not_called_fine(self):
        self.logins['immich'] = {'state': 'failed'}
        found = self.rows()['immich']
        self.assertEqual((found['status'], found['route']), ('fail', 'on'))
        self.assertIn('on from an earlier run', found['reason'])

    def test_route_off_unreadable_or_silent_is_never_pass(self):
        self.assertEqual(self.rows(routes=set())['jellyfin']['status'], 'fail')
        self.assertIn('not switched on yet', self.rows(routes=set())['jellyfin']['route_reason'])
        unreadable = self.rows(routes=None)['jellyfin']
        self.assertEqual((unreadable['status'], unreadable['route']), ('unknown', 'unknown'))
        silent = self.rows(answers={'jellyfin': 'fail', 'immich': 'pass'})
        self.assertEqual((silent['jellyfin']['status'], silent['immich']['status']), ('fail', 'pass'))

    def test_without_a_tailscale_name_there_is_no_address_to_show(self):
        found = self.rows(routes=None, status=tailscale('NeedsLogin', DNSName=''))
        self.assertEqual([one['address'] for one in found.values()], ['', ''])


class KeptRowsTests(Base):
    def run_check(self, status=None, routes=('all',), answers=True, again=True, now=1000.0):
        routes = serve(sorted(PORT.values())) if routes == ('all',) else routes
        self.tailscale = FakeTailscale(status or tailscale(peers={'k': phone()}), routes)
        self.probed = []

        def probe(name, port, address, timeout):
            self.probed.append(port)
            return answers

        return phone_status.check(self.config, self.tailscale, probe, lambda: '', now, again)

    def test_the_whole_page_in_order_with_what_is_left(self):
        found = self.run_check()
        self.assertEqual(
            [one['id'] for one in found['rows']], ['server', 'https', 'phone', 'app:jellyfin', 'app:immich']
        )
        self.assertEqual(found['unresolved'], [])
        self.assertEqual(found['observation']['status'], 'not_tested')
        self.assertEqual(
            sorted(tuple(args) for args in self.tailscale.asked),
            [('tailscale', 'serve', 'status', '--json'), ('tailscale', 'status', '--json')],
        )  # it only reads
        broken = self.run_check(status=tailscale('NeedsLogin'))
        self.assertEqual(broken['unresolved'], ['server', 'https', 'phone', 'app:jellyfin', 'app:immich'])
        self.assertEqual(len(self.tailscale.asked), 1)  # not signed in: the routes are not even asked for

    def test_the_rows_are_kept_and_shown_again_without_a_new_connection(self):
        """T23: the page opened again after a skip or a restart."""
        self.run_check(now=1000.0)
        self.assertEqual(len(self.probed), 2)
        kept = phone_status.remembered(self.config, self.addresses())
        self.assertEqual(sorted(kept), ['app:immich', 'app:jellyfin', 'https', 'phone', 'server'])
        self.assertEqual({one['checked_at'] for one in kept.values()}, {1000.0})
        again = self.run_check(again=False, now=2000.0)
        self.assertEqual(self.probed, [])  # the HTTPS row is the kept one
        https = again['rows'][1]
        self.assertEqual((https['status'], https['kept'], https['checked_at']), ('pass', True, 1000.0))
        self.assertEqual(again['unresolved'], [])
        # The visible retest checks everything again.
        self.run_check(again=True, now=3000.0)
        self.assertEqual(len(self.probed), 2)

    def test_a_kept_pass_never_covers_what_changed_or_fails_now(self):
        self.run_check(now=1000.0)
        # A route went off since: the kept HTTPS row is not used, and nothing passes for it.
        fewer = self.run_check(routes=serve([PORT['jellyfin']]), again=False, now=2000.0)
        self.assertEqual((fewer['rows'][1]['status'], fewer['rows'][1]['kept']), ('unknown', False))
        self.assertIn('app:immich', fewer['unresolved'])
        # The login of one app is no longer verified: its row fails at once, also without a retest.
        self.run_check(now=3000.0)
        self.logins['immich'] = {'state': 'failed'}
        later = self.run_check(again=False, now=4000.0)
        self.assertEqual(later['unresolved'], ['app:immich'])
        self.assertEqual(later['rows'][3]['status'], 'pass')  # the other app is untouched by it
        # Tailscale signed out: no kept row is shown as pass.
        out = self.run_check(status=tailscale('NeedsLogin'), again=False, now=5000.0)
        self.assertEqual([one['status'] for one in out['rows'][:3]], ['fail', 'unknown', 'unknown'])
        self.assertEqual(out['rows'][3]['last_passed'], 4000.0)  # when it last did pass is still known

    def test_new_addresses_drop_the_rows_that_stood_on_the_old_ones(self):
        self.run_check(now=1000.0)
        renamed = {app: address.replace('nekotest', 'renamed') for app, address in self.addresses().items()}
        self.assertEqual(sorted(phone_status.remembered(self.config, renamed)), ['phone', 'server'])
        one_changed = dict(self.addresses(), immich=renamed['immich'])
        self.assertEqual(sorted(phone_status.remembered(self.config, one_changed)), ['app:jellyfin', 'phone', 'server'])

    def test_a_damaged_or_foreign_file_is_no_kept_row(self):
        path = phone_status.saved_path(self.config)
        path.parent.mkdir(parents=True)
        for content in ('not json', '[]', '{"format": "something-else", "rows": {"server": {}}}', '{"rows": 5}'):
            path.write_text(content)
            self.assertEqual(phone_status.remembered(self.config, self.addresses()), {})
            self.assertEqual(phone_status.observation(self.config, self.addresses())['status'], 'not_tested')


class ObservationTests(Base):
    def test_only_a_persons_yes_is_recorded_and_it_is_no_machine_check(self):
        found = phone_status.observation(self.config, self.addresses())
        self.assertEqual((found['id'], found['status'], found['by']), ('observation', 'not_tested', 'person'))
        self.assertIn('Setup cannot check this for you', found['reason'])
        for answer in (False, None, 'yes', 1):
            self.assertEqual(
                phone_status.record_observation(self.config, answer, self.addresses())['status'], 'not_tested'
            )
        said = phone_status.record_observation(self.config, True, self.addresses(), now=50.0)
        self.assertEqual((said['status'], said['by']), ('observed', 'person'))
        self.assertIn('You said', said['reason'])

    def test_it_is_kept_apart_from_the_machine_rows_and_survives_them(self):
        phone_status.record_observation(self.config, True, self.addresses())
        check = lambda status: phone_status.check(  # noqa: E731
            self.config, FakeTailscale(status, serve(sorted(PORT.values()))), lambda *a: True, lambda: ''
        )
        fine = check(tailscale(peers={'k': phone()}))
        self.assertNotIn('observation', [one['id'] for one in fine['rows']])
        self.assertEqual(fine['observation']['status'], 'observed')
        # Every machine check failing later does not take the person's word back (T23) ...
        self.assertEqual(check(tailscale('Stopped'))['observation']['status'], 'observed')
        # (with no Tailscale name at all there are no addresses to hold it against)
        self.assertEqual(check(tailscale('NeedsLogin', DNSName=''))['observation']['status'], 'not_tested')
        self.assertEqual(check(tailscale(peers={}))['observation']['status'], 'observed')
        # ... and the person's word makes no machine row pass.
        self.assertIn('phone', check(tailscale(peers={}))['unresolved'])

    def test_other_addresses_need_a_new_look(self):
        phone_status.record_observation(self.config, True, self.addresses())
        moved = dict(self.addresses(), immich=f'https://{NAME}:9999/')
        self.assertEqual(phone_status.observation(self.config, moved)['status'], 'not_tested')

    def test_what_the_phone_step_was_told_in_an_earlier_run_counts(self):
        report = gateway_tailnet.report_path(self.config)
        report.parent.mkdir(parents=True)
        links = {**self.addresses(), 'dashboard': f'https://{NAME}:8448/'}
        report.write_text(
            json.dumps({'links': links, 'client_acceptance': 'needs_setup', 'phone_steps_done': ['tailscale']})
        )
        self.assertEqual(phone_status.observation(self.config, self.addresses())['status'], 'not_tested')
        report.write_text(
            json.dumps({'links': links, 'client_acceptance': 'needs_setup', 'phone_steps_done': [gateway_tailnet.AWAY]})
        )
        self.assertEqual(phone_status.observation(self.config, self.addresses())['status'], 'observed')
        report.write_text(json.dumps({'links': links, 'client_acceptance': 'user_confirmed'}))
        self.assertEqual(phone_status.observation(self.config, self.addresses())['status'], 'observed')
        report.write_text(json.dumps({'links': {'jellyfin': 'https://other/'}, 'client_acceptance': 'user_confirmed'}))
        self.assertEqual(phone_status.observation(self.config, self.addresses())['status'], 'not_tested')


class PrivateNamesTests(Base):
    """The machine name, the addresses and the phone's name are in no log, nothing printed
    and no reason; an app's address is only in the field the page shows it from."""

    def test_names_and_addresses_stay_out_of_logs_output_and_reasons(self):
        logged = io.StringIO()
        handler = logging.StreamHandler(logged)
        root = logging.getLogger()
        level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(lambda: (root.removeHandler(handler), root.setLevel(level)))
        printed = io.StringIO()
        results = []
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
            for status in (
                tailscale(peers={'k': phone()}),
                tailscale(magic=False),
                tailscale(certificates=False),
                tailscale('NeedsLogin'),
                'garbage',
            ):
                for answers in (True, False):
                    fake = FakeTailscale(status, serve(sorted(PORT.values())))
                    why = lambda: gateway_tools.certificate_why(  # noqa: E731
                        NAME, {'Health': [f'TLS certificate for {NAME} failed']}, '', None
                    ).replace(NAME, 'this server')
                    results.append(phone_status.check(self.config, fake, lambda *a, answers=answers: answers, why))
            results.append(phone_status.record_observation(self.config, True, self.addresses()))
        secrets = (NAME, 'nekotest', OWN_ADDRESS, 'private-phone-name', 'someone@example.org')
        for secret in secrets:
            self.assertNotIn(secret, logged.getvalue() + printed.getvalue())
        for found in results:
            rows = found['rows'] + [found['observation']] if 'rows' in found else [found]
            for one in rows:
                for key, value in one.items():
                    if key != 'address':
                        for secret in secrets:
                            self.assertNotIn(secret, json.dumps(value), (one['id'], key))


if __name__ == '__main__':
    unittest.main()
