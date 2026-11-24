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

    def test_the_logins_question_is_shown_as_the_terminal_asks_it(self):
        """ACCOUNT-SETUP.md and GUIDED-SETUP.md showed the question of an earlier release
        ("Logins for the apps: 1 = NekoServer makes a strong username ...")."""
        asked = questions.terminal('logins_who').strip()
        for name in ('GUIDED-SETUP.md', 'ACCOUNT-SETUP.md'):
            self.assertIn('```text\n' + asked + '\n```', doc(name), name)

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


class NoSudoTests(unittest.TestCase):
    def test_the_readme_says_what_to_do_before_the_first_sudo(self):
        """1.0 round T5-01: Debian installed with a root password has no sudo, and the
        README's first commands stopped with sudo's own "command not found" or "not in the
        sudoers file". Before the first command it says what to do, in install.sh's words."""
        readme = (ROOT.parent / 'README.md').read_text()
        before = readme.split('## Install')[1].split('```bash')[0]
        for words in ('command not found', 'not in the sudoers file', 'su -', 'apt install sudo', 'usermod -aG sudo'):
            self.assertIn(words, before)
        script = (ROOT / 'install.sh').read_text()
        for words in ('su -', 'apt install sudo', 'usermod -aG sudo', 'log out, log in again'):
            self.assertIn(words, script)


class NextcloudDatabaseRestoreTests(unittest.TestCase):
    """1.0 round T1-R2: RECOVERY.md loaded Nextcloud's database copy as `nextcloud`, so
    every table belonged to it, while Nextcloud signs in to its database with an account
    of its own (config.php's dbuser, made by `occ maintenance:install`): "permission
    denied for table oc_appconfig", and Nextcloud stayed down. The copy is loaded as that
    account, into a database it owns, and stops at the first error."""

    def test_the_copy_is_loaded_as_the_account_nextcloud_uses(self):
        # Nextcloud is installed with the container's superuser, and makes its own account.
        self.assertIn('--database-user="$POSTGRES_USER"', (ROOT / 'modules' / 'nextcloud.py').read_text())
        steps = doc('RECOVERY.md').split("### Nextcloud's database")[1].split(chr(10) + '### ')[0]
        self.assertIn('echo $CONFIG["dbuser"]', steps)
        self.assertIn('createdb -U nextcloud -O DBUSER nextcloud', steps)
        self.assertIn('psql -U DBUSER -d nextcloud --single-transaction -v ON_ERROR_STOP=1', steps)
        self.assertNotIn('psql -U nextcloud', steps)


def root_only_steps(text):
    """The commands of RECOVERY.md's two by-hand database restores."""
    immich = text.split('### Rebuild Immich from the USB drive')[1].split(chr(10) + '### ')[0]
    nextcloud = text.split("### Nextcloud's database")[1].split(chr(10) + '### ')[0]
    return immich + nextcloud


