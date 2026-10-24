"""The runtime build (runtime/build.py): static when the toolchain can, else for this PC."""
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

        def __init__(self):
            self.installed = []

        def install(self, wanted, timeout=900):
            self.installed.append(list(wanted))

    def test_the_plain_rust_when_it_is_new_enough(self):
        import host
        family = self.Family()
        host.toolchain(family, log=lambda line: None, oldest=(1, 85), version=lambda: (1, 85))
        self.assertEqual(family.installed, [['rust', 'gcc']])

    def test_ubuntus_versioned_rust_when_the_plain_one_is_too_old(self):
        import host
        family = self.Family()
        host.toolchain(family, log=lambda line: None, oldest=(1, 85), version=lambda: (1, 75))
        self.assertEqual(family.installed, [['rust', 'gcc'], ['rustc-1.85', 'cargo-1.85']])

    def test_another_family_with_too_old_a_rust_is_told(self):
        import host
        family = self.Family()
        family.name = 'fedora'
        with self.assertRaisesRegex(RuntimeError, 'Rust 1.85 or newer is needed .* this system has 1.70'):
            host.toolchain(family, log=lambda line: None, oldest=(1, 85), version=lambda: (1, 70))

    def test_the_oldest_rust_comes_from_cargo_toml(self):
        import host
        self.assertGreaterEqual(host.oldest_rust(), (1, 85))
        self.assertEqual(host.rustc_version(run=lambda *a, **k: mock.Mock(stdout='rustc 1.85.1 (4eb161250 2025-03-15)')), (1, 85))
        self.assertIsNone(host.rustc_version(run=mock.Mock(side_effect=OSError)))


if __name__ == '__main__':
    unittest.main()
