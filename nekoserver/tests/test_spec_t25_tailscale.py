"""Wizard specification T25: setup and the runtime never switch on Tailscale Funnel
(apps open to the whole internet), an exit node, or port forwarding.

Every Tailscale command NekoServer can run is read from its own source, in the two ways
it writes them: a list in Python (['tailscale', 'status', '--json']) and an array in
Rust (&["tailscale", "status", "--json"]). Each must be one of the three commands in
use, with only the options listed here.

What breaks each test (one line each):
- adding '--advertise-exit-node' to the ['tailscale', 'up', ...] list in
  modules/tailscale.py fails test_tailscale_up_has_two_fixed_options_and_nothing_else;
- a new ['tailscale', 'funnel', ...] or ['tailscale', 'set', ...] list anywhere fails
  test_every_tailscale_command_is_status_up_or_serve;
- removing "or any(funnel.values())" from gateway_tools.canonical fails
  test_a_serve_setup_with_funnel_on_is_refused_and_left_alone;
- a firewall rule with '-t', 'nat' fails test_no_command_forwards_a_port.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import gateway_tools  # noqa: E402
import spec_sources  # noqa: E402

# The only Tailscale commands in use, and every constant word each may carry. A computed
# word (None) is allowed only where named below.
SERVE_WORDS = {'serve', '--bg', 'status', '--json', 'off'}
UP_WORDS = ['tailscale', 'up', '--json', '--timeout=45s']
STATUS_WORDS = ['tailscale', 'status', '--json']
# Never part of any command, option or setting NekoServer writes.
FORBIDDEN = re.compile(
    r'tailscale\s+(funnel|set|configure|ssh|file|drive)\b'
    r'|\bfunnel\s+(--bg|--https|--tcp|on\b|[0-9])'
    r'|--advertise-exit-node|--advertise-routes|--exit-node|--accept-routes|--advertise-connector'
    r'|\bupnpc\b|\bnatpmpc\b|miniupnp|nat-pmp|natpmp'
    r'|-j\s+(DNAT|MASQUERADE|REDIRECT)|-t\s+nat\b|ip_forward\s*=\s*1'
)


# Lists that begin with the word and are no command: the names of setup's phone steps.
NOT_COMMANDS = {('modules/gateway_tailnet.py', ('tailscale', None, None))}


def python_commands(program):
    """(file, line, words) of every command list naming `program`, also behind sudo."""
    found = []
    for name, tree in spec_sources.python_files().items():
        for line, words in spec_sources.command_lists(tree):
            words = words[words.index(program) :] if program in words[:2] else []
            # The word alone is a name (the package to install), not a command.
            if len(words) > 1 and (name, tuple(words)) not in NOT_COMMANDS:
                found.append((name, line, words))
    return found


def rust_commands(program):
    """(file, words) of every array in the runtime that starts with `program`."""
    found = []
    for path in sorted(spec_sources.RUNTIME.glob('*.rs')):
        code = spec_sources.rust_code(path.name)
        for match in re.finditer(r'\[\s*"' + program + r'"\s*((?:,\s*"[^"]*"\s*)*)(,?\s*[^\]"]*)\]', code):
            words = [program] + re.findall(r'"([^"]*)"', match.group(1))
            words += [None] if match.group(2).strip(' ,\n') else []
            if len(words) > 1:  # the word alone is a name, not a command
                found.append((path.name, words))
    return found


class TailscaleCommandTests(unittest.TestCase):
    def setUp(self):
        self.python = python_commands('tailscale')
        self.rust = rust_commands('tailscale')

    def test_the_reading_finds_the_commands_that_are_there(self):
        files = {name for name, _, _ in self.python}
        self.assertLessEqual({'modules/tailscale.py', 'gateway_tools.py', 'uninstaller.py', 'acceptance.py'}, files)
        self.assertIn(('modules/tailscale.py', UP_WORDS), [(name, words) for name, _, words in self.python])
        self.assertIn(('collect.rs', STATUS_WORDS), self.rust)
        self.assertGreaterEqual(len(self.python) + len(self.rust), 12)

    def test_every_tailscale_command_is_status_up_or_serve(self):
        for where, words in [(f'{name}:{line}', words) for name, line, words in self.python] + self.rust:
            self.assertIn(words[1], ('status', 'up', 'serve'), f'{where}: {words}')

    def test_tailscale_status_only_reads(self):
        for where, words in [(f'{name}:{line}', words) for name, line, words in self.python] + self.rust:
            if words[1] == 'status':
                self.assertEqual(words, STATUS_WORDS, where)

    def test_tailscale_up_has_two_fixed_options_and_nothing_else(self):
        ups = [(f'{name}:{line}', words) for name, line, words in self.python if words[1] == 'up']
        self.assertEqual([where.split(':')[0] for where, _ in ups], ['modules/tailscale.py'])
        for where, words in ups:
            self.assertEqual(words, UP_WORDS, where)  # no computed word, no other option
        self.assertEqual([words for _, words in self.rust if words[1] == 'up'], [])

    def test_tailscale_serve_only_reads_adds_or_removes_a_private_https_route(self):
        serves = [(f'{name}:{line}', words) for name, line, words in self.python if words[1] == 'serve']
        self.assertGreaterEqual(len(serves), 6)
        for where, words in serves:
            self.assertLessEqual({word for word in words[1:] if word is not None}, SERVE_WORDS | {'--https='}, where)
            if words[2:] != ['status', '--json']:
                self.assertEqual(words[2], '--bg', where)  # a route: never a foreground or Funnel one
                self.assertLessEqual(len(words), 5, where)
        self.assertEqual([words for _, words in self.rust if words[1] == 'serve'], [])

    def test_the_computed_route_option_is_only_ever_https_and_a_port(self):
        self.assertEqual(gateway_tools.https('8443'), '--https=8443')
        self.assertEqual(gateway_tools.https(443), '--https=443')
        for bad in ('443 --set-path=/', 'funnel', '0', '70000', '', None, '--https=443', '443\n--bg'):
            with self.assertRaises((RuntimeError, ValueError, TypeError), msg=repr(bad)):
                gateway_tools.https(bad)

    def test_a_serve_setup_with_funnel_on_is_refused_and_left_alone(self):
        private = {'TCP': {'443': {'HTTPS': True}}, 'Web': {}, 'AllowFunnel': {'server.example.ts.net:443': False}}
        self.assertEqual(gateway_tools.canonical(private), {'TCP': private['TCP'], 'Web': {}})
        public = dict(private, AllowFunnel={'server.example.ts.net:443': True})
        with self.assertRaisesRegex(RuntimeError, 'Funnel'):
            gateway_tools.canonical(public)
        with self.assertRaisesRegex(RuntimeError, 'Funnel'):
            gateway_tools.canonical(dict(private, AllowFunnel={'server.example.ts.net:443': 'yes'}))


class NoForwardingTests(unittest.TestCase):
    def shipped_text(self):
        """Every line of code NekoServer ships: Python, the runtime (without its tests
        and comments), the shell scripts, the two web pages' scripts, the app files."""
        for name, tree in spec_sources.python_files().items():
            yield name, (spec_sources.PROJECT / name).read_text(encoding='utf-8')
        for path in sorted(spec_sources.RUNTIME.glob('*.rs')):
            yield path.name, spec_sources.rust_code(path.name)
        for pattern in ('*.sh', 'dashboard/*.js', 'setup-gui/*.js', 'compose/*.yaml'):
            for path in sorted(spec_sources.PROJECT.glob(pattern)):
                yield path.name, path.read_text(encoding='utf-8')

    def test_no_shipped_code_names_funnel_an_exit_node_or_a_forwarding_tool(self):
        seen = 0
        for name, text in self.shipped_text():
            seen += 1
            for number, line in enumerate(text.split('\n'), 1):
                self.assertIsNone(FORBIDDEN.search(line), f'{name}:{number}: {line.strip()[:160]}')
        self.assertGreater(seen, 100)
        # The pattern does catch what it is for.
        for bad in (
            'tailscale funnel --bg 443',
            "['tailscale', 'up', '--advertise-exit-node']",
            'tailscale set --exit-node=1.2.3.4',
            'upnpc -a 192.168.1.5 443 443 tcp',
            'iptables -t nat -A PREROUTING -j DNAT',
            'net.ipv4.ip_forward=1',
        ):
            self.assertIsNotNone(FORBIDDEN.search(bad), bad)

    def test_no_command_forwards_a_port(self):
        firewalls = ('iptables', 'ip6tables', 'nft', 'ufw', 'firewall-cmd', 'sysctl', 'upnpc', 'natpmpc', 'ssh')
        found = [(name, words) for tool in firewalls for name, _, words in python_commands(tool)]
        # The one use: the isolated restore test fences its own network in (recovery_tools).
        self.assertEqual({name for name, _ in found}, {'recovery_tools.py'})
        for name, words in found:
            constants = {word for word in words if word is not None}
            self.assertFalse(
                constants & {'-t', 'nat', 'DNAT', 'MASQUERADE', 'REDIRECT', 'allow', '--add-port', '-R'}, (name, words)
            )
        self.assertEqual([found for tool in firewalls for found in rust_commands(tool)], [])
        rule = gateway_tools.recovery_tools.firewall_rule('DOCKER-USER', 'neko-rt-1')
        self.assertEqual(rule[-2:], ['-j', 'DROP'], rule)  # it closes a way out, it opens none


if __name__ == '__main__':
    unittest.main()