class RootOnlyFolderTests(unittest.TestCase):
    """1.0 round T1-R1: the by-hand Immich and Nextcloud database restores, followed word for
    word as the normal user with sudo, stopped at their first command: `cd
    /srv/nekoserver/compose/immich` said "Permission denied", as did `sha256sum` in the USB
    drive's nextcloud-database folder and the `< copy` that loads it, and "open it in the file
    manager" cannot open a folder only root may. Setup makes these folders root's alone, so
    every step reaches them through sudo."""

    def test_the_folders_the_steps_use_are_roots_alone(self):
        self.assertIn('ensure_directory(compose_dir, 0o700)', (ROOT / 'modules' / 'photos.py').read_text())
        backup = (ROOT / 'runtime' / 'src' / 'backup.rs').read_text()
        self.assertIn('let folder = usb.join(kind.folder);\n    common::directory(&folder, 0o700)?;', backup)

    def test_every_step_reaches_a_root_only_folder_through_sudo(self):
        steps = root_only_steps(doc('RECOVERY.md'))
        self.assertNotIn('file manager to see', steps)
        self.assertNotRegex(steps, r'(?m)^\s*cd /srv/')
        self.assertNotRegex(steps, r'< /')
        blocks = '\n'.join(re.findall(r'```bash\n(.*?)```', steps, re.S))
        inline = re.findall(r'`([^`\n]+)`', re.sub(r'```bash\n.*?```', '', steps, flags=re.S))
        for command in inline + [line.strip() for line in blocks.splitlines()]:
            if re.search(r'(?:^|\s)(?:sha256sum|cat|ls) ', command) and not command.startswith('sudo '):
                self.fail(f'{command!r} reads a root-only folder without sudo')
        self.assertIn('sudo cat /path/to/nextcloud-DATE.sql | sudo docker compose -p neko-nextcloud', steps)
        self.assertIn('sudo sha256sum /mnt/nekoserver-media/backups/nextcloud-database/nextcloud-DATE.sql', steps)
        self.assertIn('sudo ls -l /media/$USER/*/immich-database/', steps)


class FirstReadTests(unittest.TestCase):
    """1.0 round T5-02 to T5-05: what a first-time reader of the README is told matches
    what setup and install.sh do."""

    def readme(self):
        return (ROOT.parent / 'README.md').read_text()

    def test_the_no_sudo_passage_covers_arch_too(self):
        # T5-02: Arch can be installed without sudo too; there it is the wheel group.
        before = self.readme().split('## Install')[1].split('```bash')[0]
        for words in ('pacman -S sudo', 'usermod -aG wheel', '%wheel ALL=(ALL:ALL) ALL'):
            self.assertIn(words, before)

    def test_ask_nekos_model_is_among_what_stops_setup_after_the_approval(self):
        # T5-03: with Ask Neko switched on, its model is asked during the run.
        self.assertIn('def ask_model(', (ROOT / 'modules' / 'assistant.py').read_text())
        stops = self.readme().split('After the approval only these stop it')[1].split(chr(10))[0]
        self.assertIn('Ask Neko', stops)

    def test_the_shared_folders_are_said_to_need_something_to_share(self):
        # T5-04: with no app, or Nextcloud alone, the shares step shares nothing.
        self.assertIn('def anything_to_share(', (ROOT / 'modules' / 'samba.py').read_text())
        for doc in (ROOT.parent / 'README.md', ROOT / 'README.md', ROOT / 'GUIDED-SETUP.md'):
            said = [
                line
                for line in doc.read_text().splitlines()
                if 'shared folders' in line and ('lways set up' in line or 'set up either way' in line)
            ]
            self.assertTrue(said, doc)
            for line in said:
                self.assertIn('something to share', line, doc)

    def test_the_pc_is_said_to_be_intel_or_amd(self):
        # T5-05: install.sh refuses anything but x86_64 (an ARM board, say).
        self.assertIn('[[ "$machine" != x86_64 ]]', (ROOT / 'install.sh').read_text())
        need = self.readme().split('## What you need')[1].split(chr(10) + '- ')[1]
        for words in ('64-bit Intel or AMD', 'x86_64', 'ARM'):
            self.assertIn(words, need)


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

    def test_the_password_list_is_named_as_the_recovery_menu_shows_it(self):
        """The weekly USB copy's log and two guides sent the reader to the recovery menu's
        "App password list"; the menu has "The list of app passwords"."""
        import recovery_wizard
        from neko_common import SHARED

        name = SHARED['recovery_menu']['password_list']
        self.assertTrue(dict(recovery_wizard.MENU)['passwords'].startswith(name))
        self.assertIn('common::shared("recovery_menu", "password_list")', (ROOT / 'runtime/src/backup.rs').read_text())
        for path in [*GUIDES, *MESSAGES]:
            self.assertNotIn('App password list', path.read_text(), path.name)
        for guide in ('STORAGE.md', 'MONTHLY-MAINTENANCE.md', 'RECOVERY.md'):
            self.assertIn(f'**{name}**', doc(guide))


