"""Wizard specification T31: the recovery menu and the drive repair never appear on the
way through an install or an update. Only  install.sh --recover  opens them.

What breaks each test (one line each):
- `import recovery_wizard` in wizard.py (or any module) fails
  test_only_the_recover_option_imports_the_recovery_menu;
- changing `if args.recover:` in installer.main to `if args.recover or args.wizard:` fails
  test_no_other_option_of_setup_opens_the_recovery_menu;
- adding '--recover' to the command in upgrade.run_setup fails
  test_no_step_of_install_or_update_starts_a_recovery_program;
- an ['e2fsck', ...] list in modules/storage.py fails
  test_a_drive_is_repaired_only_from_the_recovery_menu;
- `recover=true` in install.sh's --wizard case fails
  test_install_sh_turns_recovery_on_only_for_its_own_option.
"""

import ast
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import installer  # noqa: E402
import spec_sources  # noqa: E402

# The recovery side: the menu, the programs it starts, and their shared tools. The
# uninstaller is a menu of its own, started only by uninstall.sh.
RECOVERY_SIDE = {
    'recovery_wizard.py',
    'recover.py',
    'app-recovery.py',
    'host-recovery.py',
    'monthly-recovery.py',
    'music-recovery.py',
    'snapshot_restore.py',
    'uninstaller.py',
}
RECOVERY_PROGRAM = re.compile(r'recover\.py|-recovery\.py|recovery_wizard|--recover\b')
REPAIR_PROGRAMS = ('e2fsck', 'fsck', 'fsck.ext4', 'xfs_repair', 'ntfsfix', 'btrfs')
REPAIR_WORDS = {'check', 'rescue', '--repair', 'zero-log'}  # of btrfs: its other commands only read


class OpenedMenu(Exception):
    pass


def enclosing_ifs(tree, wanted):
    """The conditions of every `if` around the node `wanted`, outermost first."""

    def search(node, conditions):
        if node is wanted:
            return conditions
        for child in ast.iter_child_nodes(node):
            guarded = isinstance(node, ast.If) and child in node.body
            inside = conditions + [ast.unparse(node.test)] if guarded else conditions
            found = search(child, inside)
            if found is not None:
                return found
        return None

    return search(tree, [])


class RecoveryMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = spec_sources.python_files()
        cls.install_side = {name: tree for name, tree in cls.files.items() if name not in RECOVERY_SIDE}

    def test_the_reading_covers_setup_update_and_every_step(self):
        for name in ('installer.py', 'wizard.py', 'onboard.py', 'upgrade.py', 'setup_gui.py', 'guided_setup.py'):
            self.assertIn(name, self.install_side)
        self.assertGreater(len([name for name in self.install_side if name.startswith('modules/')]), 30)

    def test_only_the_recover_option_imports_the_recovery_menu(self):
        importers = [
            (name, line)
            for name, tree in self.files.items()
            for line, module in spec_sources.imports(tree)
            if module.split('.')[0] == 'recovery_wizard'
        ]
        self.assertEqual([name for name, _ in importers], ['installer.py'], importers)
        tree = self.files['installer.py']
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.Import) and n.names[0].name == 'recovery_wizard')
        self.assertEqual(enclosing_ifs(tree, node), ['args.recover'])
        # And the menu is used nowhere else in that file.
        uses = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == 'recovery_wizard']
        self.assertEqual([enclosing_ifs(tree, use) for use in uses], [['args.recover']])

    def test_no_other_option_of_setup_opens_the_recovery_menu(self):
        tree = self.files['installer.py']
        options = {}  # every option of installer.py, and whether it takes a value
        for call in ast.walk(tree):
            if isinstance(call, ast.Call) and getattr(call.func, 'attr', '') == 'add_argument':
                takes_value = not any(
                    key.arg == 'action' and 'store_' in getattr(key.value, 'value', '') for key in call.keywords
                )
                options[call.args[0].value] = takes_value
        self.assertIn('--recover', options)
        self.assertGreaterEqual(len(options), 10)
        menu = types.ModuleType('recovery_wizard')
        menu.run = mock.Mock(side_effect=OpenedMenu)
        fake_wizard = types.ModuleType('wizard')
        fake_wizard.run = mock.Mock(return_value=0)
        with (
            mock.patch.dict(sys.modules, {'recovery_wizard': menu, 'wizard': fake_wizard}),
            mock.patch.object(installer, 'execute', return_value=0),
            mock.patch.object(installer, 'settings_for', return_value=None),
            mock.patch.object(installer, 'finish_update', return_value=0),
            mock.patch('sys.stderr'),
        ):
            tried = [[]]
            for option, takes_value in options.items():
                if option != '--recover':
                    tried.append([option, 'full'] if takes_value else [option])
            tried += [['--wizard', '--apply'], ['--apply', '--profile', 'full'], ['--module', 'storage', '--apply']]
            for argv in tried:
                try:
                    installer.main(argv)
                except SystemExit:
                    pass  # an option setup refuses on its own
                menu.run.assert_not_called()
            # The trap is real: the one option that is for recovery does open it.
            with self.assertRaises(OpenedMenu):
                installer.main(['--recover'])
        self.assertGreaterEqual(fake_wizard.run.call_count, 2)  # setup itself did start

    def test_no_step_of_install_or_update_starts_a_recovery_program(self):
        runners = ('run_program', 'run', 'run_cmd', 'Popen', 'check_call', 'check_output', 'call', 'execv', 'execvp')
        checked = 0
        for name, tree in self.install_side.items():
            for line, words in spec_sources.command_lists(tree):
                checked += 1
                for word in words:  # a program or an option has no space in it; a sentence is no command
                    if word and ' ' not in word:
                        self.assertIsNone(RECOVERY_PROGRAM.search(word), f'{name}:{line}: {words}')
            for call in ast.walk(tree):
                if isinstance(call, ast.Call) and getattr(call.func, 'attr', getattr(call.func, 'id', '')) in runners:
                    for text in spec_sources.text_of(call):
                        self.assertIsNone(RECOVERY_PROGRAM.search(text), f'{name}:{call.lineno}: {text[:120]}')
        self.assertGreater(checked, 300)
        # The same reading does see the recovery programs where they are started.
        menu_text = [
            text
            for call in ast.walk(self.files['recovery_wizard.py'])
            if isinstance(call, ast.Call)
            for text in spec_sources.text_of(call)
        ]
        self.assertTrue(any(RECOVERY_PROGRAM.search(text) for text in menu_text))

    def test_a_drive_is_repaired_only_from_the_recovery_menu(self):
        repairs = []
        for name, tree in self.files.items():
            for line, words in spec_sources.command_lists(tree):
                program = words[1] if words[0] == 'sudo' and len(words) > 1 else words[0]
                if program in REPAIR_PROGRAMS and (program != 'btrfs' or REPAIR_WORDS & set(words)):
                    repairs.append(name)
        self.assertEqual(set(repairs), {'recovery_wizard.py'}, repairs)
        asked = [
            name
            for name, tree in self.files.items()
            if any(isinstance(n, ast.Constant) and n.value == 'repair_drive' for n in ast.walk(tree))
        ]
        self.assertEqual(sorted(asked), ['questions.py', 'recovery_wizard.py'])

    def test_the_window_shows_a_menu_of_its_own_only_for_recovery_and_removal(self):
        callers = [
            name
            for name, tree in self.files.items()
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == 'session'
            and getattr(call.func.value, 'id', '') == 'setup_gui'
        ]
        self.assertEqual(sorted(callers), ['recovery_wizard.py', 'uninstaller.py'])

    def test_install_sh_turns_recovery_on_only_for_its_own_option(self):
        lines = (spec_sources.PROJECT / 'install.sh').read_text(encoding='utf-8').split('\n')
        on = [number for number, line in enumerate(lines) if re.search(r'\brecover=true\b', line)]
        self.assertEqual(len(on), 1)
        case = next(line.strip() for line in reversed(lines[: on[0]]) if line.strip().endswith(')'))
        self.assertEqual(case, '--recover)')
        # With no option it is the setup wizard, and setup gets the person's own options.
        self.assertIn('  set -- --wizard', lines)
        started = [line.strip() for line in lines if 'installer.py"' in line and 'python3' in line]
        self.assertEqual(
            sorted(started), ['exec python3 "$ROOT/installer.py" "$@"', 'python3 "$ROOT/installer.py" --finish-update']
        )


if __name__ == '__main__':
    unittest.main()
