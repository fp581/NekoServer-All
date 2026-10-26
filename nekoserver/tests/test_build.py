"""The runtime build (runtime/build.py): static when the toolchain can, else for this PC."""
import contextlib
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('runtime_build', ROOT / 'runtime/build.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class SpaceTests(unittest.TestCase):
    """The build stops with a clear message before Cargo fills a nearly full disk."""
    class Stat:
        def __init__(self, free_mb):
            self.f_bavail, self.f_frsize = free_mb * 256, 4096

    def test_free_space_is_read_where_the_runtime_folder_is(self):
        seen = []
        self.assertEqual(build.free_megabytes(Path('/x'), statvfs=lambda p: seen.append(p) or self.Stat(2048)), 2048)
        self.assertEqual(seen, [Path('/x')])

    def test_the_build_refuses_a_nearly_full_disk_before_compiling(self):
        with mock.patch.object(build, 'free_megabytes', return_value=300), \
                mock.patch.object(build.os, 'geteuid', return_value=1000), \
                mock.patch.object(build.platform, 'system', return_value='Linux'), \
                mock.patch.object(build.platform, 'machine', return_value='x86_64'), \
                mock.patch.object(build.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, r'needs about 1024 MB free .*; 300 MB is free'):
                build.main()
            run.assert_not_called()


class FailureTests(unittest.TestCase):
    """A build that fails says so in one line; only the downloads are tried again."""
    def test_the_dependencies_are_downloaded_again_before_an_offline_build(self):
        results, slept, said = [1, 101, 0], [], []
        run = mock.Mock(side_effect=lambda argv, **kwargs: mock.Mock(returncode=results.pop(0)))
        build.fetch('cargo', [], run=run, sleep=slept.append, say=said.append)
        self.assertEqual([c.args[0] for c in run.call_args_list], [['cargo', 'fetch', '--locked']] * 3)
        self.assertEqual(slept, [build.FETCH_PAUSE] * 2)
        self.assertIn('2 of 3', said[0])
        run = mock.Mock(return_value=mock.Mock(returncode=101))
        with self.assertRaisesRegex(RuntimeError, r'dependencies failed 3 times .*connected to the internet'):
            build.fetch('cargo', ['--target', build.STATIC], run=run, sleep=slept.append, say=said.append)
        self.assertEqual(run.call_args.args[0], ['cargo', 'fetch', '--locked', '--target', build.STATIC])

    def test_the_build_itself_needs_no_network_and_fails_in_one_line(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(returncode=101 if 'build' in argv else 0)
        with mock.patch.object(build, 'free_megabytes', return_value=4096), \
                mock.patch.object(build.os, 'geteuid', return_value=1000), \
                mock.patch.object(build.platform, 'system', return_value='Linux'), \
                mock.patch.object(build.platform, 'machine', return_value='x86_64'), \
                mock.patch.object(build, 'toolchain', return_value=('cargo', 'rustc', 'host', Path('/none'), 'rustc 1.90')), \
                mock.patch.object(build.subprocess, 'run', side_effect=run), \
                mock.patch('sys.stderr') as stderr:
            self.assertEqual(build.cli(), 1)
        self.assertIn(['cargo', 'build', '--release', '--locked', '--offline'], calls)
        self.assertLess(calls.index(['cargo', 'fetch', '--locked']), calls.index(['cargo', 'build', '--release', '--locked', '--offline']))
        # The retried download is the first cargo call, and none after it uses the network
        # (cleaning reads crates.io's index too, so it once failed first, called a failed clean).
        cargo_calls = [argv for argv in calls if argv[0] == 'cargo']
        self.assertEqual(cargo_calls[0], ['cargo', 'fetch', '--locked'])
        self.assertTrue(all('--offline' in argv for argv in cargo_calls[1:]), cargo_calls)
        self.assertIn(['cargo', 'clean', '--release', '--offline', '-p', 'nekoserver-runtime'], cargo_calls)
        printed = ''.join(c.args[0] for c in stderr.write.call_args_list)
        self.assertIn('ERROR: building the runtime: Compiling the runtime failed', printed)
        self.assertNotIn('Traceback', printed)

    def test_files_are_replaced_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'nekoserver-runtime'
            target.write_bytes(b'old')
            # Cut off just before the rename.
            with mock.patch.object(build.os, 'replace', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
                build.replace_file(target, b'new build', 0o750)
            self.assertEqual(target.read_bytes(), b'old')
            build.replace_file(target, b'new build', 0o750)
            self.assertEqual((target.read_bytes(), target.stat().st_mode & 0o777), (b'new build', 0o750))


class BuiltPathTests(unittest.TestCase):
    """The executable is read from where cargo put it: a CARGO_TARGET_DIR in the
    environment moves it (the rig's shared build folder, a developer's own)."""
    def test_the_runtime_folder_by_default_and_the_shared_folder_when_set(self):
        self.assertEqual(build.built_path('x86_64-unknown-linux-gnu', [], environ={}),
                         build.ROOT / 'target/release/nekoserver-runtime')
        self.assertEqual(build.built_path(build.STATIC, ['--target', build.STATIC], environ={}),
                         build.ROOT / 'target' / build.STATIC / 'release/nekoserver-runtime')
        self.assertEqual(build.built_path(build.STATIC, ['--target', build.STATIC], environ={'CARGO_TARGET_DIR': '/home/me/shared'}),
                         Path('/home/me/shared') / build.STATIC / 'release/nekoserver-runtime')
        self.assertEqual(build.built_path('host', [], environ={'CARGO_TARGET_DIR': ''}), build.ROOT / 'target/release/nekoserver-runtime')


class PlanTests(unittest.TestCase):
    def test_static_only_with_the_musl_library_and_rust_lld(self):
        with tempfile.TemporaryDirectory() as tmp:
            sysroot = Path(tmp)
            host = 'x86_64-unknown-linux-gnu'
            # A distribution's toolchain: the host library alone.
            (sysroot / 'lib/rustlib' / host / 'lib').mkdir(parents=True)
            self.assertEqual(build.plan(host, sysroot), (host, []))
            # The musl library without rust-lld: still this PC's own build.
            (sysroot / 'lib/rustlib' / build.STATIC / 'lib').mkdir(parents=True)
            self.assertEqual(build.plan(host, sysroot), (host, []))
            # Both: the static release build, linked by rust-lld.
            linker = sysroot / 'lib/rustlib' / host / 'bin/rust-lld'
            linker.parent.mkdir(parents=True)
            linker.write_text('')
            target, arguments = build.plan(host, sysroot)
            self.assertEqual(target, build.STATIC)
            self.assertEqual(arguments[:2], ['--target', build.STATIC])
            self.assertEqual(arguments[3], 'target.' + build.STATIC + '.linker=' + json.dumps(str(linker)))

    def test_a_rust_new_enough_is_found_under_its_plain_or_versioned_name(self):
        # Ubuntu 24.04: rustc 1.75 as "rustc", and 1.85 beside it as "rustc-1.85".
        on_path = {'cargo': '/usr/bin/cargo', 'rustc': '/usr/bin/rustc', 'cargo-1.85': '/usr/bin/cargo-1.85',
                   'rustc-1.85': '/usr/bin/rustc-1.85', 'cargo-1.91': '/usr/bin/cargo-1.91'}   # 1.91 without its rustc
        found = build.pairs(which=on_path.get, listing=['cargo-1.85', 'cargo-1.91', 'cargo-1.75'])
        self.assertEqual(found, [('/usr/bin/cargo', '/usr/bin/rustc'), ('/usr/bin/cargo-1.85', '/usr/bin/rustc-1.85')])
        self.assertEqual(build.pairs(which=lambda name: None, listing=[]), [])
        self.assertGreaterEqual(build.oldest_rust(), (1, 85))

    def test_without_rust_the_build_says_what_to_install(self):
        with mock.patch.object(build, 'pairs', return_value=[]):
            with self.assertRaisesRegex(RuntimeError, r'Rust 1\.\d+ or newer .* sudo bash install.sh installs it'):
                build.toolchain()


class BuildNeededTests(unittest.TestCase):
    """install.sh builds the runtime when the release folder has none, or one built from
    other sources than these."""
    def project(self, tmp):
        root = Path(tmp) / 'runtime'
        (root / 'src').mkdir(parents=True)
        (root / 'bin').mkdir()
        for name, text in (('Cargo.toml', '[package]\n'), ('Cargo.lock', '# lock\n'), ('build.py', '# build\n'), ('src/main.rs', 'fn main() {}\n')):
            (root / name).write_text(text)
        return root

    def record(self, root, binary=b'\x7fELF\x02\x01x'):
        import hashlib
        (root / 'bin/nekoserver-runtime').write_bytes(binary)
        sources = ['Cargo.toml', 'Cargo.lock', 'build.py', 'src/main.rs']
        (root / 'build.json').write_text(json.dumps({
            'version': '0', 'target': 'x86_64-unknown-linux-gnu', 'binary_sha256': hashlib.sha256(binary).hexdigest(),
            'source_sha256': {rel: hashlib.sha256((root / rel).read_bytes()).hexdigest() for rel in sources}}))

    def test_none_yet_other_sources_or_a_matching_build(self):
        import native_runtime
        from neko_common import VERSION
        runs = lambda *args, **kwargs: mock.Mock(returncode=0, stdout='nekoserver-runtime ' + VERSION + '\n')   # noqa: E731
        with tempfile.TemporaryDirectory() as tmp:
            root = self.project(tmp)
            self.assertTrue(native_runtime.build_needed(tmp, runs))          # no runtime at all
            self.record(root)
            self.assertFalse(native_runtime.build_needed(tmp, runs))         # built from these sources, runs here
            (root / 'src/main.rs').write_text('fn main() { println!(); }\n')
            self.assertTrue(native_runtime.build_needed(tmp, runs))          # a source changed since
            self.record(root)
            (root / 'src/extra.rs').write_text('')
            self.assertTrue(native_runtime.build_needed(tmp, runs))          # a source the record never saw
            (root / 'src/extra.rs').unlink()
            (root / 'bin/nekoserver-runtime').unlink()
            self.assertTrue(native_runtime.build_needed(tmp, runs))          # the record without its binary
            (root / 'build.json').write_text('{')
            self.assertTrue(native_runtime.build_needed(tmp, runs))          # an unreadable record

    def test_a_binary_its_record_does_not_describe_is_built_again(self):
        # A build cut off after the new binary was written, before its record was.
        import native_runtime
        from neko_common import VERSION

        def runs(*args, **kwargs):
            return mock.Mock(returncode=0, stdout='nekoserver-runtime ' + VERSION + '\n')
        with tempfile.TemporaryDirectory() as tmp:
            root = self.project(tmp)
            self.record(root)
            self.assertFalse(native_runtime.build_needed(tmp, runs))
            (root / 'bin/nekoserver-runtime').write_bytes(b'\x7fELF\x02\x01another build')
            self.assertTrue(native_runtime.build_needed(tmp, runs))

    def test_a_runtime_that_does_not_run_here_is_built_again(self):
        # A copy built on another Linux (kept on the SSD through a reinstall), or of
        # another version, counts as missing.
        import native_runtime
        from neko_common import VERSION
        with tempfile.TemporaryDirectory() as tmp:
            root = self.project(tmp)
            self.record(root)
            cannot_start = lambda *args, **kwargs: (_ for _ in ()).throw(OSError('Exec format error'))   # noqa: E731
            self.assertTrue(native_runtime.build_needed(tmp, cannot_start))
            other = lambda *args, **kwargs: mock.Mock(returncode=0, stdout='nekoserver-runtime 0.1.0\n')   # noqa: E731
            self.assertTrue(native_runtime.build_needed(tmp, other))
            crashes = lambda *args, **kwargs: mock.Mock(returncode=127, stdout='')   # noqa: E731
            self.assertTrue(native_runtime.build_needed(tmp, crashes))
            self.assertTrue(native_runtime.runs_here(root / 'bin/nekoserver-runtime',
                                                     lambda *a, **k: mock.Mock(returncode=0, stdout='nekoserver-runtime ' + VERSION)))

    def test_this_pcs_own_build_is_accepted_as_the_static_one_is(self):
        import native_runtime
        self.assertIn('x86_64-unknown-linux-gnu', native_runtime.TARGETS)
        self.assertIn(native_runtime.TARGET, native_runtime.TARGETS)


class DeployTests(unittest.TestCase):
    """A deployed copy carries its build record; one NekoServer put there is replaced when
    the runtime was built again, one without a record never is."""
    def setUp(self):
        import native_runtime
        self.native_runtime = native_runtime
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / 'nekoserver'
        (self.project / 'runtime').mkdir(parents=True)
        self.manifest = self.project / 'runtime/build.json'
        self.manifest.write_text('{"version": "x", "binary_sha256": "1"}\n')
        self.destination = Path(self.tmp.name) / 'lib/v1'
        self.destination.mkdir(parents=True)
        self.target = self.destination / native_runtime.NAME
        self.record = Path(str(self.target) + native_runtime.RECORD_SUFFIX)

    def deploy(self, blob):
        # The release's record names the build being deployed, as build.py writes it.
        self.manifest.write_text(json.dumps({'version': 'x', 'binary_sha256': hashlib.sha256(blob).hexdigest()}) + '\n')
        with mock.patch.object(self.native_runtime, 'payload', return_value=blob):
            return self.native_runtime.deploy(self.project, {}, self.destination)

    def test_a_copy_someone_replaced_keeps_its_place_even_with_the_record_beside_it(self):
        self.deploy(b'one')
        self.target.write_bytes(b'their own build')
        with self.assertRaisesRegex(RuntimeError, 'NekoServer did not put it there'):
            self.deploy(b'two')
        self.assertEqual(self.target.read_bytes(), b'their own build')

    def test_a_record_that_cannot_be_read_protects_the_copy(self):
        self.deploy(b'one')
        self.record.write_text('not json')
        with self.assertRaisesRegex(RuntimeError, 'NekoServer did not put it there'):
            self.deploy(b'two')
        self.assertEqual(self.target.read_bytes(), b'one')

    def test_a_stale_record_protects_the_copy(self):
        self.deploy(b'one')
        self.record.write_text('{"version": "x", "binary_sha256": "another build"}')
        with self.assertRaisesRegex(RuntimeError, 'NekoServer did not put it there'):
            self.deploy(b'two')
        self.assertEqual(self.target.read_bytes(), b'one')
        # Deploying the same bytes again only refreshes the record.
        self.deploy(b'one')
        self.assertEqual(self.record.read_text(), self.manifest.read_text())

    def test_first_deployment_writes_the_copy_and_its_record(self):
        self.assertEqual(self.deploy(b'one'), self.target)
        self.assertEqual(self.target.read_bytes(), b'one')
        self.assertEqual(self.record.read_text(), self.manifest.read_text())
        self.assertEqual(oct(self.target.stat().st_mode & 0o777), '0o750')

    def test_a_rebuilt_runtime_replaces_the_copy_nekoserver_made(self):
        self.deploy(b'one')
        self.manifest.write_text('{"version": "x", "binary_sha256": "2"}\n')
        self.deploy(b'two')
        self.assertEqual(self.target.read_bytes(), b'two')
        self.assertEqual(self.record.read_text(), self.manifest.read_text())

    def test_a_copy_without_a_record_is_never_overwritten(self):
        self.target.write_bytes(b'theirs')
        self.target.chmod(0o750)
        with self.assertRaisesRegex(RuntimeError, 'NekoServer did not put it there'):
            self.deploy(b'ours')
        self.assertEqual(self.target.read_bytes(), b'theirs')
        self.assertFalse(self.record.exists())
        # The same bytes, though, are simply recognised (and get their record).
        self.deploy(b'theirs')
        self.assertTrue(self.record.is_file())


class ToolchainTests(unittest.TestCase):
    """python3 host.py toolchain: Rust and gcc from the family, and Ubuntu's versioned
    newer Rust when the plain one is too old."""
    class Family:
        name = 'debian'

        def __init__(self, ubuntu=True, offers=None, has=()):
            self.installed = []
            self.is_ubuntu = ubuntu
            self.offers = offers or {'rustc': (1, 75)}   # apt's candidate versions
            self.present = set(has)

        def ubuntu(self):
            return self.is_ubuntu

        def candidate(self, package):
            return self.offers.get(package)

        def has(self, package):
            return package in self.present

        def install(self, wanted, timeout=900):
            self.installed.append(list(wanted))

    def test_the_plain_rust_when_it_is_new_enough(self):
        import host
        family = self.Family()
        host.toolchain(family, log=lambda line: None, oldest=(1, 85), version=lambda: (1, 85), which=lambda name: '/usr/bin/' + name)
        self.assertEqual(family.installed, [['gcc']])     # the Rust already there stays as it is

    def test_debian_12s_rustc_web_already_there_is_kept_on_every_later_run(self):
        """An update, or a retry after the build failed: rustc-web provides rustc but is not
        the package rustc, and asking apt for rustc would replace it with the older 1.63."""
        import host
        family = self.Family(ubuntu=False, offers={'rustc': (1, 63), 'rustc-web': (1, 85)}, has={'rustc-web', 'cargo-web', 'gcc'})
        host.toolchain(family, log=lambda line: None, oldest=(1, 85), version=lambda: (1, 85), which=lambda name: '/usr/bin/' + name)
        self.assertEqual(family.installed, [['gcc']])
        self.assertFalse(any(name in ('rust', 'rustc', 'cargo') for names in family.installed for name in names))

    def test_a_new_enough_rustc_without_cargo_gets_the_cargo_that_goes_with_it(self):
        import host
        family = self.Family(ubuntu=False, offers={'rustc': (1, 63), 'rustc-web': (1, 85)}, has={'rustc-web'})
        host.toolchain(family, log=lambda line: None, oldest=(1, 85), version=lambda: (1, 85), which=lambda name: None)
        self.assertEqual(family.installed, [['gcc', 'cargo-web']])
        family = self.Family()
        family.name = 'fedora'
        host.toolchain(family, log=lambda line: None, oldest=(1, 85), version=lambda: (1, 85), which=lambda name: None)
        self.assertEqual(family.installed, [['gcc', 'rust']])

    def test_following_the_switch_advice_ends_the_loop_through_real_apt(self):
        """Through the real Debian family: after  apt install rustc-web cargo-web  (the
        advice when an older rustc is installed) the next run asks apt for nothing of Rust's."""
        import host
        apt = []
        present = {'rustc-web', 'cargo-web'}

        def run(argv, **kw):
            if argv[0] == 'dpkg-query':
                there = argv[-1] in present
                return mock.Mock(returncode=0 if there else 1, stdout='install ok installed' if there else '', stderr='')
            apt.append(argv) if argv[0] == 'apt-get' else None
            return mock.Mock(returncode=0, stdout='', stderr='')
        with mock.patch('host.shutil.which', return_value='/usr/bin/apt-get'), mock.patch('host.setup_progress.packages'):
            family = host.Debian(run, {'ID': 'debian', 'VERSION_ID': '12', 'VERSION_CODENAME': 'bookworm'})
            family.refreshed, family.journal, family._reconcile = True, None, lambda: None
            family.prepare, family.protect = (lambda *a: {}), (lambda *a: None)
            family.locked = contextlib.nullcontext      # not this test PC's package lock
            host.toolchain(family, log=lambda line: None, oldest=(1, 85), version=lambda: (1, 85), which=lambda name: '/usr/bin/' + name)
        installs = [argv for argv in apt if 'install' in argv]
        self.assertEqual(len(installs), 1)
        self.assertEqual(installs[0][-1], 'gcc')
        self.assertFalse({'rustc', 'cargo'} & set(installs[0]))

    def test_ubuntus_versioned_rust_when_the_plain_one_is_too_old(self):
        import host
        family = self.Family()
        host.toolchain(family, log=lambda line: None, oldest=(1, 85), version=lambda: (1, 75))
        self.assertEqual(family.installed, [['rust', 'gcc'], ['rustc-1.85', 'cargo-1.85']])

    def test_debian_12_gets_rustc_web_debians_newer_rust(self):
        """The reported case: stock Debian 12 has rustc 1.63, and the versioned names Ubuntu
        uses (rustc-1.85) do not exist there."""
        import host
        family = self.Family(ubuntu=False, offers={'rustc': (1, 63), 'rustc-web': (1, 85)})
        version = lambda: (1, 85) if family.installed else None   # rustc appears once installed
        host.toolchain(family, log=lambda line: None, oldest=(1, 85), version=version)
        self.assertEqual(family.installed, [['gcc', 'rustc-web', 'cargo-web']])
        self.assertFalse(any('rustc-1.85' in names for names in family.installed))

    def test_debian_12_with_an_older_rust_installed_is_told_how_to_switch(self):
        import host
        family = self.Family(ubuntu=False, offers={'rustc': (1, 63), 'rustc-web': (1, 96)}, has={'rustc', 'cargo'})
        with self.assertRaisesRegex(RuntimeError, r'older than 1.85.*sudo apt install rustc-web cargo-web'):
            host.toolchain(family, log=lambda line: None, oldest=(1, 85), version=lambda: (1, 63))
        self.assertEqual(family.installed, [])        # nothing replaced behind your back

    def test_debian_without_a_new_enough_rust_says_what_to_check(self):
        import host
        family = self.Family(ubuntu=False, offers={'rustc': (1, 63)})
        with self.assertRaisesRegex(RuntimeError, 'rustc-web is not available.*security repositories.*Debian 13'):
            host.toolchain(family, log=lambda line: None, oldest=(1, 85), version=lambda: None)

    def test_debian_13s_own_rust_is_used(self):
        import host
        family = self.Family(ubuntu=False, offers={'rustc': (1, 85)})
        version = lambda: (1, 85) if family.installed else None
        host.toolchain(family, log=lambda line: None, oldest=(1, 85), version=version)
        self.assertEqual(family.installed, [['rust', 'gcc']])

    def test_another_family_with_too_old_a_rust_is_told(self):
        import host
        family = self.Family()
        family.name = 'fedora'
        with self.assertRaisesRegex(RuntimeError, 'Rust 1.85 or newer is needed .* this system has 1.70'):
            host.toolchain(family, log=lambda line: None, oldest=(1, 85), version=lambda: (1, 70))

    def test_apts_candidate_version_is_read(self):
        import host
        policies = {'rustc': 'rustc:\n  Installed: (none)\n  Candidate: 1.63.0+dfsg1-2\n',
                    'rustc-web': 'rustc-web:\n  Installed: (none)\n  Candidate: 1:1.96.0+dfsg1-1~deb12u2\n',
                    'nothing': 'nothing:\n  Installed: (none)\n  Candidate: (none)\n'}

        def run(argv, **kw):
            return mock.Mock(returncode=0, stdout=policies.get(argv[-1], '') if argv[0] == 'apt-cache' else '')
        with mock.patch('host.shutil.which', return_value='/usr/bin/apt-get'):
            family = host.Debian(run, {'ID': 'debian', 'VERSION_ID': '12', 'VERSION_CODENAME': 'bookworm'})
            family.refreshed = True     # the lists' lock of the PC running the tests is not asked
            self.assertEqual(family.candidate('rustc'), (1, 63))
            self.assertEqual(family.candidate('rustc-web'), (1, 96))
            self.assertIsNone(family.candidate('nothing'))
            self.assertFalse(family.ubuntu())

    def test_the_oldest_rust_comes_from_cargo_toml(self):
        import host
        self.assertGreaterEqual(host.oldest_rust(), (1, 85))
        self.assertEqual(host.rustc_version(run=lambda *a, **k: mock.Mock(stdout='rustc 1.85.1 (4eb161250 2025-03-15)')), (1, 85))
        self.assertIsNone(host.rustc_version(run=mock.Mock(side_effect=OSError)))


if __name__ == '__main__':
    unittest.main()