class NeedsTests(unittest.TestCase):
    """0.52.3 loop (T3L-01): the TV needs Movies and shows (setup leaves it out without it),
    and no guide that offers the TV said so: a reader who wanted it for games alone found
    the switch greyed and setup skipping it."""

    # The guides that offer each extra that needs another (a card of its own; a switch
    # inside a card needs no words, the card it is in says it).
    OFFERED_IN = {'tv': ('../README.md', 'GUIDED-SETUP.md', 'TV-SETUP.md')}

    def test_a_guide_that_offers_an_extra_says_what_it_needs(self):
        import explanations

        titles = {row['id']: row['title'] for row in explanations.EXTRAS}
        needing = [row for row in explanations.EXTRAS if row.get('needs') and not row.get('under')]
        self.assertEqual(sorted(row['id'] for row in needing), sorted(self.OFFERED_IN))
        for row in needing:
            # "Movies and shows - Jellyfin" is named in the guides as **Movies and shows**.
            needed = [titles[need].split(' - ')[0] for need in row['needs']]
            for guide in self.OFFERED_IN[row['id']]:
                with self.subTest(extra=row['id'], guide=guide):
                    self.assertTrue(any(f'needs **{name}**' in doc(guide) for name in needed), needed)


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
        self.assertIn('the disk Linux runs from\n       Can be the apps drive or the media and backups drive.', printed)
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


class BeginnerFirstTests(unittest.TestCase):
    """Deep check plans 381, 383 to 386 and 390: the pages README.md sends a home user to
    (phone alerts, the outage warning, Nextcloud, this PC's own backups, sleep and logs)
    began with the administrator's reference ("an OS-local Rust oneshot worker and timer").
    Each now starts with what it is, what you do and what to do when it stops; the
    reference is kept, under "For administrators"."""

    PAGES = {
        'NOTIFICATIONS.md': ('## Set it up', '**Send alerts to my phone**'),
        'HEARTBEAT.md': ('## Set it up', '**Warn me if the server stops checking in**'),
        'NEXTCLOUD.md': ('**Open it.**', '**Personal files - Nextcloud**'),
        'HOST-BACKUPS.md': ('**Getting something back:**', '**Steam and Samba settings, from a nightly backup**'),
        'SERVER-OS.md': ('**The PC no longer goes to sleep.**', '`"server_os"`'),
    }
    JARGON = (
        'oneshot',
        'systemd',
        'Compose',
        'PostgreSQL',
        'logind',
        'drop-in',
        'UID',
        'tdbsam',
        'needs_setup',
        'noninteractive',
        'bearer',
        'hdparm',
        'journald',
        'Rust',
        '` module',
        ' profile',
    )

    def test_each_page_starts_with_the_user_and_keeps_the_reference(self):
        for name, (first, named) in self.PAGES.items():
            with self.subTest(doc=name):
                text = doc(name)
                admin = text.index('\n## For administrators\n')
                user_part = text[:admin]
                self.assertLess(text.index(first), admin)
                self.assertIn(named, user_part)
                for word in self.JARGON:
                    self.assertNotIn(word, user_part)
                self.assertGreater(len(text) - admin, 2000)

    def test_what_the_pages_name_is_what_setup_shows(self):
        import explanations
        import recovery_wizard

        titles = {row['id']: row['title'] for row in explanations.EXTRAS}
        self.assertIn(f"**{titles['notifications']}**", doc('NOTIFICATIONS.md'))
        self.assertIn(f"**{titles['heartbeat']}**", doc('HEARTBEAT.md'))
        self.assertIn(f"**{titles['nextcloud']}**", doc('NEXTCLOUD.md'))
        menu = dict(recovery_wizard.MENU)
        self.assertIn(f"**{menu['host']}**", doc('HOST-BACKUPS.md'))
        self.assertIn(f"**{menu['cloud']}**", doc('NEXTCLOUD.md'))
        self.assertIn(f"**{dict(recovery_wizard.SECTIONS)['restore']}**", doc('HOST-BACKUPS.md'))

    def test_the_steps_on_the_phone_are_the_ones_setup_shows(self):
        from modules import heartbeat, notifications

        self.assertIn(f'**{notifications.TEST_TITLE}**', doc('NOTIFICATIONS.md'))
        self.assertIn('Healthchecks.io (free)', ' '.join(heartbeat.form_lines({})))
        self.assertIn('Healthchecks.io (free)', doc('HEARTBEAT.md'))

    def test_no_page_says_the_program_comes_ready_made_or_that_updates_skip_new_steps(self):
        """NOTIFICATIONS.md: "The native executable is included; a Rust compiler is not needed
        on the server" (setup builds it on the PC, README.md). Three pages: "select the
        module explicitly when upgrading" (an update adds a release's new steps to the
        profiles of an older my-config.json: config_checks.merge_profiles)."""
        import config_checks

        self.assertTrue(callable(config_checks.merge_profiles))
        for guide in GUIDES:
            text = guide.read_text()
            for stale in (
                'a Rust compiler is not needed',
                'explicitly when upgrading',
                'select it explicitly',
                'explicit module command when upgrading',
                'to install it explicitly',
                'no Rust toolchain is needed',
                '512GB',
            ):
                self.assertNotIn(stale, text, guide.name)


