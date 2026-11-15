"""Deep check plans 376-390: the guides followed literally, as a beginner reads them.
What a guide quotes (a question, a line of the terminal, an option, a section of another
guide) is what setup really says and has, so the reader finds on the screen what the
page told them to look for."""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import installer  # noqa: E402
import network_access  # noqa: E402
import questions  # noqa: E402
from modules import storage  # noqa: E402

GUIDES = sorted(ROOT.glob('*.md'))
# A question quoted as code: `Is your phone set up?`, with or without its [yes/no].
QUOTED_QUESTION = re.compile(r'`([A-Z][^`\n]{8,}\?)(?: \[yes/no\])?`')
SOURCES = [*ROOT.glob('*.py'), *ROOT.glob('modules/*.py')]


def doc(name):
    return (ROOT / name).read_text()


def joined(text):
    """Source text with its wrapped string literals read as one line each."""
    text = re.sub(r"'\s*\n\s*[fr]?'", '', text)
    text = re.sub(r'"\s*\n\s*[fr]?"', '', text)
    text = re.sub(r'\\\n\s*', '', text)
    return text.replace('\\"', '"').replace("\\'", "'")


def asked_titles():
    """Every question of questions.py as a pattern: its {names} stand for any words."""
    return [re.compile(re.sub(r'\\\{\w+\\\}', '.+', re.escape(entry['title']))) for entry in questions.Q.values()]


class QuotedQuestionTests(unittest.TestCase):
    """GUIDED-SETUP.md and README.md told the reader to answer `Did the Tailscale page say
    this server is connected? [yes/no]`, `Ask these questions again?`, `Continue with
    these drives anyway?` and others: questions setup stopped asking in those words when
    they moved to questions.py. A beginner waits for a line that never comes."""

    def test_every_question_a_guide_quotes_is_one_setup_asks(self):
        titles = asked_titles()
        source = '\n'.join(joined(path.read_text()) for path in SOURCES)
        unknown = []
        for guide in GUIDES:
            for quoted in QUOTED_QUESTION.findall(guide.read_text()):
                if not (any(title.fullmatch(quoted) for title in titles) or quoted in source):
                    unknown.append(f'{guide.name}: {quoted}')
        self.assertEqual(unknown, [])

    def test_the_home_network_is_said_not_asked(self):
        """GUIDED-SETUP.md showed a question (`Choose your home-network interface number:`)
        for a PC with a cable and Wi-Fi. Setup takes the cable and says so."""
        said = 'Home network: using ' + network_access.plainly(
            {'interface': 'enp3s0', 'ip': '192.168.1.20', 'cidr': '192.168.1.0/24'}, is_wireless=lambda name: False
        )
        text = doc('GUIDED-SETUP.md')
        self.assertIn(said, text)
        self.assertNotIn('interface number', text)


class DriveListTests(unittest.TestCase):
    """GUIDED-SETUP.md's example of the drive list was the list of an earlier release: it
    said the disk Linux runs from "cannot be used", which a PC with one drive reads as
    "NekoServer cannot be installed here"."""

    def test_the_example_is_what_setup_prints(self):
        text = doc('GUIDED-SETUP.md')
        example = text.split('```text\nEverything connected to this PC:\n')[1].split('```')[0]
        printed = (ROOT / 'tests/golden/drive-questions.txt').read_text().splitlines()
        self.assertGreater(len(example.splitlines()), 5)
        for line in example.splitlines():
            self.assertIn(line, printed)

    def test_what_the_guide_says_setup_says_back_is_printed(self):
        printed = (ROOT / 'tests/golden/drive-questions.txt').read_text()
        said = re.search(r'`(Drive 2 \([^`]+)`', doc('GUIDED-SETUP.md')).group(1)
        self.assertIn(said, printed)

    def test_a_drive_for_one_groups_apps_is_promised_only_while_it_is_offered(self):
        self.assertFalse(storage.OFFER_GROUP_INSTALLS, 'offered again: say so in README.md and GUIDED-SETUP.md')
        for name in ('README.md', 'GUIDED-SETUP.md'):
            text = doc(name)
            self.assertIn("for one group's apps is not offered yet", text, name)
            self.assertNotIn("asks where each group's apps are installed", text, name)
            self.assertNotIn('moves to 0.51.x', text, name)


class SetupWindowTests(unittest.TestCase):
    def test_a_closed_window_opens_again_from_its_address(self):
        """GUIDED-SETUP.md said the address "does not open again in a new browser" and
        quoted a page that does not exist; the account that opened the window can load
        the address again (setup_gui.Window.admit), as README.md and the terminal say."""
        text = doc('GUIDED-SETUP.md')
        self.assertNotIn('does not open again', text)
        self.assertNotIn('This setup window was already opened', text)
        self.assertIn('This setup window is open somewhere else', joined((ROOT / 'setup_gui.py').read_text()))
        self.assertIn('This setup window is open somewhere else', text)


class OptionsTests(unittest.TestCase):
    """install.sh --help ended "README.md shows when each is needed", and README.md named
    one of the twelve options."""

    def options(self):
        return re.findall(r'^\s+(--[a-z-]+)', self.help_text(), re.M)

    def help_text(self):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            installer.main(['--help'])
        return out.getvalue()

    def test_every_option_of_setup_is_in_the_readme(self):
        part = doc('README.md').split('### Other ways to run setup')[1].split('\n## ')[0]
        options = [option for option in self.options() if option != '--help']
        self.assertGreater(len(options), 8)
        for option in options:
            self.assertIn(option, part)

    def test_the_readme_names_no_option_setup_does_not_have(self):
        part = doc('README.md').split('### Other ways to run setup')[1].split('\n## ')[0]
        for option in set(re.findall(r'--[a-z][a-z-]+', part)):
            self.assertIn(option, self.options() + ['--help'])

    def test_help_names_the_section(self):
        self.assertIn('README.md, "Other ways to run setup"', ' '.join(self.help_text().split()))


