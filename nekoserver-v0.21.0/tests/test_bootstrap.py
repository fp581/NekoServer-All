"""Exercise the entrypoint with an isolated command PATH, never the host pacman."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = 'lsblk findmnt blkid mount umount flock runuser sha256sum du id install stat cp chmod chown df timeout tar curl openssl ip lspci systemctl'.split()


class BootstrapTests(unittest.TestCase):
    def launch(self, args, missing=(), repo_failure=False):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            source = (ROOT / 'install.sh').read_text().replace('/etc/os-release', str(root / 'os-release'))
            (root / 'os-release').write_text('ID=arch\n')
            (root / 'install.sh').write_text(source)
            (root / 'installer.py').write_text('')
            (bin_dir / 'dirname').symlink_to('/usr/bin/dirname')
            for name in COMMANDS:
                if name not in missing:
                    (bin_dir / name).symlink_to('/bin/true')
            (bin_dir / 'uname').write_text('#!/bin/bash\necho x86_64\n')
            python = '#!/bin/bash\nif [[ "$1" != -c ]]; then printf "%s\\n" "$*" >> "$TRACE"; fi\n'
            (root / 'python-template').write_text(python)
            (root / 'python-template').chmod(0o755)
            if 'python3' not in missing:
                (bin_dir / 'python3').symlink_to(root / 'python-template')
            pacman = '#!/bin/bash\nprintf "%s\\n" "$*" >> "$TRACE"\n'
            if repo_failure:
                pacman += 'exit 1\n'
            else:
                pacman += 'if [[ "$1" == -S ]]; then /bin/cp "$TEMPLATE" "$TOOLS/python3"; /bin/ln -sf /bin/true "$TOOLS/curl"; fi\n'
            (bin_dir / 'pacman').write_text(pacman)
            for name in ('uname', 'pacman'):
                (bin_dir / name).chmod(0o755)
            trace = root / 'trace'
            result = subprocess.run(['/bin/bash', str(root / 'install.sh'), *args], text=True, capture_output=True,
                                    env={**os.environ, 'PATH': str(bin_dir), 'TRACE': str(trace), 'TOOLS': str(bin_dir), 'TEMPLATE': str(root / 'python-template')})
            return result, trace.read_text() if trace.exists() else ''

    @unittest.skipUnless(os.geteuid() == 0, 'apply fixture requires root shell')
    def test_fetches_missing_packages_before_installer(self):
        result, trace = self.launch(['--profile', 'full', '--apply'], missing=('python3', 'curl'))
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = trace.splitlines()
        self.assertIn('-Si -- python curl', lines[0])
        self.assertIn('-S --needed --noconfirm -- python curl', lines[1])
        self.assertIn('installer.py --profile full --apply', lines[2])
        self.assertNotIn('-Sy', trace)

    @unittest.skipUnless(os.geteuid() == 0, 'apply fixture requires root shell')
    def test_unavailable_package_blocks_installer(self):
        result, trace = self.launch(['--apply'], missing=('python3',), repo_failure=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('installer.py', trace)
        self.assertNotIn('-S --needed', trace)

    def test_read_only_never_installs_missing_python(self):
        for mode in ('--check', '--dry-run'):
            result, trace = self.launch([mode], missing=('python3',))
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(trace, '')
            self.assertIn('never install', result.stderr)

    def test_conflicting_modes_do_not_install(self):
        for mode in ('--check', '--dry-run', '--help'):
            result, trace = self.launch(['--apply', mode], missing=('python3',))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(trace, '')

    def test_check_forwards_without_package_writes(self):
        result, trace = self.launch(['--check', '--config', 'my-config.json'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('installer.py --check --config my-config.json', trace)
        self.assertNotIn('-S', trace)