class DashboardGuideTests(unittest.TestCase):
    """Deep check plans 382 and 387: DASHBOARD.md and the dashboard's own Help guides
    (dashboard/app.js) read against the page."""

    APP = ROOT / 'dashboard/app.js'

    def routes(self):
        listed = self.APP.read_text().split('const ROUTES = {')[1].split('};')[0]
        return re.findall(r":\s*'([^']+)'", listed)

    def test_every_page_of_the_dashboard_is_in_the_pages_table(self):
        """The Updates page (where an app is updated and NekoServer upgraded) was missing."""
        table = doc('DASHBOARD.md').split('## The pages')[1].split('\n## ')[0]
        pages = self.routes()
        self.assertGreater(len(pages), 10)
        for page in pages:
            self.assertIn(f'| **{page}** |', table)

    def test_the_system_tab_is_described_as_it_is(self):
        """ "System: what it will cover (a later release)": the tab shows the waiting Linux
        updates and whether a restart is needed (system_updates.py); only installing them
        from the page is for later."""
        text = doc('DASHBOARD.md')
        self.assertNotIn('what it will cover', text)
        self.assertIn("'No package updates waiting.'", self.APP.read_text())
        self.assertIn('whether the computer needs a restart', text)

    def test_help_names_the_sensors_package_of_every_linux(self):
        """Help said to install the "lm_sensors" package: on Debian and Ubuntu that name
        does not exist (lm-sensors), nor on openSUSE (sensors). DASHBOARD.md has each."""
        guides = self.APP.read_text().split('const GUIDES = {')[1].split('\n};')[0]
        lines = [line for line in guides.splitlines() if 'lm_sensors' in line]
        self.assertEqual(len(lines), 2)
        for line in lines:
            for name in (
                'lm-sensors',
                '\u201csensors\u201d on openSUSE',
                'DASHBOARD.md, \u201cA sensor is missing\u201d',
            ):
                self.assertIn(name, line)
        commands = doc('DASHBOARD.md').split('## A sensor is missing')[1].split('\n## ')[0]
        for command in (
            'pacman -S lm_sensors',
            'apt-get install lm-sensors',
            'dnf install lm_sensors',
            'zypper install sensors',
        ):
            self.assertIn(command, commands)

    def test_help_names_the_switches_of_setups_first_page(self):
        """ "Set up background notifications with setup" did not say how."""
        import explanations

        titles = {row['id']: row['title'] for row in explanations.EXTRAS}
        guides = self.APP.read_text().split('const GUIDES = {')[1].split('\n};')[0]
        for extra in ('notifications', 'heartbeat'):
            self.assertIn('\u201c' + titles[extra] + '\u201d', guides)