class UpdateTimesTests(unittest.TestCase):
    def test_the_readme_says_what_the_monthly_night_does(self):
        """README.md said the server "updates the apps" on the 1st of each month. Since
        0.42.0 the monthly check updates nothing; the apps update on a weekly night
        (MONTHLY-MAINTENANCE.md, and the dashboard's own Help)."""
        backups = doc('README.md').split('## Backups')[1].split('\n## ')[0]
        self.assertNotIn('then updates the apps', backups)
        self.assertIn('It updates nothing', backups)
        self.assertIn('`update_day`', backups)


MESSAGES = [
    *SOURCES,
    *ROOT.glob('dashboard/*.js'),
    *ROOT.glob('setup-gui/*.js'),
    *ROOT.glob('runtime/src/**/*.rs'),
    ROOT / 'runtime/src/shared.json',
]
# A guide's section named in a message: STORAGE.md, "Where each app keeps your files".
NAMED_SECTION = re.compile(r'([A-Z][A-Z0-9-]+\.md), ["\u201c]([^"\u201d]{3,90})["\u201d]')


def sections(text):
    """What a guide names its parts: its headings and its bold paragraph labels."""
    heads = re.findall(r'^#{1,6}\s+(.*)$', text, re.M) + re.findall(r'\*\*([^*]+)\*\*', text)
    return [head.replace('`', '').strip().lower() for head in heads]


class MessageReferenceTests(unittest.TestCase):
    """Deep check plan 389: a message that sends the reader to a guide names a guide and a
    section that are there."""

    def test_every_section_a_message_names_is_in_that_guide(self):
        named = 0
        for path in MESSAGES:
            for name, section in NAMED_SECTION.findall(joined(path.read_text())):
                if not (ROOT / name).is_file():
                    continue  # a file name in an example, not one of the guides
                named += 1
                # A name wrapped over two lines of a comment or of a list of strings.
                section = re.sub(r"'?,?\s*\n\s*(//[/!]?|#|')?\s*", ' ', section)
                with self.subTest(source=path.name, guide=name, section=section):
                    self.assertTrue(any(section.lower().rstrip('.') in s for s in sections(doc(name))))
        self.assertGreater(named, 12)

    def test_immich_folders_elsewhere_are_sent_to_the_section_that_says_how_to_move_them(self):
        """ "Follow STORAGE.md for a verified migration": STORAGE.md had none, only "the
        verified, stopped-writer migration described in the prior release"."""
        said = joined((ROOT / 'modules/photos.py').read_text())
        self.assertIn('STORAGE.md, "Where each app keeps your files"', said)
        self.assertNotIn('described in the prior release', doc('STORAGE.md'))


class StorageGuideTests(unittest.TestCase):
    """Deep check plan 378: STORAGE.md read against setup."""

    def test_the_nas_switch_is_named_as_the_first_page_shows_it(self):
        import explanations

        title = next(row['title'] for row in explanations.EXTRAS if row['id'] == 'network_storage')
        text = doc('STORAGE.md')
        self.assertEqual(text.count(f'*{title}*'), 2)
        self.assertNotIn('are in a network storage box (NAS)*', text)

    def test_the_disk_linux_runs_from_can_hold_the_apps_and_the_media(self):
        """STORAGE.md said it "is never offered as the SSD, HDD or USB drive"; since 0.50.0
        it is, for a PC with one drive (only the USB backup drive must be a drive of its own)."""
        printed = (ROOT / 'tests/golden/drive-questions.txt').read_text()
        self.assertIn('the disk Linux runs from\n       Can be the app SSD or the media HDD.', printed)
        text = doc('STORAGE.md')
        self.assertNotIn('never offered as the SSD', text)
        self.assertIn('is never offered as the USB drive', text)

    def test_the_monthly_check_installs_no_updates(self):
        self.assertNotIn('install updates', doc('STORAGE.md'))


class CardGuideTests(unittest.TestCase):
    """Deep check plan 380: CARD-RECOVERY.md followed on a card (a hand-written one)."""

    def test_verify_is_described_as_it_answers(self):
        """ "It says ok, damaged or missing for each one": a card with nothing wrong gets
        one line of counts, and only damaged or missing files are named."""
        import tempfile

        import neko_restore

        sys.path.insert(0, str(ROOT / 'tests'))
        import test_neko_restore

        with tempfile.TemporaryDirectory() as tmp:
            card = test_neko_restore.Card(Path(tmp) / 'card')
            for n in range(4):
                card.add(f'2021/beach/{n}.jpg', b'sand')
            said = []
            with test_neko_restore.cards_under(tmp):
                code = neko_restore.main(['--card', str(card.top), 'verify'], say=said.append)
        self.assertEqual((code, len(said)), (0, 1))
        text = doc('CARD-RECOVERY.md')
        self.assertIn(f'`{said[0]}`', text)
        self.assertNotIn('for each one', text)

    def test_every_option_of_the_tool_is_in_the_guide(self):
        source = (ROOT / 'neko_restore.py').read_text()
        options = set(re.findall(r"add_argument\(\s*'(--[a-z-]+)'", source))
        self.assertGreater(len(options), 6)
        text = doc('CARD-RECOVERY.md')
        for option in options:
            self.assertIn(option, text)


if __name__ == '__main__':
    unittest.main()
