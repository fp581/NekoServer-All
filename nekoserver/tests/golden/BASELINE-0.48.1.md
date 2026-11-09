# Baseline before the 0.48.2 cleanup (release 0.48.1, main at c9f021c)

Measured on 2026-11-09 on the NekoUbuntu test distro (Python 3.12.3, coverage.py 7.17.2),
on a copy of `nekoserver/` from this branch with `tests/test_golden_units.py` taken out
(the numbers are the tree's before this PR). The cleanup PR shows its after-numbers
next to these.

## Python test suite

```
python3 -m unittest discover -s tests
Ran 1477 tests in 83.488s
OK (skipped=74)
```

With the golden-file tests of this PR: `Ran 1483 tests`, `OK (skipped=74)`.

## Python coverage

```
python3 -m coverage run --source=. --omit='tests/*,lab/*,runtime/*' -m unittest discover -s tests
python3 -m coverage report
TOTAL                            19319   4113    79%
```

19 319 statements in 83 files, 4 113 not run: 78.7 % (79.2 % with the golden-file tests,
which run the producers of `media_monitor`, `host_backup` and `server_os`).

Files worth watching (statements, missed, covered):

| File | Stmts | Miss | Cover |
| --- | ---: | ---: | ---: |
| `neko_common.py` | 888 | 72 | 92% |
| `host.py` | 1109 | 62 | 94% |
| `recovery_wizard.py` | 1409 | 131 | 91% |
| `installer.py` | 460 | 72 | 84% |
| `upgrade.py` | 502 | 59 | 88% |
| `migrate.py` | 577 | 106 | 82% |
| `setup_gui.py` | 402 | 48 | 88% |
| `acceptance.py` | 622 | 130 | 79% |
| `gateway_tools.py` | 585 | 157 | 73% |
| `native_runtime.py` | 258 | 102 | 60% |
| `modules/storage.py` | 766 | 92 | 88% |
| `modules/network_storage.py` | 410 | 59 | 86% |
| `modules/nextcloud.py` | 302 | 39 | 87% |
| `modules/photo_backup.py` | 248 | 33 | 87% |
| `modules/media.py` | 249 | 46 | 82% |
| `modules/dashboard.py` | 419 | 84 | 80% |
| `modules/samba.py` | 216 | 58 | 73% |
| `modules/maintenance.py` | 116 | 33 | 72% |
| `modules/photos.py` | 171 | 50 | 71% |
| `modules/server_os.py` | 223 | 73 | 67% |
| `modules/host_backup.py` | 153 | 53 | 65% |
| `modules/docker.py` | 214 | 85 | 60% |
| `modules/tailscale.py` | 64 | 27 | 58% |
| `modules/tv.py` | 260 | 126 | 52% |
| `modules/music_backup.py` | 36 | 25 | 31% |
| `modules/gateway_tailnet.py` | 89 | 62 | 30% |
| `modules/media_monitor.py` | 53 | 40 | 25% |
| `modules/gateway_lan.py` | 17 | 13 | 24% |
| `music_client.py` | 77 | 61 | 21% |
| `gateway_cert.py` | 39 | 39 | 0% |
| `music-setup.py` | 42 | 42 | 0% |
| `app-recovery.py`, `host-recovery.py` | 4 each | 4 | 0% |

## Rust runtime

- 477 `#[test]` functions in `runtime/src/*.rs` (counted in the sources; the distro's
  cargo 1.75 cannot read the Cargo.lock v4, so `cargo test -- --list` was not run there).
- Every source file but `main.rs` has a `#[cfg(test)]` module.

## Lines

Counted with `wc -l` (no cloc on the distro); `runtime/target` and `__pycache__` excluded.

| What | Lines | Files |
| --- | ---: | ---: |
| Python production (`*.py` outside `tests/` and `lab/`) | 35 923 | 84 |
| Python `lab/` (the NAS lab) | 494 | |
| Rust production (`runtime/src/*.rs` before each file's `#[cfg(test)]`) | 13 277 | 25 |
| Rust tests (from `#[cfg(test)]` to the end of each file) | 26 013 | 24 |
| JavaScript (`dashboard/app.js`, `dashboard/sw.js`, `setup-gui/setup.js`) | 5 495 | 3 |
| Shell (`install.sh`, `uninstall.sh`) | 599 | 2 |
| Python tests (`tests/*.py`, before this PR's golden test) | 30 483 | 56 |
| Test harnesses (`tests/*.mjs`) | 193 | 2 |

Python tests before this PR: 1 477 `def test_` functions in 56 files.

## Golden files

`tests/test_golden_units.py` freezes 224 generated files in four configurations
(57 `defaults`, 53 `media-drive`, 57 `before-0.48.0`, 57 `past-midnight`); see README.md.