class RestoredSettingsTests(unittest.TestCase):
    """Deep check plan 389: after restoring app settings the recovery menu says "See
    APP-BACKUPS.md or HOST-BACKUPS.md for the exact steps", and neither page had them."""

    def test_the_pages_the_menu_names_have_the_steps(self):
        import recovery_wizard

        said = joined((ROOT / 'recovery_wizard.py').read_text())
        self.assertIn('See APP-BACKUPS.md or HOST-BACKUPS.md for the exact steps.', said)
        apps = doc('APP-BACKUPS.md')
        steps = apps.split("## Putting one app's settings back")[1].split('\n## ')[0]
        self.assertLess(apps.index("## Putting one app's settings back"), apps.index('## For administrators'))
        self.assertIn(f"**{dict(recovery_wizard.MENU)['apps']}**", steps)
        for line in (
            'sudo docker stop neko-jellyfin-jellyfin-1',
            'sudo cp -a FOLDER/appdata/jellyfin /srv/nekoserver/appdata/',
            'sudo docker start neko-jellyfin-jellyfin-1',
        ):
            self.assertIn(line, steps)
        self.assertIn('sudo cp -a FILE WHERE-IT-BELONGS', doc('HOST-BACKUPS.md'))
        self.assertIn('(APP-BACKUPS.md#putting-one-apps-settings-back)', doc('RECOVERY.md'))

    def follow_the_steps(self, on_its_drive):
        """The guide's commands run as written (docker left out) in a private mount
        namespace: Jellyfin's settings on the SSD, or on a drive of its own, mounted at
        /srv/nekoserver/appdata/jellyfin by setup's /etc/fstab line."""
        import os
        import shutil
        import subprocess
        import tempfile

        if os.geteuid() != 0 or not shutil.which('unshare'):
            self.skipTest('needs root and unshare for a private mount')
        steps = doc('APP-BACKUPS.md').split("## Putting one app's settings back")[1].split('\n## ')[0]
        blocks = re.findall(r'```bash\n(.*?)```', steps, re.S)
        with tempfile.TemporaryDirectory(dir='/var/tmp') as d:
            top = Path(d)
            ssd, restore = top / 'ssd', top / 'restore'
            ondrive = top / 'drives/big/nekoserver/appdata/jellyfin'
            fstab = top / 'fstab'
            for folder in (ssd / 'appdata/jellyfin', restore / 'appdata/jellyfin', ondrive):
                folder.mkdir(parents=True)
            present = ondrive if on_its_drive else ssd / 'appdata/jellyfin'
            (present / 'library.db').write_text('present')
            (present / 'made-after-the-backup').write_text('present')
            (restore / 'appdata/jellyfin/library.db').write_text('restored')
            target = ssd / 'appdata/jellyfin'
            fstab.write_text(
                storage.installed_line(str(ondrive), str(target), str(top / 'drives/big'), str(ssd)) + '\n'
            )

            def shell(block):
                found = subprocess.run(['grep', 'appdata/jellyfin', str(fstab)], capture_output=True, text=True)
                lines = [line for line in block.replace('sudo ', '').splitlines() if 'docker ' not in line]
                text = '\n'.join(lines).replace('/srv/nekoserver', str(ssd)).replace('FOLDER', str(restore))
                return text.replace('ONDRIVE', found.stdout.split()[0])

            # mount(8) reads this fstab in place of the PC's; setup's line is mounted first.
            script = [f'mount() {{ command mount -T {fstab} "$@"; }}', f'mount {target}' if on_its_drive else '']
            script.append(shell(blocks[0]).replace('mv ', f'mv 2>{top}/said ', 1))
            # As the guide says: the other commands only where mv said the folder is busy.
            busy = [shell(block) for block in blocks[1:] if 'umount' in block] or ['false']
            script.append(f'if grep -q "Device or resource busy" {top}/said; then\n' + '\n'.join(busy) + '\nfi')
            script.append(f'ls -A {target}')
            done = subprocess.run(['unshare', '-m', 'bash', '-c', '\n'.join(script)], capture_output=True, text=True)
            self.assertEqual(done.stdout.split(), ['library.db'], done.stderr)
            self.assertEqual((present.parent / 'jellyfin.before' / 'made-after-the-backup').read_text(), 'present')
            self.assertEqual((present.parent / 'jellyfin.before' / 'library.db').read_text(), 'present')
            self.assertEqual((present / 'library.db').read_text(), 'restored')

    def test_the_steps_put_the_settings_back_with_the_app_on_the_ssd(self):
        self.follow_the_steps(on_its_drive=False)

    def test_the_steps_put_the_settings_back_with_the_app_on_a_drive_of_its_own(self):
        """Deep check 791: with Jellyfin installed on a drive of its own, its folder on the
        SSD is a mount point; `mv` refused it ("Device or resource busy"), and the `cp`
        after it poured the backup into the present settings: files made since stayed
        mixed in, and nothing was put aside to go back to."""
        self.follow_the_steps(on_its_drive=True)

    def test_the_containers_and_folders_of_the_steps_are_the_ones_setup_makes(self):
        """The app's container is its Compose project and service (neko-<app>, <app>), and
        its settings are appdata/<app> on the SSD, as the snapshot keeps them."""
        self.assertIn("project neko-{name}", (ROOT / 'modules/media.py').read_text())
        self.assertIn('"${NEKO_DATA}/appdata/jellyfin:/config"', (ROOT / 'compose/jellyfin.yaml').read_text())
        self.assertNotIn('container_name', (ROOT / 'compose/jellyfin.yaml').read_text())
        self.assertIn('("appdata", devices.settings(service))', (ROOT / 'runtime/src/app_snapshot.rs').read_text())


