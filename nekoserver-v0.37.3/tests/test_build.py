"""The runtime build (runtime/build.py): static when the toolchain can, else for this PC."""
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
        with tempfile.TemporaryDirectory() as tmp:
            root = self.project(tmp)
            self.assertTrue(native_runtime.build_needed(tmp))          # no runtime at all
            self.record(root)
            self.assertFalse(native_runtime.build_needed(tmp))         # built from these sources
            (root / 'src/main.rs').write_text('fn main() { println!(); }\n')
            self.assertTrue(native_runtime.build_needed(tmp))          # a source changed since
            self.record(root)
            (root / 'src/extra.rs').write_text('')
            self.assertTrue(native_runtime.build_needed(tmp))          # a source the record never saw
            (root / 'src/extra.rs').unlink()
            (root / 'bin/nekoserver-runtime').unlink()
            self.assertTrue(native_runtime.build_needed(tmp))          # the record without its binary
            (root / 'build.json').write_text('{')
            self.assertTrue(native_runtime.build_needed(tmp))          # an unreadable record

    def test_this_pcs_own_build_is_accepted_as_the_static_one_is(self):
        import native_runtime
        self.assertIn('x86_64-unknown-linux-gnu', native_runtime.TARGETS)
        self.assertIn(native_runtime.TARGET, native_runtime.TARGETS)


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