class GatewayGuideTests(unittest.TestCase):
    """Deep check plan 386: README.md says of the browser's certificate warning
    "GATEWAY.md explains how to trust it"; GATEWAY.md said "Export instructions are in
    gateway_cert.py --help", and that help had no instructions."""

    def test_the_guide_says_what_to_do_about_the_warning(self):
        text = doc('GATEWAY.md')
        part = text.split('## The warning at the home address')[1].split('\n## ')[0]
        self.assertLess(text.index('## The warning at the home address'), text.index('## For administrators'))
        self.assertIn('sudo python3 gateway_cert.py --export ~/nekoserver-root.crt', part)
        for device in ('Windows', 'macOS', 'iPhone', 'Android', 'Firefox'):
            self.assertIn(device, part)
        self.assertNotIn('Export instructions are in gateway_cert.py --help', text)
        self.assertIn('(GATEWAY.md#the-warning-at-the-home-address)', doc('README.md'))

    def test_the_file_name_of_the_example_is_one_the_tool_takes(self):
        import tempfile

        import gateway_cert

        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(gateway_cert.export_target(home + '/nekoserver-root.crt').name, 'nekoserver-root.crt')

    def test_the_home_network_is_not_selected_by_hand(self):
        self.assertNotIn('Select the physical home interface', doc('GATEWAY.md'))

    def test_a_tv_is_sent_only_where_it_can_go(self):
        """Deep check 553-555: "if an app refuses the certificate, use its Tailscale address"
        sent an LG, Samsung or Roku TV owner to a Tailscale app those TVs don't have (their
        Jellyfin apps can't take the server's certificate either), and casting from the
        phone, which fails on both addresses, was not mentioned."""
        text = doc('GATEWAY.md')
        self.assertLess(text.index('## On a TV'), text.index('## For administrators'))
        part = text.split('## On a TV')[1].split('\n## ')[0]
        for tv in ('LG', 'Samsung', 'Roku', 'Android TV', 'Apple TV', 'Fire TV'):
            self.assertIn(tv, part)
        self.assertIn('no Tailscale app', part)
        self.assertIn('HDMI', part)
        self.assertIn('Casting from the phone', part)
        self.assertIn('(GATEWAY.md#on-a-tv)', doc('README.md'))

    def test_a_tv_signs_in_without_typing_the_password(self):
        """Deep check 553: setup makes Jellyfin's password 32 random letters, and a TV app
        asked for it to be typed with the remote. Jellyfin's Quick Connect (on by default)
        signs the TV in with a code entered on the phone instead."""
        part = doc('GATEWAY.md').split('## On a TV')[1].split('\n## ')[0]
        self.assertIn('Quick Connect', part)
        self.assertIn('code', part)


class ToolHelpTests(unittest.TestCase):
    """Deep check plan 388: --help of the tools the guides tell a person to run. recover.py
    listed seven commands and eight options without a word on any of them, acceptance.py
    --save and --manual, and so on: the reader had to go back to the guide for each."""

    # The tool, and the commands of it that have options of their own.
    TOOLS = {
        'installer.py': (),
        'acceptance.py': (),
        'account-setup.py': (),
        'backup_secrets.py': (),
        'gateway_cert.py': (),
        'migrate.py': (),
        'recover.py': ('check', 'checkpoint', 'reattach', 'snapshot', 'restore-test', 'resume', 'install-docker'),
        'neko_restore.py': ('list', 'verify', 'restore', 'where'),
        'upgrade.py': (),
        'system_updates.py': (),
        'app-recovery.py': (),
        'host-recovery.py': (),
        'monthly-recovery.py': (),
        'music-recovery.py': (),
        'music-setup.py': (),
    }
    # An option, or a command of a list of commands, on a line of its own: nothing said.
    BARE = re.compile(r'^ {2,4}((?:-\w, )?--[\w-]+(?: \S+)?|[a-z][\w-]+)\s*$')

    def help_of(self, tool, *command):
        import subprocess

        done = subprocess.run(
            [sys.executable, str(ROOT / tool), *command, '--help'], capture_output=True, text=True, check=False
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def unexplained(self, text):
        lines = text.splitlines()
        return [
            found.group(1)
            for n, line in enumerate(lines)
            if (found := self.BARE.match(line)) and not (n + 1 < len(lines) and lines[n + 1].startswith(' ' * 6))
        ]

    def test_every_option_and_command_says_what_it_is(self):
        for tool, commands in self.TOOLS.items():
            for command in ((), *((name,) for name in commands)):
                with self.subTest(tool=tool, command=command):
                    self.assertEqual(self.unexplained(self.help_of(tool, *command)), [])

    def test_recovery_md_has_a_row_for_every_command_of_recover_py(self):
        """ "RECOVERY.md explains each command": its table had no row for resume and
        install-docker."""
        import recover

        table = doc('RECOVERY.md').split('### What each operation does')[1].split('\n### ')[0]
        self.assertGreater(len(recover.COMMANDS), 6)
        for command in recover.COMMANDS:
            self.assertRegex(table, rf'(?m)^\| {command} \|')
        self.assertIn('RECOVERY.md, "What each operation does"', recover.EPILOG)

    def test_the_settings_file_is_explained_once_for_every_tool(self):
        import neko_common

        for tool in ('acceptance.py', 'account-setup.py', 'recover.py', 'migrate.py', 'music-setup.py'):
            self.assertIn(' '.join(neko_common.SETTINGS_HELP.split()[:6]), ' '.join(self.help_of(tool).split()))


if __name__ == '__main__':
    unittest.main()
